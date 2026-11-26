from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .db import connect


class ApiError(Exception):
    def __init__(
        self,
        status: int = 400,
        code: str = "bad_request",
        message: str = "请求不合法",
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def utc_now(value: Optional[str] = None) -> str:
    if value:
        return value
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class FamilyPoolService:
    """Transactional application service.

    The HTTP layer is deliberately thin; all business decisions live here so
    tests and the replay script can call the same code.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.conn = connect(db_path)
        self.lock = threading.RLock()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ---------- low-level helpers ----------

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    def _get(self, table: str, item_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "not_found", f"{table} 记录不存在: {item_id}")
        return dict(row)

    def _person(self, wristband_or_person_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM persons WHERE id = ? OR band_code = ?",
            (wristband_or_person_id, wristband_or_person_id),
        ).fetchone()
        if row is None:
            raise ApiError(
                404,
                "wristband_not_found",
                f"腕带/人员不存在: {wristband_or_person_id}",
            )
        return dict(row)

    def _ledger(
        self,
        entry_type: str,
        summary: str,
        *,
        session_id: Optional[str] = None,
        ref_type: str = "",
        ref_id: str = "",
        payload: Optional[Dict[str, Any]] = None,
        at: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO ledger_entries(
                at, session_id, entry_type, ref_type, ref_id,
                summary, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(at),
                session_id,
                entry_type,
                ref_type,
                ref_id,
                summary,
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )

    def _idempotent(self, table: str, key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        row = self.conn.execute(
            f"SELECT * FROM {table} WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return self._row(row)

    def _active_slot_numbers(self, adult_id: str, session_id: Optional[str] = None) -> List[int]:
        if session_id:
            rows = self.conn.execute(
                """
                SELECT slot_no FROM slot_allocations
                WHERE adult_id = ? AND session_id = ?
                  AND status IN ('reserved', 'consumed')
                ORDER BY slot_no
                """,
                (adult_id, session_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT slot_no FROM slot_allocations
                WHERE adult_id = ? AND status IN ('reserved', 'consumed')
                ORDER BY slot_no
                """,
                (adult_id,),
            ).fetchall()
        return [r["slot_no"] for r in rows]

    def _reserve_slot(
        self,
        *,
        session_id: str,
        adult_id: str,
        child_id: str,
        purpose: str,
        ref_type: str,
        ref_id: str,
        at: str,
        allocation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        consume: bool = False,
    ) -> Dict[str, Any]:
        adult = self._get("persons", adult_id)
        if adult["role"] != "adult":
            raise ApiError(422, "not_an_adult", f"{adult['display_name']} 不是成人腕带")

        existing_pair = self.conn.execute(
            """
            SELECT * FROM slot_allocations
            WHERE session_id = ? AND adult_id = ? AND child_id = ?
              AND status IN ('reserved', 'consumed')
            """,
            (session_id, adult_id, child_id),
        ).fetchone()
        if existing_pair is not None:
            return dict(existing_pair)

        used = set(self._active_slot_numbers(adult_id, session_id))
        capacity = int(adult["care_capacity"])
        free = [n for n in range(1, capacity + 1) if n not in used]
        if not free:
            raise ApiError(
                409,
                "no_free_slot",
                f"{adult['display_name']} 的 {capacity} 个陪同名额已全部占用",
                {
                    "adult_id": adult_id,
                    "capacity": capacity,
                    "used_slot_numbers": sorted(used),
                    "requested_child_id": child_id,
                },
            )

        slot_no = free[0]
        alloc_id = allocation_id or new_id("SLOT")
        status = "consumed" if consume else "reserved"
        try:
            self.conn.execute(
                """
                INSERT INTO slot_allocations(
                    id, session_id, adult_id, slot_no, child_id, purpose,
                    ref_type, ref_id, status, idempotency_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alloc_id,
                    session_id,
                    adult_id,
                    slot_no,
                    child_id,
                    purpose,
                    ref_type,
                    ref_id,
                    status,
                    idempotency_key,
                    at,
                    at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ApiError(
                409,
                "slot_reused",
                "陪同名额已被其他受理占用，不能重复使用",
                {"adult_id": adult_id, "slot_no": slot_no},
            ) from exc
        return self._get("slot_allocations", alloc_id)

    # ---------- venue graph ----------

    def create_zone(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                zone_id = data.get("id") or new_id("ZONE")
                self.conn.execute(
                    """
                    INSERT INTO zones(id, name, sort_no, active)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        sort_no=excluded.sort_no,
                        active=excluded.active
                    """,
                    (zone_id, data["name"], int(data.get("sort_no", 0))),
                )
                self.conn.commit()
                return self._get("zones", zone_id)
            except Exception:
                self.conn.rollback()
                raise

    def create_port(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                port_id = data.get("id") or new_id("PORT")
                self._get("zones", data["from_zone_id"])
                self._get("zones", data["to_zone_id"])
                if data["from_zone_id"] == data["to_zone_id"]:
                    raise ApiError(422, "same_zone", "口岸必须连接两个不同区域")
                self.conn.execute(
                    """
                    INSERT INTO ports(
                        id, name, from_zone_id, to_zone_id,
                        bidirectional, active
                    ) VALUES (?, ?, ?, ?, ?, 1)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        from_zone_id=excluded.from_zone_id,
                        to_zone_id=excluded.to_zone_id,
                        bidirectional=excluded.bidirectional,
                        active=excluded.active
                    """,
                    (
                        port_id,
                        data["name"],
                        data["from_zone_id"],
                        data["to_zone_id"],
                        1 if data.get("bidirectional", True) else 0,
                    ),
                )
                self.conn.commit()
                return self._get("ports", port_id)
            except Exception:
                self.conn.rollback()
                raise

    def add_visibility(self, data: Dict[str, Any]) -> Dict[str, Any]:
        a, b = sorted([data["zone_a_id"], data["zone_b_id"]])
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self._get("zones", a)
                self._get("zones", b)
                self.conn.execute(
                    "INSERT OR REPLACE INTO visibility_edges(zone_a, zone_b) VALUES (?, ?)",
                    (a, b),
                )
                self.conn.commit()
                return {"zone_a": a, "zone_b": b, "visible": True}
            except Exception:
                self.conn.rollback()
                raise

    def can_see(self, zone_a: Optional[str], zone_b: Optional[str]) -> bool:
        if not zone_a or not zone_b:
            return False
        if zone_a == zone_b:
            return True
        a, b = sorted([zone_a, zone_b])
        row = self.conn.execute(
            "SELECT 1 FROM visibility_edges WHERE zone_a=? AND zone_b=?",
            (a, b),
        ).fetchone()
        return row is not None

    def get_venue(self) -> Dict[str, Any]:
        zones = [self._row(r) for r in self.conn.execute("SELECT * FROM zones ORDER BY sort_no, id")]
        ports = [self._row(r) for r in self.conn.execute("SELECT * FROM ports WHERE active=1 ORDER BY id")]
        vis = [self._row(r) for r in self.conn.execute("SELECT * FROM visibility_edges")]
        visibility = [
            {"zone_a_id": r["zone_a"], "zone_b_id": r["zone_b"]} for r in vis
        ]
        return {"zones": zones, "ports": ports, "visibility_edges": visibility}

    # ---------- families / persons / sessions ----------

    def create_family(self, data: Dict[str, Any]) -> Dict[str, Any]:
        family_id = data.get("id") or new_id("FAM")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT INTO families(id, name, note) VALUES (?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, note=excluded.note",
                    (family_id, data["name"], data.get("note", "")),
                )
                self.conn.commit()
                return self._get("families", family_id)
            except Exception:
                self.conn.rollback()
                raise

    def add_consent(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self._get("families", data["family_id"])
                self._get("families", data["other_family_id"])
                self.conn.execute(
                    """
                    INSERT INTO family_consents(
                        family_id, other_family_id,
                        takeover_allowed, borrow_allowed
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(family_id, other_family_id) DO UPDATE SET
                        takeover_allowed=excluded.takeover_allowed,
                        borrow_allowed=excluded.borrow_allowed
                    """,
                    (
                        data["family_id"],
                        data["other_family_id"],
                        1 if data.get("takeover_allowed", True) else 0,
                        1 if data.get("borrow_allowed", True) else 0,
                    ),
                )
                self.conn.commit()
                return {
                    "family_id": data["family_id"],
                    "other_family_id": data["other_family_id"],
                    "takeover_allowed": bool(data.get("takeover_allowed", True)),
                    "borrow_allowed": bool(data.get("borrow_allowed", True)),
                }
            except Exception:
                self.conn.rollback()
                raise

    def _consent_allows(
        self, family_a: str, family_b: str, column: str = "takeover_allowed"
    ) -> bool:
        if family_a == family_b:
            return True
        if column not in {"takeover_allowed", "borrow_allowed"}:
            raise ValueError("invalid consent column")
        row = self.conn.execute(
            f"""
            SELECT 1 FROM family_consents
            WHERE {column}=1 AND (
                (family_id=? AND other_family_id=?)
                OR (family_id=? AND other_family_id=?)
            )
            LIMIT 1
            """,
            (family_a, family_b, family_b, family_a),
        ).fetchone()
        return row is not None

    def create_person(self, data: Dict[str, Any]) -> Dict[str, Any]:
        person_id = data.get("id") or new_id("P")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self._get("families", data["family_id"])
                if data["role"] not in {"adult", "child"}:
                    raise ApiError(422, "invalid_role", "腕带角色必须是 adult 或 child")
                capacity = int(data.get("care_capacity", 1 if data["role"] == "adult" else 0))
                self.conn.execute(
                    """
                    INSERT INTO persons(
                        id, band_code, display_name, role, family_id,
                        care_capacity, active
                    ) VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(id) DO UPDATE SET
                        band_code=excluded.band_code,
                        display_name=excluded.display_name,
                        role=excluded.role,
                        family_id=excluded.family_id,
                        care_capacity=excluded.care_capacity,
                        active=excluded.active
                    """,
                    (
                        person_id,
                        data.get("band_code", person_id),
                        data["display_name"],
                        data["role"],
                        data["family_id"],
                        capacity,
                    ),
                )
                self.conn.commit()
                return self._get("persons", person_id)
            except Exception:
                self.conn.rollback()
                raise

    def open_session(self, data: Dict[str, Any]) -> Dict[str, Any]:
        session_id = data.get("id") or new_id("SESS")
        at = utc_now(data.get("opened_at"))
        mode = data.get("operating_mode", "hybrid")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    """
                    INSERT INTO sessions(
                        id, name, operating_mode, status,
                        starts_at, ends_at, opened_at
                    ) VALUES (?, ?, ?, 'open', ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        operating_mode=excluded.operating_mode,
                        starts_at=excluded.starts_at,
                        ends_at=excluded.ends_at
                    """,
                    (
                        session_id,
                        data["name"],
                        mode,
                        data.get("starts_at"),
                        data.get("ends_at"),
                        at,
                    ),
                )
                self._ledger(
                    "session.opened",
                    f"开场：{data['name']}（{mode}）",
                    session_id=session_id,
                    ref_type="session",
                    ref_id=session_id,
                    payload={"operating_mode": mode},
                    at=at,
                )
                self.conn.commit()
                return self._get("sessions", session_id)
            except Exception:
                self.conn.rollback()
                raise

    def create_aid_group(self, data: Dict[str, Any]) -> Dict[str, Any]:
        group_id = data.get("id") or new_id("GROUP")
        at = utc_now(data.get("at"))
        family_ids = list(data.get("family_ids", []))
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self._get("sessions", data["session_id"])
                self.conn.execute(
                    "INSERT INTO aid_groups(id, session_id, name, created_at) VALUES (?, ?, ?, ?)",
                    (group_id, data["session_id"], data["name"], at),
                )
                for fam_id in family_ids:
                    self._get("families", fam_id)
                    self.conn.execute(
                        "INSERT OR REPLACE INTO aid_group_families(group_id, family_id) VALUES (?, ?)",
                        (group_id, fam_id),
                    )
                self._ledger(
                    "aid_group.formed",
                    f"组团：{data['name']}",
                    session_id=data["session_id"],
                    ref_type="aid_group",
                    ref_id=group_id,
                    payload={"family_ids": family_ids},
                    at=at,
                )
                self.conn.commit()
                return self.get_aid_group(group_id)
            except Exception:
                self.conn.rollback()
                raise

    def get_aid_group(self, group_id: str) -> Dict[str, Any]:
        group = self._get("aid_groups", group_id)
        rows = self.conn.execute(
            "SELECT family_id FROM aid_group_families WHERE group_id=?",
            (group_id,),
        ).fetchall()
        group["family_ids"] = [r["family_id"] for r in rows]
        return group

    def create_plan_segment(self, data: Dict[str, Any]) -> Dict[str, Any]:
        segment_id = data.get("id") or new_id("SEG")
        at = utc_now(data.get("at"))
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                session = self._get("sessions", data["session_id"])
                child = self._get("persons", data["child_id"])
                primary = self._get("persons", data["primary_adult_id"])
                backup = self._get("persons", data["backup_adult_id"])
                if child["role"] != "child":
                    raise ApiError(422, "not_a_child", "计划中的孩子腕带角色不正确")
                if primary["role"] != "adult" or backup["role"] != "adult":
                    raise ApiError(422, "not_an_adult", "主陪/备陪必须是成人")
                self._get("zones", data["zone_id"])
                self.conn.execute(
                    """
                    INSERT INTO plan_segments(
                        id, session_id, family_id, child_id, activity,
                        zone_id, starts_at, ends_at,
                        primary_adult_id, backup_adult_id,
                        window_starts_at, window_ends_at,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?)
                    """,
                    (
                        segment_id,
                        data["session_id"],
                        data.get("family_id", child["family_id"]),
                        data["child_id"],
                        data["activity"],
                        data["zone_id"],
                        data["starts_at"],
                        data["ends_at"],
                        data["primary_adult_id"],
                        data["backup_adult_id"],
                        data.get("window_starts_at", data["starts_at"]),
                        data.get("window_ends_at", data["ends_at"]),
                        at,
                    ),
                )
                self._ledger(
                    "plan.segment_published",
                    f"预编段落已发给家庭：{data['activity']}",
                    session_id=data["session_id"],
                    ref_type="plan_segment",
                    ref_id=segment_id,
                    payload={
                        "child_id": data["child_id"],
                        "primary_adult_id": data["primary_adult_id"],
                        "backup_adult_id": data["backup_adult_id"],
                    },
                    at=at,
                )
                self.conn.commit()
                return self._get("plan_segments", segment_id)
            except Exception:
                self.conn.rollback()
                raise

    # ---------- receipts / actual movement ----------

    def location_of(self, wristband_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        person = self._person(wristband_id)
        wristband_id = person["id"]
        if session_id:
            row = self.conn.execute(
                """
                SELECT * FROM receipts
                WHERE wristband_id=? AND session_id=?
                ORDER BY seq DESC LIMIT 1
                """,
                (wristband_id, session_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM receipts WHERE wristband_id=? ORDER BY seq DESC LIMIT 1",
                (wristband_id,),
            ).fetchone()
        if row is None:
            return {
                "wristband_id": wristband_id,
                "display_name": person["display_name"],
                "role": person["role"],
                "family_id": person["family_id"],
                "location_kind": "no_receipt",
                "present_in_water": False,
                "zone_id": None,
                "zone_name": None,
                "receipt_id": None,
                "since": None,
            }
        receipt = dict(row)
        zone_name = None
        if receipt["zone_id"]:
            z = self.conn.execute(
                "SELECT name FROM zones WHERE id=?", (receipt["zone_id"],)
            ).fetchone()
            zone_name = z["name"] if z else None
        return {
            "wristband_id": wristband_id,
            "display_name": person["display_name"],
            "role": person["role"],
            "family_id": person["family_id"],
            "location_kind": receipt["location_kind"],
            "present_in_water": receipt["location_kind"] == "inside",
            "zone_id": receipt["zone_id"],
            "zone_name": zone_name,
            "receipt_id": receipt["id"],
            "since": receipt["created_at"],
            "source": receipt["source"],
        }

    def _insert_receipt_and_event(
        self,
        *,
        wristband_id: str,
        session_id: Optional[str],
        location_kind: str,
        zone_id: Optional[str],
        event_kind: str,
        at: str,
        source: str,
        staff_id: Optional[str] = None,
        port_id: Optional[str] = None,
        from_zone_id: Optional[str] = None,
        permit_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        note: str = "",
        corrects_receipt_id: Optional[str] = None,
        commit: bool = False,
    ) -> Dict[str, Any]:
        event_id = new_id("EVT")
        receipt_id = new_id("RCPT")
        self.conn.execute(
            """
            INSERT INTO crossing_events(
                id, session_id, at, wristband_id, kind, port_id,
                from_zone_id, to_zone_id, receipt_id, permit_id,
                staff_id, idempotency_key, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                at,
                wristband_id,
                event_kind,
                port_id,
                from_zone_id,
                zone_id if location_kind == "inside" else None,
                receipt_id,
                permit_id,
                staff_id,
                idempotency_key,
                json.dumps({"note": note}, ensure_ascii=False),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO receipts(
                id, wristband_id, session_id, location_kind, zone_id,
                source, event_id, corrects_receipt_id, staff_id, note,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                wristband_id,
                session_id,
                location_kind,
                zone_id if location_kind == "inside" else None,
                source,
                event_id,
                corrects_receipt_id,
                staff_id,
                note,
                at,
            ),
        )
        self.conn.execute(
            "UPDATE crossing_events SET receipt_id=? WHERE id=?",
            (receipt_id, event_id),
        )
        if commit:
            self.conn.commit()
        return {"event_id": event_id, "receipt_id": receipt_id}

    def gate_entry(self, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        key = data.get("idempotency_key")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if key:
                    old = self.conn.execute(
                        "SELECT * FROM crossing_events WHERE idempotency_key=?",
                        (key,),
                    ).fetchone()
                    if old:
                        self.conn.commit()
                        result = self.location_of(data["wristband_id"])
                        result["idempotent_replay"] = True
                        result["event_id"] = old["id"]
                        return result

                band = self._person(data["wristband_id"])
                zone = self._get("zones", data["zone_id"])
                latest = self.conn.execute(
                    "SELECT * FROM receipts WHERE wristband_id=? ORDER BY seq DESC LIMIT 1",
                    (band["id"],),
                ).fetchone()
                if latest and latest["location_kind"] == "inside" and not data.get("allow_duplicate_scan"):
                    raise ApiError(
                        409,
                        "already_in_water",
                        f"{band['display_name']} 的最新回条已在 {zone['name']}，不能重复入区",
                        self.location_of(band["id"]),
                    )
                refs = self._insert_receipt_and_event(
                    wristband_id=band["id"],
                    session_id=data.get("session_id"),
                    location_kind="inside",
                    zone_id=zone["id"],
                    event_kind="gate_entry",
                    at=at,
                    source="gate_scan",
                    staff_id=data.get("staff_id"),
                    idempotency_key=key,
                    note=data.get("note", "闸机入区"),
                )
                self._ledger(
                    "gate.entry_confirmed",
                    f"工作人员确认 {band['display_name']} 实际入区至 {zone['name']}",
                    session_id=data.get("session_id"),
                    ref_type="wristband",
                    ref_id=band["id"],
                    payload={"zone_id": zone["id"], **refs},
                    at=at,
                )
                self.conn.commit()
                result = self.location_of(band["id"])
                result.update(refs)
                return result
            except Exception:
                self.conn.rollback()
                raise

    def cross_port(self, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        key = data.get("idempotency_key")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if key:
                    old = self.conn.execute(
                        "SELECT * FROM crossing_events WHERE idempotency_key=?",
                        (key,),
                    ).fetchone()
                    if old:
                        self.conn.commit()
                        return {
                            "idempotent_replay": True,
                            "event": self._row(old),
                            "location": self.location_of(data["wristband_id"]),
                        }
                band = self._person(data["wristband_id"])
                port = self._get("ports", data["port_id"])
                loc = self.location_of(band["id"])
                if not loc["present_in_water"]:
                    raise ApiError(
                        409,
                        "not_in_water",
                        "腕带没有有效的水区内回条，不能确认过口",
                        loc,
                    )
                current = loc["zone_id"]
                if current == port["from_zone_id"]:
                    dest = port["to_zone_id"]
                elif port["bidirectional"] and current == port["to_zone_id"]:
                    dest = port["from_zone_id"]
                else:
                    raise ApiError(
                        409,
                        "no_marked_port",
                        f"当前区域与标明口岸 {port['name']} 不相连，不能跨过",
                        {"current_zone_id": current, "port_id": port["id"]},
                    )
                refs = self._insert_receipt_and_event(
                    wristband_id=band["id"],
                    session_id=data.get("session_id"),
                    location_kind="inside",
                    zone_id=dest,
                    event_kind="port_cross",
                    at=at,
                    source="port_scan",
                    staff_id=data.get("staff_id"),
                    port_id=port["id"],
                    from_zone_id=current,
                    idempotency_key=key,
                    note=data.get("note", "逐人确认实际过口"),
                )
                dest_zone = self._get("zones", dest)
                self._ledger(
                    "port.cross_confirmed",
                    f"{band['display_name']} 经 {port['name']} 实际到达 {dest_zone['name']}",
                    session_id=data.get("session_id"),
                    ref_type="port",
                    ref_id=port["id"],
                    payload={"from_zone_id": current, "to_zone_id": dest, **refs},
                    at=at,
                )
                self.conn.commit()
                return {
                    **refs,
                    "location": self.location_of(band["id"]),
                    "visible_with_previous_zone": self.can_see(current, dest),
                }
            except Exception:
                self.conn.rollback()
                raise

    def correct_receipt(self, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        band_id = data["wristband_id"]
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                band = self._person(band_id)
                band_id = band["id"]
                latest = self.conn.execute(
                    "SELECT * FROM receipts WHERE wristband_id=? ORDER BY seq DESC LIMIT 1",
                    (band_id,),
                ).fetchone()
                corrects = latest["id"] if latest else None
                kind = "outside" if data.get("location_kind") == "outside" else "inside"
                zone_id = None
                if kind == "inside":
                    zone_id = data["zone_id"]
                    self._get("zones", zone_id)
                if not data.get("reason"):
                    raise ApiError(422, "reason_required", "更正实际回条必须填写原因")
                refs = self._insert_receipt_and_event(
                    wristband_id=band_id,
                    session_id=data.get("session_id"),
                    location_kind=kind,
                    zone_id=zone_id,
                    event_kind="receipt_correction",
                    at=at,
                    source="correction",
                    staff_id=data.get("staff_id"),
                    from_zone_id=latest["zone_id"] if latest else None,
                    idempotency_key=data.get("idempotency_key"),
                    note=data["reason"],
                    corrects_receipt_id=corrects,
                )
                self._ledger(
                    "receipt.corrected",
                    f"凭实际情况更正 {band['display_name']} 的入区回条",
                    session_id=data.get("session_id"),
                    ref_type="wristband",
                    ref_id=band_id,
                    payload={"reason": data["reason"], "corrects": corrects, **refs},
                    at=at,
                )
                self.conn.commit()
                return self.location_of(band_id)
            except Exception:
                self.conn.rollback()
                raise

    # ---------- direct care assignment ----------

    def assign_care(self, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        session_id = data["session_id"]
        child_id = data["child_id"]
        adult_id = data["adult_id"]
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self._get("sessions", session_id)
                child = self._get("persons", child_id)
                adult = self._get("persons", adult_id)
                if child["role"] != "child" or adult["role"] != "adult":
                    raise ApiError(422, "invalid_person_role", "陪同关系必须是成人陪儿童")
                adult_loc = self.location_of(adult_id, session_id)
                child_loc = self.location_of(child_id, session_id)
                if data.get("validate_presence", True):
                    if not adult_loc["present_in_water"]:
                        raise ApiError(409, "adult_not_in_water", "成人没有水区内实际回条", adult_loc)
                    if not child_loc["present_in_water"]:
                        raise ApiError(409, "child_not_in_water", "儿童没有水区内实际回条", child_loc)
                    if not self.can_see(adult_loc["zone_id"], child_loc["zone_id"]):
                        raise ApiError(
                            409,
                            "not_same_or_visible",
                            "成人与儿童不在同区或可看见区域",
                            {"adult": adult_loc, "child": child_loc},
                        )
                alloc = self._reserve_slot(
                    session_id=session_id,
                    adult_id=adult_id,
                    child_id=child_id,
                    purpose=data.get("purpose", "own_care"),
                    ref_type="care_assignment",
                    ref_id=data.get("id", ""),
                    at=at,
                    consume=True,
                )
                assignment_id = data.get("id") or new_id("CARE")
                self.conn.execute(
                    """
                    INSERT INTO care_assignments(
                        id, session_id, child_id, adult_id, kind, source,
                        segment_id, slot_allocation_id, status,
                        starts_at, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        assignment_id,
                        session_id,
                        child_id,
                        adult_id,
                        data.get("kind", "primary"),
                        data.get("source", "direct"),
                        data.get("segment_id"),
                        alloc["id"],
                        at,
                        data.get("note", ""),
                    ),
                )
                self._ledger(
                    "care.assigned",
                    f"{adult['display_name']} 实际陪同 {child['display_name']}",
                    session_id=session_id,
                    ref_type="care_assignment",
                    ref_id=assignment_id,
                    payload={"slot_allocation_id": alloc["id"], "slot_no": alloc["slot_no"]},
                    at=at,
                )
                self.conn.commit()
                return self._get("care_assignments", assignment_id)
            except Exception:
                self.conn.rollback()
                raise

    # ---------- handovers ----------

    def _validate_plan_window(self, ho_data: Dict[str, Any], at: str) -> Dict[str, Any]:
        segment_id = ho_data.get("segment_id")
        if not segment_id:
            raise ApiError(422, "segment_required", "预编制必须引用已发给家庭的段落")
        seg = self._get("plan_segments", segment_id)
        if ho_data.get("session_id") != seg["session_id"]:
            raise ApiError(422, "session_mismatch", "段落不属于当前场次")
        if ho_data["departing_adult_id"] != seg["primary_adult_id"]:
            raise ApiError(409, "not_primary_for_segment", "申请人不是该段落登记的主陪")
        if ho_data["receiving_adult_id"] != seg["backup_adult_id"]:
            raise ApiError(409, "not_backup_for_segment", "接替人不是该段落已发家庭的备陪")
        if not (seg["window_starts_at"] <= at <= seg["window_ends_at"]):
            raise ApiError(
                409,
                "outside_cross_zone_window",
                "当前时间不在预编可用跨区/接替时段内",
                {"at": at, "window": [seg["window_starts_at"], seg["window_ends_at"]]},
            )
        return seg

    def create_handover(self, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        key = data.get("idempotency_key")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._idempotent("handovers", key)
                if existing:
                    self.conn.commit()
                    return self.get_handover(existing["id"])
                session = self._get("sessions", data["session_id"])
                source = data.get("source")
                if not source:
                    source = "plan" if session["operating_mode"] == "prearranged" else "onsite"
                allowed_sources = {
                    "prearranged": {"plan"},
                    "onsite": {"onsite"},
                    "hybrid": {"plan", "onsite"},
                }[session["operating_mode"]]
                if source not in allowed_sources:
                    raise ApiError(
                        409,
                        "session_mode_conflict",
                        f"本场所选场制为 {session['operating_mode']}，不能使用 {source}",
                    )
                departing = self._get("persons", data["departing_adult_id"])
                receiver = self._get("persons", data["receiving_adult_id"])
                if departing["role"] != "adult" or receiver["role"] != "adult":
                    raise ApiError(422, "not_an_adult", "离开人和接替人都必须是成人")
                for child_id in data.get("child_ids", []):
                    child = self._get("persons", child_id)
                    if child["role"] != "child":
                        raise ApiError(422, "not_a_child", "交接名单包含非儿童腕带")
                ho_id = data.get("id") or new_id("HO")
                self.conn.execute(
                    """
                    INSERT INTO handovers(
                        id, session_id, aid_group_id, departing_adult_id,
                        receiving_adult_id, source, status, segment_id,
                        idempotency_key, created_at, note
                    ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?)
                    """,
                    (
                        ho_id,
                        data["session_id"],
                        data.get("aid_group_id"),
                        data["departing_adult_id"],
                        data["receiving_adult_id"],
                        source,
                        data.get("segment_id"),
                        key,
                        at,
                        data.get("note", ""),
                    ),
                )
                for child_id in data.get("child_ids", []):
                    self.conn.execute(
                        "INSERT OR REPLACE INTO handover_children(handover_id, child_id, slot_allocation_id) "
                        "VALUES (?, ?, NULL)",
                        (ho_id, child_id),
                    )
                self._ledger(
                    "handover.proposed",
                    f"发起交接：{departing['display_name']} -> {receiver['display_name']}",
                    session_id=data["session_id"],
                    ref_type="handover",
                    ref_id=ho_id,
                    payload={"child_ids": data.get("child_ids", []), "source": source},
                    at=at,
                )
                self.conn.commit()
                return self.get_handover(ho_id)
            except Exception:
                self.conn.rollback()
                raise

    def confirm_handover(self, handover_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                ho = self._get("handovers", handover_id)
                if ho["status"] in {"confirmed", "prepared", "effective"}:
                    self.conn.commit()
                    return self.get_handover(handover_id)
                if ho["status"] != "proposed":
                    raise ApiError(409, "handover_not_confirmable", "交接单当前状态不能确认", {"status": ho["status"]})
                confirmed_by = data.get("confirmed_by")
                if confirmed_by and confirmed_by != ho["receiving_adult_id"]:
                    raise ApiError(403, "wrong_receiver_band", "只有指定接替成人腕带可以确认")
                receiver_loc = self.location_of(ho["receiving_adult_id"], ho["session_id"])
                if not receiver_loc["present_in_water"]:
                    raise ApiError(
                        409,
                        "receiver_not_in_water",
                        "接替成人没有实际入区回条；已售票不能证明人在水区",
                        receiver_loc,
                    )
                # Child list may not yet exist if created only via prepare payload;
                # low-level create expects child_ids and stores nothing until prepare.
                child_rows = self.conn.execute(
                    "SELECT child_id FROM handover_children WHERE handover_id=?",
                    (handover_id,),
                ).fetchall()
                for row in child_rows:
                    child_loc = self.location_of(row["child_id"], ho["session_id"])
                    if not child_loc["present_in_water"] or not self.can_see(
                        receiver_loc["zone_id"], child_loc["zone_id"]
                    ):
                        raise ApiError(409, "receiver_not_visible", "接替人与儿童不在同区或可见区域")
                self.conn.execute(
                    "UPDATE handovers SET status='confirmed', receiver_confirmed_at=? WHERE id=?",
                    (at, handover_id),
                )
                self._ledger(
                    "handover.confirmed",
                    "仍在场的指定成人已确认接下孩子",
                    session_id=ho["session_id"],
                    ref_type="handover",
                    ref_id=handover_id,
                    payload={"confirmed_by": confirmed_by or ho["receiving_adult_id"]},
                    at=at,
                )
                self.conn.commit()
                return self.get_handover(handover_id)
            except Exception:
                self.conn.rollback()
                raise

    def _materialize_children_and_slots(
        self, ho: Dict[str, Any], child_ids: List[str], at: str
    ) -> List[Dict[str, Any]]:
        result = []
        for child_id in child_ids:
            child = self._get("persons", child_id)
            if child["role"] != "child":
                raise ApiError(422, "not_a_child", "交接名单包含非儿童腕带")
            if not self._consent_allows(child["family_id"], ho["receiving_adult_family_id"], "takeover_allowed"):
                raise ApiError(
                    403,
                    "family_takeover_not_allowed",
                    "这两个家庭未登记允许互相接手",
                    {"child_family_id": child["family_id"], "receiver_family_id": ho["receiving_adult_family_id"]},
                )
            alloc = self._reserve_slot(
                session_id=ho["session_id"],
                adult_id=ho["receiving_adult_id"],
                child_id=child_id,
                purpose="handover" if child["family_id"] == ho["receiving_adult_family_id"] else "handover",
                ref_type="handover",
                ref_id=ho["id"],
                at=at,
            )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO handover_children(
                    handover_id, child_id, slot_allocation_id
                ) VALUES (?, ?, ?)
                """,
                (ho["id"], child_id, alloc["id"]),
            )
            result.append({"child_id": child_id, "slot_allocation_id": alloc["id"], "slot_no": alloc["slot_no"]})
        return result

    def prepare_handover(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create/confirm/allocate a handover in one atomic counter operation."""
        at = utc_now(data.get("at"))
        key = data.get("idempotency_key")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._idempotent("handovers", key)
                if existing:
                    self.conn.commit()
                    return self.get_handover(existing["id"])

                session = self._get("sessions", data["session_id"])
                source = data.get("source")
                if not source:
                    source = "plan" if session["operating_mode"] == "prearranged" else "onsite"
                allowed_sources = {
                    "prearranged": {"plan"},
                    "onsite": {"onsite"},
                    "hybrid": {"plan", "onsite"},
                }[session["operating_mode"]]
                if source not in allowed_sources:
                    raise ApiError(409, "session_mode_conflict", "本场所选场制与交接来源不一致")

                ho_id = data.get("id") or new_id("HO")
                departing = self._get("persons", data["departing_adult_id"])
                receiver = self._get("persons", data["receiving_adult_id"])
                child_ids = list(data.get("child_ids", []))
                if not child_ids:
                    raise ApiError(422, "children_required", "交接必须指明孩子")
                if departing["role"] != "adult" or receiver["role"] != "adult":
                    raise ApiError(422, "not_an_adult", "离开人和接替人都必须是成人")

                ho_data = {**data, "departing_adult_id": departing["id"], "receiving_adult_id": receiver["id"]}
                seg = None
                if source == "plan":
                    seg = self._validate_plan_window(ho_data, at)
                elif source == "onsite" and not data.get("receiver_confirmed", False):
                    raise ApiError(
                        409,
                        "receiver_confirmation_required",
                        "现场互助团必须先由仍在场的指定成人确认接下哪些孩子",
                    )

                # Physical presence and visibility are checked before reserving.
                dep_loc = self.location_of(departing["id"], session["id"])
                recv_loc = self.location_of(receiver["id"], session["id"])
                if not dep_loc["present_in_water"]:
                    raise ApiError(409, "departing_adult_not_in_water", "离开成人没有水区内实际回条", dep_loc)
                if not recv_loc["present_in_water"]:
                    raise ApiError(
                        409,
                        "receiver_not_in_water",
                        "接替成人还在闸机外/无入区回条；已售票不能假定其已在水区",
                        recv_loc,
                    )
                child_locs = []
                for child_id in child_ids:
                    child = self._get("persons", child_id)
                    loc = self.location_of(child_id, session["id"])
                    if not loc["present_in_water"]:
                        raise ApiError(409, "child_not_in_water", f"{child['display_name']} 没有水区内回条", loc)
                    if not self.can_see(recv_loc["zone_id"], loc["zone_id"]):
                        raise ApiError(
                            409,
                            "receiver_not_visible",
                            "接替人与孩子不在同区或彼此可看见的区域",
                            {"receiver": recv_loc, "child": loc},
                        )
                    child_locs.append(loc)

                confirmed_at = at if data.get("receiver_confirmed", source == "plan") else None
                self.conn.execute(
                    """
                    INSERT INTO handovers(
                        id, session_id, aid_group_id, departing_adult_id,
                        receiving_adult_id, source, status, segment_id,
                        receiver_confirmed_at, prepared_at,
                        idempotency_key, created_at, note
                    ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ho_id,
                        session["id"],
                        data.get("aid_group_id"),
                        departing["id"],
                        receiver["id"],
                        source,
                        (seg or {}).get("id") if seg else data.get("segment_id"),
                        confirmed_at,
                        at,
                        key,
                        at,
                        data.get("note", ""),
                    ),
                )
                ho = self._get("handovers", ho_id)
                ho["receiving_adult_family_id"] = receiver["family_id"]
                slots = self._materialize_children_and_slots(ho, child_ids, at)

                if source == "onsite":
                    self._ledger(
                        "handover.confirmed",
                        f"{receiver['display_name']} 已确认接下 {len(child_ids)} 名孩子",
                        session_id=session["id"],
                        ref_type="handover",
                        ref_id=ho_id,
                        payload={"confirmed_by": receiver["id"]},
                        at=at,
                    )
                self._ledger(
                    "handover.prepared",
                    f"前台已把有限陪同名额留给本交接：{departing['display_name']} -> {receiver['display_name']}",
                    session_id=session["id"],
                    ref_type="handover",
                    ref_id=ho_id,
                    payload={"children": slots, "source": source},
                    at=at,
                )
                for slot in slots:
                    self._ledger(
                        "slot.reserved",
                        f"陪同名额 {receiver['display_name']}#{slot['slot_no']} 已预备入账",
                        session_id=session["id"],
                        ref_type="slot_allocation",
                        ref_id=slot["slot_allocation_id"],
                        payload=slot,
                        at=at,
                    )
                self.conn.commit()
                return self.get_handover(ho_id)
            except Exception:
                self.conn.rollback()
                raise

    def prepare_existing_handover(self, handover_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Front desk allocates finite slots after proposal/confirmation."""
        at = utc_now(data.get("at"))
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                ho = self._get("handovers", handover_id)
                if ho["status"] in {"prepared", "effective"}:
                    self.conn.commit()
                    return self.get_handover(handover_id)
                if ho["source"] == "onsite" and ho["status"] != "confirmed":
                    raise ApiError(409, "receiver_confirmation_required", "现场互助交接尚未经成人确认")
                if ho["source"] == "plan":
                    self._validate_plan_window(ho, at)

                child_ids = data.get("child_ids") or [
                    r["child_id"]
                    for r in self.conn.execute(
                        "SELECT child_id FROM handover_children WHERE handover_id=?",
                        (handover_id,),
                    )
                ]
                if not child_ids:
                    raise ApiError(422, "children_required", "交接必须指明孩子")

                receiver = self._get("persons", ho["receiving_adult_id"])
                departing_loc = self.location_of(ho["departing_adult_id"], ho["session_id"])
                receiver_loc = self.location_of(receiver["id"], ho["session_id"])
                if not departing_loc["present_in_water"]:
                    raise ApiError(409, "departing_adult_not_in_water", "离开成人没有水区内回条", departing_loc)
                if not receiver_loc["present_in_water"]:
                    raise ApiError(409, "receiver_not_in_water", "接替成人没有实际入区回条", receiver_loc)

                ho_with_family = {**ho, "receiving_adult_family_id": receiver["family_id"]}
                slots = []
                for child_id in child_ids:
                    child_loc = self.location_of(child_id, ho["session_id"])
                    if not child_loc["present_in_water"]:
                        raise ApiError(409, "child_not_in_water", "儿童没有水区内回条", child_loc)
                    if not self.can_see(receiver_loc["zone_id"], child_loc["zone_id"]):
                        raise ApiError(409, "receiver_not_visible", "接替人与儿童不在同区或可见区域")
                    slots.extend(self._materialize_children_and_slots(ho_with_family, [child_id], at))

                self.conn.execute(
                    "UPDATE handovers SET status='prepared', prepared_at=? WHERE id=?",
                    (at, handover_id),
                )
                self._ledger(
                    "handover.prepared",
                    "前台已为已确认交接预留有限陪同名额",
                    session_id=ho["session_id"],
                    ref_type="handover",
                    ref_id=handover_id,
                    payload={"children": slots},
                    at=at,
                )
                for slot in slots:
                    self._ledger(
                        "slot.reserved",
                        f"陪同名额 {receiver['display_name']}#{slot['slot_no']} 已预备入账",
                        session_id=ho["session_id"],
                        ref_type="slot_allocation",
                        ref_id=slot["slot_allocation_id"],
                        payload=slot,
                        at=at,
                    )
                self.conn.commit()
                return self.get_handover(handover_id)
            except Exception:
                self.conn.rollback()
                raise

    def get_handover(self, handover_id: str) -> Dict[str, Any]:
        ho = self._get("handovers", handover_id)
        rows = self.conn.execute(
            """
            SELECT hc.child_id, hc.slot_allocation_id, sa.slot_no, sa.status AS slot_status
            FROM handover_children hc
            LEFT JOIN slot_allocations sa ON sa.id = hc.slot_allocation_id
            WHERE hc.handover_id=?
            """,
            (handover_id,),
        ).fetchall()
        ho["children"] = [dict(r) for r in rows]
        dep = self.conn.execute("SELECT display_name FROM persons WHERE id=?", (ho["departing_adult_id"],)).fetchone()
        recv = self.conn.execute("SELECT display_name, care_capacity FROM persons WHERE id=?", (ho["receiving_adult_id"],)).fetchone()
        ho["departing_adult_name"] = dep["display_name"] if dep else None
        ho["receiving_adult_name"] = recv["display_name"] if recv else None
        ho["receiver_capacity"] = recv["care_capacity"] if recv else None
        ho["receiver_used_slots"] = self._active_slot_numbers(
            ho["receiving_adult_id"], ho["session_id"]
        )
        return ho

    def evaluate_handover(self, handover_id: str) -> Dict[str, Any]:
        ho = self._get("handovers", handover_id)
        reasons: List[Dict[str, Any]] = []
        receiver_loc = self.location_of(ho["receiving_adult_id"], ho["session_id"])
        if not receiver_loc["present_in_water"]:
            reasons.append({
                "code": "receiver_not_in_water",
                "message": "接替成人当前不在水区；已售票不能作为入区回条",
                "location": receiver_loc,
            })
        child_rows = self.conn.execute(
            "SELECT * FROM handover_children WHERE handover_id=?",
            (handover_id,),
        ).fetchall()
        children = []
        for row in child_rows:
            child_id = row["child_id"]
            loc = self.location_of(child_id)
            item: Dict[str, Any] = {"child_id": child_id, "location": loc}
            if not loc["present_in_water"]:
                reasons.append({"code": "child_not_in_water", "message": "孩子当前不在水区", "details": item})
            elif receiver_loc["present_in_water"] and not self.can_see(
                receiver_loc["zone_id"], loc["zone_id"]
            ):
                reasons.append({
                    "code": "receiver_not_visible",
                    "message": "接替人实际回条所在区与孩子所在区彼此看不见",
                    "details": {"receiver": receiver_loc, "child": loc},
                })
            alloc_id = row["slot_allocation_id"]
            alloc = self._row(self.conn.execute(
                "SELECT * FROM slot_allocations WHERE id=?", (alloc_id,)
            ).fetchone()) if alloc_id else None
            item["allocation"] = alloc
            if not alloc or alloc["status"] not in {"reserved", "consumed"}:
                reasons.append({"code": "slot_missing_or_released", "message": "交接名额不在有效占用状态", "details": item})
            elif alloc["adult_id"] != ho["receiving_adult_id"] or alloc["child_id"] != child_id:
                reasons.append({"code": "slot_mismatch", "message": "名额与交接双方不一致", "details": item})
            children.append(item)

        receiver = self._get("persons", ho["receiving_adult_id"])
        used = self._active_slot_numbers(receiver["id"], ho["session_id"])
        if len(used) > int(receiver["care_capacity"]):
            reasons.append({
                "code": "capacity_exceeded",
                "message": "接替成人的有效陪同人数超过容量",
                "details": {"capacity": receiver["care_capacity"], "used": used},
            })
        return {
            "ok": not reasons,
            "reasons": reasons,
            "handover_id": handover_id,
            "status": ho["status"],
            "receiver_location": receiver_loc,
            "children": children,
            "receiver_used_slots": used,
        }

    # ---------- permits / gate consumption ----------

    def _adult_active_child_ids(self, adult_id: str, session_id: str) -> List[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT child_id FROM care_assignments
            WHERE adult_id=? AND session_id=? AND status='active'
            """,
            (adult_id, session_id),
        ).fetchall()
        return [r["child_id"] for r in rows]

    def issue_permit(self, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        key = data.get("idempotency_key")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._idempotent("permits", key)
                if existing:
                    self.conn.commit()
                    return self.get_permit(existing["id"])
                session = self._get("sessions", data["session_id"])
                band = self._person(data["wristband_id"])
                loc = self.location_of(band["id"], session["id"])
                if not loc["present_in_water"] and not data.get("allow_absent_request"):
                    raise ApiError(409, "wristband_not_in_water", "当前腕带没有水区内回条，不能请求本次离区", loc)

                handover_id = data.get("handover_id")
                ho = None
                if handover_id:
                    ho = self._get("handovers", handover_id)
                    if ho["departing_adult_id"] != band["id"]:
                        raise ApiError(403, "not_departing_adult", "腕带不是该交接的离开人")
                    if ho["status"] not in {"prepared", "effective"}:
                        raise ApiError(409, "handover_not_prepared", "交接尚未完成预备入账", {"status": ho["status"]})
                    evaluation = self.evaluate_handover(handover_id)
                    if not evaluation["ok"]:
                        raise ApiError(409, "handover_not_ready", "接替安排当前不满足放行条件", evaluation)
                elif band["role"] == "adult":
                    active_children = self._adult_active_child_ids(band["id"], session["id"])
                    if active_children:
                        raise ApiError(
                            409,
                            "handover_required",
                            "该成人仍登记陪同孩子；独自离区前必须有有效接替交接",
                            {"active_child_ids": active_children},
                        )
                elif band["role"] == "child":
                    escort_id = data.get("escort_adult_id")
                    pickup_name = data.get("pickup_name", "")
                    if not escort_id and not pickup_name:
                        raise ApiError(
                            409,
                            "escort_or_pickup_required",
                            "儿童腕带请求离区必须登记同行成人或授权接领人",
                        )
                    if escort_id:
                        escort_loc = self.location_of(escort_id, session["id"])
                        if not escort_loc["present_in_water"] or not self.can_see(
                            escort_loc["zone_id"], loc["zone_id"]
                        ):
                            raise ApiError(409, "escort_not_available", "同行成人当前不在同区/可见区域", escort_loc)

                permit_id = data.get("id") or new_id("PMT")
                ptype = data.get("permit_type", "gate_exit")
                self.conn.execute(
                    """
                    INSERT INTO permits(
                        id, session_id, wristband_id, permit_type, status,
                        handover_id, escort_adult_id, pickup_name,
                        issued_at, idempotency_key
                    ) VALUES (?, ?, ?, ?, 'issued', ?, ?, ?, ?, ?)
                    """,
                    (
                        permit_id,
                        session["id"],
                        band["id"],
                        ptype,
                        handover_id,
                        data.get("escort_adult_id"),
                        data.get("pickup_name", ""),
                        at,
                        key,
                    ),
                )
                if handover_id:
                    self.conn.execute(
                        "UPDATE handovers SET permit_id=? WHERE id=?",
                        (permit_id, handover_id),
                    )
                self._ledger(
                    "permit.issued",
                    f"已签发 {band['display_name']} 的本次过闸许可 {permit_id}",
                    session_id=session["id"],
                    ref_type="permit",
                    ref_id=permit_id,
                    payload={"handover_id": handover_id, "type": ptype},
                    at=at,
                )
                self.conn.commit()
                return self.get_permit(permit_id)
            except Exception:
                self.conn.rollback()
                raise

    def get_permit(self, permit_id: str) -> Dict[str, Any]:
        permit = self._get("permits", permit_id)
        attempts = [
            self._row(r)
            for r in self.conn.execute(
                "SELECT * FROM permit_attempts WHERE permit_id=? ORDER BY id",
                (permit_id,),
            )
        ]
        for a in attempts:
            a["reasons"] = json.loads(a.pop("reasons_json"))
            a["snapshot"] = json.loads(a.pop("snapshot_json"))
        permit["attempts"] = attempts
        return permit

    def consume_permit(self, permit_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        key = data.get("idempotency_key")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                permit = self._get("permits", permit_id)
                if key:
                    old_event = self.conn.execute(
                        "SELECT * FROM crossing_events WHERE idempotency_key=?",
                        (key,),
                    ).fetchone()
                    if old_event:
                        self.conn.commit()
                        result = self.get_permit(permit_id)
                        result["idempotent_replay"] = True
                        result["crossing_event"] = self._row(old_event)
                        return result
                if permit["status"] == "consumed":
                    existing_event = None
                    if permit["crossing_event_id"]:
                        existing_event = self._row(self._get("crossing_events", permit["crossing_event_id"]))
                    self.conn.commit()
                    return {
                        **self.get_permit(permit_id),
                        "idempotent_replay": True,
                        "crossing_event": existing_event,
                    }
                if permit["status"] != "issued":
                    raise ApiError(409, "permit_not_consumable", "许可状态不允许过闸", {"status": permit["status"]})

                band = self._get("persons", permit["wristband_id"])
                current_loc = self.location_of(band["id"], permit["session_id"])
                if not current_loc["present_in_water"]:
                    evaluation = {"ok": False, "reasons": [{
                        "code": "wristband_not_in_water",
                        "message": "腕带当前没有水区内回条",
                        "details": current_loc,
                    }]}
                elif permit["handover_id"]:
                    evaluation = self.evaluate_handover(permit["handover_id"])
                else:
                    evaluation = {"ok": True, "reasons": []}

                if not current_loc["present_in_water"] or not evaluation["ok"]:
                    snapshot = {
                        "wristband_location": current_loc,
                        "handover_evaluation": evaluation,
                    }
                    self.conn.execute(
                        """
                        INSERT INTO permit_attempts(
                            permit_id, at, allowed, reasons_json, snapshot_json
                        ) VALUES (?, ?, 0, ?, ?)
                        """,
                        (
                            permit_id,
                            at,
                            json.dumps(evaluation.get("reasons", []), ensure_ascii=False),
                            json.dumps(snapshot, ensure_ascii=False),
                        ),
                    )
                    self._ledger(
                        "gate.denied",
                        f"工作人员不予兑现 {band['display_name']} 的过闸：接替/位置条件失效",
                        session_id=permit["session_id"],
                        ref_type="permit",
                        ref_id=permit_id,
                        payload={"reasons": evaluation.get("reasons", [])},
                        at=at,
                    )
                    self.conn.commit()
                    raise ApiError(
                        409,
                        "gate_not_released",
                        "本次过闸办不了；预备交接和名额保持入账",
                        {"permit_id": permit_id, "evaluation": evaluation, "location": current_loc},
                    )

                refs = self._insert_receipt_and_event(
                    wristband_id=band["id"],
                    session_id=permit["session_id"],
                    location_kind="outside",
                    zone_id=None,
                    event_kind="gate_exit",
                    at=at,
                    source="gate_scan",
                    staff_id=data.get("staff_id"),
                    from_zone_id=current_loc["zone_id"],
                    permit_id=permit_id,
                    idempotency_key=key,
                    note=data.get("note", "凭许可实际出闸"),
                )
                self.conn.execute(
                    "UPDATE permits SET status='consumed', consumed_at=?, crossing_event_id=? WHERE id=?",
                    (at, refs["event_id"], permit_id),
                )
                self.conn.execute(
                    """
                    INSERT INTO permit_attempts(
                        permit_id, at, allowed, reasons_json, snapshot_json
                    ) VALUES (?, ?, 1, '[]', ?)
                    """,
                    (permit_id, at, json.dumps({"location": current_loc}, ensure_ascii=False)),
                )
                self._ledger(
                    "gate.crossed",
                    f"{band['display_name']} 已真正过闸离开",
                    session_id=permit["session_id"],
                    ref_type="crossing_event",
                    ref_id=refs["event_id"],
                    payload={"permit_id": permit_id, **refs},
                    at=at,
                )

                effective_handover = None
                if permit["handover_id"]:
                    ho_id = permit["handover_id"]
                    ho = self._get("handovers", ho_id)
                    self.conn.execute(
                        "UPDATE handovers SET status='effective', effective_at=? WHERE id=?",
                        (at, ho_id),
                    )
                    hc_rows = self.conn.execute(
                        "SELECT child_id, slot_allocation_id FROM handover_children WHERE handover_id=?",
                        (ho_id,),
                    ).fetchall()
                    for hc in hc_rows:
                        if hc["slot_allocation_id"]:
                            self.conn.execute(
                                "UPDATE slot_allocations SET status='consumed', updated_at=? WHERE id=?",
                                (at, hc["slot_allocation_id"]),
                            )
                        # The departing adult's own slot is released after the
                        # physical exit; the receiver's consumed slot remains.
                        own = self.conn.execute(
                            """
                            SELECT * FROM slot_allocations
                            WHERE adult_id=? AND child_id=?
                              AND status IN ('reserved', 'consumed')
                              AND id != ?
                            ORDER BY created_at LIMIT 1
                            """,
                            (ho["departing_adult_id"], hc["child_id"], hc["slot_allocation_id"] or ""),
                        ).fetchone()
                        if own:
                            self.conn.execute(
                                "UPDATE slot_allocations SET status='released', updated_at=? WHERE id=?",
                                (at, own["id"]),
                            )
                        self.conn.execute(
                            """
                            UPDATE care_assignments SET status='ended', ends_at=?
                            WHERE adult_id=? AND child_id=? AND status='active'
                            """,
                            (at, ho["departing_adult_id"], hc["child_id"]),
                        )
                        care_id = new_id("CARE")
                        self.conn.execute(
                            """
                            INSERT INTO care_assignments(
                                id, session_id, child_id, adult_id, kind,
                                source, handover_id, slot_allocation_id,
                                status, starts_at, note
                            ) VALUES (?, ?, ?, ?, 'actual', ?, ?, ?, 'active', ?, ?)
                            """,
                            (
                                care_id,
                                permit["session_id"],
                                hc["child_id"],
                                ho["receiving_adult_id"],
                                ho["source"],
                                ho_id,
                                hc["slot_allocation_id"],
                                at,
                                "过闸兑现后实际接手",
                            ),
                        )
                    effective_handover = self.get_handover(ho_id)
                    self._ledger(
                        "handover.effective",
                        "过闸已发生，接替人成为实际接手人",
                        session_id=permit["session_id"],
                        ref_type="handover",
                        ref_id=ho_id,
                        payload={
                            "receiving_adult_id": ho["receiving_adult_id"],
                            "children": [r["child_id"] for r in hc_rows],
                        },
                        at=at,
                    )
                self.conn.commit()
                return {
                    "permit": self.get_permit(permit_id),
                    "crossing_event_id": refs["event_id"],
                    "receipt_id": refs["receipt_id"],
                    "location": self.location_of(band["id"], permit["session_id"]),
                    "handover": effective_handover,
                }
            except ApiError:
                # Business denials have already persisted their audit rows.
                raise
            except Exception:
                self.conn.rollback()
                raise

    # ---------- companion time borrowing ----------

    def create_time_account(self, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        account_id = data.get("id") or new_id("TIME")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                session = self._get("sessions", data["session_id"])
                adult = self._get("persons", data["adult_id"])
                family_id = data.get("family_id", adult["family_id"])
                self._get("families", family_id)
                if data.get("segment_id"):
                    self._get("plan_segments", data["segment_id"])
                units = int(data.get("sellable_units", 1))
                self.conn.execute(
                    """
                    INSERT INTO companion_time_accounts(
                        id, session_id, family_id, adult_id, segment_id,
                        label, original_minutes, sellable_units,
                        units_remaining, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        label=excluded.label,
                        original_minutes=excluded.original_minutes,
                        sellable_units=excluded.sellable_units,
                        units_remaining=excluded.units_remaining
                    """,
                    (
                        account_id,
                        session["id"],
                        family_id,
                        adult["id"],
                        data.get("segment_id"),
                        data.get("label", f"{adult['display_name']}原陪同时间"),
                        int(data.get("original_minutes", 0)),
                        units,
                        int(data.get("units_remaining", units)),
                        at,
                    ),
                )
                self._ledger(
                    "time_account.created",
                    f"建立可借用陪同时间：{adult['display_name']}",
                    session_id=session["id"],
                    ref_type="time_account",
                    ref_id=account_id,
                    payload={"sellable_units": units},
                    at=at,
                )
                self.conn.commit()
                return self._get("companion_time_accounts", account_id)
            except Exception:
                self.conn.rollback()
                raise

    def apply_time_loan(self, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        key = data.get("idempotency_key")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._idempotent("time_loan_requests", key)
                if existing:
                    self.conn.commit()
                    return self.get_time_loan(existing["id"])
                account = self._get("companion_time_accounts", data["account_id"])
                session = self._get("sessions", account["session_id"])
                to_family = self._get("families", data["to_family_id"])
                child = self._get("persons", data["child_id"])
                if child["family_id"] != to_family["id"]:
                    raise ApiError(422, "child_family_mismatch", "借用家庭与孩子所属家庭不一致")
                if not self._consent_allows(account["family_id"], to_family["id"], "borrow_allowed"):
                    raise ApiError(403, "borrow_not_allowed", "两家未登记允许借用陪同时间/名额")
                loan_id = data.get("id") or new_id("LOAN")
                self.conn.execute(
                    """
                    INSERT INTO time_loan_requests(
                        id, session_id, account_id, from_family_id,
                        to_family_id, child_id, fulfilling_adult_id,
                        units, starts_at, ends_at, status,
                        fulfillment_state, idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied',
                              'awaiting_decision', ?, ?)
                    """,
                    (
                        loan_id,
                        session["id"],
                        account["id"],
                        account["family_id"],
                        to_family["id"],
                        child["id"],
                        data.get("fulfilling_adult_id"),
                        int(data.get("units", 1)),
                        data.get("starts_at"),
                        data.get("ends_at"),
                        key,
                        at,
                    ),
                )
                self._ledger(
                    "time_loan.applied",
                    f"{to_family['name']} 申请借用 {account['label']}",
                    session_id=session["id"],
                    ref_type="time_loan",
                    ref_id=loan_id,
                    payload={"account_id": account["id"], "child_id": child["id"]},
                    at=at,
                )
                self.conn.commit()
                return self.get_time_loan(loan_id)
            except Exception:
                self.conn.rollback()
                raise

    def decide_time_loan(self, loan_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        approve = bool(data.get("approve", True))
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                loan = self._get("time_loan_requests", loan_id)
                if loan["status"] in {"approved_sold", "rejected", "fulfilled"}:
                    self.conn.commit()
                    return self.get_time_loan(loan_id)
                if not approve:
                    self.conn.execute(
                        """
                        UPDATE time_loan_requests
                        SET status='rejected', decision_reason=?, decided_at=?
                        WHERE id=?
                        """,
                        (data.get("reason", "前台未分配名额"), at, loan_id),
                    )
                    self._ledger(
                        "time_loan.rejected",
                        "陪同时间借用申请被拒绝",
                        session_id=loan["session_id"],
                        ref_type="time_loan",
                        ref_id=loan_id,
                        payload={"reason": data.get("reason")},
                        at=at,
                    )
                    self.conn.commit()
                    return self.get_time_loan(loan_id)

                account = self._get("companion_time_accounts", loan["account_id"])
                units = int(loan["units"])
                if int(account["units_remaining"]) < units:
                    raise ApiError(
                        409,
                        "loan_quota_exhausted",
                        "甲原来的可借用陪同时间/名额已经卖出或使用，不能重复使用",
                        {
                            "account_id": account["id"],
                            "units_remaining": account["units_remaining"],
                            "requested_units": units,
                        },
                    )
                self.conn.execute(
                    "UPDATE companion_time_accounts SET units_remaining=units_remaining-? WHERE id=?",
                    (units, account["id"]),
                )
                fulfillment_adult = loan["fulfilling_adult_id"] or data.get("fulfilling_adult_id")
                state = "awaiting_present_adult"
                reason = "借用名额已售出/记账，但尚未有实际在水区的兑现成人"
                if fulfillment_adult:
                    loc = self.location_of(fulfillment_adult, loan["session_id"])
                    if loc["present_in_water"]:
                        state = "ready"
                        reason = "指定兑现成人已有实际入区回条"
                    else:
                        reason = "指定成人仍在闸机外或无回条；售票不能证明其在水区"
                self.conn.execute(
                    """
                    UPDATE time_loan_requests
                    SET status='approved_sold', fulfillment_state=?,
                        fulfilling_adult_id=?, decision_reason=?, decided_at=?
                    WHERE id=?
                    """,
                    (state, fulfillment_adult, reason, at, loan_id),
                )
                self._ledger(
                    "time_loan.sold",
                    "借用申请已批准并扣减可借用名额；实际陪同仍需在场兑现",
                    session_id=loan["session_id"],
                    ref_type="time_loan",
                    ref_id=loan_id,
                    payload={"units": units, "fulfillment_state": state},
                    at=at,
                )
                self.conn.commit()
                return self.get_time_loan(loan_id)
            except ApiError:
                self.conn.rollback()
                raise
            except Exception:
                self.conn.rollback()
                raise

    def fulfill_time_loan(self, loan_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        at = utc_now(data.get("at"))
        adult_id = data.get("fulfilling_adult_id")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                loan = self._get("time_loan_requests", loan_id)
                if loan["status"] == "fulfilled":
                    self.conn.commit()
                    result = self.get_time_loan(loan_id)
                    result["idempotent_replay"] = True
                    return result
                if loan["status"] != "approved_sold":
                    raise ApiError(409, "loan_not_approved", "借用名额尚未批准/售出")
                adult_id = adult_id or loan["fulfilling_adult_id"]
                if not adult_id:
                    raise ApiError(422, "fulfilling_adult_required", "必须指定实际在场的兑现成人")
                adult = self._get("persons", adult_id)
                loc = self.location_of(adult_id, loan["session_id"])
                if not loc["present_in_water"]:
                    raise ApiError(
                        409,
                        "adult_not_in_water",
                        "兑现成人没有实际入区回条；已售出的借用名额不能假定人在水区",
                        loc,
                    )
                child_loc = self.location_of(loan["child_id"], loan["session_id"])
                if not child_loc["present_in_water"]:
                    raise ApiError(409, "child_not_in_water", "借用家庭儿童尚未实际入区", child_loc)
                if not self.can_see(loc["zone_id"], child_loc["zone_id"]):
                    raise ApiError(409, "not_same_or_visible", "兑现成人与儿童不在同区/可见区域")
                if not self._consent_allows(loan["from_family_id"], loan["to_family_id"], "borrow_allowed"):
                    raise ApiError(403, "borrow_not_allowed", "家庭借用许可不存在")
                alloc = self._reserve_slot(
                    session_id=loan["session_id"],
                    adult_id=adult_id,
                    child_id=loan["child_id"],
                    purpose="cross_family_loan",
                    ref_type="time_loan",
                    ref_id=loan_id,
                    at=at,
                    consume=True,
                )
                care_id = new_id("CARE")
                self.conn.execute(
                    """
                    INSERT INTO care_assignments(
                        id, session_id, child_id, adult_id, kind, source,
                        slot_allocation_id, status, starts_at, note
                    ) VALUES (?, ?, ?, ?, 'actual', 'loan', ?, 'active', ?, ?)
                    """,
                    (care_id, loan["session_id"], loan["child_id"], adult_id, alloc["id"], at, "借用陪同时间实际兑现"),
                )
                self.conn.execute(
                    """
                    UPDATE time_loan_requests
                    SET status='fulfilled', fulfillment_state='fulfilled',
                        fulfilling_adult_id=?, slot_allocation_id=?, fulfilled_at=?
                    WHERE id=?
                    """,
                    (adult_id, alloc["id"], at, loan_id),
                )
                self._ledger(
                    "time_loan.fulfilled",
                    f"{adult['display_name']} 已凭实际入区回条兑现借用陪同",
                    session_id=loan["session_id"],
                    ref_type="time_loan",
                    ref_id=loan_id,
                    payload={"slot_allocation_id": alloc["id"], "care_assignment_id": care_id},
                    at=at,
                )
                self.conn.commit()
                return self.get_time_loan(loan_id)
            except Exception:
                self.conn.rollback()
                raise

    def get_time_loan(self, loan_id: str) -> Dict[str, Any]:
        return self._get("time_loan_requests", loan_id)

    # ---------- answers / state ----------

    def answer(self, session_id: str) -> Dict[str, Any]:
        session = self._get("sessions", session_id)
        persons = [self._row(r) for r in self.conn.execute("SELECT * FROM persons WHERE active=1 ORDER BY id")]
        locations = [self.location_of(p["id"], session_id) for p in persons]

        effective_handovers = [
            self.get_handover(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM handovers WHERE session_id=? AND status='effective' ORDER BY effective_at",
                (session_id,),
            )
        ]
        prepared_handovers = [
            self.get_handover(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM handovers WHERE session_id=? AND status='prepared' ORDER BY prepared_at",
                (session_id,),
            )
        ]
        care = [
            self._row(r)
            for r in self.conn.execute(
                "SELECT * FROM care_assignments WHERE session_id=? AND status='active' ORDER BY starts_at",
                (session_id,),
            )
        ]
        exits = [
            self._row(r)
            for r in self.conn.execute(
                """
                SELECT e.*, p.display_name
                FROM crossing_events e JOIN persons p ON p.id=e.wristband_id
                WHERE e.session_id=? AND e.kind='gate_exit'
                ORDER BY e.at, e.rowid
                """,
                (session_id,),
            )
        ]
        entries = [
            self._row(r)
            for r in self.conn.execute(
                """
                SELECT e.*, p.display_name
                FROM crossing_events e JOIN persons p ON p.id=e.wristband_id
                WHERE e.session_id=? AND e.kind='gate_entry'
                ORDER BY e.at, e.rowid
                """,
                (session_id,),
            )
        ]
        child_answers = []
        for p in persons:
            if p["role"] != "child":
                continue
            actual_adults = [
                c["adult_id"]
                for c in care
                if c["child_id"] == p["id"] and c["kind"] == "actual"
            ]
            active_adults = [c["adult_id"] for c in care if c["child_id"] == p["id"]]
            actual_names = [
                next((x["display_name"] for x in persons if x["id"] == aid), aid)
                for aid in actual_adults
            ]
            child_answers.append({
                "child_id": p["id"],
                "display_name": p["display_name"],
                "location": next(l for l in locations if l["wristband_id"] == p["id"]),
                "active_caregiver_ids": active_adults,
                "actual_receiver_ids": actual_adults,
                "actual_receiver_names": actual_names,
            })
        return {
            "session": session,
            "people_locations": locations,
            "children": child_answers,
            "effective_handovers": effective_handovers,
            "prepared_handovers": prepared_handovers,
            "gate_exits": exits,
            "gate_entries": entries,
            "active_care_assignments": care,
        }

    def ledger(self, session_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        if session_id:
            rows = self.conn.execute(
                "SELECT * FROM ledger_entries WHERE session_id=? ORDER BY seq DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM ledger_entries ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            result.append(d)
        return result

    def full_state(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        venue = self.get_venue()
        families = [self._row(r) for r in self.conn.execute("SELECT * FROM families ORDER BY id")]
        persons = [self.location_of(r["id"]) | self._row(r) for r in self.conn.execute("SELECT * FROM persons ORDER BY id")]
        sessions = [self._row(r) for r in self.conn.execute("SELECT * FROM sessions ORDER BY opened_at DESC")]
        slots = [self._row(r) for r in self.conn.execute("SELECT * FROM slot_allocations ORDER BY created_at DESC")]
        permits = [self._row(r) for r in self.conn.execute("SELECT * FROM permits ORDER BY issued_at DESC")]
        loans = [self._row(r) for r in self.conn.execute("SELECT * FROM time_loan_requests ORDER BY created_at DESC")]
        answer = self.answer(session_id) if session_id else None
        return {
            "venue": venue,
            "families": families,
            "persons": persons,
            "sessions": sessions,
            "slot_allocations": slots,
            "permits": permits,
            "time_loans": loans,
            "answer": answer,
            "ledger": self.ledger(session_id, 100),
        }

    def seed_demo_if_empty(self) -> Dict[str, Any]:
        zone_count = self.conn.execute("SELECT COUNT(*) AS n FROM zones").fetchone()["n"]
        person_count = self.conn.execute("SELECT COUNT(*) AS n FROM persons").fetchone()["n"]
        if zone_count or person_count:
            return {
                "seeded": False,
                "reason": "database already contains venue/person data",
                "zones": zone_count,
                "wristbands": person_count,
            }
        from .seed import seed_demo_world
        return seed_demo_world(self)

    def reset_and_seed_demo(self) -> Dict[str, Any]:
        from .seed import seed_demo_world

        with self.lock:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            tables = [
                "ledger_entries", "permit_attempts", "permits", "handovers",
                "handover_children", "care_assignments", "time_loan_requests",
                "companion_time_accounts", "slot_allocations", "plan_segments",
                "aid_group_families", "aid_groups", "crossing_events", "receipts",
                "sessions", "persons", "family_consents", "visibility_edges",
                "ports", "zones", "families",
            ]
            for table in tables:
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.execute("DELETE FROM sqlite_sequence")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.commit()
        return seed_demo_world(self)
