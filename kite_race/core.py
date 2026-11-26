"""Core state machine for the paper-node kite formation race.

The service is intentionally independent of HTTP so the same rules can be
exercised by unit tests, the command-line clients, and the HTTP API.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from itertools import count
from threading import RLock
from typing import Any, Iterable
import uuid


class RaceError(ValueError):
    """A rule violation reported to clients."""


class RaceMode(str, Enum):
    BRIEFING = "briefing"
    LIVE = "live"


@dataclass(frozen=True)
class Arc:
    """A directed arc. Two opposite arcs may share one physical rope id."""

    arc_id: str
    rope_id: str
    src: str
    dst: str
    direction: int  # +1 is the rope's printed forward direction, -1 reverse.

    def is_opposite_rope_edge(self, other: "Arc") -> bool:
        return (
            self.rope_id == other.rope_id
            and self.src == other.dst
            and self.dst == other.src
            and self.direction == -other.direction
        )


@dataclass(frozen=True)
class Mark:
    """A front/back observation of a flag at a node and tick."""

    flag: str
    face: str
    node: str
    tick: int

    def key(self) -> tuple[str, str]:
        return (self.flag, self.face)

    def to_dict(self) -> dict[str, Any]:
        return {"flag": self.flag, "face": self.face, "node": self.node, "tick": self.tick}


@dataclass(frozen=True)
class RouteOption:
    """One permitted way to perform a cue.

    ``condition`` is the mark that makes this branch visible/usable.  An empty
    condition means the branch is always available.  In a briefing race it is
    sent only to the flyer who can observe the condition.
    """

    option_id: str
    nodes: tuple[str, ...]
    condition: Mark | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "nodes": list(self.nodes),
            "condition": None if self.condition is None else self.condition.to_dict(),
        }


@dataclass
class RouteAssignment:
    person: str
    start: str
    handoff_node: str | None
    before_handoff: bool
    options: list[RouteOption] = field(default_factory=list)
    required_marks: list[Mark] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "person": self.person,
            "start": self.start,
            "handoff_node": self.handoff_node,
            "before_handoff": self.before_handoff,
            "options": [option.to_dict() for option in self.options],
            "required_marks": [mark.to_dict() for mark in self.required_marks],
        }


@dataclass
class Briefing:
    race_id: str
    handoff_node: str
    assignments: dict[str, RouteAssignment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "race_id": self.race_id,
            "handoff_node": self.handoff_node,
            "assignments": {name: item.to_dict() for name, item in self.assignments.items()},
        }


@dataclass
class HandoffRecord:
    handoff_id: str
    race_id: str
    node: str
    tick: int | None
    leader: str
    receiver: str
    invitation_id: str | None = None
    confirmations: dict[str, int] = field(default_factory=dict)
    scored: bool = False
    score_pair: tuple[str, str] | None = None
    refusal_reason: str | None = None
    formation_segment_id: str | None = None

    @property
    def is_confirmed(self) -> bool:
        return self.leader in self.confirmations and self.receiver in self.confirmations

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "race_id": self.race_id,
            "node": self.node,
            "tick": self.tick,
            "leader": self.leader,
            "receiver": self.receiver,
            "invitation_id": self.invitation_id,
            "confirmations": dict(self.confirmations),
            "confirmed": self.is_confirmed,
            "scored": self.scored,
            "score_pair": list(self.score_pair) if self.score_pair else None,
            "refusal_reason": self.refusal_reason,
            "formation_segment_id": self.formation_segment_id,
        }


@dataclass
class Invitation:
    invitation_id: str
    race_id: str
    node: str
    leader: str
    receiver: str
    request_tick: int
    option_id: str
    status: str = "offered"  # offered, accepted, refunded

    def to_dict(self) -> dict[str, Any]:
        return {
            "invitation_id": self.invitation_id,
            "race_id": self.race_id,
            "node": self.node,
            "leader": self.leader,
            "receiver": self.receiver,
            "request_tick": self.request_tick,
            "option_id": self.option_id,
            "status": self.status,
        }


@dataclass
class ProgramCue:
    person: str
    flag: str
    face: str
    handoff_before: bool
    handoff_after: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "person": self.person,
            "flag": self.flag,
            "face": self.face,
            "handoff_before": self.handoff_before,
            "handoff_after": self.handoff_after,
        }


@dataclass
class Race:
    race_id: str
    name: str
    graph: FieldGraph
    mode: RaceMode
    roster: list[str]
    tokens: dict[str, str]
    program: list[ProgramCue]
    handoff_nodes: set[str]
    positions: dict[str, dict[int, str]] = field(default_factory=lambda: defaultdict(dict))
    marks: dict[str, list[Mark]] = field(default_factory=lambda: defaultdict(list))
    handoffs: list[HandoffRecord] = field(default_factory=list)
    invitations: list[Invitation] = field(default_factory=list)
    briefings: dict[str, Briefing] = field(default_factory=dict)
    finalized: bool = False
    result: dict[str, Any] | None = None

    def token_owner(self, token: str) -> str:
        for person, person_token in self.tokens.items():
            if person_token == token:
                return person
        raise RaceError("invalid client token")


# FieldGraph is referenced in Race before its definition.  Resolve the type now.
@dataclass
class FieldGraph:
    nodes: set[str]
    arcs: list[Arc]
    flags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_endpoints: dict[tuple[str, str], Arc] = {}
        for arc in self.arcs:
            endpoints = (arc.src, arc.dst)
            if endpoints in self._by_endpoints:
                raise RaceError(f"duplicate directed arc {endpoints[0]}->{endpoints[1]}")
            self._by_endpoints[endpoints] = arc
        unknown = set(self.flags) - self.nodes
        if unknown:
            raise RaceError(f"flags placed on unknown nodes: {sorted(unknown)}")
        for arc in self.arcs:
            if arc.src not in self.nodes or arc.dst not in self.nodes:
                raise RaceError(f"arc {arc.arc_id} leaves the field")

    def arc_between(self, src: str, dst: str) -> Arc | None:
        return self._by_endpoints.get((src, dst))

    def validate_path(self, nodes: Iterable[str]) -> list[Arc]:
        nodes = tuple(nodes)
        if len(nodes) < 1:
            raise RaceError("a route needs at least one node")
        if any(node not in self.nodes for node in nodes):
            raise RaceError("route leaves the paper field")
        used: list[Arc] = []
        for src, dst in zip(nodes, nodes[1:]):
            if src == dst:
                continue  # Waiting at a node consumes a tick without a rope arc.
            arc = self.arc_between(src, dst)
            if arc is None:
                raise RaceError(f"no directed paper rope arc {src}->{dst}")
            used.append(arc)
        return used

    def path_marks(self, person: str, nodes: tuple[str, ...], start_tick: int = 0) -> list[Mark]:
        """Marks that a referee would see on a planned path."""

        self.validate_path(nodes)
        seen: list[Mark] = []
        for tick, node in enumerate(nodes[1:], start=start_tick + 1):
            if node in self.flags:
                face = infer_face(self, nodes, node, tick)
                seen.append(Mark(self.flags[node], face, node, tick))
        return seen


def infer_face(graph: FieldGraph, nodes: tuple[str, ...], node: str, tick: int) -> str:
    """Infer front/back from the direction of arrival.

    A flag's front faces its incoming printed-forward arc.  This keeps the
    paper fixture explicit while still allowing a path to mention a node
    without separately naming every observation.
    """

    if tick <= 0 or tick >= len(nodes):
        return "front"
    arc = graph.arc_between(nodes[tick - 1], nodes[tick])
    return "front" if arc is not None and arc.direction > 0 else "back"


@dataclass
class _ArcEvent:
    arc: Arc
    end_tick: int
    marks: list[Mark]


@dataclass
class _Barrier:
    handoff_id: str
    tick: int


@dataclass
class ReducedSegment:
    person: str
    index: int
    handoff_before: str | None
    handoff_after: str | None
    arcs: list[Arc]
    marks: list[Mark]


class RaceService:
    def __init__(self) -> None:
        self._lock = RLock()
        self._races: dict[str, Race] = {}
        self._identifiers = count(1)

    def _race(self, race_id: str) -> Race:
        try:
            return self._races[race_id]
        except KeyError as exc:
            raise RaceError("unknown race") from exc

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._identifiers):04d}"

    def create_race(
        self,
        name: str,
        graph: FieldGraph,
        roster: list[str],
        program: list[ProgramCue],
        handoff_nodes: Iterable[str],
        mode: RaceMode,
    ) -> Race:
        if len(roster) != len(set(roster)):
            raise RaceError("roster contains duplicate people")
        if len(roster) != 2:
            raise RaceError("this program is for a two-person team")
        handoff_set = set(handoff_nodes)
        if not handoff_set or not handoff_set <= graph.nodes:
            raise RaceError("handoff nodes must be known field nodes")
        if len(program) != 2:
            raise RaceError("program must contain exactly two ordered flag cues")
        people = set(roster)
        for cue in program:
            if cue.person not in people:
                raise RaceError(f"program cue assigned to unknown person {cue.person}")
        race_id = self._next_id("race")
        tokens = {person: uuid.uuid4().hex for person in roster}
        race = Race(
            race_id=race_id,
            name=name,
            graph=graph,
            mode=mode,
            roster=list(roster),
            tokens=tokens,
            program=list(program),
            handoff_nodes=handoff_set,
        )
        self._races[race_id] = race
        return race

    def token_for(self, race_id: str, person: str) -> str:
        race = self._race(race_id)
        try:
            return race.tokens[person]
        except KeyError as exc:
            raise RaceError("unknown team member") from exc

    def register_roster(self, race_id: str) -> dict[str, str]:
        race = self._race(race_id)
        return dict(race.tokens)

    def report_position(self, race_id: str, person: str, tick: int, node: str) -> None:
        with self._lock:
            race = self._race(race_id)
            self._require_open(race)
            if person not in race.tokens:
                raise RaceError("unknown team member")
            if tick < 0:
                raise RaceError("ticks cannot be negative")
            if node not in race.graph.nodes:
                raise RaceError("unknown field node")
            existing = race.positions[person].get(tick)
            if existing is not None and existing != node:
                raise RaceError(f"conflicting position already reported at tick {tick}")
            if existing == node:
                return
            earlier = [(t, n) for t, n in race.positions[person].items() if t < tick]
            if earlier:
                prev_tick, prev_node = max(earlier)
                if tick != prev_tick + 1:
                    raise RaceError("position reports must use every tick (waiting is explicit)")
                if prev_node != node and race.graph.arc_between(prev_node, node) is None:
                    raise RaceError(f"no directed arc {prev_node}->{node}; reverse edge is closed")
            race.positions[person][tick] = node

    def report_mark(self, race_id: str, person: str, mark: Mark) -> None:
        with self._lock:
            race = self._race(race_id)
            self._require_open(race)
            if person not in race.tokens:
                raise RaceError("unknown team member")
            if mark.node not in race.graph.nodes or race.graph.flags.get(mark.node) != mark.flag:
                raise RaceError("mark does not match a flag on the field")
            if mark.face not in {"front", "back"}:
                raise RaceError("mark face must be front or back")
            if race.positions[person].get(mark.tick) != mark.node:
                raise RaceError("a mark requires a referee position report at the same node and tick")
            if mark not in race.marks[person]:
                race.marks[person].append(mark)
                race.marks[person].sort(key=lambda item: item.tick)

    def issue_briefing(
        self,
        race_id: str,
        handoff_node: str,
        assignments: dict[str, RouteAssignment],
    ) -> Briefing:
        with self._lock:
            race = self._race(race_id)
            if race.mode is not RaceMode.BRIEFING:
                raise RaceError("route sheets are issued only in a briefing race")
            if handoff_node not in race.handoff_nodes:
                raise RaceError(f"{handoff_node} is not an allowed handoff point")
            if set(assignments) != set(race.roster):
                raise RaceError("briefing must contain one private sheet per teammate")
            for person, assignment in assignments.items():
                if assignment.person != person:
                    raise RaceError("route sheet owner mismatch")
                if not assignment.options:
                    raise RaceError(f"{person} needs at least one usable route")
                for option in assignment.options:
                    arcs = race.graph.validate_path(option.nodes)
                    if option.nodes[0] != assignment.start:
                        raise RaceError("route option does not start at assigned node")
                    if assignment.handoff_node is not None and option.nodes[-1] != assignment.handoff_node:
                        raise RaceError("route option does not end at its assigned handoff")
                    self._validate_option_marks(race, assignment, option, arcs)
            briefing = Briefing(race_id, handoff_node, assignments)
            race.briefings[handoff_node] = briefing
            return briefing

    def _validate_option_marks(
        self,
        race: Race,
        assignment: RouteAssignment,
        option: RouteOption,
        arcs: list[Arc],
    ) -> None:
        person = assignment.person
        marks: list[Mark] = []
        arc_index = 0
        for tick, node in enumerate(option.nodes[1:], start=1):
            if node in race.graph.flags:
                face = "front" if arc_index >= len(arcs) or arcs[arc_index].direction > 0 else "back"
                marks.append(Mark(race.graph.flags[node], face, node, tick))
            if option.nodes[tick - 1] != node:
                arc_index += 1
        required = {
            (cue.flag, cue.face)
            for cue in race.program
            if cue.person == person and cue.handoff_before == assignment.before_handoff
        }
        observed = {mark.key() for mark in marks}
        if not required <= observed:
            raise RaceError(f"option {option.option_id} cannot perform {person}'s required cue")
        if option.condition is not None:
            condition = option.condition
            if race.graph.flags.get(condition.node) != condition.flag:
                raise RaceError("branch condition is not a visible field flag")

    def request_live_handoff(
        self,
        race_id: str,
        leader_token: str,
        node: str,
        tick: int,
        option_id: str,
        route_nodes: tuple[str, ...],
    ) -> Invitation:
        with self._lock:
            race = self._race(race_id)
            leader = race.token_owner(leader_token)
            if race.mode is not RaceMode.LIVE:
                raise RaceError("live segment requests require a live-command race")
            if node not in race.handoff_nodes:
                raise RaceError(f"{node} is not an allowed handoff point")
            if race.positions[leader].get(tick) != node:
                raise RaceError("leader must be at a contactable node at the request tick")
            receiver = self._other_person(race, leader)
            # A live receiver must not already have occupied the handoff point:
            # an invitation cannot be used to backfill an earlier presence.
            for reported_tick, reported_node in race.positions[receiver].items():
                if reported_node == node and reported_tick <= tick:
                    raise RaceError("receiver cannot be backfilled onto a point before invitation")
            try:
                arcs = race.graph.validate_path(route_nodes)
                if route_nodes[0] != node:
                    raise RaceError("leader's requested segment must start at the handoff node")
                if len(route_nodes) > 1 and route_nodes[-1] != node:
                    raise RaceError("a leader's detour segment must return to the handoff node")
                if len(route_nodes) > 1 and any(src != dst for src, dst in zip(route_nodes, route_nodes[1:])):
                    self._validate_live_leader_route(race, leader, route_nodes, arcs)
            except RaceError:
                invitation = Invitation(
                    self._next_id("inv"),
                    race_id,
                    node,
                    leader,
                    receiver,
                    tick,
                    option_id,
                    status="refunded",
                )
                race.invitations.append(invitation)
                raise
            invitation = Invitation(
                self._next_id("inv"),
                race_id,
                node,
                leader,
                receiver,
                tick,
                option_id,
            )
            race.invitations.append(invitation)
            return invitation

    def _validate_live_leader_route(
        self,
        race: Race,
        leader: str,
        nodes: tuple[str, ...],
        arcs: list[Arc],
    ) -> None:
        required = [
            cue
            for cue in race.program
            if cue.person == leader and cue.handoff_before and not cue.handoff_after
        ]
        marks: list[Mark] = []
        arc_index = 0
        for tick, node in enumerate(nodes[1:], start=1):
            if node in race.graph.flags:
                face = "front" if arc_index >= len(arcs) or arcs[arc_index].direction > 0 else "back"
                marks.append(Mark(race.graph.flags[node], face, node, tick))
            if nodes[tick - 1] != node:
                arc_index += 1
        have = [mark.key() for mark in marks]
        want = [(cue.flag, cue.face) for cue in required]
        if not all(key in have for key in want):
            raise RaceError("requested branch misses the leader's flag cue")

    def accept_invitation(self, race_id: str, receiver_token: str, invitation_id: str) -> Invitation:
        with self._lock:
            race = self._race(race_id)
            receiver = race.token_owner(receiver_token)
            invitation = self._find_invitation(race, invitation_id)
            if invitation.receiver != receiver:
                raise RaceError("invitation is addressed to the other teammate")
            if invitation.status == "refunded":
                raise RaceError("that branch was refunded and cannot be accepted")
            if invitation.status == "accepted":
                return invitation
            invitation.status = "accepted"
            return invitation

    def confirm_handoff(
        self,
        race_id: str,
        token: str,
        node: str,
        tick: int,
        invitation_id: str | None = None,
        handoff_id: str | None = None,
    ) -> HandoffRecord:
        with self._lock:
            race = self._race(race_id)
            person = race.token_owner(token)
            self._require_open(race)
            if node not in race.handoff_nodes:
                raise RaceError(f"{node} is not an allowed handoff point")
            if race.positions[person].get(tick) != node:
                raise RaceError("confirmation requires a referee report placing this person at P on that tick")
            if len(race.handoffs) >= 1:
                record = race.handoffs[0]
                if handoff_id is not None and record.handoff_id != handoff_id:
                    raise RaceError("unknown handoff id")
                if record.node == node and record.tick == tick:
                    # A different teammate completes the joint; the same token
                    # redelivering its confirmation is idempotent.
                    record.confirmations.setdefault(person, tick)
                    return record
                raise RaceError("this two-cue program permits only one handoff")

            invitation = None
            if invitation_id is not None:
                invitation = self._find_invitation(race, invitation_id)
                if invitation.status != "accepted":
                    raise RaceError("cannot confirm against an invitation that was not accepted")
                if invitation.node != node:
                    raise RaceError("confirmation node differs from the exact invitation")
            if race.mode is RaceMode.LIVE and invitation is None:
                raise RaceError("live handoffs require an exact accepted invitation")

            record = HandoffRecord(
                handoff_id=self._next_id("hop"),
                race_id=race_id,
                node=node,
                tick=tick,
                leader=race.roster[0],
                receiver=race.roster[1],
                invitation_id=invitation_id,
            )
            # The first confirmation creates the single joint.  A later lone
            # "I'm here" at another point can never create another one.
            record.confirmations[person] = tick
            race.handoffs.append(record)
            if invitation is not None and person == invitation.receiver:
                # The leader's live request is a standing confirmation while
                # waiting at the same node; the receiver's acceptance fixes the
                # actual tick.  Referee positions still prove the leader waited.
                if race.positions[invitation.leader].get(tick) != node:
                    raise RaceError("leader is no longer at the invited handoff node")
                record.confirmations.setdefault(invitation.leader, tick)
            return record

    def finalize(self, race_id: str) -> dict[str, Any]:
        with self._lock:
            race = self._race(race_id)
            self._require_open(race)
            for person in race.roster:
                if not race.positions[person]:
                    raise RaceError(f"{person} has no referee track")
            handoff = race.handoffs[0] if race.handoffs else None
            if handoff is None or not handoff.is_confirmed:
                raise RaceError("no separately confirmed joint handoff exists")
            self._validate_continuous_track(race, handoff)
            segments = self._reduce_tracks(race, handoff)
            self._score_program(race, handoff, segments)
            result = {
                "race_id": race.race_id,
                "mode": race.mode.value,
                "handoff": handoff.to_dict(),
                "segments": [segment_to_dict(item) for item in segments],
                "formation": [],
            }
            if handoff.scored:
                result["formation"] = [
                    {
                        "formation_segment_id": handoff.formation_segment_id,
                        "score_pair": list(handoff.score_pair or ()),
                        "handoff_id": handoff.handoff_id,
                        "rule": "credit goes to the pair that actually completed the confirmed joint",
                    }
                ]
            race.result = result
            race.finalized = True
            return result

    def query_person(self, race_id: str, token: str) -> dict[str, Any]:
        with self._lock:
            race = self._race(race_id)
            person = race.token_owner(token)
            handoffs = [item.to_dict() for item in race.handoffs if person in (item.leader, item.receiver)]
            private_briefing = None
            if race.briefings:
                briefing = next(iter(race.briefings.values()))
                assignment = briefing.assignments.get(person)
                private_briefing = {
                    "race_id": briefing.race_id,
                    "handoff_node": briefing.handoff_node,
                    "assignment": assignment.to_dict() if assignment else None,
                }
            return {
                "race_id": race.race_id,
                "person": person,
                "finalized": race.finalized,
                "handoffs": handoffs,
                "result": race.result,
                "briefing": private_briefing,
                "invitations": [
                    item.to_dict() for item in race.invitations if item.leader == person or item.receiver == person
                ],
            }

    def _validate_continuous_track(self, race: Race, handoff: HandoffRecord) -> None:
        for person in race.roster:
            ticks = sorted(race.positions[person])
            if ticks[0] != 0:
                raise RaceError(f"{person} must report tick zero")
            if ticks != list(range(ticks[-1] + 1)):
                raise RaceError(f"{person} has a gap in referee ticks")
            for prev_tick, tick in zip(ticks, ticks[1:]):
                src = race.positions[person][prev_tick]
                dst = race.positions[person][tick]
                if src != dst and race.graph.arc_between(src, dst) is None:
                    raise RaceError(f"{person} used a closed reverse edge")
            if race.positions[person].get(handoff.tick or -1) != handoff.node:
                raise RaceError(f"{person} is not at the handoff node at tick {handoff.tick}")

    def _reduce_tracks(self, race: Race, handoff: HandoffRecord) -> list[ReducedSegment]:
        assert handoff.tick is not None
        segments: list[ReducedSegment] = []
        for person in race.roster:
            positions = race.positions[person]
            marks_by_tick: dict[int, list[Mark]] = defaultdict(list)
            for mark in race.marks[person]:
                marks_by_tick[mark.tick].append(mark)
            before_events: list[Any] = []
            after_events: list[Any] = []
            for tick in sorted(positions):
                if tick > 0:
                    src = positions[tick - 1]
                    dst = positions[tick]
                    if src != dst:
                        arc = race.graph.arc_between(src, dst)
                        assert arc is not None
                        event = _ArcEvent(arc, tick, list(marks_by_tick.get(tick, [])))
                        (before_events if tick <= handoff.tick else after_events).append(event)
                    else:
                        for mark in marks_by_tick.get(tick, []):
                            (before_events if tick <= handoff.tick else after_events).append(mark)
                else:
                    for mark in marks_by_tick.get(tick, []):
                        before_events.append(mark)
            before = reduce_segment(person, 0, None, handoff.handoff_id, before_events)
            after = reduce_segment(
                person,
                1,
                handoff.handoff_id,
                None,
                after_events,
            )
            segments.extend([before, after])
        return segments

    def _score_program(
        self,
        race: Race,
        handoff: HandoffRecord,
        segments: list[ReducedSegment],
    ) -> None:
        by_side: dict[tuple[str, bool], ReducedSegment] = {}
        for segment in segments:
            is_before = segment.handoff_after == handoff.handoff_id
            by_side[(segment.person, is_before)] = segment
        for cue in race.program:
            segment = by_side.get((cue.person, not cue.handoff_after))
            if segment is None:
                handoff.scored = False
                handoff.refusal_reason = "program side missing"
                return
            observed = [mark.key() for mark in segment.marks]
            if (cue.flag, cue.face) not in observed:
                handoff.scored = False
                handoff.refusal_reason = (
                    f"missing required {cue.face} of {cue.flag} for {cue.person} after cancellation"
                )
                return
        if not handoff.is_confirmed:
            handoff.scored = False
            handoff.refusal_reason = "both teammates did not separately confirm"
            return
        handoff.scored = True
        handoff.score_pair = (handoff.leader, handoff.receiver)
        handoff.formation_segment_id = f"formation_{handoff.handoff_id}"

    def _find_invitation(self, race: Race, invitation_id: str) -> Invitation:
        for invitation in race.invitations:
            if invitation.invitation_id == invitation_id:
                return invitation
        raise RaceError("unknown invitation")

    @staticmethod
    def _other_person(race: Race, person: str) -> str:
        return next(item for item in race.roster if item != person)

    @staticmethod
    def _require_open(race: Race) -> None:
        if race.finalized:
            raise RaceError("race is finalized and read-only")


def reduce_segment(
    person: str,
    index: int,
    handoff_before: str | None,
    handoff_after: str | None,
    events: list[Any],
) -> ReducedSegment:
    """Cancel a forward-then-reverse round trip on one rope within a segment.

    The forward printed direction must be completed first; merely taking an
    available reverse edge and later returning forward is not the rule's
    original-edge turnback.  A confirmed handoff is represented by placing
    events in separate segments, so cancellation can never cross a joint.
    A one-way rope cannot produce the opposite event at all; movement
    validation rejects it before this stage.
    """

    stack: list[_ArcEvent] = []
    surviving_arcs: list[Arc] = []
    canceled_ticks: set[int] = set()
    all_marks: list[Mark] = []
    for event in events:
        if isinstance(event, Mark):
            all_marks.append(event)
            continue
        if isinstance(event, _Barrier):
            # Current callers split at barriers, but keep the rule explicit.
            for item in stack:
                surviving_arcs.append(item.arc)
            stack.clear()
            continue
        all_marks.extend(event.marks)
        if (
            stack
            and stack[-1].arc.rope_id == event.arc.rope_id
            and stack[-1].arc.direction == +1
            and event.arc.direction == -1
            and event.arc.is_opposite_rope_edge(stack[-1].arc)
        ):
            canceled = stack.pop()
            canceled_ticks.add(canceled.end_tick)
            canceled_ticks.add(event.end_tick)
            continue
        stack.append(event)
    for item in stack:
        surviving_arcs.append(item.arc)
    surviving_marks = [mark for mark in all_marks if mark.tick not in canceled_ticks]
    surviving_marks.sort(key=lambda mark: mark.tick)
    return ReducedSegment(
        person=person,
        index=index,
        handoff_before=handoff_before,
        handoff_after=handoff_after,
        arcs=surviving_arcs,
        marks=surviving_marks,
    )


def segment_to_dict(segment: ReducedSegment) -> dict[str, Any]:
    return {
        "person": segment.person,
        "segment_index": segment.index,
        "handoff_before": segment.handoff_before,
        "handoff_after": segment.handoff_after,
        "arcs": [
            {
                "arc_id": arc.arc_id,
                "rope_id": arc.rope_id,
                "src": arc.src,
                "dst": arc.dst,
                "direction": arc.direction,
            }
            for arc in segment.arcs
        ],
        "marks": [mark.to_dict() for mark in segment.marks],
    }
