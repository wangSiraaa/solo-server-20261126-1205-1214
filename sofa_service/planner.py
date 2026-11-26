"""Whole-unit and piecewise delivery planners.

The piecewise planner searches a tiny macro-state graph.  A macro edge moves one
loose part from its current resting pose (or from the elevator source) to any
reachable pose; primitive translation/rotation edges are retained for replay.
A second kind of macro edge installs a craft joint after exact alignment and
clearance checks.  Joined parts are fixed, so their occupied cells remain
obstacles for all later trips.
"""

from __future__ import annotations

import heapq
import itertools
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from .catalog import model_hash
from .floorplan import FloorPlan, normalize_floorplan
from .geometry import Cell, Placement, PlanError, normalize_shape, placement_json
from .movement import pose_violations, reachable_poses
from .ordering import (
    OrderContext,
    candidate_seat_poses,
    final_layout_fits,
    goal_poses_for_seat,
    joint_clear_cells,
    normalize_order,
)

MAX_MACRO_STATES = 12000


def _obstacle_cells(
    ctx: OrderContext,
    poses: Tuple[Optional[Placement], ...],
    exclude: Optional[int] = None,
) -> Set[Cell]:
    out: Set[Cell] = set()
    for idx, pose in enumerate(poses):
        if idx == exclude or pose is None:
            continue
        out.update(ctx.items[idx].shape.occupancy(pose))
    return out


def _whole_shape(ctx: OrderContext, seat_pose: Placement = (0, 0, 0)):
    goals = goal_poses_for_seat(ctx, seat_pose)
    cells: Set[Cell] = set()
    for item in ctx.items:
        pose = goals[item.id]
        cells.update(item.shape.occupancy(pose))

    spec: Dict[str, Any] = {
        "key": f"whole-{ctx.model_id}",
        "sku": "whole-preinstalled-kit",
        "kind": "whole",
        "name": "整装预装组合（含保护套）",
        "cells": [[c[0], c[1]] for c in sorted(cells)],
        "fulcrum": [0, 0],
    }
    wu = ctx.model.get("wholeUnit") or {}
    if wu.get("baseCwSweep"):
        spec["rotationTemplates"] = [
            {
                "from": 0,
                "direction": "cw",
                "pivot": [0, 0],
                "swept": wu["baseCwSweep"],
            }
        ]
        spec["completeTemplates"] = True
    else:
        spec["autoRotationTemplate"] = True
    shape = normalize_shape(spec)
    cover = wu.get("protectionCover")
    shape.metadata["protectionCover"] = cover
    shape.metadata["preinstalled"] = wu.get("preinstalled", [])
    return shape


def _primitive_steps(
    ctx: OrderContext,
    item_idx: int,
    actions: List[Dict[str, Any]],
    start: Placement,
    step_no: itertools.count,
    trip_number: int,
    include_begin: bool = True,
) -> List[Dict[str, Any]]:
    item = ctx.items[item_idx]
    steps: List[Dict[str, Any]] = []
    if include_begin:
        begin_id = f"S{next(step_no):03d}"
        steps.append(
            {
                "stepId": begin_id,
                "kind": "begin_trip",
                "trip": trip_number,
                "itemId": item.id,
                "sku": item.sku,
                "role": item.role,
                "instruction": f"第 {trip_number} 趟：从电梯取 {item.name}",
                "at": placement_json(start),
            }
        )
    current = start
    for act in actions:
        sid = f"S{next(step_no):03d}"
        to = act["to"]
        pose = (to["pivot"][0], to["pivot"][1], to["orientation"])
        occ = sorted([[c[0], c[1]] for c in item.shape.occupancy(pose)])
        step = {
            "stepId": sid,
            "kind": act["type"],
            "trip": trip_number,
            "itemId": item.id,
            "sku": item.sku,
            "role": item.role,
            "instruction": (
                f"{item.name}：向{'东南西北'[0]}..."
                if act["type"] == "translate"
                else f"{item.name}：以 {act.get('pivot')} 为支点转90度"
            ),
            **act,
            "occupiedAfter": occ,
        }
        if act["type"] == "translate":
            dir_text = {
                (1, 0): "东",
                (-1, 0): "西",
                (0, 1): "南",
                (0, -1): "北",
            }.get((act.get("dx"), act.get("dy")), "一格")
            step["instruction"] = f"{item.name}：向{dir_text}平移一格"
        else:
            step["instruction"] = (
                f"{item.name}：以 {act.get('pivot')} 为支点"
                f"{'顺' if act.get('direction') == 'cw' else '逆'}时针转90度；"
                f"扫过 {len(act.get('sweptCells', []))} 格"
            )
        steps.append(step)
        current = pose

    park_id = f"S{next(step_no):03d}"
    steps.append(
        {
            "stepId": park_id,
            "kind": "park",
            "trip": trip_number,
            "itemId": item.id,
            "sku": item.sku,
            "role": item.role,
            "at": placement_json(current),
            "occupiedAfter": sorted([[c[0], c[1]] for c in item.shape.occupancy(current)]),
            "instruction": f"将 {item.name} 暂放/就位在指定格位，勿占后续接合空格",
        }
    )
    return steps


