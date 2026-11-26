"""Scenario 2: temporal integrity of mentoring evidence.

* 乙's practicum with 丙 cannot retroactively prove 乙 was qualified back then.
* If only 乙's own qualification is moved earlier, the *same* practicum evidence
  is not usable for new certificates/endorsements.
"""

from __future__ import annotations

import unittest

from harness import WorkshopTestCase, T
from lampshop.errors import Conflict


class TemporalIntegrityTests(WorkshopTestCase):
    def test_mentoring_time_shifted_before_qualification_is_unusable(self):
        """只把乙带丙的见习移到乙获得带教资格以前 -> cannot back new certs/endorsements.

        Concretely: 乙's mentoring qualification only becomes valid *after* the
        乙->丙 practicum ends.  The practicum was never validly supervised, so it
        is not evidence and 丙 cannot be certified against it.
        """
        # 乙 earns mentoring standing only on 2026-01-20, i.e. AFTER teaching 丙
        # on 2026-01-10.  (The old evidence is placed before the qualification.)
        self.grant("B", cap="production", vf="2026-01-05T14:00:00Z")
        self.grant("B", cap="mentoring", vf="2026-01-20T00:00:00Z", enforce=False)
        ev = self.learn("shade", "B", "C", T["bc_start"], T["bc_end"],
                        station="bench1", registered_at=T["reg_bc"])
        # The record exists and is queryable...
        self.assertEqual(ev["status"], "completed")
        # ...but it is not usable evidence, and it never back-dates 乙's authority.
        self.assertFalse(ev["usable_at"])
        self.assertIn("not_validly_supervised", ev["usable_reasons"])
        elig = self.svc.eligibility("C", "shade", T["c_qual"])
        self.assertFalse(elig["eligible_for_production"])
        # And 丙 cannot be granted production standing against it.
        with self.assertRaises(Conflict) as ctx:
            self.grant("C", cap="production", vf=T["c_qual"])
        self.assertEqual(ctx.exception.code, "NOT_ELIGIBLE")

    def test_mentoring_cannot_be_inferred_then_used_to_teach(self):
        """在学员获得某项工艺的资格之前，其带别人的经历不能倒过来证明带教资格.

        乙 has *no* mentoring qualification at all yet teaches 丙.  That teaching
        must not itself be read as proof 乙 was a qualified teacher.
        """
        self.grant("B", cap="production", vf=T["b_qual"])  # production only, no mentoring
        ev = self.learn("shade", "B", "C", T["bc_start"], T["bc_end"],
                        station="bench1", registered_at=T["reg_bc"])
        self.assertFalse(ev["usable_at"])
        # 丙 gets nothing out of it.
        self.assertFalse(self.svc.eligibility("C", "shade", T["c_qual"])["eligible_for_production"])
        # Nor does 乙 acquire mentoring authority by having taught.
        lineage = self.svc.person_lineage("B", "shade", T["b_qual"])
        self.assertIsNone(lineage["mentoring_qualification"])

    def test_qualification_window_must_cover_whole_practicum(self):
        """A qualification valid for only part of the practicum does not supervise it."""
        # Fresh student 壬 so the baseline 乙 practicum does not interfere.
        self.svc.create_person("学徒壬", "R", role="apprentice")
        # 丁 qualified to mentor only 2026-01-15T00:00..2026-01-15T10:00, but the
        # practicum runs to 13:00 -- the window does not cover it.
        self.grant("D", cap="mentoring", vf="2026-01-15T00:00:00Z",
                   vu="2026-01-15T10:00:00Z", enforce=False)
        ev = self.learn("shade", "D", "R", "2026-01-15T09:00:00Z", "2026-01-15T13:00:00Z",
                        station="bench1")
        self.assertFalse(ev["usable_at"])
        self.assertIn("not_validly_supervised", ev["usable_reasons"])

    def test_preserved_timeline_is_usable(self):
        """Control: with the original ordering the practicum IS valid evidence."""
        self.make_bc()
        evs = self.svc.list_evidence(person_id="C", craft_id="shade")
        self.assertTrue(evs)
        self.assertTrue(evs[0]["usable_at"])


if __name__ == "__main__":
    unittest.main()
