"""Small grid floor plans (at most 6 x 6)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from .geometry import Cell, PlanError, cell, cell_json

ZONE_CHARS = {
    "E": "elevator",
    "D": "elevator_door",
    "C": "corridor",
    "N": "entry",
    "H": "home",
    "A": "assembly",
    ".": "generic",
    " ": "void",
}


@dataclass
class FloorPlan:
    id: str
    width: int
    height: int
    walls: FrozenSet[Cell]
    zones: Dict[str, FrozenSet[Cell]]
    property_blocked: FrozenSet[Cell] = frozenset()
    doorways: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    revision: int = 1
    name: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> FrozenSet[Cell]:
        return self.walls | self.property_blocked

    def in_bounds(self, c: Cell) -> bool:
        x, y = c
        return 0 <= x < self.width and 0 <= y < self.height

    def zone_at(self, c: Cell) -> Optional[str]:
        for zone, cs in self.zones.items():
            if c in cs:
                return zone
        return None

    def geometry_hash(self) -> str:
        payload = {
            "width": self.width,
            "height": self.height,
            "walls": sorted(list(c) for c in self.walls),
            "propertyBlocked": sorted(list(c) for c in self.property_blocked),
            "zones": {k: sorted(list(c) for c in v) for k, v in self.zones.items()},
            "doorways": self.doorways,
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        rows = []
        for y in range(self.height):
            line = []
            for x in range(self.width):
                c = (x, y)
                if c in self.walls:
                    line.append("#")
                else:
                    z = self.zone_at(c)
                    ch = "."
                    for candidate, name in ZONE_CHARS.items():
                        if name == z:
                            ch = candidate
                            break
                    line.append(ch)
            rows.append("".join(line))
        return {
            "id": self.id,
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "maxCells": 36,
            "rows": rows,
            "revision": self.revision,
            "geometryHash": self.geometry_hash(),
            "walls": [cell_json(c) for c in sorted(self.walls)],
            "propertyBlocked": [cell_json(c) for c in sorted(self.property_blocked)],
            "blocked": [cell_json(c) for c in sorted(self.blocked)],
            "zones": {k: [cell_json(c) for c in sorted(v)] for k, v in self.zones.items()},
            "doorways": self.doorways,
        }


def normalize_floorplan(data: Dict[str, Any]) -> FloorPlan:
    fp_id = str(data.get("id", "floorplan"))
    rows: Optional[Sequence[str]] = data.get("rows")
    width = int(data.get("width", 0))
    height = int(data.get("height", 0))

    walls: Set[Cell] = set(cells_from_any(data.get("walls", [])))
    blocked_explicit = set(cells_from_any(data.get("blocked", [])))
    property_blocked = set(cells_from_any(data.get("propertyBlocked", [])))

    zones: Dict[str, Set[Cell]] = {
        name: set(cells_from_any(cs))
        for name, cs in (data.get("zones") or {}).items()
    }

    # Tolerate cell-annotation plans: [{"x":2,"y":1,"zone":"corridor"}].
    for annotated in data.get("cells", []) if isinstance(data.get("cells"), list) else []:
        if not isinstance(annotated, dict):
            continue
        c = cell(annotated.get("pivot", [annotated.get("x"), annotated.get("y")]))
        if annotated.get("blocked") or annotated.get("wall") or annotated.get("type") == "wall":
            walls.add(c)
        zone_name = annotated.get("zone") or annotated.get("zoneName") or annotated.get("label")
        if zone_name in ("wall", "#", "blocked"):
            walls.add(c)
        elif zone_name:
            zone_name = ZONE_CHARS.get(str(zone_name), str(zone_name))
            zones.setdefault(zone_name, set()).add(c)

    # Tolerate a numeric/string matrix under "grid".
    grid = data.get("grid")
    if grid and not rows:
        height = height or len(grid)
        width = width or max(len(r) for r in grid)
        for y, row in enumerate(grid):
            for x, value in enumerate(row):
                c = (x, y)
                if value in ("#", 1, "wall"):
                    walls.add(c)
                elif isinstance(value, str) and value in ZONE_CHARS:
                    zone = ZONE_CHARS[value]
                    if zone != "void":
                        zones.setdefault(zone, set()).add(c)
                elif isinstance(value, dict):
                    if value.get("blocked"):
                        walls.add(c)
                    z = value.get("zone")
                    if z:
                        zones.setdefault(str(z), set()).add(c)

    if rows:
        height = height or len(rows)
        width = width or max(len(r) for r in rows)
        if len(rows) != height:
            raise PlanError("floorplan height does not match rows")
        for y, row in enumerate(rows):
            if len(row) != width:
                raise PlanError(f"row {y} has length {len(row)}, expected {width}")
            for x, ch in enumerate(row):
                if ch == "#":
                    walls.add((x, y))
                elif ch and ch != " ":
                    zone = ZONE_CHARS.get(ch)
                    if zone and zone != "void":
                        zones.setdefault(zone, set()).add((x, y))
    if width == 0 or height == 0:
        raise PlanError("floorplan must have rows or explicit width/height")
    if width > 6 or height > 6:
        raise PlanError("floor plans are limited to 6x6 cells")

    # Explicit "blocked" is treated as permanent wall unless caller separates.
    walls |= blocked_explicit - property_blocked
    zones = {k: frozenset(v) for k, v in zones.items()}  # type: ignore

    doorways = {}
    for d in data.get("doorways", []) if isinstance(data.get("doorways"), list) else []:
        did = str(d.get("id", "door"))
        doorways[did] = dict(d)
    if isinstance(data.get("doorways"), dict):
        for did, d in data["doorways"].items():
            doorways[str(did)] = dict(d)

    return FloorPlan(
        id=fp_id,
        name=data.get("name"),
        width=width,
        height=height,
        walls=frozenset(walls),
        zones=zones,
        property_blocked=frozenset(property_blocked),
        doorways=doorways,
        revision=int(data.get("revision", 1)),
        raw=dict(data),
    )


def cells_from_any(value: Any) -> List[Cell]:
    out: List[Cell] = []
    if not value:
        return out
    for v in value:
        if isinstance(v, dict):
            out.append(cell(v.get("pivot", [v.get("x"), v.get("y")])))
        else:
            out.append(cell(v))
    return out


def occupancy_map(
    placements: Dict[str, Tuple[Cell, int] | Dict[str, Any]],
    shapes: Dict[str, Any],
    exclude: Optional[str] = None,
) -> Dict[Cell, str]:
    """Map occupied cell -> item id.  Shapes may be Shape or normalized dict."""
    from .geometry import normalize_shape

    out: Dict[Cell, str] = {}
    for item_id, p in placements.items():
        if item_id == exclude or p is None:
            continue
        shape = shapes[item_id]
        if not isinstance(shape, Shape):
            shape = normalize_shape(shape)
        occ = shape.occupancy(p)
        for c in occ:
            out[c] = item_id
    return out