def _join_step(
    ctx: OrderContext,
    joint_idx: int,
    seat_pose_for_clearance: Placement,
    step_no: itertools.count,
) -> Dict[str, Any]:
    j = ctx.joints[joint_idx]
    host = ctx.item_by_id[j.host]
    member = ctx.item_by_id[j.member]
    clear = joint_clear_cells(seat_pose_for_clearance, j.rule)
    sid = f"S{next(step_no):03d}"
    return {
        "stepId": sid,
        "kind": "join",
        "jointInstanceId": j.id,
        "ruleId": j.rule_id,
        "hostItemId": j.host,
        "memberItemId": j.member,
        "connectionFace": j.rule.get("face"),
        "fasteners": j.rule.get("fasteners"),
        "requiredClearCells": [[c[0], c[1]] for c in clear],
        "instruction": (
            f"连接 {host.name} 与 {member.name}（{j.rule.get('face')} 面，"
            f"{j.rule.get('fasteners', 1)} 件固件）；接合空格 "
            f"{[[c[0], c[1]] for c in clear]}"
        ),
    }


def _joint_violation(
    ctx: OrderContext,
    fp: FloorPlan,
    poses: Tuple[Optional[Placement], ...],
    joint_idx: int,
) -> Optional[Dict[str, Any]]:
    j = ctx.joints[joint_idx]
    by_id = {item.id: idx for idx, item in enumerate(ctx.items)}
    hi = by_id[j.host]
    mi = by_id[j.member]
    hp = poses[hi]
    mp = poses[mi]
    if hp is None or mp is None:
        return {"code": "JOINT_PARTS_NOT_PRESENT", "jointId": j.id}
    required_member = _target_from_host(hp, j.rule, ctx.items[mi].sku)
    if mp != required_member:
        return {
            "code": "JOINT_ALIGNMENT",
            "jointId": j.id,
            "required": placement_json(required_member),
            "actual": placement_json(mp),
        }
    clear = joint_clear_cells(hp if j.rule.get("clearHost", "seat") == "seat" else hp, j.rule)
    # Built-in clearance is specified relative to the seat.  If host is not
    # seat but rule says relativeTo=seat, find seat pose.
    if j.rule.get("clearanceRelativeTo") == "seat":
        seat_idx = next(i for i, it in enumerate(ctx.items) if it.role == "seat")
        clear = joint_clear_cells(poses[seat_idx], j.rule)  # type: ignore[arg-type]

    occupied = _obstacle_cells(ctx, poses)
    for c in clear:
        if not fp.in_bounds(c):
            return {"code": "CLEARANCE_OUT_OF_BOUNDS", "jointId": j.id, "cell": [c[0], c[1]]}
        if c in fp.blocked:
            return {"code": "CLEARANCE_BLOCKED", "jointId": j.id, "cell": [c[0], c[1]]}
        if c in occupied:
            owner = None
            for idx, pose in enumerate(poses):
                if pose is not None and c in ctx.items[idx].shape.occupancy(pose):
                    owner = ctx.items[idx].id
                    break
            return {
                "code": "CLEARANCE_OCCUPIED",
                "jointId": j.id,
                "cell": [c[0], c[1]],
                "occupiedBy": owner,
            }
    return None


