"""ASCII grid rendering for terminal replay."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .floorplan import normalize_floorplan
from .geometry import Cell, normalize_shape

GLYPH = {
    "seat1": "S",
    "back1": "B",
    "armLeft": "L",
    "armRight": "R",
    "whole-kit": "W",
}


def render_grid(
    floorplan: Any,
    occupied: Optional[Dict[str, Iterable[Cell]]] = None,
    sweep: Optional[Iterable[Cell]] = None,
    blocked_extra: Optional[Iterable[Cell]] = None,
) -> str:
    fp = normalize_floorplan(floorplan) if isinstance(floorplan, dict) else floorplan
    sweep_set = set(sweep or [])
    extra = set(blocked_extra or [])
    cell_owner: Dict[Cell, str] = {}
    for owner, cells in (occupied or {}).items():
        for c in cells:
            cell_owner[tuple(c)] = owner

    lines = []
    for y in range(fp.height):
        row = []
        for x in range(fp.width):
            c = (x, y)
            if c in cell_owner:
                owner = cell_owner[c]
                row.append(GLYPH.get(owner, owner[0].upper()))
            elif c in sweep_set:
                row.append("*")
            elif c in extra or c in fp.property_blocked:
                row.append("X")
            elif c in fp.walls:
                row.append("#")
            else:
                zone = fp.zone_at(c)
                row.append(
                    {
                        "elevator": "E",
                        "elevator_door": "D",
                        "corridor": ".",
                        "entry": "N",
                        "home": "H",
                        "assembly": "A",
                        "generic": " ",
                    }.get(zone or "", "?")
                )
        lines.append("".join(row))
    return "\n".join(lines)


def sheet_ascii_frames(sheet: Dict[str, Any], floorplan: Any) -> List[Dict[str, str]]:
    """Return one ASCII frame per geometric/park/join step."""
    fp = normalize_floorplan(floorplan) if isinstance(floorplan, dict) else floorplan
    shapes = {
        item["id"]: normalize_shape(item["shape"])
        for item in (sheet.get("orderSnapshot") or {}).get("items", [])
    }
    if sheet.get("shape"):
        shapes["whole-kit"] = normalize_shape(sheet["shape"])
    current: Dict[str, Set[Cell]] = {}
    frames = []
    for step in sheet.get("steps", []):
        iid = step.get("itemId")
        if step.get("kind") == "begin_trip" and iid in shapes and step.get("at"):
            at = step["at"]
            current[iid] = set(
                shapes[iid].occupancy((at["pivot"][0], at["pivot"][1], at.get("orientation", 0)))
            )
        elif step.get("occupiedAfter") and iid:
            current[iid] = {tuple(c) for c in step["occupiedAfter"]}
        occupied = {k: v for k, v in current.items()}
        sweep = None
        if step.get("sweptCells"):
            sweep = {tuple(c) for c in step["sweptCells"]}
        frames.append(
            {
                "stepId": step["stepId"],
                "instruction": step.get("instruction", step["kind"]),
                "grid": render_grid(fp, occupied, sweep),
            }
        )
    return frames
