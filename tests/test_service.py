"""End-to-end tests for the badge service.

Run with: ``python3 -m pytest tests -q`` or ``python3 -m unittest discover tests``.

These cover each requirement in the prompt:

1. plan a schedule -> enough badges, two co-present guests can each speak;
2. whole-season fixed badges, order-independent within the schedule;
3. post-show lookup by speech number;
4. live lending judged on the present set; leave frees a badge; published
   screenshots never change;
5. legend changes fork a new season; old season's speeches keep old agreement;
6. the two-round legend change / collapse scenario;
7. the "third guest pre-borrowed the only deep badge" scenario;
8. the micro demo: sequential then merged screen, theme change, re-issued
   appearance receipt.
"""

from __future__ import annotations

import json
import unittest

from badges.model import default_theme, collapsed_theme, Theme, StyleSpec, ColorRef, SurfaceSpec
from badges.planning import SessionSpec, plan_schedule, co_appearance_groups
from badges.service import Service, ServiceError
from badges.live import render_screen
from badges.http_api import handle
from badges.server import Recorder, Replayer

ROUND = ["round_dark", "round_light"]


class DistinguishabilityTests(unittest.TestCase):
    def test_round_pair_distinguishable_in_default_theme(self):
        t = default_theme()
        self.assertTrue(  # every context
            all(
                t.render("round_dark", s, m) != t.render("round_light", s, m)
                for s, m in t.contexts
            )
        )

    def test_collapsed_theme_indistinguishable_on_large_subtitle(self):
        t = collapsed_theme()
        self.assertFalse(
            all(
                t.render("round_dark", s, m) != t.render("round_light", s, m)
                for s, m in t.contexts
            )
        )
        # Specifically only the subtitle-bar large-text context collapses.
        bad = [
            (s, m)
            for s, m in t.contexts
            if t.render("round_dark", s, m) == t.render("round_light", s, m)
        ]
        self.assertEqual(bad, [("字幕条", "大字")])


class PlanTests(unittest.TestCase):
    def _schedule(self):
        return [
            SessionSpec("E1", "开场", ("alice", "bob")),
            SessionSpec("E2", "中段", ("bob", "carol")),
            SessionSpec("E3", "收尾", ("alice", "carol")),
        ]

    def test_plan_finds_assignment_for_round_pair(self):
        t = default_theme()
        # Two co-present guests, only the round pair: feasible in the default theme.
        res = plan_schedule(t, [SessionSpec("E1", "", ("alice", "bob"))], allowances={g: ROUND for g in ("alice", "bob")})
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.assignment, {"alice": "round_dark", "bob": "round_light"})

    def test_plan_uses_shape_when_round_pair_insufficient(self):
        # Three pairwise co-present guests cannot all be coloured with only two
        # round badges; with shaped badges allowed the planner finds a plan.
        t = default_theme()
        sched = [SessionSpec("E", "", ("alice", "bob", "carol"))]
        impossible = plan_schedule(t, sched, allowances={g: ROUND for g in ("alice", "bob", "carol")})
        self.assertFalse(impossible.ok)
        possible = plan_schedule(t, sched)  # full theme has shaped badges
        self.assertTrue(possible.ok, possible.reason)
        self.assertEqual(len(set(possible.assignment.values())), 3)

    def test_plan_order_independent(self):
        t = default_theme()
        sched = [
            SessionSpec("E1", "", ("alice", "bob")),
            SessionSpec("E2", "", ("bob", "carol")),
        ]
        pools = {"alice": ROUND, "bob": ROUND, "carol": ["round_dark", "square_dark"]}
        a = plan_schedule(t, sched, allowances=pools)
        b = plan_schedule(t, list(reversed(sched)), allowances=pools)
        self.assertEqual(a.ok, b.ok)
        self.assertTrue(a.ok)

    def test_simulcast_merges_rooms(self):
        t = default_theme()
        sessions = [SessionSpec("E1", "", ("a", "b")), SessionSpec("E2", "", ("c", "d"))]
        groups = co_appearance_groups(sessions, simulcasts=[["E1", "E2"]])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0], ["a", "b", "c", "d"])

    def test_pinned_signed_styles_are_honoured(self):
        t = default_theme()
        sessions = [SessionSpec("E1", "", ("a", "b"))]
        res = plan_schedule(t, sessions, pinned={"a": "square_dark"})
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.assignment["a"], "square_dark")

    def test_start_order_invariance(self):
        # A whole-season plan must be achievable regardless of which session
        # starts first, so the signed badges can be printed ahead of time.
        import json, os

        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data = json.load(open(os.path.join(here, "fixtures", "schedule.json"), encoding="utf-8"))
        sessions = [SessionSpec(s["id"], s["label"], tuple(s["guests"])) for s in data["sessions"]]
        t = default_theme()
        pools = data["allowances"]
        results = []
        for perm in [sessions, list(reversed(sessions)), [sessions[1], sessions[2], sessions[0]]]:
            res = plan_schedule(t, perm, allowances=pools, simulcasts=data["simulcasts"])
            self.assertTrue(res.ok, res.reason)
            results.append(res.assignment)
        # Assignment is over the same guest set; feasibility is order-invariant.
        self.assertTrue(all(r.keys() == results[0].keys() for r in results))


