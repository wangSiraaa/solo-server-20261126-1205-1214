from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    window_mode TEXT NOT NULL CHECK (window_mode IN ('sealed', 'revisit')),
    complete_pairing INTEGER NOT NULL DEFAULT 0,
    allow_unfound INTEGER NOT NULL DEFAULT 1,
    feature_schema TEXT NOT NULL DEFAULT '{}',
    inspection_budget INTEGER NOT NULL DEFAULT 2,
    willing_round_default INTEGER NOT NULL DEFAULT 1,
    prepare_budget INTEGER NOT NULL DEFAULT 4,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registration_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    complete_pairing INTEGER NOT NULL,
    allow_unfound INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    item_no TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_stock'
        CHECK (status IN ('in_stock', 'reserved', 'out', 'unclaimed')),
    appearance TEXT NOT NULL DEFAULT '{}',
    disposition_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_features (
    item_no TEXT NOT NULL REFERENCES items(item_no),
    feature_key TEXT NOT NULL,
    actual_value TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '[]',
    revealed INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (item_no, feature_key)
);

CREATE TABLE IF NOT EXISTS claims (
    claim_no TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    claimant TEXT NOT NULL,
    appearance TEXT NOT NULL DEFAULT '{}',
    memory TEXT NOT NULL DEFAULT '{}',
    willing_rounds INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'filed'
        CHECK (status IN ('filed', 'approved', 'out', 'no_match', 'cancelled')),
    disposition_note TEXT NOT NULL DEFAULT '',
    statement_hash TEXT NOT NULL DEFAULT '',
    sealed_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_answers (
    claim_no TEXT NOT NULL REFERENCES claims(claim_no),
    round_no INTEGER NOT NULL,
    feature_key TEXT NOT NULL,
    question_snapshot TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '[]',
    answer_value TEXT NOT NULL,
    window_id TEXT NOT NULL DEFAULT '',
    shift_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (claim_no, round_no)
);

CREATE TABLE IF NOT EXISTS inspections (
    inspection_no TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    item_no TEXT NOT NULL REFERENCES items(item_no),
    results TEXT NOT NULL DEFAULT '{}',
    inspector TEXT NOT NULL DEFAULT '',
    window_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shifts (
    shift_id TEXT PRIMARY KEY,
    prepare_budget INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shift_preparations (
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    item_no TEXT NOT NULL REFERENCES items(item_no),
    prepared_at TEXT NOT NULL,
    PRIMARY KEY (shift_id, item_no)
);

CREATE TABLE IF NOT EXISTS confirmed_pairs (
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    claim_no TEXT NOT NULL REFERENCES claims(claim_no),
    item_no TEXT NOT NULL REFERENCES items(item_no),
    external_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (claim_no),
    UNIQUE (item_no)
);

CREATE TABLE IF NOT EXISTS vouchers (
    voucher_no TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    claim_no TEXT NOT NULL REFERENCES claims(claim_no),
    item_no TEXT NOT NULL REFERENCES items(item_no),
    status TEXT NOT NULL DEFAULT 'signed'
        CHECK (status IN ('signed', 'out', 'cancelled')),
    sign_window_id TEXT NOT NULL DEFAULT '',
    signed_at TEXT NOT NULL,
    outbound_window_id TEXT NOT NULL DEFAULT '',
    outbound_at TEXT,
    request_id TEXT NOT NULL DEFAULT ''
);

-- A cancelled voucher leaves history but releases its claim/item for a new one.
CREATE UNIQUE INDEX IF NOT EXISTS vouchers_one_active_claim
    ON vouchers(claim_no) WHERE status IN ('signed', 'out');
CREATE UNIQUE INDEX IF NOT EXISTS vouchers_one_active_item
    ON vouchers(item_no) WHERE status IN ('signed', 'out');

CREATE TABLE IF NOT EXISTS handoffs (
    voucher_no TEXT PRIMARY KEY REFERENCES vouchers(voucher_no),
    item_no TEXT NOT NULL UNIQUE REFERENCES items(item_no),
    window_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    handed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no TEXT NOT NULL,
    window_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    next_value INTEGER NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection. Callers close it; transactions are explicit."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def next_number(conn: sqlite3.Connection, counter_name: str, prefix: str) -> str:
    """Atomically allocate a restart-safe human-readable number."""
    row = conn.execute(
        """
        INSERT INTO counters(name, next_value) VALUES (?, 1)
        ON CONFLICT(name) DO UPDATE SET next_value = counters.next_value + 1
        RETURNING next_value
        """,
        (counter_name,),
    ).fetchone()
    return f"{prefix}-{int(row['next_value']):04d}"


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
