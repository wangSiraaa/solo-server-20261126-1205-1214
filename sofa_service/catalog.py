"""Built-in custom sofa catalog and shop assembly craft."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict

from .geometry import normalize_shape

BAR_SWEEP = [[0, 0], [1, 0], [1, 1], [0, 1]]
BAR_PHASES = [
    [[0, 0], [1, 0]],
    [[0, 0], [1, 0], [1, 1], [0, 1]],
    [[0, 0], [0, 1]],
]


def bar_component(sku: str, kind: str, name: str) -> Dict[str, Any]:
    return {
        "sku": sku,
        "kind": kind,
        "name": name,
        "cells": [[0, 0], [1, 0]],
        "fulcrum": [0, 0],
        "rotationTemplates": [
            {
                "from": 0,
                "direction": "cw",
                "pivot": [0, 0],
                "swept": BAR_SWEEP,
                "phases": BAR_PHASES,
            }
        ],
        "completeTemplates": True,
    }


def default_catalog() -> Dict[str, Any]:
    """Return catalog model ``yunwu-two-seat``.

    Coordinates in craft offsets are relative to the seat pivot in catalog
    orientation.  y decreases toward the back of the sofa.  With the back on
    the north side, a seated person faces +y; their left is +x.
    """
    seat = bar_component("seat-2", "seat", "双格座体")
    back = bar_component("back-detachable", "back", "可拆长条靠背")

    arm_standard = {
        "sku": "arm-std",
        "kind": "arm",
        "name": "标准扶手",
        "cells": [[0, 0]],
        "fulcrum": [0, 0],
        "rotationTemplates": [
            {"from": 0, "direction": "cw", "pivot": [0, 0], "swept": [[0, 0]]}
        ],
        "completeTemplates": True,
    }
    arm_lug = {
        "sku": "arm-lug",
        "kind": "arm",
        "name": "带凸耳扶手",
        # In catalog orientation the lug is the +x cell.  Side/order placement
        # rotates the arm so the lug ends on the permitted rear/outer face.
        "cells": [[0, 0], [1, 0]],
        "fulcrum": [0, 0],
        "rotationTemplates": [
            {
                "from": 0,
                "direction": "cw",
                "pivot": [0, 0],
                "swept": BAR_SWEEP,
                "phases": BAR_PHASES,
            }
        ],
        "completeTemplates": True,
        "metadata": {"hasLug": True, "lugCells": [[1, 0]]},
    }

    model = {
        "id": "yunwu-two-seat",
        "revision": "craft-v1",
        "name": "云雾双座定制沙发",
        "maxComponentKinds": 3,
        "componentKinds": ["seat", "back", "arm"],
        "components": {
            "seat-2": seat,
            "back-detachable": back,
            "arm-std": arm_standard,
            "arm-lug": arm_lug,
        },
        "allowedCombinations": [
            {
                "seat": ["seat-2"],
                "back": ["back-detachable"],
                "arm:left": ["arm-std", "arm-lug", None],
                "arm:right": ["arm-std", "arm-lug", None],
            }
        ],
        "installationFaces": ["N", "E", "W"],
        "craft": {
            "version": "craft-v1",
            "joints": [
                {
                    "id": "back-to-seat",
                    "hostRole": "seat",
                    "memberRole": "back",
                    "face": "N",
                    "memberPivot": [0, -1],
                    "memberOrientation": 0,
                    # Access/clearance cell at the back-left corner.
                    "clearCells": [[-1, -1]],
                    "fasteners": 2,
                },
                {
                    "id": "left-arm-to-seat",
                    "hostRole": "seat",
                    "memberRole": "arm:left",
                    "face": "E",
                    # Seated-left is world +x because the sitter faces +y.
                    # A lug arm is rotated to o=3 so its lug points rear/north;
                    # the one-cell standard arm stays orientation-invariant.
                    "memberPivot": [2, 0],
                    "memberOrientation": 0,
                    "orientationBySku": {"arm-lug": 3},
                    "clearCells": [],
                    "fasteners": 1,
                },
                {
                    "id": "right-arm-to-seat",
                    "hostRole": "seat",
                    "memberRole": "arm:right",
                    "face": "W",
                    "memberPivot": [-1, 0],
                    "memberOrientation": 0,
                    "orientationBySku": {"arm-lug": 3},
                    "clearCells": [],
                    "fasteners": 1,
                },
            ],
            "orientationConvention": (
                "Back is north; sitter faces south/+y. "
                "orientation=left puts the ordered feature on world +x."
            ),
        },
        "wholeUnit": {
            "protectionCover": "breathable-cover-A",
            "lockedTransportOrientation": 0,
            "preinstalled": ["seat", "back", "arm:left", "arm:right"],
            "customerInstall": ["screw legs", "remove outer wrap"],
            # Explicit conservative quarter-turn sweep around seat pivot.
            "baseCwSweep": [
                [-1, -1], [0, -1], [1, -1],
                [-1, 0], [0, 0], [1, 0], [2, 0],
                [-1, 1], [0, 1], [1, 1], [2, 1],
                [0, 2], [1, 2],
            ],
        },
        "pricing": {
            "baseCny": 399,
            "wholeCny": 260,
            "piecewiseFirstTripCny": 180,
            "piecewiseAdditionalTripCny": 70,
            "stepCny": 2,
        },
    }
    catalog = {"id": "shop-default", "revision": "catalog-v1", "models": {model["id"]: model}}
    return catalog


def catalog_hash(catalog: Dict[str, Any]) -> str:
    blob = json.dumps(catalog, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def model_hash(model: Dict[str, Any]) -> str:
    blob = json.dumps(model, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def normalized_component(model: Dict[str, Any], sku: str):
    if sku not in model["components"]:
        raise KeyError(f"unknown component SKU {sku}")
    return normalize_shape(model["components"][sku])


def revised_clearance_catalog(
    catalog: Dict[str, Any], model_id: str, clear_cells, joint_id: str = "back-to-seat"
) -> Dict[str, Any]:
    """Return a new catalog revision with a changed required clearance face."""
    out = copy.deepcopy(catalog)
    model = out["models"][model_id]
    for joint in model["craft"]["joints"]:
        if joint["id"] == joint_id:
            joint["clearCells"] = [list(c) for c in clear_cells]
            joint["craftChangedAt"] = "next-revision"
    old = model.get("revision", "craft-v1")
    model["craft"]["version"] = old + "+clearance"
    model["revision"] = old + "+clearance"
    out["revision"] = out.get("revision", "catalog") + "+clearance"
    return out