def _target_from_host(host_pose: Placement, rule: Dict[str, Any], member_sku: Optional[str] = None) -> Placement:
    from .ordering import joint_target_pose

    return joint_target_pose(host_pose, rule, member_sku)


def _heuristic(
    ctx: OrderContext,
    poses: Tuple[Optional[Placement], ...],
    mask: int,
    goals: Dict[str, Placement],
) -> int:
    h = 0
    by_id = {item.id: idx for idx, item in enumerate(ctx.items)}
    for item in ctx.items:
        p = poses[by_id[item.id]]
        g = goals[item.id]
        if p is None:
            h += 12
            continue
        h += abs(p[0] - g[0]) + abs(p[1] - g[1])
        od = abs(p[2] - g[2]) % 4
        h += min(od, 4 - od) * 2
    h += (len(ctx.joints) - mask.bit_count()) * 8
    return h


def plan_piecewise_for_seat(
    ctx: OrderContext,
    fp: FloorPlan,
    seat_pose: Placement,
    goals: Dict[str, Placement],
    max_states: int = MAX_MACRO_STATES,
    greedy: bool = False,
) -> Dict[str, Any]:
    n = len(ctx.items)
    full_mask = (1 << len(ctx.joints)) - 1
    start: Tuple[Optional[Placement], ...] = tuple(None for _ in range(n))
    start_key = (start, 0)

    # Parent stores previous key and a macro edge description.
    parent: Dict[Tuple[Tuple[Optional[Placement], ...], int], Tuple[Any, Dict[str, Any]]] = {
        start_key: (None, {})
    }
    gscore = {start_key: 0}
    counter = itertools.count()
    heap: List[Tuple[int, int, int, Tuple[Optional[Placement], ...], int]] = []
    h0 = _heuristic(ctx, start, 0, goals)
    heapq.heappush(heap, (h0, 0, next(counter), start, 0))
    explored = 0
    reach_cache: Dict[Tuple[Any, ...], Any] = {}

    def get_reach(idx: int, start: Placement, obstacles: Set[Cell]):
        key = (idx, start, tuple(sorted(obstacles)))
        if key not in reach_cache:
            reach_cache[key] = reachable_poses(fp, ctx.items[idx].shape, start, obstacles)
        return reach_cache[key]

    while heap:
        f, g, _, poses, mask = heapq.heappop(heap)
        key = (poses, mask)
        if g != gscore.get(key):
            continue
        explored += 1
        if explored > max_states:
            return {"feasible": False, "violations": [{"code": "SEARCH_LIMIT", "message": "macro search limit"}], "explored": explored}

        if mask == full_mask:
            # Joints can only be installed in aligned poses and joined parts
            # cannot move; exact goal check remains explicit.
            if all(poses[i] == goals[item.id] for i, item in enumerate(ctx.items)):
                return _finish_piecewise(ctx, fp, parent, key, explored, seat_pose, goals)

        frozen: Set[int] = set()
        for ji, j in enumerate(ctx.joints):
            if mask & (1 << ji):
                by_id = {item.id: idx for idx, item in enumerate(ctx.items)}
                frozen.add(by_id[j.host])
                frozen.add(by_id[j.member])

        # Join edges first: progress is almost always useful.
        for ji in range(len(ctx.joints)):
            if mask & (1 << ji):
                continue
            viol = _joint_violation(ctx, fp, poses, ji)
            if viol is not None:
                continue
            new_mask = mask | (1 << ji)
            new_key = (poses, new_mask)
            if new_key in parent:
                continue
            edge = {"kind": "join", "jointIndex": ji}
            parent[new_key] = (key, edge)
            ng = g + 1
            gscore[new_key] = ng
            h = _heuristic(ctx, poses, new_mask, goals)
            priority = h if greedy else ng + h
            heapq.heappush(heap, (priority, ng, next(counter), poses, new_mask))

        # Move one loose part to every currently reachable resting pose.
        for idx, item in enumerate(ctx.items):
            if idx in frozen:
                continue
            current = poses[idx]
            source = item.source if current is None else current
            obstacles = _obstacle_cells(ctx, poses, exclude=idx)
            reach, start_violations = get_reach(idx, source, obstacles)
            if reach is None:
                continue
            for end in reach.poses():
                if end == current:
                    continue
                new_poses_list = list(poses)
                new_poses_list[idx] = end
                new_poses = tuple(new_poses_list)
                new_key = (new_poses, mask)
                if new_key in parent:
                    continue
                primitive_actions = reach.path(end) or []
                edge = {
                    "kind": "move",
                    "itemIndex": idx,
                    "from": source,
                    "to": end,
                    "actions": primitive_actions,
                    "firstTrip": current is None,
                }
                parent[new_key] = (key, edge)
                # Charge a setup fee for choosing a temporary park pose.
                # This prefers one continuous carry to the final pose while
                # retaining every reachable park pose for difficult orders.
                park_penalty = 1 if greedy else (0 if end == goals[item.id] else 8)
                ng = g + max(1, len(primitive_actions)) + park_penalty
                gscore[new_key] = ng
                h = _heuristic(ctx, new_poses, mask, goals)
                priority = h if greedy else ng + h
                heapq.heappush(heap, (priority, ng, next(counter), new_poses, mask))

    return {
        "feasible": False,
        "violations": [{"code": "NO_PIECEWISE_PLAN", "message": "exhausted all small-grid states"}],
        "explored": explored,
    }


