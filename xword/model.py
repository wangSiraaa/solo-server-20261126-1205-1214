"""Core domain model for the crossword-arena service.

A ``Scenario`` is one tiny board (1..4 entries).  A ``Session`` pairs a
scenario with an append-only event log plus revision snapshots.  Authoritative
state is *only* the log; everything else is a deterministic fold, which is what
makes replay, restart and judge re-adjudication exact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------
# canonical helpers
# --------------------------------------------------------------------------

def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


class XwordError(Exception):
    """Illegal user action (mapped to HTTP 4xx by the server)."""


# --------------------------------------------------------------------------
# scenario description
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    id: str
    answer: str
    direction: str                 # "H" or "V"
    clue: str
    cells: Tuple[Tuple[int, int], ...]

    def __post_init__(self) -> None:
        if self.direction not in ("H", "V"):
            raise XwordError(f"entry {self.id}: direction must be H or V")
        if self.answer != self.answer.upper():
            raise XwordError(f"entry {self.id}: answer must be upper-case")
        if len(self.answer) != len(self.cells):
            raise XwordError(f"entry {self.id}: answer/cells length mismatch")
        if len(self.cells) != len(set(self.cells)):
            raise XwordError(f"entry {self.id}: repeated cell")

    @property
    def endpoints(self) -> Tuple[Tuple[int, int], ...]:
        if len(self.cells) == 1:
            return (self.cells[0],)
        return (self.cells[0], self.cells[-1])

    def letter_at(self, cell: Tuple[int, int]) -> str:
        return self.answer[self.cells.index(cell)]

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "answer": self.answer,
                "direction": self.direction, "clue": self.clue,
                "cells": [list(c) for c in self.cells]}


# --------------------------------------------------------------------------
# scenario / session
# --------------------------------------------------------------------------

class Session:
    def __init__(self, scenario: Dict[str, Any]) -> None:
        self.scenario = normalise_scenario(scenario)
        self.scenario_id = self.scenario["id"]
        self.log: List[Dict[str, Any]] = []
        self.cuts: Dict[str, int] = {"r0": 0}
        self.current_revision = "r0"
        self._seq = 0

    # -- logging -----------------------------------------------------------
    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def append(self, event: Dict[str, Any]) -> int:
        if "seq" not in event:
            event["seq"] = self.next_seq()
        else:
            # an explicit seq (replayed "original number") is kept verbatim;
            # _seq only ratchets forward so future fresh events stay ordered
            self._seq = max(self._seq, int(event["seq"]))
        self.log.append(event)
        return int(event["seq"])

    def make_revision(self, label: str) -> str:
        label = str(label)
        if label in self.cuts:
            raise XwordError(f"revision {label} already exists")
        self.cuts[label] = len(self.log)
        self.current_revision = label
        return label

    def events_upto(self, revision: Optional[str]) -> List[Dict[str, Any]]:
        if revision in (None, "head", "current"):
            return list(self.log)
        if revision not in self.cuts:
            raise XwordError(f"unknown revision {revision}")
        return list(self.log[: self.cuts[revision]])

    # -- lookups -----------------------------------------------------------
    def entries(self) -> Dict[str, Entry]:
        out: Dict[str, Entry] = {}
        for e in self.scenario["entries"]:
            entry = Entry(id=e["id"], answer=e["answer"],
                          direction=e["direction"], clue=e["clue"],
                          cells=tuple(tuple(c) for c in e["cells"]))
            out[entry.id] = entry
        return out

    def crossings(self) -> Dict[str, List[str]]:
        ents = self.entries()
        by_cell: Dict[Tuple[int, int], List[str]] = {}
        for eid, e in ents.items():
            for c in e.cells:
                by_cell.setdefault(c, []).append(eid)
        result: Dict[str, List[str]] = {}
        for eid, e in ents.items():
            seen: List[str] = []
            for c in e.cells:
                for other in by_cell[c]:
                    if other != eid and other not in seen:
                        seen.append(other)
            result[eid] = seen
        return result

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {"scenario": self.scenario, "log": list(self.log),
                "cuts": dict(self.cuts),
                "current_revision": self.current_revision,
                "seq": self._seq}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        s = cls(data["scenario"])
        s.log = list(data.get("log", []))
        s.cuts = dict(data.get("cuts", {"r0": 0}))
        s.current_revision = data.get("current_revision", "r0")
        s._seq = int(data.get("seq", 0))
        return s


def normalise_scenario(scenario: Dict[str, Any]) -> Dict[str, Any]:
    s = json.loads(json.dumps(scenario))
    s.setdefault("id", digest(s)[:12])
    s.setdefault("name", s["id"])
    s.setdefault("mode", "race")
    if s["mode"] not in ("race", "sealed"):
        raise XwordError("mode must be 'race' or 'sealed'")
    s.setdefault("hint_scope", "public")
    if s["hint_scope"] not in ("public", "team"):
        raise XwordError("hint_scope must be 'public' or 'team'")
    s.setdefault("deadline", None)
    s.setdefault("disclosure", "conceal" if s["mode"] == "sealed" else "open")
    entries = s.get("entries", [])
    if not (1 <= len(entries) <= 4):
        raise XwordError("scenario must contain 1..4 entries")
    ids = [e["id"] for e in entries]
    if len(set(ids)) != len(ids):
        raise XwordError("duplicate entry id")

    # crossing consistency: any two entries sharing a cell must agree on the
    # letter there, otherwise the board is unsolvable
    cell_letter: Dict[Tuple[int, int], Tuple[str, str]] = {}
    for e in entries:
        for i, c in enumerate(e["cells"]):
            cell = (c[0], c[1])
            letter = e["answer"][i]
            if cell in cell_letter and cell_letter[cell][0] != letter:
                raise XwordError(
                    f"crossing conflict at {cell}: "
                    f"{cell_letter[cell][1]} wants {cell_letter[cell][0]}, "
                    f"{e['id']} wants {letter}")
            cell_letter[cell] = (letter, e["id"])

    table = dict(s.get("score_table", {}))
    for e in entries:
        table.setdefault(e["id"], 1)
    s["score_table"] = {k: int(v) for k, v in table.items()}

    # independent cells = deduplicated endpoints; interior crossing letters can
    # never score on their own.
    endpoints: List[List[int]] = []
    for e in entries:
        cells = e["cells"]
        ends = [cells[0]] if len(cells) == 1 else [cells[0], cells[-1]]
        for c in ends:
            if list(c) not in endpoints:
                endpoints.append(list(c))
    s["independent_cells"] = sorted(endpoints)

    # per-cell point table: explicit cell_points wins; else endpoint inherits
    # its entry's score_table value; afterwards any explicit entry re-applied.
    explicit = {str(k): int(v) for k, v in dict(s.get("cell_points", {})).items()}
    resolved: Dict[str, int] = {}
    for e in entries:
        cells = e["cells"]
        ends = [cells[0]] if len(cells) == 1 else [cells[0], cells[-1]]
        for c in ends:
            resolved[f"{c[0]},{c[1]}"] = int(table[e["id"]])
    resolved.update(explicit)
    s["cell_points"] = resolved
    return s
