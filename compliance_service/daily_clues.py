"""Extract warning clues and evacuation/relief arrangements from daily records.

This module deliberately stays conservative: it only turns text into a clue when
the record itself names a warning/dynamic event.  It does not infer a missing
arrangement from a missing sentence and never treats a requirement as execution.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date as calendar_date
from typing import Any, Iterable


_DATE = re.compile(r"(?P<y>20\d{2})[年\-/\.](?P<m>\d{1,2})[月\-/\.](?P<d>\d{1,2})日?")
_FACE = re.compile(r"(?<!\d)(?P<face>\d{3,6})\s*(?:工作面|采面|面)")
_EVENT_KEY = re.compile(r"(?:(?:预警|危险|事件)(?:编号|ID)|event_id)[\s#：:]*([A-Za-z0-9][A-Za-z0-9_-]*)", re.I)
_VALUE = re.compile(r"(?P<metric>[\u4e00-\u9fffA-Za-z]{1,12})[^\d]{0,12}(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>MPa|kJ|J|mJ|次|吨|t|m|米|mm|\u4e07)?", re.I)

_NEGATIVE = re.compile(r"(?:未发生|未出现|没有|无|未发现|未见|未达到|未超|无明显|未记录|未找到|未查到)(?:.{0,14})(?:预警|危险|动力|冲击|煤炮|震动)")
_WARNING = re.compile(r"(?:出现|发生|触发|达到|超过|达到临界|发布|升级|进入|存在).{0,14}(?:预警|危险)|(?:预警|危险)(?:线索|信号|等级|事件|状态)")
_OCCURRENCE = re.compile(r"(?:出现|发生|触发|发布|升级|进入).{0,14}(?:预警|动力现象|冲击|煤炮|震动)|(?:预警|动力)(?:事件|记录)[：:]")
_DYNAMIC = re.compile(r"(?:动力现象|冲击地压显现|煤炮|顶板[^。；]{0,8}(?:来压|显现)|震动|破坏现象)")
_GENERIC = re.compile(r"(?:预警指标|监测预警指标|预警临界指标|建立.*监测|进行.*监测)")


def _date(text: str) -> str | None:
    m = _DATE.search(text or "")
    if not m:
        return None
    try:
        return calendar_date(int(m['y']), int(m['m']), int(m['d'])).isoformat()
    except ValueError:
        return None


def _title_date(name: str, observation: dict[str, Any] | None) -> str | None:
    """Read a daily-report title date at day granularity (never a time)."""
    full = _date(name)
    if full:
        return full
    m = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", name or "")
    if not m:
        return None
    year = None
    if observation:
        period = observation.get("period") or observation.get("check_cycle")
        if isinstance(period, dict):
            year = str(period.get("start", ""))[:4]
        elif isinstance(period, str) and re.match(r"20\d{2}", period):
            year = period[:4]
        elif re.match(r"20\d{2}", str(observation.get("start") or "")):
            year = str(observation["start"])[:4]
    if not year:
        return None
    return f"{int(year):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"


def _records(evidence):
    """Keep clauses separate so one face's negation cannot hide another event."""
    for source in evidence:
        text = str(source.get("text") or "")
        dates = {_date(match.group()) for match in _DATE.finditer(text)} - {None}
        faces = {f"{match['face']}工作面" for match in _FACE.finditer(text)}
        for sentence in re.split(r"[。；;\r\n]+", text):
            if not sentence.strip():
                continue
            metadata = dict(source.get("metadata") or {})
            if len(dates) == 1:
                metadata["daily_date_context"] = next(iter(dates))
            if len(faces) == 1:
                metadata["daily_face_context"] = next(iter(faces))
            yield {**source, "text": sentence.strip(), "metadata": metadata}


def _face(text: str) -> str | None:
    m = _FACE.search(text or "")
    return f"{m['face']}工作面" if m else None


def _file_map(files: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(f.get("file_id")): f for f in files if f.get("file_id")}


def _quality(e: dict[str, Any]) -> str:
    metadata = e.get("metadata") or {}
    q = metadata.get("recognition_quality")
    if q in {"native", "independently_compared_candidate", "confirmed"}:
        return "confirmed"
    # Registered native parser blocks carry a locator/kind (and usually
    # ``has_images``); an unlabelled provider block must remain a candidate.
    if q is None and (metadata.get("has_images") is not None or e.get("kind") in {"paragraph", "table_row", "sheet", "page"}):
        return "confirmed"
    return "candidate"


def _refs(e: dict[str, Any]) -> list[str]:
    return [str(e.get("evidence_id") or e.get("block_id"))] if (e.get("evidence_id") or e.get("block_id")) else []


def _event_key(text: str) -> str | None:
    m = _EVENT_KEY.search(text or "")
    return m.group(1) if m else None


def _details(text: str) -> dict[str, Any]:
    d: dict[str, Any] = {}
    method = re.search(r"微震|地音|应力(?:在线)?|钻屑|电磁辐射", text)
    if method:
        d["method"] = method.group(0)
    level = re.search(r"([A-Ca-c])\s*级(?:预警|危险)?", text)
    if level:
        d["level"] = level.group(1).upper()
    v = _VALUE.search(text)
    if v:
        d.update(metric=v.group("metric"), value=float(v.group("value")), unit=v.group("unit") or None)
    q = re.sub(r"\s+", " ", text).strip()
    if q:
        d["quote"] = q[:500]
    return d


