"""Read the graded-threshold decision rule from the document itself.

Every mine writes its own standard ("c 及以上需立即撤人卸压", "以 c 为基准"), and the
user cannot enumerate them in advance. So the rule is parsed from the same table/
text the threshold came from, and when it cannot be read the grade comparison is
reported as unresolved instead of guessing.
"""

from __future__ import annotations

import re

GRADES = ("a", "b", "c", "d")

_ABOVE = re.compile(
    r"(?P<grade>[abcd])\s*(?:及|级)?\s*(?:以上|及以上)\s*[^\n]{0,24}?"
    r"(?:立即|马上|即刻)?\s*[^\n]{0,8}?(?P<action>撤人|卸压|撤出|停产)")
_BASELINE = re.compile(
    r"(?:以|按)\s*(?P<grade>[abcd])\s*(?:级)?\s*(?:为|作)\s*(?:基准|阈值|界限)")
_RANGE = re.compile(
    r"(?P<low>[abcd])\s*[、/和至-]\s*(?P<high>[abcd])\s*(?:级|档)?\s*[^\n]{0,16}?"
    r"(?:撤人|卸压)")


def _expand(grade: str) -> list:
    if grade not in GRADES:
        return []
    return list(GRADES[GRADES.index(grade):])


def _snippet(text: str, match) -> str:
    start = max(0, match.start() - 20)
    return text[start:match.end() + 20].strip()


def extract_grade_rule(texts) -> dict:
    """Return the rule declared by the document, with the evidence snippet."""
    result = {"baseline_grade": None, "immediate_grades": [], "evidence": None,
              "source": "document", "status": "unresolved"}
    for raw in texts or []:
        text = str(raw or "")
        if not text.strip():
            continue
        match = _BASELINE.search(text)
        if match and result["baseline_grade"] is None:
            result["baseline_grade"] = match.group("grade")
            result["evidence"] = _snippet(text, match)
            continue
        match = _ABOVE.search(text)
        if match:
            grade = match.group("grade")
            if result["baseline_grade"] is None:
                result["baseline_grade"] = grade
            for item in _expand(grade):
                if item not in result["immediate_grades"]:
                    result["immediate_grades"].append(item)
            result["evidence"] = result["evidence"] or _snippet(text, match)
            continue
        match = _RANGE.search(text)
        if match:
            for item in _expand(match.group("low")):
                if item not in result["immediate_grades"]:
                    result["immediate_grades"].append(item)
            result["baseline_grade"] = result["baseline_grade"] or match.group("low")
            result["evidence"] = result["evidence"] or _snippet(text, match)
    if result["baseline_grade"]:
        result["status"] = "declared"
        if not result["immediate_grades"]:
            result["immediate_grades"] = _expand(result["baseline_grade"])
    return result


def apply_rule(schema: dict, rule: dict | None) -> dict:
    """Document rule wins over the variant default; absent both stays unset."""
    effective = dict(schema)
    if rule and rule.get("baseline_grade"):
        effective["baseline_grade"] = rule["baseline_grade"]
        effective["over_threshold_grades"] = tuple(rule.get("immediate_grades") or
                                                   [rule["baseline_grade"]])
        effective["immediate_action_grades"] = tuple(rule.get("immediate_grades") or
                                                     [rule["baseline_grade"]])
        effective["baseline_source"] = "document"
    elif schema.get("baseline_grade"):
        effective["baseline_source"] = "variant"
    else:
        effective["baseline_source"] = "unset"
    return effective
