from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from sofa_service.server import Handler, SERVICE
from sofa_service.scenarios import narrow_order, small_order


class HttpServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        SERVICE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.opener = build_opener(ProxyHandler({}))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def call(self, method: str, path: str, body=None, headers=None):
        data = None
        h = headers or {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            h = {"Content-Type": "application/json", **h}
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_quote_confirm_claim_and_provenance_over_http(self) -> None:
        status, health = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])

        payload = {
            "customerId": "http-customer",
            "floorPlanId": "small-smooth",
            "measurementVersionId": None,
            "order": small_order("whole"),
        }
        status, quote = self.call("POST", "/quotes", payload)
        self.assertEqual(status, 201, quote)
        self.assertTrue(quote["feasible"])

        status, order = self.call("POST", f"/quotes/{quote['quoteId']}/confirm", {"customerId": "http-customer"})
        self.assertEqual(status, 200, order)
        oid = order["orderId"]

        status, claimed = self.call("POST", f"/orders/{oid}/claim", {"masterId": "http-master"})
        self.assertEqual(status, 200, claimed)
        token = claimed["claimToken"]
        self.assertTrue(token)

        status, prov = self.call("GET", f"/orders/{oid}/provenance")
        self.assertEqual(status, 200)
        self.assertIn("catalogHash", prov)

        total = len(quote["stepSheet"]["steps"])
        for i in range(1, total + 1):
            status, result = self.call(
                "POST",
                f"/orders/{oid}/steps/{i}/complete",
                {"claimToken": token},
            )
            self.assertEqual(status, 200, result)

        status, finished = self.call("GET", f"/orders/{oid}")
        self.assertEqual(finished["status"], "completed")

    def test_http_block_and_measurement_history(self) -> None:
        status, versions = self.call("GET", "/survey/doors/entry-narrow/measurements")
        self.assertEqual(status, 200)
        self.assertEqual(len(versions["items"]), 2)
        self.assertEqual(versions["items"][-1]["id"], "m-door-v2")

        payload = {
            "customerId": "http-block",
            "floorPlanId": "narrow-l-turn",
            "measurementVersionId": "m-door-v1",
            "order": narrow_order("piecewise"),
        }
        # Fill required default order fields by using server scenario defaults?
        # The API does not automatically load scenario order, so this minimal
        # payload relies on catalog defaults.
        status, quote = self.call("POST", "/quotes", payload)
        self.assertEqual(status, 201, quote)
        oid = self.call("POST", f"/quotes/{quote['quoteId']}/confirm", {"customerId": "http-block"})[1]["orderId"]
        token = self.call("POST", f"/orders/{oid}/claim", {"masterId": "m"})[1]["claimToken"]

        status, fp = self.call("POST", "/floorplans/narrow-l-turn/blocks", {"cells": [[3, 2]]})
        self.assertEqual(status, 200)
        self.assertIn([3, 2], fp["propertyBlocked"])

        status, validation = self.call("POST", f"/orders/{oid}/revalidate", {})
        self.assertEqual(status, 200)
        self.assertFalse(validation["valid"])

        status, result = self.call(
            "POST", f"/orders/{oid}/steps/1/complete", {"claimToken": token}
        )
        self.assertEqual(status, 200)
        self.assertFalse(result["canIssueNext"])


if __name__ == "__main__":
    unittest.main()
