#!/usr/bin/env python3
"""End-to-end invariant tests using only the Python standard library."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import server


class HandoverApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "handover-test.db")
        self.httpd = server.build_server("127.0.0.1", 0, self.db_path)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def restart_process_like(self) -> None:
        """Stop the HTTP server and start a new one against the same DB file."""
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.httpd = server.build_server("127.0.0.1", 0, self.db_path)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict]:
        data = None
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, headers=hdrs, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, json.loads(raw) if raw else {}

    def rebuild(self, scenario: str) -> None:
        status, body = self.call(
            "POST", "/v1/admin/rebuild", {"scenario": scenario}
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["seeded"])

    def quote(self, hid: str, root: str = "ent_project") -> tuple[int, dict]:
        return self.call(
            "POST",
            "/v1/handovers",
            {
                "id": hid,
                "from_account_id": "jia",
                "to_account_id": "yi",
                "root_entry_id": root,
                "ttl_seconds": 3600,
            },
        )

    def confirm(self, hid: str, account: str = "yi") -> tuple[int, dict]:
        return self.call(
            "POST", f"/v1/handovers/{hid}/confirm", {"account_id": account}
        )

    def test_baseline_exact_cells_upload_and_idempotent_retry(self) -> None:
        self.rebuild("standard")

        status, scan = self.call("GET", "/v1/scan?root_entry_id=ent_project")
        self.assertEqual(status, 200)
        self.assertEqual(scan["unique_file_count"], 2)
        self.assertEqual(scan["total_size_if_all_owned_by_sender"], 5)

        _, jia = self.call("GET", "/v1/accounts/jia")
        _, yi = self.call("GET", "/v1/accounts/yi")
        self.assertEqual((jia["used_cells"], jia["free_cells"]), (5, 0))
        self.assertEqual((yi["used_cells"], yi["free_cells"]), (0, 5))

        status, quote = self.quote("HO-BASE")
        self.assertEqual(status, 201, quote)
        self.assertEqual(quote["totals"]["transfer_cells"], 5)

        status, accepted = self.confirm("HO-BASE")
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["accounts"]["from"]["free_cells"], 5)
        self.assertEqual(accepted["accounts"]["to"]["used_cells"], 5)
        self.assertEqual(accepted["accounts"]["to"]["free_cells"], 0)

        # Ordinary upload entry point: A can fill the returned five cells.
        status, a_upload = self.call(
            "POST",
            "/v1/uploads",
            {"owner_id": "jia", "name": "甲用回五格", "size_cells": 5},
            {"X-Account-Id": "jia"},
        )
        self.assertEqual(status, 201, a_upload)
        self.assertEqual(a_upload["account"]["free_cells"], 0)

        # B cannot add even one cell.
        status, b_upload = self.call(
            "POST",
            "/v1/uploads",
            {"owner_id": "yi", "name": "乙多一格", "size_cells": 1},
            {"X-Account-Id": "yi"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(b_upload["error"]["code"], "quota_exceeded")

        # Retrying the last confirmation must be a replay, not another transfer.
        status, retry = self.confirm("HO-BASE")
        self.assertEqual(status, 200)
        self.assertTrue(retry["idempotent_replay"])
        _, yi_after = self.call("GET", "/v1/accounts/yi")
        _, jia_after = self.call("GET", "/v1/accounts/jia")
        self.assertEqual(yi_after["used_cells"], 5)
        self.assertFalse(yi_after["can_add_one_cell"])
        self.assertEqual(jia_after["used_cells"], 5)

    def test_alias_counts_one_physical_file_and_two_orders_do_not_charge_twice(
        self,
    ) -> None:
        self.rebuild("alias_draft")
        _, scan = self.call("GET", "/v1/scan?root_entry_id=ent_project")
        self.assertEqual(scan["unique_file_count"], 1)
        self.assertEqual(scan["files"][0]["size_cells"], 3)
        self.assertEqual(len(scan["files"][0]["paths"]), 2)

        self.assertEqual(self.quote("HO-ALIAS-1")[0], 201)
        self.assertEqual(self.quote("HO-ALIAS-2")[0], 201)

        status, first = self.confirm("HO-ALIAS-1")
        self.assertEqual(status, 200, first)
        self.assertEqual(first["accounts"]["to"]["free_cells"], 2)

        status, second = self.confirm("HO-ALIAS-2")
        self.assertEqual(status, 200, second)
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(second["settlement"]["transferred_file_ids"], [])
        self.assertEqual(
            second["settlement"]["already_owned_file_ids"], ["file_draft_san"]
        )
        self.assertEqual(second["settlement"]["newly_transferred_cells"], 0)
        self.assertEqual(len(second["ownership_ledger"]), 0)

        status, retry = self.confirm("HO-ALIAS-1")
        self.assertEqual(status, 200)
        self.assertTrue(retry["idempotent_replay"])
        _, yi = self.call("GET", "/v1/accounts/yi")
        self.assertEqual((yi["used_cells"], yi["free_cells"]), (3, 2))

        # A brand-new quote sees the file as already recipient-owned and can
        # also be closed as a zero-cell completion order.
        status, _ = self.quote("HO-ALIAS-3")
        self.assertEqual(status, 201)
        status, third = self.confirm("HO-ALIAS-3")
        self.assertEqual(status, 200, third)
        self.assertEqual(third["totals"]["transfer_cells"], 0)
        self.assertEqual(
            third["totals"]["recipient_owned_files"], 1
        )
        _, yi = self.call("GET", "/v1/accounts/yi")
        self.assertEqual((yi["used_cells"], yi["free_cells"]), (3, 2))

    def test_preupload_blocks_whole_quote_and_new_quote_finishes(self) -> None:
        self.rebuild("blocked_upload")
        _, yi = self.call("GET", "/v1/accounts/yi")
        self.assertEqual((yi["used_cells"], yi["free_cells"]), (1, 4))

        self.assertEqual(self.quote("HO-FAIL")[0], 201)
        status, failed = self.confirm("HO-FAIL")
        self.assertEqual(status, 409)
        self.assertEqual(failed["error"]["code"], "quota_exceeded")
        self.assertEqual(failed["error"]["details"]["shortfall_cells"], 1)

        _, jia = self.call("GET", "/v1/accounts/jia")
        self.assertEqual(jia["used_cells"], 5)

        _, old_quote = self.call("GET", "/v1/handovers/HO-FAIL")
        self.assertEqual(old_quote["status"], "quoted")
        self.assertIn(
            "confirm_blocked_quota", [e["event_type"] for e in old_quote["events"]]
        )

        status, deleted = self.call("DELETE", "/v1/files/file_yi_blocker")
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted["account"]["free_cells"], 5)

        self.assertEqual(self.quote("HO-NEW")[0], 201)
        status, accepted = self.confirm("HO-NEW")
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted["accounts"]["to"]["used_cells"], 5)
        self.assertEqual(accepted["accounts"]["from"]["free_cells"], 5)

    def test_borrowed_files_listed_and_permissions_are_separate(self) -> None:
        self.rebuild("borrowed")
        _, quote = self.call(
            "POST",
            "/v1/handovers",
            {
                "id": "HO-BORROW",
                "from_account_id": "jia",
                "to_account_id": "yi",
                "root_entry_id": "ent_project",
            },
        )
        self.assertEqual(quote["totals"]["transfer_cells"], 5)
        self.assertEqual(quote["totals"]["borrowed_files"], 1)
        self.assertEqual(quote["totals"]["blocked_files"], 1)
        self.assertEqual(
            quote["borrowed_items"][0]["actual_owner_id"], "bing"
        )

        status, accepted = self.confirm("HO-BORROW")
        self.assertEqual(status, 200, accepted)
        self.assertEqual(
            accepted["settlement"]["borrowed_file_ids"], ["file_bing_reference"]
        )
        status, borrowed_read = self.call(
            "GET", "/v1/files/file_bing_reference/content?as=yi"
        )
        self.assertEqual(status, 200, borrowed_read)
        self.assertFalse(borrowed_read["permissions"]["can_write"])
        status, borrowed_edit = self.call(
            "POST",
            "/v1/files/file_bing_reference/versions",
            {"content": "cannot edit borrowed"},
            {"X-Account-Id": "yi"},
        )
        self.assertEqual(status, 403, borrowed_edit)

        status, _ = self.call(
            "POST",
            "/v1/grants",
            {"account_id": "bing", "file_id": "file_draft_san", "can_read": True},
        )
        self.assertEqual(status, 201)
        status, content = self.call(
            "GET", "/v1/files/file_draft_san/content?as=bing"
        )
        self.assertEqual(status, 200)
        self.assertFalse(content["permissions"]["can_write"])

        status, denied = self.call(
            "POST",
            "/v1/files/file_draft_san/versions",
            {"content": "not allowed"},
            {"X-Account-Id": "bing"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"]["code"], "write_permission_required")

        self.call(
            "POST",
            "/v1/grants",
            {
                "account_id": "bing",
                "file_id": "file_draft_san",
                "can_read": True,
                "can_write": True,
            },
        )
        status, edited = self.call(
            "POST",
            "/v1/files/file_draft_san/versions",
            {"content": "丙受托改稿"},
            {"X-Account-Id": "bing"},
        )
        self.assertEqual(status, 200, edited)
        self.assertEqual(edited["file"]["current_version"], 2)

        status, forbidden = self.call(
            "POST",
            "/v1/handovers",
            {
                "id": "HO-DELEGATE",
                "from_account_id": "jia",
                "to_account_id": "yi",
                "root_entry_id": "ent_project",
            },
            {"X-Account-Id": "bing"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(forbidden["error"]["code"], "not_owner")

    def test_owner_edit_makes_stale_quote_and_new_quote_completes(self) -> None:
        self.rebuild("standard")
        self.assertEqual(self.quote("HO-VERSION-1")[0], 201)

        status, edited = self.call(
            "POST",
            "/v1/files/file_draft_er/versions",
            {"content": "甲继续编辑"},
            {"X-Account-Id": "jia"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(edited["file"]["current_version"], 2)

        status, failed = self.confirm("HO-VERSION-1")
        self.assertEqual(status, 409)
        self.assertEqual(failed["error"]["code"], "quote_stale")

        _, old = self.call("GET", "/v1/handovers/HO-VERSION-1")
        self.assertEqual(old["status"], "stale")
        _, jia = self.call("GET", "/v1/accounts/jia")
        self.assertEqual(jia["used_cells"], 5)

        self.assertEqual(self.quote("HO-VERSION-2")[0], 201)
        status, accepted = self.confirm("HO-VERSION-2")
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted["status"], "accepted")

    def test_expired_quote_requires_new_quote(self) -> None:
        self.rebuild("standard")
        status, expired_quote = self.call(
            "POST",
            "/v1/handovers",
            {
                "id": "HO-EXPIRED",
                "from_account_id": "jia",
                "to_account_id": "yi",
                "root_entry_id": "ent_project",
                "expires_at": "2000-01-01T00:00:00Z",
            },
        )
        self.assertEqual(status, 201, expired_quote)
        status, failed = self.confirm("HO-EXPIRED")
        self.assertEqual(status, 410)
        self.assertEqual(failed["error"]["code"], "quote_expired")
        _, detail = self.call("GET", "/v1/handovers/HO-EXPIRED")
        self.assertEqual(detail["status"], "expired")

        self.assertEqual(self.quote("HO-FRESH")[0], 201)
        status, accepted = self.confirm("HO-FRESH")
        self.assertEqual(status, 200, accepted)

    def test_actual_owners_survive_server_restart_by_handover_number(self) -> None:
        self.rebuild("standard")
        self.assertEqual(self.quote("HO-PERSIST")[0], 201)
        self.assertEqual(self.confirm("HO-PERSIST")[0], 200)

        self.restart_process_like()

        status, owners = self.call("GET", "/v1/handovers/HO-PERSIST/ownership")
        self.assertEqual(status, 200, owners)
        owner_by_file = {x["file_id"]: x["actual_owner_id"] for x in owners["owners"]}
        self.assertEqual(
            owner_by_file,
            {"file_draft_san": "yi", "file_draft_er": "yi"},
        )

        status, detail = self.call("GET", "/v1/handovers/HO-PERSIST")
        self.assertEqual(status, 200)
        self.assertEqual(detail["status"], "accepted")

        status, retry = self.confirm("HO-PERSIST")
        self.assertEqual(status, 200)
        self.assertTrue(retry["idempotent_replay"])

    def test_equal_size_independent_files_still_count_twice(self) -> None:
        status, _ = self.call(
            "POST",
            "/v1/accounts",
            {"id": "same_size", "name": "同尺寸两稿", "quota_cells": 4},
        )
        self.assertEqual(status, 201)
        for fid in ("same_a", "same_b"):
            status, uploaded = self.call(
                "POST",
                "/v1/uploads",
                {
                    "id": fid,
                    "owner_id": "same_size",
                    "name": fid,
                    "size_cells": 2,
                },
            )
            self.assertEqual(status, 201, uploaded)
        _, account = self.call("GET", "/v1/accounts/same_size")
        self.assertEqual(account["used_cells"], 4)
        self.assertEqual(account["free_cells"], 0)

        status, third = self.call(
            "POST",
            "/v1/uploads",
            {"id": "same_c", "owner_id": "same_size", "name": "third", "size_cells": 1},
        )
        self.assertEqual(status, 409, third)

    def test_concurrent_confirms_and_uploads_cannot_double_charge(self) -> None:
        self.rebuild("standard")
        self.assertEqual(self.quote("HO-CONCURRENT")[0], 201)

        confirm_results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def do_confirm() -> None:
            result = self.confirm("HO-CONCURRENT")
            with lock:
                confirm_results.append(result)

        threads = [threading.Thread(target=do_confirm) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(status == 200 for status, _ in confirm_results))
        replays = [body.get("idempotent_replay") for _, body in confirm_results]
        self.assertEqual(replays.count(False), 1)
        self.assertEqual(replays.count(True), 5)
        _, detail = self.call("GET", "/v1/handovers/HO-CONCURRENT")
        self.assertEqual(len(detail["ownership_ledger"]), 2)
        _, yi = self.call("GET", "/v1/accounts/yi")
        self.assertEqual(yi["used_cells"], 5)

        # A fresh five-cell account receiving ten simultaneous one-cell uploads
        # must end with exactly five used cells.
        status, _ = self.call(
            "POST", "/v1/accounts", {"id": "race", "name": "抢空间账号", "quota_cells": 5}
        )
        self.assertEqual(status, 201)

        upload_results: list[int] = []

        def do_upload(i: int) -> None:
            status, _ = self.call(
                "POST",
                "/v1/uploads",
                {
                    "owner_id": "race",
                    "name": f"并发稿 {i}",
                    "size_cells": 1,
                    "id": f"race_file_{i}",
                },
            )
            with lock:
                upload_results.append(status)

        threads = [threading.Thread(target=do_upload, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(upload_results).count(201), 5)
        self.assertEqual(sorted(upload_results).count(409), 5)
        _, race_account = self.call("GET", "/v1/accounts/race")
        self.assertEqual(race_account["used_cells"], 5)
        self.assertEqual(race_account["free_cells"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
