"""Tests for the bookstore serialised-reading service.

These follow the scenario in the brief closely:

* two readers learn the same fact from two different branches (甲 letter /
  乙 recording) and can attend the same reading;
* served sessions must be prepared and locked;
* a newly published revision may move a reveal one segment later, and an
  explicitly carried fact survives it;
* removing an unfinished (mis-tapped) read narrows access;
* cyclic ``describes`` references never manufacture a fact;
* an open session joins latecomers at a real meeting point;
* a staff member cannot serve two overlapping sessions even when the readers'
  facts are identical;
* make-up reads (补读) persist for the next session;
* the state survives a process restart.
"""

import json
import os
import tempfile
import unittest

from bookclub.store import BookClubError, Store


def build_story(store: Store) -> dict:
    """Publish the baseline edition and return useful ids."""
    store.define_fact("key_under_bridge", "the key is under the bridge")
    store.define_fact("letter_warning", "the warning in the letter")
    store.define_fact("recording_confession", "the confession on the tape")
    store.define_fact("brother_alive", "the brother is still alive")
    store.define_work("w", "The Bridge")

    segments = [
        # s1: opening, discloses nothing important
        {"id": "s1", "title": "Opening", "text": "A quiet town wakes.",
         "describes": ["s4", "s6"]},
        # 甲 branch: the letter
        {"id": "s2a", "title": "The Letter", "text": "She reads the letter.",
         "discloses": ["letter_warning"]},
        # 乙 branch: the recording
        {"id": "s2b", "title": "The Tape", "text": "The tape hisses.",
         "discloses": ["recording_confession"]},
        # both branches reveal the key is under the bridge
        {"id": "s3a", "title": "Under the Bridge (letter path)",
         "text": "The letter says: the key is under the bridge.",
         "discloses": ["key_under_bridge"], "requires": ["letter_warning"]},
        {"id": "s3b", "title": "Under the Bridge (tape path)",
         "text": "The tape says: the key is under the bridge.",
         "discloses": ["key_under_bridge"], "requires": ["recording_confession"]},
        # final segment both branches converge on
        {"id": "s4", "title": "The Finale", "text": "They open the door.",
         "requires": ["key_under_bridge"], "discloses": ["brother_alive"]},
        # an alternate ending, also needs the key
        {"id": "s5", "title": "Alternate Ending", "text": "They walk away.",
         "requires": ["key_under_bridge"], "discloses": ["brother_alive"]},
        # a segment that only alludes to others (cyclic describe with s1)
        {"id": "s6", "title": "Coda", "text": "Much later...",
         "describes": ["s1", "s4"]},
    ]
    turn = store.publish_turn("w", "v1", segments)
    return {"turn": turn.id}


