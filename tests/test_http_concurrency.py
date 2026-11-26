import json
import tempfile
import threading
import unittest
import urllib.request
import os

from journal.http_server import make_server
from journal.service import JournalService, JournalError


def _post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, json.loads(r.read())


class HttpSmoke(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.httpd = make_server(self.db, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.service.db.close()
        os.path.exists(self.db) and os.remove(self.db)

    def test_flow_and_restart(self):
        _post(self.base + "/issues", {"issue_id": "I", "mode": "rolling"})
        _post(self.base + "/issues/I/translator",
              {"translator_id": "t1", "slots": ["周二"]})
        _post(self.base + "/issues/I/piece",
              {"piece_id": "island", "title": "海岛", "points": 6,
               "eligible": ["t1"]})
        _post(self.base + "/issues/I/pledge",
              {"pledge_id": "p1", "supporter": "alice", "points": 6,
               "accepts": ["island"]})
        code, body = _post(self.base + "/issues/I/open-piece",
                           {"request_id": "r1", "piece": "island"})
        self.assertEqual(code, 200)
        rid = body["receipt_id"]
        code, body = _post(self.base + "/issues/I/open-piece",
                           {"request_id": "r1", "piece": "island"})
        self.assertEqual(body["receipt_id"], rid)  # idempotent

        code, q = _get(self.base + "/issues/I")
        self.assertTrue(q["pieces"]["island"]["opened"])
        self.assertEqual(q["pieces"]["island"]["funding"][0]["points"], 6)

    def test_editor_decision_returns_409(self):
        _post(self.base + "/issues", {"issue_id": "D", "mode": "joint"})
        _post(self.base + "/issues/D/translator", {"translator_id": "t1",
              "slots": ["周二", "周三"]})
        _post(self.base + "/issues/D/translator", {"translator_id": "t2",
              "slots": ["周四", "周五"]})
        for pid in ("a", "b", "c"):
            _post(self.base + "/issues/D/piece",
                  {"piece_id": pid, "points": 5, "eligible": ["t1", "t2"]})
        _post(self.base + "/issues/D/pledge",
              {"pledge_id": "p", "supporter": "carol", "points": 10,
               "accepts": ["a", "b", "c"]})
        code, body = _post(self.base + "/issues/D/open-joint", {"request_id": "r"})
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "needs_editor_decision")
        code, body = _post(self.base + "/issues/D/open-joint",
                           {"request_id": "r", "selection": "a|c"})
        self.assertEqual(code, 200)


class Concurrency(unittest.TestCase):
    """Two threads racing the same request id must deduct once."""

    def test_parallel_same_request_single_deduction(self):
        db = tempfile.mktemp(suffix=".db")
        try:
            s = JournalService(db)
            s.create_issue("I", "i", "rolling")
            s.add_translator("t1", "T1", ["周二"])
            s.add_piece("I", "island", "海岛", 6, 1, ["t1"])
            s.pledge("I", "p1", "alice", 6, accepts=["island"])

            results, errors = [], []

            def worker():
                svc = JournalService(db)
                try:
                    results.append(svc.open_piece("I", "r1", "island")["receipt_id"])
                except Exception as e:  # noqa: BLE001
                    errors.append(repr(e))
                finally:
                    svc.db.close()

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(set(results)), 1, results)  # one receipt id
            q = s.query_issue("I")
            self.assertEqual(len(q["pieces"]["island"]["funding"]), 1)
            self.assertEqual(q["pieces"]["island"]["funding"][0]["points"], 6)
            s.db.close()
        finally:
            os.path.exists(db) and os.remove(db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
