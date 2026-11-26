"""End-to-end tests for the changeover backend.

Each test maps to a clause in the brief.  Run with::

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from changeover.engine import build_engine
from changeover.fixtures import build_baseline
from changeover.fixtures_successive import build_successive, display


def confirm(e, sid, v):
    for k in ("received", "buffered", "displayed"):
        e.device_state(sid, k, v)


class Base(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".jsonl")
        self._engines = []

        def cleanup():
            for e in self._engines:
                try:
                    e.store.close()
                except Exception:
                    pass
            if os.path.exists(self.path):
                os.unlink(self.path)

        self.addCleanup(cleanup)

    def _track(self, e):
        self._engines.append(e)
        return e

    def restart(self):
        return self._track(build_engine(self.path))


# ---------------------------------------------------------------------------
# Baseline: "east -> corridor -> side door -> lecture room; room is poetry's,
# so east may open for poetry."
# ---------------------------------------------------------------------------
class BaselineTest(Base):
    def test_baseline_east_opens_for_poetry(self):
        e, op = build_baseline(self.path); self._track(e)
        self.assertEqual(op["status"], "active")
        self.assertTrue(op["live_walkable"])
        w = e.walk("sc-east", "poetry")
        self.assertTrue(w["ok"])
        self.assertEqual([h["door"] for h in w["route"]], ["d-east", "d-side"])

    def test_copy_sent_is_not_visitor_having_seen(self):
        e, _ = build_baseline(self.path); self._track(e)
        # send a fresh copy to west but never confirm it; west still shows nothing
        e.send_copy("sc-west", "poetry", "d-side")
        sv = e.screen_view("sc-west")
        self.assertFalse(sv["showing"])
        self.assertIsNone(sv["display_target"])
        # no opening is granted off an unconfirmed copy
        with self.assertRaises(Exception) as cm:
            e.open_direct("poetry", "sc-west")
        self.assertEqual(cm.exception.code, "not_walkable")

    def test_late_older_display_is_ignored(self):
        e, _ = build_baseline(self.path); self._track(e)
        v2 = e.send_copy("sc-hall", "poetry", "d-side")["sent_version"]
        confirm(e, "sc-hall", v2)
        out = e.device_state("sc-hall", "displayed", v2 - 1)
        self.assertTrue(any("stale_display" in str(n) for n in out["notes"]))

    def test_displayed_requires_buffered(self):
        e, _ = build_baseline(self.path); self._track(e)
        v = e.send_copy("sc-west", "poetry", "d-side")["sent_version"]
        e.device_state("sc-west", "received", v)
        out = e.device_state("sc-west", "displayed", v)  # never buffered
        self.assertTrue(any("before_buffered" in str(n) for n in out["notes"]))
        self.assertFalse(e.screen_view("sc-west")["showing"])


# ---------------------------------------------------------------------------
# First posture: "only change the corridor door field receipt to still
# partitioned -> the original opening must be retracted."
# ---------------------------------------------------------------------------
class RetractOnPartitionTest(Base):
    def test_partition_receipt_retracts_opening(self):
        e, op = build_baseline(self.path); self._track(e)
        self.assertEqual(op["status"], "active")
        e.door_field("d-side", False)
        v = e.opening_view(op["number"])
        self.assertEqual(v["status"], "retracted")
        self.assertIsNone(v["live_walkable"])
        self.assertIn("door_blocked", v["retracted_reason"])
        # east can no longer be certified
        with self.assertRaises(Exception) as cm:
            e.open_direct("poetry", "sc-east")
        self.assertEqual(cm.exception.code, "not_walkable")


# ---------------------------------------------------------------------------
# Second posture: "restore the doors, but let the corridor sign's real display
# stay pointed back at the east door -> also cannot sign open."
# ---------------------------------------------------------------------------
class StaleGuidanceTest(Base):
    def test_corridor_pointing_back_to_east_blocks_open(self):
        e, op = build_baseline(self.path); self._track(e)
        e.door_field("d-side", False)
        self.assertEqual(e.opening_view(op["number"])["status"], "retracted")
        # doors restored
        e.door_field("d-side", True)
        # but corridor's real display now points back through d-east (a loop)
        v = e.send_copy("sc-hall", "poetry", "d-east")["sent_version"]
        confirm(e, "sc-hall", v)
        w = e.walk("sc-east", "poetry")
        self.assertFalse(w["ok"])
        self.assertEqual(w["reason"], "cycle_without_exit")
        with self.assertRaises(Exception) as cm:
            e.open_direct("poetry", "sc-east")
        self.assertEqual(cm.exception.code, "not_walkable")


# ---------------------------------------------------------------------------
# Privacy: a corridor sign may not reveal an activity that forbids being seen.
# ---------------------------------------------------------------------------
class PrivacyTest(Base):
    def test_corridor_sign_cannot_reveal_hidden_activity(self):
        e = self._track(build_engine(self.path))
        e.add_zone("east", "东门", "entrance")
        e.add_zone("hall", "走廊", "corridor")
        e.add_zone("room", "讲室", "room")
        e.add_door("d1", "east", "hall", "both", True)
        e.add_door("d2", "hall", "room", "both", True)
        e.add_screen("sc-east", "east")
        e.add_screen("sc-hall", "hall")
        e.add_screen("sc-room", "room")
        e.register_activity("secret", "密讲", ["room"], allow_seen=False, host="h")
        v = e.send_copy("sc-east", "secret", "d1")["sent_version"]; confirm(e, "sc-east", v)
        v = e.send_copy("sc-hall", "secret", "d2")["sent_version"]; confirm(e, "sc-hall", v)
        w = e.walk("sc-east", "secret")
        self.assertFalse(w["ok"])
        self.assertIn("privacy_leak", w["reason"])


# ---------------------------------------------------------------------------
# Cycle without exit is not walkable.
# ---------------------------------------------------------------------------
class CycleTest(Base):
    def test_open_loop_is_not_walkable(self):
        e = self._track(build_engine(self.path))
        e.add_zone("a", "A", "entrance")
        e.add_zone("b", "B", "corridor")
        e.add_zone("c", "C", "room")
        e.add_door("dab", "a", "b", "both", True)
        e.add_door("dba", "b", "a", "both", True)
        e.add_door("dbc", "b", "c", "both", True)
        e.add_screen("sa", "a")
        e.add_screen("sb", "b")
        e.register_activity("x", "X", ["c"], True, "h")
        v = e.send_copy("sa", "x", "dab")["sent_version"]; confirm(e, "sa", v)
        v = e.send_copy("sb", "x", "dba")["sent_version"]; confirm(e, "sb", v)
        w = e.walk("sa", "x")
        self.assertFalse(w["ok"])
        self.assertEqual(w["reason"], "cycle_without_exit")


# ---------------------------------------------------------------------------
# Whole-hall: partitions must be field-confirmed; conflicts masked; full route
# signage confirmed; doors open; only then certify.
# ---------------------------------------------------------------------------
class WholeHallTest(Base):
    def _setup(self):
        e, _ = build_baseline(self.path); self._track(e)
        e.register_activity("symposium", "研讨", ["room"], True, "研讨主持")
        c = e.start_changeover(
            "whole_hall", "poetry", "symposium", ["sc-east"],
            doors_to_open=["d-side"], doors_to_partition=["d-side"],
        )["changeover"]
        return e, c

    def test_cannot_open_before_partition_confirmed(self):
        e, c = self._setup()
        # everything else staged, but partition never field-confirmed
        e.door_field("d-side", False)
        for s in e.conflict_screens(e.state.changeovers[c]):
            e.set_mask(s, True)
        plan = e._route_plan("east", "symposium", extra_open=["d-side"])
        for sid, did in plan:
            v = e.send_copy(sid, "symposium", did)["sent_version"]; confirm(e, sid, v)
            e.set_mask(sid, False)
        e.door_field("d-side", True)  # even opened, partition unconfirmed
        res = e.attempt_open(c)["results"][0]
        self.assertFalse(res["opened"])
        gates = {b["gate"] for b in res["blockers"]}
        self.assertIn("partitions_confirmed", gates)

    def test_full_whole_hall_handover(self):
        e, c = self._setup()
        e.door_field("d-side", False)
        e.changeover_field(c, "d-side")
        for s in e.conflict_screens(e.state.changeovers[c]):
            e.set_mask(s, True)
        plan = e._route_plan("east", "symposium", extra_open=["d-side"])
        for sid, did in plan:
            v = e.send_copy(sid, "symposium", did)["sent_version"]; confirm(e, sid, v)
            e.set_mask(sid, False)
        e.door_field("d-side", True)
        res = e.attempt_open(c)["results"][0]
        self.assertTrue(res["opened"])
        self.assertEqual(res["opening"]["status"], "active")
        receipts = e.receipts_view()
        self.assertTrue(any("visitors may enter" in r["message"] for r in receipts))

    def test_lagging_screen_blocks_but_others_can_advance(self):
        e, c = self._setup()
        e.door_field("d-side", False)
        e.changeover_field(c, "d-side")
        for s in e.conflict_screens(e.state.changeovers[c]):
            e.set_mask(s, True)
        plan = e._route_plan("east", "symposium", extra_open=["d-side"])
        # confirm all but the last route screen
        for sid, did in plan[:-1]:
            v = e.send_copy(sid, "symposium", did)["sent_version"]; confirm(e, sid, v)
            e.set_mask(sid, False)
        last_sid, last_did = plan[-1]
        v = e.send_copy(last_sid, "symposium", last_did)["sent_version"]
        e.device_state(last_sid, "received", v)  # lagging: no display confirmation
        e.door_field("d-side", True)
        st = e.status(c)
        self.assertIn(last_sid, st["pending_confirmations"])
        self.assertIn("blocked", st)
        res = e.attempt_open(c)["results"][0]
        self.assertFalse(res["opened"])
        # meanwhile masking the other conflicts was possible
        self.assertTrue(e.screen_view("sc-east")["masked"] is False)  # east got unmasked when staged
        # finish the lagging screen: confirm display, then unmask it
        confirm(e, last_sid, v)
        e.set_mask(last_sid, False)
        res = e.attempt_open(c)["results"][0]
        self.assertTrue(res["opened"])


# ---------------------------------------------------------------------------
# Successive: never drop the last old lifeline; only switch after host ends old.
# ---------------------------------------------------------------------------
class SuccessiveTest(Base):
    def setUp(self):
        super().setUp()
        self.e = self._track(build_successive(self.path))
        self.c = self.e.start_changeover(
            "successive", "lecture", "poetry", ["sc-east", "sc-west"]
        )["changeover"]

    def test_both_entrances_can_switch_independently(self):
        e, c = self.e, self.c
        r = e.switch_entrance(c, "sc-east")
        for cp in r["copies"]:
            confirm(e, cp["screen"], cp["version"])
        self.assertTrue(e.walk("sc-east", "poetry")["ok"])
        self.assertTrue(e.walk("sc-west", "lecture")["ok"])  # lifeline intact

    def test_refuse_last_lifeline_before_old_ended(self):
        e, c = self.e, self.c
        r = e.switch_entrance(c, "sc-east")
        for cp in r["copies"]:
            confirm(e, cp["screen"], cp["version"])
        with self.assertRaises(Exception) as cm:
            e.switch_entrance(c, "sc-west")
        self.assertEqual(cm.exception.code, "last_old_lifeline")

    def test_last_switch_allowed_after_old_ended(self):
        e, c = self.e, self.c
        r = e.switch_entrance(c, "sc-east")
        for cp in r["copies"]:
            confirm(e, cp["screen"], cp["version"])
        e.end_activity("lecture")
        r = e.switch_entrance(c, "sc-west")
        for cp in r["copies"]:
            confirm(e, cp["screen"], cp["version"])
        self.assertTrue(e.walk("sc-west", "poetry")["ok"])

    def test_display_guard_blocks_premature_lifeline_erase(self):
        """Even a direct display confirmation cannot erase the last old route."""
        e, c = self.e, self.c
        r = e.switch_entrance(c, "sc-east")
        for cp in r["copies"]:
            confirm(e, cp["screen"], cp["version"])
        # try to re-point west (the lifeline) directly to poetry before host ends old
        v = e.send_copy("sc-west", "poetry", "d-west")["sent_version"]
        e.device_state("sc-west", "received", v)
        e.device_state("sc-west", "buffered", v)
        with self.assertRaises(Exception) as cm:
            e.device_state("sc-west", "displayed", v)
        self.assertEqual(cm.exception.code, "would_drop_old_lifeline")
        self.assertTrue(e.walk("sc-west", "lecture")["ok"])

    def test_block_when_new_route_needs_protected_screen(self):
        """If east's new path must reuse the corridor sign that carries west's
        old route, the partial switch is refused rather than breaking the old path."""
        e = self.e
        # make west's old route run through the corridor sign
        display(e, "sc-west", "lecture", "d-west")
        display(e, "sc-hall", "lecture", "d-h-old")
        with self.assertRaises(Exception) as cm:
            e.switch_entrance(self.c, "sc-east")
        self.assertEqual(cm.exception.code, "blocked_by_lifeline")
        self.assertTrue(e.walk("sc-west", "lecture")["ok"])


# ---------------------------------------------------------------------------
# Offline inspection: after a device drops, query what guidance really remains.
# ---------------------------------------------------------------------------
class OfflineInspectionTest(Base):
    def test_left_guidance_after_dropout(self):
        e, op = build_baseline(self.path); self._track(e)
        # The hall screen drops offline after having confirmed its display.
        e.set_online("sc-hall", False)
        hall = e.screen_view("sc-hall")
        self.assertFalse(hall["online"])
        # Its *confirmed* display survives the dropout, so the last real
        # guidance is still queryable (not a merely-sent copy).
        self.assertEqual(hall["display_target"], "poetry")
        self.assertEqual(hall["display_door"], "d-side")
        g = e.left_guidance(entrance_screen="sc-east", target="poetry")
        self.assertTrue(g["walk"]["ok"])  # reading real display, not liveness
        # But a copy that was only sent (never confirmed) does not appear.
        e.set_online("sc-west", False)
        e.send_copy("sc-west", "poetry", "d-west")
        self.assertIsNone(e.screen_view("sc-west")["display_target"])

    def test_unreached_route_may_point_to_reception_not_open(self):
        e, _ = build_baseline(self.path); self._track(e)
        e.door_field("d-west", True)  # west passage is physically open
        e.door_field("d-desk", True)  # and so is the way to the reception desk
        # Stage the whole unready route to the reception desk as a holding sign.
        v = e.send_copy("sc-west", "__reception__", "d-west")["sent_version"]; confirm(e, "sc-west", v)
        v = e.send_copy("sc-hall", "__reception__", "d-desk")["sent_version"]; confirm(e, "sc-hall", v)
        w = e.walk("sc-west", "__reception__")
        self.assertTrue(w["ok"])
        self.assertEqual([h["door"] for h in w["route"]], ["d-west", "d-desk"])
        # That is NOT an opening for poetry.
        with self.assertRaises(Exception) as cm:
            e.open_direct("poetry", "sc-west")
        self.assertEqual(cm.exception.code, "not_walkable")


# ---------------------------------------------------------------------------
# Restart / determinism: a deliberately late "displayed" of an old version must
# not change the reproduced walking route after a restart.
# ---------------------------------------------------------------------------
class RestartTest(Base):
    def test_restart_reproduces_route_despite_late_stale_display(self):
        e, op = build_baseline(self.path); self._track(e)
        n = op["number"]
        expected = [(h["screen"], h["door"]) for h in op["route"]]
        v2 = e.send_copy("sc-hall", "poetry", "d-side")["sent_version"]
        confirm(e, "sc-hall", v2)
        # late stale confirmation of the *old* version arrives out of order
        e.device_state("sc-hall", "displayed", v2 - 1)
        e.store.close()
        e2 = self.restart()
        v = e2.opening_view(n)
        self.assertEqual(v["status"], "active")
        self.assertEqual([(h["screen"], h["door"]) for h in v["route"]], expected)
        self.assertTrue(v["live_walkable"])
        # and the live walk after restart matches the snapshot
        w = e2.walk("sc-east", "poetry")
        self.assertEqual([(h["screen"], h["door"]) for h in w["route"]], expected)

    def test_restart_after_retraction_stays_retracted(self):
        e, op = build_baseline(self.path); self._track(e)
        e.door_field("d-side", False)
        e.store.close()
        e2 = self.restart()
        self.assertEqual(e2.opening_view(op["number"])["status"], "retracted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