class TwoBranchesSameFact(unittest.TestCase):
    def setUp(self):
        self.store = Store(None)
        self.ids = build_story(self.store)
        self.t1 = self.ids["turn"]

    def test_either_branch_satisfies_later_segment(self):
        s = self.store
        # Reader A takes the letter path, reader B the tape path.
        s.read_segment("A", "w", self.t1, "s2a")
        s.read_segment("A", "w", self.t1, "s3a")
        s.read_segment("B", "w", self.t1, "s2b")
        s.read_segment("B", "w", self.t1, "s3b")
        self.assertTrue(s.has_fact("A", "key_under_bridge", self.t1))
        self.assertTrue(s.has_fact("B", "key_under_bridge", self.t1))
        # Both can enter the finale; the server records the fact for each.
        self.assertTrue(s.can_enter("A", self.t1, "s4")["allowed"])
        self.assertTrue(s.can_enter("B", self.t1, "s4")["allowed"])
        s.read_segment("A", "w", self.t1, "s4")
        s.read_segment("B", "w", self.t1, "s4")

    def test_served_session_covers_both_branch_readers(self):
        s = self.store
        for reader, branch in (("A", "s2a"), ("B", "s2b")):
            s.read_segment(reader, "w", self.t1, branch)
        sess = s.register_session("Friday reading", "w", kind="served")
        s.register_reader(sess.id, "A")
        s.register_reader(sess.id, "B")
        result = s.serve(sess.id)
        # Each reader can take their own branch toward the shared finale; multiple
        # viable paths are surfaced rather than silently collapsing to one.
        self.assertIn("s3a", result["reader_menus"]["A"])
        self.assertIn("s3b", result["reader_menus"]["B"])
        self.assertEqual(set(result["goals"]), {"s4", "s5"})
        route = result["line"]["route"]
        # One continuous leader route reaches a convergence goal, using a single
        # branch reveal (the two branches carry the same fact, so one suffices).
        self.assertTrue({"s3a", "s3b"} & set(route["segments"]))
        self.assertTrue({"s4", "s5"} & set(route["segments"]))
        self.assertEqual(route["needs_remediation"], [])
        locked = s.lock(sess.id)
        self.assertEqual(locked["status"], "locked")
        self.assertEqual(locked["line"]["route"]["segments"], route["segments"])

    def test_same_title_different_outcome_does_not_confuse_facts(self):
        s = self.store
        # Publish a variant where the 'Under the Bridge' titles are reused but
        # the actual topic (fact) differs.  Matching by title would wrongly
        # grant the key; matching by fact does not.
        s.define_fact("vault_code", "the vault code")
        turn2 = s.publish_turn("w", "v2-variant", [
            {"id": "x1", "title": "Under the Bridge", "text": "the vault code",
             "discloses": ["vault_code"]},
            {"id": "x2", "title": "Finale", "text": "door", "requires": ["key_under_bridge"]},
        ])
        s.read_segment("C", "w", turn2.id, "x1")
        self.assertTrue(s.has_fact("C", "vault_code", turn2.id))
        # Did not learn the key despite the same segment title.
        self.assertFalse(s.has_fact("C", "key_under_bridge", turn2.id))


class Revisions(unittest.TestCase):
    def setUp(self):
        self.store = Store(None)
        self.ids = build_story(self.store)
        self.t1 = self.ids["turn"]
        s = self.store
        # B reads the tape path (s2b -> s3b) on v1.
        s.read_segment("B", "w", self.t1, "s2b")
        s.read_segment("B", "w", self.t1, "s3b")
        self.assertTrue(s.has_fact("B", "key_under_bridge", self.t1))

    def test_moved_reveal_invalidates_without_carry(self):
        s = self.store
        # v2 moves the key reveal from s3b to a new later segment s3b2.
        turn2 = s.publish_turn("w", "v2-moved", [
            {"id": "s2b", "title": "The Tape", "text": "hiss",
             "discloses": ["recording_confession"]},
            {"id": "s3b_old", "title": "Under the Bridge (old spot)",
             "text": "no longer says where", "describes": ["s3b2"]},
            {"id": "s3b2", "title": "Under the Bridge (moved)",
             "text": "the key is under the bridge",
             "discloses": ["key_under_bridge"]},
            {"id": "s4", "title": "The Finale", "text": "door",
             "requires": ["key_under_bridge"], "discloses": ["brother_alive"]},
        ], revision_of=self.t1)
        # The editor did NOT mark the moved reveal as unchanged.
        self.assertFalse(s.has_fact("B", "key_under_bridge", turn2.id))
        self.assertFalse(s.can_enter("B", turn2.id, "s4")["allowed"])
        # Reading the new location restores access.
        s.read_segment("B", "w", turn2.id, "s3b2")
        self.assertTrue(s.has_fact("B", "key_under_bridge", turn2.id))
        self.assertTrue(s.can_enter("B", turn2.id, "s4")["allowed"])

    def test_reveal_kept_when_segment_declared_unchanged(self):
        s = self.store
        # v2 keeps s3b identical and editor declares it unchanged.
        turn2 = s.publish_turn("w", "v2-kept", [
            {"id": "s2b", "title": "The Tape", "text": "hiss",
             "discloses": ["recording_confession"]},
            {"id": "s3b", "title": "Under the Bridge", "text": "the key is under the bridge",
             "discloses": ["key_under_bridge"]},
            {"id": "s4", "title": "The Finale", "text": "door",
             "requires": ["key_under_bridge"], "discloses": ["brother_alive"]},
        ], revision_of=self.t1, unchanged_segments=["s3b"])
        self.assertTrue(s.has_fact("B", "key_under_bridge", turn2.id))

    def test_fact_level_carry_survives_move(self):
        s = self.store
        turn2 = s.publish_turn("w", "v2-carried", [
            {"id": "s2b", "title": "The Tape", "text": "hiss",
             "discloses": ["recording_confession"]},
            {"id": "s3b2", "title": "Under the Bridge (moved)",
             "text": "the key is under the bridge",
             "discloses": ["key_under_bridge"]},
            {"id": "s4", "title": "The Finale", "text": "door",
             "requires": ["key_under_bridge"], "discloses": ["brother_alive"]},
        ], revision_of=self.t1, carry_facts=["key_under_bridge"])
        self.assertTrue(s.has_fact("B", "key_under_bridge", turn2.id))


