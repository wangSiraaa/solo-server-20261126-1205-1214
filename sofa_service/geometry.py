"""Grid geometry and explicit rotation templates.

The simulation uses integer cells.  ``orientation`` is the number of visual
quarter-turns clockwise from the component's catalog orientation:

    0: east  (+x)
    1: south (+y)
    2: west
    3: north

Thus R_cw(x, y) = (-y, x) when y increases down the drawing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

Cell = Tuple[int, int]
Placement = Tuple[int, int, int]  # pivot x, pivot y, orientation


class PlanError(ValueError):
    """Invalid catalog, floor-plan or plan request."""


def cell(value: Sequence[Any]) -> Cell:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PlanError(f"cell must be [x, y], got {value!r}")
    return int(value[0]), int(value[1])


def cells(values: Iterable[Sequence[Any]]) -> FrozenSet[Cell]:
    return frozenset(cell(v) for v in values)


def cell_json(c: Cell) -> List[int]:
    return [int(c[0]), int(c[1])]


def rotate_cell(c: Cell, orientation: int) -> Cell:
    x, y = c
    o = orientation % 4
    if o == 0:
        return x, y
    if o == 1:  # visual clockwise in screen coordinates
        return -y, x
    if o == 2:
        return -x, -y
    return y, -x  # o == 3


def rotate_set(cs: Iterable[Cell], orientation: int) -> FrozenSet[Cell]:
    return frozenset(rotate_cell(c, orientation) for c in cs)


def add(c: Cell, d: Cell) -> Cell:
    return c[0] + d[0], c[1] + d[1]


def sub(c: Cell, d: Cell) -> Cell:
    return c[0] - d[0], c[1] - d[1]


def manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


@dataclass(frozen=True)
class Shape:
    key: str
    kind: str
    cells: FrozenSet[Cell]
    # "orientation:direction" -> every grid cell swept by that quarter turn.
    sweeps: Dict[str, FrozenSet[Cell]] = field(default_factory=dict)
    # Optional phase sets make replay output more explanatory.
    phases: Dict[str, Tuple[FrozenSet[Cell], ...]] = field(default_factory=dict)
    sku: Optional[str] = None
    name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def occupancy(self, placement: Placement | Dict[str, Any]) -> FrozenSet[Cell]:
        p, o = _placement_tuple(placement)
        return frozenset(add(p, rotate_cell(c, o)) for c in self.cells)

    def sweep(
        self, orientation: int, direction: str, pivot: Cell
    ) -> Optional[FrozenSet[Cell]]:
        d = "cw" if direction in ("cw", "clockwise") else "ccw"
        raw = self.sweeps.get(f"{int(orientation) % 4}:{d}")
        if raw is None:
            return None
        return frozenset(add(pivot, c) for c in raw)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "sku": self.sku or self.key,
            "kind": self.kind,
            "name": self.name,
            "cells": [cell_json(c) for c in sorted(self.cells)],
            "rotationSweeps": {
                k: sorted(cell_json(c) for c in v) for k, v in self.sweeps.items()
            },
            "rotationPhases": {
                k: [[cell_json(c) for c in sorted(ph)] for ph in phases]
                for k, phases in self.phases.items()
            },
            "metadata": self.metadata,
        }


def _placement_tuple(placement: Placement | Dict[str, Any]) -> Tuple[Cell, int]:
    if isinstance(placement, tuple):
        px, py, o = placement
        return (int(px), int(py)), int(o) % 4
    if isinstance(placement, dict):
        if "pivot" in placement:
            p = cell(placement["pivot"])
        else:
            p = (int(placement["x"]), int(placement["y"]))
        return p, int(placement.get("orientation", 0)) % 4
    raise PlanError(f"invalid placement {placement!r}")


def placement_json(placement: Placement | Dict[str, Any]) -> Dict[str, Any]:
    p, o = _placement_tuple(placement)
    return {"pivot": cell_json(p), "orientation": o}


def continuous_quarter_sweep(
    rel_cells: Iterable[Cell], samples: int = 12, conservative: bool = True
) -> FrozenSet[Cell]:
    """Rasterize a quarter turn.

    Catalogs normally provide the authoritative template directly.  This helper
    is used by the built-in catalog to materialize those explicit templates.
    The conservative mode also marks floor/ceil corner cells touched by a
    sampled endpoint.
    """
    out: set[Cell] = set()
    cs = list(rel_cells)
    for i in range(samples + 1):
        theta = (math.pi / 2.0) * i / samples
        co, si = math.cos(theta), math.sin(theta)
        for x, y in cs:
            xr = x * co - y * si
            yr = x * si + y * co
            candidates = {(round(xr), round(yr))}
            if conservative:
                for xx in (math.floor(xr), math.ceil(xr)):
                    for yy in (math.floor(yr), math.ceil(yr)):
                        candidates.add((xx, yy))
            out.update(candidates)
    return frozenset(out)


def materialize_quarter_templates(
    rel_cells: FrozenSet[Cell],
    base_cw_sweep: Optional[FrozenSet[Cell]] = None,
    base_phases: Optional[Tuple[FrozenSet[Cell], ...]] = None,
) -> Tuple[Dict[str, FrozenSet[Cell]], Dict[str, Tuple[FrozenSet[Cell], ...]]]:
    """Build all 8 direction/orientation templates from one 0:cw template."""
    if base_cw_sweep is None:
        base_cw_sweep = continuous_quarter_sweep(rel_cells)
    start = frozenset(rel_cells)
    end = rotate_set(rel_cells, 1)
    if base_phases is None:
        middle = base_cw_sweep - start - end
        if middle:
            base_phases = (start, middle, end)
        else:
            base_phases = (start, end)

    sweeps: Dict[str, FrozenSet[Cell]] = {}
    phases: Dict[str, Tuple[FrozenSet[Cell], ...]] = {}
    for o in range(4):
        cw = rotate_set(base_cw_sweep, o)
        sweeps[f"{o}:cw"] = cw
        phases[f"{o}:cw"] = tuple(rotate_set(p, o) for p in base_phases)
    for o in range(4):
        prev = (o - 1) % 4
        # A CCW turn from o to prev sweeps the same cells as the CW prev->o.
        sweeps[f"{o}:ccw"] = sweeps[f"{prev}:cw"]
        phases[f"{o}:ccw"] = tuple(reversed(phases[f"{prev}:cw"]))
    return sweeps, phases


def normalize_shape(data: Dict[str, Any]) -> Shape:
    key = str(data.get("key") or data.get("sku") or data.get("id") or "part")
    kind = str(data.get("kind", "part"))
    raw_cells = cells(data.get("cells", []))
    fulcrum = cell(data.get("fulcrum", [0, 0]))
    rel = frozenset(sub(c, fulcrum) for c in raw_cells)

    explicit = (
        data.get("rotationSweeps")
        or data.get("rotationTemplates")
        or data.get("rotationTemplate")
    )
    sweeps: Dict[str, FrozenSet[Cell]] = {}
    phases: Dict[str, Tuple[FrozenSet[Cell], ...]] = {}

    def ingest_template(k: str, value: Any) -> None:
        # Accept friendly keys "cw"/"ccw" as the catalog-orientation template.
        if k in ("cw", "clockwise"):
            k = "0:cw"
        elif k in ("ccw", "counterclockwise"):
            k = "0:ccw"
        if isinstance(value, dict):
            sweeps[k] = cells(value.get("swept", value.get("sweptCells", value.get("cells", []))))
            if "phases" in value:
                phases[k] = tuple(cells(p) for p in value["phases"])
        else:
            sweeps[k] = cells(value)

    if isinstance(explicit, dict) and ("swept" in explicit or "sweptCells" in explicit or "cells" in explicit):
        # A singular template object rather than a keyed mapping.
        o = int(explicit.get("from", explicit.get("fromOrientation", explicit.get("orientation", 0)))) % 4
        d = explicit.get("direction", "cw")
        d = "cw" if d in ("cw", "clockwise") else "ccw"
        ingest_template(f"{o}:{d}", explicit)
    elif isinstance(explicit, dict):
        for raw_key, value in explicit.items():
            key = str(raw_key)
            if ":" not in key and key in ("cw", "ccw", "clockwise", "counterclockwise"):
                key = "0:" + ("cw" if key in ("cw", "clockwise") else "ccw")
            ingest_template(key, value)
    elif isinstance(explicit, list):
        for tpl in explicit:
            o = int(tpl.get("from", tpl.get("fromOrientation", tpl.get("orientation", 0)))) % 4
            d = tpl.get("direction", "cw")
            d = "cw" if d in ("cw", "clockwise") else "ccw"
            k = f"{o}:{d}"
            ingest_template(k, tpl)

    # Catalogs are expected to provide templates.  For convenience custom
    # shapes may set autoRotationTemplate=true; the materialized raster is then
    # stored on the normalized shape and used as the authoritative template.
    if not sweeps and data.get("autoRotationTemplate", True):
        base = None
        if data.get("baseCwSweep"):
            base = cells(data["baseCwSweep"])
        sweeps, phases = materialize_quarter_templates(rel, base)
    elif "0:cw" in sweeps and len(sweeps) < 8 and data.get("completeTemplates", True):
        base = sweeps["0:cw"]
        sweeps, phases = materialize_quarter_templates(rel, base)

    return Shape(
        key=key,
        sku=data.get("sku", key),
        kind=kind,
        name=data.get("name"),
        cells=rel,
        sweeps=sweeps,
        phases=phases,
        metadata=dict(data.get("metadata", {})),
    )


def orientation_name(o: int) -> str:
    return ["east", "south", "west", "north"][int(o) % 4]
