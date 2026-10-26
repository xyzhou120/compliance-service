import unittest

from compliance_service.daily_clues import extract_daily_clues


def ev(eid, fid, text, role="actual_record", group=None, quality=None):
    x = {"evidence_id": eid, "file_id": fid, "text": text, "source_role": role}
    if group:
        x["material_group"] = group
    if quality:
        x["metadata"] = {"recognition_quality": quality}
    return x


FILES = [
    {"file_id": "d1", "role": "actual_record", "material_group": "prevention_daily"},
    {"file_id": "d2", "role": "actual_record", "material_group": "dispatch_daily"},
    {"file_id": "req", "role": "requirement", "material_group": "design"},
]


class DailyClueTests(unittest.TestCase):
    def test_level_label_is_only_a_candidate_and_not_event_confirmation(self):
        out = extract_daily_clues([
            ev("weak", "d1", "2026-10-14 403105工作面危险等级弱", quality="native"),
        ], FILES)
        self.assertEqual(1, len(out["clues"]))
        self.assertEqual("candidate", out["clues"][0]["certainty"])
        self.assertEqual("confirmed", out["clues"][0]["transcription_certainty"])
        self.assertEqual("candidate", out["clues"][0]["event_certainty"])

    def test_unrecorded_dynamic_phenomenon_is_not_an_occurrence(self):
        for phrase in ("未记录动力现象", "未找到动力现象记录", "未见煤炮", "未发生动力现象"):
            with self.subTest(phrase=phrase):
                out = extract_daily_clues([ev("negative", "d1", "2026-10-14 403105工作面" + phrase, quality="native")], FILES)
                self.assertEqual([], out["clues"])

    def test_missing_action_record_is_not_explicit_failure(self):
        for phrase in ("未找到撤人记录", "没有撤人记录", "未记录撤人情况", "未查到卸压记录"):
            with self.subTest(phrase=phrase):
                out = extract_daily_clues([ev("missing", "d2", "2026-10-14 403105工作面" + phrase, quality="native")], FILES)
                self.assertEqual("evidence_not_found", out["arrangements"][0]["state"])

    def test_same_clause_multiple_faces_or_dates_is_not_strongly_bound(self):
        for text in ("2026-10-14 403105工作面和403106工作面发生预警", "2026-10-14和2026-10-15 403105工作面发生预警"):
            with self.subTest(text=text):
                out = extract_daily_clues([ev("ambiguous", "d1", text, quality="native")], FILES)
                self.assertEqual("candidate", out["clues"][0]["certainty"])
                self.assertTrue(out["gaps"])

    def test_multi_day_faces_and_event_fields(self):
        out = extract_daily_clues([
            ev("e1", "d1", "2026年1月5日 403105工作面发生B级预警，事件编号 W-17，微震能量12kJ，持续处理中"),
            ev("e2", "d1", "2026-01-06 403106工作面出现动力现象，已解除"),
        ], FILES)
        self.assertEqual(["2026-01-05", "2026-01-06"], [x["date"] for x in out["clues"]])
        self.assertEqual(["403105工作面", "403106工作面"], [x["workface"] for x in out["clues"]])
        self.assertEqual("warning", out["clues"][0]["event_type"])
        self.assertEqual("W-17", out["clues"][0]["event_key"])
        self.assertTrue(out["clues"][0]["continues"])
        self.assertTrue(out["clues"][1]["closed"])

    def test_negative_and_generic_monitoring_are_not_clues(self):
        out = extract_daily_clues([
            ev("n1", "d1", "2026年1月5日 403105工作面未发生预警，无动力现象"),
            ev("n2", "d1", "冲击地压预警指标及监测制度"),
        ], FILES)
        self.assertEqual([], out["clues"])

    def test_arranged_not_arranged_and_planned_are_distinct(self):
        out = extract_daily_clues([
            ev("a1", "d2", "2026年1月5日 403105工作面已组织撤人并完成卸压施工"),
            ev("a2", "d2", "2026年1月6日 403105工作面未安排撤人，计划卸压"),
        ], FILES)
        states = {(x["date"], x["action"]): x["state"] for x in out["arrangements"]}
        self.assertEqual("arranged", states[("2026-01-05", "evacuate")])
        self.assertEqual("arranged", states[("2026-01-05", "relief")])
        self.assertEqual("not_arranged", states[("2026-01-06", "evacuate")])
        self.assertEqual("planned", states[("2026-01-06", "relief")])

    def test_requirement_does_not_create_arrangement_and_missing_binding_is_gap(self):
        out = extract_daily_clues([
            ev("r1", "req", "403105工作面应安排撤人和卸压", role="requirement"),
            ev("c1", "d1", "出现预警，已安排撤人", quality="independently_compared_candidate"),
        ], FILES)
        self.assertEqual(1, len(out["arrangements"]))
        self.assertEqual("candidate", out["arrangements"][0]["certainty"])
        self.assertEqual("candidate", out["clues"][0]["certainty"])
        self.assertTrue(any(g["kind"] == "clue_missing_binding" for g in out["gaps"]))
        self.assertTrue(any(g["kind"] == "arrangement_missing_binding" for g in out["gaps"]))


if __name__ == "__main__":
    unittest.main()
