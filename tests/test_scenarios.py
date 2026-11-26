import json
import tempfile
import unittest
import os

from journal.service import JournalService, JournalError, NeedsEditorDecision


def new_rolling(path=":memory:", issue="I1"):
    s = JournalService(path)
    s.create_issue(issue, "serial", "rolling")
    return s


class SinglePieceUnconditional(unittest.TestCase):
    """The 'open the one unconditional piece' scenario."""

    def test_open_and_receipt_and_query(self):
        s = new_rolling()
        s.add_translator("t1", "T1", ["周二"])
        s.add_piece("I1", "island", "海岛", 6, 1, ["t1"], wish_points=99)

        # Advertised wish (99) is NOT money: without a pledge nothing opens.
        self.assertEqual(s.list_candidates("I1"), [])
        with self.assertRaises(JournalError):
            s.open_piece("I1", "no-money", "island")

        s.pledge("I1", "p1", "alice", 6, accepts=["island"])
        cands = s.list_candidates("I1")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["pieces"][0]["points"], 6)

        receipt = s.open_piece("I1", "req-open-island", "island")
        self.assertEqual(receipt["kind"], "open_piece")
        p = receipt["pieces"][0]
        self.assertEqual(p["piece_id"], "island")
        self.assertEqual(p["translator_id"], "t1")
        self.assertEqual(p["slots"], ["周二"])
        self.assertEqual(p["points"], 6)
        self.assertEqual(receipt["supporter_deducted"], {"alice": 6})
        self.assertEqual(receipt["pledge_deducted"], {"p1": 6})

        q = s.query_issue("I1")
        self.assertTrue(q["pieces"]["island"]["opened"])
        self.assertEqual(q["pieces"]["island"]["translator"]["translator_id"], "t1")
        self.assertEqual(q["pieces"]["island"]["translator"]["slots"], ["周二"])
        self.assertEqual(q["pieces"]["island"]["funding"],
                         [{"pledge_id": "p1", "supporter": "alice", "points": 6}])

    def test_second_open_replay_is_idempotent(self):
        s = new_rolling()
        s.add_translator("t1", "T1", ["周二"])
        s.add_piece("I1", "island", "海岛", 6, 1, ["t1"])
        s.pledge("I1", "p1", "alice", 6, accepts=["island"])
        r1 = s.open_piece("I1", "req1", "island")
        r2 = s.open_piece("I1", "req1", "island")
        self.assertEqual(r1["receipt_id"], r2["receipt_id"])
        # Only one deduction row ever existed.
        q = s.query_issue("I1")
        self.assertEqual(q["pieces"]["island"]["funding"],
                         [{"pledge_id": "p1", "supporter": "alice", "points": 6}])
        # A *different* request id on an already-open piece is rejected.
        with self.assertRaises(JournalError):
            s.open_piece("I1", "req2", "island")


class ConditionalCannotOpen(unittest.TestCase):
    """New issue: the six points are now 'pay only if the other piece opens'."""

    def test_conditional_with_untranslatable_other_piece_fails(self):
        s = new_rolling(path=":memory:", issue="I2")
        s.add_translator("t1", "T1", ["周二"])
        s.add_piece("I2", "island", "海岛", 6, 1, ["t1"])
        # 'other' has nobody who can translate it.
        s.add_piece("I2", "other", "另一篇", 6, 1, [])
        s.pledge("I2", "p1", "alice", 6, accepts=["island"], requires=["other"])

        # 'other' can never open (no translator), so its condition never holds.
        self.assertEqual(s.list_candidates("I2"), [])
        with self.assertRaises(JournalError):
            s.open_piece("I2", "req", "island")


class RestoredUnconditionalButSlotGone(unittest.TestCase):
    """Restore unconditional support, but the Tuesday shift is already gone."""

    def test_slot_already_booked_globally(self):
        path = tempfile.mktemp(suffix=".db")
        try:
            s = JournalService(path)
            s.create_issue("I1", "a", "rolling")
            s.add_translator("t1", "T1", ["周二"])
            s.add_piece("I1", "island", "海岛", 6, 1, ["t1"])
            s.pledge("I1", "p1", "alice", 6, accepts=["island"])
            s.open_piece("I1", "req1", "island")  # consumes t1's 周二

            s.create_issue("I2", "b", "rolling")
            s.add_piece("I2", "island2", "海岛二", 6, 1, ["t1"])
            s.pledge("I2", "p2", "alice", 6, accepts=["island2"])  # unconditional
            # t1's only shift is booked by I1, so I2 cannot place it.
            self.assertEqual(s.list_candidates("I2"), [])
            with self.assertRaises(JournalError):
                s.open_piece("I2", "req2", "island2")
            # Nothing was deducted for the failed attempt.
            q = s.query_issue("I2")
            self.assertEqual(q["pieces"]["island2"]["funding"], [])
            s.db.close()
        finally:
            os.path.exists(path) and os.remove(path)


