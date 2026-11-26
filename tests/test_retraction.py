"""Scenario 4: retraction & late-verified guidance; compare old vs new pieces."""

from __future__ import annotations

import unittest

from harness import WorkshopTestCase, T
from lampshop.errors import Conflict


class RetractionTests(WorkshopTestCase):
    def _signed_then_retract(self):
        """Sign a piece on 乙->丙 evidence, then retract that evidence afterwards."""
        self.ready_to_sign()
        first = self.sign(product="old-piece", request_id="req-old")
        self.assertTrue(first["signed"])
        # Evidence 乙->丙 is retracted *after* the piece was signed.
        ev_bc = self.svc.list_evidence(person_id="C", craft_id="shade")[0]
        self.svc.retract_evidence(ev_bc["id"], "recorded against wrong crew", at_ts="2026-01-20T00:00:00Z")
        return ev_bc["id"], first

    def test_old_piece_keeps_its_basis_but_new_piece_cannot_reuse_it(self):
        """给出一笔旧见习被撤回 ... 对照所选制度查询旧件和新件."""
        ev_id, first = self._signed_then_retract()
        # Old piece: basis snapshot is intact, but a status view shows retraction.
        trace = self.svc.product_basis("old-piece")
        self.assertTrue(trace["signed"])
        self.assertEqual(trace["signature"]["id"], first["signature_id"])
        cited = trace["current_status_of_cited_evidence"]
        self.assertTrue(any(c["evidence_id"] == ev_id and c["status"] == "retracted" for c in cited))

        # New piece: the retracted basis can no longer anchor a signature.
        # Put 丙 back on a signing post so the *only* remaining failure is the
        # missing basis (this isolates lineage from post authority).
        self.svc.create_shift("C", "signer", "2026-01-21T08:00:00Z", "2026-01-21T18:00:00Z")
        self.svc.create_product("lamp", "new-piece")
        with self.assertRaises(Conflict) as ctx:
            self.svc.sign_product("new-piece", "C", "req-new", at_ts="2026-01-21T10:00:00Z")
        self.assertEqual(ctx.exception.code, "NO_VALID_BASIS")
        # 丙's *production qualification* record itself is not silently deleted.
        self.assertTrue(self.svc.list_qualifications(person_id="C", capacity="production"))

    def test_retraction_is_an_event_not_erasure(self):
        """不能将更正伪装成从未签过成品 -- original completion event remains."""
        ev_id, first = self._signed_then_retract()
        trail = self.svc.audit_trail(entity_id=ev_id)
        actions = [t["action"] for t in trail]
        self.assertIn("complete", actions)
        self.assertIn("retract", actions)
        # The completed_at timestamp is preserved in the retraction audit entry.
        retract_entry = next(t for t in trail if t["action"] == "retract")
        self.assertIsNotNone(retract_entry["details"]["prior_completed_at"])
        # The signature still exists and references the original basis.
        self.assertTrue(self.svc.get_product("old-piece")["signature"])

    def test_late_verified_independent_guidance_as_alternate_line(self):
        """另一条独立指导记录后来才查实的例子.

        丙's chain via 乙 is retracted.  A separate independent_guidance record,
        completed earlier but only *verified* later, can be the valid basis -- but
        only from the moment it is verified, not retroactively.
        """
        self.ready_to_sign()
        # An independent guidance record: 甲 directly guided 丙 on a side job.
        ev_ind = self.svc.register_evidence(
            "shade", "2026-01-08T09:00:00Z", "2026-01-08T13:00:00Z",
            ["A"], ["C"], ["cut", "sew"], station_id="bench2",
            kind="independent_guidance", registered_at="2026-01-07T00:00:00Z",
            evidence_id="ev-ind",
        )
        self.svc.complete_evidence("ev-ind", "2026-01-08T13:00:00Z")

        # Before verification the independent record is NOT usable.
        pre = self.svc.eligibility("C", "shade", "2026-01-09T00:00:00Z")
        self.assertNotIn("ev-ind", pre["usable_evidence"])

        # Retract the 乙->丙 evidence.
        ev_bc = [e for e in self.svc.list_evidence(person_id="C", craft_id="shade")
                 if e["id"] != "ev-ind"][0]
        self.svc.retract_evidence(ev_bc["id"], "roster error", at_ts="2026-01-20T00:00:00Z")

        # Verify the independent guidance *later*.
        self.svc.verify_evidence("ev-ind", "2026-01-22T00:00:00Z")

        # At a time before verification it is still not usable.
        before = self.svc.eligibility("C", "shade", "2026-01-21T00:00:00Z")
        self.assertNotIn("ev-ind", before["usable_evidence"])
        # After verification it becomes usable, and can anchor a new signature.
        self.svc.create_shift("C", "signer", "2026-01-23T08:00:00Z", "2026-01-23T18:00:00Z")
        elig_after = self.svc.eligibility("C", "shade", "2026-01-23T10:00:00Z")
        self.assertIn("ev-ind", elig_after["usable_evidence"])
        self.svc.create_product("lamp", "piece-2")
        res = self.svc.sign_product("piece-2", "C", "req-2", at_ts="2026-01-23T10:00:00Z")
        self.assertTrue(res["signed"])
        chain = res["basis"]["crafts"]["shade"]["primary"]
        self.assertEqual(chain[0]["mentor"], "A")
        self.assertEqual(chain[0]["evidence_id"], "ev-ind")

    def test_boundary_signature_after_retraction_instant_fails(self):
        ev_id, _ = self._signed_then_retract()
        self.svc.create_product("lamp", "piece-late")
        with self.assertRaises(Conflict):
            self.svc.sign_product("piece-late", "C", "r", at_ts="2026-01-20T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
