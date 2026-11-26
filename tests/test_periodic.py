"""Scenario 7: periodic certification regime (the alternative operating model)."""

from __future__ import annotations

import unittest

from harness import WorkshopTestCase, T
from lampshop.service import REGIME_PERIODIC, REGIME_ENDORSEMENT
from lampshop.errors import Conflict


class PeriodicRegimeTests(WorkshopTestCase):
    def setUp(self):
        super().setUp()
        self.svc.set_regime(REGIME_PERIODIC)

    def _open_first_period(self):
        p = self.svc.create_period("2026-01-11", "2026-01-31", review_policy="honor_through_period",
                                   period_id="P1")
        # 丙 does not yet have production qual; issuance does the eligibility check.
        result = self.svc.open_period("P1", ["C"], ["shade"],
                                      issued_at="2026-01-11T00:00:00Z")
        return result

    def test_period_issues_certificate_and_signs(self):
        self.make_bc()  # 乙 qualified, teaches 丙; no production qual for 丙 yet
        res = self._open_first_period()
        self.assertEqual(len(res["issued"]), 1)
        cert_id = res["issued"][0]["certificate_id"]
        self.svc.create_shift("C", "signer", T["shift_start"], T["shift_end"])
        self.svc.create_product("lamp", "lamp1")
        signed = self.svc.sign_product("lamp1", "C", "req", at_ts=T["sign"])
        self.assertTrue(signed["signed"])
        self.assertEqual(signed["basis"]["regime"], "periodic")
        self.assertEqual(signed["basis"]["crafts"]["shade"]["certificate_id"], cert_id)

    def test_signature_outside_certificate_validity_fails(self):
        self.make_bc()
        self._open_first_period()
        self.svc.create_shift("C", "signer", "2026-02-05T08:00:00Z", "2026-02-05T18:00:00Z")
        self.svc.create_product("lamp", "lamp1")
        with self.assertRaises(Conflict) as ctx:
            self.svc.sign_product("lamp1", "C", "req", at_ts="2026-02-05T10:00:00Z")
        self.assertEqual(ctx.exception.code, "NO_VALID_CERTIFICATE")

    def test_open_period_rejects_insufficient_experience(self):
        # 丙 has no usable experience at all in this variant.
        p = self.svc.create_period("2026-01-11", "2026-01-31", period_id="P0")
        res = self.svc.open_period("P0", ["C"], ["shade"], issued_at="2026-01-11T00:00:00Z")
        self.assertEqual(res["issued"], [])
        self.assertTrue(any(s["reason"] == "insufficient_valid_experience" for s in res["skipped"]))

    def test_open_period_rejects_broken_lineage(self):
        """重验沿途师承 -- if 乙's mentoring wasn't valid at the time, no cert is issued."""
        self.grant("B", cap="production", vf="2026-01-05T14:00:00Z")
        self.grant("B", cap="mentoring", vf="2026-01-20T00:00:00Z", enforce=False)
        self.learn("shade", "B", "C", T["bc_start"], T["bc_end"], station="bench1")
        p = self.svc.create_period("2026-01-21", "2026-01-31", period_id="Pbad")
        res = self.svc.open_period("Pbad", ["C"], ["shade"], issued_at="2026-01-21T00:00:00Z")
        self.assertEqual(res["issued"], [])

    def test_honor_policy_keeps_cert_but_next_period_reverifies(self):
        """旧证据有误：有效期内发出的证照按复核约定处理；下一期要重验沿途师承."""
        self.make_bc()
        self._open_first_period()
        # Signature inside P1 while cert valid.
        self.svc.create_shift("C", "signer", T["shift_start"], T["shift_end"])
        self.svc.create_product("lamp", "lamp1")
        self.svc.sign_product("lamp1", "C", "req1", at_ts=T["sign"])
        # Old evidence turns out wrong -> retract it.
        ev_bc = [e for e in self.svc.list_evidence(person_id="C", craft_id="shade")][0]
        self.svc.retract_evidence(ev_bc["id"], "wrong", at_ts="2026-01-15T00:00:00Z")

        # Review under honor_through_period: cert stays active.
        review = self.svc.review_period("P1", at_ts="2026-01-16T00:00:00Z")
        honored = [a for a in review["affected"] if a["decision"] == "honored_through_period"]
        self.assertTrue(honored)
        certs = self.svc.list_certificates(person_id="C", craft_id="shade")
        self.assertTrue(any(c["status"] == "active" for c in certs))
        # The already-signed piece is untouched.
        self.assertTrue(self.svc.get_product("lamp1")["signature"])
        # KEY CONTRAST: under honor_through_period, 丙 may STILL sign a NEW piece
        # inside the period on the honored certificate (unlike endorsement, which
        # would refuse at NO_VALID_BASIS).
        self.svc.create_shift("C", "signer", "2026-01-17T08:00:00Z", "2026-01-17T18:00:00Z")
        self.svc.create_product("lamp", "lamp-honored")
        honored_sign = self.svc.sign_product("lamp-honored", "C", "req-honored",
                                             at_ts="2026-01-17T10:00:00Z")
        self.assertTrue(honored_sign["signed"])
        self.assertEqual(honored_sign["basis"]["regime"], "periodic")

        # Next period must re-verify: the retracted basis prevents re-issuance.
        p2 = self.svc.create_period("2026-02-01", "2026-02-28", review_policy="honor_through_period",
                                    period_id="P2")
        res2 = self.svc.open_period("P2", ["C"], ["shade"], issued_at="2026-02-01T00:00:00Z")
        self.assertEqual(res2["issued"], [])
        self.assertTrue(any(s["reason"] == "insufficient_valid_experience" for s in res2["skipped"]))

    def test_root_mentor_is_certified_by_root_authority(self):
        """The founder has no upstream apprenticeship; root standing certifies them."""
        p = self.svc.create_period("2026-01-01", "2026-12-31", period_id="Proot")
        res = self.svc.open_period("Proot", ["A"], ["shade"], issued_at="2026-01-02T00:00:00Z")
        self.assertEqual(len(res["issued"]), 1)
        self.svc.create_shift("A", "signer", "2026-01-03T08:00:00Z", "2026-01-03T18:00:00Z")
        self.svc.create_product("lamp", "lamp-root")
        signed = self.svc.sign_product("lamp-root", "A", "req-root", at_ts="2026-01-03T10:00:00Z")
        self.assertTrue(signed["signed"])

    def test_revoke_policy_revokes_cert(self):
        """Under revoke_on_retraction, retracting the basis revokes the live cert."""
        self.make_bc()
        self.svc.create_period("2026-01-11", "2026-01-31",
                               review_policy="revoke_on_retraction", period_id="Pr")
        res = self.svc.open_period("Pr", ["C"], ["shade"], issued_at="2026-01-11T00:00:00Z")
        self.assertEqual(len(res["issued"]), 1)
        # Retract the cited evidence, then run the review.
        ev_bc = [e for e in self.svc.list_evidence(person_id="C", craft_id="shade")][0]
        self.svc.retract_evidence(ev_bc["id"], "wrong", at_ts="2026-01-15T00:00:00Z")
        review = self.svc.review_period("Pr", at_ts="2026-01-16T00:00:00Z")
        revoked = [a for a in review["affected"] if a["decision"] == "revoked"]
        self.assertEqual(len(revoked), 1)
        certs = self.svc.list_certificates(person_id="C", craft_id="shade")
        self.assertTrue(any(c["status"] == "revoked" for c in certs))
        # Signing now fails.
        self.svc.create_shift("C", "signer", T["shift_start"], T["shift_end"])
        self.svc.create_product("lamp", "lamp1")
        with self.assertRaises(Conflict) as ctx:
            self.svc.sign_product("lamp1", "C", "req", at_ts=T["sign"])
        self.assertEqual(ctx.exception.code, "NO_VALID_CERTIFICATE")


if __name__ == "__main__":
    unittest.main()
