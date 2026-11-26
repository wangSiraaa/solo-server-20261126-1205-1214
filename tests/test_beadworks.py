import json
import os
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from beadworks.server import serve
from beadworks.service import Beadworks, ConflictError, RemovalNotFeasible


class DomainScenarioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "beadworks.json")
        self.svc = Beadworks(self.db_path)
        self.svc.create_person(person_id="owner", name="物主")
        self.svc.create_person(person_id="other", name="另一持有人")
        self.friday = "2026-11-27"

    def tearDown(self):
        self.tmp.cleanup()

    def _green_chain(self, chain_id="c", piece_id="g", ring_width=2, clasp_kind="shop", holder=None):
        self.svc.receive_piece(piece_id=piece_id, name="青珠", owner_id="owner", hole_width=3)
        self.svc.add_clasp(
            clasp_id=f"{chain_id}-clasp",
            size=3,
            kind=clasp_kind,
            required_holder_id=holder,
            fitted=True,
        )
        self.svc.create_chain(
            chain_id=chain_id,
            segments=[
                {
                    "left": {"type": "closed"},
                    "right": {"type": "port", "claspId": f"{chain_id}-clasp"},
                    "track": [
                        {"type": "bead", "pieceId": piece_id},
                        {"type": "ring", "ringId": f"{chain_id}-ring", "width": ring_width},
                    ],
                }
            ],
        )

    def test_green_baseline_is_feasible_and_persists_individual_return(self):
        self._green_chain()
        quote = self.svc.quote_removal("c", "g", self.friday)
        self.assertTrue(quote["feasible"])
        self.assertEqual("right", quote["chosenDirection"])
        self.assertEqual("c-clasp", quote["chosenRoute"]["exitClaspId"])

        removal = self.svc.remove_and_return_piece("c", "g", "owner", self.friday)
        self.assertFalse(removal["wholeChainReturned"])
        self.assertEqual([], self.svc.current_piece_ids("c"))

        restarted = Beadworks(self.db_path)
        loc = restarted.locate_piece("g")
        self.assertEqual("owner", loc["holderId"])
        self.assertTrue(loc["individualReturn"])

    def test_ring_equal_to_hole_passes_but_wider_ring_blocks(self):
        self.assertEqual("PASS", self.svc.try_fit(3, 3)["code"])
        self._green_chain(chain_id="equal", piece_id="equal-bead", ring_width=3)
        self.assertTrue(self.svc.quote_removal("equal", "equal-bead", self.friday)["feasible"])

        self._green_chain(chain_id="wide", piece_id="wide-bead", ring_width=4)
        quote = self.svc.quote_removal("wide", "wide-bead", self.friday)
        self.assertFalse(quote["feasible"])
        self.assertIn("RING_BLOCKS_HOLE", [b["code"] for b in quote["blockers"]])
        with self.assertRaises(RemovalNotFeasible):
            self.svc.remove_and_return_piece("wide", "wide-bead", "owner", self.friday)

    def test_absent_required_holder_blocks_sealed_clasp(self):
        self._green_chain(
            chain_id="sealed",
            piece_id="sealed-bead",
            clasp_kind="sealed",
            holder="other",
        )
        visit = self.svc.schedule_visit("other", self.friday)
        self.svc.mark_visit_absent(visit["id"])
        quote = self.svc.quote_removal("sealed", "sealed-bead", self.friday)
        self.assertFalse(quote["feasible"])
        self.assertIn("HOLDER_ABSENT", [b["code"] for b in quote["blockers"]])

    def test_arrived_holder_consent_opens_sealed_clasp(self):
        self._green_chain(
            chain_id="sealed-ok",
            piece_id="sealed-ok-bead",
            clasp_kind="sealed",
            holder="other",
        )
        visit = self.svc.schedule_visit("other", self.friday)
        self.svc.check_in_visit(visit["id"])
        consent = self.svc.grant_consent("other", "sealed-ok-clasp", self.friday, visit["id"])
        quote = self.svc.quote_removal("sealed-ok", "sealed-ok-bead", self.friday, consent["id"])
        self.assertTrue(quote["feasible"], quote["blockers"])
        removal = self.svc.remove_and_return_piece(
            "sealed-ok", "sealed-ok-bead", "owner", self.friday, consent_id=consent["id"]
        )
        self.assertEqual(consent["id"], removal["consentId"])

    def test_unfitted_catalog_clasp_is_not_an_exit(self):
        self.svc.receive_piece(piece_id="p", name="珠", owner_id="owner", hole_width=3)
        self.svc.stock_clasp(clasp_id="catalog", size=3)
        with self.assertRaises(ConflictError):
            self.svc.create_chain(
                chain_id="bad",
                segments=[{"right": {"type": "port", "claspId": "catalog"}, "track": [{"type": "bead", "pieceId": "p"}]}],
            )

        self.svc.add_clasp(clasp_id="fitted", size=3, kind="shop", fitted=True)
        self.svc.create_chain(
            chain_id="ok",
            segments=[{"right": {"type": "port", "claspId": "fitted"}, "track": [{"type": "bead", "pieceId": "p"}]}],
        )
        self.svc.stock_clasp(clasp_id="extra", size=99)
        quote = self.svc.quote_removal("ok", "p", self.friday)
        self.assertTrue(quote["feasible"])
        self.assertNotIn("extra", quote["chosenRoute"]["exitClaspId"])

    def test_neighbor_without_temp_permission_blocks_and_permitted_neighbors_are_recorded(self):
        self.svc.receive_piece(piece_id="target", name="目标", owner_id="owner", hole_width=5)
        self.svc.receive_piece(
            piece_id="locked-neighbor", name="不许暂存", owner_id="owner", hole_width=5,
            temp_removal_allowed=False,
        )
        self.svc.add_clasp(clasp_id="c", size=4, kind="shop", fitted=True)
        self.svc.create_chain(
            chain_id="chain",
            segments=[
                {
                    "right": {"type": "port", "claspId": "c"},
                    "track": [
                        {"type": "bead", "pieceId": "target"},
                        {"type": "bead", "pieceId": "locked-neighbor"},
                    ],
                }
            ],
        )
        quote = self.svc.quote_removal("chain", "target", self.friday)
        self.assertFalse(quote["feasible"])
        self.assertIn("TEMP_REMOVAL_NOT_ALLOWED", [b["code"] for b in quote["blockers"]])

        self.svc.db["pieces"]["locked-neighbor"]["tempRemovalAllowed"] = True
        self.svc.commit()
        removal = self.svc.remove_and_return_piece("chain", "target", "owner", self.friday)
        self.assertEqual(["locked-neighbor"], removal["temporaryPieceIdsClearOrder"])
        temp_records = [r for r in self.svc.db["temp_removals"] if r["removalId"] == removal["id"]]
        self.assertEqual(1, len(temp_records))
        self.assertTrue(temp_records[0]["restored"])
        self.assertEqual("on_chain", self.svc.get_piece("locked-neighbor")["status"])

    def test_whole_chain_return_is_not_individual_piece_return(self):
        self._green_chain()
        self.svc.return_whole_chain("c", "owner", self.friday)
        loc = self.svc.locate_piece("g")
        self.assertEqual("owner", loc["holderId"])
        self.assertFalse(loc["individualReturn"])
        self.assertIsNone(loc["individualRemovalId"])
        out_quote = self.svc.quote_removal("c", "g", self.friday)
        self.assertFalse(out_quote["feasible"])
        self.assertIn("CHAIN_NOT_AT_COUNTER", [b["code"] for b in out_quote["blockers"]])

        # After the chain is physically received again, a real individual removal
        # creates the distinct piece-level record.
        self.svc.receive_chain("c")
        self.svc.remove_and_return_piece("c", "g", "owner", self.friday)
        self.assertTrue(self.svc.locate_piece("g")["individualReturn"])

    def test_segmented_order_requires_shop_controlled_clasps_and_supports_rental_next_holder(self):
        for pid in ("a", "b"):
            self.svc.receive_piece(piece_id=f"piece-{pid}", name=pid, owner_id="owner", hole_width=4)
        self.svc.stock_clasp(clasp_id="shop-c", size=4, kind="shop")
        self.svc.stock_clasp(clasp_id="sealed-c", size=4, kind="sealed", required_holder_id="other")
        with self.assertRaises(Exception):
            self.svc.create_segmented_order(
                groups=[{"ownerId": "owner", "pieceIds": ["piece-a"]}, {"ownerId": "owner", "pieceIds": ["piece-b"]}],
                due_date="2026-11-30",
                connector_clasp_ids=["sealed-c"],
            )

        # The failed sealed choice was not fitted; use the shop clasp successfully.
        order = self.svc.create_segmented_order(
            order_id="ord",
            groups=[{"ownerId": "owner", "pieceIds": ["piece-a"]}, {"ownerId": "owner", "pieceIds": ["piece-b"]}],
            due_date="2026-11-30",
            connector_clasp_ids=["shop-c"],
        )
        self.assertEqual("finished", order["status"])
        self.svc.remove_and_return_piece(order["chainId"], "piece-a", "owner", "2026-12-01")

        renter = self.svc.create_person(name="next renter")
        rental = self.svc.create_rental(
            chain_id=order["chainId"], renter_id=renter["id"], start_date="2026-12-02", due_date="2027-01-02"
        )
        self.svc.hand_over_rental(rental["id"])
        report = self.svc.remaining_chain_report(order["chainId"])
        self.assertEqual(["piece-b"], report["pieceIds"])
        self.assertEqual(renter["id"], report["holderId"])
        self.assertIn("shop-c", report["attachedClaspIds"])


