"""Scoring engine: a deterministic, two-pass fold over the event log.

Pass 1 resolves the answer key per entry from ``judge_revise`` events (the last
revision in the log wins).  Pass 2 replays every action in ``seq`` order and
settles drafts, hints and full-word submissions.

Why a fold?  It gives us all the guarantees the brief asks for, for free:

* **replay** -- folding the same log twice yields the same board;
* **restart** -- reload the persisted log and fold it again;
* **re-judging** -- a corrected accepted-spelling is picked up on the next
  fold, so old hand-ins are re-adjudicated while their original actions stay
  in the log;
* **idempotency** -- every ``action_id`` is remembered, so replaying a hand-in
  (even one whose acknowledgement was lost) can never invent new points.

Per-cell provenance makes the scoring auditable:

    none   -- nothing known yet
    draft  -- the team typed it, not backed by a settled word
    cross  -- dragged in along a crossing from a hint or an already settled word
    hint   -- the server revealed it to that team
    solve  -- earned by a team's own accepted hand-in

Only ``solve`` on an *endpoint* of the settled word can ever carry points, and
points are keyed by ``"row,col"`` -- identical letters in different cells never
share credit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .model import Session

Cell = Tuple[int, int]


class _Reverse:
    """Wraps a value so it sorts in reverse (for heterogeneous tie-break keys)."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __lt__(self, other: "_Reverse") -> bool:
        return self.value > other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Reverse) and self.value == other.value

PROV_NONE = "none"
PROV_DRAFT = "draft"
PROV_CROSS = "cross"
PROV_HINT = "hint"
PROV_SOLVE = "solve"

# provenance values that still permit an independent earn
EARN_OK = (PROV_NONE, PROV_DRAFT)


def ckey(cell) -> str:
    return f"{int(cell[0])},{int(cell[1])}"


@dataclass
class CellInfo:
    cell: Cell
    provenance: str = PROV_NONE
    source_entry: Optional[str] = None
    earned: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"provenance": self.provenance,
                "source_entry": self.source_entry,
                "earned": self.earned}


@dataclass
class TeamState:
    team: str
    cells: Dict[str, CellInfo] = field(default_factory=dict)
    drafts: Dict[str, Dict[str, str]] = field(default_factory=dict)
    hints: List[Dict[str, Any]] = field(default_factory=list)
    handins: List[Dict[str, Any]] = field(default_factory=list)
    pending: List[Dict[str, Any]] = field(default_factory=list)
    sealed_done: List[Dict[str, Any]] = field(default_factory=list)
    score: int = 0
    solved_entries: List[str] = field(default_factory=list)
    last_final_seq: int = 0
    action_ids: Set[str] = field(default_factory=set)
    revealed: bool = False

    def cell(self, c: Cell) -> CellInfo:
        k = ckey(c)
        if k not in self.cells:
            self.cells[k] = CellInfo(cell=(int(c[0]), int(c[1])))
        return self.cells[k]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "team": self.team,
            "score": self.score,
            "solved_entries": list(self.solved_entries),
            "hint_cells": [h["cell"] for h in self.hints],
            "last_final_seq": self.last_final_seq,
            "revealed": self.revealed,
            "cells": {k: v.to_dict() for k, v in sorted(self.cells.items())},
            "handins": [dict(h) for h in self.handins],
            "sealed": [dict(h) for h in self.sealed_done],
            "pending": [dict(h) for h in self.pending],
        }


@dataclass
class Outcome:
    accepted: bool = False
    reason: str = ""
    points: int = 0
    newly_scored: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"accepted": self.accepted, "reason": self.reason,
                "points": self.points, "newly_scored": list(self.newly_scored)}


