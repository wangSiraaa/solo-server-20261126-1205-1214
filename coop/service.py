"""Transactional application service used by both local HTTP clients."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from typing import Any

from .engine import canonical_calculation_json, evaluate_window


class CoopError(Exception):
    status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(CoopError):
    status_code = 404


class Conflict(CoopError):
    status_code = 409


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class CoopService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.lock = threading.RLock()

    # ---------- generic helpers ----------

    def _audit(
        self,
        client_id: str,
        action: str,
        entity_type: str,
        entity_id: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO audit_events
               (client_id, actor, action, entity_type, entity_id, detail_json)
               VALUES (?, 'local-client', ?, ?, ?, ?)""",
            (client_id, action, entity_type, entity_id, _json_dumps(detail or {})),
        )

    def _get_customer(self, customer_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"customer not found: {customer_id}")
        return row

    def _get_line(self, line_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM access_lines WHERE id = ?", (line_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"access line not found: {line_id}")
        return row

    def _get_case(self, case_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM compensation_cases WHERE id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"compensation case not found: {case_id}")
        return row

    def _notification(
        self,
        *,
        customer_id: str,
        kind: str,
        title: str,
        body: str,
        client_id: str,
        renewal_order_id: str | None = None,
        case_id: str | None = None,
        evidence_id: str | None = None,
    ) -> str:
        notification_id = _new_id("NTF")
        self.conn.execute(
            """INSERT INTO notifications
               (id, customer_id, kind, title, body, renewal_order_id,
                case_id, evidence_id, delivered_by_client)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                notification_id,
                customer_id,
                kind,
                title,
                body,
                renewal_order_id,
                case_id,
                evidence_id,
                client_id,
            ),
        )
        return notification_id

    # ---------- master data and migration ----------

    def create_customer(
        self, customer_id: str, name: str, client_id: str
    ) -> dict[str, Any]:
        with self.lock, self.conn:
            existing = self.conn.execute(
                "SELECT * FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
            if existing is not None:
                if existing["name"] != name:
                    raise Conflict(
                        "customer id already exists with a different name",
                        details={"existing": row_dict(existing)},
                    )
                return {"customer": row_dict(existing), "existed": True}

            self.conn.execute(
                "INSERT INTO customers (id, name, created_via_client) VALUES (?, ?, ?)",
                (customer_id, name, client_id),
            )
            self._audit(client_id, "customer.create", "customer", customer_id, {"name": name})
            return {"customer": row_dict(self._get_customer(customer_id)), "existed": False}

    def create_line(
        self,
        line_id: str,
        customer_id: str,
        label: str,
        address: str,
        opened_at: int,
        client_id: str,
        returned_at: int | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.conn:
            self._get_customer(customer_id)
            if label not in {"old", "new", "other"}:
                raise CoopError("label must be old, new, or other")
            if not isinstance(opened_at, int) or opened_at < 0:
                raise CoopError("opened_at must be a non-negative integer minute")

            existing = self.conn.execute(
                "SELECT * FROM access_lines WHERE id = ?", (line_id,)
            ).fetchone()
            if existing is not None:
                return {"line": row_dict(existing), "existed": True}

            self.conn.execute(
                """INSERT INTO access_lines
                   (id, customer_id, label, address, opened_at, returned_at,
                    created_via_client)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (line_id, customer_id, label, address, opened_at, returned_at, client_id),
            )
            self._audit(
                client_id,
                "line.create",
                "access_line",
                line_id,
                {
                    "customer_id": customer_id,
                    "label": label,
                    "opened_at": opened_at,
                    "returned_at": returned_at,
                },
            )
            return {"line": row_dict(self._get_line(line_id)), "existed": False}

    def add_commitment(
        self,
        commitment_id: str,
        line_id: str,
        start_minute: int,
        end_minute: int,
        client_id: str,
        kind: str = "promised",
        contract_id: str | None = None,
        evidence_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.conn:
            line = self._get_line(line_id)
            if kind not in {"promised", "test"}:
                raise CoopError("commitment kind must be promised or test")
            if start_minute > end_minute:
                raise CoopError("commitment start must be <= end")

            self.conn.execute(
                """INSERT OR IGNORE INTO commitment_intervals
                   (id, line_id, start_minute, end_minute, kind, contract_id,
                    evidence_id, created_via_client)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    commitment_id,
                    line_id,
                    start_minute,
                    end_minute,
                    kind,
                    contract_id,
                    evidence_id,
                    client_id,
                ),
            )
            row = self.conn.execute(
                "SELECT * FROM commitment_intervals WHERE id = ?", (commitment_id,)
            ).fetchone()
            self._audit(
                client_id,
                "commitment.add",
                "commitment_interval",
                commitment_id,
                {"line_id": line_id, "range": [start_minute, end_minute], "kind": kind},
            )
            return {"commitment": row_dict(row), "line_customer_id": line["customer_id"]}

    def add_evidence(
        self,
        evidence_id: str,
        kind: str,
        summary: str,
        client_id: str,
        customer_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.conn:
            if customer_id is not None:
                self._get_customer(customer_id)
            self.conn.execute(
                """INSERT OR IGNORE INTO evidence
                   (id, customer_id, kind, summary, payload_json, source_client)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    evidence_id,
                    customer_id,
                    kind,
                    summary,
                    _json_dumps(payload or {}),
                    client_id,
                ),
            )
            row = self.conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
            self._audit(client_id, "evidence.add", "evidence", evidence_id, {"kind": kind})
            return {"evidence": row_dict(row)}

    # ---------- fault reports and line facts ----------

    def record_fault_report(
        self,
        report_id: str,
        line_id: str,
        start_minute: int,
        client_id: str,
        end_minute: int | None = None,
        fault_key: str | None = None,
        reporter: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.conn:
            line = self._get_line(line_id)
            customer_id = line["customer_id"]

            existing_report = self.conn.execute(
                "SELECT * FROM fault_reports WHERE report_id = ?", (report_id,)
            ).fetchone()
            if existing_report is not None:
                outage = self.conn.execute(
                    "SELECT * FROM outages WHERE source_report_id = ? OR fault_key = ?",
                    (report_id, existing_report["fault_key"]),
                ).fetchone()
                return {
                    "report": row_dict(existing_report),
                    "outage": row_dict(outage),
                    "replayed": True,
                    "duplicate": True,
                }

            key = fault_key or f"AUTO-{start_minute}-{end_minute}"
            prior_same_fault = self.conn.execute(
                "SELECT * FROM fault_reports WHERE line_id = ? AND fault_key = ? ORDER BY received_at LIMIT 1",
                (line_id, key),
            ).fetchone()

            self.conn.execute(
                """INSERT INTO fault_reports
                   (report_id, customer_id, line_id, fault_key, start_minute,
                    end_minute, reporter, via_client, duplicate_of_report_id,
                    payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report_id,
                    customer_id,
                    line_id,
                    key,
                    start_minute,
                    end_minute,
                    reporter,
                    client_id,
                    prior_same_fault["report_id"] if prior_same_fault else None,
                    _json_dumps(payload or {}),
                ),
            )

            existing_outage = self.conn.execute(
                "SELECT * FROM outages WHERE line_id = ? AND fault_key = ?",
                (line_id, key),
            ).fetchone()
            duplicate_fault = existing_outage is not None

            if existing_outage is None:
                outage_id = f"OUT-{key}"
                self.conn.execute(
                    """INSERT INTO outages
                       (id, line_id, fault_key, start_minute, end_minute,
                        status, source_report_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        outage_id,
                        line_id,
                        key,
                        start_minute,
                        end_minute,
                        "repaired" if end_minute is not None else "open",
                        report_id,
                    ),
                )
            else:
                outage_id = existing_outage["id"]

            self._audit(
                client_id,
                "fault_report.record",
                "fault_report",
                report_id,
                {
                    "line_id": line_id,
                    "fault_key": key,
                    "duplicate_fault": duplicate_fault,
                    "range": [start_minute, end_minute],
                },
            )
            outage = self.conn.execute("SELECT * FROM outages WHERE id = ?", (outage_id,)).fetchone()
            report = self.conn.execute(
                "SELECT * FROM fault_reports WHERE report_id = ?", (report_id,)
            ).fetchone()
            return {
                "report": row_dict(report),
                "outage": row_dict(outage),
                "duplicate_fault": duplicate_fault,
                "replayed": False,
            }

    def record_repair(
        self,
        outage_id: str,
        repaired_at: int,
        client_id: str,
        evidence_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.conn:
            outage = self.conn.execute("SELECT * FROM outages WHERE id = ?", (outage_id,)).fetchone()
            if outage is None:
                raise NotFound(f"outage not found: {outage_id}")
            if repaired_at < outage["start_minute"]:
                raise CoopError("repair minute cannot precede fault start")

            self.conn.execute(
                """UPDATE outages
                   SET end_minute = ?, status = 'repaired',
                       repair_evidence_id = COALESCE(?, repair_evidence_id)
                   WHERE id = ?""",
                (repaired_at, evidence_id, outage_id),
            )
            self._audit(
                client_id,
                "repair.record",
                "outage",
                outage_id,
                {"repaired_at": repaired_at, "evidence_id": evidence_id},
            )
            return {"outage": row_dict(self.conn.execute("SELECT * FROM outages WHERE id=?", (outage_id,)).fetchone())}

    def correct_outage(
        self,
        outage_id: str,
        evidence_id: str,
        client_id: str,
        new_status: str = "void",
        new_start_minute: int | None = None,
        new_end_minute: int | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.conn:
            outage = self.conn.execute("SELECT * FROM outages WHERE id = ?", (outage_id,)).fetchone()
            if outage is None:
                raise NotFound(f"outage not found: {outage_id}")
            evidence = self.conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
            if evidence is None:
                raise NotFound(f"evidence not found: {evidence_id}")
            if new_status not in {"void", "corrected"}:
                raise CoopError("new_status must be void or corrected")

            old = row_dict(outage)
            if new_status == "void":
                self.conn.execute(
                    "UPDATE outages SET status='void', correction_evidence_id=? WHERE id=?",
                    (evidence_id, outage_id),
                )
                replacement = None
            else:
                self.conn.execute(
                    "UPDATE outages SET status='corrected', correction_evidence_id=? WHERE id=?",
                    (evidence_id, outage_id),
                )
                replacement_id = _new_id("OUT-CORRECTED")
                start = outage["start_minute"] if new_start_minute is None else new_start_minute
                end = new_end_minute
                self.conn.execute(
                    """INSERT INTO outages
                       (id, line_id, fault_key, start_minute, end_minute, status,
                        source_report_id, correction_evidence_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        replacement_id,
                        outage["line_id"],
                        (outage["fault_key"] or outage_id) + "-corrected",
                        start,
                        end,
                        "repaired" if end is not None else "open",
                        outage["source_report_id"],
                        evidence_id,
                    ),
                )
                replacement = row_dict(
                    self.conn.execute("SELECT * FROM outages WHERE id=?", (replacement_id,)).fetchone()
                )

            self._audit(
                client_id,
                "outage.correct",
                "outage",
                outage_id,
                {"old": old, "new_status": new_status, "evidence_id": evidence_id},
            )
            return {
                "old_outage": old,
                "corrected_outage": row_dict(
                    self.conn.execute("SELECT * FROM outages WHERE id=?", (outage_id,)).fetchone()
                ),
                "replacement_outage": replacement,
            }

    def correct_commitment_to_test(
        self,
        line_id: str,
        start_minute: int,
        end_minute: int,
        evidence_id: str,
        client_id: str,
        correction_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace any active promise overlapping the range with test access.

        Non-overlapping left/right pieces are preserved.  Old rows are marked
        superseded rather than deleted, keeping the original contract trace.
        """
        with self.lock, self.conn:
            self._get_line(line_id)
            evidence = self.conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if evidence is None:
                raise NotFound(f"evidence not found: {evidence_id}")
            cid = correction_id or _new_id("CMT-CORR")

            intervals = self.conn.execute(
                """SELECT * FROM commitment_intervals
                   WHERE line_id=? AND status='active'
                     AND start_minute < ? AND end_minute > ?
                   ORDER BY start_minute""",
                (line_id, end_minute, start_minute),
            ).fetchall()
            if not intervals:
                raise Conflict("no active commitment interval overlaps correction range")

            created: list[dict[str, Any]] = []
            for index, interval in enumerate(intervals):
                s, e = interval["start_minute"], interval["end_minute"]
                overlap_s = max(s, start_minute)
                overlap_e = min(e, end_minute)
                new_cid = cid if len(intervals) == 1 else f"{cid}-{index}"

                self.conn.execute(
                    "UPDATE commitment_intervals SET status='superseded', superseded_by=? WHERE id=?",
                    (new_cid, interval["id"]),
                )

                pieces = []
                if s < overlap_s:
                    pieces.append((f"{interval['id']}-left", s, overlap_s, interval["kind"]))
                pieces.append((new_cid, overlap_s, overlap_e, "test"))
                if overlap_e < e:
                    pieces.append((f"{interval['id']}-right", overlap_e, e, interval["kind"]))

                for pid, ps, pe, kind in pieces:
                    self.conn.execute(
                        """INSERT INTO commitment_intervals
                           (id, line_id, start_minute, end_minute, kind, status,
                            contract_id, evidence_id, created_via_client)
                           VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                        (
                            pid,
                            line_id,
                            ps,
                            pe,
                            kind,
                            interval["contract_id"],
                            evidence_id if kind == "test" else interval["evidence_id"],
                            client_id,
                        ),
                    )
                    row = self.conn.execute("SELECT * FROM commitment_intervals WHERE id=?", (pid,)).fetchone()
                    created.append(row_dict(row))

            self._audit(
                client_id,
                "commitment.correct_to_test",
                "access_line",
                line_id,
                {"range": [start_minute, end_minute], "evidence_id": evidence_id, "created": [x["id"] for x in created]},
            )
            return {"line_id": line_id, "active_commitments": created}

    # ---------- compensation calculation and correction ----------

    def run_compensation(
        self,
        case_id: str,
        customer_id: str,
        window_start: int,
        window_end: int,
        client_id: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.conn:
            self._get_customer(customer_id)
            existing = self.conn.execute(
                "SELECT * FROM compensation_cases WHERE id=? OR case_key=?",
                (case_id, case_id),
            ).fetchone()
            if existing is not None:
                raise Conflict(
                    "compensation case already exists; use recalculate_compensation",
                    details={"case_id": existing["id"]},
                )

            self.conn.execute(
                """INSERT INTO compensation_cases
                   (id, customer_id, case_key, window_start, window_end,
                    status, current_version, created_via_client)
                   VALUES (?, ?, ?, ?, ?, 'open', 0, ?)""",
                (case_id, customer_id, case_id, window_start, window_end, client_id),
            )
            self._calculate_and_settle(case_id, None, client_id, initial=True)
            self._audit(client_id, "compensation.run", "case", case_id, {"window": [window_start, window_end]})
            return self.get_case(case_id)

    def recalculate_compensation(
        self,
        case_id: str,
        client_id: str,
        evidence_id: str | None = None,
        as_of_minute: int | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.conn:
            self._get_case(case_id)
            if evidence_id is not None:
                evidence = self.conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
                if evidence is None:
                    raise NotFound(f"evidence not found: {evidence_id}")
            if as_of_minute is not None and as_of_minute < 0:
                raise CoopError("as_of_minute must be non-negative")
            self._calculate_and_settle(
                case_id,
                evidence_id,
                client_id,
                initial=False,
                as_of_minute=as_of_minute,
            )
            self._audit(
                client_id,
                "compensation.recalculate",
                "case",
                case_id,
                {"evidence_id": evidence_id},
            )
            return self.get_case(case_id)

    def _calculate_and_settle(
        self,
        case_id: str,
        evidence_id: str | None,
        client_id: str,
        *,
        initial: bool,
        as_of_minute: int | None = None,
    ) -> None:
        case = self._get_case(case_id)
        calc = evaluate_window(
            self.conn, case["customer_id"], case["window_start"], case["window_end"]
        )
        version_no = case["current_version"] + 1
        version_id = f"{case_id}-V{version_no}"

        vouchers = self.conn.execute(
            "SELECT * FROM vouchers WHERE case_id=?", (case_id,)
        ).fetchall()
        current_net = sum(
            v["granted_minutes"] - v["cancelled_minutes"] - v["clawed_back_minutes"]
            for v in vouchers
        )

        self.conn.execute(
            """INSERT INTO compensation_versions
               (id, case_id, version_no, expected_minutes, current_net_minutes,
                trigger_evidence_id, calculation_json, via_client)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id,
                case_id,
                version_no,
                calc["expected_minutes"],
                current_net,
                evidence_id,
                canonical_calculation_json(calc),
                client_id,
            ),
        )

        for minute_row in calc["minutes"]:
            self.conn.execute(
                """INSERT INTO compensation_minute_rows
                   (version_id, minute, promised_line_ids_json,
                    down_promised_line_ids_json, owed, reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    version_id,
                    minute_row["minute"],
                    _json_dumps(minute_row["promised"]),
                    _json_dumps(minute_row["down_promised"]),
                    1 if minute_row["owed"] else 0,
                    minute_row["reason"],
                ),
            )

        self.conn.execute(
            "UPDATE compensation_cases SET current_version=?, status='corrected' WHERE id=?",
            (version_no, case_id),
        )

        target = calc["expected_minutes"]
        difference = target - current_net

        if difference > 0:
            self._issue_voucher(
                case_id=case_id,
                version_id=version_id,
                minutes=difference,
                client_id=client_id,
                evidence_id=evidence_id,
                supplemental=not initial,
            )
        elif difference < 0:
            self._recover_overissue(
                case_id=case_id,
                overage=-difference,
                evidence_id=evidence_id,
                client_id=client_id,
                version_id=version_id,
                as_of_minute=as_of_minute,
            )

        if initial and target == 0:
            self._notification(
                customer_id=case["customer_id"],
                kind="compensation_zero",
                title="SLA 复算结果：无需补时",
                body=f"案件 {case_id} 的半开区间复算结果为 0 分钟。",
                client_id=client_id,
                case_id=case_id,
                evidence_id=evidence_id,
            )

    def _issue_voucher(
        self,
        *,
        case_id: str,
        version_id: str,
        minutes: int,
        client_id: str,
        evidence_id: str | None,
        supplemental: bool,
    ) -> None:
        case = self._get_case(case_id)
        suffix = "SUP" if supplemental else "BASE"
        voucher_id = f"VCH-{case_id}-{suffix}-V{version_id.split('-V')[-1]}"
        # IDs are kept short and deterministic for the demo; uuid avoids collision
        # if the same business case is intentionally recreated in another DB.
        if self.conn.execute("SELECT 1 FROM vouchers WHERE id=?", (voucher_id,)).fetchone():
            voucher_id = _new_id(f"VCH-{case_id}-{suffix}")
        code = f"TIME-{voucher_id}".upper()
        self.conn.execute(
            """INSERT INTO vouchers
               (id, customer_id, case_id, origin_version_id, code,
                granted_minutes, status)
               VALUES (?, ?, ?, ?, ?, ?, 'available')""",
            (voucher_id, case["customer_id"], case_id, version_id, code, minutes),
        )
        self.conn.execute(
            """INSERT INTO voucher_ledger
               (id, voucher_id, entry_type, minutes_delta, evidence_id, note, via_client)
               VALUES (?, ?, 'grant', ?, ?, ?, ?)""",
            (
                _new_id("VL"),
                voucher_id,
                minutes,
                evidence_id,
                "迟到证据复算后补发" if supplemental else "首次 SLA 结算发放时券",
                client_id,
            ),
        )
        self._notification(
            customer_id=case["customer_id"],
            kind="voucher_issued",
            title=f"到账 {minutes} 分钟时券",
            body=(
                f"案件 {case_id} 第 {version_id.split('-V')[-1]} 版计算确认应付 "
                f"{minutes} 补偿分钟；券号 {code}，可在后续续期时选择抵扣。"
            ),
            client_id=client_id,
            case_id=case_id,
            evidence_id=evidence_id,
        )

    def _recover_overissue(
        self,
        *,
        case_id: str,
        overage: int,
        evidence_id: str | None,
        client_id: str,
        version_id: str,
        as_of_minute: int | None = None,
    ) -> None:
        case = self._get_case(case_id)
        remaining = overage

        vouchers = self.conn.execute(
            "SELECT * FROM vouchers WHERE case_id=? ORDER BY rowid",
            (case_id,),
        ).fetchall()

        # 1) Cancel minutes that have not yet been selected for a renewal.
        for voucher in vouchers:
            if remaining <= 0:
                break
            available = (
                voucher["granted_minutes"]
                - voucher["redeemed_minutes"]
                - voucher["cancelled_minutes"]
            )
            take = min(remaining, max(0, available))
            if take <= 0:
                continue

            self.conn.execute(
                "UPDATE vouchers SET cancelled_minutes = cancelled_minutes + ? WHERE id=?",
                (take, voucher["id"]),
            )
            self.conn.execute(
                """INSERT INTO voucher_ledger
                   (id, voucher_id, entry_type, minutes_delta, evidence_id, note, via_client)
                   VALUES (?, ?, 'cancel', -?, ?, ?, ?)""",
                (
                    _new_id("VL"),
                    voucher["id"],
                    take,
                    evidence_id,
                    f"晚到证据 {evidence_id or 'N/A'} 改判；注销尚未兑现的 {take} 分钟",
                    client_id,
                ),
            )
            remaining -= take

        # 2) Already-used minutes cannot be erased from the performed service.
        # Charge them back to the exact renewal order that redeemed the voucher.
        if remaining > 0:
            redemptions = self.conn.execute(
                """SELECT r.* FROM voucher_redemptions r
                   JOIN vouchers v ON v.id = r.voucher_id
                   WHERE v.case_id = ?
                     AND r.minutes_applied - r.minutes_clawed_back > 0
                   ORDER BY r.rowid""",
                (case_id,),
            ).fetchall()

            for redemption in redemptions:
                if remaining <= 0:
                    break
                recoverable = (
                    redemption["minutes_applied"] - redemption["minutes_clawed_back"]
                )
                take = min(remaining, recoverable)
                if take <= 0:
                    continue

                amount = take * redemption["unit_amount_minor"]
                order = self.conn.execute(
                    "SELECT * FROM renewal_orders WHERE id=?",
                    (redemption["renewal_order_id"],),
                ).fetchone()

                consumed_minutes: int | None = None
                performed_interval: list[int] | None = None
                if order is not None and as_of_minute is not None:
                    performed_end = max(
                        order["service_start"],
                        min(as_of_minute, order["service_end"]),
                    )
                    consumed_minutes = max(0, performed_end - order["service_start"])
                    performed_interval = [order["service_start"], performed_end]

                clawback_detail = {
                    "as_of_minute": as_of_minute,
                    "service_interval": (
                        [order["service_start"], order["service_end"]] if order else None
                    ),
                    "performed_half_open_interval": performed_interval,
                    "performed_minutes": consumed_minutes,
                    "consumption_policy": (
                        "时券在续期受理时已形成账单减免；未用权益注销，已兑现减免按原资费挂账。"
                        "上网服务段不做负数回滚。"
                    ),
                    "redemption_id": redemption["id"],
                    "unit_amount_minor": redemption["unit_amount_minor"],
                }

                self.conn.execute(
                    "UPDATE voucher_redemptions SET minutes_clawed_back = minutes_clawed_back + ? WHERE id=?",
                    (take, redemption["id"]),
                )
                self.conn.execute(
                    "UPDATE vouchers SET clawed_back_minutes = clawed_back_minutes + ? WHERE id=?",
                    (take, redemption["voucher_id"]),
                )
                self.conn.execute(
                    """INSERT INTO voucher_ledger
                       (id, voucher_id, entry_type, minutes_delta,
                        renewal_order_id, evidence_id, note, via_client)
                       VALUES (?, ?, 'clawback', -?, ?, ?, ?, ?)""",
                    (
                        _new_id("VL"),
                        redemption["voucher_id"],
                        take,
                        redemption["renewal_order_id"],
                        evidence_id,
                        (
                            f"证据 {evidence_id or 'N/A'} 证明原发多 {take} 分钟；"
                            "券已用于续期，上网段不撤回，转为该续期单差额挂账"
                        ),
                        client_id,
                    ),
                )
                self.conn.execute(
                    """INSERT INTO account_ledger
                       (id, customer_id, renewal_order_id, case_id, evidence_id,
                        kind, amount_minor, minutes, description, detail_json,
                        via_client)
                       VALUES (?, ?, ?, ?, ?, 'voucher_clawback_charge', ?, ?, ?, ?, ?)""",
                    (
                        _new_id("AL"),
                        case["customer_id"],
                        redemption["renewal_order_id"],
                        case_id,
                        evidence_id,
                        amount,
                        take,
                        (
                            f"晚到证据 {evidence_id or 'N/A'} 改判案件 {case_id}；"
                            f"追回已在续期单 {redemption['renewal_order_id']} 兑现的 "
                            f"{take} 分钟，合成资费 {redemption['unit_amount_minor']}/分钟"
                        ),
                        _json_dumps(clawback_detail),
                        client_id,
                    ),
                )
                consumed_clause = ""
                if consumed_minutes is not None and performed_interval is not None:
                    consumed_clause = (
                        f"截至查询时刻 minute {as_of_minute}，该续期段已履行 "
                        f"{performed_interval[0]}≤t<{performed_interval[1]} 共 "
                        f"{consumed_minutes} 分钟；这些上网分钟保留，不做删除。"
                    )
                self._notification(
                    customer_id=case["customer_id"],
                    kind="clawback_notice",
                    title="补偿更正：已兑现时券需结算差额",
                    body=(
                        f"晚到证据 {evidence_id or 'N/A'} 使案件 {case_id} 应付分钟下调。"
                        f"其中 {take} 分钟已用于续期回执 {order['receipt_id'] if order else 'N/A'}，"
                        f"对应续期单 {redemption['renewal_order_id']} 补记 {amount} 合成币应付款。"
                        f"{consumed_clause}"
                        "已开通并履行的上网时段保持不变，不会被删除或断网。"
                    ),
                    client_id=client_id,
                    renewal_order_id=redemption["renewal_order_id"],
                    case_id=case_id,
                    evidence_id=evidence_id,
                )
                remaining -= take

        if remaining > 0:
            # Invariant failure rather than silently losing an auditable difference.
            raise CoopError(
                "could not recover all over-issued compensation minutes",
                details={"remaining": remaining},
            )

        for voucher in vouchers:
            self._refresh_voucher_status(voucher["id"])

        self._notification(
            customer_id=case["customer_id"],
            kind="compensation_corrected",
            title="补偿案件已更正",
            body=(
                f"案件 {case_id} 已按证据 {evidence_id or 'N/A'} 生成 {version_id}；"
                f"本次追回 {overage} 个过发分钟。未用分钟已注销，已用部分已回到原续期期挂账。"
            ),
            client_id=client_id,
            case_id=case_id,
            evidence_id=evidence_id,
        )

    def _refresh_voucher_status(self, voucher_id: str) -> None:
        v = self.conn.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
        if v is None:
            return
        available = v["granted_minutes"] - v["redeemed_minutes"] - v["cancelled_minutes"]
        if v["clawed_back_minutes"] > 0 and available == 0:
            status = "overissued_settled"
        elif available == 0 and v["redeemed_minutes"] > 0:
            status = "redeemed"
        elif v["redeemed_minutes"] > 0:
            status = "partially_redeemed"
        elif available == 0 and v["cancelled_minutes"] >= v["granted_minutes"]:
            status = "cancelled"
        else:
            status = "available"
        self.conn.execute("UPDATE vouchers SET status=? WHERE id=?", (status, voucher_id))

    # ---------- renewal receipt, voucher redemption, payment ----------

    def list_renewal_options(
        self,
        customer_id: str,
        client_id: str,
        voucher_id: str | None = None,
    ) -> dict[str, Any]:
        """Synthetic integer-minute renewal catalog where a voucher may be used."""
        with self.lock:
            self._get_customer(customer_id)
            packages = [
                {"package_code": "coop-10-minutes", "service_minutes": 10, "rate_minor_per_minute": 1},
                {"package_code": "coop-30-minutes", "service_minutes": 30, "rate_minor_per_minute": 1},
                {"package_code": "coop-60-minutes", "service_minutes": 60, "rate_minor_per_minute": 1},
            ]

            voucher = None
            available = 0
            if voucher_id:
                row = self.conn.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
                if row is None:
                    raise NotFound(f"voucher not found: {voucher_id}")
                if row["customer_id"] != customer_id:
                    raise Conflict("voucher belongs to another customer")
                voucher = row_dict(row)
                available = (
                    row["granted_minutes"] - row["redeemed_minutes"] - row["cancelled_minutes"]
                )

            latest = self.conn.execute(
                "SELECT MAX(service_end) AS end FROM renewal_orders WHERE customer_id=?",
                (customer_id,),
            ).fetchone()
            suggested_start = latest["end"] if latest and latest["end"] is not None else 0

            options = []
            for package in packages:
                list_amount = package["service_minutes"] * package["rate_minor_per_minute"]
                applicable = min(available, package["service_minutes"]) if voucher_id else 0
                discount = applicable * package["rate_minor_per_minute"]
                options.append(
                    {
                        **package,
                        "list_amount_minor": list_amount,
                        "voucher_applicable_minutes": applicable,
                        "discount_minor": discount,
                        "amount_due_minor": list_amount - discount,
                        "suggested_service_start": suggested_start,
                        "eligible": True,
                    }
                )
            return {
                "customer_id": customer_id,
                "voucher": voucher,
                "voucher_available_minutes": available,
                "options": options,
                "choice_rule": "客户可选择任意 eligible 续期套餐使用未注销时券；本经营模式为账单抵扣，不重叠安排服务分钟。",
            }

    def receive_renewal_receipt(
        self,
        receipt_id: str,
        customer_id: str,
        service_start: int,
        client_id: str,
        order_id: str | None = None,
        package_code: str = "coop-30-integer-minutes",
        service_minutes: int = 30,
        rate_minor_per_minute: int = 1,
        voucher_id: str | None = None,
        voucher_minutes: int | None = None,
        paid_amount_minor: int | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.conn:
            self._get_customer(customer_id)
            order_id = order_id or f"RNW-{receipt_id}"
            payload = {
                "receipt_id": receipt_id,
                "customer_id": customer_id,
                "service_start": service_start,
                "order_id": order_id,
                "package_code": package_code,
                "service_minutes": service_minutes,
                "rate_minor_per_minute": rate_minor_per_minute,
                "voucher_id": voucher_id,
                "voucher_minutes": voucher_minutes,
                "paid_amount_minor": paid_amount_minor,
            }
            payload_hash = hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()

            prior_receipt = self.conn.execute(
                "SELECT * FROM renewal_receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if prior_receipt is not None:
                if prior_receipt["payload_hash"] != payload_hash:
                    raise Conflict(
                        "receipt_id already used with a different renewal payload",
                        details={"receipt_id": receipt_id},
                    )
                self.conn.execute(
                    """UPDATE renewal_receipts
                       SET delivery_count = delivery_count + 1,
                           last_via_client=?, last_received_at=datetime('now')
                       WHERE receipt_id=?""",
                    (client_id, receipt_id),
                )
                self._audit(
                    client_id,
                    "renewal_receipt.redelivered",
                    "renewal_receipt",
                    receipt_id,
                    {"order_id": prior_receipt["renewal_order_id"]},
                )
                return self._renewal_result(
                    prior_receipt["renewal_order_id"], duplicate=True, receipt_redelivered=True
                )

            if self.conn.execute("SELECT 1 FROM renewal_orders WHERE id=?", (order_id,)).fetchone():
                raise Conflict(f"order_id already exists with another receipt: {order_id}")

            if service_minutes <= 0 or rate_minor_per_minute <= 0:
                raise CoopError("service_minutes and rate must be positive integers")

            list_amount = service_minutes * rate_minor_per_minute
            applied_minutes = 0
            discount = 0
            unit_amount = rate_minor_per_minute

            if voucher_id:
                voucher = self.conn.execute(
                    "SELECT * FROM vouchers WHERE id=?", (voucher_id,)
                ).fetchone()
                if voucher is None:
                    raise NotFound(f"voucher not found: {voucher_id}")
                if voucher["customer_id"] != customer_id:
                    raise Conflict("voucher belongs to another customer")
                available = (
                    voucher["granted_minutes"]
                    - voucher["redeemed_minutes"]
                    - voucher["cancelled_minutes"]
                )
                applied_minutes = (
                    voucher_minutes if voucher_minutes is not None else available
                )
                if applied_minutes <= 0:
                    raise CoopError("voucher_minutes must be positive")
                if applied_minutes > available:
                    raise Conflict(
                        "voucher does not have enough usable minutes",
                        details={"available": available, "requested": applied_minutes},
                    )
                discount = applied_minutes * unit_amount
                if discount > list_amount:
                    raise CoopError("voucher discount cannot exceed renewal list amount")

            amount_due = list_amount - discount
            if paid_amount_minor is None:
                paid_amount = amount_due
            else:
                paid_amount = paid_amount_minor
            if paid_amount < 0 or paid_amount > amount_due:
                raise CoopError("paid_amount_minor must be between 0 and amount_due")

            service_end = service_start + service_minutes
            self.conn.execute(
                """INSERT INTO renewal_orders
                   (id, customer_id, receipt_id, package_code, service_minutes,
                    rate_minor_per_minute, list_amount_minor, voucher_id,
                    voucher_minutes_applied, discount_minor, amount_due_minor,
                    paid_amount_minor, service_start, service_end, via_client)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    customer_id,
                    receipt_id,
                    package_code,
                    service_minutes,
                    rate_minor_per_minute,
                    list_amount,
                    voucher_id,
                    applied_minutes,
                    discount,
                    amount_due,
                    paid_amount,
                    service_start,
                    service_end,
                    client_id,
                ),
            )
            self.conn.execute(
                """INSERT INTO renewal_receipts
                   (receipt_id, payload_hash, renewal_order_id, delivery_count,
                    first_via_client, last_via_client)
                   VALUES (?, ?, ?, 1, ?, ?)""",
                (receipt_id, payload_hash, order_id, client_id, client_id),
            )

            self.conn.execute(
                """INSERT INTO account_ledger
                   (id, customer_id, renewal_order_id, kind, amount_minor,
                    minutes, description, via_client)
                   VALUES (?, ?, ?, 'renewal_charge', ?, ?, ?, ?)""",
                (
                    _new_id("AL"),
                    customer_id,
                    order_id,
                    list_amount,
                    service_minutes,
                    f"续期套餐 {package_code}，整数分钟 [{service_start},{service_end})",
                    client_id,
                ),
            )

            if voucher_id:
                voucher = self.conn.execute("SELECT * FROM vouchers WHERE id=?", (voucher_id,)).fetchone()
                self.conn.execute(
                    """INSERT INTO voucher_redemptions
                       (id, voucher_id, renewal_order_id, minutes_applied,
                        unit_amount_minor)
                       VALUES (?, ?, ?, ?, ?)""",
                    (_new_id("VR"), voucher_id, order_id, applied_minutes, unit_amount),
                )
                self.conn.execute(
                    "UPDATE vouchers SET redeemed_minutes = redeemed_minutes + ? WHERE id=?",
                    (applied_minutes, voucher_id),
                )
                self.conn.execute(
                    """INSERT INTO voucher_ledger
                       (id, voucher_id, entry_type, minutes_delta,
                        renewal_order_id, note, via_client)
                       VALUES (?, ?, 'redeem', 0, ?, ?, ?)""",
                    (
                        _new_id("VL"),
                        voucher_id,
                        order_id,
                        f"客户选择在续期单 {order_id} 使用 {applied_minutes} 分钟；"
                        "minutes_delta=0 表示补偿权益本身仍成立，只是已选择兑现账期",
                        client_id,
                    ),
                )
                self.conn.execute(
                    """INSERT INTO account_ledger
                       (id, customer_id, renewal_order_id, kind, amount_minor,
                        minutes, description, via_client)
                       VALUES (?, ?, ?, 'voucher_discount', ?, ?, ?, ?)""",
                    (
                        _new_id("AL"),
                        customer_id,
                        order_id,
                        -discount,
                        applied_minutes,
                        f"时券 {voucher['code']} 抵扣 {applied_minutes} 分钟 × "
                        f"{unit_amount} 合成资费",
                        client_id,
                    ),
                )
                self._refresh_voucher_status(voucher_id)

            if paid_amount:
                self.conn.execute(
                    """INSERT INTO account_ledger
                       (id, customer_id, renewal_order_id, kind, amount_minor,
                        minutes, description, via_client)
                       VALUES (?, ?, ?, 'payment', ?, 0, ?, ?)""",
                    (
                        _new_id("AL"),
                        customer_id,
                        order_id,
                        -paid_amount,
                        f"续期回执 {receipt_id} 收款 {paid_amount} 合成币",
                        client_id,
                    ),
                )

            self._notification(
                customer_id=customer_id,
                kind="renewal_accepted",
                title="续期与补时抵扣已入账",
                body=(
                    f"续期单 {order_id} / 回执 {receipt_id} 已受理；"
                    f"服务段 [{service_start},{service_end}) 不会因后续更正被删除。"
                    + (f" 时券抵扣 {discount} 合成币。" if discount else "")
                ),
                client_id=client_id,
                renewal_order_id=order_id,
            )
            self._audit(
                client_id,
                "renewal.receive_receipt",
                "renewal_order",
                order_id,
                {"receipt_id": receipt_id, "voucher_id": voucher_id, "discount": discount},
            )
            return self._renewal_result(order_id, duplicate=False, receipt_redelivered=False)

    def pay_outstanding_balance(
        self,
        renewal_order_id: str,
        client_id: str,
        payment_id: str | None = None,
        amount_minor: int | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.conn:
            order = self.conn.execute(
                "SELECT * FROM renewal_orders WHERE id=?", (renewal_order_id,)
            ).fetchone()
            if order is None:
                raise NotFound(f"renewal order not found: {renewal_order_id}")
            balance = self._order_balance(renewal_order_id)
            pay_amount = balance if amount_minor is None else amount_minor
            if pay_amount <= 0:
                raise Conflict("renewal order has no positive outstanding balance")
            if pay_amount > balance:
                raise CoopError("payment exceeds outstanding balance")

            pid = payment_id or _new_id("PAY")
            self.conn.execute(
                """INSERT INTO account_ledger
                   (id, customer_id, renewal_order_id, kind, amount_minor,
                    description, via_client)
                   VALUES (?, ?, ?, 'clawback_settlement_payment', ?, ?, ?)""",
                (
                    pid,
                    order["customer_id"],
                    renewal_order_id,
                    -pay_amount,
                    f"结算更正后的续期挂账差额 {pay_amount} 合成币",
                    client_id,
                ),
            )
            self._notification(
                customer_id=order["customer_id"],
                kind="balance_settled",
                title="更正差额已结清",
                body=f"续期单 {renewal_order_id} 的 {pay_amount} 合成币差额已收讫；上网段保持不变。",
                client_id=client_id,
                renewal_order_id=renewal_order_id,
            )
            self._audit(client_id, "renewal.pay_balance", "renewal_order", renewal_order_id, {"amount": pay_amount})
            return self._renewal_result(renewal_order_id, duplicate=False, receipt_redelivered=False)

    def _order_balance(self, order_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_minor), 0) AS balance FROM account_ledger WHERE renewal_order_id=?",
            (order_id,),
        ).fetchone()
        return int(row["balance"])

    def _renewal_result(
        self, order_id: str, *, duplicate: bool, receipt_redelivered: bool
    ) -> dict[str, Any]:
        order = self.conn.execute("SELECT * FROM renewal_orders WHERE id=?", (order_id,)).fetchone()
        if order is None:
            raise NotFound(order_id)
        receipt = self.conn.execute(
            "SELECT * FROM renewal_receipts WHERE receipt_id=?", (order["receipt_id"],)
        ).fetchone()
        entries = []
        for row in self.conn.execute(
            "SELECT * FROM account_ledger WHERE renewal_order_id=? ORDER BY rowid",
            (order_id,),
        ).fetchall():
            entry = row_dict(row) or {}
            if entry.get("detail_json"):
                entry["detail"] = json.loads(entry.pop("detail_json"))
            entries.append(entry)
        redemptions = self.conn.execute(
            "SELECT * FROM voucher_redemptions WHERE renewal_order_id=? ORDER BY rowid",
            (order_id,),
        ).fetchall()
        return {
            "order": row_dict(order),
            "receipt": row_dict(receipt),
            "duplicate_receipt": duplicate,
            "receipt_redelivered": receipt_redelivered,
            "account_entries": entries,
            "balance_minor": self._order_balance(order_id),
            "redemptions": [row_dict(x) for x in redemptions],
            "service_statement": (
                f"已履行/将履行的服务半开区间为 [{order['service_start']},"
                f"{order['service_end']})；更正只调整账务，不删除上网分钟。"
            ),
        }

    # ---------- read models ----------

    def health(self, client_id: str) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute("SELECT sqlite_version() AS v").fetchone()
            return {"ok": True, "sqlite_version": row["v"], "service": "coop-local"}

    def get_renewal(
        self,
        order_or_receipt_id: str,
        client_id: str,
        as_of_minute: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            order = self.conn.execute(
                "SELECT id FROM renewal_orders WHERE id=? OR receipt_id=?",
                (order_or_receipt_id, order_or_receipt_id),
            ).fetchone()
            if order is None:
                raise NotFound(f"renewal not found: {order_or_receipt_id}")
            result = self._renewal_result(order["id"], duplicate=False, receipt_redelivered=False)
            if as_of_minute is not None:
                o = result["order"]
                end = max(o["service_start"], min(as_of_minute, o["service_end"]))
                result["consumption_as_of"] = {
                    "as_of_minute": as_of_minute,
                    "performed_interval": [o["service_start"], end],
                    "performed_minutes": max(0, end - o["service_start"]),
                    "preserved": True,
                }
            return result

    def get_case(self, case_id: str, client_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            case = self._get_case(case_id)
            versions = self.conn.execute(
                "SELECT * FROM compensation_versions WHERE case_id=? ORDER BY version_no",
                (case_id,),
            ).fetchall()
            vouchers = self.conn.execute(
                "SELECT * FROM vouchers WHERE case_id=? ORDER BY id", (case_id,)
            ).fetchall()
            version_rows: list[dict[str, Any]] = []
            for version in versions:
                minute_rows = self.conn.execute(
                    "SELECT * FROM compensation_minute_rows WHERE version_id=? ORDER BY minute",
                    (version["id"],),
                ).fetchall()
                vd = row_dict(version)
                if vd is not None:
                    vd["calculation"] = json.loads(vd.pop("calculation_json"))
                    vd["minute_rows"] = [row_dict(x) for x in minute_rows]
                version_rows.append(vd or {})

            voucher_rows: list[dict[str, Any]] = []
            for voucher in vouchers:
                ledger = self.conn.execute(
                    "SELECT * FROM voucher_ledger WHERE voucher_id=? ORDER BY rowid",
                    (voucher["id"],),
                ).fetchall()
                redemptions = self.conn.execute(
                    "SELECT * FROM voucher_redemptions WHERE voucher_id=? ORDER BY rowid",
                    (voucher["id"],),
                ).fetchall()
                v = row_dict(voucher) or {}
                v["ledger"] = [row_dict(x) for x in ledger]
                v["redemptions"] = [row_dict(x) for x in redemptions]
                voucher_rows.append(v)

            return {
                "case": row_dict(case),
                "versions": version_rows,
                "vouchers": voucher_rows,
            }

    def get_customer_ledger(self, customer_id: str, client_id: str) -> dict[str, Any]:
        with self.lock:
            customer = row_dict(self._get_customer(customer_id))
            lines = []
            for line in self.conn.execute(
                "SELECT * FROM access_lines WHERE customer_id=? ORDER BY id", (customer_id,)
            ).fetchall():
                ld = row_dict(line) or {}
                ld["commitments"] = [
                    row_dict(x)
                    for x in self.conn.execute(
                        "SELECT * FROM commitment_intervals WHERE line_id=? ORDER BY start_minute, id",
                        (line["id"],),
                    ).fetchall()
                ]
                ld["outages"] = [
                    row_dict(x)
                    for x in self.conn.execute(
                        "SELECT * FROM outages WHERE line_id=? ORDER BY start_minute, id",
                        (line["id"],),
                    ).fetchall()
                ]
                ld["fault_reports"] = [
                    row_dict(x)
                    for x in self.conn.execute(
                        "SELECT * FROM fault_reports WHERE line_id=? ORDER BY received_at, report_id",
                        (line["id"],),
                    ).fetchall()
                ]
                lines.append(ld)

            cases = [
                self.get_case(c["id"])
                for c in self.conn.execute(
                    "SELECT id FROM compensation_cases WHERE customer_id=? ORDER BY id",
                    (customer_id,),
                ).fetchall()
            ]
            vouchers = [
                row_dict(x)
                for x in self.conn.execute(
                    "SELECT * FROM vouchers WHERE customer_id=? ORDER BY id", (customer_id,)
                ).fetchall()
            ]
            renewals = []
            for r in self.conn.execute(
                "SELECT id FROM renewal_orders WHERE customer_id=? ORDER BY id", (customer_id,)
            ).fetchall():
                renewals.append(self._renewal_result(r["id"], duplicate=False, receipt_redelivered=False))
            ledger = []
            for row in self.conn.execute(
                "SELECT * FROM account_ledger WHERE customer_id=? ORDER BY rowid",
                (customer_id,),
            ).fetchall():
                entry = row_dict(row) or {}
                if entry.get("detail_json"):
                    entry["detail"] = json.loads(entry.pop("detail_json"))
                ledger.append(entry)
            notifications = [
                row_dict(x)
                for x in self.conn.execute(
                    "SELECT * FROM notifications WHERE customer_id=? ORDER BY rowid",
                    (customer_id,),
                ).fetchall()
            ]
            evidence = [
                row_dict(x)
                for x in self.conn.execute(
                    "SELECT * FROM evidence WHERE customer_id=? ORDER BY id", (customer_id,)
                ).fetchall()
            ]
            audit = [
                row_dict(x)
                for x in self.conn.execute(
                    "SELECT * FROM audit_events WHERE client_id=? OR entity_id IN "
                    "(SELECT id FROM compensation_cases WHERE customer_id=?) "
                    "ORDER BY id DESC LIMIT 100",
                    (client_id, customer_id),
                ).fetchall()
            ]
            return {
                "customer": customer,
                "lines": lines,
                "evidence": evidence,
                "cases": cases,
                "vouchers": vouchers,
                "renewals": renewals,
                "account_ledger": ledger,
                "notifications": notifications,
                "recent_audit_for_caller_client": audit,
                "query_client": client_id,
            }

    def list_notifications(self, customer_id: str, client_id: str) -> dict[str, Any]:
        with self.lock:
            self._get_customer(customer_id)
            rows = self.conn.execute(
                "SELECT * FROM notifications WHERE customer_id=? ORDER BY rowid", (customer_id,)
            ).fetchall()
            return {"notifications": [row_dict(x) for x in rows], "client_id": client_id}

    def trace_evidence(self, evidence_id: str, client_id: str | None = None) -> dict[str, Any]:
        """Answer: this late evidence changed which case version and which bill?"""
        with self.lock:
            evidence = self.conn.execute(
                "SELECT * FROM evidence WHERE id=?", (evidence_id,)
            ).fetchone()
            if evidence is None:
                raise NotFound(f"evidence not found: {evidence_id}")

            versions = []
            for row in self.conn.execute(
                "SELECT * FROM compensation_versions WHERE trigger_evidence_id=? ORDER BY rowid",
                (evidence_id,),
            ).fetchall():
                version = row_dict(row) or {}
                if version.get("calculation_json"):
                    version["calculation"] = json.loads(version.pop("calculation_json"))
                versions.append(version)
            account_rows = self.conn.execute(
                "SELECT * FROM account_ledger WHERE evidence_id=? ORDER BY rowid",
                (evidence_id,),
            ).fetchall()
            account_entries = []
            renewal_ids: list[str] = []
            for row in account_rows:
                entry = row_dict(row) or {}
                if entry.get("detail_json"):
                    entry["detail"] = json.loads(entry.pop("detail_json"))
                account_entries.append(entry)
                if entry.get("renewal_order_id") and entry["renewal_order_id"] not in renewal_ids:
                    renewal_ids.append(entry["renewal_order_id"])

            voucher_entries = [
                row_dict(x)
                for x in self.conn.execute(
                    "SELECT * FROM voucher_ledger WHERE evidence_id=? ORDER BY rowid",
                    (evidence_id,),
                ).fetchall()
            ]
            notifications = [
                row_dict(x)
                for x in self.conn.execute(
                    "SELECT * FROM notifications WHERE evidence_id=? ORDER BY rowid",
                    (evidence_id,),
                ).fetchall()
            ]
            renewals = [
                self._renewal_result(rid, duplicate=False, receipt_redelivered=False)
                for rid in renewal_ids
            ]
            case_ids = sorted({v["case_id"] for v in versions if v.get("case_id")})
            return {
                "evidence": row_dict(evidence),
                "changed_case_ids": case_ids,
                "calculation_versions": versions,
                "voucher_ledger_entries": voucher_entries,
                "account_entries": account_entries,
                "affected_renewal_orders": renewals,
                "notifications": notifications,
                "answer": (
                    "该证据只重算其触发的补偿案件版本；若案件券已兑现，差额通过 "
                    "voucher_redemptions 回到原续期单，不改挂其他账期。"
                ),
            }
