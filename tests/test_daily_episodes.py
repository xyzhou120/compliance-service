import unittest

from compliance_service.daily_episodes import aggregate_daily_findings


def clue(day, face="403105", event_type="预警", **extra):
    return {"date": day, "workface": face, "event_type": event_type,
            "evidence_refs": [f"日报:{day}"], **extra}


def arrangement(day, kind, state=None, face="403105", **extra):
    item = {"date": day, "workface": face, "action_type": kind,
            "evidence_refs": [f"安排:{day}:{kind}"]}
    if state:
        item["state"] = state
    item.update(extra)
    return item


class DailyEpisodeTests(unittest.TestCase):
    def test_missing_record_language_cannot_become_explicit_negative(self):
        result = aggregate_daily_findings([clue("2026-10-14")],
            [arrangement("2026-10-14", "撤人", "evidence_not_found", text="未找到撤人记录")], [])
        issue = result["issues"][0]
        self.assertEqual("evidence_not_found", issue["arrangement_states"]["evacuation"])
        self.assertNotEqual("confirmed", issue["certainty"])

    def test_same_day_unidentified_event_does_not_share_arrangements(self):
        result = aggregate_daily_findings([
            clue("2026-10-14", evidence_refs=["first"]),
            clue("2026-10-14", evidence_refs=["second"]),
        ], [arrangement("2026-10-14", "撤人", "未安排")], [])
        self.assertTrue(all(issue["certainty"] != "confirmed" for issue in result["issues"]))
        self.assertTrue(all(issue["arrangement_states"]["evacuation"] == "evidence_not_found" for issue in result["issues"]))

    def test_same_day_distinct_unidentified_events_have_distinct_episode_ids(self):
        result = aggregate_daily_findings([
            clue("2026-10-14", evidence_refs=["first"]),
            clue("2026-10-14", evidence_refs=["second"]),
        ], [], [])
        self.assertEqual(2, len({episode["episode_id"] for episode in result["episodes"]}))

    def test_explicit_missing_and_absent_are_distinguished(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05")],
            [arrangement("2026-01-05", "撤人", "未安排")],
            [],
        )
        issue = result["issues"][0]
        self.assertEqual(issue["arrangement_states"], {
            "evacuation": "explicit_not_arranged", "unloading": "evidence_not_found"})
        self.assertIn("evacuation_not_arranged", issue["problem_codes"])
        self.assertIn("明确未安排撤人", issue["description"])
        self.assertIn("本次资料未找到卸压安排证据", issue["description"])
        self.assertEqual(issue["certainty"], "confirmed")

    def test_planned_only_is_candidate(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05")],
            [arrangement("2026-01-05", "卸压", "计划")], [],
        )
        issue = result["issues"][0]
        self.assertEqual(issue["arrangement_states"]["unloading"], "planned_only")
        self.assertEqual(issue["certainty"], "candidate")
        self.assertIn("unloading_planned_only", issue["problem_codes"])

    def test_without_key_adjacent_days_stay_separate(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05"), clue("2026-01-06")], [], [],
        )
        self.assertEqual(result["summary"]["episode_count"], 2)
        self.assertEqual([e["dates"] for e in result["episodes"]], [["2026-01-05"], ["2026-01-06"]])

    def test_explicit_continuation_merges_only_consecutive_dates(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05", continuation_key="E1"),
             clue("2026-01-06", continuation_key="E1"),
             clue("2026-01-08", continuation_key="E1")], [], [],
        )
        self.assertEqual([e["dates"] for e in result["episodes"]],
                         [["2026-01-05", "2026-01-06"], ["2026-01-08"]])
        self.assertEqual(result["issues"][0]["episode_id"], result["issues"][1]["episode_id"])
        self.assertNotEqual(result["issues"][1]["episode_id"], result["issues"][2]["episode_id"])

    def test_event_key_is_explicit_identifier_but_event_type_is_not(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05", event_key="E1"), clue("2026-01-06", event_key="E1"),
             clue("2026-01-07")], [], [],
        )
        self.assertEqual(result["summary"]["episode_count"], 2)
        self.assertEqual(result["issues"][0]["episode_id"], result["issues"][1]["episode_id"])
        self.assertNotEqual(result["issues"][1]["episode_id"], result["issues"][2]["episode_id"])

    def test_version_conflict_keeps_all_versions_and_suspends_certainty(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05")],
            [arrangement("2026-01-05", "撤人", "未安排")],
            [
                {"report_date": "2026-01-05", "file_group": "调度日报", "file_id": "a",
                 "attachment_name": "日报.pdf", "content_hash": "h1", "evidence_refs": ["file:a"]},
                {"report_date": "2026-01-05", "file_group": "调度日报", "file_id": "b",
                 "attachment_name": "日报.pdf", "content_hash": "h2", "evidence_refs": ["file:b"]},
            ],
        )
        issue = result["issues"][0]
        self.assertTrue(issue["version_conflict"])
        self.assertEqual(issue["certainty"], "unverifiable")
        self.assertIn("file:a", issue["evidence_refs"])
        self.assertIn("file:b", issue["evidence_refs"])
        self.assertIn("存在多版本资料", issue["description"])

    def test_workface_normalization_and_observation_filter(self):
        result = aggregate_daily_findings(
            [clue("2026年1月5日", face=" ４０３１０５工作面 "), clue("2026-01-10")],
            [], [], {"start": "2026-01-01", "end": "2026-01-06"},
        )
        self.assertEqual(result["summary"]["clue_count"], 1)
        self.assertEqual(result["issues"][0]["workface"], "403105")

    def test_arrangement_requires_face_and_date_or_matching_event_key(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05", event_id="E1")],
            [arrangement("2026-01-04", "撤人", "未安排", face=""),
             arrangement("2026-01-04", "卸压", "未安排", face="403105", event_id="OTHER"),
             arrangement("2026-01-04", "卸压", "未安排", face="403105", event_id="E1")],
            [],
        )
        states = result["issues"][0]["arrangement_states"]
        self.assertEqual(states["evacuation"], "evidence_not_found")
        self.assertEqual(states["unloading"], "explicit_not_arranged")

    def test_candidate_clue_cannot_be_confirmed(self):
        item = clue("2026-01-05", certainty="candidate")
        result = aggregate_daily_findings([item], [arrangement("2026-01-05", "撤人", "未安排")], [],)
        self.assertEqual(result["issues"][0]["certainty"], "candidate")

    def test_arranged_and_not_arranged_conflict_is_unknown(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05")],
            [arrangement("2026-01-05", "撤人", "已安排"), arrangement("2026-01-05", "撤人", "未安排")], [],
        )
        issue = result["issues"][0]
        self.assertEqual(issue["arrangement_states"]["evacuation"], "unknown")
        self.assertIn("evacuation_unknown", issue["problem_codes"])
        self.assertEqual(issue["certainty"], "unverifiable")
        self.assertTrue(issue["arrangement_conflicts"])

    def test_closed_clue_cuts_continuation_episode(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05", continuation_key="E1", closed=True), clue("2026-01-06", continuation_key="E1")], [], [],
        )
        self.assertEqual(result["summary"]["episode_count"], 2)

    def test_source_conflict_requires_metadata_date_and_group(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05")], [],
            [{"file_id": "a", "evidence_refs": ["a"]}, {"file_id": "b", "evidence_refs": ["b"]}],
        )
        self.assertFalse(result["issues"][0]["version_conflict"])

    def test_candidate_arrangement_cannot_be_definitive(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05")],
            [arrangement("2026-01-05", "撤人", "未安排", certainty="candidate")], [],
        )
        issue = result["issues"][0]
        self.assertEqual(issue["arrangement_states"]["evacuation"], "unknown")
        self.assertEqual(issue["certainty"], "unverifiable")
        self.assertIn("安排状态无法确认", issue["description"])

    def test_same_day_distinct_clues_have_distinct_issue_ids(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05", event_id="E1", evidence_refs=["日报:E1"]),
             clue("2026-01-05", event_id="E2", evidence_refs=["日报:E2"])], [], [],
        )
        self.assertEqual(len({issue["issue_id"] for issue in result["issues"]}), 2)

    def test_identical_hash_attachments_are_retained_without_version_conflict(self):
        result = aggregate_daily_findings(
            [clue("2026-01-05")], [],
            [{"report_date": "2026-01-05", "material_group": "调度日报", "file_id": "a",
              "attachment_name": "日报.pdf", "content_hash": "same", "evidence_refs": ["a"]},
             {"report_date": "2026-01-05", "material_group": "调度日报", "file_id": "b",
              "attachment_name": "日报-副本.pdf", "content_hash": "same", "evidence_refs": ["b"]}],
        )
        issue = result["issues"][0]
        self.assertFalse(issue["version_conflict"])
        self.assertEqual({v["file_id"] for v in issue["source_versions"]}, {"a", "b"})


if __name__ == "__main__":
    unittest.main()