def extract_daily_clues(evidence: Iterable[dict[str, Any]], files: Iterable[dict[str, Any]], *, observation: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return ``clues``, execution ``arrangements`` and explicit binding gaps.

    ``evidence`` contains registered blocks.  OCR candidates are accepted only
    after the service marks them ``independently_compared_candidate``.
    """
    fmap = _file_map(files)
    clues: list[dict[str, Any]] = []
    arrangements: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    clue_i = arrange_i = 0
    for e in _records(evidence):
        f = fmap.get(str(e.get("file_id")), {})
        role = e.get("source_role") or f.get("role")
        group = e.get("material_group") or f.get("material_group")
        if role != "actual_record":
            continue
        quality = _quality(e)
        text = str(e.get("text") or "")
        if not text.strip():
            continue
        meta = e.get("metadata") or {}
        clause_dates = {_date(match.group()) for match in _DATE.finditer(text)} - {None}
        clause_faces = {match['face'] for match in _FACE.finditer(text)}
        file_metadata = f.get("source_metadata") or {}
        action_layer = str(meta.get("action_layer") or file_metadata.get("action_layer") or "")
        if not action_layer:
            action_layer = "instruction" if group == "release" else "execution"
        date = (_date(text) or meta.get("daily_date_context") or f.get("report_date")
                or file_metadata.get("report_date")
                or _title_date(str(f.get("original_name") or f.get("source_path") or ""), observation))
        face = _face(text) or meta.get("daily_face_context")
        # A single clause naming several dates/faces has no unique binding.
        if len(clause_dates) > 1:
            date = None
        if len(clause_faces) > 1:
            face = None
        refs = _refs(e)
        # A warning in a daily report is the only event entry point.  Generic
        # threshold/monitoring prose and explicit negation are excluded.
        is_negative = bool(_NEGATIVE.search(text))
        is_dynamic = bool(_DYNAMIC.search(text)) and not is_negative
        is_warning = bool(_WARNING.search(text)) and not is_negative and not (_GENERIC.search(text) and not re.search(r"(?:发生|出现|触发|达到|超过|发布|升级)", text))
        if group == "prevention_daily" and (is_warning or is_dynamic):
            clue_i += 1
            event_type = "dynamic" if is_dynamic and not is_warning else "warning"
            missing = [k for k, v in (("date", date), ("workface", face)) if not v]
            item = {
                "clue_id": f"clue-{clue_i:04d}", "date": date, "workface": face,
                "event_type": event_type, "event_key": _event_key(text),
                "continuation_key": (_event_key(text) if re.search(r"持续|连续|延续", text) else None),
                "continues": bool(re.search(r"持续|连续|延续", text)),
                "closed": bool(re.search(r"解除|结束|消除|恢复正常", text)),
                "certainty": "candidate" if missing or not _OCCURRENCE.search(text) else quality,
                "transcription_certainty": quality,
                "event_certainty": "candidate" if missing or not _OCCURRENCE.search(text) else quality,
                "evidence_refs": refs, "source_file_id": e.get("file_id"),
                "details": _details(text),
            }
            clues.append(item)
            if missing:
                gaps.append({"kind": "clue_missing_binding", "missing": missing, "evidence_refs": refs, "source_file_id": e.get("file_id"), "reason": "日报线索缺少明确日期或工作面，不能跨文件关联"})
        # Arrangements are execution records from any actual material.  A
        # requirement/approved plan cannot become an arranged action here.
        action_specs = (("evacuate", r"撤人|撤离|人员撤出|停工撤"), ("relief", r"卸压|解危|卸压施工|卸压钻孔"))
        for action, terms in action_specs:
            if not re.search(terms, text):
                continue
            missing_record = bool(re.search(r"(?:未找到|没有找到|未查到|未记录|没有记录|无记录)[^，,。；;]{0,8}(?:" + terms + r")|(?:没有|无)[^，,。；;]{0,8}(?:" + terms + r")[^，,。；;]{0,4}(?:记录|证据|材料)", text))
            neg = bool(re.search(r"(?:未|没有|无|未曾|尚未)[^，,。；;]{0,8}(?:" + terms + r")", text))
            done = bool(re.search(r"(?:已|完成|落实|组织|人员.*(?:撤出|撤离)|施工完成).{0,8}(?:" + terms + r")", text))
            planned = bool(re.search(r"(?:计划|拟|准备|要求|应当|需|将要).{0,8}(?:" + terms + r")", text))
            state = "evidence_not_found" if missing_record else ("not_arranged" if neg else ("arranged" if done else ("planned" if planned else "unknown")))
            arrange_i += 1
            arrangements.append({
                "arrangement_id": f"arrangement-{arrange_i:04d}", "date": date, "workface": face,
                "action": action, "state": state, "event_key": _event_key(text),
                "certainty": "candidate" if not date or not face else quality, "evidence_refs": refs, "source_file_id": e.get("file_id"),
                "action_layer": ("instruction" if re.search(r"(?:安排|指令|通知|要求)[^，,。；;]{0,8}(?:" + terms + r")", text) else action_layer),
                "details": {"quote": re.sub(r"\s+", " ", text).strip()[:500]},
            })
            if not date or not face:
                gaps.append({"kind": "arrangement_missing_binding", "missing": [k for k,v in (("date",date),("workface",face)) if not v], "evidence_refs": refs, "source_file_id": e.get("file_id"), "reason": "安排记录缺少日期或工作面，暂不能关联线索"})
    return {"clues": clues, "arrangements": arrangements, "gaps": gaps}

