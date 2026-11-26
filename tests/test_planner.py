"""排程器纯函数测试。"""

import unittest

from photolab.domain import StepCard
from photolab.planner import (
    candidate_shapes, placements_for, plan_batch, plan_paper_variants,
)

CARD = StepCard.from_pairs([("阶二", 2), ("阶三", 3)])


def face(cell, level="阶二"):
    return {"cell": cell, "role": "face", "level": level}


def bg(cell, level="阶三"):
    return {"cell": cell, "role": "background", "level": level}


def paper(pid, start):
    return {"id": pid, "rows": 1, "cols": 2,
            "start_lights": start, "beam_per_cell": {}}


class PlannerTest(unittest.TestCase):
    def test_minimal_business_has_unique_operation(self):
        """两相邻格最小生意：脸累计二、背景累计一，恰好一种只照背景两拍的操作。"""
        variants = plan_paper_variants(
            "P1", 1, 2, {(0, 0): 2, (0, 1): 1}, {},
            [face((0, 0)), bg((0, 1))], CARD, shot_budget=2,
        )
        self.assertEqual(len(variants), 1)
        plan = variants[0]
        self.assertEqual(len(plan.shots), 2)
        for shot in plan.shots:
            self.assertEqual(shot.mask_shape, ((0, 0),))      # 只开背景格的遮片
            self.assertEqual(shot.offset, (0, 1))
            self.assertEqual(shot.exposures, {"0,1": 1})
        self.assertEqual(plan.final_lights, {"0,0": 2, "0,1": 3})
        self.assertEqual(plan.final_levels, {"0,0": "阶二", "0,1": "阶三"})

    def test_shape_catalog_and_flip_positions(self):
        # 两格纸规范化后有 2 种形状：单格（靠翻动偏移覆盖左右）和整版
        shapes = candidate_shapes(1, 2)
        self.assertEqual(set(shapes), {((0, 0),), ((0, 0), (0, 1))})
        positions = placements_for(((0, 0),), 1, 2)
        self.assertEqual([p.offset for p in positions], [(0, 0), (0, 1)])
        self.assertEqual(
            [set(p.openings) for p in positions],
            [{(0, 0)}, {(0, 1)}],
        )

    def test_card_target_starting_at_four_is_infeasible_in_two_shots(self):
        card4 = StepCard.from_pairs([("阶二", 2), ("阶三", 4)])
        batch = plan_batch(
            [paper("P1", {(0, 0): 2, (0, 1): 1})],
            {"P1": [face((0, 0)), bg((0, 1))]},
            card4, sheet_budget=5, shot_budget=2,
        )
        self.assertFalse(batch.feasible)
        self.assertIn("到不了", batch.reason)
        self.assertIn("起始光量 4", batch.reason)

    def test_face_past_target_level_is_infeasible(self):
        batch = plan_batch(
            [paper("P1", {(0, 0): 3, (0, 1): 1})],   # 脸已吃到 3，越过阶二
            {"P1": [face((0, 0)), bg((0, 1))]},
            CARD, sheet_budget=5, shot_budget=2,
        )
        self.assertFalse(batch.feasible)
        self.assertIn("不能擦掉", batch.reason)

    def test_different_start_paper_after_restart(self):
        """另一张起点不同的纸：脸已在阶二、背景尚未入阶，恰好一种拍法（只照背景三拍）。"""
        variants = plan_paper_variants(
            "P2", 1, 2, {(0, 0): 2, (0, 1): 0}, {},
            [face((0, 0)), bg((0, 1))], CARD, shot_budget=3,
        )
        self.assertEqual(len(variants), 1)
        self.assertEqual(len(variants[0].shots), 3)
        self.assertEqual(variants[0].final_lights, {"0,0": 2, "0,1": 3})
        self.assertEqual(variants[0].final_levels, {"0,0": "阶二", "0,1": "阶三"})

    def test_sheet_budget_blocks_full_sheet_mask(self):
        """片材只给 1 张且只刻全通遮片时，保不住脸 -> 不可行；给背景遮片即可。"""
        batch = plan_batch(
            [paper("P1", {(0, 0): 2, (0, 1): 1})],
            {"P1": [face((0, 0)), bg((0, 1))]},
            CARD, sheet_budget=0, shot_budget=2,
        )
        self.assertFalse(batch.feasible)
        self.assertIn("片材预算", batch.reason)

    def test_background_may_accept_darker_than_target_floor(self):
        """背景可加深：落在目标阶及更深且未越上界的光量都算满足。"""
        variants = plan_paper_variants(
            "P1", 1, 2, {(0, 0): 2, (0, 1): 1}, {},
            [face((0, 0)), bg((0, 1), "阶三")], CARD, shot_budget=3,
        )
        finals = {tuple(v.final_lights.items()) for v in variants}
        # 背景最终 3 或 4 都在阶三（样卡最后一阶上无界），方案不止一个
        self.assertIn((("0,0", 2), ("0,1", 3)), finals)
        self.assertIn((("0,0", 2), ("0,1", 4)), finals)

    def test_beam_per_cell_uploaded_by_shop_changes_shot_count(self):
        """店里上传每拍透两：背景从一到三只需一拍（末阶无界，两拍到五也算阶三）。"""
        variants = plan_paper_variants(
            "P1", 1, 2, {(0, 0): 2, (0, 1): 1},
            {(0, 0): 2, (0, 1): 2},
            [face((0, 0)), bg((0, 1))], CARD, shot_budget=2,
        )
        shortest = variants[0]
        self.assertEqual(len(shortest.shots), 1)
        self.assertEqual(shortest.shots[0].exposures, {"0,1": 2})
        self.assertEqual(shortest.final_lights, {"0,0": 2, "0,1": 3})
        # 任何可行方案都绝不照脸：脸始终停在光量二
        self.assertTrue(all(pp.final_lights["0,0"] == 2 for pp in variants))

    def test_multi_paper_batch_shares_masks_and_respects_budget(self):
        """两张纸共用同一张单格遮片；片材预算为 0 时整批拒收。"""
        papers = [
            paper("P1", {(0, 0): 2, (0, 1): 1}),
            paper("P2", {(0, 0): 2, (0, 1): 0}),
        ]
        reqs = {"P1": [face((0, 0)), bg((0, 1))],
                "P2": [face((0, 0)), bg((0, 1))]}
        batch = plan_batch(papers, reqs, CARD, sheet_budget=5, shot_budget=5)
        self.assertTrue(batch.feasible)
        self.assertEqual(batch.sheets_used, 1)          # 两纸共用一张遮片
        self.assertEqual(batch.shot_total, 2 + 3)
        blocked = plan_batch(papers, reqs, CARD, sheet_budget=0, shot_budget=5)
        self.assertFalse(blocked.feasible)
        self.assertIn("片材预算", blocked.reason)
        no_time = plan_batch(papers, reqs, CARD, sheet_budget=5, shot_budget=3)
        self.assertFalse(no_time.feasible)
        self.assertIn("拍数预算", no_time.reason)


if __name__ == "__main__":
    unittest.main()
