#!/usr/bin/env python3
"""Local disk-handover backend for a three-person studio.

Only the Python standard library is required.  Run:

    python3 server.py --port 8080 --db ./data/diskhandover.db

The service implements the "whole-disk quote" product: a quote is a durable,
versioned, expiring manifest.  Ownership and quota effects happen only when the
quote is confirmed in one SQLite transaction.  Retrying a confirmed quote with
the same handover number is an idempotent replay.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    quota_cells INTEGER NOT NULL CHECK (quota_cells >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    size_cells INTEGER NOT NULL CHECK (size_cells > 0),
    transferable INTEGER NOT NULL DEFAULT 1 CHECK (transferable IN (0, 1)),
    current_version INTEGER NOT NULL DEFAULT 1,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_owner ON files(owner_id);

CREATE TABLE IF NOT EXISTS file_versions (
    file_id TEXT NOT NULL REFERENCES files(id),
    version INTEGER NOT NULL,
    content_text TEXT NOT NULL DEFAULT '',
    size_cells INTEGER NOT NULL,
    actor_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (file_id, version)
);

CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES entries(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('folder', 'file', 'shortcut', 'alias')),
    file_id TEXT REFERENCES files(id),
    target_entry_id TEXT REFERENCES entries(id),
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(parent_id);
CREATE INDEX IF NOT EXISTS idx_entries_file ON entries(file_id);
CREATE INDEX IF NOT EXISTS idx_entries_target ON entries(target_entry_id);

CREATE TABLE IF NOT EXISTS grants (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    file_id TEXT NOT NULL REFERENCES files(id),
    can_read INTEGER NOT NULL DEFAULT 0 CHECK (can_read IN (0, 1)),
    can_write INTEGER NOT NULL DEFAULT 0 CHECK (can_write IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, file_id)
);

CREATE TABLE IF NOT EXISTS handovers (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'whole_quote',
    from_account_id TEXT NOT NULL REFERENCES accounts(id),
    to_account_id TEXT NOT NULL REFERENCES accounts(id),
    root_entry_id TEXT NOT NULL REFERENCES entries(id),
    status TEXT NOT NULL CHECK (status IN
        ('quoted', 'accepted', 'expired', 'revoked', 'stale')),
    total_cells INTEGER NOT NULL DEFAULT 0,
    unique_file_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    accepted_at TEXT,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_handovers_from ON handovers(from_account_id);
CREATE INDEX IF NOT EXISTS idx_handovers_to ON handovers(to_account_id);

CREATE TABLE IF NOT EXISTS handover_items (
    handover_id TEXT NOT NULL REFERENCES handovers(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL REFERENCES files(id),
    disposition TEXT NOT NULL CHECK (disposition IN
        ('transfer', 'borrowed', 'blocked', 'recipient_owned')),
    item_status TEXT NOT NULL DEFAULT 'quoted'
        CHECK (item_status IN ('quoted', 'transferred', 'not_transferred')),
    size_cells INTEGER NOT NULL,
    quoted_version INTEGER NOT NULL,
    quoted_owner_id TEXT NOT NULL,
    paths_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (handover_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_handover_items_file ON handover_items(file_id);

CREATE TABLE IF NOT EXISTS handover_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id TEXT NOT NULL REFERENCES handovers(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_handover_events_ho
    ON handover_events(handover_id, id);

CREATE TABLE IF NOT EXISTS ownership_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id TEXT REFERENCES handovers(id),
    file_id TEXT NOT NULL REFERENCES files(id),
    from_owner_id TEXT NOT NULL REFERENCES accounts(id),
    to_owner_id TEXT NOT NULL REFERENCES accounts(id),
    size_cells INTEGER NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ownership_file ON ownership_ledger(file_id, id);
"""


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(6)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def immediate(db: sqlite3.Connection) -> Iterable[sqlite3.Connection]:
    """Serialize all state-changing transactions with SQLite's write lock."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.isolation_level = None
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 15000")
    db.execute("PRAGMA synchronous = FULL")
    # WAL is useful if the demo opens a second inspection connection.
    try:
        db.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return db


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA_SQL)


def reset_db(db: sqlite3.Connection, scenario: str | None = None) -> dict[str, Any]:
    with immediate(db):
        db.executescript(
            """
            DROP TABLE IF EXISTS ownership_ledger;
            DROP TABLE IF EXISTS handover_events;
            DROP TABLE IF EXISTS handover_items;
            DROP TABLE IF EXISTS handovers;
            DROP TABLE IF EXISTS grants;
            DROP TABLE IF EXISTS file_versions;
            DROP TABLE IF EXISTS entries;
            DROP TABLE IF EXISTS files;
            DROP TABLE IF EXISTS accounts;
            """
        )
        db.executescript(SCHEMA_SQL)
    if scenario:
        return seed_scenario(db, scenario)
    return {"scenario": None, "seeded": False}


# ---------------------------------------------------------------------------
# Row conversion and small domain helpers
# ---------------------------------------------------------------------------


def account_dict(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    used = db.execute(
        """
        SELECT COALESCE(SUM(size_cells), 0) AS used
        FROM files
        WHERE owner_id = ? AND deleted_at IS NULL
        """,
        (row["id"],),
    ).fetchone()["used"]
    quota = int(row["quota_cells"])
    used = int(used)
    return {
        "id": row["id"],
        "name": row["name"],
        "quota_cells": quota,
        "used_cells": used,
        "free_cells": quota - used,
        "can_add_one_cell": quota - used >= 1,
        "created_at": row["created_at"],
    }


def get_account(db: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        raise ApiError(404, "account_not_found", f"账号不存在: {account_id}")
    return row


def require_account(db: sqlite3.Connection, account_id: str | None) -> sqlite3.Row:
    if not account_id:
        raise ApiError(400, "account_id_required", "该调用需要账号标识")
    return get_account(db, account_id)


def get_entry(db: sqlite3.Connection, entry_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise ApiError(404, "entry_not_found", f"目录入口不存在: {entry_id}")
    return row


def require_folder(db: sqlite3.Connection, entry_id: str | None) -> sqlite3.Row:
    if not entry_id:
        raise ApiError(400, "parent_entry_id_required", "缺少父文件夹入口")
    row = get_entry(db, entry_id)
    if row["kind"] != "folder":
        raise ApiError(409, "not_a_folder", f"入口不是文件夹: {entry_id}")
    return row


def get_file(db: sqlite3.Connection, file_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM files WHERE id = ? AND deleted_at IS NULL", (file_id,)
    ).fetchone()
    if not row:
        raise ApiError(404, "file_not_found", f"文件不存在或已删除: {file_id}")
    return row


def file_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "owner_id": row["owner_id"],
        "size_cells": row["size_cells"],
        "transferable": bool(row["transferable"]),
        "current_version": row["current_version"],
        "deleted_at": row["deleted_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def quota_snapshot(
    db: sqlite3.Connection, account_id: str, required_cells: int = 0
) -> dict[str, Any]:
    row = get_account(db, account_id)
    result = account_dict(db, row)
    result["required_cells"] = int(required_cells)
    result["fits"] = result["free_cells"] >= required_cells
    result["shortfall_cells"] = max(0, required_cells - result["free_cells"])
    return result


def assert_actor_owns(
    actor_id: str | None, owner_id: str, action: str = "操作"
) -> None:
    # No X-Account-Id/actor means the trusted local console is calling.
    if actor_id and actor_id != owner_id:
        raise ApiError(
            403,
            "not_owner",
            f"身份 {actor_id} 不能{action}属于 {owner_id} 的资源；浏览、编辑和交接是不同授权",
            {"actor_id": actor_id, "owner_id": owner_id},
        )


def can_write_file(db: sqlite3.Connection, actor_id: str | None, file_row: sqlite3.Row) -> bool:
    if not actor_id or actor_id == file_row["owner_id"]:
        return True
    grant = db.execute(
        "SELECT can_read, can_write FROM grants WHERE account_id=? AND file_id=?",
        (actor_id, file_row["id"]),
    ).fetchone()
    return bool(grant and grant["can_write"])


def can_read_file(db: sqlite3.Connection, actor_id: str | None, file_row: sqlite3.Row) -> bool:
    if not actor_id or actor_id == file_row["owner_id"]:
        return True
    grant = db.execute(
        "SELECT can_read, can_write FROM grants WHERE account_id=? AND file_id=?",
        (actor_id, file_row["id"]),
    ).fetchone()
    return bool(grant and (grant["can_read"] or grant["can_write"]))


# ---------------------------------------------------------------------------
# Graph reachability: folders, multiple links to one file, and back-links
# ---------------------------------------------------------------------------


def scan_reachable(db: sqlite3.Connection, root_entry_id: str) -> dict[str, Any]:
    """Return unique physical files and every observed logical path.

    Entry IDs are cycle-guarded.  File entries are collected on every logical
    route, while aggregation by file ID prevents a shortcut/alias from making
    the same physical file count twice.
    """
    root = get_entry(db, root_entry_id)
    files: dict[str, dict[str, Any]] = {}
    visited_containers: set[str] = set()
    shortcut_hops = 0
    max_hops = 10000

    def add_file(file_id: str, logical_path: str, entry_id: str) -> None:
        row = db.execute(
            "SELECT * FROM files WHERE id=? AND deleted_at IS NULL", (file_id,)
        ).fetchone()
        if not row:
            return
        bucket = files.setdefault(
            file_id,
            {"file": row, "paths": [], "entry_ids": []},
        )
        if logical_path not in bucket["paths"]:
            bucket["paths"].append(logical_path)
        if entry_id not in bucket["entry_ids"]:
            bucket["entry_ids"].append(entry_id)

    def walk(entry_id: str, logical_path: str, depth: int = 0) -> None:
        nonlocal shortcut_hops
        if depth > 100:
            raise ApiError(409, "path_too_deep", "目录/快捷入口嵌套过深")
        row = get_entry(db, entry_id)
        kind = row["kind"]

        if kind == "file":
            if row["file_id"]:
                add_file(row["file_id"], logical_path, entry_id)
            return

        # Folders and links are guarded so an alias back to an ancestor cannot
        # send the walker into an infinite loop.
        if entry_id in visited_containers:
            return
        visited_containers.add(entry_id)

        if kind == "folder":
            children = db.execute(
                "SELECT * FROM entries WHERE parent_id=? ORDER BY position, rowid",
                (entry_id,),
            ).fetchall()
            for child in children:
                child_path = f"{logical_path.rstrip('/')}/{child['name']}"
                walk(child["id"], child_path, depth + 1)
            return

        if kind in ("shortcut", "alias"):
            shortcut_hops += 1
            if shortcut_hops > max_hops:
                raise ApiError(409, "too_many_shortcut_hops", "快捷入口展开次数过多")
            target_id = row["target_entry_id"]
            if not target_id:
                return
            target = get_entry(db, target_id)
            # The shortcut's own path becomes the path prefix through which the
            # target is observed.  A file target is therefore listed under the
            # alias name.
            walk(target["id"], logical_path, depth + 1)

    root_path = "/" + root["name"].strip("/")
    walk(root_entry_id, root_path)

    result_files = []
    for file_id, bucket in files.items():
        row = bucket["file"]
        result_files.append(
            {
                "file_id": file_id,
                "size_cells": int(row["size_cells"]),
                "owner_id": row["owner_id"],
                "current_version": int(row["current_version"]),
                "transferable": bool(row["transferable"]),
                "name": row["name"],
                "paths": bucket["paths"],
                "entry_ids": bucket["entry_ids"],
            }
        )
    result_files.sort(key=lambda x: (x["paths"][0] if x["paths"] else "", x["file_id"]))
    return {
        "root_entry_id": root_entry_id,
        "root_name": root["name"],
        "unique_file_count": len(result_files),
        "total_size_if_all_owned_by_sender": sum(x["size_cells"] for x in result_files),
        "files": result_files,
        "visited_entry_count": len(visited_containers),
    }


# ---------------------------------------------------------------------------
# Request operations
# ---------------------------------------------------------------------------


def create_account(db: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    account_id = body.get("id") or new_id("acc_")
    name = body.get("name")
    if not name:
        raise ApiError(400, "name_required", "账号需要 name")
    try:
        quota = int(body.get("quota_cells", body.get("quota", 10)))
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_quota", "quota_cells 必须是整数格数")
    if quota < 0:
        raise ApiError(400, "invalid_quota", "quota_cells 不能为负")
    ts = now_iso()
    with immediate(db):
        try:
            db.execute(
                "INSERT INTO accounts(id,name,quota_cells,created_at) VALUES(?,?,?,?)",
                (account_id, name, quota, ts),
            )
        except sqlite3.IntegrityError as exc:
            raise ApiError(
                409, "account_exists", f"账号 id 或名称已存在: {account_id}/{name}"
            ) from exc
        row = get_account(db, account_id)
        return account_dict(db, row)


def create_folder(db: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    entry_id = body.get("id") or new_id("ent_folder_")
    name = body.get("name")
    if not name:
        raise ApiError(400, "name_required", "文件夹需要 name")
    parent_id = body.get("parent_entry_id", body.get("parent_id"))
    if parent_id:
        require_folder(db, parent_id)
    ts = now_iso()
    with immediate(db):
        try:
            db.execute(
                """
                INSERT INTO entries(id,parent_id,name,kind,position,created_at)
                VALUES(?,?,?,'folder',?,?)
                """,
                (entry_id, parent_id, name, int(body.get("position", 0)), ts),
            )
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "entry_exists", f"入口 ID 已存在: {entry_id}") from exc
        row = get_entry(db, entry_id)
        return dict(row)


def create_shortcut(db: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    entry_id = body.get("id") or new_id("ent_link_")
    name = body.get("name")
    target_id = body.get("target_entry_id")
    parent_id = body.get("parent_entry_id", body.get("parent_id"))
    kind = body.get("kind", "shortcut")
    if kind not in ("shortcut", "alias"):
        raise ApiError(400, "invalid_kind", "快捷入口 kind 应为 shortcut/alias")
    if not name:
        raise ApiError(400, "name_required", "快捷入口需要 name")
    require_folder(db, parent_id)
    if not target_id:
        raise ApiError(400, "target_entry_id_required", "快捷入口需要 target_entry_id")
    get_entry(db, target_id)
    ts = now_iso()
    with immediate(db):
        try:
            db.execute(
                """
                INSERT INTO entries(id,parent_id,name,kind,target_entry_id,
                                    position,created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    entry_id,
                    parent_id,
                    name,
                    kind,
                    target_id,
                    int(body.get("position", 0)),
                    ts,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "entry_exists", f"入口 ID 已存在: {entry_id}") from exc
        return dict(get_entry(db, entry_id))


