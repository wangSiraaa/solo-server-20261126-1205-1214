"""Scenario 5: disputed minutes when one person is at two stations at once."""

from __future__ import annotations

import unittest

from harness import WorkshopTestCase, T


class DisputedMinutesTests(WorkshopTestCase):
    def test_overlapping_stations_are_reported_and_deduplicated(self):
        """同一人同时出现在两个台位不能把两边的时长都算足，需能查出受争议的分钟."""
        # Use a fresh person so the baseline 甲->乙 practicum does not interfere.
        self.svc.create_person("学徒己", "F", role="apprentice")
        # Station 1: 09:00-13:00 (240 min). Station 2: 11:00-14:00 (180 min).
        ev1 = self.svc.register_evidence(
            "shade", "2026-01-15T09:00:00Z", "2026-01-15T13:00:00Z",
            ["A"], ["F"], ["cut", "sew"], station_id="bench1")
        self.svc.complete_evidence(ev1["id"], "2026-01-15T13:00:00Z")
        ev2 = self.svc.register_evidence(
            "shade", "2026-01-15T11:00:00Z", "2026-01-15T14:00:00Z",
            ["A"], ["F"], ["cut", "sew"], station_id="bench2")
        self.svc.complete_evidence(ev2["id"], "2026-01-15T14:00:00Z")

        report = self.svc.disputed_minutes("F", at_ts="2026-01-16T00:00:00Z")
        # 11:00-13:00 overlaps => 120 disputed minutes.
        self.assertAlmostEqual(report["total_disputed_minutes"], 120.0, places=3)
        self.assertEqual(len(report["pairs"]), 1)
        self.assertEqual(report["pairs"][0]["overlap_minutes"], 120.0)

        # Accrual uses the UNION, so 己 gets 300 min (09:00-14:00), not 420.
        elig = self.svc.eligibility("F", "shade", "2026-01-16T00:00:00Z")
        self.assertAlmostEqual(elig["accrued_minutes"], 300.0, places=3)

    def test_same_station_does_not_count_as_dispute(self):
        """Two records at the same station are not a station conflict."""
        self.svc.create_person("学徒庚", "G", role="apprentice")
        ev1 = self.svc.register_evidence(
            "shade", "2026-01-15T09:00:00Z", "2026-01-15T13:00:00Z",
            ["A"], ["G"], ["cut"], station_id="bench1")
        self.svc.complete_evidence(ev1["id"], "2026-01-15T13:00:00Z")
        ev2 = self.svc.register_evidence(
            "shade", "2026-01-15T11:00:00Z", "2026-01-15T14:00:00Z",
            ["A"], ["G"], ["sew"], station_id="bench1")
        self.svc.complete_evidence(ev2["id"], "2026-01-15T14:00:00Z")
        report = self.svc.disputed_minutes("G", at_ts="2026-01-16T00:00:00Z")
        self.assertEqual(report["pairs"], [])
        self.assertEqual(report["total_disputed_minutes"], 0.0)


    def test_three_way_overlap_counts_each_minute_once(self):
        """Three simultaneous stations: 10:00-11:00 is contested once, not thrice."""
        self.svc.create_person("学徒癸", "K", role="apprentice")
        specs = [
            ("2026-01-15T09:00:00Z", "2026-01-15T12:00:00Z", "bench1"),
            ("2026-01-15T10:00:00Z", "2026-01-15T11:00:00Z", "bench2"),
            ("2026-01-15T10:00:00Z", "2026-01-15T11:00:00Z", "bench1b"),
        ]
        self.svc.create_station("bench1b", "一号乙台")
        for s, e, st in specs:
            ev = self.svc.register_evidence("shade", s, e, ["A"], ["K"], ["cut"],
                                            station_id=st)
            self.svc.complete_evidence(ev["id"], e)
        report = self.svc.disputed_minutes("K", at_ts="2026-01-16T00:00:00Z")
        # Contested window is exactly 10:00-11:00 = 60 min (counted once).
        self.assertAlmostEqual(report["total_disputed_minutes"], 60.0, places=3)
        # Three pairwise overlaps are still individually reported.
        self.assertEqual(len(report["pairs"]), 3)


if __name__ == "__main__":
    unittest.main()
