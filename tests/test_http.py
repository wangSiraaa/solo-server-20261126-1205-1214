"""End-to-end HTTP tests: real server, real restarts, idempotent resend."""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request

from harness import WorkshopTestCase, T
from lampshop.api import make_server
from lampshop.db import Database
from lampshop.service import DomainService


class HttpServerMixin:
    def start_server(self, service):
        httpd = make_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        host, port = httpd.server_address
        self.base = f"http://{host}:{port}"
        return httpd

    def call(self, action, body=None, method="POST"):
        url = f"{self.base}/{action}"
        data = None
        headers = {}
        if method == "POST":
            data = json.dumps(body or {}).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())


class HttpEndToEndTests(WorkshopTestCase, HttpServerMixin):
    def test_full_http_flow_and_idempotent_resend_after_restart(self):
        self.start_server(self.svc)
        # Create a product and sign over HTTP.
        status, body = self.call("product", {"id": "http-lamp", "product_type_id": "lamp"})
        self.assertEqual(status, 200, body)

        self.ready_to_sign()
        status, signed = self.call("sign", {
            "product_id": "http-lamp", "signer_id": "C",
            "request_id": "http-req-1", "at_ts": T["sign"],
        })
        self.assertEqual(status, 200, signed)
        self.assertTrue(signed["signed"])
        sig_id = signed["signature_id"]

        # Query basis by product number over HTTP.
        status, trace = self.call("product_basis/http-lamp", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(trace["signed"])

        # "Restart": tear down, reopen DB, new server, resend same request.
        self.start_server(DomainService(Database(self.db_path)))
        status, replay = self.call("sign", {
            "product_id": "http-lamp", "signer_id": "C",
            "request_id": "http-req-1", "at_ts": T["sign"],
        })
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay["signature_id"], sig_id)
        self.assertTrue(replay["replayed"])

        # Even a fresh request id must not create a second signer.
        status, again = self.call("sign", {
            "product_id": "http-lamp", "signer_id": "B",
            "request_id": "http-req-2", "at_ts": T["sign"],
        })
        self.assertEqual(status, 200, again)
        self.assertEqual(again["signature_id"], sig_id)

        # Confirm exactly one signature persisted.
        with Database(self.db_path).read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM signatures WHERE product_id=?", ("http-lamp",)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_http_error_mapping(self):
        self.start_server(self.svc)
        status, body = self.call("sign", {"product_id": "nope", "signer_id": "C", "request_id": "x"})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "NOT_FOUND")
        # Missing field -> 400 validation.
        status, body = self.call("sign", {"product_id": "nope"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "VALIDATION")


if __name__ == "__main__":
    unittest.main()
