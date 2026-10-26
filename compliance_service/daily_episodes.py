"""Daily warning clue aggregation for the stop-work/unloading check.

The extractor is deliberately kept separate from this module.  It receives
candidate clues and arrangements with evidence references and produces a
deterministic, evidence-bounded description of daily issues.  In particular,
absence of an arrangement is never upgraded to an explicit violation merely
because no matching record was supplied.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import hashlib
import re
import unicodedata
from typing import Any, Iterable, Mapping


_ACTION_ALIASES = {
    "evacuation": ("evacuation", "personnel_evacuation", "撤人", "撤离", "人员撤出", "撤出人员", "stop_work"),
    "unloading": ("unloading", "pressure_relief", "卸压", "卸压措施", "卸压施工", "relief"),
}


def _value(record: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = record.get(name)
        if value not in (None, ""):
            return value
    return None


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    # Accept common spreadsheet date/time representations while preserving the
    # original value in evidence_refs supplied by the caller.
    match = re.search(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return text[:10] if re.match(r"^\d{4}-\d\d-\d\d", text) else text


def _parse_date(value: Any) -> date | None:
    text = _date_text(value)
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).strip())


def _workface(record: Mapping[str, Any]) -> str | None:
    value = _value(record, "workface", "work_face", "working_face", "工作面", "面名")
    if value is None:
        return None
    text = _norm_text(value)
    # These suffixes are presentation noise, not a different working face.
    return re.sub(r"(工作面|采面)$", "", text) or text


def _refs(record: Mapping[str, Any]) -> list[Any]:
    refs = _value(record, "evidence_refs", "evidence", "source_refs", "证据定位")
    if refs is None:
        return []
    if isinstance(refs, (str, int)):
        return [refs]
    return list(refs)


def _text(record: Mapping[str, Any]) -> str:
    values = (_value(record, "text", "content", "description", "statement", "原文", "事实") or "",)
    return " ".join(str(v) for v in values if v)


def _action_kind(record: Mapping[str, Any]) -> str | None:
    raw = _value(record, "action", "action_type", "arrangement_type", "kind", "类型", "安排类型")
    text = _norm_text(raw) + _norm_text(_text(record))
    for kind, aliases in _ACTION_ALIASES.items():
        if any(_norm_text(alias) in text for alias in aliases):
            return kind
    return None


def _state(record: Mapping[str, Any]) -> str:
    raw = _value(record, "state", "status", "arrangement_state", "result", "状态", "结论")
    text = _norm_text(raw) + _norm_text(_text(record))
    canonical = _norm_text(raw).lower()
    if canonical == "evidence_not_found" or re.search(r"未找到|未查到|未记录|没有.{0,8}记录|无.{0,8}记录", text):
        return "evidence_not_found"
    if canonical in {"explicit_not_arranged", "not_arranged", "未安排", "未执行", "未落实"}:
        return "explicit_not_arranged"
    if canonical in {"planned_only", "planned", "计划", "拟"}:
        return "planned_only"
    if canonical in {"arranged", "executed", "completed", "已安排", "已执行", "已落实"}:
        return "arranged"
    if any(token in text for token in ("未安排", "未执行", "未落实", "未组织", "没有撤人", "未撤人", "未卸压", "没有卸压")):
        return "explicit_not_arranged"
    if any(token in text for token in ("计划", "拟", "应当", "安排中", "待执行")):
        return "planned_only"
    if any(token in text for token in ("已安排", "已执行", "已落实", "已组织", "完成", "正常", "符合")):
        return "arranged"
    return "unknown"


def _issue_code(kind: str, state: str) -> str:
    suffix = {
        "explicit_not_arranged": "not_arranged",
        "evidence_not_found": "evidence_not_found",
        "planned_only": "planned_only",
        "unknown": "unknown",
    }[state]
    return f"{kind}_{suffix}"


def _source_conflicts(source_files: Iterable[Mapping[str, Any]]) -> dict[Any, dict[str, Any]]:
    """Return conflict metadata indexed by date and by source file id.

    Two versions are retained.  A conflict is raised only when the supplied
    metadata establishes two source identities (or two hashes for one name),
    rather than treating two unrelated files on one date as duplicates.
    """
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for source in source_files or ():
        metadata = source.get("source_metadata") or {}
        # Source attachment dates describe the delivery file, not the event.
        # Version grouping is allowed only with an explicit report_date.
        day = _date_text(_value(source, "report_date") or metadata.get("report_date"))
        group = _value(source, "material_group", "file_group", "文件组", "清单") or metadata.get("material_group")
        mine = _value(source, "mine_code", "mine", "矿井编码") or metadata.get("mine_code") or metadata.get("mine") or ""
        if not day or not group:
            continue
        groups[(mine, day, _norm_text(group))].append(source)
    conflicts: dict[Any, dict[str, Any]] = {}
    for key, records in groups.items():
        ids = {_value(r, "file_id", "id", "attachment_id") for r in records}
        ids.discard(None)
        by_name: dict[str, set[Any]] = defaultdict(set)
        for record in records:
            name = _value(record, "attachment_name", "filename", "name", "文件名")
            digest = _value(record, "content_hash", "sha256", "hash", "内容哈希")
            if name is not None:
                by_name[_norm_text(name)].add(digest or _value(record, "file_id", "id"))
        same_name_different_hash = any(len(hashes) > 1 for hashes in by_name.values())
        hashes = [_value(r, "content_hash", "sha256", "hash", "内容哈希") for r in records]
        all_known_hashes_same = len(hashes) == len(records) and all(hashes) and len(set(hashes)) == 1
        # Multiple attachment ids are not a version conflict when every
        # supplied content hash is known and identical (the duplicate bytes
        # are still retained in ``versions`` below).
        if len(ids) < 2 and not same_name_different_hash:
            continue
        day = key[1]
        info = {"date": day, "mine_code": key[0], "file_group": key[2], "source_refs": [], "file_ids": [],
                "version_conflict": not all_known_hashes_same,
                "versions": [dict(record) for record in records]}
        for record in records:
            info["source_refs"].extend(_refs(record))
            file_id = _value(record, "file_id", "id", "attachment_id")
            if file_id is not None:
                info["file_ids"].append(file_id)
        info["source_refs"] = list(dict.fromkeys(info["source_refs"]))
        info["file_ids"] = list(dict.fromkeys(info["file_ids"]))
        if day:
            scope_key = (key[0], day)
            existing = conflicts.get(scope_key)
            if existing:
                existing["source_refs"] = list(dict.fromkeys(existing["source_refs"] + info["source_refs"]))
                existing["file_ids"] = list(dict.fromkeys(existing["file_ids"] + info["file_ids"]))
                existing["file_groups"] = list(dict.fromkeys(existing.get("file_groups", [existing.get("file_group", "")]) + [info["file_group"]]))
                existing["file_group"] = existing["file_groups"][0] if len(existing["file_groups"]) == 1 else "multiple"
                existing["versions"].extend(info["versions"])
                existing["version_conflict"] = existing["version_conflict"] or info["version_conflict"]
            else:
                info["file_groups"] = [info["file_group"]]
                conflicts[scope_key] = info
        for record in records:
            file_id = _value(record, "file_id", "id", "attachment_id")
            if file_id is not None:
                conflicts[f"file:{file_id}"] = info
    return conflicts


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = "|".join(str(part or "") for part in parts)
    return f"{prefix}-{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12]}"


def aggregate_daily_findings(
    clues: Iterable[Mapping[str, Any]],
    arrangements: Iterable[Mapping[str, Any]],
    source_files: Iterable[Mapping[str, Any]],
    observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build daily issues and cautiously merge explicitly continued episodes.

    ``clues`` are warning/trigger candidates, while ``arrangements`` are
    positive, negative, planned, or ambiguous records.  The function never
    invents a missing event or treats a missing record as explicit denial.
    """
    conflicts = _source_conflicts(source_files)
    arrangement_records = list(arrangements or ())
    start = _parse_date(_value(observation or {}, "start", "date_start"))
    end = _parse_date(_value(observation or {}, "end", "date_end"))

    normalized_clues: list[dict[str, Any]] = []
    for raw in clues or ():
        day = _date_text(_value(raw, "date", "clue_date", "event_date", "time", "日期"))
        parsed = _parse_date(day)
        if not day or (start and parsed and parsed < start) or (end and parsed and parsed > end):
            continue
        face = _workface(raw)
        event_type = _value(raw, "event_type", "trigger_type", "事件类型") or "warning"
        # event_key is an explicit identifier when supplied.  A plain event
        # type is intentionally not inferred as a key, so adjacent warnings
        # remain separate unless the extractor establishes continuity.
        event_id = _value(raw, "event_id", "event_key_id", "event_key", "事件标识")
        continuation = _value(raw, "continuation_key", "episode_key", "continuation_id", "连续事件标识")
        normalized_clues.append({
            "raw": raw, "date": day, "parsed_date": parsed, "workface": face,
            "event_type": str(event_type), "event_id": event_id,
            "continuation_key": continuation, "refs": _refs(raw),
            "text": _text(raw), "certainty": raw.get("certainty", "confirmed"),
            "closed": bool(raw.get("closed", False)), "mine_code": _value(raw, "mine_code", "mine", "矿井编码") or "",
        })
    normalized_clues.sort(key=lambda item: (item["date"], item["workface"] or "", item["event_type"], str(item["event_id"] or "")))
    # One OCR page can emit the same warning title, level and coefficient as
    # separate candidate blocks. Keep one daily clue for one source block; the
    # original quote remains inside the clue's raw/details payload.
    deduped: list[dict[str, Any]] = []
    seen_clues: set[tuple[Any, ...]] = set()
    for clue in normalized_clues:
        key = (clue["date"], clue["workface"], clue["event_type"], clue["event_id"], tuple(clue["refs"]))
        if key in seen_clues:
            continue
        seen_clues.add(key)
        deduped.append(clue)
    normalized_clues = deduped
    counts: dict[tuple[Any, ...], int] = defaultdict(int)
    for clue in normalized_clues:
        counts[(clue["mine_code"], clue["date"], clue["workface"])] += 1

    def matching_arrangements(clue: Mapping[str, Any]) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for raw in arrangement_records:
            day = _date_text(_value(raw, "date", "arrangement_date", "event_date", "time", "日期"))
            face = _workface(raw)
            if not face or not clue["workface"] or face != clue["workface"]:
                continue
            clue_id = clue["event_id"] or clue["continuation_key"]
            arrangement_id = _value(raw, "event_id", "event_key", "continuation_key", "episode_key", "事件标识")
            if counts[(clue["mine_code"], clue["date"], clue["workface"])] > 1 and not (clue_id and arrangement_id and clue_id == arrangement_id):
                continue  # Same-day events cannot borrow an unassigned action.
            if clue_id and arrangement_id and clue_id != arrangement_id:
                continue
            if day != clue["date"] and not (clue_id and arrangement_id and clue_id == arrangement_id):
                continue
            mine = _value(raw, "mine_code", "mine", "矿井编码") or ""
            if clue["mine_code"] and mine and mine != clue["mine_code"]:
                continue
            kind = _action_kind(raw)
            if kind:
                state = _state(raw)
                # Candidate OCR/VLM evidence cannot establish either an
                # arranged or explicitly-not-arranged state.
                if str(_value(raw, "certainty", "evidence_certainty") or "").lower() == "candidate":
                    state = "unknown"
                matches.append({"raw": raw, "kind": kind, "state": state, "refs": _refs(raw)})
        return matches

    issues: list[dict[str, Any]] = []
    for clue in normalized_clues:
        matches = matching_arrangements(clue)
        by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for match in matches:
            by_kind[match["kind"]].append(match)
        day_conflict = conflicts.get((clue["mine_code"], clue["date"]))
        clue_file_ids = _value(clue["raw"], "file_id", "source_file_id")
        if clue_file_ids is None:
            clue_file_ids = []
        elif isinstance(clue_file_ids, (str, int)):
            clue_file_ids = [clue_file_ids]
        file_conflicts = [conflicts[f"file:{fid}"] for fid in clue_file_ids if f"file:{fid}" in conflicts]
        source_info = day_conflict or (file_conflicts[0] if file_conflicts else None)
        conflict_info = source_info if source_info and source_info.get("version_conflict") else None
        problem_codes: list[str] = []
        states: dict[str, str] = {}
        action_conflicts: list[str] = []
        refs = list(clue["refs"])
        for kind in ("evacuation", "unloading"):
            records = by_kind.get(kind, [])
            state = "evidence_not_found" if not records else "unknown"
            if records:
                ranked = {record["state"] for record in records}
                if "explicit_not_arranged" in ranked and "arranged" in ranked:
                    state = "unknown"
                    action_conflicts.append(kind)
                elif "explicit_not_arranged" in ranked:
                    state = "explicit_not_arranged"
                elif "arranged" in ranked:
                    state = "arranged"
                elif "planned_only" in ranked:
                    state = "planned_only"
                elif ranked == {"evidence_not_found"}:
                    state = "evidence_not_found"
            states[kind] = state
            if state != "arranged":
                problem_codes.append(_issue_code(kind, state))
            for record in records:
                refs.extend(record["refs"])
        if source_info:
            refs.extend(source_info.get("source_refs", []))
        refs = list(dict.fromkeys(refs))
        clue_identity = clue["event_id"] or clue["continuation_key"] or tuple(refs)
        issue_id = _stable_id("issue", clue["mine_code"], clue["date"], clue["workface"], clue["event_type"], clue_identity)
        if conflict_info or action_conflicts:
            certainty = "unverifiable"
        elif clue["certainty"] != "confirmed":
            certainty = "candidate" if clue["certainty"] == "candidate" else "unverifiable"
        elif any(state == "explicit_not_arranged" for state in states.values()):
            certainty = "confirmed"
        elif any(state == "planned_only" for state in states.values()):
            certainty = "candidate"
        else:
            certainty = "unverifiable"
        descriptions = []
        for kind, label in (("evacuation", "撤人"), ("unloading", "卸压")):
            state = states[kind]
            if state == "explicit_not_arranged":
                descriptions.append(f"明确未安排{label}")
            elif state == "evidence_not_found":
                descriptions.append(f"本次资料未找到{label}安排证据")
            elif state == "planned_only":
                descriptions.append(f"仅发现{label}计划，未确认已执行")
            elif state == "unknown":
                descriptions.append(f"{label}安排状态无法确认")
        if conflict_info:
            descriptions.append("存在多版本资料（同一资料范围存在多个版本），暂缓确定性判断")
        if action_conflicts:
            descriptions.append("同一动作存在已安排与未安排的相互冲突证据，暂缓确定性判断")
        issues.append({
            "issue_id": issue_id, "date_start": clue["date"], "date_end": clue["date"],
            "workface": clue["workface"], "event_type": clue["event_type"],
            "problem_codes": problem_codes, "description": f"{clue['date']}发现{clue['event_type']}线索；" + "；".join(descriptions),
            "evidence_refs": refs, "arrangement_states": states, "certainty": certainty,
            "version_conflict": bool(conflict_info), "episode_id": None,
            "event_id": clue["event_id"], "continuation_key": clue["continuation_key"],
            "mine_code": clue["mine_code"],
            "closed": clue["closed"], "arrangement_conflicts": action_conflicts,
            "source_versions": source_info.get("versions", []) if source_info else [],
        })

    # Merge only a supplied continuation/event identifier and consecutive days.
    episode_groups: list[list[dict[str, Any]]] = []
    keyed: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        key = issue.get("continuation_key") or issue.get("event_id")
        if key is not None and issue["workface"]:
            keyed[(issue["mine_code"], issue["workface"], issue["event_type"], str(key))].append(issue)
        else:
            episode_groups.append([issue])
    for group in keyed.values():
        group.sort(key=lambda item: item["date_start"])
        run: list[dict[str, Any]] = []
        for issue in group:
            previous = _parse_date(run[-1]["date_end"]) if run else None
            current = _parse_date(issue["date_start"])
            if run and (run[-1].get("closed") or not previous or not current or current != previous + timedelta(days=1)):
                episode_groups.append(run)
                run = []
            run.append(issue)
        if run:
            episode_groups.append(run)
    episodes: list[dict[str, Any]] = []
    for group in sorted(episode_groups, key=lambda entries: entries[0]["date_start"]):
        episode_id = _stable_id("episode", group[0]["mine_code"], group[0]["workface"], group[0]["event_type"], group[0].get("continuation_key") or group[0].get("event_id") or group[0]["issue_id"], group[0]["date_start"])
        refs = list(dict.fromkeys(ref for issue in group for ref in issue["evidence_refs"]))
        for issue in group:
            issue["episode_id"] = episode_id
        episodes.append({
            "episode_id": episode_id, "date_start": group[0]["date_start"], "date_end": group[-1]["date_end"],
            "dates": [issue["date_start"] for issue in group], "workface": group[0]["workface"],
            "event_type": group[0]["event_type"], "issue_ids": [issue["issue_id"] for issue in group],
            "description": "；".join(issue["description"] for issue in group),
            "evidence_refs": refs, "version_conflict": any(issue["version_conflict"] for issue in group),
            "certainty": "unverifiable" if any(issue["certainty"] == "unverifiable" for issue in group) else ("confirmed" if all(issue["certainty"] == "confirmed" for issue in group) else "candidate"),
        })
    gaps: list[str] = []
    if not normalized_clues:
        gaps.append("未识别到带日期的防冲日报预警线索")
    if any(issue["version_conflict"] for issue in issues):
        gaps.append("存在多版本资料，相关问题暂缓确定性判断")
    summary = {
        "clue_count": len(normalized_clues), "issue_count": len(issues), "episode_count": len(episodes),
        "confirmed_count": sum(issue["certainty"] == "confirmed" for issue in issues),
        "candidate_count": sum(issue["certainty"] == "candidate" for issue in issues),
        "unverifiable_count": sum(issue["certainty"] == "unverifiable" for issue in issues),
        "version_conflict_count": sum(issue["version_conflict"] for issue in issues),
    }
    return {"issues": issues, "episodes": episodes, "gaps": gaps, "summary": summary}


__all__ = ["aggregate_daily_findings"]
