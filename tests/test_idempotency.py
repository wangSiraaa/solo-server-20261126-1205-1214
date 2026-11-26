"""Scenario 8: idempotency across restart and concurrent signing."""

from __future__ import annotations

import threading
import unittest

from harness import WorkshopTestCase, T
from lampshop.db import Database
from lampshop.service import DomainService
from lampshop.errors import Conflict


def _sign_worker(args):
    """Module-level worker so multiprocessing can pickle it."""
    path, product_id, rid = args
    svc = DomainService(Database(path))
    try:
        r = svc.sign_product(product_id, "C", rid, at_ts=T["sign"])
        return r["signature_id"]
    except Exception as exc:  # pragma: no cover
        return f"ERR:{getattr(exc, 'code', exc)}"


class IdempotencyTests(WorkshopTestCase):
    def test_resend_after_restart_returns_same_signer(self):
        """服务重启后重送先前的签名请求不能给一件东西生成两位最终签收人."""
        self.ready_to_sign()
        self.svc.create_product("lamp", "lamp1")
        first = self.svc.sign_product("lamp1", "C", "req-A", at_ts=T["sign"])
        # Simulate a restart: drop the in-memory objects, reopen from disk.
        fresh_db = Database(self.db_path)
        fresh = DomainService(fresh_db)
        replay = fresh.sign_product("lamp1", "C", "req-A", at_ts=T["sign"])
        self.assertEqual(replay["signature_id"], first["signature_id"])
        self.assertTrue(replay["replayed"])
        # Exactly one signature row.
        with fresh_db.read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM signatures WHERE product_id=?", ("lamp1",)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_fresh_request_after_restart_cannot_add_second_signer(self):
        self.ready_to_sign()
        self.svc.create_product("lamp", "lamp1")
        first = self.svc.sign_product("lamp1", "C", "req-A", at_ts=T["sign"])
        fresh = DomainService(Database(self.db_path))
        # A brand-new request id (e.g. retry with new key) must NOT double-sign.
        again = fresh.sign_product("lamp1", "C", "req-B", at_ts=T["sign"])
        self.assertEqual(again["signature_id"], first["signature_id"])
        self.assertTrue(again["duplicate_request"])

    def test_concurrent_signers_yield_one_final_signer(self):
        self.ready_to_sign()
        self.svc.create_shift("B", "signer", T["shift_start"], T["shift_end"])
        self.svc.create_product("lamp", "lamp1")
        results, errors = [], []

        def attempt(signer, rid):
            try:
                results.append(self.svc.sign_product("lamp1", signer, rid, at_ts=T["sign"]))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=attempt, args=("C", "r-C")),
                   threading.Thread(target=attempt, args=("B", "r-B"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors, errors)
        sig_ids = {r["signature_id"] for r in results}
        self.assertEqual(len(sig_ids), 1)
        with self.db.read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM signatures WHERE product_id=?", ("lamp1",)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_request_id_reuse_across_products_is_rejected(self):
        self.ready_to_sign()
        self.svc.create_product("lamp", "lamp1")
        self.svc.create_product("lamp", "lamp2")
        self.svc.sign_product("lamp1", "C", "shared", at_ts=T["sign"])
        with self.assertRaises(Conflict) as ctx:
            self.svc.sign_product("lamp2", "C", "shared", at_ts=T["sign"])
        self.assertEqual(ctx.exception.code, "REQUEST_REUSED")

    def test_concurrent_processes_yield_one_final_signer(self):
        """Separate OS processes racing on the same file still yield one signer.

        This exercises the UNIQUE(product_id) guarantee independently of the
        in-process lock.
        """
        import multiprocessing as mp

        self.ready_to_sign()
        self.svc.create_product("lamp", "lamp1")
        path = self.db_path
        ctx = mp.get_context("fork")
        args = [(path, "lamp1", f"proc-{i}") for i in range(4)]
        with ctx.Pool(4) as pool:
            sig_ids = pool.map(_sign_worker, args)
        ok = {s for s in sig_ids if not s.startswith("ERR")}
        self.assertEqual(len(ok), 1, sig_ids)
        with self.db.read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM signatures WHERE product_id=?",
                             ("lamp1",)).fetchone()[0]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
