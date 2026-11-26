import json
import tempfile
import unittest
from pathlib import Path

from whiteboard.model import (
    MAX_STEPS_PER_DRAFT,
    WhiteboardError,
    evaluate_patch,
    make_board,
    plan_merge,
    simulate,
)
from whiteboard.service import Service
from whiteboard.store import Store


def row_three(wall=True):
    walls = [[[0, 1], [0, 2]]] if wall else []
    return make_board(1, 3, walls=walls)


class FloodFillTests(unittest.TestCase):
    def test_paint_stops_at_wall(self):
        board = row_three(True)
        result = simulate(board, [{"op": "paint", "at": [0, 0], "color": "blue"}])
        self.assertEqual(result["board"]["cells"], [["blue", "blue", "white"]])
        self.assertEqual(result["trace"][0]["painted"], [[0, 0], [0, 1]])

    def test_removed_wall_allows_full_puddle(self):
        board = row_three(True)
        simulate(
            board,
            [
                {"op": "remove_wall", "edge": [[0, 1], [0, 2]]},
                {"op": "paint", "at": [0, 0], "color": "blue"},
            ],
        )
        # simulate does not mutate input; check via returned board.
        result = simulate(
            row_three(True),
            [
                {"op": "remove_wall", "edge": "0,1|0,2"},
                {"op": "paint", "at": "0,0", "color": "blue"},
            ],
        )
        self.assertEqual(result["board"]["cells"], [["blue", "blue", "blue"]])

    def test_spread_uses_origin_color_only(self):
        board = make_board(1, 4)
        board["cells"][0][1] = "red"
        event = simulate(board, [{"op": "paint", "at": [0, 0], "color": "blue"}])
        self.assertEqual(event["board"]["cells"], [["blue", "red", "white", "white"]])

    def test_step_limit(self):
        with self.assertRaises(WhiteboardError):
            simulate(row_three(), [{"op": "paint", "at": [0, 0], "color": c} for c in "abcde"])

    def test_board_size_limit(self):
        with self.assertRaises(WhiteboardError):
            make_board(6, 1)


class MergeSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(Store(str(Path(self.tmp.name) / "state.json")))
        self.service.create_class(
            {
                "id": "lesson",
                "convention": "patch",
                "rows": 1,
                "cols": 3,
                "walls": [[[0, 1], [0, 2]]],
            }
        )
        self.service.create_group("lesson", {"id": "jia"})
        self.service.create_group("lesson", {"id": "yi"})
        self.service.create_draft(
            "lesson",
            {
                "id": "jia",
                "group_id": "jia",
                "score": 5,
                "steps": [{"op": "paint", "at": [0, 0], "color": "blue"}],
            },
        )
        self.service.create_draft(
            "lesson",
            {
                "id": "yi",
                "group_id": "yi",
                "score": 3,
                "steps": [{"op": "remove_wall", "edge": [[0, 1], [0, 2]]}],
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_group_preview_is_private_and_keeps_wall_context(self):
        preview = self.service.preview_group(
            "jia", {"steps": [{"op": "paint", "at": [0, 0], "color": "blue"}]}
        )
        self.assertEqual(preview["preview_board"]["cells"], [["blue", "blue", "white"]])
        group_yi = self.service.get_group("yi")
        self.assertEqual(group_yi["board"]["cells"], [["white", "white", "white"]])

    def test_patch_keeps_a_submitted_picture_after_b_opens_gap(self):
        plan = self.service.plan_merge("lesson", {})
        self.assertEqual(plan["best"]["score"], 8)
        self.assertEqual(plan["best"]["draft_ids"], ["jia", "yi"])
        board = plan["best"]["board"]
        self.assertEqual(board["cells"], [["blue", "blue", "white"]])
        self.assertFalse(board["walls"]["0,1|0,2"])

    def test_planning_does_not_early_count_unselected_wall(self):
        before = self.service.get_class("lesson")["current_board"]
        self.service.plan_merge("lesson", {"draft_ids": ["jia"]})
        after = self.service.get_class("lesson")["current_board"]
        self.assertEqual(before["walls"], after["walls"])
        self.assertTrue(after["walls"]["0,1|0,2"])

    def test_cell_three_blue_ban_does_not_change_patch(self):
        self.service.update_lesson(
            "lesson", {"forbidden": [{"cell": [0, 2], "color": "blue"}]}
        )
        plan = self.service.plan_merge("lesson", {})
        self.assertEqual(plan["best"]["score"], 8)
        self.assertEqual(plan["best"]["draft_ids"], ["jia", "yi"])

    def test_cell_two_blue_ban_blocks_a_patch(self):
        self.service.update_lesson(
            "lesson", {"forbidden": [{"cell": [0, 1], "color": "blue"}]}
        )
        plan = self.service.plan_merge("lesson", {})
        self.assertEqual(plan["best"]["draft_ids"], ["yi"])
        self.assertEqual(plan["best"]["score"], 3)

    def test_patch_write_conflict_is_not_legal(self):
        self.service.create_draft(
            "lesson",
            {
                "id": "bing",
                "score": 4,
                "steps": [{"op": "paint", "at": [0, 0], "color": "red"}],
            },
        )
        plan = self.service.plan_merge("lesson", {"draft_ids": ["jia", "bing"]})
        self.assertEqual(plan["best"]["draft_ids"], ["jia"])
        self.assertNotEqual(plan["best"]["score"], 9)
        direct = evaluate_patch(
            self.service.get_class("lesson")["current_board"],
            {d["id"]: d for d in self.service.list_drafts("lesson")},
            ["jia", "bing"],
            {},
        )
        self.assertTrue(direct["conflicts"])

    def test_replay_order_changes_puddle(self):
        # 同一套稿件放到 replay 约定的课节中。
        self.service.create_class(
            {"id": "replay", "convention": "replay", "rows": 1, "cols": 3,
             "walls": [[[0, 1], [0, 2]]]}
        )
        self.service.create_draft(
            "replay", {"id": "a", "score": 5,
                       "steps": [{"op": "paint", "at": [0, 0], "color": "blue"}]}
        )
        self.service.create_draft(
            "replay", {"id": "b", "score": 3,
                       "steps": [{"op": "remove_wall", "edge": [[0, 1], [0, 2]]}]}
        )
        ab = self.service.plan_merge("replay", {"order": ["a", "b"]})["best"]
        ba = self.service.plan_merge("replay", {"order": ["b", "a"]})["best"]
        self.assertEqual(ab["board"]["cells"], [["blue", "blue", "white"]])
        self.assertEqual(ba["board"]["cells"], [["blue", "blue", "blue"]])
        self.assertTrue(ba["draft_reports"][1]["changed_because_of_merge_context"])

        self.service.update_lesson(
            "replay", {"forbidden": [{"cell": [0, 2], "color": "blue"}]}
        )
        banned_ba = self.service.plan_merge("replay", {"order": ["b", "a"]})
        self.assertIsNone(banned_ba["best"])
        allowed_ab = self.service.plan_merge("replay", {"order": ["a", "b"]})["best"]
        self.assertEqual(allowed_ab["score"], 8)

    def test_must_keep_cell_blocks_patch(self):
        self.service.update_lesson(
            "lesson", {"must_keep": [{"cell": [0, 0], "color": "white"}]}
        )
        plan = self.service.plan_merge("lesson", {})
        self.assertEqual(plan["best"]["draft_ids"], ["yi"])
        master = self.service.get_class("lesson")["current_board"]
        illegal = evaluate_patch(
            master,
            {d["id"]: d for d in self.service.list_drafts("lesson")},
            ["jia"],
            {"must_keep": [{"cell": [0, 0], "color": "white"}]},
        )
        self.assertTrue(any(v["kind"] == "must_keep" for v in illegal["violations"]))

    def test_publish_signs_new_board_and_persists(self):
        published = self.service.publish_merge("lesson", {})
        new_id = published["current_board_id"]
        self.assertTrue(new_id.startswith("board_"))
        self.assertEqual(self.service.get_class("lesson")["current_board_id"], new_id)
        path = Path(self.tmp.name) / "state.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn(new_id, raw["boards"])

        reopened = Service(Store(str(path)))
        board = reopened.get_board(new_id)
        self.assertEqual(board["cells"], [["blue", "blue", "white"]])
        drafts = reopened.list_drafts("lesson")
        self.assertTrue(all(d["status"] == "published" for d in drafts))


if __name__ == "__main__":
    unittest.main()
