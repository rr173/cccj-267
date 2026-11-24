#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import time
import unittest
from pathlib import Path

APP = Path("/workspace/app/sla_report.py")
spec = importlib.util.spec_from_file_location("sla_report", APP)
sla = importlib.util.module_from_spec(spec)
sys.modules["sla_report"] = sla
assert spec.loader is not None
spec.loader.exec_module(sla)


def row_by_id(report):
    return {r["case_id"]: r for r in report["cases"]}


class IntervalTests(unittest.TestCase):
    def test_overlapping_merge_preserves_one_minute_recovery_gap(self):
        # Times are arbitrary integer UTC minutes.  First two overlap and end
        # at 630; third starts at 631, so minute [630,631) is a real recovery.
        merged = sla.merge_intervals([(600, 630), (620, 630), (631, 660)])
        self.assertEqual(merged, [(600, 630), (631, 660)])
        gaps = sla.subtract_interval(600, 660, merged)
        self.assertEqual(gaps, [(630, 631)])

    def test_adjacent_half_open_intervals_have_no_gap(self):
        self.assertEqual(sla.merge_intervals([(600, 630), (630, 660)]), [(600, 660)])


class SampleReportTests(unittest.TestCase):
    def setUp(self):
        evidence_data, contracts, calendars = sla.sample_data()
        self.contracts = contracts
        self.calendars = calendars
        self.evidence5 = sla.evidence_from_data(evidence_data, 5)
        self.evidence10 = sla.evidence_from_data(evidence_data, 10)

    def report(self, evidence):
        return sla.generate_report(
            evidence=evidence,
            contract_pack=self.contracts,
            calendar_pack=self.calendars,
            as_of_text="2026-01-06T00:00Z",
            cutoff=evidence.cutoff,
        )

    def test_original_cutoff_results_match_hand_calculations(self):
        rows = row_by_id(self.report(self.evidence5))

        exact = rows["C-WAIT-EXACT"]
        self.assertTrue(exact["issuable"])
        self.assertEqual(exact["used_minutes"], 120)
        self.assertEqual(exact["timeliness"], sla.TIMELINESS_ON_TIME)

        recovery = rows["C-RECOVERY"]
        self.assertEqual(recovery["used_minutes"], 121)
        self.assertEqual(recovery["timeliness"], sla.TIMELINESS_TIMEOUT)
        self.assertEqual(recovery["earliest_breach"]["at"], "2026-01-05T12:00Z")
        self.assertEqual(recovery["effective_wait_intervals"], [
            {"start": "2026-01-05T10:00Z", "end": "2026-01-05T10:30Z", "minutes": 30},
            {"start": "2026-01-05T10:31Z", "end": "2026-01-05T11:00Z", "minutes": 29},
        ])

        level = rows["C-LEVEL-DOWN-UP"]
        self.assertEqual(level["used_minutes"], 420)
        self.assertEqual(level["current_level"], "ENTERPRISE")
        self.assertEqual(level["quota_minutes"], 480)
        self.assertEqual(level["earliest_breach"]["at"], "2026-01-05T11:00Z")
        self.assertEqual(level["earliest_breach"]["kind"], "level_change")
        self.assertEqual(level["earliest_breach"]["source_record_id"], "L-DOWN")
        self.assertEqual(level["timeliness"], sla.TIMELINESS_TIMEOUT)

        comp_level = rows["C-COMPLETION-LEVEL"]
        self.assertEqual(comp_level["used_minutes"], 120)
        self.assertEqual(comp_level["current_level"], "STD")
        self.assertEqual(comp_level["timeliness"], sla.TIMELINESS_ON_TIME)
        self.assertFalse(comp_level["level_timeline"][-1]["applied"])

        no_cal = rows["C-NO-CALENDAR"]
        self.assertFalse(no_cal["issuable"])
        self.assertIsNone(no_cal["used_minutes"])
        self.assertEqual(no_cal["known_used_minutes"], 0)
        self.assertEqual(no_cal["timeliness"], sla.TIMELINESS_INDETERMINATE)
        self.assertEqual(no_cal["coverage_gaps"][0]["minutes"], 480)
        self.assertTrue(any("calendar_not_found" in x for x in no_cal["blocking_reasons"]))

        year = rows["C-YEAR-BOUNDARY"]
        self.assertEqual(year["used_minutes"], 120)
        self.assertEqual(year["timeliness"], sla.TIMELINESS_ON_TIME)

    def test_higher_cutoff_revisions_and_calendar_flip_statuses(self):
        original = self.report(self.evidence5)
        restated = self.report(self.evidence10)
        rows = row_by_id(restated)

        revoked = rows["C-REVOKED-WAIT"]
        self.assertEqual(revoked["used_minutes"], 180)
        self.assertEqual(revoked["timeliness"], sla.TIMELINESS_TIMEOUT)
        self.assertEqual(revoked["earliest_breach"]["at"], "2026-01-05T11:01Z")

        replaced = rows["C-REPLACED-INTAKE"]
        self.assertEqual(replaced["intake_at"], "2026-01-05T10:00Z")
        self.assertEqual(replaced["used_minutes"], 120)
        self.assertEqual(replaced["timeliness"], sla.TIMELINESS_ON_TIME)

        cal = rows["C-CALENDAR-FIX"]
        self.assertEqual(cal["calendar"]["version"], 2)
        self.assertEqual(cal["used_minutes"], 240)
        self.assertEqual(cal["timeliness"], sla.TIMELINESS_ON_TIME)

        diff = sla.compare_reports(original, restated)
        self.assertEqual(diff["status_flip_count"], 3)
        self.assertEqual(diff["transition_counts"]["on_time->timeout"], 1)
        self.assertEqual(diff["transition_counts"]["timeout->on_time"], 2)
        ids = {x["case_id"] for x in diff["status_flips"]}
        self.assertEqual(ids, {"C-REVOKED-WAIT", "C-REPLACED-INTAKE", "C-CALENDAR-FIX"})


