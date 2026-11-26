"""Core domain logic for the community-bookstore serialised reading service.

The service models an author's short story as a *work* published in *turns*
(editions/versions).  A turn has *segments* that reference *facts* (for example
"the key is under the bridge").  A reader may enter a later segment only if
they have genuinely read the fact that segment requires, on the edition in
force for their session.

Key design decisions (each maps to a requirement in the brief):

* Reading is recorded per ``(reader, fact, turn)``.  A fact read on one turn
  does *not* silently satisfy the same fact on another turn; an editor must
  carry it forward when publishing a revision.
* Carry is **segment-scoped**, matching "indicate for old segments which facts
  still hold in the new version".  If a disclosure moves to a later segment,
  readers who only read the old location do not keep the fact.
* Facts are matched by content id, never by segment title or end page.
* The same fact may be disclosed by two branches (甲/乙); either path satisfies
  a later requirement.
* A revision may move a reveal one segment later; readers who read the old
  location lose access until they read the new one.
* A reader who tapped into a segment but never finished it has no fact; removing
  that bogus record narrows access.
* Structural cross references (``describes``) are metadata only and never
  manufacture a fact, so mutual/cyclic references cannot conjure a reveal.
* Menus for a *served* session are snapshotted at serve time against one locked
  turn; late joiners to an *open* session only receive segments that also
  cover everything already-served readers received.
* A staff member cannot serve two sessions with overlapping physical time;
  unchanged reader facts are irrelevant to that constraint.

Persistence is a single JSON snapshot so the demo and tests can restart the
process and resume exactly where they left off.
"""

from __future__ import annotations

import itertools
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


class BookClubError(Exception):
    """Client-facing validation failure; ``status`` maps to an HTTP status."""

    def __init__(self, code: str, message: str, *, status: int = 400,
                 extra: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}

    def as_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Fact:
    id: str
    description: str


@dataclass
class Segment:
    id: str
    title: str
    text: str = ""
    # Facts the reader learns by reading this segment.
    discloses: List[str] = field(default_factory=list)
    # Facts the reader must already hold to legitimately enter this segment.
    requires: List[str] = field(default_factory=list)
    # Structural "alludes to ..." references.  Never a source of facts.
    describes: List[str] = field(default_factory=list)


