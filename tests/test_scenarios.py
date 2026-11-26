"""Scenario tests mirroring the brief end-to-end through the Service layer.

Covers:
* the small example (direct 12 -> true 10; subtract 22-12 -> true 10);
* editing the first usage receipt to a no-bias replacement piece;
* restoring the receipt but demanding at least 11;
* two groups of same-box/different-piece boards + a genuinely releasable plate
  (service must NOT recall the whole box);
* two workers claiming from the same old screen, surviving a restart, ending
  with exactly one effective disposition and its measurement chain;
* an on-site recheck remeasure that flips a work order;
* a workshop remeasure reading that flips a work order.
"""

import os
import tempfile
import unittest

from app.engine import Service
from app.store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "service.db")
        self.svc = Service(Store(self.db))

    def reopen(self):
        self.svc = Service(Store(self.db))
        return self.svc

    # convenience ----------------------------------------------------------
    def recipe(self):
        svc = self.svc
        # P2 is really +2 off; P0 is a genuine no-bias replacement.
        svc.upsert_piece(piece_id="P2", kind="start", nominal_bias=0, true_bias=2)
        svc.upsert_piece(piece_id="P0", kind="start", nominal_bias=0, true_bias=0)
        svc.create_ruler(ruler_id="R1")

    def board(self, bid, **kw):
        kw.setdefault("tenant_id", "T1")
        kw.setdefault("ref_point_id", f"{bid}-ref")
        return self.svc.create_board(board_id=bid, **kw)

    def receipt(self, bid, start="P2", reading=None, a=None, b=None):
        if a is None:
            payload = {"board_id": bid, "ruler_id": "R1", "method": "direct",
                       "config": {"start_piece": start}, "reading": reading}
        else:
            payload = {"board_id": bid, "ruler_id": "R1", "method": "subtract",
                       "config": {"start_piece": start},
                       "reading_a": a, "reading_b": b}
        return self.svc.submit_usage_receipt(payload)

    def wo_for(self, calib_id, board_id):
        for w in self.svc.list_work_orders(calibration_id=calib_id):
            if w["board_id"] == board_id and w["scope"] == "board":
                return w
        return None


class SmallExampleTests(Base):
    def setUp(self):
        super().setUp()
        self.recipe()
        # Board A: direct read 12 with +2 piece -> true 10, accepts exactly 10.
        self.board("A", min_len=10, max_len=10)
        self.receipt("A", start="P2", reading=12)
        # Board B: subtract 22-12 with same piece -> true 10, accepts exactly 10.
        self.board("B", min_len=10, max_len=10)
        self.receipt("B", start="P2", a=12, b=22)

    def test_both_read_as_ten_and_release(self):
        self.assertEqual(self.svc.evaluate_board("A")["true_length"], 10)
        self.assertEqual(self.svc.evaluate_board("B")["true_length"], 10)
        cal = self.svc.add_calibration(piece_biases={"P2": 2})
        self.svc.dispatch_calibration(calibration_id=cal["id"])
        plan = self.svc.plan(calibration_id=cal["id"])
        self.assertEqual(plan["released_boards"], ["A", "B"])
        self.assertEqual(plan["retest"]["must_retest"], [])

    def test_edit_receipt_to_no_bias_piece_breaks_direct_board(self):
        cal = self.svc.add_calibration(piece_biases={"P2": 2})
        self.svc.dispatch_calibration(calibration_id=cal["id"])
        # change first usage receipt: now the ruler carried the no-bias P0;
        # same raw 12 -> true 12 -> A no longer fits "exactly 10".
        self.svc.replace_usage_receipt(
            board_id="A",
            payload={"board_id": "A", "ruler_id": "R1", "method": "direct",
                     "config": {"start_piece": "P0"}, "reading": 12})
        ev = self.svc.evaluate_board("A")
        self.assertEqual(ev["true_length"], 12)
        self.assertEqual(ev["action"], "recheck")
        self.svc.dispatch_calibration(calibration_id=cal["id"])
        plan = self.svc.plan(calibration_id=cal["id"])
        self.assertIn("A", plan["recheck_boards"])
        self.assertEqual(plan["retest"]["must_retest"], ["A"])
        # B still correct: subtraction cancels the piece bias regardless.
        self.assertIn("B", plan["released_boards"])

    def test_restore_receipt_with_min_eleven_still_opens_order(self):
        cal = self.svc.add_calibration(piece_biases={"P2": 2})
        self.svc.dispatch_calibration(calibration_id=cal["id"])
        # restore original receipt (P2, read 12 -> true 10)
        self.svc.replace_usage_receipt(
            board_id="A",
            payload={"board_id": "A", "ruler_id": "R1", "method": "direct",
                     "config": {"start_piece": "P2"}, "reading": 12})
        # but the assembly now needs at least 11
        self.svc.set_requirement(board_id="A", min_len=11, max_len=None)
        ev = self.svc.evaluate_board("A")
        self.assertEqual(ev["true_length"], 10)
        self.assertEqual(ev["action"], "recheck")
        self.svc.dispatch_calibration(calibration_id=cal["id"])
        plan = self.svc.plan(calibration_id=cal["id"])
        self.assertIn("A", plan["recheck_boards"])
        # a concrete work order exists for A
        wo = self.wo_for(cal["id"], "A")
        self.assertIsNotNone(wo)
        self.assertEqual(wo["kind"], "recheck")
        self.assertEqual(wo["requirement"], {"min": 11, "max": None})

    def test_recheck_remeasure_changes_work_order(self):
        cal = self.svc.add_calibration(piece_biases={"P2": 2})
        self.svc.set_requirement(board_id="A", min_len=11, max_len=None)
        self.svc.dispatch_calibration(calibration_id=cal["id"])
        wo = self.wo_for(cal["id"], "A")
        self.assertEqual(wo["kind"], "recheck")
        # on-site recheck: board actually measures 11 true (read 13 with P2)
        res = self.svc.complete_recheck(
            board_id="A",
            payload={"method": "direct", "ruler_id": "R1",
                     "config": {"start_piece": "P2"}, "reading": 13})
        self.assertEqual(res["evaluation"]["true_length"], 11)
        self.assertEqual(res["changed_from"], "recheck")
        self.assertEqual(res["work_order"]["kind"], "release")
        self.assertEqual(res["work_order"]["status"], "completed")


