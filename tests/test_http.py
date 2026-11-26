"""End-to-end HTTP tests exercising the API layer (routing, decoding, errors)."""

import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from bookclub.api import serve_http
from bookclub.store import Store


class HttpCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(None)
        self.httpd = serve_http(self.store, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method, path, body=None, expect=200):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                payload = json.loads(resp.read().decode())
                self.assertEqual(resp.status, expect)
                return payload
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
            self.assertEqual(exc.code, expect, payload)
            return payload


class HttpFlow(HttpCase):
    def test_full_http_flow(self):
        c = self.call
        c("POST", "/facts", {"id": "key", "description": "钥匙在桥下"}, expect=201)
        c("POST", "/facts", {"id": "letter", "description": "信"}, expect=201)
        c("POST", "/facts", {"id": "tape", "description": "录音"}, expect=201)
        c("POST", "/facts", {"id": "end_fact", "description": "结局"}, expect=201)
        c("POST", "/works", {"id": "w", "title": "桥"}, expect=201)
        turn = c("POST", "/works/w/turns", {
            "label": "v1", "segments": [
                {"id": "sA", "title": "信", "discloses": ["letter"]},
                {"id": "sB", "title": "录音", "discloses": ["tape"]},
                {"id": "sA2", "title": "桥(信)", "discloses": ["key"], "requires": ["letter"]},
                {"id": "sB2", "title": "桥(录音)", "discloses": ["key"], "requires": ["tape"]},
                {"id": "end", "title": "结局", "requires": ["key"], "discloses": ["end_fact"]},
            ]}, expect=201)["turn"]["id"]

        # Non-ASCII reader in a URL path must round-trip.
        c("POST", "/read", {"reader": "阿禾", "work": "w", "turn": turn, "segment": "sA"})
        c("POST", "/read", {"reader": "阿禾", "work": "w", "turn": turn, "segment": "sA2"})
        c("POST", "/read", {"reader": "阿岚", "work": "w", "turn": turn, "segment": "sB"})
        c("POST", "/read", {"reader": "阿岚", "work": "w", "turn": turn, "segment": "sB2"})
        rv = c("GET", "/readers/" + urllib.parse.quote("阿禾", safe=""))
        self.assertEqual(rv["reads"][turn]["key"], ["sA2"])

        sess = c("POST", "/sessions", {"title": "周五", "work": "w", "kind": "served",
                                       "goals": ["end"]}, expect=201)["session"]["id"]
        c("POST", f"/sessions/{sess}/register", {"reader": "阿禾"})
        c("POST", f"/sessions/{sess}/register", {"reader": "阿岚"})
        served = c("POST", f"/sessions/{sess}/serve", {})
        self.assertEqual(served["line"]["route"]["needs_remediation"], [])
        locked = c("POST", f"/sessions/{sess}/lock", {})
        self.assertEqual(locked["status"], "locked")

        # Error mapping: a reader without the required fact gets 409 listing it.
        err2 = c("POST", "/read", {"reader": "路人", "work": "w", "turn": turn,
                                   "segment": "end"}, expect=409)
        self.assertEqual(err2["error"]["missing"], ["key"])
        # A reader who holds the fact enters fine.
        ok = c("POST", "/read", {"reader": "阿禾", "work": "w", "turn": turn,
                                 "segment": "end"})
        self.assertTrue(ok["now_has"]["end_fact"])

        # Unknown route -> 404; wrong method -> 405.
        c("GET", "/nope", expect=404)
        c("GET", "/read", expect=405)

    def test_front_desk_and_slot_conflict(self):
        c = self.call
        c("POST", "/facts", {"id": "key", "description": "key"}, expect=201)
        c("POST", "/facts", {"id": "e", "description": "e"}, expect=201)
        c("POST", "/works", {"id": "w", "title": "t"}, expect=201)
        turn = c("POST", "/works/w/turns", {"label": "v1", "segments": [
            {"id": "s", "title": "s", "discloses": ["key"]},
            {"id": "end", "title": "end", "requires": ["key"], "discloses": ["e"]},
        ]}, expect=201)["turn"]["id"]
        c("POST", "/read", {"reader": "R", "work": "w", "turn": turn, "segment": "s"})
        clerk = c("POST", "/staff", {"name": "程"}, expect=201)["staff"]["id"]
        s1 = c("POST", "/sessions", {"title": "a", "work": "w", "kind": "served",
                                     "goals": ["end"]}, expect=201)["session"]["id"]
        c("POST", f"/sessions/{s1}/register", {"reader": "R"})
        c("POST", f"/sessions/{s1}/serve", {"staff": clerk, "start": 0, "end": 60})
        s2 = c("POST", "/sessions", {"title": "b", "work": "w", "kind": "served",
                                     "goals": ["end"]}, expect=201)["session"]["id"]
        c("POST", f"/sessions/{s2}/register", {"reader": "R"})
        clash = c("POST", f"/sessions/{s2}/serve",
                  {"staff": clerk, "start": 30, "end": 90}, expect=409)
        self.assertEqual(clash["error"]["code"], "staff_double_booked")
        decision = c("POST", f"/sessions/{s2}/front-desk",
                     {"reader": "R", "fact": "key", "staff": clerk,
                      "start": 30, "end": 90})["decision"]
        self.assertEqual(decision["action"], "rebook")
        self.assertEqual(decision["fact_state"]["status"], "has")


if __name__ == "__main__":
    unittest.main()
