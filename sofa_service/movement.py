"""Exact one-cell translation / quarter-turn search on a grid."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .floorplan import FloorPlan
from .geometry import Cell, Placement, Shape, _placement_tuple, add, orientation_name, placement_json

DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1)]


@dataclass
class Violation:
    code: str
    message: str
    cells: List[List[int]]

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "cells": self.cells}


def _blocked_reason(fp: FloorPlan, c: Cell, obstacle_cells: Optional[Set[Cell]]) -> Optional[Violation]:
    if not fp.in_bounds(c):
        return Violation("OUT_OF_BOUNDS", f"cell {c} is outside the plan", [[c[0], c[1]]])
    if c in fp.walls:
        return Violation("WALL_COLLISION", f"cell {c} is a wall", [[c[0], c[1]]])
    if c in fp.property_blocked:
        return Violation(
            "PROPERTY_BLOCKED",
            f"property management blocked template cell {c}",
            [[c[0], c[1]]],
        )
    if obstacle_cells and c in obstacle_cells:
        return Violation(
            "ITEM_COLLISION",
            f"cell {c} is occupied by another part/subassembly",
            [[c[0], c[1]]],
        )
    return None


def pose_violations(
    fp: FloorPlan,
    shape: Shape,
    placement: Placement | Dict[str, Any],
    obstacle_cells: Optional[Set[Cell]] = None,
) -> List[Violation]:
    p, o = _placement_tuple(placement)
    out: List[Violation] = []
    for c in shape.occupancy((p[0], p[1], o)):
        v = _blocked_reason(fp, c, obstacle_cells)
        if v:
            out.append(v)
    return out


def rotation_violations(
    fp: FloorPlan,
    shape: Shape,
    state: Placement,
    direction: str,
    obstacle_cells: Optional[Set[Cell]] = None,
) -> Tuple[Optional[Placement], List[Violation], FrozenSet[Cell]]:
    px, py, o = state
    d = "cw" if direction in ("cw", "clockwise") else "ccw"
    swept_world = shape.sweep(o, d, (px, py))
    violations: List[Violation] = []
    if swept_world is None:
        violations.append(
            Violation(
                "NO_ROTATION_TEMPLATE",
                f"catalog does not provide an {o}:{d} rotation template for {shape.key}",
                [],
            )
        )
        return None, violations, frozenset()

    hit_cells: List[Cell] = []
    for c in sorted(swept_world):
        v = _blocked_reason(fp, c, obstacle_cells)
        if v:
            violations.append(v)
            hit_cells.append(c)
    no = (o + (1 if d == "cw" else 3)) % 4
    end: Placement = (px, py, no)
    violations.extend(pose_violations(fp, shape, end, obstacle_cells))
    return end, violations, swept_world


def _edge_action(kind: str, state: Placement, nxt: Placement, **extra: Any) -> Dict[str, Any]:
    px, py, o = state
    nx, ny, no = nxt
    action: Dict[str, Any] = {
        "type": kind,
        "from": {"pivot": [px, py], "orientation": o, "orientationName": orientation_name(o)},
        "to": {"pivot": [nx, ny], "orientation": no, "orientationName": orientation_name(no)},
    }
    if kind == "translate":
        action.update({"dx": nx - px, "dy": ny - py})
    elif kind == "rotate":
        action.update(extra)
    return action


@dataclass
class Reachability:
    start: Placement
    parents: Dict[Placement, Optional[Placement]]
    actions: Dict[Placement, Dict[str, Any]]
    order: List[Placement]

    def path(self, goal: Placement) -> Optional[List[Dict[str, Any]]]:
        if goal not in self.parents:
            return None
        states: List[Placement] = []
        cur: Optional[Placement] = goal
        while cur is not None:
            states.append(cur)
            cur = self.parents[cur]
        states.reverse()
        actions: List[Dict[str, Any]] = []
        for a, b in zip(states, states[1:]):
            actions.append(self.actions[b])
        return actions

    def poses(self) -> Set[Placement]:
        return set(self.parents.keys())


def reachable_poses(
    fp: FloorPlan,
    shape: Shape,
    start: Placement | Dict[str, Any],
    obstacle_cells: Optional[Set[Cell]] = None,
    max_states: int = 2000,
) -> Tuple[Optional[Reachability], List[Violation]]:
    sj = placement_json(start)
    s: Placement = (sj["pivot"][0], sj["pivot"][1], sj["orientation"])
    start_violations = pose_violations(fp, shape, s, obstacle_cells)
    if start_violations:
        return None, start_violations

    q = deque([s])
    parents: Dict[Placement, Optional[Placement]] = {s: None}
    actions: Dict[Placement, Dict[str, Any]] = {}
    order: List[Placement] = [s]

    while q:
        state = q.popleft()
        px, py, o = state

        # A one-cell part is orientation-invariant; turning it only creates
        # equivalent search states and replay noise.
        if len(shape.cells) > 1:
            # Rotations are expanded before translations.  At equal path length
            # this prefers the explicit pivot turn at a designed corridor pocket
            # over sliding a still-long pose into a cell that is only meant as a
            # rotation sweep cell.
            for direction in ("cw", "ccw"):
                nxt, violations, swept = rotation_violations(
                    fp, shape, state, direction, obstacle_cells
                )
                if violations or nxt is None or nxt in parents:
                    continue
                parents[nxt] = state
                actions[nxt] = _edge_action(
                    "rotate",
                    state,
                    nxt,
                    direction=direction,
                    pivot=[px, py],
                    sweptCells=[[c[0], c[1]] for c in sorted(swept)],
                )
                q.append(nxt)
                order.append(nxt)
                if len(parents) >= max_states:
                    return Reachability(s, parents, actions, order), []

        for dx, dy in DIRS:
            nxt = (px + dx, py + dy, o)
            if nxt in parents:
                continue
            if pose_violations(fp, shape, nxt, obstacle_cells):
                continue
            parents[nxt] = state
            act = _edge_action("translate", state, nxt)
            actions[nxt] = act
            q.append(nxt)
            order.append(nxt)
            if len(parents) >= max_states:
                return Reachability(s, parents, actions, order), []

    return Reachability(s, parents, actions, order), []


def plan_motion(
    fp: FloorPlan,
    shape: Shape,
    start: Placement | Dict[str, Any],
    goal: Optional[Placement | Dict[str, Any]] = None,
    goal_predicate: Optional[Callable[[Placement], bool]] = None,
    obstacle_cells: Optional[Set[Cell]] = None,
    max_states: int = 2000,
) -> Dict[str, Any]:
    reach, violations = reachable_poses(fp, shape, start, obstacle_cells, max_states)
    if reach is None:
        return {"feasible": False, "violations": [v.to_dict() for v in violations], "actions": []}

    target: Optional[Placement] = None
    if goal is not None:
        gj = placement_json(goal)
        target = (gj["pivot"][0], gj["pivot"][1], gj["orientation"])
        if target not in reach.parents:
            target = None
    elif goal_predicate is not None:
        for st in reach.order:
            if goal_predicate(st):
                target = st
                break

    if target is None:
        return {
            "feasible": False,
            "violations": [{"code": "NO_PATH", "message": "no reachable goal pose", "cells": []}],
            "actions": [],
            "reachableCount": len(reach.parents),
        }

    actions = reach.path(target) or []
    return {
        "feasible": True,
        "goal": {"pivot": [target[0], target[1]], "orientation": target[2]},
        "actions": actions,
        "reachableCount": len(reach.parents),
    }