@dataclass
class Turn:
    id: str
    label: str
    segments: Dict[str, Segment]
    published_at: float
    revision_of: Optional[str] = None
    # Old segment ids whose disclosures still hold verbatim in this revision.
    unchanged_segments: List[str] = field(default_factory=list)
    # Facts carried regardless of which segment disclosed them.
    carry_facts: List[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Session:
    id: str
    title: str
    kind: str  # "served" | "open"
    work: str
    turn: Optional[str] = None  # pinned/locked edition; None means live current
    status: str = "open"        # open | served | locked | closed
    # Segments this co-reading intends to reach together (e.g. the finale
    # that needs the shared fact).  These define the convergence point.
    goals: List[str] = field(default_factory=list)
    slots: List[dict] = field(default_factory=list)
    served_by: Optional[str] = None
    joined: List[str] = field(default_factory=list)
    ready: Dict[str, List[str]] = field(default_factory=dict)
    served_at: Optional[float] = None
    locked_at: Optional[float] = None
    line: Optional[dict] = None


@dataclass
class Practice:
    """A make-up read (补读): a short re-read that supplies one missing fact."""

    id: str
    reader: str
    session: str
    work: str
    turn: str
    segment: str
    context_session: Optional[str] = None
    created_at: float = 0.0
    completed_at: Optional[float] = None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    def __init__(self, path: Optional[str]):
        self.path = path
        self._lock = threading.RLock()
        self.facts: Dict[str, Fact] = {}
        self.works: Dict[str, dict] = {}
        self.turns: Dict[str, Turn] = {}
        self.sessions: Dict[str, Session] = {}
        self.staff: Dict[str, dict] = {}
        self.practices: Dict[str, Practice] = {}
        self.requests: Dict[Tuple[str, str, str], dict] = {}
        # reader -> turn -> fact -> list of segment ids that actually disclosed it
        self.reads: Dict[str, Dict[str, Dict[str, List[str]]]] = {}
        self._seq = itertools.count(1)
        if path and os.path.exists(path):
            self._load()
        self._seq = itertools.count(self._next_peak())

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for f in raw.get("facts", []):
            self.facts[f["id"]] = Fact(**f)
        self.works = raw.get("works", {})
        for t in raw.get("turns", []):
            segs = {s["id"]: Segment(**s) for s in t["segments"]}
            self.turns[t["id"]] = Turn(
                id=t["id"], label=t["label"], segments=segs,
                published_at=t.get("published_at", 0.0),
                revision_of=t.get("revision_of"),
                unchanged_segments=t.get("unchanged_segments", []),
                carry_facts=t.get("carry_facts", []),
                note=t.get("note", ""),
            )
        for s in raw.get("sessions", []):
            self.sessions[s["id"]] = Session(**s)
        for st in raw.get("staff", []):
            self.staff[st["id"]] = st
        for p in raw.get("practices", []):
            self.practices[p["id"]] = Practice(**p)
        self.requests = {
            tuple(json.loads(k)): v for k, v in raw.get("requests", {}).items()
        }
        self.reads = raw.get("reads", {})

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            raw = {
                "facts": [asdict(f) for f in self.facts.values()],
                "works": self.works,
                "turns": [
                    {
                        "id": t.id, "label": t.label,
                        "segments": [asdict(s) for s in t.segments.values()],
                        "published_at": t.published_at,
                        "revision_of": t.revision_of,
                        "unchanged_segments": t.unchanged_segments,
                        "carry_facts": t.carry_facts,
                        "note": t.note,
                    }
                    for t in self.turns.values()
                ],
                "sessions": [asdict(s) for s in self.sessions.values()],
                "staff": list(self.staff.values()),
                "practices": [asdict(p) for p in self.practices.values()],
                "requests": {json.dumps(list(k)): v for k, v in self.requests.items()},
                "reads": self.reads,
            }
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            payload = json.dumps(raw, ensure_ascii=False, indent=1)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bookclub-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                self._atomic_replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

    @staticmethod
    def _atomic_replace(tmp: str, dest: str) -> None:
        """``os.replace`` with retries and an in-place fallback.

        Some network/overlay filesystems (e.g. virtiofs) intermittently reject
        rename with ``PermissionError``.  Writes happen under the store lock, so
        falling back to an in-place overwrite is still consistent for this
        single-writer service.
        """
        last: Optional[Exception] = None
        for attempt in range(6):
            try:
                os.replace(tmp, dest)
                return
            except PermissionError as exc:  # transient on virtiofs/overlay
                last = exc
                time.sleep(0.01 * (attempt + 1))
        # Last resort: overwrite in place.
        try:
            with open(tmp, "r", encoding="utf-8") as src:
                data = src.read()
            with open(dest, "w", encoding="utf-8") as out:
                out.write(data)
        except OSError:
            if last is not None:
                raise last
            raise

    def _next_peak(self) -> int:
        peak = 1
        for coll in (self.facts, self.turns, self.sessions, self.works,
                     self.practices, self.staff):
            for key in coll:
                parts = str(key).rsplit("-", 1)
                if len(parts) == 2 and parts[1].isdigit():
                    peak = max(peak, int(parts[1]) + 1)
        return peak

    def _id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq):04d}"

    # -- effective fact resolution ----------------------------------------

    def _direct_sources(self, reader: str, fact: str, turn: str) -> List[str]:
        return self.reads.get(reader, {}).get(turn, {}).get(fact, [])

    def sources(self, reader: str, fact: str, turn: str,
                _seen: Optional[set] = None) -> List[str]:
        """Return provenance ``"turn:segment"`` strings for an effective fact.

        Directly-recorded readings win.  Otherwise, if ``turn`` is a revision,
        a fact carries when either (a) its original segment is declared
        unchanged, or (b) the fact is in ``carry_facts``.  Segment-scoped carry
        is what makes "move the reveal to the next segment" invalidate only the
        readers who read the old location.
        """
        _seen = _seen or set()
        key = (reader, fact, turn)
        if key in _seen:
            return []
        _seen.add(key)
        direct = self._direct_sources(reader, fact, turn)
        if direct:
            return [f"{turn}:{seg}" for seg in direct]
        cur = self.turns.get(turn)
        if not cur or not cur.revision_of:
            return []
        prev = cur.revision_of
        prev_sources = self.sources(reader, fact, prev, _seen)
        if not prev_sources:
            return []
        result: List[str] = []
        old = self.turns.get(prev)
        for origin in prev_sources:
            seg = origin.rsplit(":", 1)[-1]
            # The old segment must still exist and still disclose the fact in the
            # new edition, and the editor must have declared it unchanged.
            if (old and seg in old.segments
                    and fact in old.segments[seg].discloses
                    and seg in cur.unchanged_segments
                    and seg in cur.segments
                    and fact in cur.segments[seg].discloses):
                result.append(origin)
        if fact in cur.carry_facts:
            for origin in prev_sources:
                if origin not in result:
                    result.append(origin)
        return result

    def has_fact(self, reader: str, fact: str, turn: str) -> bool:
        return bool(self.sources(reader, fact, turn))

    def fact_sources(self, reader: str, fact: str, turn: str) -> List[str]:
        return sorted(self.sources(reader, fact, turn))

    def _record_fact(self, reader: str, fact: str, turn: str, segment: str) -> bool:
        self.reads.setdefault(reader, {})
        by_turn = self.reads[reader].setdefault(turn, {})
        new = fact not in by_turn
        srcs = by_turn.setdefault(fact, [])
        if segment not in srcs:
            srcs.append(segment)
        return new

    # -- authoring ---------------------------------------------------------

    def define_fact(self, fact_id: str, description: str) -> Fact:
        with self._lock:
            if fact_id in self.facts:
                self.facts[fact_id].description = description
            else:
                self.facts[fact_id] = Fact(id=fact_id, description=description)
            self.save()
            return self.facts[fact_id]

    def define_work(self, work_id: str, title: str) -> dict:
        with self._lock:
            if work_id in self.works:
                self.works[work_id]["title"] = title
            else:
                self.works[work_id] = {"id": work_id, "title": title,
                                       "turns": [], "current": None}
            self.save()
            return self.works[work_id]

    def _validate_segments(self, segments: List[dict]) -> Dict[str, Segment]:
        out: Dict[str, Segment] = {}
        for raw in segments:
            if "id" not in raw:
                raise BookClubError("bad_segment", "every segment needs an id")
            refs = list(raw.get("discloses", [])) + list(raw.get("requires", []))
            missing = [f for f in refs if f not in self.facts]
            if missing:
                raise BookClubError(
                    "unknown_fact",
                    f"segment {raw['id']!r} references undefined facts {missing}",
                )
            out[raw["id"]] = Segment(
                id=raw["id"],
                title=raw.get("title", raw["id"]),
                text=raw.get("text", ""),
                discloses=list(dict.fromkeys(raw.get("discloses", []))),
                requires=list(dict.fromkeys(raw.get("requires", []))),
                describes=list(dict.fromkeys(raw.get("describes", []))),
            )
        return out

    def publish_turn(self, work_id: str, label: str, segments: List[dict], *,
                     revision_of: Optional[str] = None,
                     unchanged_segments: Optional[Iterable[str]] = None,
                     carry_facts: Optional[Iterable[str]] = None,
                     note: str = "") -> Turn:
        """Publish a new edition.

        ``unchanged_segments`` names old segments whose disclosures still hold;
        ``carry_facts`` names facts that hold regardless of source.  Both are
        only meaningful with ``revision_of``.
        """
        with self._lock:
            if work_id not in self.works:
                raise BookClubError("unknown_work", f"work {work_id!r} does not exist")
            if revision_of and revision_of not in self.turns:
                raise BookClubError("unknown_turn", f"turn {revision_of!r} does not exist")
            unchanged = list(dict.fromkeys(unchanged_segments or []))
            carry = list(dict.fromkeys(carry_facts or []))
            if (unchanged or carry) and not revision_of:
                raise BookClubError("bad_carry", "carry requires revision_of")
            if revision_of:
                old = self.turns[revision_of]
                unknown = [s for s in unchanged if s not in old.segments]
                if unknown:
                    raise BookClubError("unknown_segment", f"unchanged segments not in {revision_of}: {unknown}")
            unknown_facts = [f for f in carry if f not in self.facts]
            if unknown_facts:
                raise BookClubError("unknown_fact", f"carry references undefined facts {unknown_facts}")
            segs = self._validate_segments(segments)
            turn = Turn(
                id=self._id("turn"), label=label, segments=segs,
                published_at=time.time(), revision_of=revision_of,
                unchanged_segments=unchanged, carry_facts=carry, note=note,
            )
            self.turns[turn.id] = turn
            self.works[work_id]["turns"].append(turn.id)
            self.works[work_id]["current"] = turn.id
            self.save()
            return turn

    # -- helpers -----------------------------------------------------------

    def _session(self, session_id: str) -> Session:
        sess = self.sessions.get(session_id)
        if not sess:
            raise BookClubError("unknown_session", f"session {session_id!r} does not exist", status=404)
        return sess

    def _turn(self, turn_id: str) -> Turn:
        turn = self.turns.get(turn_id)
        if not turn:
            raise BookClubError("unknown_turn", f"turn {turn_id!r} does not exist", status=404)
        return turn

    def _turn_for_session(self, sess: Session) -> str:
        if sess.turn and sess.turn in self.turns:
            return sess.turn
        cur = self.works[sess.work]["current"]
        if cur is None:
            raise BookClubError("no_turn", "work has no published turn")
        return cur

    # -- sessions: registration -------------------------------------------

    def register_session(self, title: str, work_id: str, *, kind: str = "served",
                         turn: Optional[str] = None,
                         goals: Optional[Iterable[str]] = None,
                         capacity: Optional[int] = None) -> Session:
        with self._lock:
            if work_id not in self.works:
                raise BookClubError("unknown_work", f"work {work_id!r} does not exist")
            if kind not in ("served", "open"):
                raise BookClubError("bad_kind", "kind must be 'served' or 'open'")
            if turn is not None and turn not in self.works[work_id]["turns"]:
                raise BookClubError("unknown_turn", "turn does not belong to this work")
            goals = list(dict.fromkeys(goals or []))
            if goals:
                edition = self.turns[turn] if turn else self.turns[self.works[work_id]["current"]]
                unknown = [g for g in goals if g not in edition.segments]
                if unknown:
                    raise BookClubError("unknown_segment", f"goal segments not in edition: {unknown}")
            sess = Session(id=self._id("sess"), title=title, kind=kind,
                           work=work_id, turn=turn, goals=goals,
                           slots=[{"staff": None, "start": None, "end": None}])
            self.sessions[sess.id] = sess
            self.save()
            return sess

    def register_reader(self, session_id: str, reader: str) -> dict:
        with self._lock:
            sess = self._session(session_id)
            if sess.status == "closed":
                raise BookClubError("session_closed", "session is closed")
            if reader not in sess.joined:
                sess.joined.append(reader)
            self.reads.setdefault(reader, {})
            self.save()
            return {"session": sess.id, "reader": reader, "joined": sess.joined}

    def add_staff(self, name: str) -> dict:
        with self._lock:
            sid = self._id("staff")
            self.staff[sid] = {"id": sid, "name": name}
            self.save()
            return self.staff[sid]

    def schedule_staff(self, session_id: str, staff_id: str, start: float, end: float,
                       *, slot: int = 0) -> dict:
        """Assign a staff member to a window; reject overlapping assignments."""
        with self._lock:
            sess = self._session(session_id)
            if staff_id not in self.staff:
                raise BookClubError("unknown_staff", f"staff {staff_id!r} does not exist")
            if end <= start:
                raise BookClubError("bad_time", "end must be after start")
            conflict = self._staff_conflict(staff_id, start, end,
                                            ignore_session=sess.id, ignore_slot=slot)
            if conflict:
                raise BookClubError(
                    "staff_double_booked",
                    f"staff {staff_id!r} already serves {conflict['session']} in an "
                    "overlapping window; unchanged reader facts do not free the slot",
                    status=409,
                    extra={"conflict": conflict, "recommendation": "rebook"},
                )
            while len(sess.slots) <= slot:
                sess.slots.append({"staff": None, "start": None, "end": None})
            sess.slots[slot] = {"staff": staff_id, "start": start, "end": end}
            self.save()
            return {"session": sess.id, "slot": slot, "slots": sess.slots}

    def _staff_conflict(self, staff_id: str, start: float, end: float, *,
                        ignore_session: Optional[str] = None,
                        ignore_slot: Optional[int] = None) -> Optional[dict]:
        for sid, sess in self.sessions.items():
            for idx, slot in enumerate(sess.slots):
                if slot.get("staff") != staff_id or slot.get("start") is None:
                    continue
                if sid == ignore_session and idx == ignore_slot:
                    continue
                if _overlap(start, end, slot["start"], slot["end"]):
                    return {"session": sid, "slot": idx,
                            "start": slot["start"], "end": slot["end"]}
        return None

    # -- reading -----------------------------------------------------------

    def read_segment(self, reader: str, work_id: str, turn_id: str,
                     segment_id: str) -> dict:
        """Reader actually finishes a segment; its facts are recorded.

        Requirements are enforced: the reader cannot enter a segment without the
        facts it requires on this turn.  ``describes`` grants nothing.
        """
        with self._lock:
            turn = self._turn(turn_id)
            if work_id not in self.works or turn_id not in self.works[work_id]["turns"]:
                raise BookClubError("unknown_work_turn", "turn does not belong to work")
            seg = turn.segments.get(segment_id)
            if not seg:
                raise BookClubError("unknown_segment", f"segment {segment_id!r} not in turn")
            missing = [f for f in seg.requires if not self.has_fact(reader, f, turn_id)]
            if missing:
                raise BookClubError(
                    "missing_requirement",
                    f"cannot enter {segment_id!r}; missing facts on {turn_id}: {missing}",
                    status=409,
                    extra={"missing": missing, "turn": turn_id,
                           "remediation": "read a segment that discloses the fact first"},
                )
            gained = [f for f in seg.discloses
                      if self._record_fact(reader, f, turn_id, segment_id)]
            self.save()
            return {
                "reader": reader, "turn": turn_id, "segment": segment_id,
                "gained_facts": gained,
                "now_has": {f: self.has_fact(reader, f, turn_id) for f in seg.discloses},
            }

    def revoke_read(self, reader: str, fact: str, turn_id: str,
                    *, segment: Optional[str] = None) -> dict:
        """Remove a reading record (e.g. a mis-tap that was never really read)."""
        with self._lock:
            by_turn = self.reads.get(reader, {}).get(turn_id, {})
            if fact not in by_turn:
                raise BookClubError("no_record", f"{reader!r} has no {fact!r} record on {turn_id}")
            if segment is not None:
                if segment not in by_turn[fact]:
                    raise BookClubError("no_record", f"{reader!r} did not gain {fact!r} from {segment!r}")
                by_turn[fact].remove(segment)
                removed = [segment]
                if not by_turn[fact]:
                    del by_turn[fact]
            else:
                removed = list(by_turn.pop(fact))
            self.save()
            return {
                "reader": reader, "turn": turn_id, "fact": fact,
                "removed_from": removed,
                "now_has_fact": self.has_fact(reader, fact, turn_id),
            }

    def can_enter(self, reader: str, turn_id: str, segment_id: str) -> dict:
        """Whether ``reader`` may enter ``segment_id`` on ``turn_id`` right now."""
        with self._lock:
            turn = self._turn(turn_id)
            if segment_id not in turn.segments:
                raise BookClubError("unknown_segment", f"segment {segment_id!r} not in turn")
            seg = turn.segments[segment_id]
            missing = [f for f in seg.requires if not self.has_fact(reader, f, turn_id)]
            return {
                "reader": reader, "turn": turn_id, "segment": segment_id,
                "entry_requirements": seg.requires,
                "missing": missing,
                "allowed": not missing,
                "discloses": seg.discloses,
                "describes": seg.describes,
            }

    # -- remediation analysis (补读 vs 改约) --------------------------------

    def assess(self, session_id: str, reader: str, fact: str,
               turn: Optional[str] = None) -> dict:
        """Decide how to make ``reader`` eligible, comparing from intake.

        ``turn`` overrides the edition being compared against (used to compare
        "the same intake point, only the version given to readers changed").

        Returns status:
        * ``has``      -- fact already held on this edition; no action.
        * ``catch_up`` -- a segment on this edition discloses the fact and can be
                          read now; offer a make-up read (补读).
        * ``rebook``   -- no segment on this edition discloses the fact; a
                          different session/edition is required (改约).
        """
        with self._lock:
            sess = self._session(session_id)
            if reader not in sess.joined:
                raise BookClubError("not_joined", f"{reader!r} is not registered for this session")
            turn_id = turn or self._turn_for_session(sess)
            self._turn(turn_id)
            if self.has_fact(reader, fact, turn_id):
                return self._assess(reader, fact, turn_id, "has", [], session_id)
            candidates = self._disclosing_segments(fact, turn_id)
            if not candidates:
                return self._assess(reader, fact, turn_id, "rebook", [], session_id,
                                    reason="no segment on this edition discloses the fact")
            essentials = [c for c in candidates
                          if all(self.has_fact(reader, r, turn_id)
                                 for r in self.turns[turn_id].segments[c].requires)]
            # Prefer a branch-specific candidate (one that has prerequisites the
            # reader already meets) so the make-up read extends the branch they
            # actually read, rather than switching to a generic/other branch.
            essentials.sort(
                key=lambda c: (not self.turns[turn_id].segments[c].requires, c),
            )
            chosen = (essentials or candidates)[0]
            prereqs = list(self.turns[turn_id].segments[chosen].requires)
            return self._assess(reader, fact, turn_id, "catch_up", candidates, session_id,
                                chosen_segment=chosen, prerequisites=prereqs,
                                prerequisites_met=bool(essentials))

    def _assess(self, reader, fact, turn_id, status, candidates, session_id, **kw):
        return {"session": session_id, "reader": reader, "fact": fact,
                "turn": turn_id, "status": status,
                "candidate_segments": candidates, **kw}

    def _disclosing_segments(self, fact: str, turn_id: str) -> List[str]:
        return [s.id for s in self.turns[turn_id].segments.values() if fact in s.discloses]

    # -- goals -------------------------------------------------------------

    def _terminal_goals(self, turn: Turn) -> List[str]:
        """Default convergence segments: they need facts but feed nothing else.

        For a story this is the set of ending segments; different endings can
        share the same prerequisites (the shared fact).
        """
        required = {f for s in turn.segments.values() for f in s.requires}
        goals = [s.id for s in turn.segments.values()
                 if s.requires and s.discloses and not (set(s.discloses) & required)]
        if not goals:
            cand = [s.id for s in turn.segments.values() if s.discloses]
            goals = cand[-1:] if cand else []
        return goals

    def _goal_fact_closure(self, turn: Turn, goals: List[str]) -> set:
        """Facts that lie on *some* dependency chain toward a goal (OR-union)."""
        needed: set = set()
        worklist = [f for g in goals if g in turn.segments
                    for f in turn.segments[g].requires]
        while worklist:
            fact = worklist.pop()
            if fact in needed:
                continue
            needed.add(fact)
            for seg in turn.segments.values():
                if fact in seg.discloses:
                    worklist.extend(r for r in seg.requires if r not in needed)
        return needed

    def _goals_for(self, sess: Session, turn: Turn) -> List[str]:
        return sess.goals or self._terminal_goals(turn)

    # -- menus / serving / locking -----------------------------------------

    def _menu(self, sess: Session, reader: str, turn_id: str) -> List[str]:
        """Goal-directed chapters this reader may read now.

        Includes:

        * convergence goals the reader can already enter (the target reading);
        * enterable segments that supply a fact still needed to reach a goal the
          reader cannot enter yet.

        Segments that only add facts for already-reachable goals are excluded, so
        a reader who already holds the shared fact is not offered redundant
        branches.  When two branches can both reach a missing goal fact, both are
        offered -- that is the "multiple viable co-reading paths" case.
        """
        turn = self.turns[turn_id]
        goals = self._goals_for(sess, turn)
        enterable_goals = [g for g in goals if g in turn.segments
                           and all(self.has_fact(reader, r, turn_id)
                                   for r in turn.segments[g].requires)]
        blocked_goals = [g for g in goals if g in turn.segments and g not in enterable_goals]
        needed = self._goal_fact_closure(turn, blocked_goals)
        ready: List[str] = []
        for seg in turn.segments.values():
            if seg.id in enterable_goals:
                ready.append(seg.id)
                continue
            if not seg.discloses or seg.id in goals:
                continue
            if not all(self.has_fact(reader, r, turn_id) for r in seg.requires):
                continue
            adds = [f for f in seg.discloses if not self.has_fact(reader, f, turn_id)]
            if adds and any(f in needed for f in adds):
                ready.append(seg.id)
        return ready

    def serve(self, session_id: str, staff_id: Optional[str] = None, *,
              start: Optional[float] = None, end: Optional[float] = None) -> dict:
        """Serve a session and snapshot menus against one locked edition.

        For ``served`` sessions this is the pre-session prep: every enrolled
        reader is snapshotted and the leader's continuous route is computed.  For
        ``open`` sessions this claims the staff slot and fixes the edition so
        latecomers are assessed on the same version.
        """
        with self._lock:
            sess = self._session(session_id)
            if sess.kind == "served" and sess.status in ("served", "locked"):
                raise BookClubError("already_served", "served session is already prepared")
            if sess.kind == "open" and sess.status != "open":
                raise BookClubError("already_served", "open session is already being served")
            if staff_id is not None and staff_id not in self.staff:
                raise BookClubError("unknown_staff", f"staff {staff_id!r} does not exist")
            turn_id = self._turn_for_session(sess)
            if not sess.joined:
                raise BookClubError("empty_session", "no readers are registered yet")
            if staff_id is not None and start is not None and end is not None:
                if end <= start:
                    raise BookClubError("bad_time", "end must be after start")
                conflict = self._staff_conflict(staff_id, start, end,
                                                ignore_session=sess.id, ignore_slot=0)
                if conflict:
                    raise BookClubError(
                        "staff_double_booked",
                        f"staff {staff_id!r} is already serving {conflict['session']} "
                        "in an overlapping window; unchanged reader facts do not free it",
                        status=409,
                        extra={"conflict": conflict, "recommendation": "rebook"},
                    )
                sess.slots[0] = {"staff": staff_id, "start": start, "end": end}
            sess.turn = turn_id
            sess.served_by = staff_id or sess.served_by
            sess.status = "served"
            sess.served_at = time.time()
            goals = self._goals_for(sess, self.turns[turn_id])
            sess.ready = {r: self._menu(sess, r, turn_id) for r in sess.joined}
            line = {
                "turn": turn_id,
                "label": self.turns[turn_id].label,
                "goals": goals,
                "reader_menus": sess.ready,
                "prerequisites": {
                    r: {f: self.fact_sources(r, f, turn_id) for f in self.facts}
                    for r in sess.joined
                },
                "served_at": sess.served_at,
            }
            if sess.kind == "served":
                line["route"] = self.leader_route(sess, turn_id)
                line["locked"] = False
            else:
                # Open sessions still precompute the group's current plan so
                # latecomers can be located against a real meeting point.
                line["route"] = self.leader_route(sess, turn_id)
            sess.line = line
            self.save()
            return {"session": sess.id, "kind": sess.kind, "turn": turn_id,
                    "status": sess.status, "goals": goals,
                    "reader_menus": sess.ready, "line": line}

    def lock(self, session_id: str) -> dict:
        """Finalise a served session: freeze edition and leader route."""
        with self._lock:
            sess = self._session(session_id)
            if sess.kind != "served":
                raise BookClubError("not_served_kind", "only served sessions are locked")
            if sess.status == "locked":
                return {"session": sess.id, "status": sess.status, "line": sess.line}
            if sess.status != "served":
                raise BookClubError("not_served", "serve the session before locking it")
            sess.status = "locked"
            sess.locked_at = time.time()
            if sess.line is not None:
                sess.line["locked"] = True
            self.save()
            return {"session": sess.id, "status": sess.status, "line": sess.line}

    def leader_route(self, sess: Session, turn_id: str) -> dict:
        """Greedy continuous leader route that carries the whole group to a goal.

        Co-reading is one sequence the leader reads aloud.  A listener only
        absorbs a segment's facts when *they* meet its ``requires``; others still
        hear it but gain nothing.  So a branch only one reader is prepared for can
        still be on the route -- that reader advances while the rest wait, and the
        group converges on a goal once everyone is ready.

        Rule: repeatedly take an unused segment enterable by at least one
        resident that supplies a still-missing, goal-relevant fact to someone who
        can enter it.  Stop when every resident can enter a goal; then append that
        goal.  Residents who cannot be carried are reported for remediation.
        """
        turn = self.turns[turn_id]
        residents = list(sess.joined)
        goals = self._goals_for(sess, turn)
        goal_set = set(goals)
        closure = self._goal_fact_closure(turn, goals)
        coverage = {r: {f for f in self.facts if self.has_fact(r, f, turn_id)}
                    for r in residents}
        gained_by: Dict[str, List[str]] = {r: [] for r in residents}
        heard_by: Dict[str, List[str]] = {r: [] for r in residents}
        route: List[str] = []
        used: set = set()

        def missing() -> dict:
            return {r: (closure - coverage[r]) for r in residents}

        def all_can_reach_goal() -> bool:
            return all(any(set(turn.segments[g].requires) <= coverage[r]
                           for g in goals if g in turn.segments)
                       for r in residents)

        progressed = True
        while progressed and not all_can_reach_goal():
            progressed = False
            want = missing()
            for seg in turn.segments.values():
                if seg.id in used or seg.id in goal_set or not seg.discloses:
                    continue
                can_enter = [r for r in residents if set(seg.requires) <= coverage[r]]
                if not can_enter:
                    continue
                adds = [r for r in can_enter
                        if (set(seg.discloses) - coverage[r]) & want[r]]
                if not adds:
                    continue
                route.append(seg.id)
                used.add(seg.id)
                for r in residents:
                    heard_by[r].append(seg.id)
                    if r in can_enter:
                        for f in seg.discloses:
                            if f not in coverage[r]:
                                gained_by[r].append(f)
                        coverage[r] |= set(seg.discloses)
                progressed = True
                break

        # Append the convergence goal everyone can now enter.
        goal = None
        for g in goals:
            if g in turn.segments and all(
                    set(turn.segments[g].requires) <= coverage[r] for r in residents):
                goal = g
                break
        if goal:
            route.append(goal)
            used.add(goal)
            for r in residents:
                heard_by[r].append(goal)
                for f in turn.segments[goal].discloses:
                    if f not in coverage[r]:
                        gained_by[r].append(f)
                coverage[r] |= set(turn.segments[goal].discloses)

        remediation = []
        for r in residents:
            if any(set(turn.segments[g].requires) <= coverage[r]
                   for g in goals if g in turn.segments):
                continue
            reach = self._simulate_reach(r, turn_id, goals)
            remediation.append({
                "reader": r,
                "status": "catch_up" if reach["reachable_after"] else "rebook",
                "make_up_plan": reach["catch_up_plan"],
            })

        return {
            "turn": turn_id,
            "goals": goals,
            "segments": route,
            "gained_by": gained_by,
            "heard_by": heard_by,
            "coverage": {r: sorted(coverage[r]) for r in residents},
            "needs_remediation": remediation,
        }

    # -- open session joining ---------------------------------------------

    def join_open(self, session_id: str, reader: str, *,
                  hold_facts: Optional[Iterable[str]] = None) -> dict:
        """Join an open session mid-way.

        The ``meeting_point`` is the first segment of the group's locked route
        that the latecomer can *already* enter, i.e. where they can slot in
        without extra reading.  If none, ``make_up_plan`` lists the short
        catch-up reads needed first and ``needs_make_up`` is true.

        ``hold_facts`` credits facts the reader genuinely holds on this edition
        (e.g. from a completed make-up read); facts they do not actually hold are
        reported as unavailable rather than fabricated.
        """
        with self._lock:
            sess = self._session(session_id)
            if sess.kind != "open":
                raise BookClubError("not_open", "join_open is only for open sessions")
            turn_id = self._turn_for_session(sess)
            if reader not in sess.joined:
                sess.joined.append(reader)
            self.reads.setdefault(reader, {})
            credited, unavailable = [], []
            for fact in hold_facts or []:
                if fact not in self.facts:
                    raise BookClubError("unknown_fact", f"undefined fact {fact!r}")
                if self.has_fact(reader, fact, turn_id):
                    credited.append(fact)
                else:
                    unavailable.append(fact)
            turn = self.turns[turn_id]
            goals = self._goals_for(sess, turn)
            reach = self._simulate_reach(reader, turn_id, goals)
            route = (sess.line or {}).get("route", {}).get("segments", []) if sess.ready else []
            meeting = next((seg for seg in route
                            if seg in turn.segments
                            and set(turn.segments[seg].requires)
                            <= {f for f in self.facts if self.has_fact(reader, f, turn_id)}),
                           None)
            needs_make_up = bool(sess.ready) and not reach["reachable_now"]
            self.save()
            return {
                "session": sess.id, "reader": reader, "turn": turn_id,
                "goals": goals,
                "meeting_point": meeting,
                "route_segments": route,
                "reachable_goals_now": reach["reachable_now"],
                "make_up_plan": reach["catch_up_plan"],
                "needs_make_up": needs_make_up,
                "can_reach_goal_after_make_up": bool(reach["reachable_after"]),
                "credited_facts": credited,
                "unavailable_facts": unavailable,
            }

    def _simulate_reach(self, reader: str, turn_id: str,
                        goals: List[str]) -> dict:
        """Shortest set of segments the reader must still read to reach a goal.

        Uses breadth-first search over sets of held facts (the fact universe is
        small), so it prefers a path that builds on facts the reader already has
        rather than an arbitrary branch.  Returns
        ``reachable_now`` (goals enterable already), ``catch_up_plan`` (ordered
        segments for the shortest path), and ``reachable_after``.
        """
        turn = self.turns[turn_id]
        real_goals = [g for g in goals if g in turn.segments]
        goal_set = set(real_goals)
        held0 = frozenset(f for f in self.facts if self.has_fact(reader, f, turn_id))

        def able_goals(held: frozenset) -> List[str]:
            return [g for g in real_goals if set(turn.segments[g].requires) <= held]

        now = able_goals(held0)
        if now:
            return {"reachable_now": now, "catch_up_plan": [],
                    "reachable_after": now, "held": set(held0)}

        # BFS over fact-sets; stop at the first state that unlocks a goal.
        from collections import deque
        start = held0
        queue = deque([(start, [])])
        seen = {start}
        while queue:
            held, path = queue.popleft()
            if able_goals(held):
                return {"reachable_now": [], "catch_up_plan": path,
                        "reachable_after": able_goals(held), "held": set(held)}
            for seg in turn.segments.values():
                if seg.id in goal_set or not seg.discloses or seg.id in path:
                    continue
                if not set(seg.requires) <= held:
                    continue
                nxt = held | set(seg.discloses)
                if nxt == held or nxt in seen:
                    continue
                seen.add(nxt)
                queue.append((nxt, path + [seg.id]))
        return {"reachable_now": [], "catch_up_plan": [],
                "reachable_after": [], "held": set(held0)}

    # -- make-up reads (补读) ---------------------------------------------

    def open_practice(self, reader: str, session_id: str, fact: str, *,
                      turn: Optional[str] = None,
                      context_session: Optional[str] = None) -> Practice:
        """Create a make-up read that supplies one missing fact.

        Only valid when ``assess`` returns ``catch_up``; otherwise a rebook is
        required.  The practice pins the exact edition and segment.
        """
        with self._lock:
            sess = self._session(session_id)
            verdict = self.assess(session_id, reader, fact, turn)
            if verdict["status"] == "has":
                raise BookClubError("already_has", f"{reader!r} already has {fact!r} on this edition")
            if verdict["status"] == "rebook":
                raise BookClubError(
                    "needs_rebook",
                    f"no segment on this edition discloses {fact!r}; rebook instead (改约)",
                    status=409, extra=verdict,
                )
            segment = verdict["chosen_segment"]
            # If prerequisites are not yet met, the make-up read must start with
            # the first step of a shortest path toward a segment disclosing the
            # fact; otherwise it could never be completed.
            if not verdict.get("prerequisites_met", True):
                path = self._path_to_fact(reader, verdict["turn"], fact)
                if path:
                    segment = path[0]
            practice = Practice(
                id=self._id("prac"), reader=reader, session=session_id,
                work=sess.work, turn=verdict["turn"],
                segment=segment,
                context_session=context_session or session_id,
                created_at=time.time(),
            )
            self.practices[practice.id] = practice
            self.save()
            return practice

    def _path_to_fact(self, reader: str, turn_id: str, fact: str) -> List[str]:
        """Shortest ordered segments the reader must read to disclose ``fact``.

        Breadth-first over held-fact sets; empty if unreachable from current
        holdings.
        """
        from collections import deque
        turn = self.turns[turn_id]
        held0 = frozenset(f for f in self.facts if self.has_fact(reader, f, turn_id))
        if fact in held0:
            return []
        queue = deque([(held0, [])])
        seen = {held0}
        while queue:
            held, path = queue.popleft()
            for seg in turn.segments.values():
                if seg.id in path or not seg.discloses:
                    continue
                if not set(seg.requires) <= held:
                    continue
                if fact in seg.discloses:
                    return path + [seg.id]
                nxt = held | set(seg.discloses)
                if nxt in seen:
                    continue
                seen.add(nxt)
                queue.append((nxt, path + [seg.id]))
        return []

    def _practice_view(self, p: Practice) -> dict:
        seg = self.turns[p.turn].segments[p.segment]
        return {
            "id": p.id, "reader": p.reader, "session": p.session,
            "context_session": p.context_session, "work": p.work,
            "turn": p.turn, "segment": p.segment, "segment_title": seg.title,
            "discloses": list(seg.discloses), "completed_at": p.completed_at,
            "done": p.completed_at is not None,
        }

    def get_practice(self, practice_id: str) -> dict:
        with self._lock:
            p = self.practices.get(practice_id)
            if not p:
                raise BookClubError("unknown_practice", f"practice {practice_id!r} does not exist", status=404)
            return self._practice_view(p)

    def complete_practice(self, practice_id: str) -> dict:
        """Reader finishes a make-up read; its facts are recorded on its edition."""
        with self._lock:
            p = self.practices.get(practice_id)
            if not p:
                raise BookClubError("unknown_practice", f"practice {practice_id!r} does not exist", status=404)
            if p.completed_at is not None:
                return self._practice_view(p)
            seg = self.turns[p.turn].segments[p.segment]
            missing = [f for f in seg.requires if not self.has_fact(p.reader, f, p.turn)]
            if missing:
                raise BookClubError(
                    "missing_requirement",
                    f"make-up read needs {missing} first on {p.turn}",
                    status=409, extra={"missing": missing},
                )
            for fact in seg.discloses:
                self._record_fact(p.reader, fact, p.turn, f"practice:{p.id}")
            p.completed_at = time.time()
            self.save()
            return self._practice_view(p)

    def reader_facts_from_practices(self, reader: str) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for p in self.practices.values():
            if p.reader != reader or p.completed_at is None:
                continue
            for fact in self.turns[p.turn].segments[p.segment].discloses:
                out.setdefault(fact, {})[p.turn] = p.id
        return out

    # -- intake comparison & editorial revert ------------------------------

    def simulate_read(self, reader: str, work_id: str, turn_id: str,
                      segment_id: str) -> dict:
        """Preview a read against current records without persisting anything.

        Used to compare "the same not-yet-locked intake point, only the edition
        given to readers changed": call this before/after a revision to see how
        eligibility shifts.  It never mutates the store.
        """
        with self._lock:
            turn = self._turn(turn_id)
            if work_id not in self.works or turn_id not in self.works[work_id]["turns"]:
                raise BookClubError("unknown_work_turn", "turn does not belong to work")
            seg = turn.segments.get(segment_id)
            if not seg:
                raise BookClubError("unknown_segment", f"segment {segment_id!r} not in turn")
            missing = [f for f in seg.requires if not self.has_fact(reader, f, turn_id)]
            would_gain = [f for f in seg.discloses if not self.has_fact(reader, f, turn_id)]
            return {
                "reader": reader, "turn": turn_id, "segment": segment_id,
                "allowed": not missing, "missing": missing,
                "would_gain": would_gain if not missing else [],
                "no_op": True,
            }

    def compare_intake(self, reader: str, fact: str, *,
                       turn_a: str, turn_b: str,
                       segment: Optional[str] = None) -> dict:
        """Compare a reader's eligibility for ``fact`` between two editions.

        Both sides are measured from the *current* (unsimulated) reading
        records, i.e. the same intake point; only the edition differs.  This is
        exactly the "first pass / second pass from the same origin" comparison.
        """
        with self._lock:
            self._turn(turn_a)
            self._turn(turn_b)
            a = self._intake_state(reader, fact, turn_a, segment)
            b = self._intake_state(reader, fact, turn_b, segment)
            return {
                "reader": reader, "fact": fact,
                "turn_a": a, "turn_b": b,
                "regressed": a["has_fact"] and not b["has_fact"],
                "improved": (not a["has_fact"]) and b["has_fact"],
                "changed": a["status"] != b["status"],
            }

    def _intake_state(self, reader: str, fact: str, turn_id: str,
                      segment: Optional[str]) -> dict:
        state = {
            "turn": turn_id,
            "has_fact": self.has_fact(reader, fact, turn_id),
            "sources": self.fact_sources(reader, fact, turn_id),
            "disclosing_segments": self._disclosing_segments(fact, turn_id),
        }
        if segment is not None:
            seg = self.turns[turn_id].segments.get(segment)
            state["segment"] = segment
            state["segment_exists"] = seg is not None
            state["entry_allowed"] = bool(seg) and all(
                self.has_fact(reader, r, turn_id) for r in seg.requires)
            state["segment_discloses_fact"] = bool(seg) and fact in seg.discloses
        state["status"] = ("has" if state["has_fact"]
                           else "catch_up" if state["disclosing_segments"]
                           else "rebook")
        return state

    def revert_turn(self, turn_id: str) -> dict:
        """Restore a work's current edition to ``turn_id`` (editorial revert).

        The target edition must belong to the work.  This only changes which
        edition is *current* for future sessions; existing pinned sessions keep
        their locked edition, and no reading records are altered.
        """
        with self._lock:
            turn = self._turn(turn_id)
            work = next((w for w in self.works.values() if turn_id in w["turns"]), None)
            if not work:
                raise BookClubError("unknown_work_turn", "turn does not belong to any work")
            previous = work["current"]
            work["current"] = turn_id
            self.save()
            return {"work": work["id"], "current": turn_id,
                    "previous_current": previous}

    def delete_recording(self, reader: str, fact: str, turn_id: str,
                         *, segment: Optional[str] = None) -> dict:
        """Alias of :meth:`revoke_read` for 'undo a mis-tap that was never read'."""
        return self.revoke_read(reader, fact, turn_id, segment=segment)

    # -- rebooking (改约) --------------------------------------------------

    def rebook(self, session_id: str, reader: str, *, reason: str = "") -> dict:
        with self._lock:
            sess = self._session(session_id)
            if reader not in sess.joined:
                raise BookClubError("not_joined", f"{reader!r} is not registered for this session")
            key = (session_id, reader, "rebook")
            now = time.time()
            if key in self.requests:
                req = self.requests[key]
                req["updated_at"] = now
                if reason:
                    req["reason"] = reason
            else:
                req = {"session": session_id, "reader": reader, "kind": "rebook",
                       "reason": reason, "status": "pending",
                       "created_at": now, "updated_at": now}
                self.requests[key] = req
            self.save()
            return dict(req)

    def requests_for(self, session_id: str) -> List[dict]:
        with self._lock:
            out = [dict(v) for k, v in self.requests.items() if k[0] == session_id]
            return sorted(out, key=lambda r: (r["reader"], r["kind"]))

    def front_desk(self, session_id: str, reader: str, fact: str, *,
                   staff_id: Optional[str] = None,
                   start: Optional[float] = None,
                   end: Optional[float] = None,
                   turn: Optional[str] = None) -> dict:
        """Tell the desk whether a reader needs 补读 or 改约.

        Two independent questions are answered:

        * *facts* -- does the reader hold the fact on the session's edition?
          If not, can a segment on that edition supply it (``catch_up`` ->
          补读) or is it absent entirely (``rebook`` -> 改约)?
        * *slot*  -- when a time window and staff member are supplied, is that
          staff member free?  An overlap forces 改约 even when every reader's
          facts are identical.

        ``action`` is the single thing the desk should do next:
        ``"proceed"``, ``"catch_up"`` or ``"rebook"``.
        """
        with self._lock:
            sess = self._session(session_id)
            fact_state = self.assess(session_id, reader, fact, turn)
            reasons: List[str] = []
            action = "proceed"
            if fact_state["status"] == "catch_up":
                action = "catch_up"
                reasons.append("fact not yet held but a segment on this edition discloses it")
            elif fact_state["status"] == "rebook":
                action = "rebook"
                reasons.append("no segment on this edition discloses the fact")

            slot_state = None
            if staff_id is not None and start is not None and end is not None:
                if staff_id not in self.staff:
                    raise BookClubError("unknown_staff", f"staff {staff_id!r} does not exist")
                if end <= start:
                    raise BookClubError("bad_time", "end must be after start")
                conflict = self._staff_conflict(staff_id, start, end,
                                                ignore_session=sess.id, ignore_slot=0)
                slot_state = {"available": conflict is None, "conflict": conflict}
                if conflict:
                    # Scheduling dominates: even identical facts cannot free the
                    # staff member, so the desk must rebook the slot (改约).
                    action = "rebook"
                    reasons.append("requested staff member is serving an overlapping session")
            return {
                "session": session_id,
                "reader": reader,
                "fact": fact,
                "action": action,
                "fact_state": fact_state,
                "slot_state": slot_state,
                "reasons": reasons,
                "recommendation": {
                    "proceed": "start the co-reading as planned",
                    "catch_up": "issue a make-up read (补读) now; it carries into later sessions",
                    "rebook": "move the reader to another session/time (改约)",
                }[action],
            }

    # -- route planning ----------------------------------------------------

    def plan_route(self, reader: str, work_id: str,
                   turn_id: Optional[str] = None) -> dict:
        """Short catch-up plan for one reader on an edition (minimal segments)."""
        with self._lock:
            if work_id not in self.works:
                raise BookClubError("unknown_work", f"work {work_id!r} does not exist")
            turn_id = turn_id or self.works[work_id]["current"]
            turn = self._turn(turn_id)
            held = {f for f in self.facts if self.has_fact(reader, f, turn_id)}
            target = {f for seg in turn.segments.values() for f in seg.discloses} - held
            plan: List[str] = []
            progressed = True
            while progressed and (target - held):
                progressed = False
                for seg in turn.segments.values():
                    if seg.id in plan or not seg.discloses:
                        continue
                    if not set(seg.requires) <= held:
                        continue
                    if not (set(seg.discloses) & (target - held)):
                        continue
                    plan.append(seg.id)
                    held |= set(seg.discloses)
                    progressed = True
                    break
            missing = sorted(target - held)
            return {"reader": reader, "turn": turn_id, "plan": plan,
                    "still_missing": missing, "needs_rebook": bool(missing)}

    # -- views -------------------------------------------------------------

    def session_view(self, session_id: str) -> dict:
        with self._lock:
            sess = self._session(session_id)
            turn_id = self._turn_for_session(sess)
            view = {
                "id": sess.id, "title": sess.title, "kind": sess.kind,
                "work": sess.work, "turn": turn_id,
                "turn_label": self.turns[turn_id].label,
                "status": sess.status, "joined": sess.joined,
                "slots": sess.slots, "served_by": sess.served_by,
                "ready": sess.ready, "line": sess.line,
                "turn_is_current": turn_id == self.works[sess.work]["current"],
            }
            view["segments"] = {
                sid: {"title": s.title, "discloses": s.discloses,
                      "requires": s.requires, "describes": s.describes}
                for sid, s in self.turns[turn_id].segments.items()
            }
            return view

    def reader_view(self, reader: str) -> dict:
        with self._lock:
            return {
                "reader": reader,
                "reads": {
                    turn: {fact: sorted(srcs) for fact, srcs in facts.items()}
                    for turn, facts in self.reads.get(reader, {}).items()
                },
                "practices": {pid: self._practice_view(p)
                              for pid, p in self.practices.items() if p.reader == reader},
            }

    def turn_view(self, turn_id: str) -> dict:
        with self._lock:
            t = self._turn(turn_id)
            return {
                "id": t.id, "label": t.label, "revision_of": t.revision_of,
                "unchanged_segments": t.unchanged_segments,
                "carry_facts": t.carry_facts, "note": t.note,
                "segments": {sid: asdict(s) for sid, s in t.segments.items()},
            }

    def work_view(self, work_id: str) -> dict:
        with self._lock:
            w = self.works.get(work_id)
            if not w:
                raise BookClubError("unknown_work", f"work {work_id!r} does not exist", status=404)
            return {
                "id": w["id"], "title": w["title"], "current": w["current"],
                "turns": [{"id": tid, "label": self.turns[tid].label,
                           "revision_of": self.turns[tid].revision_of,
                           "unchanged_segments": self.turns[tid].unchanged_segments,
                           "carry_facts": self.turns[tid].carry_facts,
                           "published_at": self.turns[tid].published_at}
                          for tid in w["turns"]],
            }

    def staff_view(self) -> List[dict]:
        with self._lock:
            return [dict(s) for s in self.staff.values()]

    def take_snapshot(self) -> dict:
        with self._lock:
            return {
                "facts": [asdict(f) for f in self.facts.values()],
                "works": self.works,
                "turns": [self.turn_view(t) for t in self.turns],
                "sessions": [self.session_view(s) for s in self.sessions],
                "staff": self.staff_view(),
                "practices": [self._practice_view(p) for p in self.practices.values()],
                "reads": {
                    r: {turn: {f: sorted(src) for f, src in facts.items()}
                        for turn, facts in turns.items()}
                    for r, turns in self.reads.items()
                },
            }


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return a_start < b_end and b_start < a_end