def _finish_piecewise(
    ctx: OrderContext,
    fp: FloorPlan,
    parent,
    goal_key,
    explored: int,
    seat_pose: Placement,
    goals: Dict[str, Placement],
) -> Dict[str, Any]:
    edges: List[Dict[str, Any]] = []
    cur = goal_key
    while parent[cur][0] is not None:
        prev, edge = parent[cur]
        edges.append(edge)
        cur = prev
    edges.reverse()

    step_no = itertools.count(1)
    steps: List[Dict[str, Any]] = []
    trip = 0
    trip_by_item: Dict[str, int] = {}
    for edge in edges:
        if edge["kind"] == "move":
            idx = edge["itemIndex"]
            item = ctx.items[idx]
            include_begin = edge.get("firstTrip") or item.id not in trip_by_item
            if include_begin:
                trip += 1
                trip_by_item[item.id] = trip
            this_trip = trip_by_item[item.id]
            steps.extend(
                _primitive_steps(
                    ctx,
                    idx,
                    edge["actions"],
                    edge["from"],
                    step_no,
                    this_trip,
                    include_begin=include_begin,
                )
            )
        else:
            ji = edge["jointIndex"]
            j = ctx.joints[ji]
            seat_idx = next(i for i, it in enumerate(ctx.items) if it.role == "seat")
            # Reconstruct current pose at join from goal? Parent edge doesn't
            # store state.  Join always occurs at final aligned pose in a
            # successful terminal path, so seat goal is correct.
            steps.append(_join_step(ctx, ji, seat_pose, step_no))

    final_check_id = f"S{next(step_no):03d}"
    steps.append(
        {
            "stepId": final_check_id,
            "kind": "final_check",
            "instruction": f"核对左右朝向为 {ctx.orientation}，所有连接面固件到位，清理包装",
            "expectedOrientation": ctx.orientation,
            "finalPlacements": {
                iid: placement_json(p) for iid, p in goals.items()
            },
        }
    )
    total_primitive = sum(1 for s in steps if s.get("kind") in ("translate", "rotate"))
    return {
        "feasible": True,
        "mode": "piecewise",
        "steps": steps,
        "seatPose": placement_json(seat_pose),
        "finalPlacements": {iid: placement_json(p) for iid, p in goals.items()},
        "macroStatesExplored": explored,
        "primitiveMoves": total_primitive,
        "trips": trip,
        "assemblyOrder": [
            ctx.joints[e["jointIndex"]].id for e in edges if e["kind"] == "join"
        ],
    }


