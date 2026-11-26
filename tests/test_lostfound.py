from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lostfound import services
from lostfound.db import connect
from lostfound.seed import seed_dolphins
from lostfound.server import create_server


class MatchingScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        seed_dolphins(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_complete_pairing_forces_both_including_elimination(self) -> None:
        result = services.reconcile_round(self.conn, "dolphin-complete")
        self.assertEqual(result["mode"], "closed_complete")
        self.assertTrue(result["manifest_consistent"])
        by_no = {c["claim_no"]: c for c in result["claims"]}

        self.assertTrue(by_no["C-A"]["signable"])
        self.assertEqual(by_no["C-A"]["forced_item_no"], "I-D1")
        self.assertEqual(by_no["C-A"]["possible_item_nos"], ["I-D1"])

        self.assertTrue(by_no["C-B"]["signable"])
        self.assertEqual(by_no["C-B"]["forced_item_no"], "I-D2")
        self.assertEqual(by_no["C-B"]["possible_item_nos"], ["I-D2"])

    def test_sequential_confirmation_still_forces_second_in_closed_round(self) -> None:
        first = services.create_voucher(self.conn, {"claim_no": "C-A"})
        self.assertEqual(first["item_no"], "I-D1")

        result = services.reconcile_round(self.conn, "dolphin-complete")
        b = next(c for c in result["claims"] if c["claim_no"] == "C-B")
        self.assertTrue(b["signable"])
        self.assertEqual(b["forced_item_no"], "I-D2")

        second = services.create_voucher(self.conn, {"claim_no": "C-B"})
        self.assertEqual(second["item_no"], "I-D2")

    def test_open_unfound_registration_forbids_elimination_for_b(self) -> None:
        services.create_voucher(self.conn, {"claim_no": "C-UA"})
        result = services.reconcile_round(self.conn, "dolphin-unfound")
        b = next(c for c in result["claims"] if c["claim_no"] == "C-UB")

        self.assertFalse(b["signable"])
        self.assertIsNone(b["forced_item_no"])
        # Evidence set intentionally still includes the assigned first pot.
        self.assertEqual(b["candidate_item_nos"], ["I-U1", "I-U2"])
        self.assertEqual(b["reason_code"], "ambiguous_evidence")

        with self.assertRaises(services.ApiError) as ctx:
            services.create_voucher(self.conn, {"claim_no": "C-UB", "item_no": "I-U2"})
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.code, "not_signable")

    def test_both_stars_replay_removes_original_uniqueness(self) -> None:
        result = services.reconcile_round(self.conn, "dolphin-both-star")
        for claim in result["claims"]:
            self.assertFalse(claim["signable"], claim)
            self.assertIsNone(claim["forced_item_no"])
            self.assertEqual(len(claim["possible_item_nos"]), 2)

    def test_one_real_sealed_inspection_unlocks_unique_pickup(self) -> None:
        before = services.reconcile_round(self.conn, "sealed-check")
        self.assertFalse(before["summary"].get("signable"))

        plan = services.plan_inspections(self.conn, "sealed-check")
        self.assertEqual(plan["recommended"]["item_no"], "I-K1")

        inspection = services.create_inspection(self.conn, {"item_no": "I-K1"})
        self.assertEqual(inspection["results"], {"bottom_mark": "月"})
        k = next(c for c in inspection["reconcile"]["claims"] if c["claim_no"] == "C-K")
        self.assertTrue(k["signable"])
        self.assertEqual(k["forced_item_no"], "I-K2")

        voucher = services.create_voucher(self.conn, {"claim_no": "C-K"})
        self.assertEqual(voucher["item_no"], "I-K2")

        with self.assertRaises(services.ApiError) as ctx:
            services.create_inspection(self.conn, {"item_no": "I-K1"})
        self.assertEqual(ctx.exception.code, "inspection_budget_exhausted")

    def test_one_binding_revisit_answer_unlocks_and_rounds_are_append_only(self) -> None:
        question = services.recommend_question(self.conn, "revisit-answer", "C-R")
        self.assertEqual(question["round_no"], 1)
        self.assertEqual(question["options"], ["星", "月"])

        answer = services.answer_revisit(
            self.conn, {"claim_no": "C-R", "feature_key": "bottom_mark", "answer": "月"}
        )
        self.assertEqual(answer["round_no"], 1)
        self.assertTrue(answer["owner_view"]["eligible_to_sign"])

        voucher = services.create_voucher(self.conn, {"claim_no": "C-R"})
        self.assertEqual(voucher["item_no"], "I-R2")

        with self.assertRaises(services.ApiError) as ctx:
            services.answer_revisit(
                self.conn, {"claim_no": "C-R", "feature_key": "bottom_mark", "answer": "星"}
            )
        self.assertEqual(ctx.exception.code, "revisit_round_limit")

    def test_owner_view_is_privacy_minimal(self) -> None:
        view = services.owner_view(self.conn, "C-A")
        text = json.dumps(view, ensure_ascii=False)
        self.assertIn("message", view)
        self.assertNotIn("candidate_item_nos", view)
        self.assertNotIn("C-B", text)
        self.assertNotIn("bottom_mark", text)

    def test_prepare_shift_budget_is_enforced(self) -> None:
        services.create_shift(self.conn, {"shift_id": "night", "prepare_budget": 1})
        services.prepare_shift(self.conn, "night", ["I-R1"])
        with self.assertRaises(services.ApiError) as ctx:
            services.prepare_shift(self.conn, "night", ["I-R2"])
        self.assertEqual(ctx.exception.code, "prepare_budget_exhausted")

    def test_externally_confirmed_pairs_are_fixed_and_can_be_handed_out(self) -> None:
        result = services.import_confirmed_pairs(self.conn, {
            "round_id": "dolphin-complete",
            "external_ref": "front-desk-procedure-7",
            "pairs": [{"claim_no": "C-A", "item_no": "I-D1"}],
        })
        self.assertEqual(result["imported"][0]["status"], "signed")

        # Re-importing the same confirmed pair is idempotent.
        again = services.import_confirmed_pairs(self.conn, {
            "round_id": "dolphin-complete",
            "pairs": [{"claim_no": "C-A", "item_no": "I-D1"}],
        })
        self.assertEqual(again["imported"][0]["voucher_no"], result["imported"][0]["voucher_no"])

        voucher_no = result["imported"][0]["voucher_no"]
        handoff = services.outbound(
            self.conn, {"voucher_no": voucher_no, "window_id": "W1"}
        )
        self.assertEqual(handoff["item_no"], "I-D1")
        check = services.stock_check(self.conn)
        self.assertTrue(check["consistent"], check)


class HttpConcurrencyRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "lostfound.db")
        conn = connect(self.db_path)
        seed_dolphins(conn)
        conn.close()
        self.httpd = None
        self.thread = None
        self.httpd, self.thread = self._start()

    def tearDown(self) -> None:
        if self.httpd is not None:
            self._stop(self.httpd, self.thread)
        self.tmp.cleanup()

    def _start(self):
        httpd = create_server(self.db_path, "127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, thread

    def _stop(self, httpd, thread) -> None:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _request(self, method: str, path: str, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_same_voucher_concurrent_outbound_and_restart_stock(self) -> None:
        status, signed = self._request("POST", "/api/vouchers", {"claim_no": "C-A"})
        self.assertEqual(status, 201, signed)
        voucher_no = signed["voucher_no"]

        barrier = threading.Barrier(2)

        def redeem(window: str):
            barrier.wait(timeout=5)
            return self._request(
                "POST", "/api/outbound", {"voucher_no": voucher_no, "window_id": window}
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(redeem, ["W1", "W2"]))

        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, [200, 409], results)
        winners = [body for status, body in results if status == 200]
        losers = [body for status, body in results if status == 409]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["window_id"], winners[0]["window_id"])
        self.assertIn("already", losers[0]["error"]["code"])

        status, check = self._request("GET", "/api/stock-check")
        self.assertEqual(status, 200)
        self.assertTrue(check["consistent"], check)
        self.assertEqual(check["handoff_count"], 1)
        self.assertEqual(check["item_status_counts"].get("out"), 1)

        status, tracked = self._request("GET", f"/api/track/{voucher_no}")
        self.assertEqual(status, 200)
        self.assertEqual(tracked["view"]["status"], "out")

        # Restart against the same SQLite file; warehouse state must survive.
        old_httpd, old_thread = self.httpd, self.thread
        self._stop(old_httpd, old_thread)
        self.httpd, self.thread = self._start()

        status, tracked_after = self._request("GET", f"/api/track/{voucher_no}")
        self.assertEqual(status, 200)
        self.assertEqual(tracked_after["view"]["status"], "out")
        self.assertIsNotNone(tracked_after["view"]["handoff"])

        status, check_after = self._request("GET", "/api/stock-check")
        self.assertTrue(check_after["consistent"], check_after)
        self.assertEqual(check_after["handoff_count"], 1)

        # A post-restart retry from the other window still cannot double-hand out.
        status, body = self._request(
            "POST", "/api/outbound", {"voucher_no": voucher_no, "window_id": "W2"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "voucher_already_redeemed")


if __name__ == "__main__":
    unittest.main()
