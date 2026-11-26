"""Order normalization, craft joints and final left/right target poses."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .catalog import normalized_component
from .geometry import Cell, Placement, PlanError, _placement_tuple, add, rotate_cell, rotate_set


@dataclass
class Item:
    id: str
    role: str
    sku: str
    name: str
    shape: Any
    source: Placement
    side: Optional[str] = None
    metadata: Dict[str, Any] = None

    def to_snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "side": self.side,
            "sku": self.sku,
            "name": self.name,
            "source": {"pivot": [self.source[0], self.source[1]], "orientation": self.source[2]},
            "shape": self.shape.to_dict(),
            "metadata": self.metadata or {},
        }


@dataclass
class JointInstance:
    id: str
    rule_id: str
    host: str
    member: str
    rule: Dict[str, Any]


@dataclass
class OrderContext:
    request: Dict[str, Any]
    catalog: Dict[str, Any]
    model: Dict[str, Any]
    model_id: str
    orientation: str
    mode: str
    items: List[Item]
    joints: List[JointInstance]
    target_seat: Optional[Placement]
    assembly_zone_required: bool = True

    @property
    def item_by_id(self) -> Dict[str, Item]:
        return {i.id: i for i in self.items}

    def snapshot(self) -> Dict[str, Any]:
        body = {
            "modelId": self.model_id,
            "modelRevision": self.model.get("revision"),
            "catalogRevision": self.catalog.get("revision"),
            "orientation": self.orientation,
            "mode": self.mode,
            "items": [i.to_snapshot() for i in self.items],
            "joints": [
                {"id": j.id, "ruleId": j.rule_id, "host": j.host, "member": j.member, "rule": j.rule}
                for j in self.joints
            ],
            "targetSeat": (
                {"pivot": [self.target_seat[0], self.target_seat[1]], "orientation": self.target_seat[2]}
                if self.target_seat
                else None
            ),
        }
        body["hash"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        return body


def _combination_matches(rule: Dict[str, Any], items: List["Item"]) -> bool:
    """Match one catalog combination rule.

    Keys may be exact roles (``arm:left``) or component kinds (``arm``).
    A JSON ``null`` in the allowed SKU list means that role may be absent.
    """
    role_keys = {"seat", "back", "arm", "arm:left", "arm:right"}
    for key, allowed in rule.items():
        if key not in role_keys and not key.startswith("role:"):
            continue
        lookup = key[5:] if key.startswith("role:") else key
        matching = [
            i
            for i in items
            if i.role == lookup
            or (lookup == "arm" and i.shape.kind == "arm")
            or (":" not in lookup and i.shape.kind == lookup)
        ]
        allowed_set = {sku for sku in (allowed or []) if sku is not None}
        absence_allowed = allowed is None or None in (allowed or [])
        if not matching:
            if not absence_allowed and allowed_set:
                return False
            continue
        for item in matching:
            if item.sku not in allowed_set:
                return False
    return True


def _source_for(request: Dict[str, Any], role: str, item_id: str) -> Placement:
    sources = request.get("sources") or {}
    raw = (
        sources.get(item_id)
        or sources.get(role)
        or request.get("sourcePlacement")
        or {"pivot": request.get("sourcePivot", [0, 0]), "orientation": request.get("sourceOrientation", 0)}
    )
    p, o = _placement_tuple(raw)
    return p[0], p[1], o


def normalize_order(request: Dict[str, Any], catalog: Dict[str, Any]) -> OrderContext:
    model_id = str(request.get("modelId", "yunwu-two-seat"))
    if model_id not in catalog.get("models", {}):
        raise PlanError(f"catalog has no model {model_id}")
    model = catalog["models"][model_id]

    orientation = str(request.get("orientation", request.get("facing", "right"))).lower()
    if orientation not in ("left", "right"):
        raise PlanError("orientation must be 'left' or 'right'")
    mode = str(request.get("mode", "auto")).lower()
    if mode not in ("auto", "whole", "piecewise", "split"):
        raise PlanError("mode must be auto, whole or piecewise")
    if mode == "split":
        mode = "piecewise"

    items: List[Item] = []
    raw_items = request.get("items")
    if raw_items:
        for n, ri in enumerate(raw_items):
            role = str(ri["role"])
            sku = str(ri.get("sku") or ri.get("componentSku"))
            shape = normalized_component(model, sku)
            iid = str(ri.get("id") or f"{role}-{n+1}")
            side = ri.get("side") or (role.split(":", 1)[1] if ":" in role else None)
            items.append(
                Item(
                    id=iid,
                    role=role,
                    side=side,
                    sku=sku,
                    name=shape.name or sku,
                    shape=shape,
                    source=_source_for({**request, **ri}, role, iid),
                    metadata=ri.get("metadata", {}),
                )
            )
    else:
        seat_sku = request.get("seatSku", "seat-2")
        back_sku = request.get("backSku", "back-detachable")
        feature_arm_sku = request.get("featureArmSku") or request.get("orientationArmSku")
        if feature_arm_sku:
            other_arm_sku = request.get("otherArmSku", "arm-std")
            if orientation == "left":
                left_sku = feature_arm_sku
                right_sku = request.get("rightArmSku", other_arm_sku)
            else:
                right_sku = feature_arm_sku
                left_sku = request.get("leftArmSku", other_arm_sku)
        else:
            left_sku = request.get("leftArmSku", "arm-std")
            right_sku = request.get("rightArmSku", "arm-std")
        specs = [
            ("seat", "seat1", seat_sku, None),
            ("back", "back1", back_sku, None),
        ]
        if left_sku:
            specs.append(("arm:left", "armLeft", left_sku, "left"))
        if right_sku:
            specs.append(("arm:right", "armRight", right_sku, "right"))
        for role, iid, sku, side in specs:
            shape = normalized_component(model, sku)
            items.append(
                Item(
                    id=iid,
                    role=role,
                    sku=sku,
                    name=shape.name or sku,
                    shape=shape,
                    source=_source_for(request, role, iid),
                    side=side,
                    metadata={},
                )
            )

    roles = {i.role for i in items}
    if "seat" not in roles:
        raise PlanError("order must contain a seat")
    if "back" not in roles:
        raise PlanError("order must contain a detachable back")
    kinds = {i.shape.kind for i in items}
    if len(kinds) > int(model.get("maxComponentKinds", 3)):
        raise PlanError("component kind limit exceeded")

    # Validate allowed combinations.  The default catalog permits known SKUs.
    valid_skus = set(model.get("components", {}).keys())
    for i in items:
        if i.sku not in valid_skus:
            raise PlanError(f"SKU {i.sku} is not allowed for model {model_id}")

    # Validate allowed combinations supplied by the catalog.
    combinations = model.get("allowedCombinations") or model.get("allowed_combinations")
    if combinations:
        if not any(_combination_matches(rule, items) for rule in combinations):
            raise PlanError("selected SKUs are not in any allowed shop combination")

    seat = next(i for i in items if i.role == "seat")
    joints: List[JointInstance] = []
    for rule in model.get("craft", {}).get("joints", []):
        host_role = rule["hostRole"]
        member_role = rule["memberRole"]
        hosts = [i for i in items if i.role == host_role]
        members = [i for i in items if i.role == member_role]
        if not hosts or not members:
            continue
        # The built-in model has one seat and one member per side.
        host = hosts[0]
        member = members[0]
        joints.append(
            JointInstance(
                id=f'{rule["id"]}:{member.id}',
                rule_id=rule["id"],
                host=host.id,
                member=member.id,
                rule=rule,
            )
        )

    installation_faces = set(model.get("installationFaces", []) or [])
    for rule in model.get("craft", {}).get("joints", []):
        face = rule.get("face")
        if installation_faces and face and face not in installation_faces:
            raise PlanError(f"installation face {face} is not available for model {model_id}")

    target_seat = None
    if request.get("targetSeatPlacement") or request.get("targetSeat"):
        raw_target = request.get("targetSeatPlacement") or request.get("targetSeat")
        p, o = _placement_tuple(raw_target)
        target_seat = (p[0], p[1], o)

    return OrderContext(
        request=request,
        catalog=catalog,
        model=model,
        model_id=model_id,
        orientation=orientation,
        mode=mode,
        items=items,
        joints=joints,
        target_seat=target_seat,
        assembly_zone_required=bool(request.get("assemblyZoneRequired", True)),
    )


def joint_target_pose(seat_pose: Placement, rule: Dict[str, Any], member_sku: Optional[str] = None) -> Placement:
    px, py, so = seat_pose
    off = rule.get("memberPivot", [0, 0])
    rotated = rotate_cell((int(off[0]), int(off[1])), so)
    relative_o = int(rule.get("memberOrientation", 0))
    by_sku = rule.get("orientationBySku") or {}
    if member_sku is not None and member_sku in by_sku:
        relative_o = int(by_sku[member_sku])
    mo = (so + relative_o) % 4
    return px + rotated[0], py + rotated[1], mo


def joint_clear_cells(seat_pose: Placement, rule: Dict[str, Any]) -> List[Cell]:
    px, py, so = seat_pose
    out = []
    for c in rule.get("clearCells", []):
        rc = rotate_cell((int(c[0]), int(c[1])), so)
        out.append((px + rc[0], py + rc[1]))
    return out


def goal_poses_for_seat(ctx: OrderContext, seat_pose: Placement) -> Dict[str, Placement]:
    """Derive every item's final pose from craft joint offsets."""
    seat_id = next(i.id for i in ctx.items if i.role == "seat")
    goals: Dict[str, Placement] = {seat_id: seat_pose}
    for j in ctx.joints:
        # All built-in joints are hosted by the seat.  Resolve transitively if
        # future craft rules host another already-targeted item.
        host_goal = goals.get(j.host)
        if host_goal is None:
            host_item = ctx.item_by_id[j.host]
            if host_item.role == "seat":
                host_goal = seat_pose
            else:
                continue
        goals[j.member] = joint_target_pose(
            host_goal, j.rule, ctx.item_by_id[j.member].sku
        )
    # If a joint host was not yet known, make a second pass.
    for _ in range(3):
        for j in ctx.joints:
            if j.member in goals:
                continue
            host_goal = goals.get(j.host)
            if host_goal is None:
                continue
            goals[j.member] = joint_target_pose(host_goal, j.rule, ctx.item_by_id[j.member].sku)
    return goals


