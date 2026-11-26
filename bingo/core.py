"""Core domain rules for the neighbourhood cloud-gala sticker backend.

The module is intentionally framework-free: persistence, HTTP and terminal code
are thin adapters around :class:`Backend`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LINE_DEFS: dict[str, list[tuple[int, int]]] = {
    "R1": [(0, 0), (0, 1), (0, 2)],
    "R2": [(1, 0), (1, 1), (1, 2)],
    "R3": [(2, 0), (2, 1), (2, 2)],
    "C1": [(0, 0), (1, 0), (2, 0)],
    "C2": [(0, 1), (1, 1), (2, 1)],
    "C3": [(0, 2), (1, 2), (2, 2)],
    "D1": [(0, 0), (1, 1), (2, 2)],
    "D2": [(0, 2), (1, 1), (2, 0)],
}

CORNERS: dict[str, tuple[int, int]] = {
    "TL": (0, 0),
    "TR": (0, 2),
    "BL": (2, 0),
    "BR": (2, 2),
}


class BingoError(Exception):
    """A business-rule rejection.  The code is safe to show at a terminal."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def claim_code_hash(participant_id: str, code: str) -> str:
    return hashlib.sha256(f"claim:{participant_id}:{code}".encode("utf-8")).hexdigest()


def new_state() -> dict[str, Any]:
    return {"version": 1, "tapes": {}, "games": {}, "idempotency": {}}


def _game(state: dict[str, Any], game_id: str) -> dict[str, Any]:
    try:
        return state["games"][game_id]
    except KeyError as exc:
        raise BingoError("GAME_NOT_FOUND", f"没有这场活动：{game_id}", status=404) from exc


def _next_seq(g: dict[str, Any]) -> int:
    g["seq"] = int(g.get("seq", 0)) + 1
    return g["seq"]


def _participant(g: dict[str, Any], participant_id: str) -> dict[str, Any]:
    p = g.get("participants", {}).get(participant_id)
    if not p:
        raise BingoError("PARTICIPANT_NOT_FOUND", f"没有这位报名人：{participant_id}", status=404)
    return p


def _card(g: dict[str, Any], card_id: str) -> dict[str, Any]:
    card = g.get("cards", {}).get(card_id)
    if not card:
        raise BingoError("CARD_NOT_FOUND", f"没有这张卡：{card_id}", status=404)
    return card


def _check_owner(card: dict[str, Any], participant_id: str | None) -> None:
    if participant_id and card["participant_id"] != participant_id:
        raise BingoError(
            "CARD_OWNER_MISMATCH",
            "这张卡不属于该终端/报名人",
            details={"card_id": card["id"], "owner": card["participant_id"]},
        )


def _check_card_version(card: dict[str, Any], requested_version: int | None) -> None:
    if requested_version is None:
        return
    try:
        requested_version = int(requested_version)
    except (TypeError, ValueError) as exc:
        raise BingoError("BAD_VERSION", "card_version 必须是整数") from exc
    if requested_version != card["current_version"]:
        raise BingoError(
            "CARD_VERSION_STALE",
            "终端持有的是已失效旧版卡；旧角/旧版不能用于新兑奖",
            status=409,
            details={
                "card_id": card["id"],
                "requested_version": requested_version,
                "current_version": card["current_version"],
            },
        )