class MutualConditionalJoint(unittest.TestCase):
    """Two pledges that only pay if each other's piece opens together."""

    def test_joint_cutoff_satisfies_mutual_wait(self):
        s = JournalService(":memory:")
        s.create_issue("J", "joint", "joint")
        s.add_translator("t1", "T1", ["周二"])
        s.add_translator("t2", "T2", ["周三"])
        s.add_piece("J", "bridge", "桥梁", 6, 1, ["t1"])
        s.add_piece("J", "island", "海岛", 6, 1, ["t2"])
        s.pledge("J", "pa", "alice", 6, accepts=["bridge"], requires=["island"])
        s.pledge("J", "pb", "bob", 6, accepts=["island"], requires=["bridge"])

        # Neither single piece is fundable on its own; only the joint batch is.
        cands = s.list_candidates("J")
        self.assertEqual(len(cands), 1)
        self.assertEqual(sorted(p["piece_id"] for p in cands[0]["pieces"]),
                         ["bridge", "island"])

        receipt = s.open_joint("J", "req-joint")  # exactly one batch -> auto
        got = {p["piece_id"]: p for p in receipt["pieces"]}
        self.assertEqual(got["bridge"]["translator_id"], "t1")
        self.assertEqual(got["bridge"]["funding"],
                         [{"supporter": "alice", "pledge_id": "pa", "points": 6}])
        self.assertEqual(got["island"]["translator_id"], "t2")
        self.assertEqual(got["island"]["funding"],
                         [{"supporter": "bob", "pledge_id": "pb", "points": 6}])
        self.assertEqual(receipt["supporter_deducted"], {"alice": 6, "bob": 6})

        # Reader can check the same facts after the fact.
        q = s.query_issue("J")
        self.assertTrue(q["pieces"]["bridge"]["opened"])
        self.assertTrue(q["pieces"]["island"]["opened"])

    def test_rolling_cannot_break_mutual_wait(self):
        s = JournalService(":memory:")
        s.create_issue("R", "rolling", "rolling")
        s.add_translator("t1", "T1", ["周二"])
        s.add_translator("t2", "T2", ["周三"])
        s.add_piece("R", "bridge", "桥梁", 6, 1, ["t1"])
        s.add_piece("R", "island", "海岛", 6, 1, ["t2"])
        s.pledge("R", "pa", "alice", 6, accepts=["bridge"], requires=["island"])
        s.pledge("R", "pb", "bob", 6, accepts=["island"], requires=["bridge"])
        self.assertEqual(s.list_candidates("R"), [])
        with self.assertRaises(JournalError):
            s.open_piece("R", "r1")


class EditorDecision(unittest.TestCase):
    """Three pieces, two translators, money for only two -> editor picks."""

    def _build(self):
        s = JournalService(":memory:")
        s.create_issue("D", "pick", "joint")
        s.add_translator("t1", "T1", ["周二", "周三"])
        s.add_translator("t2", "T2", ["周四", "周五"])
        for pid, title in [("a", "A"), ("b", "B"), ("c", "C")]:
            s.add_piece("D", pid, title, 5, 1, ["t1", "t2"])
        s.pledge("D", "p1", "alice", 10, accepts=["a", "b", "c"])
        return s

    def test_multiple_maximal_batches_need_editor(self):
        s = self._build()
        cands = s.list_candidates("D")
        ids = sorted(c["candidate_id"] for c in cands)
        self.assertEqual(ids, ["a|b", "a|c", "b|c"])

        with self.assertRaises(NeedsEditorDecision) as ctx:
            s.open_joint("D", "req")
        self.assertEqual(len(ctx.exception.candidates), 3)

        # Editor chooses a specific pairing; recorded in the issue's receipt.
        receipt = s.open_joint("D", "req", selection="a|c")
        self.assertEqual([p["piece_id"] for p in receipt["pieces"]], ["a", "c"])
        self.assertEqual(sum(p["points"] for p in receipt["pieces"]), 10)
        # Slots are on distinct shifts of the two translators.
        used = {(p["translator_id"], s_) for p in receipt["pieces"] for s_ in p["slots"]}
        self.assertEqual(len(used), 2)

    def test_select_by_piece_list_also_works(self):
        s = self._build()
        receipt = s.open_joint("D", "req", selection=["b", "c"])
        self.assertEqual([p["piece_id"] for p in receipt["pieces"]], ["b", "c"])

    def test_editor_may_pick_a_smaller_subset(self):
        # The maximal batches all have size 2; a single piece is still legal.
        s = self._build()
        receipt = s.open_joint("D", "req", selection=["a"])
        self.assertEqual([p["piece_id"] for p in receipt["pieces"]], ["a"])
        self.assertEqual(receipt["supporter_deducted"], {"alice": 5})