def candidate_seat_poses(ctx: OrderContext, fp) -> List[Placement]:
    if ctx.target_seat is not None:
        return [ctx.target_seat]
    zone = fp.zones.get("assembly", frozenset())
    orientations = ctx.request.get("targetOrientations", [0, 1, 2, 3])
    if isinstance(orientations, int):
        orientations = [orientations]
    out: List[Placement] = []
    for o in orientations:
        for c in sorted(zone):
            out.append((c[0], c[1], int(o) % 4))
    return out


def final_layout_fits(ctx: OrderContext, seat_pose: Placement, fp) -> Tuple[bool, Dict[str, Placement], List[Any]]:
    goals = goal_poses_for_seat(ctx, seat_pose)
    violations = []
    item_by_id = ctx.item_by_id
    all_cells = set()
    for iid, pose in goals.items():
        item = item_by_id[iid]
        occ = item.shape.occupancy(pose)
        for c in occ:
            if not fp.in_bounds(c):
                violations.append({"code": "FINAL_OUT_OF_BOUNDS", "cell": [c[0], c[1]]})
            elif c in fp.blocked:
                violations.append({"code": "FINAL_BLOCKED", "cell": [c[0], c[1]]})
            elif ctx.assembly_zone_required and fp.zones.get("assembly") and c not in fp.zones["assembly"]:
                violations.append({"code": "NOT_IN_ASSEMBLY_ZONE", "cell": [c[0], c[1]]})
            if c in all_cells:
                violations.append({"code": "FINAL_OVERLAP", "cell": [c[0], c[1]]})
            all_cells.add(c)
    # Every order item needs a derived goal.
    for item in ctx.items:
        if item.id not in goals:
            violations.append({"code": "NO_GOAL_POSE", "itemId": item.id})
    return not violations, goals, violations
