"""SQLite schema and connection management.

Design notes
------------
* One file database (SQLite) so process restarts keep all history, which is
  what makes "resend after restart must not create a second final signer"
  enforceable.
* ``signatures.product_id`` is UNIQUE: at the schema level a product can have
  at most one *final* signer no matter how many requests arrive.
* ``signature_requests.request_id`` is UNIQUE: the API is idempotent per
  caller-supplied key across restarts.
* Timestamps are stored as normalised UTC strings (see ``timeutil``) so that
  ordering by text equals ordering by time.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'person'
);

CREATE TABLE IF NOT EXISTS crafts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    min_minutes REAL NOT NULL DEFAULT 0,
    required    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS craft_steps (
    id       TEXT PRIMARY KEY,
    craft_id TEXT NOT NULL,
    name     TEXT NOT NULL,
    seq      INTEGER NOT NULL DEFAULT 0,
    required INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS product_types (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_type_crafts (
    product_type_id TEXT NOT NULL,
    craft_id        TEXT NOT NULL,
    PRIMARY KEY (product_type_id, craft_id)
);

CREATE TABLE IF NOT EXISTS stations (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    sign_authority  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shifts (
    id        TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    post_id   TEXT NOT NULL,
    start_ts  TEXT NOT NULL,
    end_ts    TEXT NOT NULL
);

-- A piece of evidence for someone's craft competence.  Created in the
-- registration book ('registered'), only becomes *evidence* on completion
-- ('completed').  'independent_guidance' records are an alternate mentoring
-- lineage that may be confirmed (verified) later than it was completed.
CREATE TABLE IF NOT EXISTS evidence (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL DEFAULT 'apprenticeship',
    craft_id          TEXT NOT NULL,
    station_id        TEXT,
    start_ts          TEXT NOT NULL,
    end_ts            TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'registered',
    registered_at     TEXT,
    completed_at      TEXT,
    verified_at       TEXT,
    retracted_at      TEXT,
    retraction_reason TEXT,
    note              TEXT
);

CREATE TABLE IF NOT EXISTS evidence_mentors (
    evidence_id TEXT NOT NULL,
    person_id   TEXT NOT NULL,
    PRIMARY KEY (evidence_id, person_id)
);

CREATE TABLE IF NOT EXISTS evidence_students (
    evidence_id TEXT NOT NULL,
    person_id   TEXT NOT NULL,
    PRIMARY KEY (evidence_id, person_id)
);

CREATE TABLE IF NOT EXISTS evidence_steps (
    evidence_id TEXT NOT NULL,
    step_id     TEXT NOT NULL,
    PRIMARY KEY (evidence_id, step_id)
);

-- Optional finer record: which physical mentor actually guided which step.
-- Used by the substitution rule so that a stand-in must have *actually guided
-- a relevant step*, not merely hold a certificate somewhere in the shop.
CREATE TABLE IF NOT EXISTS evidence_mentor_steps (
    evidence_id TEXT NOT NULL,
    person_id   TEXT NOT NULL,
    step_id     TEXT NOT NULL,
    PRIMARY KEY (evidence_id, person_id, step_id)
);

-- Qualifications whose validity is explicit (never inferred from the fact
-- that someone mentored).  capacity: 'production' | 'mentoring'.
-- regime:     'grant' (back-office) | 'periodic' | 'endorsement'.
CREATE TABLE IF NOT EXISTS qualifications (
    id           TEXT PRIMARY KEY,
    person_id    TEXT NOT NULL,
    craft_id     TEXT NOT NULL,
    capacity     TEXT NOT NULL,
    regime       TEXT NOT NULL,
    valid_from   TEXT NOT NULL,
    valid_until  TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    revoked_at   TEXT,
    revoke_reason TEXT,
    source_kind  TEXT,
    source_ref   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id             TEXT PRIMARY KEY,
    product_type_id TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    signed_at      TEXT,
    signer_id      TEXT,
    signature_id   TEXT,
    customer_session TEXT
);

CREATE TABLE IF NOT EXISTS signatures (
    id         TEXT PRIMARY KEY,
    product_id TEXT NOT NULL UNIQUE,
    signer_id  TEXT NOT NULL,
    at_ts      TEXT NOT NULL,
    regime     TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signature_requests (
    request_id  TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Append-only trail.  Corrections are *events*, never deletions: a retracted
-- practicum still leaves its original completion event and its retraction
-- event, so a correction can never be dressed up as "never happened".
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ts       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS periods (
    id            TEXT PRIMARY KEY,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    review_policy TEXT NOT NULL DEFAULT 'honor_through_period',
    opened_at     TEXT,
    closed_at     TEXT,
    status        TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS certificates (
    id            TEXT PRIMARY KEY,
    period_id     TEXT NOT NULL,
    person_id     TEXT NOT NULL,
    craft_id      TEXT NOT NULL,
    capacity      TEXT NOT NULL DEFAULT 'production',
    valid_from    TEXT NOT NULL,
    valid_until   TEXT NOT NULL,
    basis_json    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    review_decision TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qual_person ON qualifications(person_id, craft_id, capacity);
CREATE INDEX IF NOT EXISTS idx_ev_craft ON evidence(craft_id, status);
CREATE INDEX IF NOT EXISTS idx_ev_student ON evidence_students(person_id);
CREATE INDEX IF NOT EXISTS idx_ev_mentor ON evidence_mentors(person_id);
CREATE INDEX IF NOT EXISTS idx_shift_person ON shifts(person_id, start_ts);
CREATE INDEX IF NOT EXISTS idx_cert_person ON certificates(person_id, craft_id);
"""


class Database:
    """Thin wrapper giving out short-lived connections and a write lock.

    SQLite is fine for this workload.  Writes are serialised through an
    in-process lock and use ``BEGIN IMMEDIATE`` so that two concurrent signing
    requests cannot both observe a product as 'pending' and both commit.
    """

    def __init__(self, path: str):
        self.path = path
        self._write_lock = threading.RLock()
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init_schema(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    @property
    def write_lock(self) -> threading.RLock:
        return self._write_lock

    def get_setting(self, key: str, default=None):
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self):
        """Serialised write transaction.

        ``BEGIN IMMEDIATE`` grabs the SQLite write lock up front so that
        concurrent signers cannot both read a product as pending.
        """
        with self.write_lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @contextlib.contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def set_setting(self, key: str, value: str) -> None:
        with self.write_lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
