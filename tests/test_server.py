"""Real-socket integration test for the local HTTP server."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.request

from badges.server import make_server
from badges.model import default_theme
from badges.service import Service


def _request(base, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class ServerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.service = make_server("127.0.0.1", 0, Service())
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_full_flow_over_socket(self):
        status, r = _request(self.base, "POST", "/seasons", {"theme_kind": "default", "season_id": "T1"})
        self.assertEqual(status, 201)
        self.assertEqual(r["season_id"], "T1")

        self.assertEqual(
            _request(self.base, "POST", "/seasons/T1/sign", {"guest": "a", "style": "round_dark"})[0], 201
        )
        self.assertEqual(
            _request(self.base, "POST", "/seasons/T1/sign", {"guest": "b", "style": "round_light"})[0], 201
        )
        status, start = _request(
            self.base,
            "POST",
            "/seasons/T1/sessions/start",
            {"session_id": "E1", "guests": ["a", "b"], "speech_numbers": {"a": 11, "b": 12}},
        )
        self.assertEqual(status, 201, start)

        status, sp = _request(self.base, "GET", "/speeches/11")
        self.assertEqual(status, 200)
        self.assertEqual(sp["guest"], "a")

    def test_unknown_route_404(self):
        status, _ = _request(self.base, "GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
