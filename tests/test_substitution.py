"""Scenario 6: substitution rules and lineage convergence."""

from __future__ import annotations

import unittest

from harness import WorkshopTestCase, T
from lampshop.errors import Conflict


class SubstitutionTests(WorkshopTestCase):
    def _setup_multi_mentor_practicum(self, retract_mentor_qual_at=None):
        """甲 and 丁 both guide 丙; 甲 is qualified throughout, 丁 lapses later.

        丁's mentoring authority must cover the practicum (2026-01-10 09:00-13:00)
        but expire before the signing date (2026-01-12), so 甲 must stand in.
        """
        # Give 丁 mentoring authority covering the practicum but expiring 01-11.
        self.grant("D", cap="mentoring", vf="2026-01-01T00:00:00Z",
                   vu="2026-01-11T00:00:00Z", enforce=False)
        # 丙's practicum has BOTH 甲 and 丁 as co-guides, with step attribution.
        ev = self.svc.register_evidence(
            "shade", T["bc_start"], T["bc_end"], ["A", "D"], ["C"], ["cut", "sew"],
            station_id="bench1", registered_at=T["reg_bc"],
            mentor_steps={"A": ["sew"], "D": ["cut"]})
        self.svc.complete_evidence(ev["id"], T["bc_end"])
        return ev["id"]

    def test_absent_mentor_replaced_by_actual_co_guide(self):
        """导师缺席可由另一位确实指导过相关步骤并仍有资格的导师担保."""
        ev_id = self._setup_multi_mentor_practicum()
        # At a later date 丁's mentoring authority has lapsed, but 甲 is still
        # valid and actually guided a step -> 甲 may stand in.
        at = "2026-01-12T10:00:00Z"
        self.grant("C", cap="production", vf="2026-01-11T00:00:00Z", enforce=False)
        lineage = self.svc.person_lineage("C", "shade", at)
        self.assertGreaterEqual(lineage["valid_lineage_count"], 1)
        edge = lineage["primary"][0]
        self.assertEqual(edge["mentor"], "A")
        self.assertTrue(edge["substituted"])
        self.assertIn("D", edge["substituted_for"])

    def test_shopwide_qualified_stranger_cannot_substitute(self):
        """不能全工坊找一个有证者代签.

        丙's only guide 丁 lapses.  A shop-mate 戊 holds a valid mentoring
        certificate but never guided 丙, so 戊 must NOT become the basis and 丙
        cannot sign.
        """
        # Only 丁 guides 丙; 丁 will lapse before signing.  丙 earns the
        # production qualification *while* 丁 is still valid (lineage-backed).
        self.grant("D", cap="mentoring", vf="2026-01-01T00:00:00Z",
                   vu="2026-01-11T00:00:00Z", enforce=False)
        ev = self.svc.register_evidence(
            "shade", "2026-01-10T09:00:00Z", "2026-01-10T13:00:00Z",
            ["D"], ["C"], ["cut", "sew"], station_id="bench1")
        self.svc.complete_evidence(ev["id"], "2026-01-10T13:00:00Z")
        # 戊 is fully certified (mentoring + production) but was never on 丙's record.
        self.svc.create_person("戊", "E", role="mentor")
        self.grant("E", cap="mentoring", vf="2026-01-01T00:00:00Z", enforce=False)
        # 丙's production qualification is lineage-backed, earned on 01-10.
        self.grant("C", cap="production", vf="2026-01-10T14:00:00Z")

        at = "2026-01-12T10:00:00Z"  # by now 丁 has lapsed
        lineage = self.svc.person_lineage("C", "shade", at)
        self.assertEqual(lineage["valid_lineage_count"], 0)
        mentors = {e["mentor"] for chain in lineage["chains"] for e in chain}
        self.assertNotIn("E", mentors)

        # Signing is refused because no genuine basis exists.
        self.svc.create_shift("C", "signer", T["shift_start"], T["shift_end"])
        self.svc.create_product("lamp", "lamp1")
        with self.assertRaises(Conflict) as ctx:
            self.svc.sign_product("lamp1", "C", "req", at_ts=T["sign"])
        self.assertEqual(ctx.exception.code, "NO_VALID_BASIS")

    def test_co_guide_who_did_not_guide_a_step_cannot_stand_in(self):
        """A listed co-mentor who did not actually guide a step cannot substitute.

        甲 and 丁 are both on the record, but step attribution says only 丁 guided
        the steps.  When 丁 lapses, 甲 may not stand in merely by being listed.
        """
        self.grant("D", cap="mentoring", vf="2026-01-01T00:00:00Z",
                   vu="2026-01-11T00:00:00Z", enforce=False)
        ev = self.svc.register_evidence(
            "shade", "2026-01-10T09:00:00Z", "2026-01-10T13:00:00Z",
            ["A", "D"], ["C"], ["cut", "sew"], station_id="bench1",
            mentor_steps={"D": ["cut", "sew"]})  # 甲 listed but guided nothing
        self.svc.complete_evidence(ev["id"], "2026-01-10T13:00:00Z")
        self.grant("C", cap="production", vf="2026-01-11T00:00:00Z", enforce=False)
        at = "2026-01-12T10:00:00Z"
        lineage = self.svc.person_lineage("C", "shade", at)
        self.assertEqual(lineage["valid_lineage_count"], 0)

    def test_converging_lineages_all_recorded(self):
        """多条师承汇到同一人的情形 -- all valid lineages kept, primary chosen deterministically."""
        # 丙 learns from both 甲 (direct) and 乙 (who learned from 甲).
        self.grant("B", cap="production", vf="2026-01-06T00:00:00Z")
        self.grant("B", cap="mentoring", vf="2026-01-06T00:00:00Z", enforce=False)
        # Path 1: direct from 甲.
        ev_direct = self.svc.register_evidence(
            "shade", "2026-01-08T09:00:00Z", "2026-01-08T11:00:00Z",
            ["A"], ["C"], ["cut"], station_id="bench2")
        self.svc.complete_evidence(ev_direct["id"], "2026-01-08T11:00:00Z")
        # Path 2: from 乙.
        ev_bc = self.svc.register_evidence(
            "shade", "2026-01-10T09:00:00Z", "2026-01-10T13:00:00Z",
            ["B"], ["C"], ["cut", "sew"], station_id="bench1")
        self.svc.complete_evidence(ev_bc["id"], "2026-01-10T13:00:00Z")

        self.grant("C", cap="production", vf="2026-01-11T00:00:00Z")  # lineage-backed
        self.svc.create_shift("C", "signer", T["shift_start"], T["shift_end"])
        self.svc.create_product("lamp", "lamp1")
        res = self.svc.sign_product("lamp1", "C", "req", at_ts=T["sign"])
        craft_basis = res["basis"]["crafts"]["shade"]
        self.assertEqual(craft_basis["valid_chain_count"], 2)
        self.assertEqual(len(craft_basis["chains"]), 2)
        # Primary is the direct (shorter) path.
        self.assertEqual(len(craft_basis["primary"]), 1)
        self.assertEqual(craft_basis["primary"][0]["mentor"], "A")


if __name__ == "__main__":
    unittest.main()