class Scorer:
    def __init__(self, session: Session, events: List[Dict[str, Any]]) -> None:
        self.s = session
        self.entries = session.entries()
        self.crossings = session.crossings()
        self.cell_points = dict(session.scenario["cell_points"])
        self.mode = session.scenario["mode"]
        self.hint_scope = session.scenario.get("hint_scope", "public")
        self.teams: Dict[str, TeamState] = {}
        self.cell_owner: Dict[str, str] = {}
        self.conflicts: List[Dict[str, Any]] = []
        # pass 1: answer key AND point table, resolved before any replay
        self.accepted, self.cell_points = self._resolve_keys(events)
        # Fold in *logical* order: by (seq, insertion index).  Normally seq is
        # monotonic so this is insertion order; it matters when a lost-response
        # submission is replayed later with its ORIGINAL seq -- the fair order
        # must be the original one, even though the event was appended at the
        # end of the physical log.
        indexed = list(enumerate(events))
        indexed.sort(key=lambda p: (int(p[1].get("seq", 0)), p[0]))
        self.seq_order: List[Dict[str, Any]] = [ev for _, ev in indexed]
        for ev in self.seq_order:
            self.apply(ev)

    # -- answer key + score table (pass 1) --------------------------------
    def _resolve_keys(self, events):
        keys = {eid: {e.answer} for eid, e in self.entries.items()}
        points = dict(self.s.scenario["cell_points"])
        for ev in sorted(events, key=lambda e: int(e.get("seq", 0))):
            etype = ev.get("type")
            if etype == "judge_revise" and ev.get("entry") in keys:
                keys[ev["entry"]] = {w.upper() for w in ev["accepted"]}
            elif etype == "score_table_revise":
                # a later table revision replaces the effective points; the
                # default (unlisted cells) falls back to the base table
                base = dict(self.s.scenario["cell_points"])
                base.update({str(k): int(v)
                             for k, v in ev.get("cell_points", {}).items()})
                points = base
        return keys, points

    def _resolve_accepted_only(self, events):
        return self._resolve_keys(events)[0]

    # -- helpers -----------------------------------------------------------
    def team(self, name: str) -> TeamState:
        if name not in self.teams:
            self.teams[name] = TeamState(team=name)
        return self.teams[name]

    def is_endpoint(self, entry_id: str, cell: Cell) -> bool:
        return cell in self.entries[entry_id].endpoints

    # -- fold --------------------------------------------------------------
    def apply(self, ev: Dict[str, Any]) -> Outcome:
        fn = {
            "start": self._on_start,
            "draft": self._on_draft,
            "hint_request": self._on_hint_request,
            "hint": self._on_hint,
            "handin": self._on_handin,
            "seal_handin": self._on_seal,
            "reveal_sealed": self._on_reveal,
            "judge_revise": lambda e: Outcome(reason="key-revised"),
            "score_table_revise": lambda e: Outcome(reason="table-revised"),
            "new_revision": lambda e: Outcome(reason="revision"),
            "close": lambda e: Outcome(reason="closed"),
        }.get(ev.get("type"))
        if fn is None:
            return Outcome(reason=f"ignored:{ev.get('type')}")
        return fn(ev)

    # -- handlers ----------------------------------------------------------
    def _on_start(self, ev) -> Outcome:
        for name in ev.get("team_ids", []):
            self.team(name)
        return Outcome(reason="started")

    def _on_draft(self, ev) -> Outcome:
        t = self.team(ev["team"])
        t.drafts.setdefault(ev["entry"], {}).update(ev["letters"])
        for key in ev["letters"]:
            cell = tuple(int(x) for x in key.split(","))
            ci = t.cell(cell)
            if ci.provenance == PROV_NONE:
                ci.provenance = PROV_DRAFT
                ci.source_entry = ev["entry"]
        return Outcome(reason="drafted")

    def _on_hint_request(self, ev) -> Outcome:
        self.team(ev["team"])
        return Outcome(reason="hint-requested")

    def _on_hint(self, ev) -> Outcome:
        t = self.team(ev["team"])
        cell = (int(ev["cell"][0]), int(ev["cell"][1]))
        entry_id = ev["entry"]
        aid = ev.get("action_id")
        if aid is not None:
            if aid in t.action_ids:
                return Outcome(accepted=False, reason="duplicate-action")
            t.action_ids.add(aid)
        t.hints.append({"entry": entry_id, "cell": [cell[0], cell[1]],
                        "letter": ev["letter"], "seq": int(ev.get("seq", 0))})
        ci = t.cell(cell)
        if ci.provenance != PROV_SOLVE:
            ci.provenance = PROV_HINT
            ci.source_entry = entry_id
        # A *public* hint (race) reveals the letter to everyone, so nobody may
        # later claim that cell as an independent solve.
        if self.mode == "race" and self.hint_scope == "public":
            for other in self.teams.values():
                if other.team == t.team:
                    continue
                oi = other.cell(cell)
                if oi.provenance in (PROV_NONE, PROV_DRAFT):
                    oi.provenance = PROV_CROSS
                    oi.source_entry = entry_id
        return Outcome(reason="hinted")

    def _on_handin(self, ev) -> Outcome:
        return self._store_and_judge(ev)

    def _on_seal(self, ev) -> Outcome:
        return self._store_and_judge(ev)

    def _on_reveal(self, ev) -> Outcome:
        t = self.team(ev["team"])
        t.revealed = True
        gained = 0
        for rec in sorted(t.pending, key=lambda r: r["seq"]):
            out = self._adjudicate(t, rec)
            gained += out.points
        return Outcome(reason="revealed", points=gained)

    # -- submission handling ----------------------------------------------
    def _store_and_judge(self, ev) -> Outcome:
        t = self.team(ev["team"])
        entry_id = ev["entry"]
        if entry_id not in self.entries:
            return Outcome(reason="unknown-entry")
        word = ev["word"].upper()
        aid = ev.get("action_id")
        seq = int(ev.get("seq", 0))
        if aid is not None:
            if aid in t.action_ids:
                return Outcome(accepted=False, reason="duplicate-action")
            t.action_ids.add(aid)
        rec = {"entry": entry_id, "word": word, "action_id": aid,
               "seq": seq, "team": t.team, "sealed": ev["type"] == "seal_handin",
               "accepted": None}
        # sealed mode conceals *all* submissions until the round reveal
        if self.mode == "sealed" and not t.revealed:
            t.pending.append(rec)
            return Outcome(accepted=False, reason="sealed-pending")
        return self._adjudicate(t, rec)

    def _adjudicate(self, t: TeamState, rec: Dict[str, Any]) -> Outcome:
        entry_id, word, seq = rec["entry"], rec["word"], rec["seq"]
        ok = word in self.accepted.get(entry_id, set())
        rec["accepted"] = ok
        if rec in t.pending:
            t.pending.remove(rec)
        t.sealed_done.append(rec)
        t.handins.append({"entry": entry_id, "word": word, "accepted": ok,
                          "seq": seq, "action_id": rec["action_id"],
                          "sealed": rec["sealed"]})
        if not ok:
            return Outcome(accepted=False, reason="not-accepted")
        return self._settle_word(t, entry_id, word, seq)

    def _settle_word(self, t: TeamState, entry_id: str, word: str,
                     seq: int) -> Outcome:
        e = self.entries[entry_id]
        newly: List[str] = []
        gained = 0

        # 1) independent credit for endpoint cells
        for cell in e.endpoints:
            key = ckey(cell)
            ci = t.cell(cell)
            if ci.provenance not in EARN_OK:
                # hinted, dragged in, or already solved by this team
                continue
            if self.mode == "race":
                owner = self.cell_owner.get(key)
                if owner is not None and owner != t.team:
                    self.conflicts.append({"cell": key, "entry": entry_id,
                                           "team": t.team, "lost_to": owner,
                                           "seq": seq})
                    continue
            pts = int(self.cell_points.get(key, 0))
            ci.provenance = PROV_SOLVE
            ci.source_entry = entry_id
            if pts <= 0:
                continue
            ci.earned = pts
            gained += pts
            newly.append(key)
            if self.mode == "race":
                self.cell_owner[key] = t.team

        # 2) interior letters of the settled word are "dragged" for the solver
        #    too: known, but never independently scorable on their own.
        for cell in e.cells:
            if cell in e.endpoints:
                continue
            ci = t.cell(cell)
            if ci.provenance in (PROV_NONE, PROV_DRAFT):
                ci.provenance = PROV_CROSS
                ci.source_entry = entry_id

        # 3) in race a settled word is public: crossing letters become free
        #    'cross' knowledge for every other team (so they can never score).
        if self.mode == "race":
            for other in self.teams.values():
                if other.team == t.team:
                    continue
                for cell in e.cells:
                    oi = other.cell(cell)
                    if oi.provenance in (PROV_NONE, PROV_DRAFT):
                        oi.provenance = PROV_CROSS
                        oi.source_entry = entry_id

        t.score += gained
        if gained:
            if entry_id not in t.solved_entries:
                t.solved_entries.append(entry_id)
        t.last_final_seq = max(t.last_final_seq, seq)
        return Outcome(accepted=True, reason="accepted", points=gained,
                       newly_scored=newly)

    # -- views -------------------------------------------------------------
    def _tiebreak_key(self, row: Dict[str, Any]):
        """Build a sort key from the scenario's disclosed tie-break order.

        Default order is: score desc, solved desc, hints asc, last_final_seq
        asc, team id asc.  The scenario may override ``tiebreak`` with any
        ordering of those field names (a leading "-" flips the direction).
        """
        spec = self.s.scenario.get("tiebreak") or [
            "score", "solved", "hints", "last_final_seq", "team_id"]
        key: List[Any] = []
        for field_name in spec:
            direction = 1
            name = field_name
            if field_name.startswith("-"):
                direction = -1
                name = field_name[1:]
            elif field_name.startswith("+"):
                name = field_name[1:]
            if name == "team_id":
                value = row["team"]
                # ascending by team id by default; "-team_id" reverses
                key.append(value if direction == 1 else _Reverse(value))
            elif name == "score":
                # higher score first unless explicitly flipped
                key.append(-row["score"] if direction == 1 else row["score"])
            elif name == "solved":
                key.append(-row["solved"] if direction == 1 else row["solved"])
            elif name == "hints":
                # fewer hints ranks higher -> ascending
                key.append(row["hints"] if direction == 1 else -row["hints"])
            elif name == "last_final_seq":
                key.append(row["last_final_seq"] if direction == 1
                           else -row["last_final_seq"])
            else:
                key.append(0)
        key.append(row["team"])  # final stable fallback
        return tuple(key)

    def ranking(self) -> List[Dict[str, Any]]:
        rows = []
        for t in self.teams.values():
            rows.append({"team": t.team, "score": t.score,
                         "solved": len(t.solved_entries),
                         "hints": len(t.hints),
                         "last_final_seq": t.last_final_seq})
        rows.sort(key=self._tiebreak_key)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows

    def scoreboard(self, viewer: Optional[str] = None) -> Dict[str, Any]:
        board: Dict[str, Any] = {
            "mode": self.mode,
            "cell_owner": dict(self.cell_owner),
            "conflicts": list(self.conflicts),
        }
        conceal = (self.mode == "sealed" and viewer is not None
                   and viewer in self.teams and not self.teams[viewer].revealed)
        if conceal:
            mine = self.teams[viewer]
            board["viewer"] = viewer
            board["teams"] = {viewer: mine.to_dict()}
            board["totals"] = {viewer: mine.score}
            board["ranking"] = [r for r in self.ranking() if r["team"] == viewer]
            board["concealed"] = True
            return board
        board["teams"] = {n: t.to_dict() for n, t in self.teams.items()}
        board["totals"] = {n: t.score for n, t in self.teams.items()}
        board["ranking"] = self.ranking()
        board["concealed"] = False
        return board


