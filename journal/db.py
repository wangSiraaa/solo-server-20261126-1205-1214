"""SQLite persistence for the translation-journal crowdfunding service.

The whole point of this file is that everything survives a process restart:
issues, pieces, translators' slot calendars, pledges, the global slot
bookings, and every issued receipt live in one SQLite file.  A second
``JournalService`` opened on the same path sees exactly the same data.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    mode        TEXT NOT NULL CHECK (mode IN ('joint', 'rolling')),
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pieces (
    issue_id        TEXT NOT NULL,
    piece_id        TEXT NOT NULL,
    title           TEXT,
    points_required INTEGER NOT NULL CHECK (points_required >= 0),
    duration        INTEGER NOT NULL DEFAULT 1 CHECK (duration >= 1),
    wish_points     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (issue_id, piece_id)
);

CREATE TABLE IF NOT EXISTS translators (
    id   TEXT PRIMARY KEY,
    name TEXT
);

CREATE TABLE IF NOT EXISTS translator_slots (
    translator_id TEXT NOT NULL,
    ord           INTEGER NOT NULL,
    label         TEXT NOT NULL,
    PRIMARY KEY (translator_id, ord),
    UNIQUE (translator_id, label)
);

CREATE TABLE IF NOT EXISTS piece_translators (
    issue_id      TEXT NOT NULL,
    piece_id      TEXT NOT NULL,
    translator_id TEXT NOT NULL,
    PRIMARY KEY (issue_id, piece_id, translator_id)
);

-- A slot (translator, label) can be committed at most once, across all
-- issues.  This is what makes "the only translator's Tuesday is free"
-- a globally scarce resource.
CREATE TABLE IF NOT EXISTS slot_bookings (
    translator_id TEXT NOT NULL,
    label         TEXT NOT NULL,
    issue_id      TEXT NOT NULL,
    piece_id      TEXT NOT NULL,
    receipt_id    TEXT NOT NULL,
    PRIMARY KEY (translator_id, label)
);

CREATE TABLE IF NOT EXISTS pledges (
    issue_id    TEXT NOT NULL,
    pledge_id   TEXT NOT NULL,
    supporter   TEXT NOT NULL,
    points      INTEGER NOT NULL CHECK (points >= 0),
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (issue_id, pledge_id)
);

CREATE TABLE IF NOT EXISTS pledge_accepts (
    issue_id  TEXT NOT NULL,
    pledge_id TEXT NOT NULL,
    piece_id  TEXT NOT NULL,
    PRIMARY KEY (issue_id, pledge_id, piece_id)
);

CREATE TABLE IF NOT EXISTS pledge_requires (
    issue_id  TEXT NOT NULL,
    pledge_id TEXT NOT NULL,
    piece_id  TEXT NOT NULL,
    PRIMARY KEY (issue_id, pledge_id, piece_id)
);

-- The supporter's own "per issue I will pay at most N points" ceiling.
-- It is a ceiling over the *whole* issue, never a per-piece allowance.
CREATE TABLE IF NOT EXISTS supporter_caps (
    issue_id  TEXT NOT NULL,
    supporter TEXT NOT NULL,
    cap       INTEGER NOT NULL CHECK (cap >= 0),
    PRIMARY KEY (issue_id, supporter)
);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id  TEXT PRIMARY KEY,
    issue_id    TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Idempotency ledger: one request id can only ever produce one receipt.
CREATE TABLE IF NOT EXISTS request_log (
    request_id TEXT PRIMARY KEY,
    issue_id   TEXT NOT NULL,
    action     TEXT NOT NULL,
    receipt_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deductions (
    receipt_id TEXT NOT NULL,
    issue_id   TEXT NOT NULL,
    pledge_id  TEXT NOT NULL,
    supporter  TEXT NOT NULL,
    piece_id   TEXT NOT NULL,
    points     INTEGER NOT NULL CHECK (points >= 0),
    PRIMARY KEY (receipt_id, pledge_id, piece_id)
);

CREATE TABLE IF NOT EXISTS assignments (
    receipt_id    TEXT NOT NULL,
    issue_id      TEXT NOT NULL,
    piece_id      TEXT NOT NULL,
    translator_id TEXT NOT NULL,
    slots         TEXT NOT NULL,
    PRIMARY KEY (receipt_id, piece_id)
);

CREATE TABLE IF NOT EXISTS opened_pieces (
    issue_id  TEXT NOT NULL,
    piece_id  TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    PRIMARY KEY (issue_id, piece_id)
);
"""


class Database:
    """Thin owner of a SQLite path; hands out short-lived connections."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        if self.path != ":memory:":
            parent = Path(self.path).expanduser().resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
        self._shared = None
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        if self.path == ":memory:":
            # An in-memory DB has to be shared across connections to survive.
            self._shared = sqlite3.connect(
                self.path, check_same_thread=False, isolation_level=None
            )
            self._shared.row_factory = sqlite3.Row
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        # One connection per thread: sqlite connections are not shareable
        # across threads, and the threaded HTTP server hits us concurrently.
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None,
                                   check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            self._local.conn = conn
            with self._lock:
                self._all_conns.append(conn)
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)
        conn.commit()

    @contextmanager
    def transaction(self):
        """A write transaction.  ``BEGIN IMMEDIATE`` serialises openings."""
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
            self._shared = None
        with self._lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()
        self._local = threading.local()
