"""Canonical readable scenarios used by tests and the demo."""

from __future__ import annotations

from typing import Any, Dict

from .catalog import default_catalog


def small_floorplan() -> Dict[str, Any]:
    return {
        "id": "small-smooth",
        "name": "六乘六开放小厅：电梯、门口、走廊、入户与装配区均顺畅",
        "width": 6,
        "height": 6,
        "rows": [
            "EE....",
            "EDCCCC",
            ".CCCCC",
            "..AA..",
            ".AAAAA",
            ".AAAAA",
        ],
        "doorways": [
            {"id": "elevator-door", "cell": [1, 1], "widthCm": 90},
            {"id": "home-entry", "cell": [1, 2], "widthCm": 92},
        ],
    }


def narrow_floorplan() -> Dict[str, Any]:
    return {
        "id": "narrow-l-turn",
        "name": "只能平放入门、中段必须转向的L形窄廊",
        "width": 6,
        "height": 6,
        "rows": [
            "E#C###",
            "EDCC##",
            "##CC##",
            "##C###",
            "##NAAA",
            "##AAAA",
        ],
        "doorways": [
            {"id": "elevator-door", "cell": [1, 1], "widthCm": 80},
            {"id": "entry-narrow", "cell": [2, 4], "widthCm": 78},
        ],
    }


def small_order(mode: str = "auto") -> Dict[str, Any]:
    return {
        "planId": "small-smooth-order",
        "modelId": "yunwu-two-seat",
        "orientation": "left",
        "mode": mode,
        "seatSku": "seat-2",
        "backSku": "back-detachable",
        "leftArmSku": "arm-std",
        "rightArmSku": "arm-std",
        "sourcePlacement": {"pivot": [0, 0], "orientation": 0},
        "wholeSourcePlacement": {"pivot": [1, 1], "orientation": 0},
        "targetSeatPlacement": {"pivot": [2, 4], "orientation": 0},
        "doorwayId": "home-entry",
    }


def narrow_order(
    mode: str = "piecewise",
    left_arm: str | None = "arm-std",
    right_arm: str | None = "arm-std",
    orientation: str = "right",
) -> Dict[str, Any]:
    return {
        "planId": "narrow-l-order",
        "modelId": "yunwu-two-seat",
        "orientation": orientation,
        "mode": mode,
        "seatSku": "seat-2",
        "backSku": "back-detachable",
        "leftArmSku": left_arm,
        "rightArmSku": right_arm,
        "sourcePlacement": {"pivot": [0, 1], "orientation": 0},
        "wholeSourcePlacement": {"pivot": [1, 1], "orientation": 0},
        "targetSeatPlacement": {"pivot": [3, 5], "orientation": 0},
        "doorwayId": "entry-narrow",
        "measurementVersionId": "m-door-v1",
    }


def seed_state() -> Dict[str, Any]:
    return {
        "catalog": default_catalog(),
        "floorplans": {
            "small-smooth": small_floorplan(),
            "narrow-l-turn": narrow_floorplan(),
        },
    }