class HttpApiTest(unittest.TestCase):
    def test_health_and_post_get_roundtrip(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmp.name, "http.json")
        httpd = serve(db_path, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urlopen(base + "/health", timeout=5) as resp:
                self.assertEqual(200, resp.status)
                self.assertTrue(json.loads(resp.read())["ok"])

            req = Request(
                base + "/api/people",
                data=json.dumps({"person_id": "p1", "name": "HTTP 物主"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=5) as resp:
                self.assertEqual(200, resp.status)
                self.assertEqual("p1", json.loads(resp.read())["id"])

            with urlopen(base + "/api/people/p1", timeout=5) as resp:
                self.assertEqual("HTTP 物主", json.loads(resp.read())["name"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            tmp.cleanup()

    def test_camel_case_json_is_normalized_to_service_kwargs(self):
        tmp = tempfile.TemporaryDirectory()
        httpd = serve(os.path.join(tmp.name, "camel.json"), host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            def post(path, payload):
                req = Request(
                    base + path,
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(req, timeout=5) as resp:
                    return json.loads(resp.read())

            post("/api/people", {"personId": "p1", "name": "物主"})
            post("/api/pieces", {"pieceId": "b1", "name": "珠", "ownerId": "p1", "holeWidth": 3})
            post("/api/clasps", {"claspId": "c1", "size": 3, "kind": "shop", "fitted": True})
            post(
                "/api/chains",
                {
                    "chainId": "ch1",
                    "segments": [
                        {"right": {"type": "port", "claspId": "c1"}, "track": [{"type": "bead", "pieceId": "b1"}]}
                    ],
                },
            )
            quote = post("/api/quotes/removal", {"chainId": "ch1", "pieceId": "b1", "onDate": "2026-11-27"})
            self.assertTrue(quote["feasible"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            tmp.cleanup()

    def test_domain_errors_return_json_status(self):
        tmp = tempfile.TemporaryDirectory()
        httpd = serve(os.path.join(tmp.name, "http-errors.json"), host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with self.assertRaises(HTTPError) as ctx:
                urlopen(base + "/api/people/missing", timeout=5)
            self.assertEqual(404, ctx.exception.code)
            payload = json.loads(ctx.exception.read())
            self.assertEqual("not_found", payload["error"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