class SeasonAndSpeechTests(unittest.TestCase):
    def test_sign_and_speak_then_lookup(self):
        svc = Service()
        season = svc.open_season(default_theme())
        svc.sign("alice", "round_dark")
        svc.sign("bob", "round_light")
        out = svc.start_session("E1", ["alice", "bob"], speech_numbers={"alice": 1, "bob": 2})
        self.assertEqual(out["styles"], {"alice": "round_dark", "bob": "round_light"})
        self.assertEqual(svc.attribute(1, season.season_id).guest, "alice")
        self.assertEqual(svc.attribute(2, season.season_id).guest, "bob")

    def test_cannot_reuse_speech_no_for_different_guest(self):
        svc = Service()
        svc.open_season(default_theme())
        svc.sign("alice", "round_dark")
        svc.sign("bob", "round_light")
        svc.record_speech("alice", "round_dark", "E1", speech_no=1)
        with self.assertRaises(ServiceError):
            svc.record_speech("bob", "round_light", "E1", speech_no=1)

    def test_theme_change_requires_new_season_after_signing(self):
        svc = Service()
        sid = svc.open_season(default_theme()).season_id
        svc.sign("alice", "round_dark")
        with self.assertRaises(ServiceError):
            svc.set_theme(collapsed_theme(), sid)

    def test_old_season_attribution_survives_new_season(self):
        svc = Service()
        s1 = svc.open_season(default_theme())
        svc.sign("alice", "round_dark")
        svc.sign("bob", "round_light")
        svc.start_session("E1", ["alice", "bob"], speech_numbers={"alice": 1, "bob": 2})
        # New season with a *collapsed* theme; issue a receipt & speech there.
        s2 = svc.open_season(collapsed_theme())
        svc.sign("carol", "round_dark")  # allowed alone
        svc.record_speech("carol", "round_dark", "E2", speech_no=1)
        # Season 1's speech 1 still maps to alice; season 2's to carol.
        self.assertEqual(svc.attribute(1, s1.season_id).guest, "alice")
        self.assertEqual(svc.attribute(1, s2.season_id).guest, "carol")

    def test_receipt_reissue_does_not_change_published_speech(self):
        svc = Service()
        svc.open_season(default_theme())
        svc.sign("alice", "round_dark")
        svc.sign("bob", "round_light")
        out = svc.start_session("E1", ["alice", "bob"], speech_numbers={"alice": 1})
        # Alice leaves and lends her badge to carol later.
        svc.leave("alice")
        svc.sign("carol", "round_dark")  # carol may use it now (not co-present with alice)
        svc.record_speech("carol", "round_dark", "E2", speech_no=2, receipt_id=None)
        # Original published speech unchanged.
        self.assertEqual(svc.attribute(1).guest, "alice")
        self.assertEqual(svc.attribute(2).guest, "carol")


