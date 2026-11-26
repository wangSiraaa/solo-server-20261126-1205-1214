"""Scenario 3: post assignment decides sign-off authority.

保留原先时序，只把丙从本件所需岗位换到无签收权的学徒台：
先前资格仍可查询，当次不能落款。
"""

from __future__ import annotations

import unittest

from harness import WorkshopTestCase, T
from lampshop.errors import Conflict


class PostAssignmentTests(WorkshopTestCase):
    def test_qualification_remains_but_wrong_post_blocks_signature(self):
        self.ready_to_sign()  # 丙 gets production qual + signing shift
        # Same timeline; swap 丙 onto the no-authority apprentice post instead.
        self.svc.create_shift("C", "apprentice_post", T["shift_start"], T["shift_end"])
        # Remove the signing shift so only the apprentice post is active.
        # (Create a superseding reality: the signing shift ended before now.)
        # Simpler: place the signature time on a day 丙 is only on apprentice duty.
        at = "2026-01-13T10:00:00Z"
        self.svc.create_shift("C", "apprentice_post", "2026-01-13T08:00:00Z", "2026-01-13T18:00:00Z")

        # Prior qualification is still queryable.
        q = self.svc.list_qualifications(person_id="C", craft_id="shade", capacity="production")
        self.assertTrue(any(self.svc._qual_valid_at(r, at) for r in q))
        self.assertTrue(self.svc.eligibility("C", "shade", at)["eligible_for_production"])

        self.svc.create_product("lamp", "lamp1")
        with self.assertRaises(Conflict) as ctx:
            self.svc.sign_product("lamp1", "C", "req-x", at_ts=at)
        self.assertEqual(ctx.exception.code, "POST_NOT_AUTHORIZED")
        # And no signer was recorded.
        self.assertIsNone(self.svc.get_product("lamp1")["signature"])

    def test_off_shift_cannot_sign(self):
        self.ready_to_sign()
        self.svc.create_product("lamp", "lamp1")
        at = "2026-01-15T10:00:00Z"  # no shift then
        with self.assertRaises(Conflict) as ctx:
            self.svc.sign_product("lamp1", "C", "req-x", at_ts=at)
        self.assertEqual(ctx.exception.code, "POST_NOT_AUTHORIZED")

    def test_attempted_bad_sign_does_not_consume_the_scene(self):
        """A rejected attempt leaves the product pending; a valid signer can still sign."""
        self.ready_to_sign()
        self.svc.create_product("lamp", "lamp1")
        with self.assertRaises(Conflict):
            self.svc.sign_product("lamp1", "C", "req-bad", at_ts="2026-01-15T10:00:00Z")
        res = self.svc.sign_product("lamp1", "C", "req-good", at_ts=T["sign"])
        self.assertTrue(res["signed"])


if __name__ == "__main__":
    unittest.main()
