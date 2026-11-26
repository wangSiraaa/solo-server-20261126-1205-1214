"""SQLite persistence for the fictional broadband cooperative."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_via_client TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS access_lines (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    label TEXT NOT NULL CHECK (label IN ('old', 'new', 'other')),
    address TEXT NOT NULL,
    opened_at INTEGER NOT NULL,
    returned_at INTEGER,
    created_via_client TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(customer_id, label)
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    customer_id TEXT REFERENCES customers(id),
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    source_client TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS commitment_intervals (
    id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL REFERENCES access_lines(id),
    start_minute INTEGER NOT NULL,
    end_minute INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('promised', 'test')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded')),
    contract_id TEXT,
    evidence_id TEXT REFERENCES evidence(id),
    superseded_by TEXT,
    created_via_client TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (start_minute <= end_minute)
);

CREATE TABLE IF NOT EXISTS fault_reports (
    report_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    line_id TEXT NOT NULL REFERENCES access_lines(id),
    fault_key TEXT,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER,
    reporter TEXT,
    via_client TEXT NOT NULL,
    duplicate_of_report_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS outages (
    id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL REFERENCES access_lines(id),
    fault_key TEXT,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER,
    status TEXT NOT NULL DEFAULT 'repaired'
        CHECK (status IN ('open', 'repaired', 'void', 'corrected')),
    source_report_id TEXT REFERENCES fault_reports(report_id),
    repair_evidence_id TEXT REFERENCES evidence(id),
    correction_evidence_id TEXT REFERENCES evidence(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(line_id, fault_key)
);

CREATE TABLE IF NOT EXISTS compensation_cases (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    case_key TEXT NOT NULL UNIQUE,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    current_version INTEGER NOT NULL DEFAULT 0,
    created_via_client TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS compensation_versions (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES compensation_cases(id),
    version_no INTEGER NOT NULL,
    expected_minutes INTEGER NOT NULL,
    current_net_minutes INTEGER NOT NULL,
    trigger_evidence_id TEXT REFERENCES evidence(id),
    calculation_json TEXT NOT NULL,
    via_client TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(case_id, version_no)
);

CREATE TABLE IF NOT EXISTS compensation_minute_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL REFERENCES compensation_versions(id),
    minute INTEGER NOT NULL,
    promised_line_ids_json TEXT NOT NULL,
    down_promised_line_ids_json TEXT NOT NULL,
    owed INTEGER NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vouchers (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    case_id TEXT NOT NULL REFERENCES compensation_cases(id),
    origin_version_id TEXT NOT NULL REFERENCES compensation_versions(id),
    code TEXT NOT NULL UNIQUE,
    granted_minutes INTEGER NOT NULL,
    redeemed_minutes INTEGER NOT NULL DEFAULT 0,
    cancelled_minutes INTEGER NOT NULL DEFAULT 0,
    clawed_back_minutes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'available'
        CHECK (status IN ('available', 'partially_redeemed', 'redeemed',
                          'cancelled', 'overissued_settled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS voucher_redemptions (
    id TEXT PRIMARY KEY,
    voucher_id TEXT NOT NULL REFERENCES vouchers(id),
    renewal_order_id TEXT NOT NULL,
    minutes_applied INTEGER NOT NULL,
    minutes_clawed_back INTEGER NOT NULL DEFAULT 0,
    unit_amount_minor INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS voucher_ledger (
    id TEXT PRIMARY KEY,
    voucher_id TEXT NOT NULL REFERENCES vouchers(id),
    entry_type TEXT NOT NULL
        CHECK (entry_type IN ('grant', 'redeem', 'cancel', 'clawback')),
    minutes_delta INTEGER NOT NULL,
    renewal_order_id TEXT,
    evidence_id TEXT REFERENCES evidence(id),
    note TEXT NOT NULL,
    via_client TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS renewal_orders (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    receipt_id TEXT NOT NULL UNIQUE,
    package_code TEXT NOT NULL,
    service_minutes INTEGER NOT NULL,
    rate_minor_per_minute INTEGER NOT NULL,
    list_amount_minor INTEGER NOT NULL,
    voucher_id TEXT REFERENCES vouchers(id),
    voucher_minutes_applied INTEGER NOT NULL DEFAULT 0,
    discount_minor INTEGER NOT NULL DEFAULT 0,
    amount_due_minor INTEGER NOT NULL,
    paid_amount_minor INTEGER NOT NULL DEFAULT 0,
    service_start INTEGER NOT NULL,
    service_end INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted',
    via_client TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS renewal_receipts (
    receipt_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    renewal_order_id TEXT NOT NULL REFERENCES renewal_orders(id),
    delivery_count INTEGER NOT NULL DEFAULT 1,
    first_via_client TEXT NOT NULL,
    last_via_client TEXT NOT NULL,
    first_received_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_received_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS account_ledger (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    renewal_order_id TEXT REFERENCES renewal_orders(id),
    case_id TEXT REFERENCES compensation_cases(id),
    evidence_id TEXT REFERENCES evidence(id),
    kind TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    minutes INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    via_client TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    renewal_order_id TEXT REFERENCES renewal_orders(id),
    case_id TEXT REFERENCES compensation_cases(id),
    evidence_id TEXT REFERENCES evidence(id),
    delivered_by_client TEXT NOT NULL,
    read_flag INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'local-client',
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS idempotent_requests (
    idempotency_key TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL DEFAULT 200,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_lines_customer ON access_lines(customer_id);
CREATE INDEX IF NOT EXISTS idx_commit_line ON commitment_intervals(line_id, status);
CREATE INDEX IF NOT EXISTS idx_outage_line ON outages(line_id, status);
CREATE INDEX IF NOT EXISTS idx_minute_version ON compensation_minute_rows(version_id);
CREATE INDEX IF NOT EXISTS idx_ledger_customer ON account_ledger(customer_id);
CREATE INDEX IF NOT EXISTS idx_notifications_customer ON notifications(customer_id);
"""


def default_db_path() -> str:
    project_root = Path(__file__).resolve().parent.parent
    return os.environ.get("COOP_DB", str(project_root / "demo" / "coop.sqlite3"))


def connect(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    path = Path(db_path or default_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
