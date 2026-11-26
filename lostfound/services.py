from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import next_number
from .matching import evaluate, normalize_claim_statement, with_feature_overrides


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def canonical_hash(payload: Any) -> str:
    return hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()


def get_round(conn: sqlite3.Connection, round_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
    if row is None:
        raise ApiError(404, "round_not_found", f"没有本轮登记：{round_id}")
    data = row_dict(row)
    assert data is not None
    data["feature_schema"] = loads(data.get("feature_schema"), {})
    return data


def fetch_state(conn: sqlite3.Connection, round_id: str) -> dict[str, Any]:
    round_row = get_round(conn, round_id)

    item_rows = conn.execute(
        "SELECT * FROM items WHERE round_id=? ORDER BY item_no", (round_id,)
    ).fetchall()
    items: list[dict[str, Any]] = []
    items_by_no: dict[str, dict[str, Any]] = {}
    for row in item_rows:
        item = row_dict(row)
        assert item is not None
        item["appearance"] = loads(item.get("appearance"), {})
        items.append(item)
        items_by_no[item["item_no"]] = item

    feature_rows = conn.execute(
        """
        SELECT f.* FROM item_features f
        JOIN items i ON i.item_no = f.item_no
        WHERE i.round_id=?
        ORDER BY f.item_no, f.feature_key
        """,
        (round_id,),
    ).fetchall()
    features: dict[str, dict[str, dict[str, Any]]] = {no: {} for no in items_by_no}
    for row in feature_rows:
        feature = row_dict(row)
        assert feature is not None
        feature["options"] = loads(feature.get("options"), [])
        features.setdefault(feature["item_no"], {})[feature["feature_key"]] = feature

    claim_rows = conn.execute(
        "SELECT * FROM claims WHERE round_id=? ORDER BY claim_no", (round_id,)
    ).fetchall()
    claims: list[dict[str, Any]] = []
    claims_by_no: dict[str, dict[str, Any]] = {}
    for row in claim_rows:
        claim = row_dict(row)
        assert claim is not None
        claim["appearance"] = loads(claim.get("appearance"), {})
        claim["memory"] = loads(claim.get("memory"), {})
        claim["constraints"] = normalize_claim_statement(
            claim["appearance"], claim["memory"]
        )
        claims.append(claim)
        claims_by_no[claim["claim_no"]] = claim

    return {
        "round": round_row,
        "items": items,
        "items_by_no": items_by_no,
        "features": features,
        "claims": claims,
        "claims_by_no": claims_by_no,
    }


def create_round(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    round_id = str(payload.get("round_id") or "").strip()
    if not round_id:
        raise ApiError(400, "round_id_required", "请提供 round_id")
    mode = payload.get("window_mode", "sealed")
    if mode not in ("sealed", "revisit"):
        raise ApiError(400, "bad_window_mode", "window_mode 只能是 sealed 或 revisit")

    complete = bool(payload.get("complete_pairing", False))
    allow_unfound = payload.get("allow_unfound", not complete)
    allow_unfound = bool(allow_unfound)
    if complete and allow_unfound:
        raise ApiError(
            400,
            "contradictory_registration",
            "complete_pairing=true 表示封闭完整配对；不能同时 allow_unfound=true",
        )

    now = now_iso()
    schema = payload.get("feature_schema", {})
    budget = int(payload.get("inspection_budget", 2))
    willing_default = int(payload.get("willing_round_default", 1))
    prepare_budget = int(payload.get("prepare_budget", 4))
    if budget < 0 or willing_default < 1 or prepare_budget < 0:
        raise ApiError(400, "bad_budget", "预算必须为非负数，复访轮数至少为 1")

    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM rounds WHERE round_id=?", (round_id,)).fetchone():
            raise ApiError(409, "round_exists", f"轮次已存在：{round_id}")
        conn.execute(
            """
            INSERT INTO rounds(round_id, title, window_mode, complete_pairing,
                allow_unfound, feature_schema, inspection_budget,
                willing_round_default, prepare_budget, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                round_id,
                str(payload.get("title", round_id)),
                mode,
                int(complete),
                int(allow_unfound),
                dumps(schema),
                budget,
                willing_default,
                prepare_budget,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO registration_events(round_id, complete_pairing, allow_unfound, note, created_at) VALUES (?,?,?,?,?)",
            (round_id, int(complete), int(allow_unfound), "initial registration", now),
        )
        shift_id = payload.get("shift_id") or f"shift-{round_id}"
        conn.execute(
            "INSERT OR IGNORE INTO shifts(shift_id, prepare_budget, created_at) VALUES (?,?,?)",
            (shift_id, prepare_budget, now),
        )
        conn.commit()
    except ApiError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return get_round(conn, round_id)


def update_registration(
    conn: sqlite3.Connection, round_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    round_row = get_round(conn, round_id)
    complete = bool(payload.get("complete_pairing", round_row["complete_pairing"]))
    allow = bool(payload.get("allow_unfound", round_row["allow_unfound"]))
    if complete and allow:
        raise ApiError(400, "contradictory_registration", "完整封闭配对不能同时允许未找到物")
    note = str(payload.get("note", "registration changed"))
    now = now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE rounds SET complete_pairing=?, allow_unfound=? WHERE round_id=?",
            (int(complete), int(allow), round_id),
        )
        conn.execute(
            "INSERT INTO registration_events(round_id, complete_pairing, allow_unfound, note, created_at) VALUES (?,?,?,?,?)",
            (round_id, int(complete), int(allow), note, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_round(conn, round_id)


def _feature_specs(payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}

    def add(key: str, raw: Any, default_revealed: bool) -> None:
        if isinstance(raw, dict):
            value = raw.get("value", raw.get("actual", raw.get("actual_value")))
            if value is None:
                raise ApiError(400, "bad_feature", f"特征 {key} 缺少 value")
            revealed = raw.get("revealed")
            if revealed is None:
                revealed = not bool(raw.get("locked", False)) and default_revealed
            options = raw.get("options")
        else:
            value = raw
            revealed = default_revealed
            options = None
        schema_spec = schema.get(key, {}) if isinstance(schema.get(key), dict) else {}
        if options is None:
            options = schema_spec.get("options", [])
        specs[str(key)] = {
            "actual_value": str(value),
            "revealed": bool(revealed),
            "options": [str(x) for x in options],
        }

    for key, value in (payload.get("features") or {}).items():
        add(key, value, True)
    for key, value in (payload.get("recorded_features") or {}).items():
        add(key, value, True)
    for key, value in (payload.get("secret_features") or {}).items():
        add(key, value, True)
    for key, value in (payload.get("locked_features") or {}).items():
        add(key, value, False)
    return specs


def create_item(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    round_id = str(payload.get("round_id") or "")
    round_row = get_round(conn, round_id)
    schema = round_row.get("feature_schema", {})
    specs = _feature_specs(payload, schema)
    now = now_iso()

    conn.execute("BEGIN IMMEDIATE")
    try:
        item_no = str(payload.get("item_no") or "").strip() or next_number(conn, "item", "I")
        if conn.execute("SELECT 1 FROM items WHERE item_no=?", (item_no,)).fetchone():
            raise ApiError(409, "item_exists", f"实物编号已存在：{item_no}")
        conn.execute(
            "INSERT INTO items(item_no, round_id, label, status, appearance, created_at) VALUES (?,?,?,?,?,?)",
            (
                item_no,
                round_id,
                str(payload.get("label") or item_no),
                "in_stock",
                dumps(payload.get("appearance") or {}),
                now,
            ),
        )
        for key, spec in specs.items():
            conn.execute(
                "INSERT INTO item_features(item_no, feature_key, actual_value, options, revealed) VALUES (?,?,?,?,?)",
                (item_no, key, spec["actual_value"], dumps(spec["options"]), int(spec["revealed"])),
            )
        conn.commit()
    except ApiError:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ApiError(409, "item_conflict", str(exc)) from exc
    except Exception:
        conn.rollback()
        raise

    state = fetch_state(conn, round_id)
    return {"item": state["items_by_no"][item_no], "features": state["features"][item_no]}


def create_claim(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    round_id = str(payload.get("round_id") or "")
    round_row = get_round(conn, round_id)
    appearance = payload.get("appearance") or {}
    memory = payload.get("memory") or payload.get("remembered") or {}
    try:
        # Validate all constraints before writing.
        normalize_claim_statement(appearance, memory)
    except ValueError as exc:
        raise ApiError(400, "bad_memory", str(exc)) from exc

    willing = int(payload.get("willing_rounds", round_row["willing_round_default"]))
    if not 1 <= willing <= 10:
        raise ApiError(400, "bad_round_limit", "愿意复访的轮数应在 1 到 10 之间")

    statement = {
        "appearance": appearance,
        "memory": memory,
        "willing_rounds": willing,
    }
    now = now_iso()
    sealed_at = now  # Intake is the binding sealed statement before later prompts.

    conn.execute("BEGIN IMMEDIATE")
    try:
        claim_no = str(payload.get("claim_no") or "").strip() or next_number(conn, "claim", "C")
        if conn.execute("SELECT 1 FROM claims WHERE claim_no=?", (claim_no,)).fetchone():
            raise ApiError(409, "claim_exists", f"申领编号已存在：{claim_no}")
        conn.execute(
            """
            INSERT INTO claims(claim_no, round_id, claimant, appearance, memory,
                willing_rounds, status, statement_hash, sealed_at, created_at)
            VALUES (?,?,?,?,?,?, 'filed', ?,?,?)
            """,
            (
                claim_no,
                round_id,
                str(payload.get("claimant") or "匿名失主"),
                dumps(appearance),
                dumps(memory),
                willing,
                canonical_hash(statement),
                sealed_at,
                now,
            ),
        )
        conn.commit()
    except ApiError:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ApiError(409, "claim_conflict", str(exc)) from exc
    except Exception:
        conn.rollback()
        raise
    return owner_view(conn, claim_no)


def reconcile_round(conn: sqlite3.Connection, round_id: str) -> dict[str, Any]:
    state = fetch_state(conn, round_id)
    result = evaluate(state)
    result["round_id"] = round_id
    result["window_mode"] = state["round"]["window_mode"]
    result["budgets"] = _budget_view(conn, state["round"])
    return result


def _budget_view(conn: sqlite3.Connection, round_row: dict[str, Any]) -> dict[str, Any]:
    inspections_used = conn.execute(
        "SELECT COUNT(*) AS n FROM inspections WHERE round_id=?",
        (round_row["round_id"],),
    ).fetchone()["n"]
    return {
        "inspection_budget": round_row["inspection_budget"],
        "inspections_used": inspections_used,
        "inspections_remaining": max(0, round_row["inspection_budget"] - inspections_used),
        "prepare_budget_per_shift": round_row["prepare_budget"],
    }


def owner_view(conn: sqlite3.Connection, claim_no: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM claims WHERE claim_no=?", (claim_no,)).fetchone()
    if row is None:
        raise ApiError(404, "claim_not_found", f"没有申领编号：{claim_no}")
    claim = row_dict(row)
    assert claim is not None
    evaluation = reconcile_round(conn, claim["round_id"])
    mine = next((c for c in evaluation["claims"] if c["claim_no"] == claim_no), None)
    voucher = _active_voucher_for_claim(conn, claim_no)
    answers = conn.execute(
        "SELECT COUNT(*) AS n FROM claim_answers WHERE claim_no=?", (claim_no,)
    ).fetchone()["n"]

    assert mine is not None
    view: dict[str, Any] = {
        "claim_no": claim_no,
        "round_id": claim["round_id"],
        "status": claim["status"],
        "window_mode": evaluation["window_mode"],
        "revisit_rounds_used": answers,
        "revisit_rounds_allowed": claim["willing_rounds"],
    }

    if voucher:
        view["voucher_no"] = voucher["voucher_no"]
        view["item_no"] = voucher["item_no"]
        if voucher["status"] == "out":
            view["can_collect"] = False
            view["collected"] = True
            view["next_action"] = "collected"
            view["message"] = f"该凭条已于 {voucher['outbound_at']} 在 {voucher['outbound_window_id']} 号窗口完成出库。"
        else:
            view["can_collect"] = True
            view["eligible_to_sign"] = False
            view["next_action"] = "outbound_with_voucher"
            view["message"] = "签单已完成。请持领物凭条到任一出库窗口办理交接。"
        return view

    if mine["signable"]:
        view["can_claim"] = True
        view["eligible_to_sign"] = True
        view["next_action"] = "sign_voucher"
        view["message"] = "核对通过：本人可以领取确定对应的一件。下一步办理签单并取得领物凭条。"
    elif claim["status"] == "no_match":
        view["can_claim"] = False
        view["next_action"] = "closed_no_match"
        view["message"] = "本申领已显式登记为无对应实物。"
    else:
        view["can_claim"] = False
        view["eligible_to_sign"] = False
        view["next_action"] = mine["next_action"]
        if evaluation["window_mode"] == "sealed":
            view["message"] = "目前仍有歧义，待保管员按预算安排开袋检查；请等候通知，不要凭排除法签领。"
        else:
            remaining = claim["willing_rounds"] - answers
            if remaining > 0:
                view["message"] = f"目前仍有歧义，待核。可按安排再来做两选一复访；本人还可回答 {remaining} 轮。"
            else:
                view["message"] = "目前仍有歧义，且本人愿答轮数已用完，继续待核。"
    return view


def _active_voucher_for_claim(conn: sqlite3.Connection, claim_no: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM vouchers WHERE claim_no=? AND status IN ('signed','out') ORDER BY signed_at DESC LIMIT 1",
        (claim_no,),
    ).fetchone()
    return row_dict(row)


def _active_voucher_for_item(conn: sqlite3.Connection, item_no: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM vouchers WHERE item_no=? AND status IN ('signed','out') ORDER BY signed_at DESC LIMIT 1",
        (item_no,),
    ).fetchone()
    return row_dict(row)


def import_confirmed_pairs(
    conn: sqlite3.Connection, payload: dict[str, Any]
) -> dict[str, Any]:
    """Import pairs already settled by another hall procedure.

    These are explicit fixed allocations.  ``handed_out=true`` also records the
    external handoff; otherwise a signed local voucher is created so either
    outbound window can redeem it atomically.
    """
    round_id = str(payload.get("round_id") or "")
    get_round(conn, round_id)
    pairs = payload.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ApiError(400, "pairs_required", "请给出别的手续已确认的 pairs 列表")
    handed_out = bool(payload.get("handed_out", False))
    external_ref = str(payload.get("external_ref") or "external-procedure")

    conn.execute("BEGIN IMMEDIATE")
    imported: list[dict[str, Any]] = []
    try:
        now = now_iso()
        for pair in pairs:
            claim_no = str(pair.get("claim_no") or "")
            item_no = str(pair.get("item_no") or "")
            if not claim_no or not item_no:
                raise ApiError(400, "bad_pair", "每个确认对都需要 claim_no 和 item_no")

            claim = conn.execute(
                "SELECT round_id, status FROM claims WHERE claim_no=?", (claim_no,)
            ).fetchone()
            item = conn.execute(
                "SELECT round_id, status FROM items WHERE item_no=?", (item_no,)
            ).fetchone()
            if claim is None or claim["round_id"] != round_id:
                raise ApiError(404, "claim_not_in_round", f"{claim_no} 不属于 {round_id}")
            if item is None or item["round_id"] != round_id:
                raise ApiError(404, "item_not_in_round", f"{item_no} 不属于 {round_id}")

            existing_confirmed = conn.execute(
                "SELECT * FROM confirmed_pairs WHERE claim_no=? OR item_no=?",
                (claim_no, item_no),
            ).fetchall()
            for row in existing_confirmed:
                d = dict(row)
                if d["claim_no"] == claim_no and d["item_no"] == item_no:
                    continue
                raise ApiError(409, "confirmed_pair_conflict", "申领或实物已在别的确认对中")

            existing_voucher = _active_voucher_for_claim(conn, claim_no) or _active_voucher_for_item(
                conn, item_no
            )
            if existing_voucher:
                if (
                    existing_voucher["claim_no"] == claim_no
                    and existing_voucher["item_no"] == item_no
                ):
                    imported.append(row_dict(existing_voucher) or {})
                    continue
                raise ApiError(409, "active_voucher_conflict", "申领或实物已有不同的有效凭条")

            conn.execute(
                """
                INSERT OR IGNORE INTO confirmed_pairs
                    (round_id, claim_no, item_no, external_ref, created_at)
                VALUES (?,?,?,?,?)
                """,
                (round_id, claim_no, item_no, external_ref, now),
            )
            voucher_no = next_number(conn, "voucher", "P")
            status = "out" if handed_out else "signed"
            conn.execute(
                """
                INSERT INTO vouchers(voucher_no, round_id, claim_no, item_no, status,
                    sign_window_id, signed_at, outbound_window_id, outbound_at)
                VALUES (?,?,?,?,?, ?,?, ?,?)
                """,
                (
                    voucher_no,
                    round_id,
                    claim_no,
                    item_no,
                    status,
                    external_ref,
                    now,
                    external_ref if handed_out else "",
                    now if handed_out else None,
                ),
            )
            if handed_out:
                conn.execute(
                    """
                    INSERT INTO handoffs(voucher_no, item_no, window_id, request_id, handed_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (voucher_no, item_no, f"external:{external_ref}", external_ref, now),
                )
                conn.execute("UPDATE claims SET status='out' WHERE claim_no=?", (claim_no,))
                conn.execute("UPDATE items SET status='out' WHERE item_no=?", (item_no,))
            else:
                conn.execute("UPDATE claims SET status='approved' WHERE claim_no=?", (claim_no,))
                conn.execute("UPDATE items SET status='reserved' WHERE item_no=?", (item_no,))

            row = conn.execute("SELECT * FROM vouchers WHERE voucher_no=?", (voucher_no,)).fetchone()
            imported.append(row_dict(row) or {})

        conn.commit()
    except ApiError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise

    return {"round_id": round_id, "handed_out": handed_out, "imported": imported}


def create_voucher(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> dict[str, Any]:
    claim_no = str(payload.get("claim_no") or "")
    requested_item = payload.get("item_no")
    window_id = str(payload.get("window_id") or payload.get("sign_window_id") or "desk")
    if not claim_no:
        raise ApiError(400, "claim_no_required", "签单需要 claim_no")

    conn.execute("BEGIN IMMEDIATE")
    try:
        voucher = _create_voucher_locked(
            conn, claim_no, None if requested_item is None else str(requested_item), window_id
        )
        conn.commit()
    except ApiError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return voucher


def _create_voucher_locked(
    conn: sqlite3.Connection,
    claim_no: str,
    requested_item: str | None,
    window_id: str,
) -> dict[str, Any]:
    claim_row = conn.execute("SELECT * FROM claims WHERE claim_no=?", (claim_no,)).fetchone()
    if claim_row is None:
        raise ApiError(404, "claim_not_found", f"没有申领编号：{claim_no}")
    claim = row_dict(claim_row)
    assert claim is not None

    existing = _active_voucher_for_claim(conn, claim_no)
    if existing:
        if requested_item and requested_item != existing["item_no"]:
            raise ApiError(409, "claim_already_bound_other_item", "该申领已有有效凭条且实物不同")
        existing["idempotent"] = True
        return existing

    if claim["status"] in ("no_match", "cancelled"):
        raise ApiError(409, "claim_closed", f"申领状态为 {claim['status']}，不能签单")

    evaluation = reconcile_round(conn, claim["round_id"])
    mine = next((c for c in evaluation["claims"] if c["claim_no"] == claim_no), None)
    if mine is None:
        raise ApiError(500, "evaluation_missing_claim", "核对结果缺少本申领")

    if not mine["signable"]:
        raise ApiError(
            422,
            "not_signable",
            "尚不能唯一签出：" + mine["reason"],
            {"reason_code": mine["reason_code"], "next_action": mine["next_action"]},
        )
    forced = mine["forced_item_no"]
    assert forced
    if requested_item and requested_item != forced:
        raise ApiError(
            422,
            "item_not_forced",
            f"核对唯一指向 {forced}，不能改签 {requested_item}",
            {"forced_item_no": forced},
        )

    item_row = conn.execute("SELECT status FROM items WHERE item_no=?", (forced,)).fetchone()
    if item_row is None or item_row["status"] != "in_stock":
        raise ApiError(409, "item_not_in_stock", "唯一指向的实物当前不在可签库存中")

    voucher_no = next_number(conn, "voucher", "P")
    now = now_iso()
    conn.execute(
        """
        INSERT INTO vouchers(voucher_no, round_id, claim_no, item_no, status,
            sign_window_id, signed_at)
        VALUES (?,?,?,?, 'signed', ?,?)
        """,
        (voucher_no, claim["round_id"], claim_no, forced, window_id, now),
    )
    conn.execute("UPDATE claims SET status='approved' WHERE claim_no=?", (claim_no,))
    conn.execute("UPDATE items SET status='reserved' WHERE item_no=?", (forced,))
    row = conn.execute("SELECT * FROM vouchers WHERE voucher_no=?", (voucher_no,)).fetchone()
    result = row_dict(row)
    assert result is not None
    result["message"] = "签单成功；凭条是出库交接的唯一凭证。"
    return result


def sign_determinate(
    conn: sqlite3.Connection, round_id: str, window_id: str = "desk"
) -> dict[str, Any]:
    get_round(conn, round_id)
    conn.execute("BEGIN IMMEDIATE")
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        evaluation = reconcile_round(conn, round_id)
        targets = [c["claim_no"] for c in evaluation["claims"] if c.get("signable")]
        for claim_no in targets:
            try:
                created.append(_create_voucher_locked(conn, claim_no, None, window_id))
            except ApiError as exc:
                skipped.append({"claim_no": claim_no, "code": exc.code, "message": exc.message})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "round_id": round_id,
        "created_count": len(created),
        "created": created,
        "skipped": skipped,
        "remaining_reconcile": reconcile_round(conn, round_id),
    }


def create_inspection(
    conn: sqlite3.Connection, payload: dict[str, Any]
) -> dict[str, Any]:
    item_no = str(payload.get("item_no") or "")
    inspector = str(payload.get("inspector") or payload.get("window_id") or "custodian")
    if not item_no:
        raise ApiError(400, "item_no_required", "检查需要 item_no")

    conn.execute("BEGIN IMMEDIATE")
    try:
        item_row = conn.execute("SELECT * FROM items WHERE item_no=?", (item_no,)).fetchone()
        if item_row is None:
            raise ApiError(404, "item_not_found", f"没有实物编号：{item_no}")
        item = row_dict(item_row)
        assert item is not None
        round_row = get_round(conn, item["round_id"])
        if round_row["window_mode"] != "sealed":
            raise ApiError(409, "wrong_window_mode", "本轮是复访窗，不能改用封存窗开袋检查")

        used = conn.execute(
            "SELECT COUNT(*) AS n FROM inspections WHERE round_id=?", (item["round_id"],)
        ).fetchone()["n"]
        if used >= round_row["inspection_budget"]:
            raise ApiError(409, "inspection_budget_exhausted", "保管员本轮开袋检查预算已用完")

        rows = conn.execute(
            "SELECT * FROM item_features WHERE item_no=? AND revealed=0", (item_no,)
        ).fetchall()
        if not rows:
            raise ApiError(422, "nothing_sealed", "这件没有尚未揭示的锁内特征")

        results: dict[str, str] = {}
        for row in rows:
            feature = row_dict(row)
            assert feature is not None
            results[feature["feature_key"]] = feature["actual_value"]
            conn.execute(
                "UPDATE item_features SET revealed=1 WHERE item_no=? AND feature_key=?",
                (item_no, feature["feature_key"]),
            )

        inspection_no = next_number(conn, "inspection", "J")
        now = now_iso()
        conn.execute(
            """
            INSERT INTO inspections(inspection_no, round_id, item_no, results,
                inspector, window_id, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                inspection_no,
                item["round_id"],
                item_no,
                dumps(results),
                inspector,
                str(payload.get("window_id") or ""),
                now,
            ),
        )
        conn.commit()
    except ApiError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise

    return {
        "inspection_no": inspection_no,
        "item_no": item_no,
        "results": results,
        "message": "真实开袋检查已登记，锁内结果现已可用于核对。",
        "reconcile": reconcile_round(conn, item["round_id"]),
    }


def plan_inspections(conn: sqlite3.Connection, round_id: str) -> dict[str, Any]:
    round_row = get_round(conn, round_id)
    if round_row["window_mode"] != "sealed":
        raise ApiError(409, "wrong_window_mode", "只有封存窗需要开袋检查计划")
    state = fetch_state(conn, round_id)
    baseline = evaluate(state)
    baseline_signable = int(baseline.get("summary", {}).get("signable", 0))
    schema = round_row["feature_schema"]

    plans = []
    for item_no, item in state["items_by_no"].items():
        unrevealed = [
            key for key, f in state["features"].get(item_no, {}).items()
            if not int(f.get("revealed", 0))
        ]
        if not unrevealed:
            continue

        option_lists = []
        feature_options: dict[str, list[str]] = {}
        for key in unrevealed:
            options = state["features"][item_no][key].get("options") or []
            if not options:
                spec = schema.get(key, {}) if isinstance(schema.get(key), dict) else {}
                options = spec.get("options", [])
            options = [str(x) for x in options]
            if len(options) < 2:
                options = [state["features"][item_no][key]["actual_value"]]
            feature_options[key] = options
            option_lists.append(options)

        outcomes = []
        import itertools

        for combo in itertools.islice(itertools.product(*option_lists), 64):
            overrides = {(item_no, key): value for key, value in zip(unrevealed, combo)}
            simulated = with_feature_overrides(state, overrides)
            outcomes.append(int(evaluate(simulated).get("summary", {}).get("signable", 0)))

        if not outcomes:
            outcomes = [baseline_signable]
        plans.append(
            {
                "item_no": item_no,
                "unrevealed_features": unrevealed,
                "options": feature_options,
                "simulated_outcomes": len(outcomes),
                "baseline_signable": baseline_signable,
                "worst_case_signable": min(outcomes),
                "best_case_signable": max(outcomes),
                "average_signable": sum(outcomes) / len(outcomes),
                "worst_case_gain": min(outcomes) - baseline_signable,
                "average_gain": sum(outcomes) / len(outcomes) - baseline_signable,
                "best_case_gain": max(outcomes) - baseline_signable,
            }
        )

    plans.sort(
        key=lambda p: (
            p["worst_case_gain"],
            p["average_gain"],
            p["best_case_gain"],
            -len(p["unrevealed_features"]),
        ),
        reverse=True,
    )
    return {
        "round_id": round_id,
        "baseline_signable": baseline_signable,
        "recommended": plans[0] if plans else None,
        "ranked": plans,
        "budget": _budget_view(conn, round_row),
    }


def recommend_question(
    conn: sqlite3.Connection, round_id: str, claim_no: str | None = None
) -> dict[str, Any] | None:
    round_row = get_round(conn, round_id)
    if round_row["window_mode"] != "revisit":
        raise ApiError(409, "wrong_window_mode", "只有复访窗需要两选一题")
    state = fetch_state(conn, round_id)
    schema = round_row["feature_schema"]
    baseline = evaluate(state)
    baseline_signable = int(baseline.get("summary", {}).get("signable", 0))

    claim_nos = [claim_no] if claim_no else [c["claim_no"] for c in state["claims"] if c["status"] == "filed"]
    best: dict[str, Any] | None = None

    for c_no in claim_nos:
        if c_no not in state["claims_by_no"]:
            raise ApiError(404, "claim_not_found", f"没有申领编号：{c_no}")
        used = conn.execute(
            "SELECT feature_key FROM claim_answers WHERE claim_no=?", (c_no,)
        ).fetchall()
        answered = {r["feature_key"] for r in used}
        answer_count = len(used)
        claim = state["claims_by_no"][c_no]
        if answer_count >= claim["willing_rounds"]:
            continue

        for key, spec in schema.items():
            if not isinstance(spec, dict) or len(spec.get("options", [])) != 2:
                continue
            if key in answered:
                continue
            constraint = claim.get("constraints", {}).get(key)
            if constraint and constraint.get("kind") == "certain":
                continue
            options = [str(x) for x in spec["options"]]
            outcome_counts = []
            for answer in options:
                simulated = with_feature_overrides(state, {})
                simulated_claim = simulated["claims_by_no"][c_no]
                simulated_claim.setdefault("constraints", {})[key] = {
                    "kind": "certain",
                    "allowed": [answer],
                }
                outcome_counts.append(
                    int(evaluate(simulated).get("summary", {}).get("signable", 0))
                )
            question = {
                "claim_no": c_no,
                "round_no": answer_count + 1,
                "feature_key": key,
                "label": spec.get("label", key),
                "prompt": spec.get("prompt", f"请当面辨认并选择 {spec.get('label', key)}"),
                "options": options,
                "baseline_signable": baseline_signable,
                "worst_case_signable": min(outcome_counts),
                "best_case_signable": max(outcome_counts),
                "average_signable": sum(outcome_counts) / len(outcome_counts),
            }
            if best is None or (
                question["best_case_signable"],
                question["average_signable"],
            ) > (
                best["best_case_signable"],
                best["average_signable"],
            ):
                best = question
    return best


def answer_revisit(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    claim_no = str(payload.get("claim_no") or "")
    answer = str(payload.get("answer") or "")
    requested_key = payload.get("feature_key")
    window_id = str(payload.get("window_id") or "revisit-window")
    if not claim_no or not answer:
        raise ApiError(400, "claim_no_and_answer_required", "复访回答需要 claim_no 和 answer")

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM claims WHERE claim_no=?", (claim_no,)).fetchone()
        if row is None:
            raise ApiError(404, "claim_not_found", f"没有申领编号：{claim_no}")
        claim = row_dict(row)
        assert claim is not None
        round_row = get_round(conn, claim["round_id"])
        if round_row["window_mode"] != "revisit":
            raise ApiError(409, "wrong_window_mode", "本轮是封存窗，不能用复访回答替代检查")

        used_rows = conn.execute(
            "SELECT feature_key FROM claim_answers WHERE claim_no=? ORDER BY round_no",
            (claim_no,),
        ).fetchall()
        next_round = len(used_rows) + 1
        if next_round > claim["willing_rounds"]:
            raise ApiError(409, "revisit_round_limit", "失主愿答轮数已用完，不能再登记新一轮")

        answered_features = {r["feature_key"] for r in used_rows}
        feature_key = str(requested_key) if requested_key else None
        if feature_key is None:
            suggested = recommend_question(conn, claim["round_id"], claim_no)
            if not suggested:
                raise ApiError(422, "no_question_available", "当前没有可问的两选一题")
            feature_key = suggested["feature_key"]

        schema = round_row["feature_schema"]
        spec = schema.get(feature_key)
        if not isinstance(spec, dict) or len(spec.get("options", [])) != 2:
            raise ApiError(400, "not_two_choice", "复访题目必须是登记在 feature_schema 中的两选一特征")
        options = [str(x) for x in spec["options"]]
        if answer not in options:
            raise ApiError(400, "bad_answer", f"回答必须是 {options} 之一")
        if feature_key in answered_features:
            raise ApiError(409, "answer_already_bound", "该轮题目已有约束力回答，不能补做或改写前访")

        shift_id = str(payload.get("shift_id") or f"shift-{claim['round_id']}")
        # Optional atomic registration of the objects this shift prepared.
        # This is where the custodian's per-shift object budget binds a visit.
        prepared_item_nos = payload.get("prepared_item_nos")
        if prepared_item_nos is not None:
            if not isinstance(prepared_item_nos, list):
                raise ApiError(400, "bad_prepared_items", "prepared_item_nos 必须是数组")
            _prepare_shift_locked(conn, shift_id, prepared_item_nos)
        elif not conn.execute("SELECT 1 FROM shifts WHERE shift_id=?", (shift_id,)).fetchone():
            raise ApiError(404, "shift_not_found", f"没有班次：{shift_id}")

        state = fetch_state(conn, claim["round_id"])
        before = evaluate(state)
        mine_before = next(c for c in before["claims"] if c["claim_no"] == claim_no)
        question_snapshot = {
            "round_no": next_round,
            "feature_key": feature_key,
            "label": spec.get("label", feature_key),
            "prompt": spec.get("prompt", f"请当面辨认并选择 {spec.get('label', feature_key)}"),
            "options": options,
            "candidate_item_nos_at_ask": mine_before.get("candidate_item_nos", []),
            "asked_at": now_iso(),
        }

        memory = loads(claim["memory"], {})
        memory[feature_key] = {"kind": "certain", "allowed": [answer]}
        conn.execute(
            "UPDATE claims SET memory=? WHERE claim_no=?",
            (dumps(memory), claim_no),
        )
        now = now_iso()
        conn.execute(
            """
            INSERT INTO claim_answers(claim_no, round_no, feature_key,
                question_snapshot, options, answer_value, window_id,
                shift_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                claim_no,
                next_round,
                feature_key,
                dumps(question_snapshot),
                dumps(options),
                answer,
                window_id,
                shift_id,
                now,
            ),
        )
        conn.commit()
    except ApiError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise

    return {
        "claim_no": claim_no,
        "round_no": next_round,
        "feature_key": feature_key,
        "answer": answer,
        "question": question_snapshot,
        "owner_view": owner_view(conn, claim_no),
        "reconcile": reconcile_round(conn, claim["round_id"]),
    }


def create_shift(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    shift_id = str(payload.get("shift_id") or "")
    budget = int(payload.get("prepare_budget", 4))
    if not shift_id or budget < 0:
        raise ApiError(400, "bad_shift", "需要 shift_id 和非负 prepare_budget")
    now = now_iso()
    conn.execute(
        """
        INSERT INTO shifts(shift_id, prepare_budget, created_at) VALUES (?,?,?)
        ON CONFLICT(shift_id) DO UPDATE SET prepare_budget=excluded.prepare_budget
        """,
        (shift_id, budget, now),
    )
    return get_shift(conn, shift_id)


def get_shift(conn: sqlite3.Connection, shift_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM shifts WHERE shift_id=?", (shift_id,)).fetchone()
    if row is None:
        raise ApiError(404, "shift_not_found", f"没有班次：{shift_id}")
    shift = row_dict(row)
    assert shift is not None
    items = [
        r["item_no"]
        for r in conn.execute(
            "SELECT item_no FROM shift_preparations WHERE shift_id=? ORDER BY item_no",
            (shift_id,),
        ).fetchall()
    ]
    shift["prepared_item_nos"] = items
    shift["prepared_count"] = len(items)
    shift["remaining"] = max(0, int(shift["prepare_budget"]) - len(items))
    return shift


def prepare_shift(conn: sqlite3.Connection, shift_id: str, item_nos: list[str]) -> dict[str, Any]:
    if not isinstance(item_nos, list) or not item_nos:
        raise ApiError(400, "item_nos_required", "请给出本班准备的 item_nos")
    conn.execute("BEGIN IMMEDIATE")
    try:
        _prepare_shift_locked(conn, shift_id, item_nos)
        conn.commit()
    except ApiError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return get_shift(conn, shift_id)


def _prepare_shift_locked(
    conn: sqlite3.Connection, shift_id: str, item_nos: list[str]
) -> dict[str, Any]:
    shift = get_shift(conn, shift_id)
    now = now_iso()
    for item_no in item_nos:
        if not conn.execute("SELECT 1 FROM items WHERE item_no=?", (item_no,)).fetchone():
            raise ApiError(404, "item_not_found", f"没有实物编号：{item_no}")
        conn.execute(
            "INSERT OR IGNORE INTO shift_preparations(shift_id, item_no, prepared_at) VALUES (?,?,?)",
            (shift_id, str(item_no), now),
        )
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM shift_preparations WHERE shift_id=?", (shift_id,)
    ).fetchone()["n"]
    if count > shift["prepare_budget"]:
        raise ApiError(
            409,
            "prepare_budget_exhausted",
            f"本班最多准备 {shift['prepare_budget']} 件不同实物，现已登记 {count} 件",
        )
    return get_shift(conn, shift_id)


def outbound(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    voucher_no = str(payload.get("voucher_no") or "")
    window_id = str(payload.get("window_id") or "")
    request_id = str(payload.get("request_id") or "")
    if not voucher_no or not window_id:
        raise ApiError(400, "voucher_and_window_required", "出库需要 voucher_no 和 window_id")

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM vouchers WHERE voucher_no=?", (voucher_no,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO outbound_attempts(voucher_no, window_id, request_id, outcome, detail, created_at) VALUES (?,?,?,?,?,?)",
                (voucher_no, window_id, request_id, "not_found", "voucher not found", now_iso()),
            )
            conn.commit()
            raise ApiError(404, "voucher_not_found", f"没有凭条：{voucher_no}")

        voucher = row_dict(row)
        assert voucher is not None
        if voucher["status"] == "out":
            handoff = conn.execute(
                "SELECT * FROM handoffs WHERE voucher_no=?", (voucher_no,)
            ).fetchone()
            assert handoff is not None
            same_request = request_id and handoff["request_id"] == request_id
            conn.execute(
                "INSERT INTO outbound_attempts(voucher_no, window_id, request_id, outcome, detail, created_at) VALUES (?,?,?,?,?,?)",
                (
                    voucher_no,
                    window_id,
                    request_id,
                    "idempotent_replay" if same_request else "conflict_already_out",
                    f"already handed at {handoff['window_id']}",
                    now_iso(),
                ),
            )
            conn.commit()
            if same_request:
                result = row_dict(handoff)
                assert result is not None
                result["idempotent"] = True
                result["message"] = "同一 request_id 的重试返回原交接回执。"
                return result
            raise ApiError(
                409,
                "voucher_already_redeemed",
                f"凭条已在 {handoff['window_id']} 号窗口完成交接，不能再次出库。",
                {"winning_window_id": handoff["window_id"], "handed_at": handoff["handed_at"]},
            )

        cur = conn.execute(
            """
            UPDATE vouchers
            SET status='out', outbound_window_id=?, outbound_at=?, request_id=?
            WHERE voucher_no=? AND status='signed'
            """,
            (window_id, now_iso(), request_id, voucher_no),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise ApiError(409, "outbound_race_lost", "另一窗口已先完成同一凭条出库")

        now = now_iso()
        conn.execute(
            "UPDATE items SET status='out' WHERE item_no=?", (voucher["item_no"],)
        )
        conn.execute(
            "UPDATE claims SET status='out' WHERE claim_no=?", (voucher["claim_no"],)
        )
        conn.execute(
            "INSERT INTO handoffs(voucher_no, item_no, window_id, request_id, handed_at) VALUES (?,?,?,?,?)",
            (voucher_no, voucher["item_no"], window_id, request_id, now),
        )
        conn.execute(
            "INSERT INTO outbound_attempts(voucher_no, window_id, request_id, outcome, detail, created_at) VALUES (?,?,?,?,?,?)",
            (voucher_no, window_id, request_id, "handed", "one successful handoff", now),
        )
        conn.commit()
    except ApiError:
        # ApiError after commit/not-found? If transaction still active rollback safe.
        try:
            conn.rollback()
        except sqlite3.OperationalError:
            pass
        raise
    except Exception:
        conn.rollback()
        raise

    handoff_row = conn.execute(
        "SELECT * FROM handoffs WHERE voucher_no=?", (voucher_no,)
    ).fetchone()
    result = row_dict(handoff_row)
    assert result is not None
    result["message"] = "出库成功；该凭条在其他窗口将无法再次交接。"
    return result


def claim_record(conn: sqlite3.Connection, claim_no: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM claims WHERE claim_no=?", (claim_no,)).fetchone()
    if row is None:
        raise ApiError(404, "claim_not_found", f"没有申领编号：{claim_no}")
    claim = row_dict(row)
    assert claim is not None
    claim["appearance"] = loads(claim.get("appearance"), {})
    claim["memory"] = loads(claim.get("memory"), {})

    answers = []
    for a in conn.execute(
        "SELECT * FROM claim_answers WHERE claim_no=? ORDER BY round_no", (claim_no,)
    ).fetchall():
        answer = row_dict(a)
        assert answer is not None
        answer["question_snapshot"] = loads(answer.get("question_snapshot"), {})
        answer["options"] = loads(answer.get("options"), [])
        answers.append(answer)

    vouchers = [
        row_dict(r)
        for r in conn.execute(
            "SELECT voucher_no,item_no,status,signed_at,outbound_at,outbound_window_id FROM vouchers WHERE claim_no=? ORDER BY signed_at",
            (claim_no,),
        ).fetchall()
    ]
    claim["answers"] = answers
    claim["vouchers"] = vouchers
    claim["owner_view"] = owner_view(conn, claim_no)
    return claim


def mark_claim_no_match(conn: sqlite3.Connection, claim_no: str, note: str) -> dict[str, Any]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status FROM claims WHERE claim_no=?", (claim_no,)).fetchone()
        if row is None:
            raise ApiError(404, "claim_not_found", claim_no)
        if _active_voucher_for_claim(conn, claim_no):
            raise ApiError(409, "claim_has_voucher", "已有有效凭条，不能记为无匹配")
        conn.execute(
            "UPDATE claims SET status='no_match', disposition_note=? WHERE claim_no=?",
            (note or "registrar explicitly left claim unmatched", claim_no),
        )
        conn.commit()
    except ApiError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return owner_view(conn, claim_no)


def mark_item_unclaimed(conn: sqlite3.Connection, item_no: str, note: str) -> dict[str, Any]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status FROM items WHERE item_no=?", (item_no,)).fetchone()
        if row is None:
            raise ApiError(404, "item_not_found", item_no)
        if _active_voucher_for_item(conn, item_no):
            raise ApiError(409, "item_has_voucher", "已有有效凭条，不能记为无人认领")
        conn.execute(
            "UPDATE items SET status='unclaimed', disposition_note=? WHERE item_no=?",
            (note or "registrar explicitly left item unclaimed", item_no),
        )
        conn.commit()
    except ApiError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return public_item_view(conn, item_no)


def public_item_view(conn: sqlite3.Connection, item_no: str) -> dict[str, Any]:
    row = conn.execute("SELECT item_no, round_id, label, status, disposition_note FROM items WHERE item_no=?", (item_no,)).fetchone()
    if row is None:
        raise ApiError(404, "item_not_found", f"没有实物编号：{item_no}")
    return dict(row)


def voucher_view(conn: sqlite3.Connection, voucher_no: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM vouchers WHERE voucher_no=?", (voucher_no,)).fetchone()
    if row is None:
        raise ApiError(404, "voucher_not_found", f"没有凭条：{voucher_no}")
    voucher = row_dict(row)
    assert voucher is not None
    item = public_item_view(conn, voucher["item_no"])
    handoff = conn.execute(
        "SELECT window_id, handed_at FROM handoffs WHERE voucher_no=?", (voucher_no,)
    ).fetchone()
    voucher["item"] = item
    voucher["handoff"] = row_dict(handoff)
    if voucher["status"] == "signed":
        voucher["next_action"] = "outbound_with_voucher"
    else:
        voucher["next_action"] = "collected"
    return voucher


def track_number(conn: sqlite3.Connection, number: str) -> dict[str, Any]:
    number = str(number or "").strip()
    if not number:
        raise ApiError(400, "number_required", "请提供要复查的编号")

    claim = conn.execute("SELECT 1 FROM claims WHERE claim_no=?", (number,)).fetchone()
    if claim:
        return {"type": "claim", "view": owner_view(conn, number)}

    voucher = conn.execute("SELECT 1 FROM vouchers WHERE voucher_no=?", (number,)).fetchone()
    if voucher:
        return {"type": "voucher", "view": voucher_view(conn, number)}

    item = conn.execute("SELECT 1 FROM items WHERE item_no=?", (number,)).fetchone()
    if item:
        return {"type": "item", "view": public_item_view(conn, number)}

    inspection = conn.execute(
        "SELECT * FROM inspections WHERE inspection_no=?", (number,)
    ).fetchone()
    if inspection:
        data = row_dict(inspection)
        assert data is not None
        data["results"] = loads(data["results"], {})
        return {"type": "inspection", "view": data}

    shift = conn.execute("SELECT 1 FROM shifts WHERE shift_id=?", (number,)).fetchone()
    if shift:
        return {"type": "shift", "view": get_shift(conn, number)}

    raise ApiError(404, "number_not_found", f"查无此号：{number}")


def stock_check(conn: sqlite3.Connection) -> dict[str, Any]:
    item_counts = {
        r["status"]: r["n"]
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status").fetchall()
    }
    voucher_counts = {
        r["status"]: r["n"]
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM vouchers GROUP BY status").fetchall()
    }
    handoff_count = conn.execute("SELECT COUNT(*) AS n FROM handoffs").fetchone()["n"]
    mismatches: list[str] = []

    for row in conn.execute(
        """
        SELECT v.voucher_no, v.item_no, v.status AS voucher_status,
               i.status AS item_status, h.voucher_no AS handoff_no,
               h.window_id AS handoff_window
        FROM vouchers v
        JOIN items i ON i.item_no=v.item_no
        LEFT JOIN handoffs h ON h.voucher_no=v.voucher_no
        WHERE v.status IN ('signed','out')
        """
    ).fetchall():
        d = dict(row)
        if d["voucher_status"] == "signed" and d["item_status"] != "reserved":
            mismatches.append(f"{d['voucher_no']}: signed but item {d['item_no']} is {d['item_status']}")
        if d["voucher_status"] == "out":
            if d["item_status"] != "out":
                mismatches.append(f"{d['voucher_no']}: out but item is {d['item_status']}")
            if not d["handoff_no"]:
                mismatches.append(f"{d['voucher_no']}: out without handoff row")

    for row in conn.execute(
        """
        SELECT h.voucher_no, h.item_no, i.status AS item_status, v.status AS voucher_status
        FROM handoffs h
        JOIN items i ON i.item_no=h.item_no
        JOIN vouchers v ON v.voucher_no=h.voucher_no
        """
    ).fetchall():
        d = dict(row)
        if d["item_status"] != "out" or d["voucher_status"] != "out":
            mismatches.append(f"handoff {d['voucher_no']} does not match voucher/item status")

    return {
        "consistent": not mismatches,
        "item_status_counts": item_counts,
        "voucher_status_counts": voucher_counts,
        "handoff_count": handoff_count,
        "mismatches": mismatches,
    }