def plan_whole_for_seat(
    ctx: OrderContext, fp: FloorPlan, seat_pose: Placement, goals: Dict[str, Placement]
) -> Dict[str, Any]:
    shape = _whole_shape(ctx)
    seat_item = next(i for i in ctx.items if i.role == "seat")
    source = ctx.request.get("wholeSourcePlacement") or ctx.request.get("sourcePlacement") or seat_item.source
    sj = placement_json(source)
    source_pose: Placement = (sj["pivot"][0], sj["pivot"][1], sj["orientation"])
    # Whole locked orientation must match target orientation; source may be
    # rotated by explicit quarter-turn templates.
    result = __import__("sofa_service.movement", fromlist=["plan_motion"]).plan_motion(
        fp, shape, source_pose, goal=seat_pose
    )
    if not result["feasible"]:
        return {
            "feasible": False,
            "mode": "whole",
            "violations": result.get("violations", []),
            "reachableCount": result.get("reachableCount"),
        }

    step_no = itertools.count(1)
    steps: List[Dict[str, Any]] = []
    begin_id = f"S{next(step_no):03d}"
    steps.append(
        {
            "stepId": begin_id,
            "kind": "begin_trip",
            "trip": 1,
            "itemId": "whole-kit",
            "sku": "whole-preinstalled-kit",
            "instruction": "整装件出电梯：成品左右朝向、预装组合与保护套已在备货时锁定，楼道不拆扶手",
            "at": placement_json(source_pose),
            "protectionCover": shape.metadata.get("protectionCover"),
        }
    )
    current = source_pose
    for act in result["actions"]:
        sid = f"S{next(step_no):03d}"
        to = act["to"]
        pose = (to["pivot"][0], to["pivot"][1], to["orientation"])
        step = {
            "stepId": sid,
            "kind": act["type"],
            "trip": 1,
            "itemId": "whole-kit",
            "sku": "whole-preinstalled-kit",
            **act,
            "occupiedAfter": [[c[0], c[1]] for c in sorted(shape.occupancy(pose))],
        }
        if act["type"] == "translate":
            dir_text = {(1, 0): "东", (-1, 0): "西", (0, 1): "南", (0, -1): "北"}.get(
                (act.get("dx"), act.get("dy")), "一格"
            )
            step["instruction"] = f"整装沙发：向{dir_text}平移一格"
        else:
            step["instruction"] = (
                f"整装沙发：以 {act.get('pivot')} 为支点"
                f"{'顺' if act.get('direction') == 'cw' else '逆'}时针转90度"
            )
        steps.append(step)
        current = pose

    for instruction in (
        "在装配区落位，检查锁定的成品左右朝向",
        "拆除外层保护套（保留到户易装件包装）",
        "客户易装工序：安装沙发脚并紧固预装位螺丝",
        "最终朝向与外观验收",
    ):
        sid = f"S{next(step_no):03d}"
        kind = "final_check" if instruction.startswith("最终") else "instruction"
        steps.append({"stepId": sid, "kind": kind, "instruction": instruction})

    return {
        "feasible": True,
        "mode": "whole",
        "steps": steps,
        "shape": shape.to_dict(),
        "craftLock": {
            "lockedFinalOrientation": ctx.orientation,
            "lockedSeatPose": placement_json(seat_pose),
            "preinstalledCombination": [i.sku for i in ctx.items],
            "protectionCover": shape.metadata.get("protectionCover"),
            "customerEasyInstall": (ctx.model.get("wholeUnit") or {}).get("customerInstall", []),
            "noCorridorDisassembly": True,
        },
        "seatPose": placement_json(seat_pose),
        "finalPlacements": {iid: placement_json(p) for iid, p in goals.items()},
        "primitiveMoves": len(result["actions"]),
        "trips": 1,
    }


