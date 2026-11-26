"""Event vocabulary.

Each entry is immutable once appended.  Events carry a monotonically increasing
``seq``.  ``action_id`` makes settlement idempotent across restarts/replays.
"""

from __future__ import annotations

from typing import Any, Dict, List

START = "start"
DRAFT = "draft"
HINT_REQUEST = "hint_request"
HINT = "hint"
HANDIN = "handin"
SEAL_HANDIN = "seal_handin"
REVEAL_SEALED = "reveal_sealed"
JUDGE_REVISE = "judge_revise"
SCORE_TABLE_REVISE = "score_table_revise"
NEW_REVISION = "new_revision"
CLOSE = "close"

# events that can be re-driven at the server by a client (and replayed)
USER_ACTIONS = {DRAFT, HINT_REQUEST, HINT, HANDIN, SEAL_HANDIN, REVEAL_SEALED}


def ev_start(team_ids: List[str], mode: str) -> Dict[str, Any]:
    return {"type": START, "team_ids": list(team_ids), "mode": mode}


def ev_draft(team: str, entry_id: str, letters: Dict[str, str]) -> Dict[str, Any]:
    return {"type": DRAFT, "team": team, "entry": entry_id,
            "letters": dict(letters)}


def ev_hint_request(team: str, entry_id: str, cell: List[int]) -> Dict[str, Any]:
    return {"type": HINT_REQUEST, "team": team, "entry": entry_id,
            "cell": [int(cell[0]), int(cell[1])]}


def ev_hint(team: str, entry_id: str, cell: List[int], letter: str,
            action_id: str) -> Dict[str, Any]:
    # ``teaches`` is filled by the server: every entry that shares this cell
    return {"type": HINT, "team": team, "entry": entry_id,
            "cell": [int(cell[0]), int(cell[1])], "letter": letter.upper(),
            "action_id": action_id}


def ev_handin(team: str, entry_id: str, word: str, action_id: str) -> Dict[str, Any]:
    return {"type": HANDIN, "team": team, "entry": entry_id,
            "word": word.upper(), "action_id": action_id}


def ev_seal(team: str, entry_id: str, word: str, action_id: str) -> Dict[str, Any]:
    return {"type": SEAL_HANDIN, "team": team, "entry": entry_id,
            "word": word.upper(), "action_id": action_id}


def ev_reveal(team: str) -> Dict[str, Any]:
    return {"type": REVEAL_SEALED, "team": team}


def ev_judge_revise(entry_id: str, accepted: List[str], note: str = "") -> Dict[str, Any]:
    return {"type": JUDGE_REVISE, "entry": entry_id,
            "accepted": [w.upper() for w in accepted], "note": note}


def ev_score_table_revise(cell_points: Dict[str, int], note: str = "") -> Dict[str, Any]:
    return {"type": SCORE_TABLE_REVISE,
            "cell_points": {str(k): int(v) for k, v in cell_points.items()},
            "note": note}


def ev_new_revision(label: str) -> Dict[str, Any]:
    return {"type": NEW_REVISION, "label": str(label)}


def ev_close(label: str = "final") -> Dict[str, Any]:
    return {"type": CLOSE, "label": str(label)}