class UnfinishedRead(unittest.TestCase):
    def setUp(self):
        self.store = Store(None)
        self.t1 = build_story(self.store)["turn"]

    def test_revoking_mistap_narrows_access(self):
        s = self.store
        # B mis-taps into s2b and s3b, then realises they never read the tape,
        # so the s2b record is removed.  The key fact came from s3b though and
        # s3b required the confession, so we remove the confession and the key.
        s.read_segment("B", "w", self.t1, "s2b")
        s.read_segment("B", "w", self.t1, "s3b")
        self.assertTrue(s.has_fact("B", "key_under_bridge", self.t1))
        s.revoke_read("B", "recording_confession", self.t1)
        s.revoke_read("B", "key_under_bridge", self.t1)
        self.assertFalse(s.has_fact("B", "recording_confession", self.t1))
        self.assertFalse(s.has_fact("B", "key_under_bridge", self.t1))
        self.assertFalse(s.can_enter("B", self.t1, "s4")["allowed"])
        # B must actually read the source again before re-entering.
        with self.assertRaises(BookClubError):
            s.read_segment("B", "w", self.t1, "s3b")
        s.read_segment("B", "w", self.t1, "s2b")
        s.read_segment("B", "w", self.t1, "s3b")
        self.assertTrue(s.can_enter("B", self.t1, "s4")["allowed"])


class Cycles(unittest.TestCase):
    def test_describes_never_grants_facts(self):
        s = Store(None)
        s.define_fact("secret", "the secret")
        s.define_work("cyc", "Loop")
        t = s.publish_turn("cyc", "loop-v1", [
            {"id": "a", "title": "A", "text": "refers to B", "describes": ["b"]},
            {"id": "b", "title": "B", "text": "refers to A", "describes": ["a"]},
        ])
        s.read_segment("R", "cyc", t.id, "a")
        s.read_segment("R", "cyc", t.id, "b")
        self.assertFalse(s.has_fact("R", "secret", t.id))
        # Even reading the whole cycle grants nothing the segments don't disclose.
        self.assertEqual(s.reader_view("R")["reads"].get(t.id, {}), {})


