from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bingo.core import Backend, BingoError
from bingo.scenarios import (
    late_seven_config,
    main_config,
    race_config,
    swapped_diagonal_config,
)


class BingoRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.backend = Backend(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def create(self, config: dict) -> None:
        self.backend.act("game.create", config)

    def reveal_and_stamp(self, gid: str, card_id: str, pid: str, number: int) -> None:
        self.backend.act("reveal.next", {"game_id": gid})
        self.backend.act(
            "stamp",
            {
                "game_id": gid,
                "card_id": card_id,
                "participant_id": pid,
                "number": number,
                "terminal_id": f"terminal-{pid}",
            },
        )

    def reveal_only(self, gid: str) -> None:
        self.backend.act("reveal.next", {"game_id": gid})

    def test_baseline_diagonal_and_last_sticker_then_restart(self) -> None:
        cfg = main_config("main-restart")
        gid = cfg["game_id"]
        self.create(cfg)
        self.backend.act("window.open", {"game_id": gid, "window_id": "W1"})

        for n in (2, 5, 7):
            self.reveal_and_stamp(gid, "card-A", "pA", n)

        call = self.backend.act(
            "call.create",
            {
                "game_id": gid,
                "card_id": "card-A",
                "participant_id": "pA",
                "line_id": "D1",
                "terminal_id": "terminal-A",
            },
        )
        self.assertEqual(3, call["beat"])
        result = self.backend.act(
            "claim.issue",
            {
                "game_id": gid,
                "call_id": call["id"],
                "participant_id": "pA",
                "claim_code": "2468",
            },
        )
        self.assertEqual("STK-001", result["sticker"]["id"])

        # The redeemed diagonal is locked at the card aggregate level.
        with self.assertRaises(BingoError) as ctx:
            self.backend.act(
                "call.create",
                {
                    "game_id": gid,
                    "card_id": "card-A",
                    "participant_id": "pA",
                    "line_id": "D1",
                },
            )
        self.assertEqual("LINE_ALREADY_REDEEMED", ctx.exception.code)

        # Numbers 2 and 5 were already revealed; B stamps both before swapping.
        self.backend.act(
            "stamp",
            {
                "game_id": gid,
                "card_id": "card-B",
                "participant_id": "pB",
                "number": 2,
            },
        )
        self.backend.act(
            "stamp",
            {
                "game_id": gid,
                "card_id": "card-B",
                "participant_id": "pB",
                "number": 5,
            },
        )
        swap = self.backend.act(
            "corner.swap",
            {
                "game_id": gid,
                "card_id": "card-B",
                "participant_id": "pB",
                "position": "TR",
                "block_id": "CB-3",
            },
        )
        self.assertEqual(2, swap["new_version"])
        with self.assertRaises(BingoError) as stale:
            self.backend.act(
                "call.create",
                {
                    "game_id": gid,
                    "card_id": "card-B",
                    "participant_id": "pB",
                    "card_version": 1,
                    "line_id": "D2",
                },
            )
        self.assertEqual("CARD_VERSION_STALE", stale.exception.code)

        # The retired number cannot be stamped on the old corner after swap.
        with self.assertRaises(BingoError) as retired:
            self.backend.act(
                "stamp",
                {
                    "game_id": gid,
                    "card_id": "card-B",
                    "participant_id": "pB",
                    "number": 2,
                },
            )
        self.assertEqual("RETIRED_CORNER_NUMBER", retired.exception.code)

        # 1 and 3 are the next tape beats; the pre-swap center stamp 5 survives.
        self.reveal_and_stamp(gid, "card-B", "pB", 1)
        self.reveal_and_stamp(gid, "card-B", "pB", 3)

        b_call = self.backend.act(
            "call.create",
            {
                "game_id": gid,
                "card_id": "card-B",
                "participant_id": "pB",
                "line_id": "D2",
                "terminal_id": "terminal-B",
            },
        )
        b_prize = self.backend.act(
            "claim.issue",
            {
                "game_id": gid,
                "call_id": b_call["id"],
                "participant_id": "pB",
                "claim_code": "1357",
            },
        )
        self.assertEqual("STK-002", b_prize["sticker"]["id"])

        report = self.backend.act("stickers.list", {"game_id": gid})
        self.assertEqual(0, report["remaining"])
        owners = {s["id"]: s["participant_id"] for s in report["stickers"]}
        self.assertEqual({"STK-001": "pA", "STK-002": "pB"}, owners)

        # Simulate backend restart: a fresh Backend reads the same JSON file.
        restarted = Backend(self.path)
        report_after = restarted.act("stickers.list", {"game_id": gid})
        owners_after = {s["id"]: s["participant_id"] for s in report_after["stickers"]}
        self.assertEqual(owners, owners_after)
        audit = restarted.act("card.audit", {"game_id": gid, "card_id": "card-A"})
        self.assertIn("D1", audit["redeemed_lines"])

    def test_seven_late_and_call_after_window_close(self) -> None:
        cfg = late_seven_config("late-case")
        gid = cfg["game_id"]
        self.create(cfg)
        self.backend.act("window.open", {"game_id": gid})

        for n in (2, 5, 1):
            self.reveal_and_stamp(gid, "card-L", "pA", n)

        with self.assertRaises(BingoError) as ctx:
            self.backend.act(
                "call.create",
                {"game_id": gid, "card_id": "card-L", "participant_id": "pA", "line_id": "D1"},
            )
        self.assertEqual("LINE_INCOMPLETE_AT_CALL", ctx.exception.code)
        self.assertEqual(
            [7], [m["number"] for m in ctx.exception.details["missing"]]
        )

        self.backend.act("window.close", {"game_id": gid})
        self.reveal_and_stamp(gid, "card-L", "pA", 7)

        audit = self.backend.act("card.audit", {"game_id": gid, "card_id": "card-L"})
        self.assertTrue(audit["lines"]["D1"]["complete"])
        self.assertFalse(audit["lines"]["D1"]["locked"])

        with self.assertRaises(BingoError) as late:
            self.backend.act(
                "call.create",
                {"game_id": gid, "card_id": "card-L", "participant_id": "pA", "line_id": "D1"},
            )
        self.assertEqual("WINDOW_CLOSED", late.exception.code)
        report = self.backend.act("stickers.list", {"game_id": gid})
        self.assertEqual(1, report["remaining"])

    def test_swapped_stamped_corner_does_not_reach_diagonal(self) -> None:
        cfg = swapped_diagonal_config("swap-case")
        gid = cfg["game_id"]
        self.create(cfg)
        self.backend.act("window.open", {"game_id": gid})

        self.reveal_and_stamp(gid, "card-Q", "pA", 2)
        self.backend.act(
            "corner.swap",
            {
                "game_id": gid,
                "card_id": "card-Q",
                "participant_id": "pA",
                "position": "TL",
                "block_id": "CB-10",
            },
        )
        for n in (5, 7):
            self.reveal_and_stamp(gid, "card-Q", "pA", n)

        audit = self.backend.act("card.audit", {"game_id": gid, "card_id": "card-Q"})
        d1 = audit["lines"]["D1"]
        self.assertFalse(d1["complete"])
        self.assertEqual(10, d1["missing"][0]["number"])
        self.assertIsNone(d1["cells"][0]["stamp"])
        self.assertEqual("voided", d1["cells"][0]["voided_stamp"]["status"])
        self.assertEqual(
            "NUMBER_NOT_REVEALED_BY_CALL_BEAT", d1["missing"][0]["reason"]
        )

        with self.assertRaises(BingoError) as ctx:
            self.backend.act(
                "call.create",
                {"game_id": gid, "card_id": "card-Q", "participant_id": "pA", "line_id": "D1"},
            )
        self.assertEqual("LINE_INCOMPLETE_AT_CALL", ctx.exception.code)

        # Once 10 is revealed and the NEW corner itself is stamped, the line works.
        self.reveal_and_stamp(gid, "card-Q", "pA", 10)
        call = self.backend.act(
            "call.create",
            {"game_id": gid, "card_id": "card-Q", "participant_id": "pA", "line_id": "D1"},
        )
        prize = self.backend.act(
            "claim.issue",
            {"game_id": gid, "call_id": call["id"], "participant_id": "pA", "claim_code": "2468"},
        )
        self.assertEqual("SWAP-001", prize["sticker"]["id"])

    def test_unrelated_corner_swap_preserves_diagonal_stamps(self) -> None:
        cfg = {
            "game_id": "unrelated-swap",
            "mode": "electronic_corner",
            "tape": [2, 5, 7, 10],
            "sticker_ids": ["OK-1"],
            "participants": [{"id": "pA", "claim_code": "2468"}],
            "cards": [
                {
                    "id": "card-U",
                    "participant_id": "pA",
                    "grid": [
                        [2, 9, 4],
                        [3, 5, 8],
                        [6, 1, 7],
                    ],
                }
            ],
            # TR is 4, not on D1.
            "corner_blocks": [{"id": "CB10", "number": 10, "card_id": "card-U"}],
        }
        self.create(cfg)
        gid = cfg["game_id"]
        self.backend.act("window.open", {"game_id": gid})
        self.reveal_and_stamp(gid, "card-U", "pA", 2)
        self.backend.act(
            "corner.swap",
            {
                "game_id": gid,
                "card_id": "card-U",
                "participant_id": "pA",
                "position": "TR",
                "block_id": "CB10",
            },
        )
        for n in (5, 7):
            self.reveal_and_stamp(gid, "card-U", "pA", n)

        call = self.backend.act(
            "call.create",
            {"game_id": gid, "card_id": "card-U", "participant_id": "pA", "line_id": "D1"},
        )
        prize = self.backend.act(
            "claim.issue",
            {"game_id": gid, "call_id": call["id"], "participant_id": "pA", "claim_code": "2468"},
        )
        self.assertEqual("OK-1", prize["sticker"]["id"])

    def test_unrevealed_old_corner_number_later_cannot_be_restamped(self) -> None:
        cfg = {
            "game_id": "retired-later",
            "mode": "electronic_corner",
            "tape": [2, 4, 10],
            "sticker_ids": ["X-1"],
            "participants": [{"id": "pA", "claim_code": "2468"}],
            "cards": [
                {
                    "id": "card-X",
                    "participant_id": "pA",
                    "grid": [[4, 1, 2], [3, 5, 6], [7, 8, 9]],
                }
            ],
            "corner_blocks": [{"id": "CB10", "number": 10, "card_id": "card-X"}],
        }
        self.create(cfg)
        gid = cfg["game_id"]
        self.backend.act("window.open", {"game_id": gid})
        self.reveal_and_stamp(gid, "card-X", "pA", 2)
        self.backend.act(
            "corner.swap",
            {
                "game_id": gid,
                "card_id": "card-X",
                "participant_id": "pA",
                "position": "TL",
                "block_id": "CB10",
            },
        )
        self.reveal_only(gid)  # beat 2 reveals retired old-corner number 4
        with self.assertRaises(BingoError) as ctx:
            self.backend.act(
                "stamp",
                {"game_id": gid, "card_id": "card-X", "participant_id": "pA", "number": 4},
            )
        self.assertEqual("RETIRED_CORNER_NUMBER", ctx.exception.code)

    def test_same_beat_claims_serialize_last_sticker(self) -> None:
        cfg = race_config("race-case")
        gid = cfg["game_id"]
        self.create(cfg)
        self.backend.act("window.open", {"game_id": gid})
        for n in (1, 2, 3):
            self.reveal_and_stamp(gid, "card-R1", "pA", n)
            self.reveal_and_stamp(gid, "card-R2", "pB", n)

        c1 = self.backend.act(
            "call.create",
            {"game_id": gid, "card_id": "card-R1", "participant_id": "pA", "line_id": "R1"},
        )
        c2 = self.backend.act(
            "call.create",
            {"game_id": gid, "card_id": "card-R2", "participant_id": "pB", "line_id": "C2"},
        )
        barrier = threading.Barrier(2)

        def claim(call_id: str, pid: str, code: str):
            barrier.wait(timeout=5)
            return self.backend.act(
                "claim.issue",
                {
                    "game_id": gid,
                    "call_id": call_id,
                    "participant_id": pid,
                    "claim_code": code,
                    "request_id": f"req-{call_id}",
                },
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(claim, c1["id"], "pA", "2468")
            f2 = pool.submit(claim, c2["id"], "pB", "1357")
            outcomes = []
            for future in (f1, f2):
                try:
                    outcomes.append(("won", future.result(timeout=5)))
                except BingoError as exc:
                    outcomes.append(("empty", exc))

        wins = [o for kind, o in outcomes if kind == "won"]
        empties = [o for kind, o in outcomes if kind == "empty"]
        self.assertEqual(1, len(wins))
        self.assertEqual(1, len(empties))
        self.assertEqual("STICKER_BOX_EMPTY", empties[0].code)
        self.assertIn("winner_participant_id", empties[0].details)

        report = Backend(self.path).act("stickers.list", {"game_id": gid})
        self.assertEqual(1, report["given_count"])
        self.assertEqual(0, report["remaining"])

    def test_sealed_tape_can_be_created_before_game(self) -> None:
        tape = self.backend.act("tape.seal", {"tape_id": "early", "order": [2, 5, 7]})
        self.assertIn("tape_commitment", tape)
        with self.assertRaises(BingoError):
            self.backend.act("tape.seal", {"tape_id": "early", "order": [9, 9]})

        self.backend.act(
            "game.create",
            {
                "game_id": "from-tape",
                "tape_id": "early",
                "sticker_ids": ["T1"],
                "participants": [{"id": "pA", "claim_code": "2468"}],
                "cards": [
                    {
                        "id": "c1",
                        "participant_id": "pA",
                        "grid": [[2, 9, 4], [3, 5, 8], [6, 1, 7]],
                    }
                ],
            },
        )
        reveal = self.backend.act("reveal.next", {"game_id": "from-tape"})
        self.assertEqual(2, reveal["number"])

    def test_replay_is_read_only(self) -> None:
        cfg = late_seven_config("replay-case")
        gid = cfg["game_id"]
        self.create(cfg)
        self.backend.act("reveal.next", {"game_id": gid})
        before = self.backend.act("state.get", {"game_id": gid})["beat"]
        r1 = self.backend.act("tape.replay", {"game_id": gid})
        r2 = self.backend.act("tape.replay", {"game_id": gid})
        after = self.backend.act("state.get", {"game_id": gid})["beat"]
        self.assertEqual(before, after)
        self.assertEqual([2], r1["revealed_numbers"])
        self.assertEqual(r1["revealed"], r2["revealed"])
        self.assertIsNone(r1["sealed_tape"])  # future order hidden before finish


# Helper for accidental reference; tests pass gid explicitly.
config_game_id = "unused"


if __name__ == "__main__":
    unittest.main()
