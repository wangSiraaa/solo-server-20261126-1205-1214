"""规则单元测试：python -m unittest discover -s tests -v"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from soundguide.loans import checkout, load_loan
from soundguide.models import PlanRequest
from soundguide.player import play_station, query_delivery, simulate_walk
from soundguide.planner import person_licensed, plan_route
from soundguide.rendering import person_audible_intervals
from soundguide.store import GuideError, GuideStore

MARKERS = Path(__file__).resolve().parent.parent / "data" / "markers.json"
LISTEN = "2026-11-28"  # 周六


def req(**kw) -> PlanRequest:
    base = dict(
        visitor_id="V1", route_id="R1", mode="headphone",
        listen_day=LISTEN, loan_from="2026-11-26", loan_to="2026-11-29",
    )
    base.update(kw)
    return PlanRequest(**base)


class GuideTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = GuideStore(MARKERS, Path(self.tmp.name) / "state.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_marker_shape(self):
        self.assertEqual(len(self.store.stations), 4)
        self.assertEqual(len(self.store.segments), 10)
        s04 = self.store.segments["S04"]
        self.assertEqual([(lk.person, lk.start, lk.end) for lk in s04.leaks],
                         [("P", 2.0, 4.0)])

    def test_muting_mic_p_does_not_kill_leak(self):
        plan = plan_route(self.store, req(mutes=frozenset({"MIC-P"})))
        clip = plan.clip("T2")
        segs = [self.store.segments[s] for s in clip.included_segments]
        heard = person_audible_intervals(segs, frozenset({"MIC-P"}), "P")
        self.assertEqual([(iv.start, iv.end, iv.via) for iv in heard],
                         [(2.0, 4.0, "leak")])

    def test_muting_source_track_kills_leak(self):
        # 静音音乐轨只消除漏入；P 的直录仍在
        plan = plan_route(self.store, req(mutes=frozenset({"MIC-MUSIC"})))
        clip = plan.clip("T2")
        segs = [self.store.segments[s] for s in clip.included_segments]
        self.assertEqual(
            [(iv.start, iv.end, iv.via)
             for iv in person_audible_intervals(segs, frozenset({"MIC-MUSIC"}), "P")],
            [(1.0, 10.0, "direct")],
        )
        # 两条轨都静音且 P 已无河湾许可时，必讲旁白由备选 S06 顶替，P 完全不可闻
        self.store.withdraw_license("L-P-T2")
        plan2 = plan_route(
            self.store, req(mutes=frozenset({"MIC-MUSIC", "MIC-P"}))
        )
        clip2 = plan2.clip("T2")
        segs2 = [self.store.segments[s] for s in clip2.included_segments]
        self.assertEqual(
            person_audible_intervals(segs2, frozenset({"MIC-MUSIC", "MIC-P"}), "P"),
            [],
        )
        self.assertEqual(clip2.narration_segment_id, "S06")

    def test_baseline_delivers_original_at_bend(self):
        plan = plan_route(self.store, req(require_original_station="T2"))
        self.assertTrue(plan.ok)
        self.assertEqual(plan.clip("T2").narration_segment_id, "S05")
        self.assertFalse(plan.clip("T2").uses_fallback)

    def test_withdraw_p_at_bend_blocks_original_allows_fallback(self):
        self.store.withdraw_license("L-P-T2")
        blocked = plan_route(self.store, req(require_original_station="T2"))
        self.assertFalse(blocked.ok)

        fallback = plan_route(self.store, req(mutes=frozenset({"MIC-MUSIC"})))
        self.assertTrue(fallback.ok, fallback.reasons)
        t2 = fallback.clip("T2")
        self.assertTrue(t2.uses_fallback)
        self.assertEqual(t2.narration_segment_id, "S06")
        self.assertNotIn("P", t2.audible_persons)
        # T1 的独立许可不受影响
        self.assertEqual(fallback.clip("T1").narration_segment_id, "S02")

    def test_closed_edge_blocks_checkout(self):
        self.store.close_edge("T1", "T2", reason="施工")
        with self.assertRaises(GuideError):
            checkout(self.store, req())
        self.store.reopen_edge("T1", "T2")
        self.assertTrue(plan_route(self.store, req()).ok)

    def test_license_must_cover_return_date(self):
        self.assertFalse(person_licensed(
            self.store, "R", "T3", "headphone", LISTEN, "2028-01-01"))
        self.assertTrue(person_licensed(
            self.store, "R", "T3", "headphone", LISTEN, "2026-12-31"))

    def test_headphone_only_license_blocks_speaker(self):
        plan = plan_route(self.store, req(mode="speaker"))
        self.assertFalse(plan.ok)
        self.assertTrue(any("T3" in r for r in plan.reasons))

    def test_budget_rejection_is_explicit(self):
        plan = plan_route(self.store, req(headphone_budget_s=5))
        self.assertFalse(plan.ok)
        self.assertTrue(any("预算" in r for r in plan.reasons))

    def test_closed_day_rejected(self):
        plan = plan_route(self.store, req(listen_day="2026-11-30"))  # 周一
        self.assertFalse(plan.ok)
        self.assertTrue(any("不开放" in r for r in plan.reasons))

    def test_checkout_is_immutable_after_withdrawal(self):
        loan = checkout(self.store, req())
        self.store.withdraw_license("L-P-T2")
        reloaded = load_loan(self.store, loan.issue_id)
        t2 = next(c for c in reloaded.clips if c["station_id"] == "T2")
        self.assertEqual(t2["narration_segment_id"], "S05")

    def test_reissue_is_idempotent(self):
        first = checkout(self.store, req())
        second = checkout(self.store, req())
        self.assertEqual(first.issue_id, second.issue_id)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_wrong_path_station_rejected_and_not_booked(self):
        loan = checkout(self.store, req())
        with self.assertRaises(GuideError):
            play_station(self.store, loan.issue_id, "T4")
        self.assertEqual(self.store.ledger_for(loan.issue_id, "T4"), [])

    def test_ledger_is_per_station_and_idempotent(self):
        loan = checkout(self.store, req())
        simulate_walk(self.store, loan.issue_id)
        t2 = query_delivery(self.store, loan.issue_id, "T2")
        t3 = query_delivery(self.store, loan.issue_id, "T3")
        self.assertEqual(t2["narration_segment_id"], "S05")
        self.assertEqual(t3["narration_segment_id"], "S08")
        play_station(self.store, loan.issue_id, "T2")  # 重复不新发
        self.assertEqual(len(self.store.ledger_for(loan.issue_id, "T2")), 1)

    def test_unplayed_station_reports_not_delivered(self):
        loan = checkout(self.store, req())
        report = query_delivery(self.store, loan.issue_id, "T1")
        self.assertFalse(report["delivered"])

    def test_full_route_r2_covers_four_stations(self):
        plan = plan_route(self.store, req(route_id="R2"))
        self.assertTrue(plan.ok, plan.reasons)
        self.assertEqual([c.station_id for c in plan.clips], ["T1", "T2", "T3", "T4"])


if __name__ == "__main__":
    unittest.main()
