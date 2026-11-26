"""Core state and rules for the local remote-recording studio service.

Only the Python standard library is used.  The important invariant is that a
director's send record and an actor's self-report are not hearing evidence.
Evidence is created only by the local player confirmation endpoint, and a
response take freezes that evidence at its recording-start sequence.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

MAX_LINES = 4
MAX_TAKES_PER_LINE = 3
ROLES = ("director", "jia", "yi", "bing")
ACTOR_ROLES = ("jia", "yi", "bing")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def normalize_words(value: Any) -> list[str]:
    if isinstance(value, str):
        # Audio is represented by short words.  Accept either an array or a
        # whitespace-separated string for convenience.
        words = value.split()
    elif isinstance(value, list):
        words = []
        for item in value:
            if not isinstance(item, (str, int, float)):
                raise ApiError(400, "invalid_words", "每个词必须是短字符串")
            words.append(str(item).strip())
    else:
        raise ApiError(400, "invalid_words", "words 必须是字符串数组")
    words = [w for w in words if w]
    if not words:
        raise ApiError(400, "invalid_words", "录音至少要有一个短词")
    if len(words) > 32:
        raise ApiError(400, "invalid_words", "每段录音最多 32 个短词")
    if any(len(w) > 40 for w in words):
        raise ApiError(400, "invalid_words", "单个短词最多 40 个字符")
    return words


def normalize_interval(value: Any, words_len: int | None = None) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_interval", "interval 必须包含 startIndex/endIndex")
    try:
        start = int(value.get("startIndex", value.get("start", -1)))
        end = int(value.get("endIndex", value.get("end", -1)))
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_interval", "区间下标必须是整数")
    if start < 0 or end <= start:
        raise ApiError(400, "invalid_interval", "区间必须满足 0 <= start < end")
    if words_len is not None and end > words_len:
        raise ApiError(
            422,
            "interval_out_of_range",
            f"区间结束下标 {end} 超出录音词数 {words_len}",
            {"wordCount": words_len},
        )
    return {"startIndex": start, "endIndex": end}


def ranges_cover(ranges: Iterable[tuple[int, int]], start: int, end: int) -> bool:
    """Return whether the union of half-open ranges covers [start, end)."""
    if end <= start:
        return False
    sorted_ranges = sorted((max(0, s), e) for s, e in ranges if e > s)
    covered_to = start
    for s, e in sorted_ranges:
        if s > covered_to:
            return False
        covered_to = max(covered_to, e)
        if covered_to >= end:
            return True
    return covered_to >= end


def missing_indices(ranges: Iterable[tuple[int, int]], start: int, end: int) -> list[int]:
    return [i for i in range(start, end) if not ranges_cover(ranges, i, i + 1)]


class StudioService:
    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(parent, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self._init_schema()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    @contextmanager
    def tx(self):
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.db.execute(sql, params).fetchall())

    def _init_schema(self) -> None:
        with self.tx() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    aggregate_id TEXT,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    token TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS studios (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    director_session_id TEXT,
                    created_seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scripts (
                    id TEXT PRIMARY KEY,
                    studio_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    line_count INTEGER NOT NULL,
                    created_seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rehearsals (
                    id TEXT PRIMARY KEY,
                    studio_id TEXT NOT NULL,
                    script_id TEXT NOT NULL,
                    roster_json TEXT NOT NULL,
                    initial_cue_json TEXT,
                    state TEXT NOT NULL DEFAULT 'open',
                    created_seq INTEGER NOT NULL,
                    closed_seq INTEGER,
                    delivered_sheet_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recordings (
                    id TEXT PRIMARY KEY,
                    studio_id TEXT NOT NULL,
                    script_id TEXT NOT NULL,
                    rehearsal_id TEXT,
                    line_index INTEGER NOT NULL,
                    actor_role TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    take_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    words_json TEXT,
                    start_seq INTEGER NOT NULL,
                    complete_seq INTEGER,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(script_id, line_index, take_no)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY,
                    studio_id TEXT NOT NULL,
                    recording_id TEXT NOT NULL,
                    target_session_id TEXT NOT NULL,
                    target_role TEXT NOT NULL,
                    state TEXT NOT NULL,
                    sent_seq INTEGER NOT NULL,
                    switched_seq INTEGER,
                    superseded_by TEXT,
                    switch_reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS confirmations (
                    id TEXT PRIMARY KEY,
                    client_key TEXT UNIQUE NOT NULL,
                    delivery_id TEXT NOT NULL,
                    recording_id TEXT NOT NULL,
                    target_session_id TEXT NOT NULL,
                    start_index INTEGER NOT NULL,
                    end_index INTEGER NOT NULL,
                    received_seq INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    reject_reason TEXT,
                    idempotent_replay_of TEXT,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS self_reports (
                    id TEXT PRIMARY KEY,
                    studio_id TEXT,
                    session_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recognitions (
                    id TEXT PRIMARY KEY,
                    studio_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active',
                    registered_seq INTEGER NOT NULL,
                    revoked_seq INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sheets (
                    id TEXT PRIMARY KEY,
                    studio_id TEXT NOT NULL,
                    script_id TEXT NOT NULL,
                    rehearsal_id TEXT,
                    collection_mode TEXT NOT NULL DEFAULT 'manual',
                    title TEXT,
                    recording_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    signable INTEGER NOT NULL DEFAULT 0,
                    validation_json TEXT NOT NULL,
                    signed INTEGER NOT NULL DEFAULT 0,
                    signed_seq INTEGER,
                    signed_snapshot_json TEXT,
                    canonical_hash TEXT,
                    created_seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pickup_plans (
                    id TEXT PRIMARY KEY,
                    studio_id TEXT NOT NULL,
                    script_id TEXT NOT NULL,
                    director_session_id TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    actual_ids_json TEXT,
                    bridge_recognition_ids_json TEXT,
                    state TEXT NOT NULL,
                    sheet_id TEXT,
                    created_seq INTEGER NOT NULL,
                    fulfilled_seq INTEGER,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _event(self, db: sqlite3.Connection, kind: str, aggregate_id: str | None = None,
               payload: Any = None) -> int:
        cur = db.execute(
            "INSERT INTO events(kind, aggregate_id, payload_json, created_at) VALUES(?,?,?,?)",
            (kind, aggregate_id, dumps(payload) if payload is not None else None, now_iso()),
        )
        return int(cur.lastrowid)

    # ---------- sessions and studios ----------

    def create_session(self, name: str, role: str) -> dict[str, Any]:
        if role not in ROLES:
            raise ApiError(400, "invalid_role", f"role 必须是 {ROLES}")
        name = (name or "").strip() or role
        sid = new_id("sess")
        token = secrets.token_urlsafe(24)
        with self.tx() as db:
            seq = self._event(db, "session.created", sid, {"name": name, "role": role})
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                (sid, token, name, role, seq, now_iso()),
            )
        row = self.query_one("SELECT * FROM sessions WHERE id=?", (sid,))
        return self.session_dict(row, token=token)

    def require_session(self, token: str | None, role: str | None = None) -> sqlite3.Row:
        if not token:
            raise ApiError(401, "missing_session", "需要 X-Session-Token 会话头")
        row = self.query_one("SELECT * FROM sessions WHERE token=?", (token,))
        if row is None:
            raise ApiError(401, "invalid_session", "会话不存在或服务端数据库已更换")
        if role and row["role"] != role:
            raise ApiError(403, "wrong_role", f"该操作需要 {role} 会话，当前是 {row['role']}")
        return row

    @staticmethod
    def session_dict(row: sqlite3.Row, token: str | None = None) -> dict[str, Any]:
        return {
            "sessionId": row["id"],
            "token": token if token is not None else row["token"],
            "name": row["name"],
            "role": row["role"],
            "createdSeq": row["created_seq"],
        }

    def create_studio(self, name: str, director_token: str) -> dict[str, Any]:
        director = self.require_session(director_token, "director")
        sid = new_id("studio")
        with self.tx() as db:
            seq = self._event(db, "studio.created", sid, {"name": name})
            db.execute(
                "INSERT INTO studios VALUES(?,?,?,?,?)",
                (sid, name or "未命名棚", director["id"], seq, now_iso()),
            )
        return self.studio_dict(self.query_one("SELECT * FROM studios WHERE id=?", (sid,)))

    @staticmethod
    def studio_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "studioId": row["id"],
            "name": row["name"],
            "directorSessionId": row["director_session_id"],
            "createdSeq": row["created_seq"],
        }

    def get_studio_or_404(self, studio_id: str) -> sqlite3.Row:
        row = self.query_one("SELECT * FROM studios WHERE id=?", (studio_id,))
        if row is None:
            raise ApiError(404, "studio_not_found", "棚不存在")
        return row

    # ---------- scripts ----------

    def create_script(self, studio_id: str, payload: dict[str, Any],
                      director_token: str) -> dict[str, Any]:
        self.require_session(director_token, "director")
        self.get_studio_or_404(studio_id)
        title = str(payload.get("title") or "三句台本")
        lines = payload.get("lines")
        if not isinstance(lines, list) or not lines:
            raise ApiError(400, "invalid_script", "lines 必须是非空数组")
        if len(lines) > MAX_LINES:
            raise ApiError(
                422,
                "too_many_lines",
                f"每幕限 {MAX_LINES} 句",
                {"maxLines": MAX_LINES, "actual": len(lines)},
            )
        spec_lines: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            if not isinstance(line, dict):
                raise ApiError(400, "invalid_line", f"第 {i} 句必须是对象")
            role = line.get("role")
            if role not in ACTOR_ROLES:
                raise ApiError(400, "invalid_role", f"第 {i} 句 role 必须是 {ACTOR_ROLES}")
            item: dict[str, Any] = {
                "lineIndex": i,
                "role": role,
                "label": line.get("label") or {"jia": "甲", "yi": "乙", "bing": "丙"}[role],
            }
            hearing = line.get("hearing")
            if hearing is not None:
                cue_index = int(hearing.get("cueLineIndex", i - 1))
                if cue_index < 0 or cue_index >= i:
                    raise ApiError(400, "invalid_hearing",
                                   f"第 {i} 句的听词要求必须指向更早的句子")
                interval = normalize_interval(hearing)
                item["hearing"] = {"cueLineIndex": cue_index, **interval}
            spec_lines.append(item)
        script_id = new_id("scr")
        spec = {"maxLines": MAX_LINES, "maxTakesPerLine": MAX_TAKES_PER_LINE, "lines": spec_lines}
        with self.tx() as db:
            seq = self._event(db, "script.created", script_id, {"studioId": studio_id})
            db.execute(
                "INSERT INTO scripts VALUES(?,?,?,?,?,?,?)",
                (script_id, studio_id, title, dumps(spec), len(lines), seq, now_iso()),
            )
        return self.script_dict(self.query_one("SELECT * FROM scripts WHERE id=?", (script_id,)))

    def get_script_row(self, script_id: str) -> sqlite3.Row:
        row = self.query_one("SELECT * FROM scripts WHERE id=?", (script_id,))
        if row is None:
            raise ApiError(404, "script_not_found", "台本不存在")
        return row

    def get_script(self, script_id: str) -> dict[str, Any]:
        return self.script_dict(self.get_script_row(script_id))

    @staticmethod
    def script_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "scriptId": row["id"],
            "studioId": row["studio_id"],
            "title": row["title"],
            "lineCount": row["line_count"],
            "spec": loads(row["spec_json"]),
            "createdSeq": row["created_seq"],
        }

    # ---------- rehearsals / 同棚连排 ----------

    def create_rehearsal(self, studio_id: str, payload: dict[str, Any],
                         director_token: str) -> dict[str, Any]:
        director = self.require_session(director_token, "director")
        self.get_studio_or_404(studio_id)
        script_id = payload.get("scriptId")
        script = self.get_script_row(script_id)
        if script["studio_id"] != studio_id:
            raise ApiError(400, "script_studio_mismatch", "台本不属于这个棚")
        roster = payload.get("roster")
        if not isinstance(roster, dict):
            raise ApiError(400, "invalid_roster", "roster 必须是 {role: sessionId}")
        resolved_roster: dict[str, str] = {}
        for role in ACTOR_ROLES:
            sid = roster.get(role)
            if not sid:
                continue
            session = self.query_one("SELECT * FROM sessions WHERE id=?", (sid,))
            if session is None:
                raise ApiError(404, "session_not_found", f"{role} 会话不存在")
            if session["role"] != role:
                raise ApiError(400, "roster_role_mismatch", f"会话 {sid} 不是 {role}")
            resolved_roster[role] = sid
        if not resolved_roster:
            raise ApiError(400, "empty_roster", "连排至少要圈定一名参演者")
        initial_cue = payload.get("initialCue")
        if isinstance(initial_cue, dict) and initial_cue.get("recordingId"):
            rec = self.get_recording_row(initial_cue["recordingId"])
            if rec["studio_id"] != studio_id:
                raise ApiError(400, "recording_studio_mismatch", "前词录音不属于这个棚")
        rid = new_id("run")
        with self.tx() as db:
            seq = self._event(db, "rehearsal.created", rid,
                              {"studioId": studio_id, "roster": resolved_roster})
            db.execute(
                "INSERT INTO rehearsals VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rid, studio_id, script_id, dumps(resolved_roster),
                 dumps(initial_cue) if initial_cue is not None else None,
                 "open", seq, None, None, now_iso()),
            )
        return self.rehearsal_dict(self.query_one("SELECT * FROM rehearsals WHERE id=?", (rid,)))

    def get_rehearsal_row(self, rehearsal_id: str) -> sqlite3.Row:
        row = self.query_one("SELECT * FROM rehearsals WHERE id=?", (rehearsal_id,))
        if row is None:
            raise ApiError(404, "rehearsal_not_found", "连排不存在")
        return row

    def close_rehearsal(self, rehearsal_id: str, director_token: str) -> dict[str, Any]:
        self.require_session(director_token, "director")
        row = self.get_rehearsal_row(rehearsal_id)
        if row["state"] == "open":
            with self.tx() as db:
                seq = self._event(db, "rehearsal.closed", rehearsal_id)
                db.execute("UPDATE rehearsals SET state='closed', closed_seq=? WHERE id=?",
                           (seq, rehearsal_id))
        return self.rehearsal_dict(self.get_rehearsal_row(rehearsal_id))

    def rehearsal_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "rehearsalId": row["id"],
            "studioId": row["studio_id"],
            "scriptId": row["script_id"],
            "roster": loads(row["roster_json"], {}),
            "initialCue": loads(row["initial_cue_json"]),
            "state": row["state"],
            "createdSeq": row["created_seq"],
            "closedSeq": row["closed_seq"],
            "deliveredSheetId": row["delivered_sheet_id"],
            "lockPolicy": "封场后不能新建录音；版本在导演签单时最终冻结，已签单永不重算。",
        }

    # ---------- recordings ----------

    def get_recording_row(self, recording_id: str) -> sqlite3.Row:
        row = self.query_one("SELECT * FROM recordings WHERE id=?", (recording_id,))
        if row is None:
            raise ApiError(404, "recording_not_found", "录音不存在")
        return row

    def start_recording(self, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
        session = self.require_session(token)
        script_id = payload.get("scriptId")
        line_index = int(payload.get("lineIndex", -1))
        script = self.get_script_row(script_id)
        spec = loads(script["spec_json"])
        if line_index < 0 or line_index >= script["line_count"]:
            raise ApiError(400, "invalid_line_index", "lineIndex 超出台本范围")
        line_spec = spec["lines"][line_index]
        if session["role"] != line_spec["role"]:
            raise ApiError(403, "wrong_actor",
                           f"第 {line_index} 句应由 {line_spec['role']} 录制")
        rehearsal_id = payload.get("rehearsalId")
        if rehearsal_id:
            rehearsal = self.get_rehearsal_row(rehearsal_id)
            if rehearsal["script_id"] != script_id:
                raise ApiError(400, "rehearsal_script_mismatch", "连排与台本不一致")
            if rehearsal["state"] != "open":
                raise ApiError(409, "rehearsal_closed", "连排已封场，不能再重录；不合片段请开跨场补录")
            roster = loads(rehearsal["roster_json"], {})
            if roster.get(session["role"]) != session["id"]:
                raise ApiError(403, "not_in_roster", "该会话不在这条连排的参演名单中")
        with self.lock:
            count_row = self.query_one(
                "SELECT COUNT(*) AS c FROM recordings WHERE script_id=? AND line_index=?",
                (script_id, line_index),
            )
            take_no = int(count_row["c"]) + 1
            if take_no > MAX_TAKES_PER_LINE:
                raise ApiError(
                    409,
                    "take_limit_exceeded",
                    f"每句最多 {MAX_TAKES_PER_LINE} 遍",
                    {"lineIndex": line_index, "maxTakes": MAX_TAKES_PER_LINE},
                )
            with self.tx() as db:
                seq = self._event(db, "recording.started", None,
                                  {"scriptId": script_id, "lineIndex": line_index,
                                   "takeNo": take_no, "sessionId": session["id"]})
                confirm_rows = db.execute(
                    """
                    SELECT c.* FROM confirmations c
                    WHERE c.target_session_id=? AND c.status='accepted'
                      AND c.received_seq <= ?
                    ORDER BY c.received_seq
                    """,
                    (session["id"], seq),
                ).fetchall()
                confirmations = [self.confirmation_brief(r) for r in confirm_rows]
                snapshot = {
                    "cutoffSeq": seq,
                    "rule": "只有 cutoffSeq 之前的播放器确认属于开录前经历；导演发送记录和自报听过均不计入。",
                    "confirmations": confirmations,
                }
                rec_id = new_id("rec")
                db.execute(
                    "INSERT INTO recordings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rec_id, script["studio_id"], script_id, rehearsal_id, line_index,
                     session["role"], session["id"], take_no, "recording", None,
                     seq, None, dumps(snapshot), now_iso()),
                )
        return self.recording_dict(self.get_recording_row(rec_id))

    def complete_recording(self, recording_id: str, payload: dict[str, Any],
                           token: str | None) -> dict[str, Any]:
        session = self.require_session(token)
        row = self.get_recording_row(recording_id)
        if row["session_id"] != session["id"]:
            raise ApiError(403, "not_your_recording", "只能完成本会话开始的录音")
        if row["status"] != "recording":
            raise ApiError(409, "already_completed", "这段录音已经完成，录音内容不可改写")
        words = normalize_words(payload.get("words"))
        with self.tx() as db:
            seq = self._event(db, "recording.completed", recording_id, {"wordCount": len(words)})
            db.execute(
                "UPDATE recordings SET status='recorded', words_json=?, complete_seq=? WHERE id=?",
                (dumps(words), seq, recording_id),
            )
        return self.recording_dict(self.get_recording_row(recording_id))

    @staticmethod
    def confirmation_brief(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "confirmationId": row["id"],
            "clientKey": row["client_key"],
            "deliveryId": row["delivery_id"],
            "recordingId": row["recording_id"],
            "startIndex": row["start_index"],
            "endIndex": row["end_index"],
            "receivedSeq": row["received_seq"],
            "status": row["status"],
        }

    def recording_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "recordingId": row["id"],
            "studioId": row["studio_id"],
            "scriptId": row["script_id"],
            "rehearsalId": row["rehearsal_id"],
            "lineIndex": row["line_index"],
            "actorRole": row["actor_role"],
            "sessionId": row["session_id"],
            "takeNumber": row["take_no"],
            "status": row["status"],
            "words": loads(row["words_json"]),
            "startSeq": row["start_seq"],
            "completeSeq": row["complete_seq"],
            "listeningSnapshot": loads(row["snapshot_json"], {}),
            "serverAssignedId": True,
        }

    # ---------- earpiece deliveries and player confirmations ----------

    def send_delivery(self, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
        director = self.require_session(token, "director")
        recording_id = payload.get("recordingId")
        rec = self.get_recording_row(recording_id)
        if rec["status"] != "recorded":
            raise ApiError(409, "recording_not_ready", "前词录音尚未完成")
        target_session_id = payload.get("targetSessionId")
        target_role = payload.get("targetRole")
        if target_session_id:
            target = self.query_one("SELECT * FROM sessions WHERE id=?", (target_session_id,))
        elif target_role:
            # Convenience for single-actor demo studios.  Prefer explicit
            # targetSessionId when several sessions share a role.
            target = self.query_one(
                "SELECT * FROM sessions WHERE role=? ORDER BY created_seq LIMIT 1",
                (target_role,),
            )
        else:
            raise ApiError(400, "missing_target", "需要 targetSessionId 或 targetRole")
        if target is None:
            raise ApiError(404, "target_session_not_found", "耳返目标会话不存在")
        delivery_id = new_id("del")
        with self.tx() as db:
            seq = self._event(db, "delivery.sent", delivery_id,
                              {"recordingId": recording_id, "target": target["id"]})
            old_rows = db.execute(
                "SELECT id FROM deliveries WHERE studio_id=? AND target_session_id=? AND state='active'",
                (rec["studio_id"], target["id"]),
            ).fetchall()
            old_ids = [r["id"] for r in old_rows]
            if old_ids:
                db.execute(
                    """UPDATE deliveries SET state='switched', switched_seq=?,
                       superseded_by=?, switch_reason='new_delivery'
                       WHERE studio_id=? AND target_session_id=? AND state='active'""",
                    (seq, delivery_id, rec["studio_id"], target["id"]),
                )
            db.execute(
                "INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (delivery_id, rec["studio_id"], recording_id, target["id"], target["role"],
                 "active", seq, None, None, None, now_iso()),
            )
        row = self.query_one("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        result = self.delivery_dict(row)
        result["supersededDeliveryIds"] = old_ids
        result["evidenceNotice"] = "已发送不等于已听到；必须由播放器回传词段确认。"
        return result

    def get_delivery_row(self, delivery_id: str) -> sqlite3.Row:
        row = self.query_one("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        if row is None:
            raise ApiError(404, "delivery_not_found", "耳返发送记录不存在")
        return row

    @staticmethod
    def delivery_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "deliveryId": row["id"],
            "studioId": row["studio_id"],
            "recordingId": row["recording_id"],
            "targetSessionId": row["target_session_id"],
            "targetRole": row["target_role"],
            "state": row["state"],
            "sentSeq": row["sent_seq"],
            "switchedSeq": row["switched_seq"],
            "supersededBy": row["superseded_by"],
            "switchReason": row["switch_reason"],
        }

    def confirm_playback(self, payload: dict[str, Any], token: str | None,
                         idempotency_key: str | None = None) -> tuple[int, dict[str, Any]]:
        session = self.require_session(token)
        key = str(payload.get("confirmationKey") or idempotency_key or "").strip()
        if not key:
            raise ApiError(400, "missing_confirmation_key",
                           "播放器确认必须带 confirmationKey 或 Idempotency-Key，才能去重")
        delivery_id = payload.get("deliveryId")
        delivery = self.get_delivery_row(delivery_id)
        if delivery["target_session_id"] != session["id"]:
            raise ApiError(403, "wrong_earpiece_session", "只能确认送入本会话耳返的播放")
        rec = self.get_recording_row(delivery["recording_id"])
        words = loads(rec["words_json"], [])
        interval = normalize_interval(payload, len(words))
        request_hash = hashlib.sha256(
            dumps({"deliveryId": delivery_id, **interval}).encode()
        ).hexdigest()

        existing = self.query_one("SELECT * FROM confirmations WHERE client_key=?", (key,))
        if existing is not None:
            old_payload = loads(existing["response_json"], {})
            old_hash = old_payload.get("requestHash")
            if old_hash and old_hash != request_hash:
                raise ApiError(409, "idempotency_key_conflict",
                               "同一个 confirmationKey 不能确认不同词段")
            body = {
                "confirmation": old_payload.get("confirmation", self.confirmation_brief(existing)),
                "idempotentReplay": True,
                "firstReceivedSeq": existing["received_seq"],
                "requestHash": request_hash,
            }
            replay_status = 409 if existing["status"] == "rejected" else 200
            return replay_status, body

        with self.tx() as db:
            seq = self._event(db, "playback.confirmed", key,
                              {"deliveryId": delivery_id, **interval})
            accepted = delivery["state"] == "active"
            status = "accepted" if accepted else "rejected"
            reason = None if accepted else f"delivery_{delivery['state']}"
            cid = new_id("pc")
            brief = {
                "confirmationId": cid,
                "clientKey": key,
                "deliveryId": delivery_id,
                "recordingId": rec["id"],
                "startIndex": interval["startIndex"],
                "endIndex": interval["endIndex"],
                "receivedSeq": seq,
                "status": status,
                "words": words[interval["startIndex"]:interval["endIndex"]],
            }
            response = {"confirmation": brief, "requestHash": request_hash,
                        "admissible": accepted}
            db.execute(
                "INSERT INTO confirmations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, key, delivery_id, rec["id"], session["id"],
                 interval["startIndex"], interval["endIndex"], seq, status, reason,
                 None, dumps(response), now_iso()),
            )
            if not accepted:
                # The row is intentionally retained for audit, but rejected
                # confirmations never enter a recording snapshot.
                response["warning"] = "这条耳返已被切换/关闭，新的补确认不能作为实听证据"
                return 409, response
        return 201, response

    def create_self_report(self, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
        session = self.require_session(token)
        rid = new_id("claim")
        with self.tx() as db:
            seq = self._event(db, "self_report.received", rid, payload)
            db.execute("INSERT INTO self_reports VALUES(?,?,?,?,?,?)",
                       (rid, payload.get("studioId"), session["id"], dumps(payload), seq, now_iso()))
        return {
            "selfReportId": rid,
            "admissible": False,
            "recordedSeq": seq,
            "notice": "演员自报“听过”只作陈述留存，绝不参与交片核验。",
        }

    # ---------- mutual recognition ----------

    @staticmethod
    def _interval_side(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not value.get("recordingId"):
            raise ApiError(400, "invalid_recognition_side",
                           "互认每一侧必须包含 recordingId/startIndex/endIndex")
        rec_id = value["recordingId"]
        # Existence and range are checked by caller with service context.
        interval = normalize_interval(value)
        return {"recordingId": rec_id, **interval}

    def create_recognition(self, studio_id: str, payload: dict[str, Any],
                           token: str | None) -> dict[str, Any]:
        self.require_session(token, "director")
        self.get_studio_or_404(studio_id)
        sides = payload.get("intervals")
        if isinstance(payload.get("left"), dict) and isinstance(payload.get("right"), dict):
            sides = [payload["left"], payload["right"]]
        if not isinstance(sides, list) or len(sides) != 2:
            raise ApiError(400, "invalid_recognition", "互认需要恰好两个指定区间")
        left = self._interval_side(sides[0])
        right = self._interval_side(sides[1])
        left_rec = self.get_recording_row(left["recordingId"])
        right_rec = self.get_recording_row(right["recordingId"])
        if left_rec["studio_id"] != studio_id or right_rec["studio_id"] != studio_id:
            raise ApiError(400, "studio_mismatch", "互认录音必须属于同一个棚")
        if left_rec["script_id"] != right_rec["script_id"] or left_rec["line_index"] != right_rec["line_index"]:
            raise ApiError(422, "recognition_line_mismatch", "互认区间必须是同一句台本的两遍录音")
        normalize_interval(left, len(loads(left_rec["words_json"], [])))
        normalize_interval(right, len(loads(right_rec["words_json"], [])))
        rid = new_id("mrec")
        body = {
            "mutualRecognitionId": rid,
            "studioId": studio_id,
            "left": left,
            "right": right,
            "note": payload.get("note", ""),
            "rule": "文字相同也不自动互认；只有本登记能让一侧实听证据替代另一侧指定区间。",
        }
        with self.tx() as db:
            seq = self._event(db, "recognition.registered", rid, body)
            db.execute("INSERT INTO recognitions VALUES(?,?,?,?,?,?,?)",
                       (rid, studio_id, dumps(body), "active", seq, None, now_iso()))
        body["state"] = "active"
        body["registeredSeq"] = seq
        return body

    def revoke_recognition(self, recognition_id: str, token: str | None) -> dict[str, Any]:
        self.require_session(token, "director")
        row = self.query_one("SELECT * FROM recognitions WHERE id=?", (recognition_id,))
        if row is None:
            raise ApiError(404, "recognition_not_found", "互认登记不存在")
        if row["state"] == "active":
            with self.tx() as db:
                seq = self._event(db, "recognition.revoked", recognition_id)
                db.execute("UPDATE recognitions SET state='revoked', revoked_seq=? WHERE id=?",
                           (seq, recognition_id))
            self.revalidate_open_sheets(row["studio_id"])
        row = self.query_one("SELECT * FROM recognitions WHERE id=?", (recognition_id,))
        result = loads(row["payload_json"], {})
        result.update({"state": row["state"], "registeredSeq": row["registered_seq"],
                       "revokedSeq": row["revoked_seq"]})
        return result

    def list_active_recognitions(self, studio_id: str) -> list[dict[str, Any]]:
        rows = self.query_all(
            "SELECT * FROM recognitions WHERE studio_id=? AND state='active' ORDER BY registered_seq",
            (studio_id,),
        )
        result = []
        for row in rows:
            item = loads(row["payload_json"], {})
            item["state"] = "active"
            item["registeredSeq"] = row["registered_seq"]
            item["mutualRecognitionId"] = row["id"]
            result.append(item)
        return result

    # ---------- hearing evaluation ----------

    @staticmethod
    def _snapshot_ranges(snapshot: dict[str, Any], recording_id: str) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        for c in snapshot.get("confirmations", []):
            if c.get("recordingId") == recording_id and c.get("status", "accepted") == "accepted":
                ranges.append((int(c["startIndex"]), int(c["endIndex"])))
        return ranges

    def _late_confirmations(self, response_rec: sqlite3.Row, cue_recording_id: str) -> list[dict[str, Any]]:
        return [
            self.confirmation_brief(r)
            for r in self.query_all(
                """
                SELECT c.* FROM confirmations c
                WHERE c.target_session_id=? AND c.recording_id=?
                  AND c.status='accepted' AND c.received_seq > ?
                ORDER BY c.received_seq
                """,
                (response_rec["session_id"], cue_recording_id, response_rec["start_seq"]),
            )
        ]

    def evaluate_hearing(self, response_rec: sqlite3.Row, cue_recording_id: str,
                         requirement: dict[str, Any],
                         recognitions: list[dict[str, Any]]) -> dict[str, Any]:
        snapshot = loads(response_rec["snapshot_json"], {})
        start, end = int(requirement["startIndex"]), int(requirement["endIndex"])
        ranges = self._snapshot_ranges(snapshot, cue_recording_id)
        direct = ranges_cover(ranges, start, end)
        check: dict[str, Any] = {
            "forLineIndex": response_rec["line_index"],
            "cueLineIndex": requirement.get("cueLineIndex"),
            "responseRecordingId": response_rec["id"],
            "selectedCueRecordingId": cue_recording_id,
            "requiredInterval": {"startIndex": start, "endIndex": end},
            "confirmedIntervals": [{"startIndex": s, "endIndex": e} for s, e in ranges],
            "directCoverage": direct,
            "lateConfirmationsNotCounted": self._late_confirmations(response_rec, cue_recording_id),
            "status": "pass" if direct else "fail",
            "basis": {"type": "direct_player_confirmation"} if direct else None,
            "reasons": [] if direct else [
                f"开录快照未直接证实在 {cue_recording_id} 上听全 [{start},{end})"
            ],
        }
        if not direct:
            missing = missing_indices(ranges, start, end)
            if missing:
                check["missingWordIndices"] = missing
            for recognition in recognitions:
                left, right = recognition.get("left"), recognition.get("right")
                for selected_side, heard_side in ((left, right), (right, left)):
                    if not selected_side or not heard_side:
                        continue
                    exact_required_side = (
                        selected_side.get("recordingId") == cue_recording_id
                        and int(selected_side["startIndex"]) == start
                        and int(selected_side["endIndex"]) == end
                    )
                    heard_ranges = self._snapshot_ranges(snapshot, heard_side["recordingId"])
                    if exact_required_side and ranges_cover(
                        heard_ranges, int(heard_side["startIndex"]), int(heard_side["endIndex"])
                    ):
                        check["status"] = "pass"
                        check["basis"] = {
                            "type": "mutual_recognition",
                            "mutualRecognitionId": recognition.get("mutualRecognitionId"),
                            "selectedInterval": selected_side,
                            "actuallyHeardInterval": heard_side,
                        }
                        check["reasons"] = []
                        break
                if check["status"] == "pass":
                    break
        if check["lateConfirmationsNotCounted"]:
            check["reasons"].append("存在开录后才送达的播放确认，已按规则排除，不能补成开录前经历")
        return check

    def validate_recording_ids(self, studio_id: str, script_id: str,
                               recording_ids: list[str],
                               rehearsal_id: str | None = None,
                               recognitions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        script = self.get_script_row(script_id)
        if script["studio_id"] != studio_id:
            raise ApiError(400, "script_studio_mismatch", "台本不属于这个棚")
        spec = loads(script["spec_json"])
        n = script["line_count"]
        if not isinstance(recording_ids, list) or len(recording_ids) != n:
            raise ApiError(400, "wrong_selection_length",
                           f"必须按台本顺序引用恰好 {n} 条具体录音",
                           {"expected": n})
        if recognitions is None:
            recognitions = self.list_active_recognitions(studio_id)
        line_checks: list[dict[str, Any]] = []
        recordings: list[sqlite3.Row] = []
        fatal_errors: list[str] = []
        for i, rec_id in enumerate(recording_ids):
            try:
                rec = self.get_recording_row(rec_id)
            except ApiError as e:
                fatal_errors.append(f"第 {i} 句引用不存在: {rec_id}")
                line_checks.append({"lineIndex": i, "status": "fail", "recordingId": rec_id,
                                    "reasons": [e.message]})
                recordings.append(None)  # type: ignore[arg-type]
                continue
            recordings.append(rec)
            reasons: list[str] = []
            if rec["script_id"] != script_id or rec["line_index"] != i:
                reasons.append("录音与台本句序/台本不匹配")
            if rec["status"] != "recorded":
                reasons.append("录音尚未完成")
            if rehearsal_id and rec["rehearsal_id"] != rehearsal_id:
                reasons.append("该录音不属于指定同棚连排")
            if reasons:
                fatal_errors.extend([f"第 {i} 句: {r}" for r in reasons])
            line_checks.append({
                "lineIndex": i,
                "label": spec["lines"][i].get("label"),
                "role": spec["lines"][i]["role"],
                "recordingId": rec_id,
                "takeNumber": rec["take_no"],
                "rehearsalId": rec["rehearsal_id"],
                "words": loads(rec["words_json"]),
                "status": "fail" if reasons else "pass",
                "reasons": reasons,
                "hearingChecks": [],
            })
        hearing_checks: list[dict[str, Any]] = []
        for i, line_spec in enumerate(spec["lines"]):
            req = line_spec.get("hearing")
            if not req:
                continue
            response_rec = recordings[i] if i < len(recordings) else None
            cue_index = int(req["cueLineIndex"])
            cue_rec_id = recording_ids[cue_index]
            if response_rec is None:
                continue
            check = self.evaluate_hearing(response_rec, cue_rec_id, req, recognitions)
            hearing_checks.append(check)
            line_checks[i].setdefault("hearingChecks", []).append(check)
            if check["status"] != "pass":
                line_checks[i]["status"] = "fail"
        all_pass = not fatal_errors and all(lc["status"] == "pass" for lc in line_checks)
        return {
            "signable": all_pass,
            "status": "ready" if all_pass else "pending",
            "lineChecks": line_checks,
            "hearingChecks": hearing_checks,
            "activeRecognitionIds": [r.get("mutualRecognitionId") for r in recognitions],
            "validatedAtSeq": self.query_one("SELECT COALESCE(MAX(seq),0) AS s FROM events")["s"],
        }

    # ---------- sheets ----------

    def create_sheet(self, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
        self.require_session(token, "director")
        studio_id = payload["studioId"]
        self.get_studio_or_404(studio_id)
        script_id = payload["scriptId"]
        recording_ids = payload.get("recordingIds")
        rehearsal_id = payload.get("rehearsalId")
        if rehearsal_id:
            rehearsal = self.get_rehearsal_row(rehearsal_id)
            if rehearsal["script_id"] != script_id:
                raise ApiError(400, "rehearsal_script_mismatch", "连排与台本不一致")
            if payload.get("collectionMode", "rehearsal") == "rehearsal":
                # A same-room cut is only collected after 封场.  Drafts can
                # still be evaluated through /api/sheets/validate.
                if payload.get("requireClosed", True) and rehearsal["state"] == "open":
                    raise ApiError(409, "rehearsal_not_closed",
                                   "同棚连排尚未封场；封场后才能建交片单")
        validation = self.validate_recording_ids(studio_id, script_id, recording_ids, rehearsal_id)
        sheet_id = new_id("sheet")
        with self.tx() as db:
            seq = self._event(db, "sheet.created", sheet_id,
                              {"recordingIds": recording_ids, "status": validation["status"]})
            db.execute("INSERT INTO sheets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (sheet_id, studio_id, script_id, rehearsal_id,
                        payload.get("collectionMode", "manual"), payload.get("title"),
                        dumps(recording_ids), validation["status"],
                        1 if validation["signable"] else 0, dumps(validation),
                        0, None, None, None, seq, now_iso()))
        return self.sheet_dict(self.get_sheet_row(sheet_id))

    def validate_only(self, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
        self.require_session(token, "director")
        return self.validate_recording_ids(
            payload["studioId"], payload["scriptId"], payload["recordingIds"],
            payload.get("rehearsalId"),
        )

    def get_sheet_row(self, sheet_id: str) -> sqlite3.Row:
        row = self.query_one("SELECT * FROM sheets WHERE id=?", (sheet_id,))
        if row is None:
            raise ApiError(404, "sheet_not_found", "交片单不存在")
        return row

    def revalidate_open_sheets(self, studio_id: str) -> None:
        rows = self.query_all("SELECT * FROM sheets WHERE studio_id=? AND signed=0", (studio_id,))
        recognitions = self.list_active_recognitions(studio_id)
        for row in rows:
            ids = loads(row["recording_ids_json"], [])
            validation = self.validate_recording_ids(
                studio_id, row["script_id"], ids, row["rehearsal_id"], recognitions
            )
            with self.tx() as db:
                db.execute(
                    "UPDATE sheets SET status=?, signable=?, validation_json=? WHERE id=?",
                    (validation["status"], 1 if validation["signable"] else 0,
                     dumps(validation), row["id"]),
                )

    def sign_sheet(self, sheet_id: str, token: str | None) -> dict[str, Any]:
        self.require_session(token, "director")
        row = self.get_sheet_row(sheet_id)
        if row["signed"]:
            return self.sheet_dict(row)
        ids = loads(row["recording_ids_json"], [])
        validation = self.validate_recording_ids(row["studio_id"], row["script_id"], ids,
                                                 row["rehearsal_id"])
        if not validation["signable"]:
            with self.tx() as db:
                db.execute("UPDATE sheets SET status='pending', signable=0, validation_json=? WHERE id=?",
                           (dumps(validation), sheet_id))
            raise ApiError(409, "sheet_not_signable",
                           "交片单仍有待处理的接句/听词核验", validation)
        signed_snapshot = {
            "sheetId": sheet_id,
            "status": "signed",
            "signed": True,
            "studioId": row["studio_id"],
            "scriptId": row["script_id"],
            "rehearsalId": row["rehearsal_id"],
            "collectionMode": row["collection_mode"],
            "title": row["title"],
            "recordingIdsInScriptOrder": ids,
            "validation": validation,
            "signedAt": now_iso(),
        }
        with self.tx() as db:
            seq = self._event(db, "sheet.signed", sheet_id)
            signed_snapshot["signedSeq"] = seq
            # Hash the frozen body apart from the hash field itself.
            hash_body = {k: v for k, v in signed_snapshot.items() if k != "canonicalHash"}
            digest = hashlib.sha256(dumps(hash_body).encode("utf-8")).hexdigest()
            signed_snapshot["canonicalHash"] = digest
            canonical = dumps(signed_snapshot)
            db.execute(
                """UPDATE sheets SET signed=1, signed_seq=?, status='signed', signable=1,
                   signed_snapshot_json=?, canonical_hash=?, validation_json=? WHERE id=?""",
                (seq, canonical, digest, dumps(validation), sheet_id),
            )
            if row["rehearsal_id"]:
                db.execute(
                    "UPDATE rehearsals SET state='delivered', delivered_sheet_id=? WHERE id=?",
                    (sheet_id, row["rehearsal_id"]),
                )
        return self.sheet_dict(self.get_sheet_row(sheet_id))

    def sheet_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        if row["signed"] and row["signed_snapshot_json"]:
            # Signed sheets are returned byte-for-byte from their frozen JSON.
            return loads(row["signed_snapshot_json"])
        return {
            "sheetId": row["id"],
            "studioId": row["studio_id"],
            "scriptId": row["script_id"],
            "rehearsalId": row["rehearsal_id"],
            "collectionMode": row["collection_mode"],
            "title": row["title"],
            "recordingIds": loads(row["recording_ids_json"]),
            "status": row["status"],
            "signable": bool(row["signable"]),
            "signed": bool(row["signed"]),
            "validation": loads(row["validation_json"]),
            "createdSeq": row["created_seq"],
        }

    def verify_sheet(self, sheet_id: str) -> dict[str, Any]:
        row = self.get_sheet_row(sheet_id)
        if not row["signed"]:
            return {"sheetId": sheet_id, "signed": False, "status": row["status"],
                    "hashMatches": False}
        snapshot = loads(row["signed_snapshot_json"], {})
        stored_hash = row["canonical_hash"]
        body = {k: v for k, v in snapshot.items() if k != "canonicalHash"}
        calculated = hashlib.sha256(dumps(body).encode("utf-8")).hexdigest()
        return {
            "sheetId": sheet_id,
            "signed": True,
            "status": snapshot.get("status", "signed"),
            "immutableQuery": "GET /api/sheets/{sheetId} 返回签出时冻结的 JSON",
            "storedCanonicalHash": stored_hash,
            "calculatedCanonicalHash": calculated,
            "hashMatches": calculated == stored_hash == snapshot.get("canonicalHash"),
        }

    # ---------- casting matrix for director explanations ----------

    def casting_matrix(self, script_id: str, token: str | None) -> dict[str, Any]:
        self.require_session(token, "director")
        script = self.get_script_row(script_id)
        spec = loads(script["spec_json"])
        recognitions = self.list_active_recognitions(script["studio_id"])
        takes = self.query_all(
            "SELECT * FROM recordings WHERE script_id=? AND status='recorded' ORDER BY line_index,take_no",
            (script_id,),
        )
        by_line: dict[int, list[sqlite3.Row]] = {}
        for r in takes:
            by_line.setdefault(r["line_index"], []).append(r)
        matrix = []
        for i, line in enumerate(spec["lines"]):
            req = line.get("hearing")
            item: dict[str, Any] = {"lineIndex": i, "role": line["role"], "takes": []}
            for response in by_line.get(i, []):
                take_info: dict[str, Any] = {
                    "recordingId": response["id"],
                    "takeNumber": response["take_no"],
                    "rehearsalId": response["rehearsal_id"],
                    "words": loads(response["words_json"]),
                    "snapshot": loads(response["snapshot_json"]),
                }
                if req:
                    cue_line = int(req["cueLineIndex"])
                    take_info["againstCueTakes"] = []
                    for cue in by_line.get(cue_line, []):
                        check = self.evaluate_hearing(response, cue["id"], req, recognitions)
                        take_info["againstCueTakes"].append({
                            "cueRecordingId": cue["id"],
                            "cueTakeNumber": cue["take_no"],
                            "status": check["status"],
                            "basis": check["basis"],
                            "reasons": check["reasons"],
                            "lateConfirmationsNotCounted": check["lateConfirmationsNotCounted"],
                        })
                item["takes"].append(take_info)
            matrix.append(item)
        self_reports = self.query_all(
            "SELECT id, session_id, payload_json, created_seq FROM self_reports WHERE studio_id=? ORDER BY created_seq",
            (script["studio_id"],),
        )
        return {
            "scriptId": script_id,
            "matrix": matrix,
            "selfReportsIgnored": [
                {"selfReportId": r["id"], "sessionId": r["session_id"],
                 "seq": r["created_seq"], "payload": loads(r["payload_json"])}
                for r in self_reports
            ],
            "howToExplainRejectedTake": [
                "看 listeningSnapshot.cutoffSeq：开录前只有播放器确认算证据。",
                "看 againstCueTakes[].reasons：是没听到指定录音、区间没听全、用了被撤互认，还是确认迟到。",
                "导演发送记录只在 deliveries 中证明已发送；自报在 selfReportsIgnored 中但不参与判定。",
            ],
        }

    # ---------- cross-session pickup optimization ----------

    def create_pickup_plan(self, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
        director = self.require_session(token, "director")
        studio_id = payload["studioId"]
        self.get_studio_or_404(studio_id)
        script_id = payload["scriptId"]
        script = self.get_script_row(script_id)
        spec = loads(script["spec_json"])
        n = script["line_count"]
        baseline = payload.get("baselineRecordingIds")
        if not isinstance(baseline, list) or len(baseline) != n:
            raise ApiError(400, "invalid_baseline", f"baselineRecordingIds 必须有 {n} 条")
        costs_input = payload.get("rerecordCosts")
        if not isinstance(costs_input, list) or len(costs_input) != n:
            raise ApiError(400, "invalid_costs", f"rerecordCosts 必须是长度 {n} 的数组")
        costs: list[float | int] = []
        for c in costs_input:
            try:
                value = float(c)
            except (TypeError, ValueError):
                raise ApiError(400, "invalid_costs", "重录成本必须是数字")
            if value < 0:
                raise ApiError(400, "invalid_costs", "重录成本不能为负")
            costs.append(int(value) if value.is_integer() else value)
        baseline_rows = []
        for i, rid in enumerate(baseline):
            row = self.get_recording_row(rid)
            if row["script_id"] != script_id or row["line_index"] != i or row["status"] != "recorded":
                raise ApiError(422, "invalid_baseline_take", f"第 {i} 句基线片不可用")
            baseline_rows.append(row)
        take_counts = [
            int(self.query_one(
                "SELECT COUNT(*) AS c FROM recordings WHERE script_id=? AND line_index=?",
                (script_id, i),
            )["c"])
            for i in range(n)
        ]
        recognitions = self.list_active_recognitions(studio_id)
        candidates: list[dict[str, Any]] = []

        for mask in range(1 << n):
            pickup_set = {i for i in range(n) if mask & (1 << i)}
            infeasible: list[str] = []
            choices: list[dict[str, Any]] = []
            total = 0.0
            for i in range(n):
                if i in pickup_set:
                    if take_counts[i] >= MAX_TAKES_PER_LINE:
                        infeasible.append(f"第 {i} 句已达 {MAX_TAKES_PER_LINE} 遍，不能再补录")
                    total += costs[i]
                    choices.append({"lineIndex": i, "type": "pickup", "cost": costs[i],
                                    "recordingId": None})
                else:
                    choices.append({"lineIndex": i, "type": "existing", "cost": 0,
                                    "recordingId": baseline[i],
                                    "rehearsalId": baseline_rows[i]["rehearsal_id"]})
            junctions: list[dict[str, Any]] = []
            bridges: list[dict[str, Any]] = []
            for i in range(1, n):
                left = choices[i - 1]
                right = choices[i]
                line_spec = loads(script["spec_json"])["lines"][i]
                req = line_spec.get("hearing")
                junction: dict[str, Any] = {"betweenLines": [i - 1, i], "status": "feasible"}
                if right["type"] == "pickup":
                    junction["basis"] = "补录应答时先送前词、收播放器确认，再开录"
                elif req is None:
                    junction["basis"] = "该句无听词区间要求"
                elif left["type"] == "existing":
                    check = self.evaluate_hearing(
                        baseline_rows[i], baseline[i - 1], req, recognitions
                    )
                    junction["status"] = "feasible" if check["status"] == "pass" else "infeasible"
                    junction["hearingCheck"] = check
                    if check["status"] != "pass":
                        infeasible.append(f"接句 {i-1}->{i} 不合且未安排补录第 {i} 句")
                else:
                    # Left is a future pickup while right is an old take.
                    # Preserve the later take only if its frozen snapshot
                    # already proves an interval a director can bridge by
                    # mutual recognition after the pickup gets an ID.
                    snapshot = loads(baseline_rows[i]["snapshot_json"], {})
                    s, e = int(req["startIndex"]), int(req["endIndex"])
                    heard_target = None
                    for c in snapshot.get("confirmations", []):
                        qid = c["recordingId"]
                        ranges = self._snapshot_ranges(snapshot, qid)
                        if ranges_cover(ranges, s, e):
                            qrow = self.get_recording_row(qid)
                            if qrow["line_index"] == i - 1:
                                heard_target = qid
                                break
                    if heard_target:
                        bridge = {
                            "kind": "register_mutual_recognition_after_pickup",
                            "afterPickupLine": i - 1,
                            "pickupSide": {"recordingId": None, "startIndex": s, "endIndex": e},
                            "alreadyHeardSide": {"recordingId": heard_target,
                                                 "startIndex": s, "endIndex": e},
                        }
                        bridges.append(bridge)
                        junction["basis"] = "后句可保留；补录取得录音号后登记导演互认"
                        junction["bridge"] = bridge
                    else:
                        junction["status"] = "infeasible"
                        infeasible.append(
                            f"第 {i} 句虽可保留，但没有可搭桥的开录前实听区间，前句补录会使接句失效"
                        )
                junctions.append(junction)
            feasible = not infeasible
            # Prefer lower cost, fewer pickups, then preserving the latest
            # possible suffix.  The reversed bit tuple puts line n-1 first.
            suffix_score = tuple(1 if i in pickup_set else 0 for i in range(n - 1, -1, -1))
            candidate = {
                "feasible": feasible,
                "infeasibleReasons": infeasible,
                "totalCost": total,
                "pickupLineIndexes": sorted(pickup_set),
                "choices": choices,
                "junctions": junctions,
                "bridgeRecognitions": bridges,
                "tieBreak": {"pickupCount": len(pickup_set), "reversedPickupBits": suffix_score},
                "score": (total, len(pickup_set), suffix_score),
            }
            candidates.append(candidate)
        feasible_plans = [c for c in candidates if c["feasible"]]
        best = min(feasible_plans, key=lambda c: c["score"]) if feasible_plans else None
        if best:
            best.pop("score", None)
        alternatives = sorted(
            ({k: v for k, v in c.items() if k != "score"} for c in feasible_plans),
            key=lambda c: (c["totalCost"], c["tieBreak"]["pickupCount"],
                           c["tieBreak"]["reversedPickupBits"]),
        )[:6]
        result = {
            "feasible": best is not None,
            "rule": "枚举保留基线片或逐句补录；补录成本来自输入，后句只有在存在冻结实听区间可搭桥时才保留。",
            "best": best,
            "lowestCostPickupList": [
                {"lineIndex": i, "actorRole": spec["lines"][i]["role"], "cost": costs[i]}
                for i in (best["pickupLineIndexes"] if best else [])
            ],
            "totalCost": best["totalCost"] if best else None,
            "retainedLineIndexes": [i for i in range(n) if not best or i not in set(best["pickupLineIndexes"])],
            "bridgeRecognitionTasks": best["bridgeRecognitions"] if best else [],
            "consideredSubsets": 1 << n,
            "feasibleAlternativeCount": len(feasible_plans),
            "topFeasibleAlternatives": alternatives,
            "infeasibleExampleCount": len(candidates) - len(feasible_plans),
        }
        plan_id = new_id("plan")
        state = "planned" if best else "no_feasible_plan"
        with self.tx() as db:
            seq = self._event(db, "pickup_plan.created", plan_id,
                              {"feasible": result["feasible"], "cost": result["totalCost"]})
            db.execute("INSERT INTO pickup_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (plan_id, studio_id, script_id, director["id"],
                        dumps({"baselineRecordingIds": baseline, "rerecordCosts": costs_input}),
                        dumps(result), None, None, state, None, seq, None, now_iso()))
        return self.plan_dict(self.query_one("SELECT * FROM pickup_plans WHERE id=?", (plan_id,)))

    def get_plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self.query_one("SELECT * FROM pickup_plans WHERE id=?", (plan_id,))
        if row is None:
            raise ApiError(404, "plan_not_found", "补录方案不存在")
        return row

    def plan_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = loads(row["result_json"], {})
        return {
            "planId": row["id"],
            "studioId": row["studio_id"],
            "scriptId": row["script_id"],
            "state": row["state"],
            "input": loads(row["input_json"], {}),
            "plan": result,
            "actualRecordingIds": loads(row["actual_ids_json"]),
            "bridgeRecognitionIds": loads(row["bridge_recognition_ids_json"], []),
            "sheetId": row["sheet_id"],
            "createdSeq": row["created_seq"],
            "fulfilledSeq": row["fulfilled_seq"],
            "deliveryRule": "所有 pickupLineIndexes 录齐并完成搭桥互认后，才能创建/签署交片单。",
        }

    def simulate_pickup_plan(self, plan_id: str, token: str | None,
                             words_by_line: dict[str, list[str]] | None = None) -> dict[str, Any]:
        director = self.require_session(token, "director")
        row = self.get_plan_row(plan_id)
        if row["director_session_id"] != director["id"]:
            # Any director session may operate locally, but keep audit simple.
            self.require_session(token, "director")
        plan = self.plan_dict(row)
        if not plan["plan"].get("feasible") or not plan["plan"].get("best"):
            raise ApiError(422, "plan_infeasible", "没有可行补录清单")
        if row["state"] == "completed":
            return plan
        script = self.get_script_row(row["script_id"])
        spec = loads(script["spec_json"])
        baseline = plan["input"]["baselineRecordingIds"]
        pickup_lines = plan["plan"]["best"]["pickupLineIndexes"]
        actual_ids: dict[str, str] = {str(i): rid for i, rid in enumerate(baseline)}
        bridge_ids: list[str] = []
        words_by_line = words_by_line or {}

        for i in pickup_lines:
            predecessor_id = actual_ids[str(i - 1)] if i > 0 else None
            if predecessor_id is None:
                raise ApiError(422, "missing_predecessor", "第一句无需应答；方案不应要求其听词")
            baseline_response = self.get_recording_row(baseline[i])
            actor_token_row = self.query_one(
                "SELECT token FROM sessions WHERE id=?", (baseline_response["session_id"],)
            )
            actor_token = actor_token_row["token"]
            target_session = self.query_one("SELECT * FROM sessions WHERE id=?",
                                            (baseline_response["session_id"],))
            delivery = self.send_delivery({
                "recordingId": predecessor_id,
                "targetSessionId": target_session["id"],
            }, token)
            req = spec["lines"][i].get("hearing")
            if req:
                self.confirm_playback({
                    "deliveryId": delivery["deliveryId"],
                    "confirmationKey": f"{plan_id}:line{i}:required",
                    "startIndex": req["startIndex"],
                    "endIndex": req["endIndex"],
                }, actor_token, None)
            else:
                cue = self.get_recording_row(predecessor_id)
                self.confirm_playback({
                    "deliveryId": delivery["deliveryId"],
                    "confirmationKey": f"{plan_id}:line{i}:all",
                    "startIndex": 0,
                    "endIndex": len(loads(cue["words_json"], [])),
                }, actor_token, None)
            started = self.start_recording({
                "scriptId": row["script_id"],
                "lineIndex": i,
            }, actor_token)
            words = words_by_line.get(str(i)) or words_by_line.get(i) or [
                f"补录句{i + 1}词1", "词2", "词3", "词4", "词5"
            ]
            completed = self.complete_recording(started["recordingId"], {"words": words}, actor_token)
            actual_ids[str(i)] = completed["recordingId"]

            # Register any bridge from this new pickup to an old interval
            # actually heard by the retained successor.
            for bridge in plan["plan"].get("bridgeRecognitionTasks", []):
                if bridge.get("afterPickupLine") != i:
                    continue
                pickup_side = dict(bridge["pickupSide"])
                pickup_side["recordingId"] = completed["recordingId"]
                heard_side = bridge["alreadyHeardSide"]
                recognition = self.create_recognition(row["studio_id"], {
                    "left": pickup_side,
                    "right": heard_side,
                    "note": f"跨场补录方案 {plan_id} 保留后句的搭桥互认",
                }, token)
                bridge_ids.append(recognition["mutualRecognitionId"])

        final_ids = [actual_ids[str(i)] for i in range(script["line_count"])]
        validation = self.validate_recording_ids(row["studio_id"], row["script_id"], final_ids)
        if not validation["signable"]:
            raise ApiError(409, "pickups_not_all_valid", "补录尚未形成可交的整段对话", validation)
        with self.tx() as db:
            seq = self._event(db, "pickup_plan.fulfilled", plan_id, {"actualIds": final_ids})
            db.execute(
                "UPDATE pickup_plans SET state='completed', actual_ids_json=?, bridge_recognition_ids_json=?, fulfilled_seq=? WHERE id=?",
                (dumps(actual_ids), dumps(bridge_ids), seq, plan_id),
            )
        return self.plan_dict(self.get_plan_row(plan_id))

    def create_sheet_from_plan(self, plan_id: str, token: str | None,
                               title: str | None = None) -> dict[str, Any]:
        self.require_session(token, "director")
        row = self.get_plan_row(plan_id)
        plan = self.plan_dict(row)
        best = plan["plan"].get("best") or {}
        if row["state"] != "completed":
            raise ApiError(409, "pickups_incomplete",
                           "补录清单尚未录齐，不能交片",
                           {"state": row["state"],
                            "requiredPickupLineIndexes": best.get("pickupLineIndexes", [])})
        actual = loads(row["actual_ids_json"], {})
        script = self.get_script_row(row["script_id"])
        ids = [actual.get(str(i)) for i in range(script["line_count"])]
        if any(not rid for rid in ids):
            raise ApiError(409, "pickups_incomplete", "仍有句子没有具体录音号",
                           {"actualRecordingIds": actual})
        return self.create_sheet({
            "studioId": row["studio_id"],
            "scriptId": row["script_id"],
            "collectionMode": "cross_session_pickup",
            "title": title or "跨场补录交片单",
            "recordingIds": ids,
        }, token)
