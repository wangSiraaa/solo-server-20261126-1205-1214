"""Concrete two-card / small-box fixtures used by demo and tests."""
from __future__ import annotations

from typing import Any


def _participants() -> list[dict[str, str]]:
    return [
        {"id": "pA", "name": "阿甲（终端 A）", "claim_code": "2468", "terminal_id": "terminal-A"},
        {"id": "pB", "name": "阿乙（终端 B）", "claim_code": "1357", "terminal_id": "terminal-B"},
    ]


def main_config(game_id: str = "main") -> dict[str, Any]:
    """Baseline winner plus an electronic-corner stale-version attempt."""
    return {
        "game_id": game_id,
        "name": "云联欢主示范场",
        "mode": "electronic_corner",
        # 2,5,7 are deliberately the first three sealed-tape numbers.
        "tape": [2, 5, 7, 1, 3, 4, 6, 8, 9, 10, 11],
        "sticker_ids": ["STK-001", "STK-002"],
        "participants": _participants(),
        "cards": [
            {
                "id": "card-A",
                "participant_id": "pA",
                # Main diagonal D1 is exactly 2,5,7.
                "grid": [
                    [2, 9, 4],
                    [3, 5, 8],
                    [6, 1, 7],
                ],
            },
            {
                "id": "card-B",
                "participant_id": "pB",
                # D2 = TR(2), center(5), BL(1).  The TR corner is swapped to 3.
                "grid": [
                    [10, 4, 2],
                    [6, 5, 9],
                    [1, 8, 11],
                ],
            },
        ],
        "corner_blocks": [
            {"id": "CB-3", "number": 3, "card_id": "card-B"},
        ],
    }


def late_seven_config(game_id: str = "late7") -> dict[str, Any]:
    """7 is moved after the call; later the window is closed before the late call."""
    return {
        "game_id": game_id,
        "name": "七号延后/窗闭复场",
        "mode": "electronic_corner",
        "tape": [2, 5, 1, 7, 3, 4, 6, 8, 9],
        "sticker_ids": ["LATE-001"],
        "participants": [_participants()[0]],
        "cards": [
            {
                "id": "card-L",
                "participant_id": "pA",
                "grid": [
                    [2, 9, 4],
                    [3, 5, 8],
                    [6, 1, 7],
                ],
            }
        ],
        "corner_blocks": [],
    }


def race_config(game_id: str = "race") -> dict[str, Any]:
    """Two cards complete a different line on the same final reveal; one sticker."""
    return {
        "game_id": game_id,
        "name": "同拍最后一枚争领场",
        "mode": "electronic_corner",
        "tape": [1, 2, 3, 4, 5, 6, 7, 8, 9],
        "sticker_ids": ["LAST-001"],
        "participants": _participants(),
        "cards": [
            {
                "id": "card-R1",
                "participant_id": "pA",
                "grid": [
                    [1, 2, 3],
                    [4, 5, 6],
                    [7, 8, 9],
                ],
            },
            {
                "id": "card-R2",
                "participant_id": "pB",
                # Column C2 is 1,2,3.
                "grid": [
                    [4, 1, 7],
                    [5, 2, 8],
                    [6, 3, 9],
                ],
            },
        ],
        "corner_blocks": [],
    }


def swapped_diagonal_config(game_id: str = "swapdiag") -> dict[str, Any]:
    """2 on the main diagonal is stamped, then that very corner is replaced."""
    return {
        "game_id": game_id,
        "name": "换角旧斜线路径核验场",
        "mode": "electronic_corner",
        "tape": [2, 5, 7, 10, 1],
        "sticker_ids": ["SWAP-001"],
        "participants": [_participants()[0]],
        "cards": [
            {
                "id": "card-Q",
                "participant_id": "pA",
                "grid": [
                    [2, 9, 4],
                    [3, 5, 8],
                    [6, 1, 7],
                ],
            }
        ],
        "corner_blocks": [{"id": "CB-10", "number": 10, "card_id": "card-Q"}],
    }