def create_file_link(db: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    """Attach an existing physical file under another folder entry."""
    entry_id = body.get("id") or body.get("entry_id") or new_id("ent_filelink_")
    file_id = body.get("file_id")
    parent_id = body.get("parent_entry_id", body.get("parent_id"))
    file_row = get_file(db, file_id)
    require_folder(db, parent_id)
    name = body.get("name") or body.get("entry_name") or file_row["name"]
    ts = now_iso()
    with immediate(db):
        try:
            db.execute(
                """
                INSERT INTO entries(id,parent_id,name,kind,file_id,
                                    position,created_at)
                VALUES(?,?,?,'file',?,?,?)
                """,
                (
                    entry_id,
                    parent_id,
                    name,
                    file_id,
                    int(body.get("position", 0)),
                    ts,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "entry_exists", f"入口 ID 已存在: {entry_id}") from exc
        return dict(get_entry(db, entry_id))


def create_upload(db: sqlite3.Connection, body: dict[str, Any], actor_id: str | None) -> dict[str, Any]:
    owner_id = body.get("owner_id") or body.get("account_id") or actor_id
    if not owner_id:
        raise ApiError(400, "owner_id_required", "普通上传需要 owner_id/account_id")
    assert_actor_owns(actor_id, owner_id, "向该账号空间上传")
    owner = require_account(db, owner_id)

    name = body.get("name")
    if not name:
        raise ApiError(400, "name_required", "上传文件需要 name")
    try:
        size = int(body.get("size_cells", body.get("size", 1)))
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_size", "size_cells 必须是正整数格数")
    if size <= 0:
        raise ApiError(400, "invalid_size", "size_cells 必须大于 0")

    parent_id = body.get("parent_entry_id", body.get("parent_id"))
    if parent_id:
        require_folder(db, parent_id)

    file_id = body.get("file_id") or body.get("id") or new_id("file_")
    entry_id = body.get("entry_id") or new_id("ent_file_")
    content = body.get("content", body.get("content_text", ""))
    transferable = 1 if body.get("transferable", True) else 0
    ts = now_iso()

    with immediate(db):
        # Re-check inside the write transaction so concurrent uploads and
        # confirmations cannot both spend the same free cells.
        used = db.execute(
            "SELECT COALESCE(SUM(size_cells),0) AS used FROM files "
            "WHERE owner_id=? AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()["used"]
        free = int(owner["quota_cells"]) - int(used)
        if int(used) + size > int(owner["quota_cells"]):
            snap = quota_snapshot(db, owner_id, size)
            raise ApiError(
                409,
                "quota_exceeded",
                f"上传失败：{owner['name']} 剩余 {free} 格，文件需要 {size} 格",
                {"account": snap, "upload_size_cells": size},
            )
        try:
            db.execute(
                """
                INSERT INTO files(id,owner_id,name,size_cells,transferable,
                                  current_version,created_at,updated_at)
                VALUES(?,?,?,?,?,1,?,?)
                """,
                (file_id, owner_id, name, size, transferable, ts, ts),
            )
            db.execute(
                """
                INSERT INTO file_versions(file_id,version,content_text,
                                          size_cells,actor_id,created_at)
                VALUES(?,1,?,?,?,?)
                """,
                (file_id, content, size, actor_id or owner_id, ts),
            )
            if parent_id:
                db.execute(
                    """
                    INSERT INTO entries(id,parent_id,name,kind,file_id,
                                        position,created_at)
                    VALUES(?,?,?,'file',?,?,?)
                    """,
                    (
                        entry_id,
                        parent_id,
                        body.get("entry_name", name),
                        file_id,
                        int(body.get("position", 0)),
                        ts,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ApiError(
                409, "file_or_entry_exists", f"文件/入口 ID 已存在: {exc}"
            ) from exc

        file_row = get_file(db, file_id)
        account = quota_snapshot(db, owner_id)
        return {
            "uploaded": file_dict(file_row),
            "entry_id": entry_id if parent_id else None,
            "account": account,
        }


def edit_file(
    db: sqlite3.Connection,
    file_id: str,
    body: dict[str, Any],
    actor_id: str | None,
) -> dict[str, Any]:
    file_row = get_file(db, file_id)
    if not can_write_file(db, actor_id, file_row):
        raise ApiError(
            403,
            "write_permission_required",
            "只有文件主或被明确授予可写权限的账号能改内容；浏览授权不等于可改",
            {"file_id": file_id, "actor_id": actor_id},
        )

    content = body.get("content", body.get("content_text", ""))
    new_size = body.get("size_cells", body.get("size"))
    ts = now_iso()

    with immediate(db):
        row = get_file(db, file_id)
        size = int(row["size_cells"])
        if new_size is not None:
            try:
                size = int(new_size)
            except (TypeError, ValueError):
                raise ApiError(400, "invalid_size", "size_cells 必须是正整数")
            if size <= 0:
                raise ApiError(400, "invalid_size", "size_cells 必须大于 0")
            used = db.execute(
                "SELECT COALESCE(SUM(size_cells),0) AS used FROM files "
                "WHERE owner_id=? AND deleted_at IS NULL",
                (row["owner_id"],),
            ).fetchone()["used"]
            future_used = int(used) - int(row["size_cells"]) + size
            quota = get_account(db, row["owner_id"])["quota_cells"]
            if future_used > quota:
                raise ApiError(
                    409,
                    "quota_exceeded",
                    "保存修改后的大小会超出空间",
                    quota_snapshot(db, row["owner_id"], size),
                )

        next_version = int(row["current_version"]) + 1
        db.execute(
            """
            INSERT INTO file_versions(file_id,version,content_text,size_cells,
                                      actor_id,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (file_id, next_version, content, size, actor_id or row["owner_id"], ts),
        )
        db.execute(
            """
            UPDATE files
               SET current_version=?, size_cells=?, updated_at=?
             WHERE id=?
            """,
            (next_version, size, ts, file_id),
        )
        return {
            "file": file_dict(get_file(db, file_id)),
            "edit": {
                "new_version": next_version,
                "edited_by": actor_id or row["owner_id"],
                "at": ts,
            },
        }


def delete_file(
    db: sqlite3.Connection, file_id: str, actor_id: str | None
) -> dict[str, Any]:
    row = get_file(db, file_id)
    assert_actor_owns(actor_id, row["owner_id"], "删除文件以释放空间")
    ts = now_iso()
    with immediate(db):
        db.execute("UPDATE files SET deleted_at=?, updated_at=? WHERE id=?", (ts, ts, file_id))
        return {"deleted": True, "file_id": file_id, "deleted_at": ts,
                "account": quota_snapshot(db, row["owner_id"])}


def create_grant(db: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    account_id = body.get("account_id")
    file_id = body.get("file_id")
    require_account(db, account_id)
    file_row = get_file(db, file_id)
    actor_id = body.get("actor_id")
    # If an actor is supplied, only the owner may grant permissions.
    assert_actor_owns(actor_id, file_row["owner_id"], "修改授权")
    can_read = 1 if body.get("can_read", False) else 0
    can_write = 1 if body.get("can_write", False) else 0
    # Write implies read when enforcing, but the stored scopes remain explicit.
    if can_write:
        can_read = 1
    ts = now_iso()
    with immediate(db):
        db.execute(
            """
            INSERT INTO grants(account_id,file_id,can_read,can_write,created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(account_id,file_id) DO UPDATE SET
                can_read=excluded.can_read,
                can_write=excluded.can_write
            """,
            (account_id, file_id, can_read, can_write, ts),
        )
        row = db.execute(
            "SELECT * FROM grants WHERE account_id=? AND file_id=?",
            (account_id, file_id),
        ).fetchone()
        return dict(row)


def read_file_content(
    db: sqlite3.Connection, file_id: str, actor_id: str | None
) -> dict[str, Any]:
    row = get_file(db, file_id)
    if not can_read_file(db, actor_id, row):
        raise ApiError(
            403,
            "read_permission_required",
            "没有浏览/下载权限；可浏览不代表可改或可接下用量",
            {"file_id": file_id, "actor_id": actor_id},
        )
    version = db.execute(
        "SELECT * FROM file_versions WHERE file_id=? AND version=?",
        (file_id, row["current_version"]),
    ).fetchone()
    return {
        "file_id": file_id,
        "name": row["name"],
        "current_version": row["current_version"],
        "size_cells": row["size_cells"],
        "content": version["content_text"] if version else "",
        "permissions": {
            "can_read": True,
            "can_write": can_write_file(db, actor_id, row),
            "can_transfer_by_this_call": False,
        },
    }


def create_quote(
    db: sqlite3.Connection, body: dict[str, Any], actor_id: str | None
) -> dict[str, Any]:
    from_id = body.get("from_account_id", body.get("owner_id"))
    to_id = body.get("to_account_id", body.get("recipient_id"))
    root_id = body.get("root_entry_id", body.get("folder_id"))
    quote_id = body.get("id") or body.get("handover_id") or new_id("HO-")
    if not from_id or not to_id or not root_id:
        raise ApiError(
            400,
            "missing_quote_fields",
            "报价需要 from_account_id、to_account_id、root_entry_id",
        )
    if quote_id and db.execute(
        "SELECT 1 FROM handovers WHERE id=?", (quote_id,)
    ).fetchone():
        raise ApiError(409, "handover_exists", f"交接号已存在: {quote_id}")

    from_row = get_account(db, from_id)
    to_row = get_account(db, to_id)
    assert_actor_owns(actor_id, from_id, "发起交接报价")
    get_entry(db, root_id)
    scan = scan_reachable(db, root_id)

    ttl: int | None
    if body.get("expires_at"):
        try:
            expires_dt = parse_time(str(body["expires_at"]))
        except ValueError as exc:
            raise ApiError(400, "invalid_expires_at", "expires_at 必须是 ISO-8601 时间") from exc
        ttl = body.get("ttl_seconds")
    else:
        try:
            ttl = int(body.get("ttl_seconds", body.get("expires_in_seconds", 900)))
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_ttl", "ttl_seconds 必须是整数")
        if ttl < 0:
            raise ApiError(400, "invalid_ttl", "ttl_seconds 不能为负")
        expires_dt = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    created_dt = datetime.now(timezone.utc)
    created = created_dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
    expires = expires_dt.isoformat(timespec="microseconds").replace("+00:00", "Z")

    items: list[tuple[Any, ...]] = []
    total = 0
    for f in scan["files"]:
        owner = f["owner_id"]
        if owner == from_id and f["transferable"]:
            disposition = "transfer"
            total += int(f["size_cells"])
        elif owner == from_id:
            disposition = "blocked"
        elif owner == to_id:
            disposition = "recipient_owned"
        else:
            disposition = "borrowed"
        items.append(
            (
                quote_id,
                f["file_id"],
                disposition,
                f["size_cells"],
                f["current_version"],
                owner,
                json_dumps(f["paths"]),
            )
        )

    with immediate(db):
        db.execute(
            """
            INSERT INTO handovers(id,mode,from_account_id,to_account_id,
                                  root_entry_id,status,total_cells,
                                  unique_file_count,expires_at,created_at)
            VALUES(?, 'whole_quote', ?,?,?, 'quoted', ?,?,?,?)
            """,
            (
                quote_id,
                from_id,
                to_id,
                root_id,
                total,
                len([x for x in items if x[2] == "transfer"]),
                expires,
                created,
            ),
        )
        db.executemany(
            """
            INSERT INTO handover_items(handover_id,file_id,disposition,
                                       size_cells,quoted_version,
                                       quoted_owner_id,paths_json)
            VALUES(?,?,?,?,?,?,?)
            """,
            items,
        )
        db.execute(
            "INSERT INTO handover_events(handover_id,at,event_type,detail_json)"
            " VALUES(?,?,?,?)",
            (
                quote_id,
                created,
                "quote_created",
                json_dumps(
                    {
                        "ttl_seconds": ttl,
                        "total_cells": total,
                        "reachable_unique_files": scan["unique_file_count"],
                    }
                ),
            ),
        )

    detail = get_handover_detail(db, quote_id)
    detail["pricing_note"] = body.get(
        "note",
        "整盘报价在确认前不转移所有权、不扣容量；确认是唯一归属边界。",
    )
    detail["space_at_quote"] = {
        "from": quota_snapshot(db, from_id),
        "to": quota_snapshot(db, to_id, total),
    }
    return detail


def _add_event(
    db: sqlite3.Connection, handover_id: str, event_type: str, detail: dict[str, Any]
) -> None:
    db.execute(
        "INSERT INTO handover_events(handover_id,at,event_type,detail_json)"
        " VALUES(?,?,?,?)",
        (handover_id, now_iso(), event_type, json_dumps(detail)),
    )


def confirm_quote(
    db: sqlite3.Connection,
    handover_id: str,
    body: dict[str, Any],
    actor_id: str | None,
) -> dict[str, Any]:
    row = db.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
    if not row:
        raise ApiError(404, "handover_not_found", f"交接号不存在: {handover_id}")

    account_id = body.get("account_id") or body.get("to_account_id") or actor_id
    if not account_id:
        raise ApiError(400, "account_id_required", "确认报价需要接收者账号")
    if account_id != row["to_account_id"] or (
        actor_id and actor_id != row["to_account_id"]
    ):
        raise ApiError(
            403,
            "recipient_only",
            "只有报价指定的接收者能确认；可浏览/可编辑不等于有权接下用量",
            {"expected_recipient": row["to_account_id"], "actor": account_id},
        )

    # Accepted orders are always idempotent, including retries after network
    # disconnect or process restart.
    if row["status"] == "accepted":
        detail = get_handover_detail(db, handover_id)
        detail["idempotent_replay"] = True
        detail["message"] = "该交接单已确认；本次重试未再次转移所有权或扣减空间。"
        return detail

    if row["status"] in ("expired", "revoked", "stale"):
        raise ApiError(
            409,
            f"quote_{row['status']}",
            f"交接单状态为 {row['status']}，不能确认；请用新单继续未完成部分",
            get_handover_detail(db, handover_id),
        )

    expires = parse_time(row["expires_at"])
    if datetime.now(timezone.utc) >= expires:
        with immediate(db):
            db.execute(
                "UPDATE handovers SET status='expired' WHERE id=? AND status='quoted'",
                (handover_id,),
            )
            _add_event(db, handover_id, "quote_expired", {})
        raise ApiError(
            410,
            "quote_expired",
            "报价已过失效时刻；请重新报价并以新交接号办理",
            {"handover_id": handover_id, "expires_at": row["expires_at"]},
        )

    with immediate(db):
        row = db.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
        if row["status"] == "accepted":
            detail = get_handover_detail(db, handover_id)
            detail["idempotent_replay"] = True
            return detail

        items = db.execute(
            "SELECT * FROM handover_items WHERE handover_id=? "
            "AND disposition='transfer' ORDER BY rowid",
            (handover_id,),
        ).fetchall()

        pending_files: list[sqlite3.Row] = []
        already_recipient_files: list[sqlite3.Row] = []
        conflicts: list[dict[str, Any]] = []
        for item in items:
            f = db.execute(
                "SELECT * FROM files WHERE id=? AND deleted_at IS NULL",
                (item["file_id"],),
            ).fetchone()
            if not f:
                conflicts.append(
                    {"file_id": item["file_id"], "code": "file_missing",
                     "message": "报价中的文件已不存在"}
                )
                continue

            version_ok = int(f["current_version"]) == int(item["quoted_version"])
            size_ok = int(f["size_cells"]) == int(item["size_cells"])
            if f["owner_id"] == row["to_account_id"]:
                # Another order/retry may already have moved this unique file
                # to the same recipient.  Settle the line without charging or
                # writing a second ownership-ledger row.
                if not version_ok or not size_ok:
                    conflicts.append(
                        {
                            "file_id": f["id"],
                            "name": f["name"],
                            "codes": [
                                "version_changed" if not version_ok else None,
                                "size_changed" if not size_ok else None,
                            ],
                            "quoted_version": item["quoted_version"],
                            "actual_version": f["current_version"],
                            "quoted_owner_id": item["quoted_owner_id"],
                            "actual_owner_id": f["owner_id"],
                        }
                    )
                    conflicts[-1]["codes"] = [
                        x for x in conflicts[-1]["codes"] if x
                    ]
                else:
                    already_recipient_files.append(f)
                continue

            problems = []
            if f["owner_id"] != row["from_account_id"]:
                problems.append("ownership_changed")
            if not version_ok:
                problems.append("version_changed")
            if not size_ok:
                problems.append("size_changed")
            if not f["transferable"]:
                problems.append("transfer_revoked")
            if problems:
                conflicts.append(
                    {
                        "file_id": f["id"],
                        "name": f["name"],
                        "codes": problems,
                        "quoted_version": item["quoted_version"],
                        "actual_version": f["current_version"],
                        "quoted_owner_id": item["quoted_owner_id"],
                        "actual_owner_id": f["owner_id"],
                    }
                )
            else:
                pending_files.append(f)

        if conflicts:
            # The old quote cannot silently settle a changed version/owner.
            # Mark it stale; a newly created quote can finish the current state.
            db.execute(
                "UPDATE handovers SET status='stale' WHERE id=?", (handover_id,)
            )
            for item in items:
                db.execute(
                    "UPDATE handover_items SET item_status='not_transferred' "
                    "WHERE handover_id=? AND file_id=?",
                    (handover_id, item["file_id"]),
                )
            _add_event(
                db,
                handover_id,
                "confirm_rejected_version_or_ownership",
                {"conflicts": conflicts},
            )
            # Persist the stale marker/event before raising; the surrounding
            # context's rollback then has no open transaction to undo.
            db.commit()
            raise ApiError(
                409,
                "quote_stale",
                "报价后文件版本、主人或交接许可已变化；整单未交接，请用新报价单",
                {"conflicts": conflicts, "handover_id": handover_id},
            )

        quoted_total = int(row["total_cells"])
        pending_total = sum(int(f["size_cells"]) for f in pending_files)
        already_total = sum(int(f["size_cells"]) for f in already_recipient_files)
        total = pending_total
        recipient_quota = get_account(db, row["to_account_id"])
        recipient_used = int(
            db.execute(
                "SELECT COALESCE(SUM(size_cells),0) AS used FROM files "
                "WHERE owner_id=? AND deleted_at IS NULL",
                (row["to_account_id"],),
            ).fetchone()["used"]
        )
        recipient_free = int(recipient_quota["quota_cells"]) - recipient_used
        if recipient_free < total:
            other_files = [
                dict(x)
                for x in db.execute(
                    """
                    SELECT id,name,size_cells,current_version,created_at
                    FROM files
                    WHERE owner_id=? AND deleted_at IS NULL
                    ORDER BY created_at, rowid
                    """,
                    (row["to_account_id"],),
                ).fetchall()
            ]
            details = {
                "handover_id": handover_id,
                "required_cells": total,
                "quoted_total_cells": quoted_total,
                "newly_to_transfer_cells": pending_total,
                "already_owned_cells": already_total,
                "recipient_used_cells": recipient_used,
                "recipient_free_cells": recipient_free,
                "shortfall_cells": total - recipient_free,
                "recipient_existing_files": other_files,
                "hint": "接收者可移走/删除提前上传的文件，或提高额度，然后用新单办完。",
            }
            _add_event(db, handover_id, "confirm_blocked_quota", details)
            # Durably record the blocked attempt for both sides, while no
            # ownership update has occurred yet.
            db.commit()
            # Keep status quoted: the same manifest may still be settled before
            # expiry if space is freed.  A new quote is also supported.
            raise ApiError(
                409,
                "quota_exceeded",
                f"整单需要 {total} 格，接收者当前只有 {recipient_free} 格余额；未转移任何文件",
                details,
            )

        borrowed_rows = db.execute(
            "SELECT file_id FROM handover_items "
            "WHERE handover_id=? AND disposition='borrowed'",
            (handover_id,),
        ).fetchall()

        accepted_at = now_iso()
        settlement = {
            "boundary": "transaction_commit_of_confirm",
            "accepted_at": accepted_at,
            "transferred_file_ids": [f["id"] for f in pending_files],
            "already_owned_file_ids": [f["id"] for f in already_recipient_files],
            "borrowed_file_ids": [x["file_id"] for x in borrowed_rows],
            "quoted_total_cells": quoted_total,
            "total_cells": pending_total,
            "newly_transferred_cells": pending_total,
            "already_owned_cells": already_total,
            "from_after": quota_snapshot(db, row["from_account_id"]),
            "to_after": quota_snapshot(db, row["to_account_id"]),
        }

        for f in pending_files:
            db.execute(
                "UPDATE files SET owner_id=?, updated_at=? WHERE id=?",
                (row["to_account_id"], accepted_at, f["id"]),
            )
            db.execute(
                """
                UPDATE handover_items SET item_status='transferred'
                WHERE handover_id=? AND file_id=?
                """,
                (handover_id, f["id"]),
            )
            db.execute(
                """
                INSERT INTO ownership_ledger(handover_id,file_id,from_owner_id,
                                              to_owner_id,size_cells,at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    handover_id,
                    f["id"],
                    row["from_account_id"],
                    row["to_account_id"],
                    int(f["size_cells"]),
                    accepted_at,
                ),
            )

        for f in already_recipient_files:
            # Mark this quote line settled, but do not update ownership or
            # insert another ledger/charge row.
            db.execute(
                """
                UPDATE handover_items SET item_status='transferred'
                WHERE handover_id=? AND file_id=?
                """,
                (handover_id, f["id"]),
            )

        # Third-party files are not transferred or charged, but accepting the
        # handover gives the recipient read-only "borrowed" access.
        for borrowed in borrowed_rows:
            db.execute(
                """
                INSERT INTO grants(account_id,file_id,can_read,can_write,created_at)
                VALUES(?,?,1,0,?)
                ON CONFLICT(account_id,file_id) DO UPDATE SET can_read=1
                """,
                (row["to_account_id"], borrowed["file_id"], accepted_at),
            )

        settlement["from_after"] = quota_snapshot(db, row["from_account_id"])
        settlement["to_after"] = quota_snapshot(db, row["to_account_id"])
        db.execute(
            "UPDATE handovers SET status='accepted', accepted_at=?, result_json=? "
            "WHERE id=?",
            (accepted_at, json_dumps(settlement), handover_id),
        )
        _add_event(db, handover_id, "quote_accepted", settlement)

    detail = get_handover_detail(db, handover_id)
    detail["idempotent_replay"] = False
    detail["message"] = "整单已在一个事务内接下；每份唯一文件只转移/计费一次。"
    return detail


def get_handover_detail(db: sqlite3.Connection, handover_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
    if not row:
        raise ApiError(404, "handover_not_found", f"交接号不存在: {handover_id}")

    item_rows = db.execute(
        "SELECT * FROM handover_items WHERE handover_id=? ORDER BY disposition, rowid",
        (handover_id,),
    ).fetchall()
    manifest: list[dict[str, Any]] = []
    for item in item_rows:
        current = db.execute(
            "SELECT * FROM files WHERE id=?", (item["file_id"],)
        ).fetchone()
        actual_owner = current["owner_id"] if current else None
        manifest.append(
            {
                "file_id": item["file_id"],
                "name": current["name"] if current else None,
                "disposition": item["disposition"],
                "item_status": item["item_status"],
                "size_cells": item["size_cells"],
                "quoted_version": item["quoted_version"],
                "current_version": current["current_version"] if current else None,
                "quoted_owner_id": item["quoted_owner_id"],
                "actual_owner_id": actual_owner,
                "owner_is_actual_after_commit": (
                    actual_owner == row["to_account_id"]
                    if item["disposition"] == "transfer" and row["status"] == "accepted"
                    else actual_owner == item["quoted_owner_id"]
                ),
                "paths": json.loads(item["paths_json"] or "[]"),
            }
        )

    groups: dict[str, list[dict[str, Any]]] = {
        "transfer": [],
        "borrowed": [],
        "blocked": [],
        "recipient_owned": [],
    }
    for item in manifest:
        groups[item["disposition"]].append(item)

    events = [
        {
            "at": e["at"],
            "event_type": e["event_type"],
            "detail": json.loads(e["detail_json"] or "{}"),
        }
        for e in db.execute(
            "SELECT * FROM handover_events WHERE handover_id=? ORDER BY id",
            (handover_id,),
        ).fetchall()
    ]
    ledger = [
        {
            "file_id": x["file_id"],
            "from_owner_id": x["from_owner_id"],
            "to_owner_id": x["to_owner_id"],
            "size_cells": x["size_cells"],
            "at": x["at"],
        }
        for x in db.execute(
            "SELECT * FROM ownership_ledger WHERE handover_id=? ORDER BY id",
            (handover_id,),
        ).fetchall()
    ]

    result = json.loads(row["result_json"]) if row["result_json"] else None
    return {
        "handover_number": row["id"],
        "id": row["id"],
        "mode": row["mode"],
        "status": row["status"],
        "from_account_id": row["from_account_id"],
        "to_account_id": row["to_account_id"],
        "root_entry_id": row["root_entry_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "accepted_at": row["accepted_at"],
        "totals": {
            "transfer_cells": row["total_cells"],
            "transferable_unique_files": row["unique_file_count"],
            "manifest_unique_files": len(manifest),
            "borrowed_files": len(groups["borrowed"]),
            "blocked_files": len(groups["blocked"]),
            "recipient_owned_files": len(groups["recipient_owned"]),
        },
        "transfer_items": groups["transfer"],
        "borrowed_items": groups["borrowed"],
        "blocked_items": groups["blocked"],
        "recipient_owned_items": groups["recipient_owned"],
        "manifest": manifest,
        "ownership_ledger": ledger,
        "settlement": result,
        "events": events,
        "accounts": {
            "from": quota_snapshot(db, row["from_account_id"]),
            "to": quota_snapshot(db, row["to_account_id"]),
        },
    }


# ---------------------------------------------------------------------------
# Deterministic rebuild scenarios
# ---------------------------------------------------------------------------


def seed_scenario(db: sqlite3.Connection, scenario: str) -> dict[str, Any]:
    name = scenario.lower().replace("-", "_")
    aliases = {
        "base": "standard",
        "two_drafts": "standard",
        "independent": "standard",
        "alias": "alias_draft",
        "shortcut": "alias_draft",
        "same_file_alias": "alias_draft",
        "preupload": "blocked_upload",
        "occupied": "blocked_upload",
        "blocked": "blocked_upload",
    }
    name = aliases.get(name, name)
    if name not in ("standard", "alias_draft", "blocked_upload", "borrowed"):
        raise ApiError(
            400,
            "unknown_scenario",
            "scenario 可为 standard、alias_draft、blocked_upload、borrowed",
        )

    ts = now_iso()
    with immediate(db):
        for aid, aname, quota in (
            ("jia", "甲", 5),
            ("yi", "乙", 5),
            ("bing", "丙", 5),
        ):
            db.execute(
                "INSERT INTO accounts(id,name,quota_cells,created_at) VALUES(?,?,?,?)",
                (aid, aname, quota, ts),
            )

        # An ancestor/root and the project folder.  The back-link targets root.
        db.execute(
            "INSERT INTO entries(id,parent_id,name,kind,position,created_at)"
            " VALUES('ent_root',NULL,'甲的盘根','folder',0,?)",
            (ts,),
        )
        db.execute(
            "INSERT INTO entries(id,parent_id,name,kind,position,created_at)"
            " VALUES('ent_project','ent_root','甲的项目夹','folder',1,?)",
            (ts,),
        )
        db.execute(
            "INSERT INTO entries(id,parent_id,name,kind,target_entry_id,position,created_at)"
            " VALUES('ent_back_to_root','ent_project','返回入口','shortcut','ent_root',99,?)",
            (ts,),
        )

        def add_file(fid: str, size: int, fname: str, owner: str = "jia",
                     transferable: bool = True, content: str | None = None) -> None:
            db.execute(
                """
                INSERT INTO files(id,owner_id,name,size_cells,transferable,
                                  current_version,created_at,updated_at)
                VALUES(?,?,?,?,?,1,?,?)
                """,
                (fid, owner, fname, size, 1 if transferable else 0, ts, ts),
            )
            db.execute(
                """
                INSERT INTO file_versions(file_id,version,content_text,
                                          size_cells,actor_id,created_at)
                VALUES(?,1,?,?,?,?)
                """,
                (fid, content if content is not None else fname + " v1",
                 size, owner, ts),
            )

        def file_entry(eid: str, fid: str, ename: str, pos: int) -> None:
            db.execute(
                """
                INSERT INTO entries(id,parent_id,name,kind,file_id,position,created_at)
                VALUES(?,'ent_project',?,'file',?,?,?)
                """,
                (eid, ename, fid, pos, ts),
            )

        add_file("file_draft_san", 3, "三格稿", content="三格稿 v1")
        file_entry("ent_draft_san", "file_draft_san", "三格稿", 1)

        if name == "alias_draft":
            # The former two-cell draft entry is replaced by an alias to the
            # same physical three-cell draft.  There is no second file row.
            db.execute(
                """
                INSERT INTO entries(id,parent_id,name,kind,target_entry_id,
                                    position,created_at)
                VALUES('ent_alias_to_san','ent_project',
                       '两格稿位置（别名→三格稿）','alias','ent_draft_san',2,?)
                """,
                (ts,),
            )
        else:
            add_file("file_draft_er", 2, "两格稿", content="两格稿 v1")
            file_entry("ent_draft_er", "file_draft_er", "两格稿", 2)

        if name == "blocked_upload":
            # B has already spent one cell through the ordinary upload path.
            db.execute(
                "INSERT INTO entries(id,parent_id,name,kind,position,created_at)"
                " VALUES('ent_yi_root',NULL,'乙的盘根','folder',0,?)",
                (ts,),
            )
            add_file(
                "file_yi_blocker",
                1,
                "乙提前上传的一格文件",
                owner="yi",
                content="blocker",
            )
            db.execute(
                """
                INSERT INTO entries(id,parent_id,name,kind,file_id,position,created_at)
                VALUES('ent_yi_blocker','ent_yi_root','乙提前上传的一格文件',
                       'file','file_yi_blocker',1,?)
                """,
                (ts,),
            )

        if name == "borrowed":
            add_file(
                "file_bing_reference",
                4,
                "丙所有的参考件",
                owner="bing",
                content="third-party",
            )
            file_entry("ent_bing_reference", "file_bing_reference", "丙的参考件（借阅）", 3)
            add_file(
                "file_jia_locked",
                1,
                "甲所有但禁止交接的稿",
                owner="jia",
                transferable=False,
                content="locked",
            )
            file_entry("ent_jia_locked", "file_jia_locked", "禁止交接件", 4)

    return {
        "scenario": name,
        "seeded": True,
        "accounts": {
            "jia": "甲，额度 5 格",
            "yi": "乙，额度 5 格",
            "bing": "丙，额度 5 格",
        },
        "root_entry_id": "ent_root",
        "project_entry_id": "ent_project",
        "handover_quote_example": {
            "method": "POST",
            "path": "/v1/handovers",
            "body": {
                "id": "HO-EXAMPLE",
                "from_account_id": "jia",
                "to_account_id": "yi",
                "root_entry_id": "ent_project",
                "ttl_seconds": 3600,
            },
        },
    }


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "DiskHandover/1.0"
    # The server object gets a db_path; each request opens a short connection.
    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep test output clean; errors still return JSON.
        if os.environ.get("DH_HTTP_LOG", "0") == "1":
            super().log_message(fmt, *args)

    @property
    def db_path(self) -> str:
        return self.server.db_path  # type: ignore[attr-defined]

    def _actor(self, body: dict[str, Any]) -> str | None:
        header = self.headers.get("X-Account-Id")
        query = parse_qs(urlparse(self.path).query).get("as", [None])[0]
        return header or query or body.get("actor_id")

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "invalid_json", f"请求体不是合法 JSON: {exc}")
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
        return value

    def handle_one(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        body = self._read_body() if method in ("POST", "PUT", "PATCH") else {}
        actor = self._actor(body)
        db = connect(self.db_path)
        try:
            response = self.route(method, path, parsed.query, body, actor, db)
            status = 200
            if isinstance(response, tuple):
                status, response = response
            self._send_json(status, response)
        except ApiError as exc:
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message,
                           "details": exc.details}},
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            self._send_json(
                500,
                {"error": {"code": "internal_error", "message": str(exc)}},
            )
        finally:
            db.close()

    def do_GET(self) -> None:  # noqa: N802
        self.handle_one("GET")

    def do_POST(self) -> None:  # noqa: N802
        self.handle_one("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self.handle_one("DELETE")

    def route(
        self,
        method: str,
        path: str,
        query: str,
        body: dict[str, Any],
        actor: str | None,
        db: sqlite3.Connection,
    ) -> Any:
        qs = parse_qs(query)

        if method == "GET" and path in ("/", "/health"):
            return {
                "service": "disk-handover-backend",
                "ok": True,
                "time": now_iso(),
                "product": "whole_quote",
                "endpoints": [
                    "POST /v1/admin/reset",
                    "POST /v1/admin/rebuild",
                    "POST /v1/accounts",
                    "GET  /v1/accounts/{id}",
                    "POST /v1/folders",
                    "POST /v1/shortcuts",
                    "POST /v1/file-links",
                    "POST /v1/uploads",
                    "GET  /v1/files/{id}/content",
                    "POST /v1/files/{id}/versions",
                    "DELETE /v1/files/{id}",
                    "POST /v1/grants",
                    "GET  /v1/scan?root_entry_id=...",
                    "POST /v1/handovers",
                    "POST /v1/handovers/{id}/confirm",
                    "GET  /v1/handovers/{id}",
                ],
            }

        if method == "POST" and path in (
            "/v1/admin/reset",
            "/v1/admin/rebuild",
            "/api/reset",
            "/reset",
            "/rebuild",
        ):
            scenario = body.get("scenario", body.get("seed"))
            if scenario is True:
                scenario = "standard"
            if scenario in (False, None, ""):
                scenario = None
            return (200, reset_db(db, scenario))

        if method == "POST" and path in ("/v1/accounts", "/api/accounts", "/accounts"):
            return (201, create_account(db, body))

        if method == "GET" and path == "/v1/accounts":
            rows = db.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            return [account_dict(db, r) for r in rows]

        if path.startswith("/v1/accounts/"):
            aid = path.rsplit("/", 1)[-1]
            if method == "GET":
                return account_dict(db, get_account(db, aid))

        if method == "POST" and path in ("/v1/folders", "/v1/entries/folders"):
            return (201, create_folder(db, body))

        if method == "POST" and path in (
            "/v1/shortcuts",
            "/v1/aliases",
            "/v1/entries/shortcuts",
        ):
            return (201, create_shortcut(db, body))

        if method == "POST" and path in (
            "/v1/file-links",
            "/v1/entries/file-links",
        ):
            return (201, create_file_link(db, body))

        if method == "POST" and path in ("/v1/uploads", "/v1/files", "/api/uploads", "/upload"):
            return (201, create_upload(db, body, actor))

        if method == "POST" and path == "/v1/grants":
            return (201, create_grant(db, body))

        if method == "GET" and path == "/v1/grants":
            aid = qs.get("account_id", [None])[0]
            if not aid:
                raise ApiError(400, "account_id_required", "查询授权需要 account_id")
            rows = db.execute(
                "SELECT * FROM grants WHERE account_id=? ORDER BY file_id", (aid,)
            ).fetchall()
            return [dict(r) for r in rows]

        if method == "GET" and path == "/v1/scan":
            root_id = qs.get("root_entry_id", qs.get("root", [None]))[0]
            if not root_id:
                raise ApiError(400, "root_entry_id_required", "需要 root_entry_id")
            return scan_reachable(db, root_id)

        if method == "GET" and path.startswith("/v1/entries/"):
            if path.endswith("/scan"):
                eid = [p for p in path.split("/") if p][2]
                return scan_reachable(db, eid)
            eid = path.rsplit("/", 1)[-1]
            return dict(get_entry(db, eid))

        if path.startswith("/v1/files/"):
            parts = [p for p in path.split("/") if p]
            # ['v1','files',id,...]
            fid = parts[2]
            if len(parts) == 3 and method == "GET":
                return file_dict(get_file(db, fid))
            if len(parts) == 4 and parts[3] == "content" and method == "GET":
                return read_file_content(db, fid, actor)
            if len(parts) == 4 and parts[3] in ("versions", "edit") and method == "POST":
                return edit_file(db, fid, body, actor)
            if len(parts) == 3 and method == "DELETE":
                return delete_file(db, fid, actor)

        if method == "POST" and path in (
            "/v1/handovers",
            "/v1/quotes",
            "/api/handovers",
            "/api/quote",
            "/quote",
        ):
            return (201, create_quote(db, body, actor))

        if path.startswith("/v1/handovers/") or path.startswith("/v1/quotes/"):
            parts = [p for p in path.split("/") if p]
            # v1/handovers/{id} or .../confirm
            hid = parts[2]
            if len(parts) == 3 and method == "GET":
                return get_handover_detail(db, hid)
            if len(parts) == 4 and parts[3] == "ownership" and method == "GET":
                detail = get_handover_detail(db, hid)
                return {
                    "handover_number": detail["handover_number"],
                    "status": detail["status"],
                    "owners": [
                        {
                            "file_id": x["file_id"],
                            "name": x["name"],
                            "disposition": x["disposition"],
                            "size_cells": x["size_cells"],
                            "actual_owner_id": x["actual_owner_id"],
                            "quoted_owner_id": x["quoted_owner_id"],
                            "current_version": x["current_version"],
                            "paths": x["paths"],
                        }
                        for x in detail["manifest"]
                    ],
                }
            if len(parts) == 4 and parts[3] in ("confirm", "accept") and method == "POST":
                return confirm_quote(db, hid, body, actor)

        if path.startswith("/api/handovers/"):
            parts = [p for p in path.split("/") if p]
            hid = parts[2]
            if len(parts) == 3 and method == "GET":
                return get_handover_detail(db, hid)
            if len(parts) == 4 and parts[3] == "confirm" and method == "POST":
                return confirm_quote(db, hid, body, actor)

        # A few flat aliases that make shell notes shorter.
        if path.startswith("/handovers/"):
            parts = [p for p in path.split("/") if p]
            hid = parts[1]
            if len(parts) == 2 and method == "GET":
                return get_handover_detail(db, hid)
            if len(parts) == 3 and parts[2] == "confirm" and method == "POST":
                return confirm_quote(db, hid, body, actor)

        raise ApiError(404, "route_not_found", f"没有该路由: {method} {path}")


def build_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    init_db(connect(db_path))
    server = ThreadingHTTPServer((host, port), Handler)
    server.db_path = db_path  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Disk handover backend")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "DB_PATH",
            str(Path(__file__).resolve().parent / "data" / "diskhandover.db"),
        ),
    )
    args = parser.parse_args()
    server = build_server(args.host, args.port, args.db)
    print(f"disk-handover listening on http://{args.host}:{args.port}", flush=True)
    print(f"sqlite database: {args.db}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
