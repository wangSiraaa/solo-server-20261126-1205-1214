from __future__ import annotations

import sqlite3
from typing import Any

from . import services

DEMO_ROUND_IDS = [
    "dolphin-complete",
    "dolphin-unfound",
    "dolphin-both-star",
    "sealed-check",
    "revisit-answer",
]

APPEARANCE = {"form": "dolphin_kettle", "sticker": "blue"}
SCHEMA = {
    "bottom_mark": {
        "label": "杯底印记",
        "prompt": "请当面辨认杯底印记（两选一）",
        "options": ["星", "月"],
    }
}


def reset_demo(conn: sqlite3.Connection) -> None:
    ids = tuple(DEMO_ROUND_IDS)
    placeholders = ",".join("?" for _ in ids)
    voucher_rows = conn.execute(
        f"SELECT voucher_no FROM vouchers WHERE round_id IN ({placeholders})", ids
    ).fetchall()
    voucher_nos = [r["voucher_no"] for r in voucher_rows]

    conn.execute("BEGIN IMMEDIATE")
    try:
        if voucher_nos:
            vp = ",".join("?" for _ in voucher_nos)
            conn.execute(f"DELETE FROM handoffs WHERE voucher_no IN ({vp})", voucher_nos)
            conn.execute(f"DELETE FROM outbound_attempts WHERE voucher_no IN ({vp})", voucher_nos)
        conn.execute(f"DELETE FROM vouchers WHERE round_id IN ({placeholders})", ids)
        conn.execute(
            f"DELETE FROM claim_answers WHERE claim_no IN "
            f"(SELECT claim_no FROM claims WHERE round_id IN ({placeholders}))",
            ids,
        )
        conn.execute(f"DELETE FROM claims WHERE round_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM inspections WHERE round_id IN ({placeholders})", ids)
        conn.execute(
            f"DELETE FROM shift_preparations WHERE item_no IN "
            f"(SELECT item_no FROM items WHERE round_id IN ({placeholders}))",
            ids,
        )
        conn.execute(
            f"DELETE FROM shifts WHERE shift_id IN ({placeholders})",
            tuple(f"shift-{x}" for x in ids),
        )
        conn.execute(
            f"DELETE FROM item_features WHERE item_no IN "
            f"(SELECT item_no FROM items WHERE round_id IN ({placeholders}))",
            ids,
        )
        conn.execute(f"DELETE FROM items WHERE round_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM registration_events WHERE round_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM rounds WHERE round_id IN ({placeholders})", ids)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def seed_dolphins(conn: sqlite3.Connection, reset: bool = False) -> dict[str, Any]:
    if reset:
        reset_demo(conn)

    created: dict[str, Any] = {}

    def exists(round_id: str) -> bool:
        return conn.execute("SELECT 1 FROM rounds WHERE round_id=?", (round_id,)).fetchone() is not None

    # 1. Explicitly closed, truly one-to-one registered pair.
    rid = "dolphin-complete"
    if not exists(rid):
        services.create_round(conn, {
            "round_id": rid,
            "title": "两只蓝贴海豚壶：完整一对一登记",
            "window_mode": "sealed",
            "complete_pairing": True,
            "allow_unfound": False,
            "feature_schema": SCHEMA,
            "inspection_budget": 1,
        })
        services.create_item(conn, {
            "round_id": rid, "item_no": "I-D1", "label": "蓝贴海豚壶·第一只",
            "appearance": APPEARANCE,
            "features": {"bottom_mark": "星"},
        })
        services.create_item(conn, {
            "round_id": rid, "item_no": "I-D2", "label": "蓝贴海豚壶·第二只",
            "appearance": APPEARANCE,
            "features": {"bottom_mark": "月"},
        })
        services.create_claim(conn, {
            "round_id": rid, "claim_no": "C-A", "claimant": "甲",
            "appearance": APPEARANCE, "memory": {"bottom_mark": "星"},
        })
        services.create_claim(conn, {
            "round_id": rid, "claim_no": "C-B", "claimant": "乙",
            "appearance": APPEARANCE, "memory": {"bottom_mark": ["星", "月"]},
        })
        created[rid] = "seeded"
    else:
        created[rid] = "already_exists"

    # 2. Same evidence, but registration expressly allows an unfound pot.
    rid = "dolphin-unfound"
    if not exists(rid):
        services.create_round(conn, {
            "round_id": rid,
            "title": "两只蓝贴海豚壶：开放登记，允许未找到的另一只",
            "window_mode": "sealed",
            "complete_pairing": False,
            "allow_unfound": True,
            "feature_schema": SCHEMA,
            "inspection_budget": 1,
        })
        services.create_item(conn, {
            "round_id": rid, "item_no": "I-U1", "label": "开放登记壶·第一只",
            "appearance": APPEARANCE, "features": {"bottom_mark": "星"},
        })
        services.create_item(conn, {
            "round_id": rid, "item_no": "I-U2", "label": "开放登记壶·第二只",
            "appearance": APPEARANCE, "features": {"bottom_mark": "月"},
        })
        services.create_claim(conn, {
            "round_id": rid, "claim_no": "C-UA", "claimant": "甲",
            "appearance": APPEARANCE, "memory": {"bottom_mark": "星"},
        })
        services.create_claim(conn, {
            "round_id": rid, "claim_no": "C-UB", "claimant": "乙",
            "appearance": APPEARANCE, "memory": {"bottom_mark": ["星", "月"]},
        })
        created[rid] = "seeded"
    else:
        created[rid] = "already_exists"

    # 3. Independent complete-pairing replay: both bottoms are stars.
    rid = "dolphin-both-star"
    if not exists(rid):
        services.create_round(conn, {
            "round_id": rid,
            "title": "独立重放：两只底记均为星",
            "window_mode": "sealed",
            "complete_pairing": True,
            "allow_unfound": False,
            "feature_schema": SCHEMA,
            "inspection_budget": 1,
        })
        for no, label in [("I-S1", "双星海豚壶·第一只"), ("I-S2", "双星海豚壶·第二只")]:
            services.create_item(conn, {
                "round_id": rid, "item_no": no, "label": label,
                "appearance": APPEARANCE, "features": {"bottom_mark": "星"},
            })
        services.create_claim(conn, {
            "round_id": rid, "claim_no": "C-SA", "claimant": "甲",
            "appearance": APPEARANCE, "memory": {"bottom_mark": "星"},
        })
        services.create_claim(conn, {
            "round_id": rid, "claim_no": "C-SB", "claimant": "乙",
            "appearance": APPEARANCE, "memory": {"bottom_mark": ["星", "月"]},
        })
        created[rid] = "seeded"
    else:
        created[rid] = "already_exists"

    # 4. Sealed window: one real custodian inspection removes ambiguity.
    rid = "sealed-check"
    if not exists(rid):
        services.create_round(conn, {
            "round_id": rid,
            "title": "封存窗：一次真实开袋检查可解开",
            "window_mode": "sealed",
            "complete_pairing": False,
            "allow_unfound": True,
            "feature_schema": SCHEMA,
            "inspection_budget": 1,
        })
        services.create_item(conn, {
            "round_id": rid, "item_no": "I-K1", "label": "待开袋壶 K1",
            "appearance": APPEARANCE,
            "features": {
                "bottom_mark": {
                    "value": "月", "revealed": False, "options": ["星", "月"]
                }
            },
        })
        services.create_item(conn, {
            "round_id": rid, "item_no": "I-K2", "label": "已登记底记壶 K2",
            "appearance": APPEARANCE, "features": {"bottom_mark": "星"},
        })
        services.create_claim(conn, {
            "round_id": rid, "claim_no": "C-K", "claimant": "丁",
            "appearance": APPEARANCE, "memory": {"bottom_mark": "星"},
        })
        created[rid] = "seeded"
    else:
        created[rid] = "already_exists"

    # 5. Revisit window: one binding two-choice answer removes ambiguity.
    rid = "revisit-answer"
    if not exists(rid):
        services.create_round(conn, {
            "round_id": rid,
            "title": "复访窗：一次有约束力的两选一回答可解开",
            "window_mode": "revisit",
            "complete_pairing": False,
            "allow_unfound": True,
            "feature_schema": SCHEMA,
            "willing_round_default": 1,
            "prepare_budget": 2,
        })
        services.create_item(conn, {
            "round_id": rid, "item_no": "I-R1", "label": "复访辨认壶 R1",
            "appearance": APPEARANCE, "features": {"bottom_mark": "星"},
        })
        services.create_item(conn, {
            "round_id": rid, "item_no": "I-R2", "label": "复访辨认壶 R2",
            "appearance": APPEARANCE, "features": {"bottom_mark": "月"},
        })
        services.create_claim(conn, {
            "round_id": rid, "claim_no": "C-R", "claimant": "戊",
            "appearance": APPEARANCE,
            "memory": {"bottom_mark": ["星", "月"]},
            "willing_rounds": 1,
        })
        created[rid] = "seeded"
    else:
        created[rid] = "already_exists"

    return {"seeded": created}