# --------------------------------------------------------------------------
# convenience folds
# --------------------------------------------------------------------------

def score_events(session: Session, events: List[Dict[str, Any]],
                 viewer: Optional[str] = None) -> Dict[str, Any]:
    return Scorer(session, events).scoreboard(viewer=viewer)


def score_revision(session: Session, revision: Optional[str] = None,
                   viewer: Optional[str] = None) -> Dict[str, Any]:
    return score_events(session, session.events_upto(revision), viewer=viewer)


def score_diff(session: Session, old: str, new: str) -> Dict[str, Any]:
    """Judge trace: per-team and per-cell difference between two revisions."""
    a = Scorer(session, session.events_upto(old)).scoreboard()
    b = Scorer(session, session.events_upto(new)).scoreboard()
    delta: Dict[str, Any] = {}
    for tm in sorted(set(a["totals"]) | set(b["totals"])):
        ta, tb = a["totals"].get(tm, 0), b["totals"].get(tm, 0)
        ca = a["teams"].get(tm, {}).get("cells", {})
        cb = b["teams"].get(tm, {}).get("cells", {})
        cells = {}
        for k in sorted(set(ca) | set(cb)):
            ea, eb = ca.get(k, {}).get("earned", 0), cb.get(k, {}).get("earned", 0)
            pa = ca.get(k, {}).get("provenance", "none")
            pb = cb.get(k, {}).get("provenance", "none")
            if (ea, pa) != (eb, pb):
                cells[k] = {"from": {"earned": ea, "provenance": pa},
                            "to": {"earned": eb, "provenance": pb}}
        delta[tm] = {"before": ta, "after": tb, "change": tb - ta,
                     "cells": cells}
    return {"from": old, "to": new, "teams": delta}