class LiveTests(unittest.TestCase):
    def test_enter_live_rejects_collapsing_pair(self):
        svc = Service()
        svc.open_season(collapsed_theme())
        svc.enter_live("alice", "round_dark")
        with self.assertRaises(ServiceError):
            svc.enter_live("bob", "round_light")

    def test_assign_live_uses_shapes_when_possible(self):
        svc = Service()
        svc.open_season(collapsed_theme())
        # The round pair collapses in large-subtitle, so with alice able to take
        # a shaped badge the planner separates all three instead of failing.
        pool = {
            "alice": ["round_dark", "square_dark"],
            "bob": ["round_light"],
            "carol": ["round_light", "triangle_light"],
        }
        out = svc.assign_live(["alice", "bob", "carol"], pool)
        self.assertEqual(set(out["present"]), {"alice", "bob", "carol"})
        values = list(out["present"].values())
        self.assertEqual(len(set(values)), 3)

    def test_leave_frees_badge_for_other(self):
        svc = Service()
        svc.open_season(default_theme())
        svc.enter_live("alice", "round_dark")
        svc.leave("alice")
        svc.enter_live("carol", "round_dark")  # now free
        self.assertEqual(svc.get_season().present["carol"], "round_dark")

    def test_merge_screen_rejects_inseparable_union(self):
        svc = Service()
        svc.open_season(collapsed_theme())
        # Two independent rooms each allowed one dark/light round guest.
        # Merging must fail because the subtitle-large context collapses them.
        with self.assertRaises(ServiceError):
            svc.merge_screen(
                {"A": {"alice": "round_dark"}, "B": {"bob": "round_light"}},
                rooms=["A", "B"],
            )

    def test_merge_screen_accepts_separable_union(self):
        svc = Service()
        svc.open_season(default_theme())
        out = svc.merge_screen(
            {"A": {"alice": "round_dark"}, "B": {"bob": "round_light"}}, rooms=["A", "B"]
        )
        self.assertEqual(out["present"], {"alice": "round_dark", "bob": "round_light"})


class TwoRoundLegendTests(unittest.TestCase):
    """The prompt's paired scenario.

    Two guests, only round_dark / round_light available.  In the *default*
    theme both ordinary and large modes separate them, so they can share a
    stage.  In the *collapsed* theme the large-subtitle edge alias collapses to
    the same colour, so the pair can no longer be co-signed.
    """

    def test_default_theme_round_pair_can_share_stage(self):
        svc = Service()
        svc.open_season(default_theme())
        svc.sign("g1", "round_dark")
        svc.sign("g2", "round_light")
        out = svc.start_session("E1", ["g1", "g2"], speech_numbers={"g1": 1, "g2": 2})
        self.assertTrue(out["styles"])

    def test_collapsed_theme_round_pair_cannot_share_stage(self):
        svc = Service()
        svc.open_season(collapsed_theme())
        svc.sign("g1", "round_dark")
        svc.sign("g2", "round_light")
        with self.assertRaises(ServiceError):
            svc.start_session("E1", ["g1", "g2"], speech_numbers={"g1": 1, "g2": 2})

    def test_new_season_isolated(self):
        svc = Service()
        s1 = svc.open_season(default_theme())
        svc.sign("g1", "round_dark")
        svc.sign("g2", "round_light")
        svc.start_session("E1", ["g1", "g2"], speech_numbers={"g1": 1, "g2": 2})
        s2 = svc.open_season(collapsed_theme())
        # Can't reuse the pair here.
        svc.sign("g3", "round_dark")
        svc.sign("g4", "round_light")
        with self.assertRaises(ServiceError):
            svc.start_session("E2", ["g3", "g4"])
        # Old season speech intact.
        self.assertEqual(svc.attribute(1, s1.season_id).guest, "g1")
        self.assertEqual(svc.attribute(2, s1.season_id).guest, "g2")