def plan_delivery(
    request: Dict[str, Any],
    catalog: Dict[str, Any],
    floorplan: Any,
) -> Dict[str, Any]:
    fp = normalize_floorplan(floorplan) if isinstance(floorplan, dict) else floorplan
    ctx = normalize_order(request, catalog)
    candidates = candidate_seat_poses(ctx, fp)
    fitting: List[Tuple[Placement, Dict[str, Placement], List[Any]]] = []
    for sp in candidates:
        ok, goals, viols = final_layout_fits(ctx, sp, fp)
        if ok:
            fitting.append((sp, goals, viols))

    if not fitting:
        return {
            "feasible": False,
            "reason": "NO_FINAL_LAYOUT_IN_ASSEMBLY_ZONE",
            "violations": [v for _, _, vs in candidates for v in []]
            or [{"code": "NO_FINAL_LAYOUT", "message": "no ordered final layout fits assembly zone"}],
            "floorPlanHash": fp.geometry_hash(),
            "catalogHash": model_hash(ctx.model),
            "orderSnapshot": ctx.snapshot(),
        }

    mode = ctx.mode
    whole_plans = []
    piece_plans = []
    errors: Dict[str, Any] = {}

    for sp, goals, _ in fitting:
        if mode in ("auto", "whole"):
            wp = plan_whole_for_seat(ctx, fp, sp, goals)
            if wp.get("feasible"):
                whole_plans.append(wp)
            else:
                errors["whole"] = wp
        if mode in ("auto", "piecewise"):
            pp = plan_piecewise_for_seat(ctx, fp, sp, goals)
            if (
                not pp.get("feasible")
                and any(v.get("code") == "SEARCH_LIMIT" for v in pp.get("violations", []))
            ):
                # The quality-first search has a finite budget.  A greedy
                # ordering over the same exhaustive state graph can find a
                # convoluted but valid park/re-park sequence much sooner.
                pp = plan_piecewise_for_seat(
                    ctx, fp, sp, goals, max_states=25000, greedy=True
                )
            if pp.get("feasible"):
                piece_plans.append(pp)
            else:
                errors["piecewise"] = pp
        # In an explicit mode one candidate is usually enough; in auto keep the
        # first candidate for which at least one replayable mode exists.
        if mode == "whole" and whole_plans:
            break
        if mode == "piecewise" and piece_plans:
            break
        if mode == "auto" and (whole_plans or piece_plans):
            break

    selected: Optional[Dict[str, Any]] = None
    alternatives: List[Dict[str, Any]] = []
    if mode == "whole":
        selected = min(whole_plans, key=lambda p: p["primitiveMoves"], default=None)
    elif mode == "piecewise":
        selected = min(piece_plans, key=lambda p: (p["primitiveMoves"], p["trips"]), default=None)
    else:
        # Shop policy: prefer fewer trips / whole-unit craft when it fits;
        # otherwise choose the first exhaustive piecewise solution.
        if whole_plans:
            selected = min(whole_plans, key=lambda p: p["primitiveMoves"])
            alternatives.extend(piece_plans[:1])
        elif piece_plans:
            selected = min(piece_plans, key=lambda p: (p["primitiveMoves"], p["trips"]))

    if selected is None:
        return {
            "feasible": False,
            "reason": "NO_REPLAYABLE_PLAN",
            "modeRequested": mode,
            "modeErrors": errors,
            "floorPlanHash": fp.geometry_hash(),
            "catalogHash": model_hash(ctx.model),
            "orderSnapshot": ctx.snapshot(),
        }

    selected = dict(selected)
    selected.update(
        {
            "planId": request.get("planId"),
            "modelId": ctx.model_id,
            "orientation": ctx.orientation,
            "floorPlanId": fp.id,
            "floorPlanHash": fp.geometry_hash(),
            "catalogRevision": catalog.get("revision"),
            "catalogHash": model_hash(ctx.model),
            "orderSnapshot": ctx.snapshot(),
            "alternativeModesFeasible": {
                "whole": bool(whole_plans),
                "piecewise": bool(piece_plans),
            },
        }
    )
    return selected
