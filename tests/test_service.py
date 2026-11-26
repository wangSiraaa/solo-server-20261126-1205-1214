from __future__ import annotations

import unittest

from sofa_service.catalog import default_catalog
from sofa_service.floorplan import normalize_floorplan
from sofa_service.geometry import normalize_shape
from sofa_service.movement import pose_violations, rotation_violations
from sofa_service.ordering import goal_poses_for_seat, normalize_order
from sofa_service.scenarios import (
    narrow_floorplan,
    narrow_order,
    small_floorplan,
    small_order,
)
from sofa_service.service import ServiceError, ShopService
from sofa_service.verifier import replay_step_sheet


class PlanningWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ShopService()
        self.catalog = self.service.catalog

    def test_small_order_is_smooth_and_completable(self) -> None:
        quote = self.service.create_quote(
            {
                "customerId": "c-small",
                "floorPlanId": "small-smooth",
                "order": small_order("auto"),
            }
        )
        self.assertTrue(quote["feasible"])
        self.assertEqual(quote["selectedMode"], "whole")
        self.assertTrue(quote["alternativeModesFeasible"]["piecewise"])

        order = self.service.confirm_quote(quote["quoteId"], {"customerId": "c-small"})
        claimed = self.service.claim_order(order["orderId"], {"masterId": "shi-fu"})
        token = claimed["claimToken"]
        oid = order["orderId"]
        total = len(quote["stepSheet"]["steps"])
        for i in range(1, total + 1):
            result = self.service.complete_step(oid, i, {"claimToken": token})
            if i < total:
                self.assertTrue(result["canIssueNext"], result)
        finished = self.service.get_order(oid)
        self.assertEqual(finished["status"], "completed")

    def test_intermediate_rotation_cell_is_the_only_collision(self) -> None:
        fp_data = narrow_floorplan()
        fp_data["propertyBlocked"] = [[3, 2]]
        fp = normalize_floorplan(fp_data)
        shape = normalize_shape(default_catalog()["models"]["yunwu-two-seat"]["components"]["seat-2"])
        state = (2, 1, 0)

        # Both endpoint poses remain individually valid.
        self.assertEqual(pose_violations(fp, shape, state), [])
        self.assertEqual(pose_violations(fp, shape, (2, 1, 1)), [])

        # But the explicit rotation template sweeps blocked intermediate cell.
        end, violations, swept = rotation_violations(fp, shape, state, "cw")
        self.assertEqual(end, (2, 1, 1))
        self.assertTrue(any(v.code == "PROPERTY_BLOCKED" and (3, 2) in [tuple(c) for c in v.to_dict()["cells"]] for v in violations))
        self.assertIn((3, 2), swept)

    def test_blocked_template_stops_issuance_and_restore_allows_completion(self) -> None:
        quote = self.service.create_quote(
            {
                "customerId": "c-narrow",
                "floorPlanId": "narrow-l-turn",
                "measurementVersionId": "m-door-v1",
                "order": narrow_order("piecewise"),
            }
        )
        self.assertTrue(quote["feasible"])
        sheet = quote["stepSheet"]
        turn_index = next(
            i
            for i, step in enumerate(sheet["steps"], 1)
            if step["kind"] == "rotate" and step.get("pivot") == [2, 1] and step.get("direction") == "cw"
        )

        oid = self.service.confirm_quote(quote["quoteId"], {"customerId": "c-narrow"})["orderId"]
        claimed = self.service.claim_order(oid, {"masterId": "master-zhao"})
        token = claimed["claimToken"]

        self.service.set_blocks("narrow-l-turn", [[3, 2]], reason="property cabinet")
        validation = self.service.revalidate_order(oid)
        self.assertFalse(validation["valid"])
        self.assertEqual(validation["firstInvalidStepIndex"], turn_index)
        self.assertEqual(validation["violations"][0]["code"], "PROPERTY_BLOCKED")

        # Steps before the bad turn can be reported, but no next step is issued
        # once full-sheet revalidation sees the future blocked sweep.
        for i in range(1, turn_index):
            result = self.service.complete_step(oid, i, {"claimToken": token})
            self.assertFalse(result["canIssueNext"])
            self.assertIsNone(result["nextStep"])

        with self.assertRaises(ServiceError) as ctx:
            self.service.complete_step(oid, turn_index, {"claimToken": token})
        self.assertEqual(ctx.exception.code, "STEP_SHEET_INVALID")

        # Restoring the exact geometry restores the original template hash.
        fp = self.service.restore_floorplan("narrow-l-turn")
        self.assertEqual(fp["geometryHash"], quote["floorPlanHashAtQuote"])
        self.assertTrue(self.service.revalidate_order(oid)["valid"])

        total = len(sheet["steps"])
        for i in range(turn_index, total + 1):
            result = self.service.complete_step(oid, i, {"claimToken": token})
            if i < total:
                self.assertTrue(result["canIssueNext"], f"step {i}: {result}")
        self.assertEqual(self.service.get_order(oid)["status"], "completed")

    def test_changed_clearance_invalidates_old_sheet_but_new_sequence_works(self) -> None:
        request = narrow_order()
        quote = self.service.create_quote(
            {"floorPlanId": "narrow-l-turn", "order": request, "customerId": "clearance"}
        )
        oid = self.service.confirm_quote(quote["quoteId"], {"customerId": "clearance"})["orderId"]
        self.service.claim_order(oid, {"masterId": "m"})

        self.service.change_clearance("yunwu-two-seat", [[2, 0]], "back-to-seat")
        stale = self.service.revalidate_order(oid)
        self.assertFalse(stale["valid"])
        self.assertTrue(any(v["code"] == "ORDER_SNAPSHOT_MISMATCH" for v in stale["violations"]))
        self.assertTrue(any(v["code"] == "CLEARANCE_OCCUPIED" for v in stale["violations"]))

        revised_request = narrow_order()
        revised_quote = self.service.create_quote(
            {"floorPlanId": "narrow-l-turn", "order": revised_request, "customerId": "clearance-2"}
        )
        self.assertTrue(revised_quote["feasible"])
        replay = replay_step_sheet(
            revised_quote["stepSheet"],
            self.service.floorplans["narrow-l-turn"],
            self.service.catalog,
            revised_request,
        )
        self.assertTrue(replay["valid"], replay["violations"][:3])
        order = revised_quote["stepSheet"]["assemblyOrder"]
        back_at = next(i for i, x in enumerate(order) if x.startswith("back-to-seat"))
        arm_at = next(i for i, x in enumerate(order) if x.startswith("left-arm"))
        self.assertLess(back_at, arm_at)

    def test_lug_arm_revises_sheet_and_old_actions_do_not_pass(self) -> None:
        standard_request = narrow_order()
        quote = self.service.create_quote(
            {"floorPlanId": "narrow-l-turn", "order": standard_request}
        )
        lug_request = narrow_order(left_arm="arm-lug", right_arm="arm-std")
        stale = self.service.generic_replay(
            {
                "stepSheet": quote["stepSheet"],
                "floorPlanId": "narrow-l-turn",
                "orderRequest": lug_request,
                "catalog": self.catalog,
            }
        )
        self.assertFalse(stale["valid"])
        codes = {v["code"] for v in stale["violations"]}
        self.assertIn("ORDER_SNAPSHOT_MISMATCH", codes)
        self.assertTrue(
            "WALL_COLLISION" in codes or "ITEM_COLLISION" in codes or "FINAL_OUT_OF_BOUNDS" in codes
        )

        lug_quote = self.service.create_quote(
            {"customerId": "lug", "floorPlanId": "narrow-l-turn", "order": lug_request}
        )
        self.assertTrue(lug_quote["feasible"])
        replay = replay_step_sheet(
            lug_quote["stepSheet"],
            self.service.floorplans["narrow-l-turn"],
            self.catalog,
            lug_request,
        )
        self.assertTrue(replay["valid"], replay["violations"][:3])

    def test_late_quote_confirmation_cannot_replace_claimed_order(self) -> None:
        q_v1 = self.service.create_quote(
            {
                "customerId": "survey-customer",
                "floorPlanId": "narrow-l-turn",
                "measurementVersionId": "m-door-v1",
                "order": narrow_order(),
            }
        )
        order = self.service.confirm_quote(q_v1["quoteId"], {"customerId": "survey-customer"})
        self.service.claim_order(order["orderId"], {"masterId": "master-li"})

        q_v2 = self.service.create_quote(
            {
                "customerId": "survey-customer",
                "floorPlanId": "narrow-l-turn",
                "measurementVersionId": "m-door-v2",
                "order": narrow_order(),
            }
        )
        with self.assertRaises(ServiceError) as ctx:
            self.service.confirm_quote(q_v2["quoteId"], {"customerId": "survey-customer"})
        self.assertEqual(ctx.exception.code, "ORDER_ALREADY_CLAIMED")
        self.assertEqual(ctx.exception.details["claimedOrderId"], order["orderId"])

        provenance = self.service.get_provenance(order["orderId"])
        self.assertEqual(provenance["measurementVersionId"], "m-door-v1")
        self.assertEqual(provenance["measurement"]["surveyor"], "量尺员·周")
        self.assertEqual(provenance["measurement"]["widthCm"], 80.0)

        # Replaying the same authoritative confirmation is idempotent.
        again = self.service.confirm_quote(q_v1["quoteId"], {"customerId": "survey-customer"})
        self.assertEqual(again["orderId"], order["orderId"])

    def test_left_and_right_orientation_produce_mirrored_feature_targets(self) -> None:
        left = normalize_order(
            {"orientation": "left", "featureArmSku": "arm-lug"}, default_catalog()
        )
        right = normalize_order(
            {"orientation": "right", "featureArmSku": "arm-lug"}, default_catalog()
        )
        left_goals = goal_poses_for_seat(left, (3, 5, 0))
        right_goals = goal_poses_for_seat(right, (3, 5, 0))
        self.assertEqual(left_goals["armLeft"], (5, 5, 3))
        self.assertEqual(left_goals["armRight"], (2, 5, 0))
        self.assertEqual(right_goals["armRight"], (2, 5, 3))
        self.assertEqual(right_goals["armLeft"], (5, 5, 0))

    def test_six_by_six_limit(self) -> None:
        with self.assertRaises(Exception):
            normalize_floorplan({"id": "too-big", "width": 7, "height": 3, "rows": ["." * 7] * 3})


if __name__ == "__main__":
    unittest.main()
