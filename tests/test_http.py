"""HTTP smoke test: drive the server the way screens and the back-office would."""

import json
import os
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from changeover.engine import build_engine
from changeover.server import make_server


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".jsonl")
        self.engine = build_engine(self.path)
        self.httpd = make_server(self.engine, port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.engine.store.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    def call(self, method, path, body=None, expect=200):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req) as resp:
                self.assertEqual(resp.status, expect)
                return json.loads(resp.read())
        except HTTPError as exc:
            self.assertEqual(exc.code, expect)
            return json.loads(exc.read())

    def post(self, path, body=None, expect=200):
        return self.call("POST", path, body if body is not None else {}, expect)

    def get(self, path, expect=200):
        return self.call("GET", path, None, expect)

    def _baseline(self):
        self.post("/zones", {"id": "east", "kind": "entrance", "name": "东门"})
        self.post("/zones", {"id": "hall", "kind": "corridor", "name": "走廊"})
        self.post("/zones", {"id": "room", "kind": "room", "name": "讲室"})
        self.post("/doors", {"id": "d-east", "a": "east", "b": "hall", "open": True})
        self.post("/doors", {"id": "d-side", "a": "hall", "b": "room", "open": True})
        self.post("/screens", {"id": "sc-east", "zone": "east"})
        self.post("/screens", {"id": "sc-hall", "zone": "hall"})
        self.post("/activities", {"id": "poetry", "name": "诗会", "zones": ["room"], "allow_seen": True, "host": "诗社主持人"})
        self.post("/screens/sc-east/copy", {"target": "poetry", "door": "d-east"})
        self.post("/screens/sc-hall/copy", {"target": "poetry", "door": "d-side"})
        for sid in ("sc-east", "sc-hall"):
            self.post("/screens/%s/received" % sid, {"version": 1})
            self.post("/screens/%s/buffered" % sid, {"version": 1})
            self.post("/screens/%s/display" % sid, {"version": 1})

    def test_full_http_flow(self):
        self._baseline()
        self.assertFalse(False)  # just to keep structure
        # confirm sending is not showing: send a new copy without confirm
        self.post("/screens/sc-east/copy", {"target": "poetry", "door": "d-east"})
        self.assertTrue(self.get("/screens/sc-east")["showing"])  # v1 still on glass

        # certify open + walk
        opening = self.post("/openings", {"activity": "poetry", "entrance": "sc-east"})
        self.assertEqual(opening["status"], "active")
        walk = self.post("/walk", {"entrance": "sc-east", "target": "poetry"})
        self.assertTrue(walk["ok"])

        # field receipt says still partitioned -> retraction
        self.post("/doors/d-side/field", {"open": False})
        self.assertEqual(self.get("/openings/%d" % opening["number"])["status"], "retracted")

        # restore door but hall now points back to east -> cannot certify
        self.post("/doors/d-side/field", {"open": True})
        self.post("/screens/sc-hall/copy", {"target": "poetry", "door": "d-east"})
        cur = self.get("/screens/sc-hall")["sent_version"]
        for act in ("received", "buffered", "display"):
            self.post("/screens/sc-hall/%s" % act, {"version": cur})
        res = self.post("/openings", {"activity": "poetry", "entrance": "sc-east"}, expect=409)
        self.assertEqual(res["error"], "not_walkable")

        # offline inspection still reads the real display
        self.post("/screens/sc-hall/online", {"online": False})
        g = self.get("/guidance?entrance=sc-east&target=poetry")
        self.assertFalse(g["walk"]["ok"])
        self.assertEqual(self.get("/screens/sc-hall")["display_target"], "poetry")

    def test_late_stale_display_over_http(self):
        self._baseline()
        self.post("/screens/sc-east/copy", {"target": "poetry", "door": "d-east"})
        cur = self.get("/screens/sc-east")["sent_version"]
        for act in ("received", "buffered", "display"):
            self.post("/screens/sc-east/%s" % act, {"version": cur})
        out = self.post("/screens/sc-east/display", {"version": cur - 1})
        self.assertTrue(any("stale_display" in str(n) for n in out["notes"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
