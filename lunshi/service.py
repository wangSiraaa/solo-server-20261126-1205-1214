"""轮市 domain service: SQLite persistence, rotation ledger, permits and receipts.

Only Python standard library is used.  The important invariant is:

* actual open receipts decide whether a round is complete;
* one convention/round has at most one effective ledger usage;
* land loans and transfers never, by themselves, consume a round;
* duplicate receipt numbers are idempotent.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def canonical_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def loads(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def serialize(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    for key in list(data.keys()):
        if key.endswith("_json"):
            short = key[:-5]
            data[short] = loads(data[key], None)
    return data


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS townships (
  id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conventions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  method TEXT NOT NULL,
  cycle_json TEXT NOT NULL,
  first_township_id TEXT NOT NULL,
  success_rule_json TEXT NOT NULL,
  makeup_policy TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  township_id TEXT NOT NULL REFERENCES townships(id),
  day_no INTEGER NOT NULL,
  slot_no INTEGER NOT NULL DEFAULT 1,
  iso_date TEXT,
  title TEXT,
  status TEXT NOT NULL DEFAULT 'scheduled',
  note TEXT,
  UNIQUE(township_id, day_no, slot_no)
);

CREATE TABLE IF NOT EXISTS spots (
  id TEXT PRIMARY KEY,
  township_id TEXT NOT NULL REFERENCES townships(id),
  code TEXT NOT NULL,
  label TEXT,
  allowed_categories_json TEXT NOT NULL DEFAULT '[]',
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE(township_id, code)
);

CREATE TABLE IF NOT EXISTS spot_links (
  spot_a TEXT NOT NULL REFERENCES spots(id),
  spot_b TEXT NOT NULL REFERENCES spots(id),
  adjacent INTEGER NOT NULL DEFAULT 1,
  barrier TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(spot_a, spot_b),
  CHECK(spot_a < spot_b)
);

CREATE TABLE IF NOT EXISTS vendors (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  contact TEXT,
  categories_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vendor_registrations (
  vendor_id TEXT NOT NULL REFERENCES vendors(id),
  township_id TEXT NOT NULL REFERENCES townships(id),
  categories_json TEXT NOT NULL DEFAULT '[]',
  registered_at TEXT NOT NULL,
  PRIMARY KEY(vendor_id, township_id)
);

CREATE TABLE IF NOT EXISTS booking_groups (
  id TEXT PRIMARY KEY,
  convention_id TEXT REFERENCES conventions(id),
  group_type TEXT NOT NULL,
  township_id TEXT NOT NULL,
  day_no INTEGER NOT NULL,
  slot_no INTEGER NOT NULL,
  require_adjacent INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE,
  group_id TEXT REFERENCES booking_groups(id),
  vendor_id TEXT NOT NULL REFERENCES vendors(id),
  convention_id TEXT REFERENCES conventions(id),
  township_id TEXT NOT NULL,
  day_no INTEGER NOT NULL,
  slot_no INTEGER NOT NULL,
  category TEXT NOT NULL,
  stall_name TEXT,
  status TEXT NOT NULL,
  spot_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permits (
  id TEXT PRIMARY KEY,
  permit_no TEXT NOT NULL UNIQUE,
  idempotency_key TEXT UNIQUE,
  application_id TEXT NOT NULL,
  group_id TEXT,
  convention_id TEXT,
  host_township_id TEXT NOT NULL,
  spot_township_id TEXT NOT NULL,
  spot_id TEXT NOT NULL,
  day_no INTEGER NOT NULL,
  slot_no INTEGER NOT NULL,
  round_no INTEGER,
  turn_call_id TEXT,
  status TEXT NOT NULL,
  placement_text TEXT,
  neighbors_json TEXT NOT NULL DEFAULT '[]',
  source_permit_id TEXT,
  issued_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS permits_one_active_app
  ON permits(application_id) WHERE status = 'valid';

CREATE TABLE IF NOT EXISTS allocations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  permit_id TEXT NOT NULL REFERENCES permits(id),
  spot_id TEXT NOT NULL,
  day_no INTEGER NOT NULL,
  slot_no INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS allocations_one_active_spot
  ON allocations(day_no, slot_no, spot_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS receipts (
  receipt_no TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE,
  request_hash TEXT,
  permit_id TEXT,
  application_id TEXT,
  vendor_id TEXT NOT NULL REFERENCES vendors(id),
  convention_id TEXT,
  turn_call_id TEXT,
  round_no INTEGER,
  host_township_id TEXT NOT NULL,
  spot_township_id TEXT NOT NULL,
  spot_id TEXT NOT NULL,
  day_no INTEGER NOT NULL,
  slot_no INTEGER NOT NULL,
  iso_date TEXT,
  category TEXT NOT NULL,
  stall_name TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  reported_at TEXT NOT NULL,
  withdrawn_at TEXT,
  withdrawal_reason TEXT,
  restored_at TEXT,
  source_receipt_no TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS receipts_one_live_place
  ON receipts(vendor_id, day_no, slot_no) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS receipt_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_no TEXT NOT NULL,
  event_type TEXT NOT NULL,
  detail_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turn_calls (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE,
  convention_id TEXT NOT NULL REFERENCES conventions(id),
  round_no INTEGER NOT NULL,
  township_id TEXT NOT NULL REFERENCES townships(id),
  kind TEXT NOT NULL DEFAULT 'regular',
  day_no INTEGER NOT NULL,
  slot_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'open',
  reason TEXT,
  created_at TEXT NOT NULL,
  settled_at TEXT
);
CREATE INDEX IF NOT EXISTS turn_calls_round_idx
  ON turn_calls(convention_id, round_no);

CREATE TABLE IF NOT EXISTS owed_rounds (
  id TEXT PRIMARY KEY,
  convention_id TEXT NOT NULL REFERENCES conventions(id),
  round_no INTEGER NOT NULL,
  township_id TEXT NOT NULL REFERENCES townships(id),
  source_call_id TEXT,
  status TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(convention_id, round_no)
);

CREATE TABLE IF NOT EXISTS round_usage (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE,
  convention_id TEXT NOT NULL REFERENCES conventions(id),
  round_no INTEGER NOT NULL,
  township_id TEXT NOT NULL REFERENCES townships(id),
  call_id TEXT,
  success_day_no INTEGER NOT NULL,
  success_slot_no INTEGER NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  reverses_id TEXT,
  suspended_reason TEXT,
  reversal_reason TEXT,
  created_at TEXT NOT NULL,
  reversed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS round_usage_one_effective
  ON round_usage(convention_id, round_no) WHERE status = 'effective';

CREATE TABLE IF NOT EXISTS round_usage_receipts (
  usage_id TEXT NOT NULL REFERENCES round_usage(id),
  receipt_no TEXT NOT NULL REFERENCES receipts(receipt_no),
  PRIMARY KEY(usage_id, receipt_no)
);

CREATE TABLE IF NOT EXISTS venue_loans (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE,
  spot_id TEXT NOT NULL REFERENCES spots(id),
  lender_township_id TEXT NOT NULL,
  borrower_township_id TEXT NOT NULL,
  day_no INTEGER NOT NULL,
  slot_no INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'promised',
  purpose TEXT,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS venue_loans_no_double_promise
  ON venue_loans(spot_id, day_no, slot_no)
  WHERE status IN ('promised', 'active');

CREATE TABLE IF NOT EXISTS transfer_orders (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE,
  application_id TEXT NOT NULL,
  old_permit_id TEXT,
  new_permit_id TEXT,
  receipt_no TEXT,
  loan_id TEXT,
  host_township_id TEXT NOT NULL,
  round_no INTEGER,
  from_json TEXT NOT NULL,
  to_json TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor_role TEXT NOT NULL,
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
"""


