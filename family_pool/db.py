from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zones (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sort_no INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ports (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    from_zone_id TEXT NOT NULL REFERENCES zones(id),
    to_zone_id TEXT NOT NULL REFERENCES zones(id),
    bidirectional INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1
);

-- Visibility is deliberately separate from ports/adjacency.
CREATE TABLE IF NOT EXISTS visibility_edges (
    zone_a TEXT NOT NULL REFERENCES zones(id),
    zone_b TEXT NOT NULL REFERENCES zones(id),
    PRIMARY KEY (zone_a, zone_b),
    CHECK (zone_a < zone_b)
);

CREATE TABLE IF NOT EXISTS families (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS family_consents (
    family_id TEXT NOT NULL REFERENCES families(id),
    other_family_id TEXT NOT NULL REFERENCES families(id),
    takeover_allowed INTEGER NOT NULL DEFAULT 0,
    borrow_allowed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (family_id, other_family_id)
);

CREATE TABLE IF NOT EXISTS persons (
    id TEXT PRIMARY KEY,
    band_code TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('adult', 'child')),
    family_id TEXT NOT NULL REFERENCES families(id),
    care_capacity INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    operating_mode TEXT NOT NULL DEFAULT 'hybrid'
        CHECK (operating_mode IN ('prearranged', 'onsite', 'hybrid')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closed')),
    starts_at TEXT,
    ends_at TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS aid_groups (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aid_group_families (
    group_id TEXT NOT NULL REFERENCES aid_groups(id),
    family_id TEXT NOT NULL REFERENCES families(id),
    PRIMARY KEY (group_id, family_id)
);

CREATE TABLE IF NOT EXISTS plan_segments (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    family_id TEXT NOT NULL REFERENCES families(id),
    child_id TEXT NOT NULL REFERENCES persons(id),
    activity TEXT NOT NULL,
    zone_id TEXT NOT NULL REFERENCES zones(id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    primary_adult_id TEXT NOT NULL REFERENCES persons(id),
    backup_adult_id TEXT NOT NULL REFERENCES persons(id),
    window_starts_at TEXT NOT NULL,
    window_ends_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    wristband_id TEXT NOT NULL REFERENCES persons(id),
    session_id TEXT REFERENCES sessions(id),
    location_kind TEXT NOT NULL CHECK (location_kind IN ('inside', 'outside')),
    zone_id TEXT REFERENCES zones(id),
    source TEXT NOT NULL CHECK (
        source IN ('gate_scan', 'port_scan', 'correction', 'seed')
    ),
    event_id TEXT,
    corrects_receipt_id TEXT,
    staff_id TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipt_band ON receipts(wristband_id, seq DESC);

CREATE TABLE IF NOT EXISTS crossing_events (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id),
    at TEXT NOT NULL,
    wristband_id TEXT NOT NULL REFERENCES persons(id),
    kind TEXT NOT NULL CHECK (
        kind IN ('gate_entry', 'gate_exit', 'port_cross', 'receipt_correction')
    ),
    port_id TEXT REFERENCES ports(id),
    from_zone_id TEXT REFERENCES zones(id),
    to_zone_id TEXT REFERENCES zones(id),
    receipt_id TEXT,
    permit_id TEXT,
    staff_id TEXT,
    idempotency_key TEXT UNIQUE,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_session ON crossing_events(session_id, at);
CREATE INDEX IF NOT EXISTS idx_events_band ON crossing_events(wristband_id, at);

CREATE TABLE IF NOT EXISTS slot_allocations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    adult_id TEXT NOT NULL REFERENCES persons(id),
    slot_no INTEGER NOT NULL,
    child_id TEXT NOT NULL REFERENCES persons(id),
    purpose TEXT NOT NULL CHECK (
        purpose IN ('own_care', 'plan', 'handover', 'cross_family_loan')
    ),
    ref_type TEXT NOT NULL DEFAULT '',
    ref_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'consumed', 'released')),
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_physical_slot
    ON slot_allocations(session_id, adult_id, slot_no)
    WHERE status IN ('reserved', 'consumed');
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_adult_child
    ON slot_allocations(session_id, adult_id, child_id)
    WHERE status IN ('reserved', 'consumed');

CREATE TABLE IF NOT EXISTS care_assignments (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    child_id TEXT NOT NULL REFERENCES persons(id),
    adult_id TEXT NOT NULL REFERENCES persons(id),
    kind TEXT NOT NULL CHECK (kind IN ('primary', 'backup', 'joint', 'actual')),
    source TEXT NOT NULL CHECK (source IN ('plan', 'onsite', 'direct', 'loan')),
    segment_id TEXT,
    handover_id TEXT,
    slot_allocation_id TEXT REFERENCES slot_allocations(id),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('pending', 'active', 'ended', 'revoked')),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_care_child_active
    ON care_assignments(child_id, status);

CREATE TABLE IF NOT EXISTS handovers (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    aid_group_id TEXT REFERENCES aid_groups(id),
    departing_adult_id TEXT NOT NULL REFERENCES persons(id),
    receiving_adult_id TEXT NOT NULL REFERENCES persons(id),
    source TEXT NOT NULL CHECK (source IN ('plan', 'onsite')),
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN (
            'proposed', 'confirmed', 'prepared',
            'effective', 'blocked', 'cancelled'
        )),
    segment_id TEXT,
    receiver_confirmed_at TEXT,
    prepared_at TEXT,
    effective_at TEXT,
    permit_id TEXT,
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS handover_children (
    handover_id TEXT NOT NULL REFERENCES handovers(id),
    child_id TEXT NOT NULL REFERENCES persons(id),
    slot_allocation_id TEXT REFERENCES slot_allocations(id),
    PRIMARY KEY (handover_id, child_id)
);

CREATE TABLE IF NOT EXISTS permits (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    wristband_id TEXT NOT NULL REFERENCES persons(id),
    permit_type TEXT NOT NULL DEFAULT 'gate_exit'
        CHECK (permit_type IN ('gate_exit', 'zone_exit', 'child_pickup')),
    status TEXT NOT NULL DEFAULT 'issued'
        CHECK (status IN ('issued', 'consumed', 'denied', 'cancelled')),
    handover_id TEXT REFERENCES handovers(id),
    escort_adult_id TEXT REFERENCES persons(id),
    pickup_name TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL,
    consumed_at TEXT,
    crossing_event_id TEXT,
    idempotency_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS permit_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    permit_id TEXT NOT NULL REFERENCES permits(id),
    at TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    snapshot_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS companion_time_accounts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    family_id TEXT NOT NULL REFERENCES families(id),
    adult_id TEXT NOT NULL REFERENCES persons(id),
    segment_id TEXT REFERENCES plan_segments(id),
    label TEXT NOT NULL,
    original_minutes INTEGER NOT NULL DEFAULT 0,
    sellable_units INTEGER NOT NULL DEFAULT 0,
    units_remaining INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS time_loan_requests (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    account_id TEXT NOT NULL REFERENCES companion_time_accounts(id),
    from_family_id TEXT NOT NULL REFERENCES families(id),
    to_family_id TEXT NOT NULL REFERENCES families(id),
    child_id TEXT NOT NULL REFERENCES persons(id),
    fulfilling_adult_id TEXT REFERENCES persons(id),
    units INTEGER NOT NULL DEFAULT 1,
    starts_at TEXT,
    ends_at TEXT,
    status TEXT NOT NULL DEFAULT 'applied'
        CHECK (status IN (
            'applied', 'approved_sold', 'rejected',
            'fulfilled', 'cancelled'
        )),
    fulfillment_state TEXT NOT NULL DEFAULT 'awaiting_decision'
        CHECK (fulfillment_state IN (
            'awaiting_decision', 'awaiting_present_adult',
            'ready', 'fulfilled', 'not_fulfillable'
        )),
    slot_allocation_id TEXT REFERENCES slot_allocations(id),
    decision_reason TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    fulfilled_at TEXT
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    session_id TEXT,
    entry_type TEXT NOT NULL,
    ref_type TEXT NOT NULL DEFAULT '',
    ref_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ledger_session ON ledger_entries(session_id, seq);
"""


def connect(db_path: Union[str, Path] = ":memory:") -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
    )
    return conn