class BoundarySemanticsTests(unittest.TestCase):
    def test_simultaneous_level_events_follow_business_sequence(self):
        t0 = "2026-01-05T09:00Z"
        t11 = "2026-01-05T11:00Z"
        evidence_data = {"submissions": [{"submit_no": 1, "records": [
            {"op": "put", "version": 1, "kind": "intake", "record_id": "I", "case_id": "C",
             "at": t0, "service_point": "SP", "level": "STD"},
            {"op": "put", "version": 1, "kind": "level_change", "record_id": "L1", "case_id": "C",
             "at": t11, "to_level": "LOW", "business_seq": 1},
            {"op": "put", "version": 1, "kind": "level_change", "record_id": "L2", "case_id": "C",
             "at": t11, "to_level": "HIGH", "business_seq": 2},
        ]}]}
        contracts = {"catalogs": [{
            "catalog_id": "SLA", "version": 1, "visible_from_submit_no": 1,
            "contracts": [{"contract_id": "K", "effective_from": "2026-01-01T00:00Z",
                            "service_points": ["SP"],
                            "levels": {"STD": 120, "LOW": 60, "HIGH": 1000}}],
        }]}
        calendars = {"calendars": [{
            "service_point": "SP", "version": 1, "visible_from_submit_no": 1,
            "coverage": [{"start": t0, "end": t11}],
            "open_intervals": [{"start": t0, "end": t11}],
        }]}
        report = sla.generate_report(
            evidence=sla.evidence_from_data(evidence_data, 1),
            contract_pack=contracts, calendar_pack=calendars, as_of_text=t11, cutoff=1,
        )
        row = report["cases"][0]
        self.assertEqual(row["used_minutes"], 120)
        self.assertEqual(row["current_level"], "HIGH")
        self.assertEqual(row["timeliness"], "timeout")
        self.assertEqual(row["earliest_breach"]["source_record_id"], "L1")

    def test_missing_calendar_does_not_block_case_fully_stopped_by_wait(self):
        t0 = "2026-01-05T09:00Z"
        t1 = "2026-01-05T10:00Z"
        evidence_data = {"submissions": [{"submit_no": 1, "records": [
            {"op": "put", "version": 1, "kind": "intake", "record_id": "I", "case_id": "C",
             "at": t0, "service_point": "NOWHERE", "level": "STD"},
            {"op": "put", "version": 1, "kind": "completion", "record_id": "X", "case_id": "C", "at": t1},
            {"op": "put", "version": 1, "kind": "waiting", "record_id": "W", "case_id": "C",
             "start": t0, "end": t1},
        ]}]}
        contracts = {"catalogs": [{"catalog_id": "SLA", "version": 1, "contracts": [
            {"contract_id": "K", "effective_from": "2026-01-01T00:00Z", "service_points": ["NOWHERE"],
             "levels": {"STD": 120}}]}]}
        report = sla.generate_report(
            evidence=sla.evidence_from_data(evidence_data, 1),
            contract_pack=contracts,
            calendar_pack={"calendars": []},
            as_of_text=t1,
            cutoff=1,
        )
        row = report["cases"][0]
        self.assertTrue(row["issuable"], row["blocking_reasons"])
        self.assertEqual(row["used_minutes"], 0)
        self.assertEqual(row["timeliness"], "on_time")

    def test_multi_year_interval_is_swept_without_minute_rows(self):
        start = sla.parse_utc_minute("2020-01-01T00:00Z", "test")
        length = 10 * 365 * 1440  # intentionally simple ten-year minute span
        end_text = sla.format_minute(start + length)
        start_text = sla.format_minute(start)
        evidence_data = {"submissions": [{"submit_no": 1, "records": [
            {"op": "put", "version": 1, "kind": "intake", "record_id": "I", "case_id": "LONG",
             "at": start_text, "service_point": "SP", "level": "STD"},
            {"op": "put", "version": 1, "kind": "completion", "record_id": "X", "case_id": "LONG",
             "at": end_text},
        ]}]}
        contracts = {"catalogs": [{"catalog_id": "SLA", "version": 1, "contracts": [
            {"contract_id": "K", "effective_from": "2000-01-01T00:00Z", "service_points": ["SP"],
             "levels": {"STD": 480}}]}]}
        calendars = {"calendars": [{"service_point": "SP", "version": 1,
                                    "coverage": [{"start": start_text, "end": end_text}],
                                    "open_intervals": [{"start": start_text, "end": end_text}]}]}
        started = time.perf_counter()
        report = sla.generate_report(
            evidence=sla.evidence_from_data(evidence_data, 1),
            contract_pack=contracts, calendar_pack=calendars, as_of_text=end_text, cutoff=1,
        )
        elapsed = time.perf_counter() - started
        row = report["cases"][0]
        self.assertTrue(row["issuable"])
        self.assertEqual(row["used_minutes"], length)
        self.assertEqual(row["earliest_breach"]["at"], sla.format_minute(start + 481))
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
