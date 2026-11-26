"""Fixtures and scripted race scenarios used by tests and demonstrations."""

from __future__ import annotations

from typing import Any

from .core import Arc, FieldGraph, Mark, ProgramCue, RaceMode


def standard_graph() -> FieldGraph:
    """Field with reversible ropes r1/r2, one-way r3, and a flagless r4 branch."""

    return FieldGraph(
        nodes={"S", "R", "X", "P", "B", "F", "Q", "Z"},
        arcs=[
            Arc("r1_f", "r1", "S", "R", +1),
            Arc("r1_b", "r1", "R", "S", -1),
            Arc("r2_f", "r2", "R", "X", +1),
            Arc("r2_b", "r2", "X", "R", -1),
            Arc("r5_f", "r5", "X", "P", +1),
            Arc("r5_b", "r5", "P", "X", -1),
            Arc("r3_f", "r3", "S", "Q", +1),  # only forward: no Q->S reverse
            Arc("r4_f", "r4", "Q", "P", +1),  # only forward: flagless branch
            Arc("r6_to_b", "r6", "P", "B", -1),
            Arc("r6_to_p", "r6", "B", "P", +1),
            # Arrive on the reverse direction so the required observation is
            # the blue flag's back face.
            Arc("r7_b", "r7", "F", "B", -1),
            Arc("r7_f", "r7", "B", "F", +1),
            Arc("r8_f", "r8", "P", "Z", +1),
        ],
        flags={"R": "red", "B": "blue"},
    )


def branching_graph() -> FieldGraph:
    """Adds two valid branches and a deliberately closed branch to X."""

    base = standard_graph()
    return FieldGraph(
        nodes=set(base.nodes) | {"A1", "A2"},
        arcs=list(base.arcs)
        + [
            Arc("r9_f", "r9", "X", "A1", +1),
            Arc("r9_b", "r9", "A1", "X", -1),
            Arc("r10_f", "r10", "A1", "P", +1),
            Arc("r10_b", "r10", "P", "A1", -1),
            Arc("r11_f", "r11", "X", "A2", +1),
            Arc("r12_f", "r12", "A2", "P", +1),
            Arc("r12_b", "r12", "P", "A2", -1),
            # r11 has only X->A2, so A2->X is the deliberately closed branch.
        ],
        flags=dict(base.flags),
    )


def program(jia: str = "甲", yi: str = "乙") -> list[ProgramCue]:
    return [
        ProgramCue(jia, "red", "front", handoff_before=True, handoff_after=False),
        ProgramCue(yi, "blue", "back", handoff_before=False, handoff_after=True),
    ]


def valid_track() -> dict[str, list[tuple[int, str]]]:
    return {
        "甲": [(0, "S"), (1, "R"), (2, "X"), (3, "P")],
        # 乙 waits at P from tick zero, then leaves after the tick-3 joint.
        "乙": [(0, "P"), (1, "P"), (2, "P"), (3, "P"), (4, "B"), (5, "F")],
    }


def cancellation_track() -> dict[str, list[tuple[int, str]]]:
    """甲 reaches red-front, immediately reverses the same rope, then bypasses."""

    return {
        "甲": [(0, "S"), (1, "R"), (2, "S"), (3, "Q"), (4, "P")],
        "乙": [(0, "P"), (1, "P"), (2, "P"), (3, "P"), (4, "P"), (5, "B"), (6, "F")],
    }


def adjacent_receiver_track() -> dict[str, list[tuple[int, str]]]:
    """乙 remains one edge away at the handoff tick and only confirms alone."""

    return {
        "甲": [(0, "S"), (1, "R"), (2, "X"), (3, "P")],
        "乙": [(0, "Z"), (1, "Z"), (2, "Z"), (3, "Z")],
    }


def replay_track(service, race_id: str, tracks: dict[str, list[tuple[int, str]]]) -> None:
    race = service._race(race_id)
    for person, events in tracks.items():
        for tick, node in events:
            service.report_position(race_id, person, tick, node)
            if node in race.graph.flags:
                arc = race.graph.arc_between(events[tick - 1][1], node) if tick else None
                face = "front" if arc is not None and arc.direction > 0 else "back"
                service.report_mark(race_id, person, Mark(race.graph.flags[node], face, node, tick))


def briefing_sheet(handoff_node: str = "P") -> dict[str, Any]:
    return {
        "handoff_node": handoff_node,
        "assignments": [
            {
                "person": "甲",
                "start": "S",
                "handoff_node": handoff_node,
                "before_handoff": True,
                "options": [
                    {"option_id": "main-red", "nodes": ["S", "R", "X", "P"]},
                    {
                        "option_id": "conditional-alternative",
                        "nodes": ["S", "R", "X", "A1", "P"],
                        "condition": {
                            "flag": "red",
                            "face": "front",
                            "node": "R",
                            "tick": 1,
                        },
                    },
                ],
            },
            {
                "person": "乙",
                "start": "P",
                "handoff_node": None,
                "before_handoff": False,
                "options": [{"option_id": "blue-back", "nodes": ["P", "B", "F"]}],
            },
        ],
    }


def closed_briefing_sheet() -> dict[str, Any]:
    sheet = briefing_sheet()
    sheet["assignments"][0]["options"].append(
        {
            "option_id": "closed-r11-return",
            # A2->X has no reverse arc on one-way r11, so this cannot be promised.
            "nodes": ["S", "R", "X", "A2", "X", "P"],
            "condition": {"flag": "red", "face": "front", "node": "R", "tick": 1},
        }
    )
    return sheet


def race_config(mode: RaceMode = RaceMode.BRIEFING, graph: FieldGraph | None = None) -> dict[str, Any]:
    active_graph = graph or standard_graph()
    return {
        "name": "park paper kite cup",
        "mode": mode.value,
        "graph": {
            "nodes": sorted(active_graph.nodes),
            "arcs": [
                {
                    "arc_id": arc.arc_id,
                    "rope_id": arc.rope_id,
                    "src": arc.src,
                    "dst": arc.dst,
                    "direction": arc.direction,
                }
                for arc in active_graph.arcs
            ],
            "flags": active_graph.flags,
        },
        "roster": ["甲", "乙"],
        "handoff_nodes": ["P"],
        "program": [
            {
                "person": "甲",
                "flag": "red",
                "face": "front",
                "handoff_before": True,
                "handoff_after": False,
            },
            {
                "person": "乙",
                "face": "back",
                "flag": "blue",
                "handoff_before": False,
                "handoff_after": True,
            },
        ],
    }


__all__ = [
    "standard_graph",
    "branching_graph",
    "program",
    "valid_track",
    "cancellation_track",
    "adjacent_receiver_track",
    "replay_track",
    "briefing_sheet",
    "closed_briefing_sheet",
    "race_config",
]