class Withdrawal(unittest.TestCase):
    def test_withdraw_removes_funding(self):
        s = new_rolling()
        s.add_translator("t1", "T1", ["周二"])
        s.add_piece("I1", "island", "海岛", 6, 1, ["t1"])
        s.pledge("I1", "p1", "alice", 6, accepts=["island"])
        self.assertEqual([c["candidate_id"] for c in s.list_candidates("I1")],
                         ["island"])
        s.withdraw_pledge("I1", "p1")
        self.assertEqual(s.list_candidates("I1"), [])
        with self.assertRaises(JournalError):
            s.open_piece("I1", "r", "island")

    def test_cannot_withdraw_after_paid(self):
        s = new_rolling()
        s.add_translator("t1", "T1", ["周二"])
        s.add_piece("I1", "island", "海岛", 6, 1, ["t1"])
        s.pledge("I1", "p1", "alice", 6, accepts=["island"])
        s.open_piece("I1", "r1", "island")
        with self.assertRaises(JournalError):
            s.withdraw_pledge("I1", "p1")


class SupporterCapIsPerIssue(unittest.TestCase):
    """A supporter's five-point ceiling is NOT five points per piece."""

    def test_cap_not_per_piece(self):
        s = JournalService(":memory:")
        s.create_issue("C", "cap", "joint")
        s.add_translator("t1", "T1", ["周二", "周三"])
        s.add_piece("C", "x", "X", 5, 1, ["t1"])
        s.add_piece("C", "y", "Y", 5, 1, ["t1"])
        s.pledge("C", "p1", "alice", 5, accepts=["x", "y"], supporter_cap=5)

        # Can afford only one 5-point piece; both would need 10 > cap.
        cands = s.list_candidates("C")
        self.assertEqual(sorted(c["candidate_id"] for c in cands), ["x", "y"])
        with self.assertRaises(NeedsEditorDecision):
            s.open_joint("C", "req")
        # No batch of both pieces is offered.
        self.assertNotIn("x|y", [c["candidate_id"] for c in cands])


class PersistenceAndIdempotency(unittest.TestCase):
    def test_survives_restart_and_replays(self):
        path = tempfile.mktemp(suffix=".db")
        try:
            s = JournalService(path)
            s.create_issue("J", "persist", "joint")
            s.add_translator("t1", "T1", ["周二"])
            s.add_translator("t2", "T2", ["周三"])
            s.add_piece("J", "bridge", "桥梁", 6, 1, ["t1"])
            s.add_piece("J", "island", "海岛", 6, 1, ["t2"])
            s.pledge("J", "pa", "alice", 6, accepts=["bridge"], requires=["island"])
            s.pledge("J", "pb", "bob", 6, accepts=["island"], requires=["bridge"])
            receipt = s.open_joint("J", "req1")
            s.db.close()

            # Cold restart: a brand-new process-equivalent service.
            s2 = JournalService(path)
            q = s2.query_issue("J")
            self.assertTrue(q["pieces"]["bridge"]["opened"])
            self.assertTrue(q["pieces"]["island"]["opened"])
            self.assertIn(receipt["receipt_id"], q["receipts"])
            got = s2.get_receipt(receipt["receipt_id"])
            self.assertEqual(got["pledge_deducted"], {"pa": 6, "pb": 6})

            # Resending the same cutoff does not deduct twice.
            again = s2.open_joint("J", "req1")
            self.assertEqual(again["receipt_id"], receipt["receipt_id"])
            q2 = s2.query_issue("J")
            self.assertEqual(len(q2["pieces"]["bridge"]["funding"]), 1)
            s2.db.close()
        finally:
            os.path.exists(path) and os.remove(path)

    def test_global_slot_guard_across_issues(self):
        s = JournalService(":memory:")
        s.create_issue("A", "a", "joint")
        s.add_translator("t1", "T1", ["周二"])
        s.add_piece("A", "p", "P", 3, 1, ["t1"])
        s.pledge("A", "pa", "alice", 3, accepts=["p"])
        s.open_joint("A", "ra", "p")

        s.create_issue("B", "b", "joint")
        s.add_piece("B", "q", "Q", 3, 1, ["t1"])
        s.pledge("B", "pb", "bob", 3, accepts=["q"])
        with self.assertRaises(JournalError):
            s.open_joint("B", "rb", "q")


if __name__ == "__main__":
    unittest.main(verbosity=2)
