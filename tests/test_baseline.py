"""Scenario 1: the baseline narrative and the primary sign-off."""

from __future__ import annotations

import unittest

from harness import WorkshopTestCase, T
from lampshop.errors import Conflict


class BaselineNarrativeTests(WorkshopTestCase):
    def test_acquisition_chain_甲_乙_丙(self):
        """甲 teaches 乙 both steps; 乙 (once qualified) teaches 丙; 丙 becomes eligible."""
        ev_ab = self.learn("shade", "A", "B", T["ab_start"], T["ab_end"],
                           station="bench1", registered_at=T["reg_ab"])
        self.assertEqual(ev_ab["status"], "completed")
        self.assertTrue(ev_ab["is_evidence"])
        # 乙 can now earn production standing.
        self.assertTrue(self.svc.eligibility("B", "shade", T["b_qual"])["eligible_for_production"])
        ev_bc = self.make_bc()
        self.assertTrue(ev_bc["is_evidence"])
        elig_c = self.svc.eligibility("C", "shade", T["c_qual"])
        self.assertTrue(elig_c["eligible_for_production"], elig_c)

    def test_registration_book_alone_is_not_evidence(self):
        """A registered practicum that has not completed is not evidence."""
        self.svc.create_person("学徒辛", "H", role="apprentice")
        ev = self.svc.register_evidence(
            "shade", "2026-01-15T09:00:00Z", "2026-01-15T13:00:00Z", ["A"], ["H"],
            ["cut", "sew"], station_id="bench1", registered_at="2026-01-14T00:00:00Z")
        self.assertEqual(ev["status"], "registered")
        self.assertFalse(ev["is_evidence"])
        self.assertFalse(self.svc.eligibility("H", "shade", T["b_qual"])["eligible_for_production"])

    def test_丙_signs_first_independent_piece_on_shift(self):
        """办成丙当班的第一次独立落款."""
        self.ready_to_sign()
        res = self.sign()
        self.assertTrue(res["signed"])
        self.assertEqual(res["signer_id"], "C")
        basis = res["basis"]
        self.assertEqual(basis["regime"], "endorsement")
        self.assertEqual(basis["post"]["post_id"], "signer")
        # 丙's basis is a real lineage to root 甲.
        chain = basis["crafts"]["shade"]["primary"]
        self.assertEqual(chain[0]["mentor"], "B")
        self.assertEqual(chain[1]["mentor"], "A")

    def test_product_basis_traces_from_product_number_to_basis(self):
        """店主可从成品号追到当时实际采用的依据."""
        self.ready_to_sign()
        self.sign()
        trace = self.svc.product_basis("lamp1")
        self.assertTrue(trace["signed"])
        self.assertEqual(trace["signature"]["signer_id"], "C")
        self.assertEqual(trace["signature"]["basis"]["regime"], "endorsement")
        cited = {e["evidence_id"] for e in trace["current_status_of_cited_evidence"]}
        self.assertTrue(cited)  # cites the actual practicum evidence

    def test_signers_can_sign_from_different_customer_sessions(self):
        """老师与两名学员能从不同客户会话办理 -- same product, one final signer."""
        self.ready_to_sign()
        # 乙 also on duty and qualified (root-granted production).
        self.svc.create_shift("B", "signer", T["shift_start"], T["shift_end"])
        self.svc.create_product("lamp", "lamp1")
        first = self.svc.sign_product("lamp1", "C", "sess-C", at_ts=T["sign"],
                                      customer_session="session-C")
        second = self.svc.sign_product("lamp1", "B", "sess-B", at_ts=T["sign"],
                                       customer_session="session-B")
        self.assertEqual(first["signature_id"], second["signature_id"])
        self.assertTrue(second["duplicate_request"])
        prod = self.svc.get_product("lamp1")
        self.assertEqual(prod["status"], "signed")

    def test_learner_cannot_sign_without_production_qualification(self):
        self.make_bc()
        self.svc.create_shift("C", "signer", T["shift_start"], T["shift_end"])
        self.svc.create_product("lamp", "lamp1")
        with self.assertRaises(Conflict) as ctx:
            self.svc.sign_product("lamp1", "C", "req-x", at_ts=T["sign"])
        self.assertEqual(ctx.exception.code, "NO_PRODUCTION_QUALIFICATION")


if __name__ == "__main__":
    unittest.main()
