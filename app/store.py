"""Persistence for the rental/rework service.

State is an in-memory document graph that is snapshotted to SQLite after every
mutation, plus an append-only event log used for the measurement chain and for
crash/restart continuity.

The snapshot is the source of truth on load; the event log is retained for
auditing and to rebuild the ordering of work orders / claims / dispositions.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_log (
    seq  INTEGER PRIMARY KEY,
    body TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- snapshot -----------------------------------------------------------
    def load_state(self) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state WHERE key='snapshot'").fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def save_state(self, state: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO state(key,value) VALUES('snapshot',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(state, sort_keys=True),))
            self._conn.commit()

    # -- event log ----------------------------------------------------------
    def next_seq(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM event_log").fetchone()
        return int(row[0])

    def append_event(self, seq: int, body: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO event_log(seq,body) VALUES(?,?)",
                (seq, json.dumps(body, sort_keys=True)))
            self._conn.commit()

    def events(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq,body FROM event_log ORDER BY seq").fetchall()
        return [dict(seq=s, **json.loads(b)) for s, b in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()