class LunshiService:
    def __init__(self, db_path: str = "data/lunshi.sqlite3"):
        self.db_path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(parent, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def db(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.db() as conn:
            conn.executescript(SCHEMA)

    # ---------- low level helpers ----------

    def _audit(
        self,
        conn: sqlite3.Connection,
        actor_role: str,
        action: str,
        entity_type: str,
        entity_id: str | None,
        payload: dict | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO audit_events(at, actor_role, action, entity_type, entity_id, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (now_iso(), actor_role, action, entity_type, entity_id, json.dumps(payload or {}, ensure_ascii=False)),
        )

    @staticmethod
    def _get_or_404(conn: sqlite3.Connection, table: str, entity_id: str, column: str = "id") -> sqlite3.Row:
        row = conn.execute(f"SELECT * FROM {table} WHERE {column} = ?", (entity_id,)).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"{table}.{entity_id} 不存在")
        return row

    def set_meta(self, key: str, value: str) -> None:
        with self.db() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.db() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_today(self, day_no: int) -> dict:
        self.set_meta("today_day_no", str(day_no))
        return {"today_day_no": day_no}

    # ---------- committee declarations ----------

    def create_township(self, township_id: str, name: str, seq: int) -> dict:
        with self.db() as conn:
            try:
                conn.execute(
                    "INSERT INTO townships(id,seq,name,created_at) VALUES(?,?,?,?)",
                    (township_id, seq, name, now_iso()),
                )
                self._audit(conn, "committee", "township.create", "township", township_id, {"name": name, "seq": seq})
                row = conn.execute("SELECT * FROM townships WHERE id=?", (township_id,)).fetchone()
                conn.commit()
                return serialize(row)
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                existing = conn.execute("SELECT * FROM townships WHERE id=? OR seq=?", (township_id, seq)).fetchone()
                if existing:
                    return {**serialize(existing), "duplicate": True}
                raise ApiError(409, "TOWNSHIP_CONFLICT", "乡编号或轮次序号已存在") from exc

    def list_townships(self) -> list[dict]:
        with self.db() as conn:
            rows = conn.execute("SELECT * FROM townships ORDER BY seq").fetchall()
            return [serialize(r) for r in rows]

    def declare_session(
        self,
        township_id: str,
        day_no: int,
        slot_no: int = 1,
        iso_date: str | None = None,
        title: str | None = None,
        note: str | None = None,
    ) -> dict:
        with self.db() as conn:
            self._get_or_404(conn, "townships", township_id)
            conn.execute(
                """INSERT INTO sessions(township_id,day_no,slot_no,iso_date,title,note,status)
                   VALUES(?,?,?,?,?,?,'scheduled')
                   ON CONFLICT(township_id,day_no,slot_no) DO UPDATE SET
                     iso_date=COALESCE(excluded.iso_date,sessions.iso_date),
                     title=COALESCE(excluded.title,sessions.title),
                     note=COALESCE(excluded.note,sessions.note)""",
                (township_id, day_no, slot_no, iso_date, title, note),
            )
            self._audit(
                conn, "committee", "session.declare", "session", f"{township_id}-{day_no}-{slot_no}",
                {"township_id": township_id, "day_no": day_no, "slot_no": slot_no,
                 "iso_date": iso_date, "title": title, "note": note},
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM sessions WHERE township_id=? AND day_no=? AND slot_no=?",
                (township_id, day_no, slot_no),
            ).fetchone()
            return serialize(row)

    def set_session_status(self, session_id: int, status: str, note: str | None = None) -> dict:
        with self.db() as conn:
            row = self._get_or_404(conn, "sessions", str(session_id)) if False else conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise ApiError(404, "SESSION_NOT_FOUND", f"集期 {session_id} 不存在")
            conn.execute("UPDATE sessions SET status=?, note=COALESCE(?,note) WHERE id=?", (status, note, session_id))
            self._audit(conn, "committee", "session.status", "session", str(session_id), {"status": status, "note": note})
            conn.commit()
            return serialize(conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def add_spot(
        self, spot_id: str, township_id: str, code: str, allowed_categories: Iterable[str], label: str | None = None
    ) -> dict:
        cats = list(allowed_categories)
        with self.db() as conn:
            self._get_or_404(conn, "townships", township_id)
            conn.execute(
                """INSERT INTO spots(id,township_id,code,label,allowed_categories_json,active)
                   VALUES(?,?,?,?,?,1)
                   ON CONFLICT(id) DO UPDATE SET
                     code=excluded.code, label=excluded.label,
                     allowed_categories_json=excluded.allowed_categories_json, active=1""",
                (spot_id, township_id, code, label, json.dumps(cats, ensure_ascii=False)),
            )
            self._audit(conn, "committee", "spot.upsert", "spot", spot_id, {"categories": cats})
            conn.commit()
            return serialize(conn.execute("SELECT * FROM spots WHERE id=?", (spot_id,)).fetchone())

    def set_spots_linked(self, spot_a: str, spot_b: str, adjacent: bool = True, barrier: str | None = None) -> dict:
        a, b = canonical_pair(spot_a, spot_b)
        with self.db() as conn:
            ra = self._get_or_404(conn, "spots", a)
            rb = self._get_or_404(conn, "spots", b)
            if ra["township_id"] != rb["township_id"]:
                raise ApiError(400, "SPOT_TOWNSHIP_MISMATCH", "相邻格必须属于同一乡；借地请走 venue loan")
            conn.execute(
                """INSERT INTO spot_links(spot_a,spot_b,adjacent,barrier,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(spot_a,spot_b) DO UPDATE SET
                     adjacent=excluded.adjacent, barrier=excluded.barrier, updated_at=excluded.updated_at""",
                (a, b, 1 if adjacent else 0, barrier, now_iso()),
            )
            action = "spot.connect" if adjacent else "spot.separate"
            self._audit(conn, "committee", action, "spot_link", f"{a}|{b}", {"barrier": barrier})
            conn.commit()
            return {"spots": [a, b], "adjacent": adjacent, "barrier": barrier}

    def list_spots(self, township_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM spots"
        args: tuple = ()
        if township_id:
            q += " WHERE township_id=?"
            args = (township_id,)
        q += " ORDER BY id"
        with self.db() as conn:
            return [serialize(r) for r in conn.execute(q, args).fetchall()]

    # ---------- convention ----------

    def open_convention(
        self,
        convention_id: str,
        name: str,
        cycle: list[str] | None = None,
        success_rule: dict | None = None,
        method: str = "dynamic_call",
        makeup_policy: str = "same_round_priority_retry",
    ) -> dict:
        cycle = cycle or ["A", "B", "C"]
        rule = success_rule or {
            "same_day": True,
            "same_slot": False,
            "distinct_vendors": True,
            "required_categories": ["book", "umbrella"],
        }
        with self.db() as conn:
            for tid in cycle:
                self._get_or_404(conn, "townships", tid)
            conn.execute(
                """INSERT INTO conventions(id,name,method,cycle_json,first_township_id,
                     success_rule_json,makeup_policy,created_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name""",
                (
                    convention_id,
                    name,
                    method,
                    json.dumps(cycle, ensure_ascii=False),
                    cycle[0],
                    json.dumps(rule, ensure_ascii=False),
                    makeup_policy,
                    now_iso(),
                ),
            )
            self._audit(conn, "committee", "convention.open", "convention", convention_id, {"cycle": cycle, "rule": rule})
            conn.commit()
            return serialize(conn.execute("SELECT * FROM conventions WHERE id=?", (convention_id,)).fetchone())

    def list_conventions(self) -> list[dict]:
        with self.db() as conn:
            return [serialize(r) for r in conn.execute("SELECT * FROM conventions ORDER BY created_at").fetchall()]

    # ---------- vendors ----------

    def register_vendor(
        self,
        vendor_id: str,
        name: str,
        categories: list[str],
        township_ids: list[str] | None = None,
        contact: str | None = None,
    ) -> dict:
        with self.db() as conn:
            conn.execute(
                """INSERT INTO vendors(id,name,contact,categories_json,created_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, contact=excluded.contact,
                     categories_json=excluded.categories_json""",
                (vendor_id, name, contact, json.dumps(categories, ensure_ascii=False), now_iso()),
            )
            for tid in township_ids or []:
                self._get_or_404(conn, "townships", tid)
                conn.execute(
                    """INSERT INTO vendor_registrations(vendor_id,township_id,categories_json,registered_at)
                       VALUES(?,?,?,?)
                       ON CONFLICT(vendor_id,township_id) DO NOTHING""",
                    (vendor_id, tid, json.dumps(categories, ensure_ascii=False), now_iso()),
                )
            self._audit(conn, "vendor", "vendor.register", "vendor", vendor_id, {"townships": township_ids or []})
            conn.commit()
            return self.vendor_dossier(vendor_id)

    def vendor_dossier(self, vendor_id: str) -> dict:
        with self.db() as conn:
            vendor = serialize(self._get_or_404(conn, "vendors", vendor_id))
            regs = [serialize(r) for r in conn.execute(
                "SELECT * FROM vendor_registrations WHERE vendor_id=? ORDER BY township_id", (vendor_id,)
            ).fetchall()]
            apps = [serialize(r) for r in conn.execute(
                "SELECT * FROM applications WHERE vendor_id=? ORDER BY day_no, slot_no", (vendor_id,)
            ).fetchall()]
            permits = [serialize(r) for r in conn.execute(
                "SELECT * FROM permits WHERE application_id IN (SELECT id FROM applications WHERE vendor_id=?) ORDER BY day_no",
                (vendor_id,),
            ).fetchall()]
            receipts = [serialize(r) for r in conn.execute(
                "SELECT * FROM receipts WHERE vendor_id=? ORDER BY day_no, slot_no", (vendor_id,)
            ).fetchall()]
            for p in permits:
                p["neighbors"] = loads(p.pop("neighbors_json", "[]"), []) if "neighbors_json" in p else p.get("neighbors", [])
            return {"vendor": vendor, "registrations": regs, "applications": apps, "permits": permits, "receipts": receipts}

    # ---------- applications and adjacent pairs ----------

    def _submit_application(
        self,
        conn: sqlite3.Connection,
        application_id: str,
        vendor_id: str,
        township_id: str,
        day_no: int,
        category: str,
        slot_no: int = 1,
        convention_id: str | None = None,
        group_id: str | None = None,
        stall_name: str | None = None,
        accepted: bool = True,
        idempotency_key: str | None = None,
    ) -> dict:
        existing = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        if existing:
            return {**serialize(existing), "duplicate": True}
        self._get_or_404(conn, "vendors", vendor_id)
        reg = conn.execute(
            "SELECT * FROM vendor_registrations WHERE vendor_id=? AND township_id=?", (vendor_id, township_id)
        ).fetchone()
        if reg is None:
            raise ApiError(403, "VENDOR_NOT_REGISTERED", f"摊主 {vendor_id} 未在 {township_id} 登记")
        sess = conn.execute(
            "SELECT * FROM sessions WHERE township_id=? AND day_no=? AND slot_no=?",
            (township_id, day_no, slot_no),
        ).fetchone()
        if sess is None:
            raise ApiError(409, "DAY_NOT_DECLARED", f"{township_id} 未申报第 {day_no} 日第 {slot_no} 时段")
        key = idempotency_key or application_id
        dup_key = conn.execute("SELECT * FROM applications WHERE idempotency_key=?", (key,)).fetchone()
        if dup_key:
            return {**serialize(dup_key), "duplicate": True}
        conn.execute(
            """INSERT INTO applications(id,idempotency_key,group_id,vendor_id,convention_id,township_id,
                 day_no,slot_no,category,stall_name,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                application_id, key, group_id, vendor_id, convention_id, township_id, day_no, slot_no,
                category, stall_name, "accepted" if accepted else "submitted", now_iso(), now_iso(),
            ),
        )
        self._audit(
            conn, "vendor", "application.submit", "application", application_id,
            {"vendor_id": vendor_id, "township_id": township_id, "day_no": day_no,
             "slot_no": slot_no, "category": category, "group_id": group_id},
        )
        return serialize(conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone())

    def submit_application(self, *args, **kwargs) -> dict:
        with self.db() as conn:
            result = self._submit_application(conn, *args, **kwargs)
            conn.commit()
            return result

    def create_adjacent_pair(
        self,
        group_id: str,
        township_id: str,
        day_no: int,
        stalls: list[dict],
        slot_no: int = 1,
        convention_id: str | None = None,
        require_adjacent: bool = True,
        note: str = "明确要求挨着开的双摊",
    ) -> dict:
        if len(stalls) != 2:
            raise ApiError(400, "PAIR_REQUIRES_TWO", "连摊组必须正好含两个摊")
        with self.db() as conn:
            existing = conn.execute("SELECT * FROM booking_groups WHERE id=?", (group_id,)).fetchone()
            if existing:
                apps = conn.execute("SELECT * FROM applications WHERE group_id=? ORDER BY rowid", (group_id,)).fetchall()
                return {"group": serialize(existing), "applications": [serialize(a) for a in apps], "duplicate": True}
            self._get_or_404(conn, "townships", township_id)
            sess = conn.execute(
                "SELECT * FROM sessions WHERE township_id=? AND day_no=? AND slot_no=?",
                (township_id, day_no, slot_no),
            ).fetchone()
            if sess is None:
                raise ApiError(409, "DAY_NOT_DECLARED", f"{township_id} 未申报第 {day_no} 日第 {slot_no} 时段")
            status = "accepted"
            conn.execute(
                """INSERT INTO booking_groups(id,convention_id,group_type,township_id,day_no,slot_no,
                     require_adjacent,status,note,created_at)
                   VALUES(?,?,'adjacent_pair',?,?,?,?,?,?,?)""",
                (group_id, convention_id, township_id, day_no, slot_no, 1 if require_adjacent else 0, status, note, now_iso()),
            )
            apps = []
            for i, stall in enumerate(stalls, 1):
                app_id = stall.get("application_id") or f"{group_id}-APP{i}"
                result = self._submit_application(
                    conn,
                    application_id=app_id,
                    vendor_id=stall["vendor_id"],
                    township_id=township_id,
                    day_no=day_no,
                    slot_no=slot_no,
                    category=stall["category"],
                    convention_id=convention_id,
                    group_id=group_id,
                    stall_name=stall.get("stall_name"),
                    accepted=True,
                )
                apps.append(result)
            self._audit(conn, "committee", "pair.accepted", "booking_group", group_id, {"stalls": stalls})
            conn.commit()
            group = serialize(conn.execute("SELECT * FROM booking_groups WHERE id=?", (group_id,)).fetchone())
            return {"group": group, "applications": apps}

    def get_group(self, group_id: str) -> dict:
        with self.db() as conn:
            group = serialize(self._get_or_404(conn, "booking_groups", group_id))
            apps = [serialize(r) for r in conn.execute(
                "SELECT * FROM applications WHERE group_id=? ORDER BY rowid", (group_id,)
            ).fetchall()]
            permits = [serialize(r) for r in conn.execute(
                "SELECT * FROM permits WHERE group_id=? ORDER BY rowid", (group_id,)
            ).fetchall()]
            return {"group": group, "applications": apps, "permits": permits}

    def _link_row(self, conn: sqlite3.Connection, spot_a: str, spot_b: str) -> sqlite3.Row | None:
        a, b = canonical_pair(spot_a, spot_b)
        return conn.execute("SELECT * FROM spot_links WHERE spot_a=? AND spot_b=?", (a, b)).fetchone()

    def spots_are_adjacent(self, conn: sqlite3.Connection, spot_a: str, spot_b: str) -> bool:
        link = self._link_row(conn, spot_a, spot_b)
        return bool(link and link["adjacent"] == 1 and not link["barrier"])

    def find_adjacent_pair(
        self,
        township_id: str,
        category_a: str,
        category_b: str,
        day_no: int | None = None,
        slot_no: int | None = None,
    ) -> tuple[str, str] | None:
        with self.db() as conn:
            spots = conn.execute("SELECT * FROM spots WHERE township_id=? AND active=1", (township_id,)).fetchall()
            by_id = {r["id"]: r for r in spots}
            links = conn.execute(
                "SELECT * FROM spot_links WHERE adjacent=1 AND COALESCE(barrier,'')=''"
            ).fetchall()
            for link in links:
                sa, sb = link["spot_a"], link["spot_b"]
                if sa not in by_id or sb not in by_id:
                    continue
                cats_a = loads(by_id[sa]["allowed_categories_json"], [])
                cats_b = loads(by_id[sb]["allowed_categories_json"], [])
                if category_a in cats_a and category_b in cats_b:
                    return sa, sb
                if category_b in cats_a and category_a in cats_b:
                    return sb, sa
            return None

    def venue_readiness(self, township_id: str, day_no: int, slot_no: int = 1, convention_id: str | None = None) -> dict:
        with self.db() as conn:
            township = serialize(self._get_or_404(conn, "townships", township_id))
            session = conn.execute(
                "SELECT * FROM sessions WHERE township_id=? AND day_no=? AND slot_no=?",
                (township_id, day_no, slot_no),
            ).fetchone()
            conv = None
            rule: dict = {"required_categories": ["book", "umbrella"]}
            if convention_id:
                conv = serialize(self._get_or_404(conn, "conventions", convention_id))
                rule = conv.get("success_rule") or rule
            spots = conn.execute("SELECT * FROM spots WHERE township_id=? AND active=1", (township_id,)).fetchall()
            missing = []
            for cat in rule.get("required_categories", []):
                if not any(cat in loads(s["allowed_categories_json"], []) for s in spots):
                    missing.append(cat)
            pair = self.find_adjacent_pair(township_id, rule["required_categories"][0], rule["required_categories"][1]) \
                if len(rule.get("required_categories", [])) >= 2 else None
            qualified = session is not None and session["status"] not in ("rained_out", "cancelled") and not missing
            return {
                "township": township,
                "session": serialize(session),
                "qualified_to_host": qualified,
                "missing_categories": missing,
                "adjacent_required_pair_available": pair is not None,
                "example_adjacent_pair": list(pair) if pair else None,
            }

    def _validate_spot_for_host(
        self, conn: sqlite3.Connection, spot_id: str, host_township_id: str, category: str, day_no: int, slot_no: int
    ) -> sqlite3.Row:
        spot = self._get_or_404(conn, "spots", spot_id)
        if not spot["active"]:
            raise ApiError(409, "SPOT_INACTIVE", f"格位 {spot_id} 已停用")
        cats = loads(spot["allowed_categories_json"], [])
        if category not in cats:
            raise ApiError(422, "CATEGORY_NOT_ALLOWED", f"{spot_id} 不允许 {category} 类摊；允许：{','.join(cats)}")
        if spot["township_id"] != host_township_id:
            loan = conn.execute(
                """SELECT * FROM venue_loans
                   WHERE spot_id=? AND day_no=? AND slot_no=? AND borrower_township_id=?
                     AND status IN ('promised','active')""",
                (spot_id, day_no, slot_no, host_township_id),
            ).fetchone()
            if loan is None:
                raise ApiError(409, "BORROWED_LAND_NOT_PROMISED",
                               f"{spot_id} 属于邻乡，且没有许给 {host_township_id} 的有效借地约定")
        conflict = conn.execute(
            """SELECT a.*, p.permit_no FROM allocations a JOIN permits p ON p.id=a.permit_id
               WHERE a.day_no=? AND a.slot_no=? AND a.spot_id=? AND a.status='active'""",
            (day_no, slot_no, spot_id),
        ).fetchone()
        if conflict:
            raise ApiError(409, "SPOT_DOUBLE_BOOKED", f"{spot_id} 该时段已由摆位单 {conflict['permit_no']} 占用")
        return spot

    def _build_neighbors(self, conn: sqlite3.Connection, permit: sqlite3.Row) -> list[dict]:
        spot_id = permit["spot_id"]
        out = []
        links = conn.execute(
            "SELECT * FROM spot_links WHERE (spot_a=? OR spot_b=?) AND adjacent=1 AND COALESCE(barrier,'')=''",
            (spot_id, spot_id),
        ).fetchall()
        neighbor_ids = []
        for link in links:
            neighbor_ids.append(link["spot_b"] if link["spot_a"] == spot_id else link["spot_a"])
        for nid in neighbor_ids:
            alloc = conn.execute(
                """SELECT p.*, v.name AS vendor_name, a.stall_name
                   FROM allocations al
                   JOIN permits p ON p.id=al.permit_id
                   JOIN applications a ON a.id=p.application_id
                   JOIN vendors v ON v.id=a.vendor_id
                   WHERE al.day_no=? AND al.slot_no=? AND al.spot_id=? AND al.status='active'
                     AND p.status='valid'""",
                (permit["day_no"], permit["slot_no"], nid),
            ).fetchone()
            spot = conn.execute("SELECT code,label FROM spots WHERE id=?", (nid,)).fetchone()
            item = {"spot_id": nid, "spot_code": spot["code"] if spot else nid, "relation": "adjacent"}
            if alloc:
                item.update({
                    "permit_no": alloc["permit_no"],
                    "vendor_name": alloc["vendor_name"],
                    "stall_name": alloc["stall_name"],
                    "category": alloc["category"] if "category" in alloc.keys() else None,
                })
            else:
                item["vendor_name"] = None
            out.append(item)
        return out

    def _placement_text(self, conn: sqlite3.Connection, permit: sqlite3.Row, neighbors: list[dict]) -> str:
        host = conn.execute("SELECT name FROM townships WHERE id=?", (permit["host_township_id"],)).fetchone()
        spot = conn.execute("SELECT code,label,township_id FROM spots WHERE id=?", (permit["spot_id"],)).fetchone()
        parts = [f"{host['name']}轮市第{permit['day_no']}日第{permit['slot_no']}时段"]
        if spot["township_id"] == permit["host_township_id"]:
            parts.append(f"本场 {spot['label'] or spot['code']}（{spot['code']}）")
        else:
            landlord = conn.execute("SELECT name FROM townships WHERE id=?", (spot["township_id"],)).fetchone()
            parts.append(f"借{landlord['name']}场地 {spot['label'] or spot['code']}（{spot['code']}）")
        if neighbors:
            desc = []
            for n in neighbors:
                who = n.get("stall_name") or n.get("vendor_name") or "空邻格"
                desc.append(f"{n['spot_code']}·{who}")
            parts.append("邻摊/邻格：" + "；".join(desc))
        return "。".join(parts)

    def _issue_permit(
        self,
        conn: sqlite3.Connection,
        application_id: str,
        spot_id: str,
        idempotency_key: str | None = None,
        source_permit_id: str | None = None,
    ) -> sqlite3.Row:
        app = self._get_or_404(conn, "applications", application_id)
        if app["status"] not in ("accepted", "permit_issued"):
            raise ApiError(409, "APPLICATION_NOT_ACCEPTED", f"申请 {application_id} 尚未受理")
        existing = conn.execute(
            "SELECT * FROM permits WHERE application_id=? AND status='valid'", (application_id,)
        ).fetchone()
        if existing:
            return existing
        spot = self._validate_spot_for_host(
            conn, spot_id, app["township_id"], app["category"], app["day_no"], app["slot_no"]
        )
        permit_id = new_id("PMT")
        permit_no = new_id("BAI")
        key = idempotency_key or permit_id
        conn.execute(
            """INSERT INTO permits(id,permit_no,idempotency_key,application_id,group_id,convention_id,
                 host_township_id,spot_township_id,spot_id,day_no,slot_no,status,source_permit_id,issued_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?, 'valid', ?,?)""",
            (
                permit_id, permit_no, key, application_id, app["group_id"], app["convention_id"],
                app["township_id"], spot["township_id"], spot_id, app["day_no"], app["slot_no"],
                source_permit_id, now_iso(),
            ),
        )
        conn.execute(
            "INSERT INTO allocations(permit_id,spot_id,day_no,slot_no,status,created_at) VALUES(?,?,?,?, 'active',?)",
            (permit_id, spot_id, app["day_no"], app["slot_no"], now_iso()),
        )
        conn.execute("UPDATE applications SET status='permit_issued', spot_id=?, updated_at=? WHERE id=?",
                     (spot_id, now_iso(), application_id))
        permit = conn.execute("SELECT * FROM permits WHERE id=?", (permit_id,)).fetchone()
        neighbors = self._build_neighbors(conn, permit)
        text = self._placement_text(conn, permit, neighbors)
        conn.execute("UPDATE permits SET neighbors_json=?, placement_text=? WHERE id=?",
                     (json.dumps(neighbors, ensure_ascii=False), text, permit_id))
        return conn.execute("SELECT * FROM permits WHERE id=?", (permit_id,)).fetchone()

    def issue_permit(self, application_id: str, spot_id: str, idempotency_key: str | None = None) -> dict:
        with self.db() as conn:
            try:
                permit = self._issue_permit(conn, application_id, spot_id, idempotency_key)
                self._audit(conn, "vendor", "permit.issue", "permit", permit["id"], {"application_id": application_id})
                conn.commit()
                return serialize(conn.execute("SELECT * FROM permits WHERE id=?", (permit["id"],)).fetchone())
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if idempotency_key:
                    old = conn.execute("SELECT * FROM permits WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                    if old:
                        return {**serialize(old), "duplicate": True}
                raise ApiError(409, "PERMIT_CONFLICT", "摆位单签发冲突") from exc

    def issue_group_permits(self, group_id: str, spot_ids: list[str] | None = None) -> dict:
        with self.db() as conn:
            group = self._get_or_404(conn, "booking_groups", group_id)
            apps = conn.execute("SELECT * FROM applications WHERE group_id=? ORDER BY rowid", (group_id,)).fetchall()
            if len(apps) != 2:
                raise ApiError(409, "GROUP_INCOMPLETE", "双摊组申请不完整")
            existing = conn.execute(
                "SELECT * FROM permits WHERE group_id=? AND status='valid' ORDER BY rowid", (group_id,)
            ).fetchall()
            if len(existing) == 2:
                return {"permits": [serialize(p) for p in existing], "duplicate": True}
            if spot_ids is None:
                found = self.find_adjacent_pair(group["township_id"], apps[0]["category"], apps[1]["category"])
                if found is None:
                    readiness = self.venue_readiness(group["township_id"], group["day_no"], group["slot_no"], group["convention_id"])
                    raise ApiError(
                        409,
                        "ADJACENCY_REQUIRED",
                        f"{group['township_id']}乡仍有办集资格，但现在没有可满足原双摊要求的相连两格，不能开连摊单",
                        {"readiness": readiness},
                    )
                spot_ids = list(found)
            if len(spot_ids) != 2:
                raise ApiError(400, "TWO_SPOTS_REQUIRED", "必须指定两个格位")
            if group["require_adjacent"] and not self.spots_are_adjacent(conn, spot_ids[0], spot_ids[1]):
                readiness = self.venue_readiness(group["township_id"], group["day_no"], group["slot_no"], group["convention_id"])
                raise ApiError(
                    409,
                    "ADJACENCY_REQUIRED",
                    f"{spot_ids[0]} 与 {spot_ids[1]} 已被墙隔开/不相邻；{group['township_id']}乡可办集，但不能给原双摊开连摊单",
                    {"readiness": readiness},
                )
            permits = []
            for app, spot_id in zip(apps, spot_ids):
                permits.append(self._issue_permit(conn, app["id"], spot_id))
            # Refresh neighbor text now that both allocations exist.
            for p in permits:
                fresh = conn.execute("SELECT * FROM permits WHERE id=?", (p["id"],)).fetchone()
                neighbors = self._build_neighbors(conn, fresh)
                text = self._placement_text(conn, fresh, neighbors)
                conn.execute("UPDATE permits SET neighbors_json=?, placement_text=? WHERE id=?",
                             (json.dumps(neighbors, ensure_ascii=False), text, p["id"]))
            self._audit(conn, "committee", "pair.permits_issued", "booking_group", group_id, {"spots": spot_ids})
            conn.commit()
            out = [serialize(conn.execute("SELECT * FROM permits WHERE id=?", (p["id"],)).fetchone()) for p in permits]
            return {"group_id": group_id, "permits": out}

    def get_permit(self, permit_no: str) -> dict:
        with self.db() as conn:
            p = conn.execute("SELECT * FROM permits WHERE permit_no=?", (permit_no,)).fetchone()
            if p is None:
                p = conn.execute("SELECT * FROM permits WHERE id=?", (permit_no,)).fetchone()
            return serialize(self._get_or_404(conn, "permits", permit_no if p is None else p["id"]))

    # ---------- rotation and ledger ----------

    def _effective_rounds(self, conn: sqlite3.Connection, convention_id: str) -> set[int]:
        rows = conn.execute(
            "SELECT round_no FROM round_usage WHERE convention_id=? AND status='effective'", (convention_id,)
        ).fetchall()
        return {r["round_no"] for r in rows}

    def next_round_number(self, conn: sqlite3.Connection, convention_id: str) -> int:
        effective = self._effective_rounds(conn, convention_id)
        n = 1
        while n in effective:
            n += 1
        return n

    def _venue_suitable(
        self, conn: sqlite3.Connection, conv: sqlite3.Row, township_id: str, day_no: int, slot_no: int
    ) -> tuple[bool, list[str]]:
        session = conn.execute(
            "SELECT * FROM sessions WHERE township_id=? AND day_no=? AND slot_no=?",
            (township_id, day_no, slot_no),
        ).fetchone()
        if session is None or session["status"] in ("rained_out", "cancelled"):
            return False, ["没有正常申报的集期"]
        rule = loads(conv["success_rule_json"], {})
        missing = []
        spots = conn.execute("SELECT allowed_categories_json FROM spots WHERE township_id=? AND active=1", (township_id,)).fetchall()
        for cat in rule.get("required_categories", ["book", "umbrella"]):
            if not any(cat in loads(s["allowed_categories_json"], []) for s in spots):
                missing.append(cat)
        return not missing, missing

    def call_next(
        self,
        convention_id: str,
        from_day: int = 1,
        day_no: int | None = None,
        slot_no: int = 1,
        actor: str = "committee",
    ) -> dict:
        with self.db() as conn:
            conv = self._get_or_404(conn, "conventions", convention_id)
            cycle = loads(conv["cycle_json"], ["A", "B", "C"])
            round_no = self.next_round_number(conn, convention_id)
            township_id = cycle[(round_no - 1) % len(cycle)]

            existing = conn.execute(
                """SELECT * FROM turn_calls
                   WHERE convention_id=? AND round_no=? AND status='open'
                   ORDER BY created_at DESC LIMIT 1""",
                (convention_id, round_no),
            ).fetchone()
            if existing and day_no is None:
                return {**serialize(existing), "duplicate_open_call": True}

            owed = conn.execute(
                "SELECT * FROM owed_rounds WHERE convention_id=? AND round_no=? AND status='owed'",
                (convention_id, round_no),
            ).fetchone()
            kind = "makeup" if owed else "regular"

            chosen_day = day_no
            reason = None
            if chosen_day is None:
                sessions = conn.execute(
                    """SELECT * FROM sessions
                       WHERE township_id=? AND day_no>=? AND slot_no=?
                         AND status NOT IN ('rained_out','cancelled')
                       ORDER BY day_no""",
                    (township_id, from_day, slot_no),
                ).fetchall()
                for sess in sessions:
                    ok, missing = self._venue_suitable(conn, conv, township_id, sess["day_no"], slot_no)
                    if ok:
                        chosen_day = sess["day_no"]
                        break
                    reason = f"第{sess['day_no']}日缺 {','.join(missing)}"
                if chosen_day is None:
                    raise ApiError(409, "NO_SUITABLE_VENUE",
                                   f"第{round_no}轮应由 {township_id} 先办，但暂无合用场；不跳过其优先权",
                                   {"township_id": township_id, "round_no": round_no, "reason": reason})
            else:
                ok, missing = self._venue_suitable(conn, conv, township_id, chosen_day, slot_no)
                if not ok:
                    raise ApiError(409, "VENUE_NOT_SUITABLE", f"{township_id} 第{chosen_day}日不合用：{missing}")

            if existing and day_no is not None and existing["day_no"] == chosen_day:
                return {**serialize(existing), "duplicate_open_call": True}

            call_id = new_id("CALL")
            idem = f"call:{convention_id}:r{round_no}:{township_id}:d{chosen_day}:s{slot_no}:{kind}"
            try:
                conn.execute(
                    """INSERT INTO turn_calls(id,idempotency_key,convention_id,round_no,township_id,kind,
                         day_no,slot_no,status,reason,created_at)
                       VALUES(?,?,?,?,?,?,?,?,'open',?,?)""",
                    (call_id, idem + ":" + uuid.uuid4().hex[:6], convention_id, round_no, township_id, kind,
                     chosen_day, slot_no, reason, now_iso()),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                old = conn.execute("SELECT * FROM turn_calls WHERE idempotency_key=?", (idem,)).fetchone()
                if old:
                    return {**serialize(old), "duplicate": True}
                raise
            if owed:
                conn.execute("UPDATE owed_rounds SET status='scheduled', updated_at=? WHERE id=?", (now_iso(), owed["id"]))
            self._audit(conn, actor, "turn.call", "turn_call", call_id,
                        {"round_no": round_no, "township": township_id, "day": chosen_day, "kind": kind})
            conn.commit()
            return serialize(conn.execute("SELECT * FROM turn_calls WHERE id=?", (call_id,)).fetchone())

    def list_calls(self, convention_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM turn_calls"
        args: tuple = ()
        if convention_id:
            q += " WHERE convention_id=?"
            args = (convention_id,)
        q += " ORDER BY convention_id, round_no, created_at"
        with self.db() as conn:
            return [serialize(r) for r in conn.execute(q, args).fetchall()]

    def _success_snapshot(
        self, conn: sqlite3.Connection, convention_id: str, township_id: str, day_no: int, slot_no: int | None = None
    ) -> dict:
        conv = self._get_or_404(conn, "conventions", convention_id)
        rule = loads(conv["success_rule_json"], {})
        sql = """SELECT * FROM receipts
                 WHERE convention_id=? AND host_township_id=? AND day_no=? AND status='open'"""
        args: list[Any] = [convention_id, township_id, day_no]
        if rule.get("same_slot", False) and slot_no is not None:
            sql += " AND slot_no=?"
            args.append(slot_no)
        rows = conn.execute(sql, args).fetchall()
        required = rule.get("required_categories", ["book", "umbrella"])
        distinct_vendors = rule.get("distinct_vendors", True)
        evidence: list[sqlite3.Row] = []
        used_vendors: set[str] = set()
        missing: list[str] = []
        for cat in required:
            candidates = [r for r in rows if r["category"] == cat]
            chosen = None
            for r in candidates:
                if not distinct_vendors or r["vendor_id"] not in used_vendors:
                    chosen = r
                    break
            if chosen is None:
                missing.append(cat)
            else:
                evidence.append(chosen)
                used_vendors.add(chosen["vendor_id"])
        return {
            "ok": not missing,
            "missing_categories": missing,
            "evidence": [r["receipt_no"] for r in evidence],
            "evidence_rows": evidence,
            "open_receipt_count": len(rows),
            "rule": rule,
        }

    def _post_usage(
        self,
        conn: sqlite3.Connection,
        convention_id: str,
        round_no: int,
        township_id: str,
        call_id: str | None,
        day_no: int,
        slot_no: int,
        evidence: list[str],
        idempotency_key: str,
        reverses_id: str | None = None,
    ) -> sqlite3.Row:
        if round_no > 1:
            prev = conn.execute(
                "SELECT id FROM round_usage WHERE convention_id=? AND round_no=? AND status='effective'",
                (convention_id, round_no - 1),
            ).fetchone()
            if prev is None:
                raise ApiError(
                    409,
                    "PREDECESSOR_NOT_COMPLETE",
                    f"第{round_no - 1}轮尚无有效办成记录，不能把 {township_id} 记成第{round_no}轮继任",
                    {"missing_round": round_no - 1},
                )
        already = conn.execute(
            "SELECT * FROM round_usage WHERE convention_id=? AND round_no=? AND status='effective'",
            (convention_id, round_no),
        ).fetchone()
        if already:
            return already
        usage_id = new_id("USE")
        conn.execute(
            """INSERT INTO round_usage(id,idempotency_key,convention_id,round_no,township_id,call_id,
                 success_day_no,success_slot_no,evidence_json,status,reverses_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?, 'effective', ?,?)""",
            (usage_id, idempotency_key + ":" + uuid.uuid4().hex[:6], convention_id, round_no, township_id, call_id,
             day_no, slot_no, json.dumps(evidence, ensure_ascii=False), reverses_id, now_iso()),
        )
        for receipt_no in evidence:
            conn.execute(
                "INSERT OR IGNORE INTO round_usage_receipts(usage_id,receipt_no) VALUES(?,?)",
                (usage_id, receipt_no),
            )
        if call_id:
            conn.execute(
                "UPDATE turn_calls SET status='succeeded', settled_at=? WHERE id=?", (now_iso(), call_id)
            )
        conn.execute(
            """UPDATE turn_calls SET status='superseded', reason='同一轮已有有效办成记录', settled_at=?
               WHERE convention_id=? AND round_no=? AND status='open' AND COALESCE(id,'')<>?""",
            (now_iso(), convention_id, round_no, call_id or ""),
        )
        conn.execute(
            "UPDATE owed_rounds SET status='fulfilled', updated_at=? WHERE convention_id=? AND round_no=?",
            (now_iso(), convention_id, round_no),
        )
        self._audit(conn, "system", "round.effective", "round_usage", usage_id,
                    {"round_no": round_no, "township": township_id, "evidence": evidence})
        return conn.execute("SELECT * FROM round_usage WHERE id=?", (usage_id,)).fetchone()

    def _evaluate_call(self, conn: sqlite3.Connection, call_id: str, finalize: bool = False) -> dict:
        call = self._get_or_404(conn, "turn_calls", call_id)
        if call["status"] == "succeeded":
            usage = conn.execute(
                "SELECT * FROM round_usage WHERE call_id=? AND status='effective' ORDER BY created_at DESC LIMIT 1",
                (call_id,),
            ).fetchone()
            return {"status": "succeeded", "call": serialize(call), "usage": serialize(usage)}

        snap = self._success_snapshot(
            conn, call["convention_id"], call["township_id"], call["day_no"], call["slot_no"]
        )
        result = {
            "call_id": call_id,
            "round_no": call["round_no"],
            "township_id": call["township_id"],
            "quorum_ok": snap["ok"],
            "missing_categories": snap["missing_categories"],
            "evidence": snap["evidence"],
            "open_receipt_count": snap["open_receipt_count"],
        }
        if not snap["ok"]:
            if finalize:
                conn.execute("UPDATE turn_calls SET status='failed_owed', reason=?, settled_at=? WHERE id=?",
                             ("缺少：" + ",".join(snap["missing_categories"]), now_iso(), call_id))
                self._upsert_owed(conn, call, "actual_openings_missing:" + ",".join(snap["missing_categories"]))
                result["status"] = "failed_owed"
            else:
                result["status"] = "partial"
            return result

        try:
            usage = self._post_usage(
                conn,
                call["convention_id"],
                call["round_no"],
                call["township_id"],
                call_id,
                call["day_no"],
                call["slot_no"],
                snap["evidence"],
                idempotency_key=f"settle:{call_id}",
            )
        except ApiError as exc:
            if exc.code == "PREDECESSOR_NOT_COMPLETE":
                result.update({"status": "blocked", "code": exc.code, "message": exc.message, "details": exc.details})
                return result
            raise
        result.update({"status": "succeeded", "usage": serialize(usage)})
        return result

    def evaluate_call(self, call_id: str, finalize: bool = False) -> dict:
        with self.db() as conn:
            result = self._evaluate_call(conn, call_id, finalize)
            conn.commit()
            return result

    def _upsert_owed(self, conn: sqlite3.Connection, call: sqlite3.Row, reason: str) -> None:
        existing = conn.execute(
            "SELECT * FROM owed_rounds WHERE convention_id=? AND round_no=?",
            (call["convention_id"], call["round_no"]),
        ).fetchone()
        if existing:
            conn.execute("UPDATE owed_rounds SET status='owed', reason=?, source_call_id=?, updated_at=? WHERE id=?",
                         (reason, call["id"], now_iso(), existing["id"]))
        else:
            conn.execute(
                """INSERT INTO owed_rounds(id,convention_id,round_no,township_id,source_call_id,status,reason,
                     created_at,updated_at)
                   VALUES(?,?,?,?,?, 'owed', ?,?,?)""",
                (new_id("OWE"), call["convention_id"], call["round_no"], call["township_id"], call["id"],
                 reason, now_iso(), now_iso()),
            )
        self._audit(conn, "system", "round.owed", "turn_call", call["id"],
                    {"round_no": call["round_no"], "township": call["township_id"], "reason": reason})

    def fail_call(self, call_id: str, reason: str = "当日集期结束仍未达到办成条件") -> dict:
        with self.db() as conn:
            call = self._get_or_404(conn, "turn_calls", call_id)
            conn.execute("UPDATE turn_calls SET status='failed_owed', reason=?, settled_at=? WHERE id=?",
                         (reason, now_iso(), call_id))
            self._upsert_owed(conn, call, reason)
            self._audit(conn, "committee", "call.fail", "turn_call", call_id, {"reason": reason})
            conn.commit()
            return serialize(conn.execute("SELECT * FROM turn_calls WHERE id=?", (call_id,)).fetchone())

    def rain_out_session(self, township_id: str, day_no: int, slot_no: int = 1, reason: str = "落雨撤场") -> dict:
        with self.db() as conn:
            sess = conn.execute(
                "SELECT * FROM sessions WHERE township_id=? AND day_no=? AND slot_no=?",
                (township_id, day_no, slot_no),
            ).fetchone()
            if sess is None:
                raise ApiError(404, "SESSION_NOT_FOUND", "集期不存在")
            conn.execute("UPDATE sessions SET status='rained_out', note=? WHERE id=?", (reason, sess["id"]))
            calls = conn.execute(
                "SELECT * FROM turn_calls WHERE township_id=? AND day_no=? AND slot_no=? AND status='open'",
                (township_id, day_no, slot_no),
            ).fetchall()
            failed = []
            for call in calls:
                conn.execute("UPDATE turn_calls SET status='failed_owed', reason=?, settled_at=? WHERE id=?",
                             (reason, now_iso(), call["id"]))
                self._upsert_owed(conn, call, "rain_out")
                failed.append(call["id"])
            self._audit(conn, "committee", "session.rain_out", "session", str(sess["id"]), {"failed_calls": failed})
            conn.commit()
            return {"session": serialize(conn.execute("SELECT * FROM sessions WHERE id=?", (sess["id"],)).fetchone()),
                    "owed_calls": failed}

    # ---------- receipts ----------

    def report_open(
        self,
        receipt_no: str,
        permit_no: str | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        actor: str = "vendor",
    ) -> dict:
        with self.db() as conn:
            by_no = conn.execute("SELECT * FROM receipts WHERE receipt_no=?", (receipt_no,)).fetchone()
            by_key = None
            if idempotency_key:
                by_key = conn.execute("SELECT * FROM receipts WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            existing = by_no or by_key
            if existing:
                if by_no and by_key and by_no["receipt_no"] != by_key["receipt_no"]:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同一幂等键对应不同回单号")
                if request_hash and existing["request_hash"] and request_hash != existing["request_hash"]:
                    raise ApiError(409, "RECEIPT_PAYLOAD_CONFLICT", "重复回单内容与首次不一致")
                conn.execute(
                    "INSERT INTO receipt_events(receipt_no,event_type,detail_json,created_at) VALUES(?, 'duplicate', ?,?)",
                    (existing["receipt_no"], json.dumps({"via": "report_open"}, ensure_ascii=False), now_iso()),
                )
                conn.commit()
                data = serialize(existing)
                data["duplicate"] = True
                return data

            if not permit_no:
                raise ApiError(400, "PERMIT_REQUIRED", "实到报开必须携带有效摆位单号")
            permit = conn.execute("SELECT * FROM permits WHERE permit_no=? OR id=?", (permit_no, permit_no)).fetchone()
            if permit is None:
                raise ApiError(404, "PERMIT_NOT_FOUND", f"摆位单 {permit_no} 不存在")
            if permit["status"] != "valid":
                raise ApiError(409, "PERMIT_NOT_VALID", f"摆位单状态为 {permit['status']}，不能报开")
            app = self._get_or_404(conn, "applications", permit["application_id"])

            conflict = conn.execute(
                """SELECT * FROM receipts
                   WHERE vendor_id=? AND day_no=? AND slot_no=? AND status='open'""",
                (app["vendor_id"], permit["day_no"], permit["slot_no"]),
            ).fetchone()
            if conflict:
                raise ApiError(
                    409,
                    "VENDOR_TIME_CONFLICT",
                    "同一摊主同一时段已在另一格真实报开；两地登记也不能同时到场",
                    {"existing_receipt_no": conflict["receipt_no"], "spot_id": conflict["spot_id"]},
                )

            call = conn.execute(
                """SELECT * FROM turn_calls
                   WHERE convention_id=? AND township_id=? AND day_no=? AND slot_no=? AND status='open'
                   ORDER BY created_at DESC LIMIT 1""",
                (permit["convention_id"], permit["host_township_id"], permit["day_no"], permit["slot_no"]),
            ).fetchone() if permit["convention_id"] else None

            iso = conn.execute("SELECT iso_date FROM sessions WHERE township_id=? AND day_no=? AND slot_no=?",
                               (permit["host_township_id"], permit["day_no"], permit["slot_no"])).fetchone()
            receipt_hash = request_hash or f"{permit['permit_no']}:{app['vendor_id']}:{permit['spot_id']}:{permit['day_no']}:{permit['slot_no']}:{app['category']}"
            conn.execute(
                """INSERT INTO receipts(receipt_no,idempotency_key,request_hash,permit_id,application_id,vendor_id,
                     convention_id,turn_call_id,round_no,host_township_id,spot_township_id,spot_id,day_no,slot_no,
                     iso_date,category,stall_name,status,reported_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?)""",
                (
                    receipt_no, idempotency_key or f"receipt:{receipt_no}", receipt_hash, permit["id"], app["id"],
                    app["vendor_id"], permit["convention_id"], call["id"] if call else None,
                    call["round_no"] if call else permit["round_no"], permit["host_township_id"],
                    permit["spot_township_id"], permit["spot_id"], permit["day_no"], permit["slot_no"],
                    iso["iso_date"] if iso else None, app["category"], app["stall_name"], now_iso(),
                ),
            )
            conn.execute(
                "INSERT INTO receipt_events(receipt_no,event_type,detail_json,created_at) VALUES(?, 'reported', '{}', ?)",
                (receipt_no, now_iso()),
            )
            self._audit(conn, actor, "receipt.open", "receipt", receipt_no, {"permit_no": permit["permit_no"]})
            settlement = None
            if call:
                settlement = self._evaluate_call(conn, call["id"], finalize=False)
            conn.commit()
            data = serialize(conn.execute("SELECT * FROM receipts WHERE receipt_no=?", (receipt_no,)).fetchone())
            data["settlement"] = settlement
            return data

    def _revalidate_usage(self, conn: sqlite3.Connection, usage_id: str) -> dict:
        usage = self._get_or_404(conn, "round_usage", usage_id)
        if usage["status"] != "effective":
            return serialize(usage)
        snap = self._success_snapshot(
            conn, usage["convention_id"], usage["township_id"], usage["success_day_no"], usage["success_slot_no"]
        )
        if snap["ok"]:
            return serialize(usage)

        conn.execute(
            "UPDATE round_usage SET status='reversed', reversal_reason=?, reversed_at=? WHERE id=?",
            ("回执更正后不再满足办成条件：" + ",".join(snap["missing_categories"]), now_iso(), usage_id),
        )
        if usage["call_id"]:
            conn.execute("UPDATE turn_calls SET status='success_revoked', reason=?, settled_at=? WHERE id=?",
                         ("success_revoked:" + ",".join(snap["missing_categories"]), now_iso(), usage["call_id"]))
        call = conn.execute("SELECT * FROM turn_calls WHERE id=?", (usage["call_id"],)).fetchone() if usage["call_id"] else None
        if call:
            self._upsert_owed(conn, call, "receipt_correction:" + ",".join(snap["missing_categories"]))
        else:
            # Construct enough of an owed row directly.
            fake = {
                "id": usage["call_id"] or usage["id"],
                "convention_id": usage["convention_id"],
                "round_no": usage["round_no"],
                "township_id": usage["township_id"],
            }
            self._upsert_owed(conn, fake, "receipt_correction")  # type: ignore[arg-type]

        downstream = conn.execute(
            "SELECT * FROM round_usage WHERE convention_id=? AND round_no>? AND status='effective' ORDER BY round_no",
            (usage["convention_id"], usage["round_no"]),
        ).fetchall()
        suspended = []
        for d in downstream:
            conn.execute(
                "UPDATE round_usage SET status='suspended_chain', suspended_reason=? WHERE id=?",
                (f"第{usage['round_no']}轮有效记录被冲正，继任链暂时挂起", d["id"]),
            )
            suspended.append(d["id"])
        self._audit(conn, "system", "round.reversed", "round_usage", usage_id, {"suspended": suspended})
        result = serialize(conn.execute("SELECT * FROM round_usage WHERE id=?", (usage_id,)).fetchone())
        result["suspended_downstream"] = suspended
        return result

    def withdraw_receipt(self, receipt_no: str, reason: str = "撤摊", actor: str = "committee") -> dict:
        with self.db() as conn:
            receipt = self._get_or_404(conn, "receipts", receipt_no, "receipt_no")
            if receipt["status"] == "withdrawn":
                data = serialize(receipt)
                data["duplicate"] = True
                return data
            conn.execute(
                "UPDATE receipts SET status='withdrawn', withdrawn_at=?, withdrawal_reason=? WHERE receipt_no=?",
                (now_iso(), reason, receipt_no),
            )
            conn.execute(
                "INSERT INTO receipt_events(receipt_no,event_type,detail_json,created_at) VALUES(?, 'withdrawn', ?,?)",
                (receipt_no, json.dumps({"reason": reason, "actor": actor}, ensure_ascii=False), now_iso()),
            )
            impacted = conn.execute(
                """SELECT DISTINCT u.id FROM round_usage u
                   JOIN round_usage_receipts r ON r.usage_id=u.id
                   WHERE r.receipt_no=? AND u.status='effective'""",
                (receipt_no,),
            ).fetchall()
            if not impacted and receipt["round_no"] is not None and receipt["convention_id"]:
                impacted = conn.execute(
                    "SELECT id FROM round_usage WHERE convention_id=? AND round_no=? AND status='effective'",
                    (receipt["convention_id"], receipt["round_no"]),
                ).fetchall()
            revalidated = [self._revalidate_usage(conn, row["id"]) for row in impacted]
            self._audit(conn, actor, "receipt.withdraw", "receipt", receipt_no, {"reason": reason})
            conn.commit()
            return {"receipt": serialize(conn.execute("SELECT * FROM receipts WHERE receipt_no=?", (receipt_no,)).fetchone()),
                    "revalidated_usages": revalidated}

    def restore_receipt(self, receipt_no: str, reason: str = "恢复真实开成") -> dict:
        with self.db() as conn:
            receipt = self._get_or_404(conn, "receipts", receipt_no, "receipt_no")
            if receipt["status"] == "open":
                return {"receipt": serialize(receipt), "duplicate": True}
            conflict = conn.execute(
                """SELECT * FROM receipts WHERE vendor_id=? AND day_no=? AND slot_no=? AND status='open'
                   AND receipt_no<>?""",
                (receipt["vendor_id"], receipt["day_no"], receipt["slot_no"], receipt_no),
            ).fetchone()
            if conflict:
                raise ApiError(409, "VENDOR_TIME_CONFLICT", "该摊主同一时段已在别处报开，不能同时恢复原回单")
            conn.execute(
                "UPDATE receipts SET status='open', restored_at=?, withdrawal_reason=NULL WHERE receipt_no=?",
                (now_iso(), receipt_no),
            )
            conn.execute(
                "INSERT INTO receipt_events(receipt_no,event_type,detail_json,created_at) VALUES(?, 'restored', ?,?)",
                (receipt_no, json.dumps({"reason": reason}, ensure_ascii=False), now_iso()),
            )
            posted = None
            if receipt["convention_id"] and receipt["round_no"] is not None:
                owed = conn.execute(
                    "SELECT * FROM owed_rounds WHERE convention_id=? AND round_no=?",
                    (receipt["convention_id"], receipt["round_no"]),
                ).fetchone()
                snap = self._success_snapshot(
                    conn, receipt["convention_id"], receipt["host_township_id"], receipt["day_no"], receipt["slot_no"]
                )
                if snap["ok"]:
                    posted = self._post_usage(
                        conn,
                        receipt["convention_id"],
                        receipt["round_no"],
                        receipt["host_township_id"],
                        receipt["turn_call_id"],
                        receipt["day_no"],
                        receipt["slot_no"],
                        snap["evidence"],
                        idempotency_key=f"restore:{receipt_no}:{now_iso()}",
                    )
                    # Reactivate chain-suspended usages in round order.
                    suspended = conn.execute(
                        """SELECT * FROM round_usage
                           WHERE convention_id=? AND status='suspended_chain'
                           ORDER BY round_no, created_at""",
                        (receipt["convention_id"],),
                    ).fetchall()
                    for usage in suspended:
                        prev = conn.execute(
                            "SELECT 1 FROM round_usage WHERE convention_id=? AND round_no=? AND status='effective'",
                            (usage["convention_id"], usage["round_no"] - 1),
                        ).fetchone()
                        if not prev and usage["round_no"] > 1:
                            continue
                        usnap = self._success_snapshot(
                            conn, usage["convention_id"], usage["township_id"],
                            usage["success_day_no"], usage["success_slot_no"]
                        )
                        if usnap["ok"]:
                            conn.execute(
                                "UPDATE round_usage SET status='effective', suspended_reason=NULL WHERE id=?",
                                (usage["id"],),
                            )
            self._audit(conn, "committee", "receipt.restore", "receipt", receipt_no, {"posted": posted["id"] if posted else None})
            conn.commit()
            result = {"receipt": serialize(conn.execute("SELECT * FROM receipts WHERE receipt_no=?", (receipt_no,)).fetchone())}
            if posted:
                result["usage"] = serialize(posted)
            return result

    # ---------- loans and transfers ----------

    def create_loan(
        self,
        loan_id: str,
        spot_id: str,
        borrower_township_id: str,
        day_no: int,
        slot_no: int = 1,
        purpose: str = "借地分流一半摊位",
        idempotency_key: str | None = None,
    ) -> dict:
        with self.db() as conn:
            spot = self._get_or_404(conn, "spots", spot_id)
            lender = spot["township_id"]
            if lender == borrower_township_id:
                raise ApiError(400, "LOAN_ACROSS_TOWNSHIP_REQUIRED", "借地应是借邻乡的格位")
            self._get_or_404(conn, "townships", borrower_township_id)
            active_alloc = conn.execute(
                "SELECT 1 FROM allocations WHERE spot_id=? AND day_no=? AND slot_no=? AND status='active'",
                (spot_id, day_no, slot_no),
            ).fetchone()
            if active_alloc:
                raise ApiError(409, "SPOT_ALREADY_ALLOCATED", "该格此时已排给摊位，不能再借")
            try:
                conn.execute(
                    """INSERT INTO venue_loans(id,idempotency_key,spot_id,lender_township_id,
                         borrower_township_id,day_no,slot_no,status,purpose,created_at)
                       VALUES(?,?,?,?,?,?,?, 'promised', ?,?)""",
                    (loan_id, idempotency_key or loan_id, spot_id, lender, borrower_township_id,
                     day_no, slot_no, purpose, now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                old = conn.execute("SELECT * FROM venue_loans WHERE id=? OR idempotency_key=?", (loan_id, loan_id)).fetchone()
                if old:
                    return {**serialize(old), "duplicate": True}
                promised = conn.execute(
                    "SELECT * FROM venue_loans WHERE spot_id=? AND day_no=? AND slot_no=? AND status IN ('promised','active')",
                    (spot_id, day_no, slot_no),
                ).fetchone()
                raise ApiError(409, "LAND_ALREADY_PROMISED",
                               "这块借地已许给别乡/另一用途，不能排两次",
                               {"existing_loan": serialize(promised)} if promised else None) from exc
            self._audit(conn, "committee", "loan.promise", "venue_loan", loan_id,
                        {"spot": spot_id, "borrower": borrower_township_id})
            conn.commit()
            return serialize(conn.execute("SELECT * FROM venue_loans WHERE id=?", (loan_id,)).fetchone())

    def list_loans(self) -> list[dict]:
        with self.db() as conn:
            return [serialize(r) for r in conn.execute("SELECT * FROM venue_loans ORDER BY day_no").fetchall()]

    def transfer_booking(
        self,
        application_id: str,
        to_spot_id: str,
        reason: str = "雨势不均，把一半摊迁到借地",
        to_day_no: int | None = None,
        to_slot_no: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        with self.db() as conn:
            app = self._get_or_404(conn, "applications", application_id)
            old_permit = conn.execute(
                "SELECT * FROM permits WHERE application_id=? AND status='valid' ORDER BY issued_at DESC LIMIT 1",
                (application_id,),
            ).fetchone()
            if old_permit is None:
                raise ApiError(409, "VALID_PERMIT_REQUIRED", "只有持有效摆位单的报名可以转场")
            day_no = to_day_no if to_day_no is not None else old_permit["day_no"]
            slot_no = to_slot_no if to_slot_no is not None else old_permit["slot_no"]
            target_spot = self._validate_spot_for_host(
                conn, to_spot_id, old_permit["host_township_id"], app["category"], day_no, slot_no
            )
            # Release old allocation and void old slip.
            conn.execute("UPDATE allocations SET status='released' WHERE permit_id=? AND status='active'", (old_permit["id"],))
            conn.execute("UPDATE permits SET status='transferred' WHERE id=?", (old_permit["id"],))

            new_permit = self._issue_permit(
                conn, application_id, to_spot_id,
                idempotency_key=(idempotency_key or new_id("TRFKEY")),
                source_permit_id=old_permit["id"],
            )
            # _issue_permit uses application's original day/slot; if target time differs, correct rows.
            if day_no != app["day_no"] or slot_no != app["slot_no"]:
                conn.execute("UPDATE permits SET day_no=?, slot_no=? WHERE id=?", (day_no, slot_no, new_permit["id"]))
                conn.execute("UPDATE allocations SET day_no=?, slot_no=? WHERE permit_id=? AND status='active'",
                             (day_no, slot_no, new_permit["id"]))
                conn.execute("UPDATE applications SET day_no=?, slot_no=?, updated_at=? WHERE id=?",
                             (day_no, slot_no, now_iso(), application_id))
                new_permit = conn.execute("SELECT * FROM permits WHERE id=?", (new_permit["id"],)).fetchone()

            receipt = conn.execute(
                "SELECT * FROM receipts WHERE permit_id=? AND status='open'", (old_permit["id"],)
            ).fetchone()
            receipt_no = None
            if receipt:
                conflict = conn.execute(
                    "SELECT * FROM receipts WHERE vendor_id=? AND day_no=? AND slot_no=? AND status='open' AND receipt_no<>?",
                    (receipt["vendor_id"], day_no, slot_no, receipt["receipt_no"]),
                ).fetchone()
                if conflict:
                    raise ApiError(409, "VENDOR_TIME_CONFLICT", "转场目标时段该摊主已在别处报开")
                receipt_no = receipt["receipt_no"]
                conn.execute(
                    """UPDATE receipts SET permit_id=?, spot_id=?, spot_township_id=?, day_no=?, slot_no=?
                       WHERE receipt_no=?""",
                    (new_permit["id"], to_spot_id, target_spot["township_id"], day_no, slot_no, receipt_no),
                )
                conn.execute(
                    "INSERT INTO receipt_events(receipt_no,event_type,detail_json,created_at) VALUES(?, 'moved', ?,?)",
                    (receipt_no, json.dumps({"to_spot": to_spot_id}, ensure_ascii=False), now_iso()),
                )
            # Refresh placement/neighbors.
            fresh = conn.execute("SELECT * FROM permits WHERE id=?", (new_permit["id"],)).fetchone()
            neighbors = self._build_neighbors(conn, fresh)
            text = self._placement_text(conn, fresh, neighbors)
            conn.execute("UPDATE permits SET neighbors_json=?, placement_text=? WHERE id=?",
                         (json.dumps(neighbors, ensure_ascii=False), text, new_permit["id"]))

            loan = conn.execute(
                "SELECT id FROM venue_loans WHERE spot_id=? AND day_no=? AND slot_no=? AND borrower_township_id=? AND status IN ('promised','active')",
                (to_spot_id, day_no, slot_no, old_permit["host_township_id"]),
            ).fetchone()
            transfer_id = new_id("TRF")
            conn.execute(
                """INSERT INTO transfer_orders(id,idempotency_key,application_id,old_permit_id,new_permit_id,
                     receipt_no,loan_id,host_township_id,round_no,from_json,to_json,status,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?, 'moved', ?, ?)""",
                (
                    transfer_id, idempotency_key or transfer_id, application_id, old_permit["id"], new_permit["id"],
                    receipt_no, loan["id"] if loan else None, old_permit["host_township_id"], old_permit["round_no"],
                    json.dumps({"permit": old_permit["permit_no"], "spot": old_permit["spot_id"]}, ensure_ascii=False),
                    json.dumps({"permit": new_permit["permit_no"], "spot": to_spot_id}, ensure_ascii=False),
                    reason, now_iso(),
                ),
            )
            self._audit(conn, "committee", "booking.transfer", "application", application_id,
                        {"to_spot": to_spot_id, "receipt": receipt_no})
            conn.commit()
            return {
                "transfer_id": transfer_id,
                "old_permit_no": old_permit["permit_no"],
                "new_permit": serialize(conn.execute("SELECT * FROM permits WHERE id=?", (new_permit["id"],)).fetchone()),
                "moved_receipt_no": receipt_no,
                "note": "借地和转场只改变位置，不产生新乡的轮次使用",
            }

    # ---------- reads / board / ledger ----------

    def street_news(self, day_no: int | None = None, iso_date: str | None = None, month: str | None = None) -> dict:
        with self.db() as conn:
            q = "SELECT * FROM sessions WHERE 1=1"
            args: list[Any] = []
            if day_no is not None:
                q += " AND day_no=?"
                args.append(day_no)
            if iso_date:
                q += " AND iso_date=?"
                args.append(iso_date)
            if month:
                q += " AND substr(COALESCE(iso_date,''),1,7)=?"
                args.append(month)
            q += " ORDER BY day_no, slot_no, township_id"
            sessions = conn.execute(q, args).fetchall()
            days: dict[int, dict] = {}
            for s in sessions:
                t = conn.execute("SELECT name,seq FROM townships WHERE id=?", (s["township_id"],)).fetchone()
                key = s["day_no"]
                days.setdefault(key, {"day_no": key, "iso_date": s["iso_date"], "sessions": []})
                if s["iso_date"] and not days[key]["iso_date"]:
                    days[key]["iso_date"] = s["iso_date"]
                open_receipts = conn.execute(
                    "SELECT category, COUNT(*) AS n FROM receipts WHERE host_township_id=? AND day_no=? AND slot_no=? AND status='open' GROUP BY category",
                    (s["township_id"], s["day_no"], s["slot_no"]),
                ).fetchall()
                permit_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM permits WHERE host_township_id=? AND day_no=? AND slot_no=? AND status='valid'",
                    (s["township_id"], s["day_no"], s["slot_no"]),
                ).fetchone()["n"]
                cats = [r["category"] for r in open_receipts]
                conventions = conn.execute("SELECT id,success_rule_json FROM conventions").fetchall()
                progress = []
                for c in conventions:
                    rule = loads(c["success_rule_json"], {})
                    req = rule.get("required_categories", [])
                    progress.append({
                        "convention_id": c["id"],
                        "required": req,
                        "open_categories": cats,
                        "missing": [x for x in req if x not in cats],
                    })
                item = serialize(s)
                item.update({
                    "township_name": t["name"] if t else s["township_id"],
                    "township_seq": t["seq"] if t else None,
                    "fair_open": s["status"] not in ("rained_out", "cancelled", "closed"),
                    "valid_permit_count": permit_count,
                    "open_receipt_categories": [dict(r) for r in open_receipts],
                    "round_progress": progress,
                })
                days[key]["sessions"].append(item)
            return {"query": {"day_no": day_no, "iso_date": iso_date, "month": month}, "days": list(days.values())}

    def ledger(self, convention_id: str) -> dict:
        with self.db() as conn:
            self._get_or_404(conn, "conventions", convention_id)
            usages = [serialize(r) for r in conn.execute(
                "SELECT * FROM round_usage WHERE convention_id=? ORDER BY round_no, created_at", (convention_id,)
            ).fetchall()]
            for u in usages:
                u["evidence"] = loads(u.get("evidence_json"), [])
                u["receipts"] = [r["receipt_no"] for r in conn.execute(
                    "SELECT receipt_no FROM round_usage_receipts WHERE usage_id=?", (u["id"],)
                ).fetchall()]
            effective = [u for u in usages if u["status"] == "effective"]
            owed = [serialize(r) for r in conn.execute(
                "SELECT * FROM owed_rounds WHERE convention_id=? ORDER BY round_no", (convention_id,)
            ).fetchall()]
            calls = [serialize(r) for r in conn.execute(
                "SELECT * FROM turn_calls WHERE convention_id=? ORDER BY round_no, created_at", (convention_id,)
            ).fetchall()]
            return {
                "convention_id": convention_id,
                "effective_rounds": sorted(
                    [{"round_no": u["round_no"], "township_id": u["township_id"], "usage_id": u["id"]} for u in effective],
                    key=lambda x: x["round_no"],
                ),
                "usages": usages,
                "owed_rounds": owed,
                "calls": calls,
            }

    def audit_logs(self, limit: int = 200) -> list[dict]:
        with self.db() as conn:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for r in rows:
                d = serialize(r)
                d["payload"] = loads(d.pop("payload_json", "{}"), {})
                out.append(d)
            return out