class OpenSessionAndRemediation(unittest.TestCase):
    def setUp(self):
        self.store = Store(None)
        self.t1 = build_story(self.store)["turn"]
        s = self.store
        s.read_segment("A", "w", self.t1, "s2a")
        s.read_segment("A", "w", self.t1, "s3a")
        s.read_segment("B", "w", self.t1, "s2b")
        # B has the confession but not yet the key.

    def test_latecomer_meets_group_and_make_up(self):
        s = self.store
        sess = s.register_session("Drop-in", "w", kind="open", goals=["s4"])
        s.register_reader(sess.id, "A")
        s.serve(sess.id)
        # A is already sitting in the group; B arrives late without the key.
        joined = s.join_open(sess.id, "B")
        self.assertTrue(joined["needs_make_up"])
        self.assertIn("s3b", joined["make_up_plan"])
        verdict = s.assess(sess.id, "B", "key_under_bridge")
        self.assertEqual(verdict["status"], "catch_up")
        practice = s.open_practice("B", sess.id, "key_under_bridge")
        self.assertFalse(s.has_fact("B", "key_under_bridge", self.t1))
        s.complete_practice(practice.id)
        self.assertTrue(s.has_fact("B", "key_under_bridge", self.t1))
        # Now B genuinely shares the fact; re-joining finds a real meeting point
        # (the goal segment s4 the group is heading to).
        joined2 = s.join_open(sess.id, "B")
        self.assertFalse(joined2["needs_make_up"])
        self.assertIn("s4", joined2["reachable_goals_now"])
        self.assertEqual(joined2["meeting_point"], "s4")

    def test_rebook_when_no_source_on_edition(self):
        s = self.store
        sess = s.register_session("Drop-in", "w", kind="open")
        s.register_reader(sess.id, "B")
        s.serve(sess.id)
        # Publish an edition where the key is never disclosed.
        turn2 = s.publish_turn("w", "v2-nokey", [
            {"id": "s2b", "title": "Tape", "text": "hiss", "discloses": ["recording_confession"]},
            {"id": "s4", "title": "Finale", "text": "door", "requires": ["key_under_bridge"]},
        ], revision_of=self.t1)
        # Session pinned at serve time to t1, but compare on the new edition.
        verdict = s.assess(sess.id, "B", "key_under_bridge", turn=turn2.id)
        self.assertEqual(verdict["status"], "rebook")
        with self.assertRaises(BookClubError) as ctx:
            s.open_practice("B", sess.id, "key_under_bridge", turn=turn2.id)
        self.assertEqual(ctx.exception.code, "needs_rebook")
        req = s.rebook(sess.id, "B", reason="key reveal absent in v2")
        self.assertEqual(req["kind"], "rebook")
        self.assertEqual(s.requests_for(sess.id)[0]["reader"], "B")


class StaffScheduling(unittest.TestCase):
    def setUp(self):
        self.store = Store(None)
        self.t1 = build_story(self.store)["turn"]
        s = self.store
        s.read_segment("A", "w", self.t1, "s2a")
        s.read_segment("A", "w", self.t1, "s3a")
        s.read_segment("B", "w", self.t1, "s2b")
        s.read_segment("B", "w", self.t1, "s3b")

    def test_overlapping_sessions_require_rebooking(self):
        s = self.store
        clerk = s.add_staff("Chen")
        s1 = s.register_session("Group 1", "w", kind="served")
        s.register_reader(s1.id, "A")
        s.serve(s1.id, clerk["id"], start=100.0, end=200.0)
        s.lock(s1.id)

        # Same pair of readers, facts literally unchanged, but the clerk is busy.
        s2 = s.register_session("Group 2", "w", kind="served")
        s.register_reader(s2.id, "A")
        s.register_reader(s2.id, "B")
        with self.assertRaises(BookClubError) as ctx:
            s.serve(s2.id, clerk["id"], start=150.0, end=250.0)
        self.assertEqual(ctx.exception.code, "staff_double_booked")
        self.assertEqual(ctx.exception.extra["recommendation"], "rebook")
        # A non-overlapping window is fine.
        ok = s.serve(s2.id, clerk["id"], start=200.0, end=300.0)
        self.assertEqual(ok["session"], s2.id)

    def test_schedule_staff_entry_point_is_guarded_too(self):
        s = self.store
        clerk = s.add_staff("Chen")
        s1 = s.register_session("One", "w")
        s2 = s.register_session("Two", "w")
        s.schedule_staff(s1.id, clerk["id"], 10.0, 20.0)
        with self.assertRaises(BookClubError) as ctx:
            s.schedule_staff(s2.id, clerk["id"], 15.0, 25.0)
        self.assertEqual(ctx.exception.code, "staff_double_booked")


