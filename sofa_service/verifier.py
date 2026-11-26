"""Deterministic replay and re-validation of issued step sheets."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .floorplan import FloorPlan, normalize_floorplan
from .geometry import Cell, Placement, Shape, normalize_shape, placement_json
from .movement import pose_violations, rotation_violations
from .ordering import joint_clear_cells, joint_target_pose, normalize_order


def _shape_map(sheet: Dict[str, Any], ctx=None) -> Dict[str, Shape]:
    shapes: Dict[str, Shape] = {}
    if ctx is not None:
        for item in ctx.items:
            shapes[item.id] = item.shape
    snapshot = sheet.get("orderSnapshot") or {}
    for item in snapshot.get("items", []):
        shapes.setdefault(item["id"], normalize_shape(item["shape"]))
    if sheet.get("mode") == "whole" or any(s.get("itemId") == "whole-kit" for s in sheet.get("steps", [])):
        if sheet.get("shape"):
            shapes["whole-kit"] = normalize_shape(sheet["shape"])
        else:
            # Reconstruct the whole kit from snapshot final relative cells is
            # not needed for service-created sheets because shape is stored.
            cells: Set[Cell] = set()
            for item in snapshot.get("items", []):
                sh = normalize_shape(item["shape"])
                # Catalog frame goal offsets are embedded in joints; the
                # service normally stores sheet.shape explicitly.
            spec = {"key": "whole-kit", "cells": []}
            shapes["whole-kit"] = normalize_shape(spec)
    return shapes


def _placement(p: Dict[str, Any]) -> Placement:
    pj = placement_json(p)
    return pj["pivot"][0], pj["pivot"][1], pj["orientation"]


def _occupied_except(
    poses: Dict[str, Optional[Placement]], shapes: Dict[str, Shape], active: Optional[str]
) -> Set[Cell]:
    out: Set[Cell] = set()
    for iid, pose in poses.items():
        if iid == active or pose is None or iid not in shapes:
            continue
        out.update(shapes[iid].occupancy(pose))
    return out


def replay_step_sheet(
    sheet: Dict[str, Any],
    floorplan: Any,
    catalog: Optional[Dict[str, Any]] = None,
    order_request: Optional[Dict[str, Any]] = None,
    strict_snapshot: bool = True,
) -> Dict[str, Any]:
    fp: FloorPlan = normalize_floorplan(floorplan) if isinstance(floorplan, dict) else floorplan
    ctx = None
    snapshot_mismatches: List[Dict[str, Any]] = []
    if catalog is not None and order_request is not None:
        try:
            ctx = normalize_order(order_request, catalog)
            current_snapshot = ctx.snapshot()
            old = sheet.get("orderSnapshot") or {}
            if old.get("hash") and old.get("hash") != current_snapshot.get("hash"):
                snapshot_mismatches.append(
                    {
                        "code": "ORDER_SNAPSHOT_MISMATCH",
                        "message": "order/catalog craft changed; the old step sheet is not authoritative",
                        "oldHash": old.get("hash"),
                        "currentHash": current_snapshot.get("hash"),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - report invalid request as violation
            snapshot_mismatches.append({"code": "CURRENT_ORDER_INVALID", "message": str(exc)})

    shapes = _shape_map(sheet, ctx)
    poses: Dict[str, Optional[Placement]] = {iid: None for iid in shapes}
    joints: Set[str] = set()
    step_results: List[Dict[str, Any]] = []
    violations: List[Dict[str, Any]] = list(snapshot_mismatches) if strict_snapshot else []
    first_invalid_index: Optional[int] = None

    old_fp_hash = sheet.get("floorPlanHash")
    current_fp_hash = fp.geometry_hash()
    environment_changed = bool(old_fp_hash and old_fp_hash != current_fp_hash)

    current_joint_rules: Dict[str, Any] = {}
    if ctx is not None:
        for j in ctx.joints:
            current_joint_rules[j.id] = j.rule
            current_joint_rules[j.rule_id] = j.rule

    for index, step in enumerate(sheet.get("steps", []), start=1):
        sid = step.get("stepId", f"S{index:03d}")
        kind = step.get("kind")
        item_id = step.get("itemId")
        step_violations: List[Dict[str, Any]] = []

        try:
            if kind == "begin_trip":
                assert item_id in shapes, f"unknown item {item_id}"
                pose = _placement(step["at"])
                obstacles = _occupied_except(poses, shapes, item_id)
                pvs = pose_violations(fp, shapes[item_id], pose, obstacles)
                if pvs:
                    step_violations.extend(v.to_dict() for v in pvs)
                else:
                    poses[item_id] = pose

            elif kind in ("translate", "rotate"):
                assert item_id in shapes, f"unknown item {item_id}"
                if poses.get(item_id) is None:
                    # Tolerate sheets without an explicit begin_trip by using
                    # the action's `from` as the materialized source.
                    poses[item_id] = _placement(step["from"])
                current = poses[item_id]
                expected_from = _placement(step["from"])
                if current != expected_from:
                    step_violations.append(
                        {
                            "code": "REPLAY_STATE_MISMATCH",
                            "message": "step source does not match replayed state",
                            "expected": placement_json(current),
                            "stepFrom": placement_json(expected_from),
                        }
                    )
                else:
                    obstacles = _occupied_except(poses, shapes, item_id)
                    if kind == "translate":
                        dx = int(step["dx"])
                        dy = int(step["dy"])
                        nxt = (current[0] + dx, current[1] + dy, current[2])
                        pvs = pose_violations(fp, shapes[item_id], nxt, obstacles)
                        if pvs:
                            step_violations.extend(v.to_dict() for v in pvs)
                        else:
                            poses[item_id] = nxt
                    else:
                        direction = step.get("direction", "cw")
                        nxt, rvs, swept = rotation_violations(
                            fp, shapes[item_id], current, direction, obstacles
                        )
                        if rvs:
                            step_violations.extend(v.to_dict() for v in rvs)
                        else:
                            expected_to = _placement(step["to"])
                            if nxt != expected_to:
                                step_violations.append(
                                    {
                                        "code": "TEMPLATE_TARGET_MISMATCH",
                                        "message": "template rotation result differs from sheet",
                                        "actual": placement_json(nxt),
                                        "expected": placement_json(expected_to),
                                    }
                                )
                            else:
                                poses[item_id] = nxt

            elif kind == "park":
                if item_id in shapes and step.get("at"):
                    expected = _placement(step["at"])
                    if poses.get(item_id) != expected:
                        step_violations.append(
                            {
                                "code": "PARK_POSE_MISMATCH",
                                "message": "park pose does not match replayed state",
                                "actual": placement_json(poses.get(item_id)) if poses.get(item_id) else None,
                                "expected": placement_json(expected),
                            }
                        )

            elif kind == "join":
                jid = step.get("jointInstanceId")
                host_id = step.get("hostItemId")
                member_id = step.get("memberItemId")
                rule = current_joint_rules.get(jid) or current_joint_rules.get(step.get("ruleId"))
                if rule is None:
                    # Use the rule embedded in the old snapshot.
                    for j in (sheet.get("orderSnapshot") or {}).get("joints", []):
                        if j.get("id") == jid or j.get("ruleId") == step.get("ruleId"):
                            rule = j.get("rule")
                            break
                if rule is None:
                    step_violations.append({"code": "UNKNOWN_JOINT_RULE", "jointId": jid})
                else:
                    hp = poses.get(host_id)
                    mp = poses.get(member_id)
                    if hp is None or mp is None:
                        step_violations.append({"code": "JOINT_PARTS_NOT_PRESENT", "jointId": jid})
                    else:
                        member_sku = ctx.item_by_id[member_id].sku if ctx is not None and member_id in ctx.item_by_id else None
                        required = joint_target_pose(hp, rule, member_sku)
                        if mp != required:
                            step_violations.append(
                                {
                                    "code": "JOINT_ALIGNMENT",
                                    "jointId": jid,
                                    "required": placement_json(required),
                                    "actual": placement_json(mp),
                                }
                            )
                        clear = joint_clear_cells(hp, rule)
                        occupied = _occupied_except(poses, shapes, None)
                        for c in clear:
                            if not fp.in_bounds(c):
                                step_violations.append(
                                    {"code": "CLEARANCE_OUT_OF_BOUNDS", "cell": [c[0], c[1]]}
                                )
                            elif c in fp.blocked:
                                step_violations.append(
                                    {"code": "CLEARANCE_BLOCKED", "cell": [c[0], c[1]]}
                                )
                            elif c in occupied:
                                owner = None
                                for iid, pose in poses.items():
                                    if pose and c in shapes[iid].occupancy(pose):
                                        owner = iid
                                        break
                                step_violations.append(
                                    {
                                        "code": "CLEARANCE_OCCUPIED",
                                        "cell": [c[0], c[1]],
                                        "occupiedBy": owner,
                                        "jointId": jid,
                                    }
                                )
                        if not any(v["code"].startswith(("JOINT_", "CLEARANCE_")) for v in step_violations):
                            joints.add(jid)

            elif kind == "final_check":
                expected = step.get("finalPlacements") or {}
                for iid, p in expected.items():
                    if iid in poses and poses[iid] != _placement(p):
                        step_violations.append(
                            {
                                "code": "FINAL_POSE_MISMATCH",
                                "itemId": iid,
                                "expected": p,
                                "actual": placement_json(poses[iid]) if poses[iid] else None,
                            }
                        )
                if step.get("expectedOrientation") and sheet.get("orientation"):
                    if step["expectedOrientation"] != sheet["orientation"]:
                        step_violations.append({"code": "ORIENTATION_MISMATCH"})

            elif kind in ("instruction", "note"):
                pass
            else:
                step_violations.append({"code": "UNKNOWN_STEP_KIND", "kind": kind})

        except AssertionError as exc:
            step_violations.append({"code": "REPLAY_ASSERTION", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            step_violations.append({"code": "REPLAY_ERROR", "message": str(exc)})

        result = {
            "index": index,
            "stepId": sid,
            "kind": kind,
            "valid": not step_violations,
            "violations": step_violations,
        }
        step_results.append(result)
        if step_violations:
            for v in step_violations:
                vv = dict(v)
                vv.setdefault("stepIndex", index)
                vv["stepId"] = sid
                violations.append(vv)
            if first_invalid_index is None:
                first_invalid_index = index

    valid = not violations
    return {
        "valid": valid,
        "firstInvalidStepIndex": first_invalid_index,
        "violations": violations,
        "stepResults": step_results,
        "replayedSteps": len(sheet.get("steps", [])),
        "environmentChanged": environment_changed,
        "floorPlanHash": fp.geometry_hash(),
        "sheetFloorPlanHash": old_fp_hash,
        "strictSnapshot": strict_snapshot,
        "finalPoses": {iid: placement_json(p) if p else None for iid, p in poses.items()},
        "joints": sorted(joints),
    }