class BoxScopeTests(Base):
    """Two groups of same-box/different-piece + a genuinely recheck-releasable
    small plate.

    Holding the box must not recall every board.  Pieces start at their nominal
    bias (the truth is not yet known); the calibration is what reveals the
    discrepancy and flips the affected boards.  Only boards whose own piece +
    own measuring method actually fail are touched; a small plate that is wrong
    only because of a mis-recorded piece is rechecked on site and released, not
    recalled with the box.
    """

    def setUp(self):
        super().setUp()
        svc = self.svc
        # truth NOT known yet -- only nominal bias is recorded
        svc.upsert_piece(piece_id="G1", kind="start", nominal_bias=0)
        svc.upsert_piece(piece_id="G2", kind="start", nominal_bias=0)
        svc.upsert_piece(piece_id="G3", kind="start", nominal_bias=0)
        svc.create_ruler(ruler_id="R1")
        svc.create_box(box_id="BOX", piece_ids=["G1", "G2", "G3"], tenant_id="T1")

        # group one, board B1: G1 used directly; true board is 10, needs 12.
        self.board("B1", min_len=12, max_len=12, box_id="BOX")
        self.receipt("B1", start="G1", reading=12)
        # group one, board B2: G1 used by subtraction -> bias cancels; 10 fits 10.
        self.board("B2", min_len=10, max_len=10, box_id="BOX")
        self.receipt("B2", start="G1", a=12, b=22)
        # group two, board B3: genuinely good G2 used directly; 10 fits 10.
        self.board("B3", min_len=10, max_len=10, box_id="BOX")
        self.receipt("B3", start="G2", reading=10)
        # small plate: receipt mis-recorded G3 but the board is really 6 long
        # (it was actually measured on G2).  Under nominal it looks fine.
        self.board("PLATE", min_len=6, max_len=6, box_id="BOX")
        self.receipt("PLATE", start="G3", reading=6)

    def test_before_calibration_everything_looks_fine(self):
        for bid in ("B1", "B2", "B3", "PLATE"):
            self.assertEqual(self.svc.evaluate_board(bid)["action"], "release",
                             f"{bid} should still look fine pre-calibration")

    def test_only_failing_boards_are_reworked(self):
        cal = self.svc.add_calibration(piece_biases={"G1": 2, "G2": 0, "G3": 2})
        self.svc.dispatch_calibration(calibration_id=cal["id"], box_id="BOX")
        plan = self.svc.plan(calibration_id=cal["id"])
        self.assertEqual(set(plan["recheck_boards"]), {"B1", "PLATE"})
        self.assertEqual(set(plan["released_boards"]), {"B2", "B3"})
        self.assertEqual(plan["recall_boards"], [])   # nothing recalled
        # only pieces actually used by failing, bias-sensitive boards
        self.assertEqual(set(plan["replace_pieces"]), {"G1", "G3"})

    def test_small_plate_recheck_releases_on_site(self):
        cal = self.svc.add_calibration(piece_biases={"G1": 2, "G2": 0, "G3": 2})
        self.svc.dispatch_calibration(calibration_id=cal["id"], box_id="BOX")
        # on-site recheck with the correct piece G2 (independent, no bias):
        # the plate really is 6 and satisfies the clearance -> released.
        res = self.svc.complete_recheck(
            board_id="PLATE",
            payload={"method": "direct", "ruler_id": "R1",
                     "config": {"start_piece": "G2"}, "reading": 6})
        self.assertEqual(res["evaluation"]["true_length"], 6)
        self.assertEqual(res["work_order"]["kind"], "release")
        self.assertEqual(res["work_order"]["status"], "completed")
        plan = self.svc.plan(calibration_id=cal["id"])
        self.assertNotIn("PLATE", plan["recall_boards"])
        self.assertNotIn("PLATE", plan["retest"]["must_retest"])
        self.assertIn("PLATE", plan["released_boards"])