class ScenarioEndToEnd(unittest.TestCase):
    """The headline scenario: two readers, two branches, one fact, one finale."""

    def test_full_flow(self):
        s = Store(None)
        t1 = build_story(s)["turn"]
        clerk = s.add_staff("Chen")

        # Readers take different branches to the same fact.
        s.read_segment("A", "w", t1, "s2a")
        s.read_segment("A", "w", t1, "s3a")
        s.read_segment("B", "w", t1, "s2b")
        s.read_segment("B", "w", t1, "s3b")
        self.assertEqual(s.fact_sources("A", "key_under_bridge", t1),
                         [f"{t1}:s3a"])
        self.assertEqual(s.fact_sources("B", "key_under_bridge", t1),
                         [f"{t1}:s3b"])

        # Prepare and lock the served session before the doors open.
        s1 = s.register_session("Friday reading", "w", kind="served")
        s.register_reader(s1.id, "A")
        s.register_reader(s1.id, "B")
        served = s.serve(s1.id, clerk["id"], start=0.0, end=60.0)
        self.assertEqual(served["line"]["prerequisites"]["A"]["key_under_bridge"],
                         [f"{t1}:s3a"])
        locked = s.lock(s1.id)
        self.assertEqual(locked["status"], "locked")

        # Both can jointly run the finale that needs the shared fact.
        for reader in ("A", "B"):
            self.assertTrue(s.can_enter(reader, t1, "s4")["allowed"])
        s.read_segment("A", "w", t1, "s4")
        s.read_segment("B", "w", t1, "s4")
        self.assertTrue(s.has_fact("A", "brother_alive", t1))
        self.assertTrue(s.has_fact("B", "brother_alive", t1))


class Persistence(unittest.TestCase):
    def test_restart_preserves_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            s = Store(path)
            t1 = build_story(s)["turn"]
            s.read_segment("A", "w", t1, "s2a")
            s.read_segment("A", "w", t1, "s3a")
            sess = s.register_session("Friday", "w", kind="served")
            s.register_reader(sess.id, "A")
            s.serve(sess.id)
            s.lock(sess.id)
            snapshot = s.take_snapshot()
            with open(path, encoding="utf-8") as fh:
                on_disk = json.load(fh)

            # Simulate a fresh process reading the same file.
            s2 = Store(path)
            self.assertTrue(s2.has_fact("A", "key_under_bridge", t1))
            self.assertEqual(s2.session_view(sess.id)["status"], "locked")
            self.assertEqual(s2.take_snapshot()["reads"], snapshot["reads"])
            self.assertEqual(on_disk["works"]["w"]["current"], t1)
            # New ids continue without colliding.
            s2.define_fact("another", "another fact")
            self.assertNotIn(s2.facts["another"].id, [f.id for f in s.facts.values()])