class ThirdGuestPreBorrowTests(unittest.TestCase):
    """New season, theme restored to separable, but the *only* deep round badge
    is already lawfully pre-signed to a third guest who is still using it.  The
    remaining badge cannot make the original pair's merged stream work.
    """

    def test_third_guest_pins_deep_badge_and_pair_cannot_merge(self):
        svc = Service()
        svc.open_season(default_theme())
        # Third guest lawfully pre-signed the only deep badge and is live.
        svc.sign("third", "round_dark")
        svc.enter_live("third", "round_dark")
        # The pair only has round_light left plus whatever is signed; they
        # cannot both be distinct from 'third' and each other using round_*.
        with self.assertRaises(ServiceError):
            svc.assign_live(
                ["third", "a", "b"], {"third": ["round_dark"], "a": ROUND, "b": ROUND}
            )
        # But if a shaped badge is available to 'a', it works.
        out = svc.assign_live(
            ["third", "a", "b"],
            {"third": ["round_dark"], "a": ROUND, "b": ["round_dark", "square_dark"]},
        )
        self.assertTrue(out["present"])

    def test_pair_cannot_merge_while_third_holds_deep(self):
        svc = Service()
        svc.open_season(default_theme())
        svc.enter_live("third", "round_dark")
        # Only round_light remains for the pair; merging them is impossible.
        with self.assertRaises(ServiceError):
            svc.merge_screen(
                {"A": {"third": "round_dark"}, "B": {"a": "round_light", "b": "round_light"}},
                rooms=["A", "B"],
            )


class HttpAndReplayTests(unittest.TestCase):
    def test_api_roundtrip_and_replay(self):
        import tempfile, os

        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        try:
            rec = Recorder(path=path)
            r = rec.post("/seasons", {"theme_kind": "default", "season_id": "S1"})
            self.assertEqual(r["season_id"], "S1")
            rec.post("/seasons/S1/sign", {"guest": "alice", "style": "round_dark"})
            rec.post("/seasons/S1/sign", {"guest": "bob", "style": "round_light"})
            r = rec.post(
                "/seasons/S1/sessions/start",
                {"session_id": "E1", "guests": ["alice", "bob"], "speech_numbers": {"alice": 1}},
            )
            self.assertIn("styles", r)
            r = rec.get("/seasons/S1/speeches/1")
            self.assertEqual(r["guest"], "alice")

            rep = Replayer(path)
            result = rep.run()
            self.assertTrue(result["ok"], result["mismatches"][:1])
            self.assertEqual(result["entries"], len(rec.calls))
        finally:
            os.unlink(path)


class AliasOverrideTests(unittest.TestCase):
    def test_surface_alias_override_collapses_only_target_context(self):
        import json, os

        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = json.load(open(os.path.join(here, "fixtures", "theme_alias_subtitle.json"), encoding="utf-8"))
        t = Theme.from_json(d)
        bad = [(s, m) for s, m in t.contexts if t.render("round_dark", s, m) == t.render("round_light", s, m)]
        self.assertEqual(bad, [("字幕条", "大字")])
        # In other contexts the light edge stays light.
        self.assertNotEqual(
            t.render("round_light", "房间", "大字"), t.render("round_dark", "房间", "大字")
        )


class FakeLiveRenderTests(unittest.TestCase):
    def test_screen_marks_pair_separable_in_default(self):
        t = default_theme()
        frame = render_screen(t, ["alice", "bob"], {"alice": "round_dark", "bob": "round_light"})
        self.assertTrue(frame["all_separable"])

    def test_screen_reports_collapse(self):
        t = collapsed_theme()
        frame = render_screen(t, ["alice", "bob"], {"alice": "round_dark", "bob": "round_light"})
        self.assertFalse(frame["all_separable"])


if __name__ == "__main__":
    unittest.main()