def _record_rejection(
    g: dict[str, Any],
    kind: str,
    params: dict[str, Any],
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> BingoError:
    audit = g.setdefault("attempt_log", [])
    item = {
        "id": f"X-{_next_seq(g):05d}",
        "at": now_iso(),
        "at_seq": g["seq"],
        "at_beat": g["beat"],
        "kind": kind,
        "code": code,
        "message": message,
        "participant_id": params.get("participant_id"),
        "card_id": params.get("card_id"),
        "card_version": params.get("card_version"),
        "line_id": params.get("line_id"),
        "terminal_id": params.get("terminal_id"),
        "details": details or {},
    }
    audit.append(item)
    err = BingoError(code, message, details=details)
    err.details = {**(details or {}), "audit_id": item["id"]}
    return err


def _reveal_beat(g: dict[str, Any], number: int) -> int | None:
    value = g.get("number_beats", {}).get(str(number))
    return int(value) if value is not None else None


def _valid_stamp_at(card: dict[str, Any], row: int, col: int) -> dict[str, Any] | None:
    matches = [
        s
        for s in card.get("stamps", [])
        if s["row"] == row and s["col"] == col and s["status"] == "valid"
    ]
    return matches[-1] if matches else None


def retired_numbers(card: dict[str, Any]) -> list[int]:
    return [int(x["old_number"]) for x in card.get("swap_history", [])]


def evaluate_line(
    g: dict[str, Any], card: dict[str, Any], line_id: str, *, at_beat: int | None = None
) -> dict[str, Any]:
    """Evaluate a line using the active card version.

    ``at_beat`` is used for audit/time-travel checks.  A stamp recorded after
    that beat cannot repair a call which failed at the earlier beat.
    """
    if line_id not in LINE_DEFS:
        raise BingoError("UNKNOWN_LINE", f"没有这条线：{line_id}")
    if at_beat is None:
        at_beat = g["beat"]

    grid = card["grid"]
    cells: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for row, col in LINE_DEFS[line_id]:
        number = grid[row][col]
        stamp = _valid_stamp_at(card, row, col)
        voided = [
            s
            for s in card.get("stamps", [])
            if s["row"] == row and s["col"] == col and s["status"] == "voided"
        ]
        reveal_beat = _reveal_beat(g, number)
        stamp_ok = bool(
            stamp
            and stamp["number"] == number
            and int(stamp["recorded_beat"]) <= at_beat
            and int(stamp["reveal_beat"]) <= at_beat
        )
        item = {
            "position": next(name for name, rc in CORNERS.items() if rc == (row, col))
            if (row, col) in CORNERS
            else f"r{row + 1}c{col + 1}",
            "row": row,
            "col": col,
            "number": number,
            "reveal_beat": reveal_beat,
            "stamp": stamp,
            "voided_stamp": voided[-1] if voided else None,
            "stamped": stamp_ok,
        }
        cells.append(item)
        if not stamp_ok:
            reason = "NO_VALID_STAMP"
            if reveal_beat is None or reveal_beat > at_beat:
                reason = "NUMBER_NOT_REVEALED_BY_CALL_BEAT"
            missing.append({"row": row, "col": col, "number": number, "reason": reason})

    locked = line_id in card.get("redeemed_lines", {})
    return {
        "line_id": line_id,
        "complete": not missing,
        "locked": locked,
        "missing": missing,
        "cells": cells,
        "redemption": card.get("redeemed_lines", {}).get(line_id),
    }


def audit_card(g: dict[str, Any], card_id: str) -> dict[str, Any]:
    card = _card(g, card_id)
    lines = {lid: evaluate_line(g, card, lid) for lid in LINE_DEFS}
    return {
        "card_id": card_id,
        "participant_id": card["participant_id"],
        "current_version": card["current_version"],
        "grid": card["grid"],
        "versions": card["versions"],
        "stamps": card["stamps"],
        "swap_history": card["swap_history"],
        "redeemed_lines": card["redeemed_lines"],
        "lines": lines,
    }


def seal_tape(state: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    tape_id = str(p["tape_id"])
    tapes = state.setdefault("tapes", {})
    if tape_id in tapes:
        raise BingoError("TAPE_EXISTS", f"报号带已封存，不能改号：{tape_id}", status=409)
    order = [int(x) for x in p.get("order", [])]
    if not order or len(set(order)) != len(order):
        raise BingoError("BAD_TAPE", "报号带必须非空且号码不重复")
    record = {
        "id": tape_id,
        "order": order,
        "tape_commitment": canonical_hash(order),
        "sealed_at": now_iso(),
        "status": "sealed",
    }
    tapes[tape_id] = record
    return {k: v for k, v in record.items() if k != "order"} | {"order_sealed": True}


def create_game(state: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    game_id = p["game_id"]
    if game_id in state["games"]:
        raise BingoError("GAME_EXISTS", f"活动已存在：{game_id}", status=409)

    if p.get("tape_id"):
        tape_record = state.get("tapes", {}).get(str(p["tape_id"]))
        if not tape_record:
            raise BingoError("TAPE_NOT_SEALED", f"找不到已封存报号带：{p['tape_id']}", status=404)
        tape = list(tape_record["order"])
        if p.get("tape_commitment") and p["tape_commitment"] != tape_record["tape_commitment"]:
            raise BingoError("TAPE_COMMITMENT_MISMATCH", "报号带封存哈希不符", status=409)
    else:
        tape = [int(x) for x in p.get("tape", [])]
    if not tape or len(set(tape)) != len(tape):
        raise BingoError("BAD_TAPE", "报号带必须非空且号码不重复")

    sticker_ids = [str(x) for x in p.get("sticker_ids", [])]
    if not sticker_ids or len(set(sticker_ids)) != len(sticker_ids):
        raise BingoError("BAD_STICKER_BOX", "奖箱必须至少有一枚且编号不重复")

    participants_in = p.get("participants", [])
    if not participants_in:
        raise BingoError("BAD_PARTICIPANTS", "至少要有一位报名人")
    participants: dict[str, dict[str, Any]] = {}
    for item in participants_in:
        pid = str(item["id"])
        if pid in participants:
            raise BingoError("DUPLICATE_PARTICIPANT", pid)
        code = str(item.get("claim_code", ""))
        if len(code) < 4:
            raise BingoError("BAD_CLAIM_CODE", f"{pid} 的领奖码至少四位")
        participants[pid] = {
            "id": pid,
            "name": item.get("name", pid),
            "terminal_id": item.get("terminal_id"),
            "claim_code_hash": claim_code_hash(pid, code),
            "created_at": now_iso(),
        }

    cards_in = p.get("cards", [])
    if not cards_in:
        raise BingoError("BAD_CARDS", "至少要有一张卡")
    cards: dict[str, dict[str, Any]] = {}
    for item in cards_in:
        cid = str(item["id"])
        if cid in cards:
            raise BingoError("DUPLICATE_CARD", cid)
        pid = str(item["participant_id"])
        _participant({"participants": participants}, pid)
        grid = item.get("grid")
        if not (
            isinstance(grid, list)
            and len(grid) == 3
            and all(isinstance(row, list) and len(row) == 3 for row in grid)
        ):
            raise BingoError("BAD_GRID", f"{cid} 必须是 3×3 数字卡")
        grid = [[int(x) for x in row] for row in grid]
        numbers = [n for row in grid for n in row]
        if len(set(numbers)) != 9:
            raise BingoError("BAD_GRID", f"{cid} 卡面九个号码必须唯一；换角块请走 swap 流程")
        cards[cid] = {
            "id": cid,
            "participant_id": pid,
            "current_version": 1,
            "grid": copy.deepcopy(grid),
            "versions": [
                {"version": 1, "grid": copy.deepcopy(grid), "valid": True, "created_beat": 0}
            ],
            "stamps": [],
            "swap_history": [],
            "redeemed_lines": {},
            "created_at": now_iso(),
        }

    blocks: dict[str, dict[str, Any]] = {}
    for b in p.get("corner_blocks", []):
        bid = str(b["id"])
        if bid in blocks:
            raise BingoError("DUPLICATE_BLOCK", bid)
        blocks[bid] = {
            "id": bid,
            "number": int(b["number"]),
            "eligible_card_id": b.get("card_id"),
            "status": "available",
            "used_by_card_id": None,
            "used_at_seq": None,
        }

    g: dict[str, Any] = {
        "id": game_id,
        "name": p.get("name", game_id),
        "mode": p.get("mode", "electronic_corner"),
        "tape_id": p.get("tape_id"),
        "status": "sealed",
        "created_at": now_iso(),
        "sealed_at": now_iso(),
        "tape": tape,
        "tape_commitment": canonical_hash(tape),
        "beat": 0,
        "seq": 0,
        "reveals": [],
        "number_beats": {},
        "participants": participants,
        "cards": cards,
        "corner_blocks": blocks,
        "windows": {},
        "active_window_id": None,
        "calls": {},
        "call_order": [],
        "claims": {},
        "redemptions": {},
        "stickers": [
            {"id": sid, "status": "in_box", "call_id": None, "participant_id": None}
            for sid in sticker_ids
        ],
        "attempt_log": [],
        "event_log": [{"at": now_iso(), "type": "TAPE_SEALED", "commitment": canonical_hash(tape)}],
    }
    state["games"][game_id] = g
    return {
        "game_id": game_id,
        "mode": g["mode"],
        "tape_commitment": g["tape_commitment"],
        "cards": list(cards),
        "sticker_ids": sticker_ids,
    }


def open_window(g: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    wid = str(p.get("window_id") or "W1")
    active = g.get("active_window_id")
    if active and g["windows"][active]["status"] == "open":
        raise BingoError("WINDOW_ALREADY_OPEN", f"已有开放中的窗口：{active}", status=409)
    if wid in g["windows"]:
        raise BingoError("WINDOW_EXISTS", f"窗口编号已用过：{wid}", status=409)
    window = {
        "id": wid,
        "title": p.get("title", "受理/发奖窗"),
        "status": "open",
        "opened_at": now_iso(),
        "opened_at_beat": g["beat"],
        "closed_at": None,
        "closed_at_beat": None,
    }
    g["windows"][wid] = window
    g["active_window_id"] = wid
    g["status"] = "playing"
    g["event_log"].append({"at": now_iso(), "type": "WINDOW_OPENED", "window_id": wid})
    return window


def close_window(g: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    wid = p.get("window_id") or g.get("active_window_id")
    if not wid or wid not in g["windows"]:
        raise BingoError("WINDOW_NOT_FOUND", "没有可关闭的开放窗口", status=404)
    w = g["windows"][wid]
    if w["status"] != "open":
        raise BingoError("WINDOW_ALREADY_CLOSED", f"窗口已关闭：{wid}", status=409)
    w["status"] = "closed"
    w["closed_at"] = now_iso()
    w["closed_at_beat"] = g["beat"]
    if g.get("active_window_id") == wid:
        g["active_window_id"] = None
    g["event_log"].append(
        {"at": now_iso(), "type": "WINDOW_CLOSED", "window_id": wid, "beat": g["beat"]}
    )
    return w


def reveal_next(g: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    outstanding = [cid for cid in g["call_order"] if g["calls"][cid]["status"] == "verified"]
    if outstanding and not p.get("force"):
        raise BingoError(
            "GAME_HALTED_FOR_CLAIM",
            "已有经核验的叫停，须先完成领奖手续或作废该呼叫，才能揭下一报号",
            status=409,
            details={"outstanding_call_ids": outstanding},
        )
    if g["beat"] >= len(g["tape"]):
        raise BingoError("TAPE_EXHAUSTED", "报号带已揭完；重播不会产生新号码")

    beat = g["beat"] + 1
    number = g["tape"][g["beat"]]
    reveal = {"beat": beat, "number": number, "at": now_iso(), "kind": "live_reveal"}
    g["beat"] = beat
    g["reveals"].append(reveal)
    g["number_beats"][str(number)] = beat
    g["event_log"].append({"at": now_iso(), "type": "NUMBER_REVEALED", **reveal})
    return reveal


def replay_tape(g: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    """Return the broadcast trail.  This read-only operation never increments beat."""
    full = g["status"] == "finished" or bool(p.get("operator_override"))
    order = g["tape"] if full else None
    valid = canonical_hash(g["tape"]) == g["tape_commitment"]
    return {
        "game_id": g["id"],
        "beat": g["beat"],
        "revealed": g["reveals"],
        "revealed_numbers": [x["number"] for x in g["reveals"]],
        "sealed_tape": order,
        "tape_commitment": g["tape_commitment"],
        "commitment_valid": valid,
        "note": "replay 只回放揭号记录，不是再开奖一次",
    }


def finish_game(g: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    g["status"] = "finished"
    if g.get("active_window_id"):
        close_window(g, {"window_id": g["active_window_id"]})
    g["event_log"].append({"at": now_iso(), "type": "GAME_FINISHED"})
    return replay_tape(g, {"operator_override": True})


def stamp(g: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    card = _card(g, str(p["card_id"]))
    _check_owner(card, p.get("participant_id"))
    _check_card_version(card, p.get("card_version"))

    row = p.get("row")
    col = p.get("col")
    number = p.get("number")
    if number is not None:
        number = int(number)
        positions = [(r, c) for r in range(3) for c in range(3) if card["grid"][r][c] == number]
        if not positions:
            if number in retired_numbers(card):
                raise _record_rejection(
                    g,
                    "stamp",
                    p,
                    "RETIRED_CORNER_NUMBER",
                    "这个号属于已失效旧角；即使现已揭出，也不能回旧角补章或搬到新角",
                    {"number": number},
                )
            raise _record_rejection(
                g,
                "stamp",
                p,
                "NUMBER_NOT_ON_ACTIVE_CARD",
                "当前有效卡面上没有这个号",
                {"number": number, "current_version": card["current_version"]},
            )
        row, col = positions[0]
    else:
        if row is None or col is None:
            raise BingoError("BAD_STAMP", "盖章必须给 row/col 或 number")
        row, col = int(row), int(col)
        if not (0 <= row < 3 and 0 <= col < 3):
            raise BingoError("BAD_POSITION", "格位必须在 0..2 之间")
        number = int(card["grid"][row][col])

    reveal_beat = _reveal_beat(g, number)
    if reveal_beat is None:
        raise _record_rejection(
            g,
            "stamp",
            p,
            "NUMBER_NOT_YET_REVEALED",
            "报号带尚未揭出这个号，不能预盖",
            {"number": number},
        )

    existing = _valid_stamp_at(card, row, col)
    if existing and int(existing["number"]) == number:
        return {"idempotent": True, "stamp": existing}

    stamp_item = {
        "id": f"S-{_next_seq(g):05d}",
        "seq": g["seq"],
        "card_version": card["current_version"],
        "row": row,
        "col": col,
        "position": next((name for name, rc in CORNERS.items() if rc == (row, col)), f"r{row+1}c{col+1}"),
        "number": number,
        "reveal_beat": reveal_beat,
        "recorded_beat": g["beat"],
        "recorded_at": now_iso(),
        "terminal_id": p.get("terminal_id"),
        "source": "offline_note_backfill" if p.get("source") == "offline_note" else "heard_live",
        "status": "valid",
    }
    card["stamps"].append(stamp_item)
    g["event_log"].append(
        {"at": now_iso(), "type": "STAMP", "card_id": card["id"], "stamp_id": stamp_item["id"]}
    )
    return {"idempotent": False, "stamp": stamp_item}


def swap_corner(g: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    card = _card(g, str(p["card_id"]))
    _check_owner(card, p.get("participant_id"))
    _check_card_version(card, p.get("card_version"))

    if g.get("mode") != "electronic_corner":
        raise BingoError("WRONG_VENUE_MODE", "这场不是电子换角场")
    if card.get("swap_history"):
        raise BingoError("SWAP_ALREADY_USED", "每卡只有一次换角机会", status=409)
    if card.get("redeemed_lines"):
        raise BingoError("CARD_LOCKED_AFTER_REDEMPTION", "已有线兑奖并锁定，不能再换角")
    if not (1 <= g["beat"] < len(g["tape"])):
        raise BingoError(
            "SWAP_ONLY_BETWEEN_REVEALS",
            "换角只能在已揭一号之后、下一揭号之前的间歇办理",
            details={"beat": g["beat"], "tape_length": len(g["tape"])},
        )

    pos_name = str(p.get("position", "")).upper()
    if pos_name not in CORNERS:
        raise BingoError("BAD_CORNER", "position 必须是 TL/TR/BL/BR")
    row, col = CORNERS[pos_name]

    block_id = str(p.get("block_id", ""))
    block = g.get("corner_blocks", {}).get(block_id)
    if not block:
        raise BingoError("BLOCK_NOT_FOUND", f"没有给定角块：{block_id}", status=404)
    if block["status"] != "available":
        raise BingoError("BLOCK_USED", "这个角块已被使用", status=409)
    if block.get("eligible_card_id") and block["eligible_card_id"] != card["id"]:
        raise BingoError("BLOCK_NOT_FOR_CARD", "这个给定角块不属于该卡")

    old_number = int(card["grid"][row][col])
    new_number = int(block["number"])
    other_numbers = {
        card["grid"][r][c] for r in range(3) for c in range(3) if (r, c) != (row, col)
    }
    if new_number in other_numbers:
        raise BingoError("DUPLICATE_NUMBER_AFTER_SWAP", "换角后会造成同号重复，拒绝换入")

    old_stamp = _valid_stamp_at(card, row, col)
    voided_stamp = None
    if old_stamp:
        old_stamp["status"] = "voided"
        old_stamp["voided_at"] = now_iso()
        old_stamp["void_reason"] = "OLD_CORNER_INVALID_AFTER_SWAP"
        old_stamp["voided_by_swap_seq"] = _next_seq(g)
        voided_stamp = old_stamp["id"]

    old_version = next(v for v in card["versions"] if v["version"] == card["current_version"])
    old_version["valid"] = False
    old_version["superseded_by"] = card["current_version"] + 1

    card["grid"][row][col] = new_number
    card["current_version"] += 1
    card["versions"].append(
        {
            "version": card["current_version"],
            "grid": copy.deepcopy(card["grid"]),
            "valid": True,
            "created_beat": g["beat"],
            "swap_position": pos_name,
            "block_id": block_id,
        }
    )
    swap = {
        "seq": _next_seq(g),
        "at": now_iso(),
        "at_beat": g["beat"],
        "position": pos_name,
        "row": row,
        "col": col,
        "old_number": old_number,
        "new_number": new_number,
        "block_id": block_id,
        "voided_stamp_id": voided_stamp,
        "rule": "旧角失效；原章留在原格并作废，不许搬到其他位置",
    }
    card["swap_history"].append(swap)
    block["status"] = "used"
    block["used_by_card_id"] = card["id"]
    block["used_at_seq"] = swap["seq"]
    g["event_log"].append({"at": now_iso(), "type": "CORNER_SWAPPED", **swap})
    return {"card_id": card["id"], "new_version": card["current_version"], "swap": swap}


def create_call(g: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    card = _card(g, str(p["card_id"]))
    _check_owner(card, p.get("participant_id"))
    requested_version = p.get("card_version")
    if requested_version is not None:
        try:
            requested_version = int(requested_version)
        except (TypeError, ValueError):
            requested_version = -1
        if requested_version != card["current_version"]:
            raise _record_rejection(
                g,
                "call",
                p,
                "CARD_VERSION_STALE",
                "旧版卡喊线无效：请换入当前有效版；旧角上的章不能接到新斜线上",
                {
                    "current_version": card["current_version"],
                    "requested_version": p.get("card_version"),
                },
            )

    window_id = g.get("active_window_id")
    if not window_id:
        raise _record_rejection(
            g, "call", p, "WINDOW_CLOSED", "受理/发奖窗未开放或已关闭；此时不能记为该窗已领"
        )
    window = g["windows"][window_id]
    if window["status"] != "open":
        raise _record_rejection(g, "call", p, "WINDOW_CLOSED", "受理/发奖窗已关闭")

    line_id = str(p.get("line_id", "")).upper()
    if line_id not in LINE_DEFS:
        raise _record_rejection(g, "call", p, "UNKNOWN_LINE", f"没有这条线：{line_id}")

    line_status = evaluate_line(g, card, line_id)
    if line_status["locked"]:
        raise _record_rejection(
            g,
            "call",
            p,
            "LINE_ALREADY_REDEEMED",
            "这条线已兑奖并锁住，不能用旧呼叫或新版卡重复领贴",
            {"redemption": line_status["redemption"]},
        )
    if not line_status["complete"]:
        raise _record_rejection(
            g,
            "call",
            p,
            "LINE_INCOMPLETE_AT_CALL",
            "指定喊线时刻尚未三格盖妥，不能成线",
            {
                "call_beat": g["beat"],
                "revealed_numbers": [x["number"] for x in g["reveals"]],
                "missing": line_status["missing"],
            },
        )

    seq = _next_seq(g)
    call_id = str(p.get("call_id") or f"C-{seq:05d}")
    if call_id in g["calls"]:
        raise BingoError("CALL_EXISTS", f"呼叫编号已存在：{call_id}", status=409)
    call = {
        "id": call_id,
        "seq": seq,
        "received_at": now_iso(),
        "beat": g["beat"],
        "window_id": window_id,
        "participant_id": card["participant_id"],
        "terminal_id": p.get("terminal_id"),
        "card_id": card["id"],
        "card_version": card["current_version"],
        "line_id": line_id,
        "line_snapshot": line_status["cells"],
        "revealed_numbers": [x["number"] for x in g["reveals"]],
        "client_call_ref": p.get("client_call_ref"),
        "client_occurred_at": p.get("client_occurred_at"),
        "status": "verified",
        "claim_id": None,
        "sticker_id": None,
    }
    g["calls"][call_id] = call
    g["call_order"].append(call_id)
    g["event_log"].append({"at": now_iso(), "type": "CALL_VERIFIED", "call_id": call_id, "seq": seq})
    return call


def issue_claim(g: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    call_id = str(p["call_id"])
    call = g["calls"].get(call_id)
    if not call:
        raise BingoError("CALL_NOT_FOUND", f"没有这通叫停：{call_id}", status=404)

    participant_id = str(p.get("participant_id") or call["participant_id"])
    participant = _participant(g, participant_id)
    if call["participant_id"] != participant_id:
        raise BingoError("CLAIM_OWNER_MISMATCH", "叫停人和领奖人不一致", status=403)

    code = str(p.get("claim_code", ""))
    if claim_code_hash(participant_id, code) != participant["claim_code_hash"]:
        raise _record_rejection(
            g,
            "claim",
            {**p, "card_id": call["card_id"], "line_id": call["line_id"]},
            "BAD_CLAIM_CODE",
            "领奖码或领奖手续不符，不能发贴",
        )

    if call["status"] == "awarded":
        sticker = next(s for s in g["stickers"] if s["id"] == call["sticker_id"])
        return {"idempotent": True, "call": call, "sticker": sticker}
    if call["status"] != "verified":
        raise BingoError(
            "CALL_NOT_CLAIMABLE",
            f"这通呼叫当前状态为 {call['status']}，不能发贴",
            details={"call_status": call["status"]},
            status=409,
        )

    window = g["windows"].get(call["window_id"])
    if not window or window["status"] != "open":
        call["status"] = "expired_after_window_close"
        raise _record_rejection(
            g,
            "claim",
            {**p, "card_id": call["card_id"], "line_id": call["line_id"]},
            "WINDOW_CLOSED",
            "发奖窗已关闭；线即使后来成立，也不能记为该窗已领",
            {"window_id": call["window_id"]},
        )

    card = _card(g, call["card_id"])
    if call["line_id"] in card.get("redeemed_lines", {}):
        call["status"] = "duplicate_locked"
        raise _record_rejection(
            g,
            "claim",
            {**p, "card_id": card["id"], "line_id": call["line_id"]},
            "LINE_ALREADY_REDEEMED",
            "这条线已被另一通手续兑奖并锁住",
            {"redemption": card["redeemed_lines"][call["line_id"]]},
        )

    sticker = next((s for s in g["stickers"] if s["status"] == "in_box"), None)
    if sticker is None:
        call["status"] = "unfunded"
        given = [s for s in g["stickers"] if s["status"] == "given"]
        last = max(
            given,
            key=lambda s: g["redemptions"].get(s.get("redemption_id", ""), {}).get("awarded_seq", -1),
            default=None,
        )
        last_redemption = g["redemptions"].get(last["redemption_id"]) if last else None
        raise _record_rejection(
            g,
            "claim",
            {**p, "card_id": card["id"], "line_id": call["line_id"]},
            "STICKER_BOX_EMPTY",
            "你符合连线条件，但小奖箱最后一枚已按后端提交顺序发给别人",
            {
                "last_sticker_id": last["id"] if last else None,
                "winner_participant_id": last["participant_id"] if last else None,
                "redemption": last_redemption,
                "tie_policy": "同一揭号拍内以后端锁内提交/兑奖顺序为准；第一笔完成手续者得最后一枚",
            },
        )

    seq = _next_seq(g)
    redemption_id = f"R-{seq:05d}"
    claim_id = f"CL-{seq:05d}"
    redemption = {
        "id": redemption_id,
        "claim_id": claim_id,
        "awarded_at": now_iso(),
        "awarded_seq": seq,
        "awarded_beat": g["beat"],
        "window_id": call["window_id"],
        "call_id": call["id"],
        "participant_id": participant_id,
        "terminal_id": p.get("terminal_id"),
        "card_id": card["id"],
        "card_version_at_call": call["card_version"],
        "line_id": call["line_id"],
        "sticker_id": sticker["id"],
    }
    claim = {
        "id": claim_id,
        "call_id": call["id"],
        "participant_id": participant_id,
        "terminal_id": p.get("terminal_id"),
        "code_verified": True,
        "received_at": now_iso(),
        "redemption_id": redemption_id,
    }
    g["redemptions"][redemption_id] = redemption
    g["claims"][claim_id] = claim

    sticker["status"] = "given"
    sticker["participant_id"] = participant_id
    sticker["call_id"] = call["id"]
    sticker["redemption_id"] = redemption_id

    call["status"] = "awarded"
    call["claim_id"] = claim_id
    call["sticker_id"] = sticker["id"]
    call["redemption_id"] = redemption_id

    card["redeemed_lines"][call["line_id"]] = redemption
    g["event_log"].append(
        {
            "at": now_iso(),
            "type": "STICKER_AWARDED",
            "sticker_id": sticker["id"],
            "participant_id": participant_id,
            "redemption_id": redemption_id,
        }
    )
    return {"call": call, "claim": claim, "redemption": redemption, "sticker": sticker}


def void_call(g: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    call = g["calls"].get(str(p["call_id"]))
    if not call:
        raise BingoError("CALL_NOT_FOUND", "没有这通呼叫", status=404)
    if call["status"] != "verified":
        raise BingoError("CALL_NOT_PENDING", f"呼叫状态为 {call['status']}", status=409)
    call["status"] = "voided"
    call["void_reason"] = p.get("reason", "值班员作废")
    call["voided_at"] = now_iso()
    return call


def stickers_report(g: dict[str, Any], p: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = []
    for s in g["stickers"]:
        redemption = g["redemptions"].get(s.get("redemption_id", "")) if s.get("redemption_id") else None
        rows.append({**s, "redemption": redemption})
    given = [s for s in rows if s["status"] == "given"]
    return {
        "box_size": len(rows),
        "remaining": len(rows) - len(given),
        "given_count": len(given),
        "stickers": rows,
    }


def dispatch_read(g: dict[str, Any], action: str, params: dict[str, Any]) -> Any:
    if action == "state.get":
        return g
    if action == "tape.replay":
        return replay_tape(g, params)
    if action == "card.audit":
        return audit_card(g, str(params["card_id"]))
    if action == "stickers.list":
        return stickers_report(g, params)
    if action == "attempts.list":
        return {"attempts": g["attempt_log"]}
    if action == "calls.list":
        return {"calls": [g["calls"][cid] for cid in g["call_order"]]}
    raise BingoError("UNKNOWN_ACTION", f"未知查询动作：{action}", status=404)


MUTATIONS: dict[str, Callable[[dict[str, Any], dict[str, Any]], Any]] = {
    "window.open": open_window,
    "window.close": close_window,
    "reveal.next": reveal_next,
    "game.finish": finish_game,
    "stamp": stamp,
    "corner.swap": swap_corner,
    "call.create": create_call,
    "claim.issue": issue_claim,
    "call.void": void_call,
}


class Backend:
    """Owns one JSON state file and one process-wide mutation lock."""

    def __init__(self, state_path: str | os.PathLike[str]):
        self.state_path = Path(state_path)
        self.lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if self.state_path.exists():
            with self.state_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        return new_state()

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def act(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        with self.lock:
            request_id = params.get("request_id")
            if request_id and request_id in self.state.get("idempotency", {}):
                record = self.state["idempotency"][request_id]
                if record["action"] != action:
                    raise BingoError("REQUEST_ID_CONFLICT", "request_id 被用于不同动作", status=409)
                return {**record["response"], "idempotent_replay": True}

            try:
                if action == "tape.seal":
                    result = seal_tape(self.state, params)
                elif action == "game.create":
                    result = create_game(self.state, params)
                else:
                    game_id = str(params.get("game_id", ""))
                    g = _game(self.state, game_id)
                    if action in MUTATIONS:
                        result = MUTATIONS[action](g, params)
                    elif action in {
                        "state.get",
                        "tape.replay",
                        "card.audit",
                        "stickers.list",
                        "attempts.list",
                        "calls.list",
                    }:
                        result = dispatch_read(g, action, params)
                    else:
                        raise BingoError("UNKNOWN_ACTION", f"未知动作：{action}", status=404)
            except BingoError:
                # Rejection/call-status audits are deliberately retained.
                self.save()
                raise

            if request_id and (action in MUTATIONS or action in {"game.create", "tape.seal"}):
                self.state.setdefault("idempotency", {})[request_id] = {
                    "action": action,
                    "response": result,
                    "saved_at": now_iso(),
                }
            self.save()
            return result