class ClaimRestartTests(Base):
    def setUp(self):
        super().setUp()
        self.recipe()
        self.board("A", min_len=12, max_len=12)  # will fail after calibration
        self.receipt("A", start="P2", reading=12)  # true 10 -> fails need 12
        self.cal = self.svc.add_calibration(piece_biases={"P2": 2})
        self.svc.dispatch_calibration(calibration_id=self.cal["id"])
        self.wo = self.wo_for(self.cal["id"], "A")

    def test_two_workers_same_screen_one_disposition(self):
        c1 = self.svc.claim(work_order_id=self.wo["id"], worker_id="W1",
                            screen_token="old-screen-42")
        c2 = self.svc.claim(work_order_id=self.wo["id"], worker_id="W2",
                            screen_token="old-screen-42")
        self.assertFalse(c1["deduplicated"])
        self.assertTrue(c2["deduplicated"])
        self.assertEqual(c1["id"], c2["id"])
        disps = self.svc.list_dispositions(tenant_id="T1")
        self.assertEqual(len(disps), 1)
        self.assertEqual(disps[0]["id"], c1["id"])

    def test_restart_keeps_single_disposition_and_chain(self):
        c1 = self.svc.claim(work_order_id=self.wo["id"], worker_id="W1",
                            screen_token="old-screen-42")
        # simulate a second worker tapping the same stale screen after a
        # power cycle / restart
        self.reopen()
        c2 = self.svc.claim(work_order_id=self.wo["id"], worker_id="W2",
                            screen_token="old-screen-42")
        self.assertTrue(c2["deduplicated"])
        self.assertEqual(c2["id"], c1["id"])
        disps = self.svc.list_dispositions(tenant_id="T1")
        self.assertEqual(len(disps), 1)
        chain = self.svc.get_measurement_chain(work_order_id=self.wo["id"])
        self.assertEqual(chain["disposition_id"], c1["id"])
        self.assertEqual(chain["chain"]["true_length"], 10)
        self.assertEqual(chain["chain"]["requirement"]["min"], 12)

    def test_different_screen_tokens_still_single_valid_disposition(self):
        # two genuinely different stale screens naming the same work order
        c1 = self.svc.claim(work_order_id=self.wo["id"], worker_id="W1",
                            screen_token="screen-a")
        c2 = self.svc.claim(work_order_id=self.wo["id"], worker_id="W2",
                            screen_token="screen-b")
        self.assertTrue(c2["deduplicated"])
        self.assertEqual(c1["id"], c2["id"])
        self.assertEqual(len(self.svc.list_dispositions(tenant_id="T1")), 1)


class WorkshopRemeasureTests(Base):
    def setUp(self):
        super().setUp()
        self.recipe()
        # board needs 12, covered ref -> recall (cannot recheck on site)
        self.board("D", min_len=12, max_len=12, ref_reachable=False)
        self.receipt("D", start="P2", reading=12)  # true 10 -> fails
        self.cal = self.svc.add_calibration(piece_biases={"P2": 2})
        self.svc.dispatch_calibration(calibration_id=self.cal["id"])
        self.wo = self.wo_for(self.cal["id"], "D")

    def test_covered_ref_goes_to_workshop_not_recheck(self):
        self.assertEqual(self.wo["kind"], "recall")
        plan = self.svc.plan(calibration_id=self.cal["id"])
        self.assertEqual(plan["recall_boards"], ["D"])
        self.assertEqual(plan["recheck_boards"], [])

    def test_workshop_remeasure_flips_to_release(self):
        self.assertEqual(self.wo["kind"], "recall")
        # workshop measures the real board as 12 (reads 14 with +2 piece)
        res = self.svc.complete_workshop_remeasure(
            board_id="D",
            payload={"method": "direct", "ruler_id": "R1",
                     "config": {"start_piece": "P2"}, "reading": 14})
        self.assertEqual(res["evaluation"]["true_length"], 12)
        self.assertEqual(res["changed_from"], "recall")
        self.assertEqual(res["work_order"]["kind"], "release")
        self.assertEqual(res["work_order"]["status"], "completed")

    def test_workshop_remeasure_still_fails_keeps_order(self):
        # workshop finds it is really 9 (reads 11 with +2) -> still fails 12
        res = self.svc.complete_workshop_remeasure(
            board_id="D",
            payload={"method": "direct", "ruler_id": "R1",
                     "config": {"start_piece": "P2"}, "reading": 11})
        self.assertEqual(res["evaluation"]["true_length"], 9)
        self.assertEqual(res["work_order"]["kind"], "recall")
        self.assertNotEqual(res["work_order"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