class IntakeComparison(unittest.TestCase):
    """The two passes from the same un-locked intake point."""

    def setUp(self):
        self.store = Store(None)
        self.t1 = build_story(self.store)["turn"]
        s = self.store
        s.read_segment("A", "w", self.t1, "s2a")   # letter branch entry
        s.read_segment("A", "w", self.t1, "s3a")   # key, via the letter
        s.read_segment("B", "w", self.t1, "s2b")   # tape branch entry
        s.read_segment("B", "w", self.t1, "s3b")   # key, via the tape

    def _moved_turn(self):
        # v2 keeps the *letter* reveal (s3a) verbatim and unchanged, moving only
        # the *tape* reveal from s3b into a new later segment s3c.
        return self.store.publish_turn("w", "v2", [
            {"id": "s2a", "title": "Letter", "text": "warn", "discloses": ["letter_warning"]},
            {"id": "s2b", "title": "Tape", "text": "confession", "discloses": ["recording_confession"]},
            {"id": "s3a", "title": "Under the Bridge (letter)", "text": "key",
             "discloses": ["key_under_bridge"], "requires": ["letter_warning"]},
            {"id": "s3c", "title": "Under the Bridge (tape, moved)", "text": "key",
             "discloses": ["key_under_bridge"], "requires": ["recording_confession"]},
            {"id": "s4", "title": "Finale", "text": "door",
             "requires": ["key_under_bridge"], "discloses": ["brother_alive"]},
        ], revision_of=self.t1, unchanged_segments=["s2a", "s2b", "s3a"]).id

    def test_first_pass_moved_reveal_only_hits_tape_reader(self):
        t2 = self._moved_turn()
        cmp_a = self.store.compare_intake("A", "key_under_bridge",
                                          turn_a=self.t1, turn_b=t2, segment="s4")
        cmp_b = self.store.compare_intake("B", "key_under_bridge",
                                          turn_a=self.t1, turn_b=t2, segment="s4")
        self.assertFalse(cmp_a["regressed"])   # letter reveal unchanged
        self.assertTrue(cmp_a["turn_b"]["has_fact"])
        self.assertTrue(cmp_b["regressed"])    # tape reveal moved to unread s3c
        self.assertEqual(cmp_b["turn_a"]["status"], "has")
        self.assertEqual(cmp_b["turn_b"]["status"], "catch_up")
        self.assertFalse(cmp_b["turn_b"]["has_fact"])
        self.assertFalse(cmp_b["turn_b"]["entry_allowed"])

    def test_second_pass_revert_then_remove_mistap(self):
        self._moved_turn()
        # Restore v1 as current, then drop B's never-finished key record.
        self.store.revert_turn(self.t1)
        self.assertEqual(self.store.works["w"]["current"], self.t1)
        self.store.revoke_read("B", "key_under_bridge", self.t1)
        self.assertFalse(self.store.has_fact("B", "key_under_bridge", self.t1))
        self.assertFalse(self.store.can_enter("B", self.t1, "s4")["allowed"])
        # A is untouched and still eligible.
        self.assertTrue(self.store.can_enter("A", self.t1, "s4")["allowed"])

    def test_moved_segment_unchanged_does_not_carry_the_moved_fact(self):
        # s3c is where the tape reveal now lives; B never read it.  Declaring the
        # old s3b impossible (it is gone); even if s2b stays unchanged, the key
        # does not follow because s2b never disclosed it.
        t2 = self._moved_turn()
        self.assertFalse(self.store.has_fact("B", "key_under_bridge", t2))
        self.assertTrue(self.store.has_fact("B", "recording_confession", t2))


class FrontDesk(unittest.TestCase):
    def setUp(self):
        self.store = Store(None)
        self.t1 = build_story(self.store)["turn"]

    def test_facts_vs_slot(self):
        s = self.store
        s.read_segment("A", "w", self.t1, "s2a")
        s.read_segment("A", "w", self.t1, "s3a")
        clerk = s.add_staff("Chen")
        s1 = s.register_session("first", "w", kind="served", goals=["s4"])
        s.register_reader(s1.id, "A")
        s.serve(s1.id, clerk["id"], start=0.0, end=60.0)
        s2 = s.register_session("second", "w", kind="served", goals=["s4"])
        s.register_reader(s2.id, "A")
        # Facts fine, but slot overlaps -> rebook.
        decision = s.front_desk(s2.id, "A", "key_under_bridge", staff_id=clerk["id"],
                                start=30.0, end=90.0)
        self.assertEqual(decision["fact_state"]["status"], "has")
        self.assertEqual(decision["action"], "rebook")
        self.assertIn("overlapping", " ".join(decision["reasons"]))
        # No slot query -> facts only, and facts are fine.
        decision2 = s.front_desk(s2.id, "A", "key_under_bridge")
        self.assertEqual(decision2["action"], "proceed")

    def test_missing_fact_with_source_is_catch_up(self):
        s = self.store
        sess = s.register_session("open", "w", kind="open", goals=["s4"])
        s.register_reader(sess.id, "B")
        decision = s.front_desk(sess.id, "B", "key_under_bridge")
        self.assertEqual(decision["action"], "catch_up")
        self.assertEqual(decision["fact_state"]["status"], "catch_up")

    def test_fact_never_disclosed_is_rebook(self):
        s = self.store
        s.define_fact("phantom", "never revealed")
        sess = s.register_session("open", "w", kind="open")
        s.register_reader(sess.id, "B")
        decision = s.front_desk(sess.id, "B", "phantom")
        self.assertEqual(decision["action"], "rebook")
        with self.assertRaises(BookClubError):
            s.open_practice("B", sess.id, "phantom")


