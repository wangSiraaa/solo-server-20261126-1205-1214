"""HTTP integration tests: drive the service through the real JSON API.

Includes a process-level restart test: the server is started on a fresh
SQLite file, work is claimed from a stale screen, the server is closed and a
brand-new Service is built from the same DB, then the same screen token tries
to claim again -- the tenant must still see exactly one valid disposition and
its measurement chain.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from app.engine import Service
from app.http_api import make_server
from app.store import Store


class Client:
    def __init__(self, base):
        self.base = base

    def _do(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def post(self, path, body=None):
        return self._do("POST", path, body)

    def get(self, path):
        return self._do("GET", path)


class ServerHarness:
    def __init__(self, db):
        self.db = db
        self.svc = Service(Store(db))
        self.httpd = make_server(self.svc, port=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        self.client = Client(f"http://{host}:{port}")

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "http.db")
        self.h = ServerHarness(self.db)
        self.c = self.h.client

    def tearDown(self):
        try:
            self.h.stop()
        except Exception:
            pass

    def seed_small(self):
        c = self.c
        self.assertEqual(c.post("/pieces", {"piece_id": "P2", "kind": "start",
                                            "nominal_bias": 0, "true_bias": 2})[0], 200)
        self.assertEqual(c.post("/pieces", {"piece_id": "P0", "kind": "start",
                                            "nominal_bias": 0, "true_bias": 0})[0], 200)
        c.post("/rulers", {"ruler_id": "R1"})
        c.post("/boards", {"board_id": "A", "tenant_id": "T1",
                           "min_len": 10, "max_len": 10, "ref_point_id": "Aref"})
        c.post("/boards", {"board_id": "B", "tenant_id": "T1",
                           "min_len": 10, "max_len": 10, "ref_point_id": "Bref"})
        c.post("/usage-receipts", {"board_id": "A", "ruler_id": "R1",
                                   "method": "direct",
                                   "config": {"start_piece": "P2"}, "reading": 12})
        c.post("/usage-receipts", {"board_id": "B", "ruler_id": "R1",
                                   "method": "subtract",
                                   "config": {"start_piece": "P2"},
                                   "reading_a": 12, "reading_b": 22})

    def test_small_example_over_http(self):
        self.seed_small()
        ev = self.c.get("/boards/A/evaluation")[1]
        self.assertEqual(ev["true_length"], 10)
        status, cal = self.c.post("/calibrations", {"piece_biases": {"P2": 2}})
        self.assertEqual(status, 200)
        cid = cal["id"]
        self.c.post(f"/calibrations/{cid}/dispatch", {})
        plan = self.c.get(f"/calibrations/{cid}/plan")[1]
        self.assertEqual(plan["released_boards"], ["A", "B"])
        self.assertEqual(plan["retest"]["must_retest"], [])

    def test_edit_receipt_over_http_breaks_direct_board(self):
        self.seed_small()
        cal = self.c.post("/calibrations", {"piece_biases": {"P2": 2}})[1]
        cid = cal["id"]
        self.c.post(f"/calibrations/{cid}/dispatch", {})
        # rewrite A's usage receipt to the no-bias replacement piece, same 12
        self.c.post("/boards/A/usage-receipt",
                    {"ruler_id": "R1", "method": "direct",
                     "config": {"start_piece": "P0"}, "reading": 12})
        self.c.post(f"/calibrations/{cid}/dispatch", {})
        plan = self.c.get(f"/calibrations/{cid}/plan")[1]
        self.assertIn("A", plan["recheck_boards"])
        self.assertIn("B", plan["released_boards"])

    def test_claim_idempotent_and_restart_survives(self):
        self.seed_small()
        cal = self.c.post("/calibrations", {"piece_biases": {"P2": 2}})[1]
        cid = cal["id"]
        # make A fail by tightening its requirement to at least 11
        self.c.post("/boards/A/requirement", {"min_len": 11})
        self.c.post(f"/calibrations/{cid}/dispatch", {})
        wos = self.c.get(f"/work-orders?calibration_id={cid}")[1]["work_orders"]
        wo_a = [w for w in wos if w["board_id"] == "A"][0]
        self.assertEqual(wo_a["kind"], "recheck")

        s1, d1 = self.c.post(f"/work-orders/{wo_a['id']}/claim",
                             {"worker_id": "W1", "screen_token": "old-screen"})
        s2, d2 = self.c.post(f"/work-orders/{wo_a['id']}/claim",
                             {"worker_id": "W2", "screen_token": "old-screen"})
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 200)
        self.assertFalse(d1["deduplicated"])
        self.assertTrue(d2["deduplicated"])
        self.assertEqual(d1["id"], d2["id"])

        # ---- process restart: brand new Service + server on same DB ----
        self.h.stop()
        self.h = ServerHarness(self.db)
        self.c = self.h.client

        s3, d3 = self.c.post(f"/work-orders/{wo_a['id']}/claim",
                             {"worker_id": "W2", "screen_token": "old-screen"})
        self.assertEqual(s3, 200)
        self.assertEqual(d3["id"], d1["id"])
        disps = self.c.get("/dispositions?tenant_id=T1")[1]["dispositions"]
        self.assertEqual(len(disps), 1)
        self.assertEqual(disps[0]["id"], d1["id"])
        chain = self.c.get(
            f"/work-orders/{wo_a['id']}/measurement-chain")[1]
        self.assertEqual(chain["disposition_id"], d1["id"])
        self.assertEqual(chain["chain"]["true_length"], 10)

    def test_health(self):
        self.assertEqual(self.c.get("/health")[1], {"ok": True})


if __name__ == "__main__":
    unittest.main()
