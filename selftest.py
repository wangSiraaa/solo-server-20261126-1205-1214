#!/usr/bin/env python3
"""不变量自检：python3 selftest.py"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from lunshi.scenario import CONVENTION, seed_world
from lunshi.server import serve
from lunshi.service import ApiError, LunshiService


def new_service() -> LunshiService:
    fd, path = tempfile.mkstemp(prefix="lunshi-test-", suffix=".sqlite3")
    os.close(fd)
    os.remove(path)
    svc = LunshiService(path)
    seed_world(svc)
    return svc


def a_done(svc: LunshiService) -> None:
    svc.call_next(CONVENTION, day_no=1)
    a1 = svc.submit_application("A-BOOK", "V-WEN", "A", 1, "book", convention_id=CONVENTION)
    a2 = svc.submit_application("A-UMB", "V-GU", "A", 1, "umbrella", convention_id=CONVENTION)
    p1 = svc.issue_permit(a1["id"], "A1")
    p2 = svc.issue_permit(a2["id"], "A2")
    svc.report_open("R-A-BOOK", p1["permit_no"])
    svc.report_open("R-A-UMB", p2["permit_no"])


def b_pair_accepted(svc: LunshiService) -> None:
    svc.call_next(CONVENTION, day_no=2)
    svc.create_adjacent_pair("B-PAIR", "B", 2, [
        {"vendor_id": "V-WEN", "category": "book", "application_id": "B-BOOK"},
        {"vendor_id": "V-GU", "category": "umbrella", "application_id": "B-UMB"},
    ], convention_id=CONVENTION)


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()

    def test_happy_three_rounds_duplicate_and_restart(self):
        a_done(self.svc)
        self.assertEqual(self.svc.ledger(CONVENTION)["effective_rounds"],
                         [{"round_no": 1, "township_id": "A",
                           "usage_id": self.svc.ledger(CONVENTION)["effective_rounds"][0]["usage_id"]}])

        b_pair_accepted(self.svc)
        issued = self.svc.issue_group_permits("B-PAIR")
        rb1 = self.svc.report_open("R-B-BOOK", issued["permits"][0]["permit_no"])
        rb2 = self.svc.report_open("R-B-UMB", issued["permits"][1]["permit_no"])
        self.assertEqual(rb2["settlement"]["status"], "succeeded")
        self.assertEqual(rb2["settlement"]["usage"]["round_no"], 2)
        self.assertTrue(rb1.get("duplicate") is not True)
        dup = self.svc.report_open("R-B-BOOK", issued["permits"][0]["permit_no"])
        self.assertTrue(dup["duplicate"])

        self.svc.call_next(CONVENTION, from_day=3)
        c1 = self.svc.submit_application("C-BOOK", "V-LAN", "C", 3, "book", convention_id=CONVENTION)
        c2 = self.svc.submit_application("C-UMB", "V-JIANG", "C", 3, "umbrella", convention_id=CONVENTION)
        pc1 = self.svc.issue_permit(c1["id"], "C1")
        pc2 = self.svc.issue_permit(c2["id"], "C2")
        self.svc.report_open("R-C-BOOK", pc1["permit_no"])
        rc = self.svc.report_open("R-C-UMB", pc2["permit_no"])
        self.assertEqual(rc["settlement"]["usage"]["round_no"], 3)

        rounds = [(x["round_no"], x["township_id"]) for x in self.svc.ledger(CONVENTION)["effective_rounds"]]
        self.assertEqual(rounds, [(1, "A"), (2, "B"), (3, "C")])

        # Reopen the same SQLite file, simulating restart.
        restarted = LunshiService(self.svc.db_path)
        rounds = [(x["round_no"], x["township_id"]) for x in restarted.ledger(CONVENTION)["effective_rounds"]]
        self.assertEqual(rounds, [(1, "A"), (2, "B"), (3, "C")])

    def test_withdrawal_creates_owed_round_and_blocks_b(self):
        a_done(self.svc)
        b_pair_accepted(self.svc)
        issued = self.svc.issue_group_permits("B-PAIR")

        result = self.svc.withdraw_receipt("R-A-UMB", "rain")
        self.assertEqual(result["revalidated_usages"][0]["status"], "reversed")
        self.assertEqual(self.svc.ledger(CONVENTION)["effective_rounds"], [])

        nxt = self.svc.call_next(CONVENTION, from_day=2)
        self.assertEqual((nxt["round_no"], nxt["township_id"], nxt["kind"], nxt["day_no"]),
                         (1, "A", "makeup", 4))

        rb1 = self.svc.report_open("R-B-BOOK", issued["permits"][0]["permit_no"])
        rb2 = self.svc.report_open("R-B-UMB", issued["permits"][1]["permit_no"])
        self.assertEqual(rb2["settlement"]["status"], "blocked")
        self.assertEqual(rb2["settlement"]["code"], "PREDECESSOR_NOT_COMPLETE")
        self.assertNotEqual(rb1["settlement"].get("status"), "succeeded")

        m1 = self.svc.submit_application("M-BOOK", "V-WEN", "A", 4, "book", convention_id=CONVENTION)
        m2 = self.svc.submit_application("M-UMB", "V-GU", "A", 4, "umbrella", convention_id=CONVENTION)
        p1 = self.svc.issue_permit(m1["id"], "A1")
        p2 = self.svc.issue_permit(m2["id"], "A2")
        self.svc.report_open("R-M-BOOK", p1["permit_no"])
        rm = self.svc.report_open("R-M-UMB", p2["permit_no"])
        self.assertEqual(rm["settlement"]["status"], "succeeded")
        self.assertEqual(len(self.svc.ledger(CONVENTION)["effective_rounds"]), 1)

    def test_restore_then_wall_blocks_adjacent_permit(self):
        a_done(self.svc)
        b_pair_accepted(self.svc)
        self.svc.withdraw_receipt("R-A-UMB")
        restored = self.svc.restore_receipt("R-A-UMB")
        self.assertEqual(restored["usage"]["status"], "effective")

        self.svc.set_spots_linked("B1", "B2", False, "wall")
        readiness = self.svc.venue_readiness("B", 2, 1, CONVENTION)
        self.assertTrue(readiness["qualified_to_host"])
        self.assertFalse(readiness["adjacent_required_pair_available"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.issue_group_permits("B-PAIR", ["B1", "B2"])
        self.assertEqual(ctx.exception.code, "ADJACENCY_REQUIRED")
        self.assertEqual(self.svc.get_group("B-PAIR")["permits"], [])

    def test_same_vendor_cannot_open_two_places_same_slot(self):
        a_done(self.svc)  # V-WEN has an open receipt at A day1 slot1.
        self.svc.declare_session("C", 1, 1, "2026-11-30")
        app = self.svc.submit_application("C-DUP-SLOT", "V-WEN", "C", 1, "book", convention_id=CONVENTION)
        permit = self.svc.issue_permit(app["id"], "C1")
        with self.assertRaises(ApiError) as ctx:
            self.svc.report_open("R-C-DUP", permit["permit_no"])
        self.assertEqual(ctx.exception.code, "VENDOR_TIME_CONFLICT")

    def test_rain_partial_creates_owed(self):
        self.svc.call_next(CONVENTION, day_no=1)
        app = self.svc.submit_application("ONLY-BOOK", "V-WEN", "A", 1, "book", convention_id=CONVENTION)
        permit = self.svc.issue_permit(app["id"], "A1")
        receipt = self.svc.report_open("R-ONLY-BOOK", permit["permit_no"])
        self.assertEqual(receipt["settlement"]["status"], "partial")
        failed = self.svc.fail_call(receipt["settlement"]["call_id"], "雨散，修伞未开")
        self.assertEqual(failed["status"], "failed_owed")
        self.assertEqual(self.svc.ledger(CONVENTION)["owed_rounds"][0]["status"], "owed")

    def test_loan_cannot_be_promised_twice(self):
        self.svc.create_loan("L1", "B9", "A", 4, 1)
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_loan("L2", "B9", "C", 4, 1)
        self.assertEqual(ctx.exception.code, "LAND_ALREADY_PROMISED")


class TransferReceiptTests(unittest.TestCase):
    def setUp(self):
        self.svc = new_service()

    def test_transfer_moves_same_receipt_and_does_not_consume_round(self):
        self.svc.create_loan("L1", "B9", "A", 4, 1)
        app = self.svc.submit_application("TRF-UMB", "V-GU", "A", 4, "umbrella", convention_id=CONVENTION)
        permit = self.svc.issue_permit(app["id"], "A2")
        original = self.svc.report_open("R-TRF", permit["permit_no"])
        self.assertEqual(original["spot_township_id"], "A")
        transfer = self.svc.transfer_booking("TRF-UMB", "B9")
        self.assertEqual(transfer["moved_receipt_no"], "R-TRF")
        receipt = self.svc.vendor_dossier("V-GU")["receipts"]
        current = [r for r in receipt if r["receipt_no"] == "R-TRF"][0]
        self.assertEqual(current["spot_id"], "B9")
        self.assertEqual(current["spot_township_id"], "B")
        self.assertEqual(current["host_township_id"], "A")
        self.assertEqual(self.svc.ledger(CONVENTION)["effective_rounds"], [])


class HttpTests(unittest.TestCase):
    def test_http_routes(self):
        svc = new_service()
        httpd = serve(svc.db_path, "127.0.0.1", 0)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            def get(path):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                    return json.loads(r.read().decode())

            def post(path, payload):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, json.loads(r.read().decode())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read().decode())

            self.assertTrue(get("/api/health")["ok"])
            news = get("/api/street?day_no=2")
            self.assertEqual(news["days"][0]["sessions"][0]["township_id"], "B")
            status, conv = post("/api/calls/next", {"convention_id": CONVENTION, "day_no": 1})
            self.assertEqual(status, 200)
            self.assertEqual(conv["township_id"], "A")
            status, err = post("/api/groups/nope/issue", {})
            self.assertEqual(status, 404)
            self.assertEqual(err["error"]["code"], "NOT_FOUND")
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