class PracticeCarryForward(unittest.TestCase):
    def setUp(self):
        self.store = Store(None)
        self.t1 = build_story(self.store)["turn"]
        self.store.read_segment("B", "w", self.t1, "s2b")
        self.store.read_segment("B", "w", self.t1, "s3b")
        # v2 moves the reveal; B loses the key on v2.
        self.t2 = self.store.publish_turn("w", "v2", [
            {"id": "s2b", "title": "Tape", "text": "hiss", "discloses": ["recording_confession"]},
            {"id": "s3b", "title": "Under the Bridge (moved)", "text": "key",
             "discloses": ["key_under_bridge"], "requires": ["recording_confession"]},
            {"id": "s4", "title": "Finale", "text": "door",
             "requires": ["key_under_bridge"], "discloses": ["brother_alive"]},
        ], revision_of=self.t1).id

    def test_make_up_read_carries_into_next_session(self):
        s = self.store
        # B reads the v2 tape entry so she holds the confession on v2; the key
        # reveal moved to s3b, which she has not read.
        s.read_segment("B", "w", self.t2, "s2b")
        self.assertTrue(s.has_fact("B", "recording_confession", self.t2))
        sess = s.register_session("open", "w", kind="open", turn=self.t2, goals=["s4"])
        s.register_reader(sess.id, "B")
        s.serve(sess.id)
        self.assertFalse(s.has_fact("B", "key_under_bridge", self.t2))
        p = s.open_practice("B", sess.id, "key_under_bridge")
        self.assertEqual(p.segment, "s3b")  # extends B's own branch
        s.complete_practice(p.id)
        self.assertTrue(s.has_fact("B", "key_under_bridge", self.t2))
        facts = s.reader_facts_from_practices("B")
        self.assertIn(self.t2, facts["key_under_bridge"])
        # A different session pinned to the same v2 sees B as eligible too.
        s2 = s.register_session("next", "w", kind="served", turn=self.t2, goals=["s4"])
        s.register_reader(s2.id, "B")
        self.assertEqual(s.assess(s2.id, "B", "key_under_bridge")["status"], "has")
        served = s.serve(s2.id)
        self.assertEqual(served["line"]["route"]["needs_remediation"], [])

    def test_practice_is_idempotent(self):
        s = self.store
        s.read_segment("B", "w", self.t2, "s2b")
        sess = s.register_session("open", "w", kind="open", turn=self.t2, goals=["s4"])
        s.register_reader(sess.id, "B")
        p = s.open_practice("B", sess.id, "key_under_bridge")
        first = s.complete_practice(p.id)
        second = s.complete_practice(p.id)
        self.assertEqual(first["completed_at"], second["completed_at"])


class DescribesCyclesCannotManufacture(unittest.TestCase):
    def test_cyclic_describes_never_reveal(self):
        s = Store(None)
        s.define_fact("truth", "the truth")
        s.define_fact("done", "the ending")
        s.define_work("cyc", "Cycle")
        t = s.publish_turn("cyc", "v1", [
            {"id": "a", "title": "A", "describes": ["b"]},
            {"id": "b", "title": "B", "describes": ["c"]},
            {"id": "c", "title": "C", "describes": ["a"]},
            {"id": "end", "title": "End", "requires": ["truth"], "discloses": ["done"]},
        ])
        for seg in ("a", "b", "c"):
            s.read_segment("R", "cyc", t.id, seg)
        self.assertFalse(s.has_fact("R", "truth", t.id))
        self.assertFalse(s.can_enter("R", t.id, "end")["allowed"])


if __name__ == "__main__":
    unittest.main()
