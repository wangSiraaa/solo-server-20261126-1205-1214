#!/usr/bin/env python3
"""Local community-forum backend with stable moves, versioned read receipts,
two subscription conventions, and durable screen-pagination tokens.

Only the Python standard library is required.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

DB_PATH = os.environ.get("FORUM_DB", str(Path(__file__).with_name("forum.db")))
PORT = int(os.environ.get("PORT", "8787"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  password_digest TEXT NOT NULL,
  api_token TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK (visibility IN ('public','private')),
  created_by TEXT NOT NULL REFERENCES users(id),
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members (
  group_id TEXT NOT NULL REFERENCES groups(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  role TEXT NOT NULL CHECK (role IN ('owner','moderator','member')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS topics (
  id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL REFERENCES groups(id),
  title TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL UNIQUE,
  path TEXT NOT NULL UNIQUE,
  topic_id TEXT NOT NULL REFERENCES topics(id),
  group_id TEXT NOT NULL REFERENCES groups(id),
  parent_id TEXT REFERENCES posts(id),
  root_id TEXT NOT NULL,
  depth INTEGER NOT NULL,
  author_id TEXT NOT NULL REFERENCES users(id),
  title TEXT,
  body TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  board_arrival_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_group_arrival ON posts(group_id, board_arrival_seq, path);
CREATE INDEX IF NOT EXISTS idx_posts_parent ON posts(parent_id);
CREATE INDEX IF NOT EXISTS idx_posts_path ON posts(path);

CREATE TABLE IF NOT EXISTS post_versions (
  post_id TEXT NOT NULL REFERENCES posts(id),
  version INTEGER NOT NULL,
  title TEXT,
  body TEXT NOT NULL,
  editor_id TEXT NOT NULL REFERENCES users(id),
  event_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (post_id, version)
);

CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  actor_id TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS moves (
  id TEXT PRIMARY KEY,
  root_post_id TEXT NOT NULL REFERENCES posts(id),
  post_id TEXT NOT NULL REFERENCES posts(id),
  from_group_id TEXT NOT NULL,
  from_topic_id TEXT NOT NULL,
  to_group_id TEXT NOT NULL,
  to_topic_id TEXT NOT NULL,
  subtree_json TEXT NOT NULL,
  event_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moves_post ON moves(post_id);

CREATE TABLE IF NOT EXISTS subscriptions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL CHECK (kind IN ('branch','board')),
  anchor_post_id TEXT REFERENCES posts(id),
  group_id TEXT REFERENCES groups(id),
  baseline_seq INTEGER NOT NULL DEFAULT 0,
  baseline_receipt_id TEXT,
  created_at TEXT NOT NULL,
  CHECK ((kind='branch' AND anchor_post_id IS NOT NULL AND group_id IS NULL)
      OR (kind='board' AND group_id IS NOT NULL AND anchor_post_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);

CREATE TABLE IF NOT EXISTS page_tokens (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  subscription_id TEXT REFERENCES subscriptions(id),
  context TEXT NOT NULL,
  snapshot_seq INTEGER NOT NULL,
  next_cursor TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_items (
  token TEXT NOT NULL REFERENCES page_tokens(token),
  ordinal INTEGER NOT NULL,
  item_kind TEXT NOT NULL CHECK (item_kind IN ('post','access_required')),
  post_id TEXT NOT NULL REFERENCES posts(id),
  displayed_version INTEGER,
  order_key TEXT NOT NULL,
  PRIMARY KEY (token, ordinal)
);

CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  page_token TEXT REFERENCES page_tokens(token),
  intent TEXT NOT NULL,
  event_seq INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(page_token)
);

CREATE TABLE IF NOT EXISTS receipt_items (
  receipt_id TEXT NOT NULL REFERENCES receipts(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  post_id TEXT NOT NULL REFERENCES posts(id),
  displayed_version INTEGER NOT NULL,
  PRIMARY KEY (receipt_id, post_id)
);
CREATE INDEX IF NOT EXISTS idx_receipt_items_user_post ON receipt_items(user_id, post_id, displayed_version);

CREATE TABLE IF NOT EXISTS access_requests (
  id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL REFERENCES groups(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL CHECK (status IN ('pending','approved','denied')),
  created_at TEXT NOT NULL,
  decided_at TEXT
);
"""


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def password_digest(password: str) -> str:
    return hashlib.sha256(("forum-local-salt:" + password).encode()).hexdigest()


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return "cur_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any]:
    if not cursor:
        return {}
    if not cursor.startswith("cur_"):
        raise ValueError("invalid cursor")
    text = cursor[4:]
    text += "=" * (-len(text) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(text.encode()))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid cursor") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid cursor")
    return payload


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


def add_event(conn: sqlite3.Connection, event_type: str, actor_id: str | None, payload: dict[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO events(type, actor_id, payload_json, created_at) VALUES (?,?,?,?)",
        (event_type, actor_id, json.dumps(payload, ensure_ascii=False), now_iso()),
    )
    return int(cur.lastrowid)


def public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    return {"id": d["id"], "username": d["username"], "display_name": d["display_name"]}


def get_user_by_token(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    return conn.execute("SELECT * FROM users WHERE api_token=?", (token,)).fetchone()


def can_access_group(conn: sqlite3.Connection, user_id: str, group_id: str) -> bool:
    group = conn.execute("SELECT visibility FROM groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        return False
    if group["visibility"] == "public":
        return True
    member = conn.execute(
        "SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (group_id, user_id)
    ).fetchone()
    return member is not None


def require_group_role(
    conn: sqlite3.Connection, user_id: str, group_id: str, roles: tuple[str, ...] = ("owner", "moderator")
) -> None:
    member = conn.execute(
        "SELECT role FROM group_members WHERE group_id=? AND user_id=?", (group_id, user_id)
    ).fetchone()
    if not member or member["role"] not in roles:
        raise ApiError(403, "moderator_required", "需要该小组版主权限")


def effective_receipt(conn: sqlite3.Connection, user_id: str, post_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT r.id AS receipt_id, ri.displayed_version, r.created_at, r.intent, r.page_token
        FROM receipt_items ri
        JOIN receipts r ON r.id = ri.receipt_id
        WHERE ri.user_id=? AND ri.post_id=?
        ORDER BY ri.displayed_version DESC, r.event_seq DESC
        LIMIT 1
        """,
        (user_id, post_id),
    ).fetchone()
    return dict(row) if row else None


def read_state(conn: sqlite3.Connection, user_id: str, post: sqlite3.Row) -> dict[str, Any]:
    receipt = effective_receipt(conn, user_id, post["id"])
    if receipt is None:
        state = "unseen"
        acked = None
    elif receipt["displayed_version"] < post["version"]:
        state = "updated_after_read"
        acked = receipt["displayed_version"]
    else:
        state = "read"
        acked = receipt["displayed_version"]
    return {
        "state": state,
        "acked_version": acked,
        "current_version": post["version"],
        "effective_receipt_id": receipt["receipt_id"] if receipt else None,
        "receipt_created_at": receipt["created_at"] if receipt else None,
        "needs_read": state != "read",
    }


def post_json(
    conn: sqlite3.Connection,
    user: sqlite3.Row,
    post: sqlite3.Row,
    *,
    include_body: bool = True,
) -> dict[str, Any]:
    group = conn.execute("SELECT id, slug, title, visibility FROM groups WHERE id=?", (post["group_id"],)).fetchone()
    topic = conn.execute("SELECT id, title FROM topics WHERE id=?", (post["topic_id"],)).fetchone()
    author = conn.execute("SELECT id, username, display_name FROM users WHERE id=?", (post["author_id"],)).fetchone()
    data: dict[str, Any] = {
        "id": post["id"],
        "version": post["version"],
        "seq": post["seq"],
        "path": post["path"],
        "depth": post["depth"],
        "topic_id": post["topic_id"],
        "group_id": post["group_id"],
        "parent_id": post["parent_id"],
        "root_id": post["root_id"],
        "author": public_user(author),
        "created_at": post["created_at"],
        "updated_at": post["updated_at"],
        "location": {
            "group_id": group["id"],
            "group_slug": group["slug"],
            "topic_id": topic["id"],
            "current_address": f"/groups/{group['slug']}/topics/{topic['id']}#{post['id']}",
            "old_address_lookup": f"/old-addresses/posts/{post['id']}",
        },
        "read": read_state(conn, user["id"], post),
    }
    if include_body:
        data["title"] = post["title"]
        data["body"] = post["body"]
        data["location"]["group_title"] = group["title"]
        data["location"]["topic_title"] = topic["title"]
    return data


def masked_post_tile(conn: sqlite3.Connection, user: sqlite3.Row, post: sqlite3.Row) -> dict[str, Any]:
    """Reveal only what a branch subscription needs; never title/body of a private group."""
    group = conn.execute("SELECT visibility FROM groups WHERE id=?", (post["group_id"],)).fetchone()
    move = conn.execute(
        "SELECT id FROM moves WHERE root_post_id=? ORDER BY event_seq DESC LIMIT 1", (post["id"],)
    ).fetchone()
    return {
        "item_kind": "access_required",
        "post_id": post["id"],
        "accessible": False,
        "access": "request_membership",
        "group_id": post["group_id"],
        "group_visibility": group["visibility"] if group else "private",
        "apply": {
            "method": "POST",
            "url": f"/groups/{post['group_id']}/access-requests",
            "json": {"reason": "followed branch moved here"},
        },
        "move_id": move["id"] if move else None,
        "title": None,
        "body": None,
        "message": "讨论分枝位于你未获准加入的私组；标题和正文已隐藏，仅可申请访问。",
        "read": read_state(conn, user["id"], post),
    }


def get_subscription(conn: sqlite3.Connection, user: sqlite3.Row, sub_id: str) -> sqlite3.Row:
    sub = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    if not sub or sub["user_id"] != user["id"]:
        raise ApiError(404, "subscription_not_found", "订阅不存在")
    return sub


def candidate_posts(conn: sqlite3.Connection, sub: sqlite3.Row) -> list[sqlite3.Row]:
    """Return the subscription's logical inbox in stable reading order.

    Branch subscriptions keep post identity across moves. Board subscriptions
    depend on current group occupancy and the board-arrival event sequence.
    """
    if sub["kind"] == "branch":
        rows = conn.execute(
            """
            WITH RECURSIVE tree(id) AS (
                SELECT id FROM posts WHERE id = ?
                UNION ALL
                SELECT p.id
                FROM posts p
                JOIN tree t ON p.parent_id = t.id
            )
            SELECT p.*
            FROM posts p
            JOIN tree t ON t.id = p.id
            ORDER BY p.path ASC
            """,
            (sub["anchor_post_id"],),
        ).fetchall()
        return list(rows)

    rows = conn.execute(
        """
        SELECT *
        FROM posts
        WHERE group_id = ? AND board_arrival_seq > ?
        ORDER BY board_arrival_seq ASC, path ASC
        """,
        (sub["group_id"], sub["baseline_seq"]),
    ).fetchall()
    return list(rows)


def order_key_for(sub: sqlite3.Row, post: sqlite3.Row) -> tuple[Any, ...]:
    if sub["kind"] == "branch":
        return (post["path"],)
    return (post["board_arrival_seq"], post["path"])


def cursor_matches_after(payload: dict[str, Any], sub: sqlite3.Row, post: sqlite3.Row) -> bool:
    if not payload:
        return True
    key = order_key_for(sub, post)
    try:
        if sub["kind"] == "branch":
            return post["path"] > payload["path"]
        return (post["board_arrival_seq"], post["path"]) > (
            int(payload["arrival_seq"]),
            str(payload["path"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(400, "invalid_cursor", "分页游标已损坏") from exc


def make_next_cursor(sub: sqlite3.Row, post: sqlite3.Row) -> str:
    if sub["kind"] == "branch":
        return encode_cursor({"kind": "branch", "path": post["path"]})
    return encode_cursor(
        {"kind": "board", "arrival_seq": post["board_arrival_seq"], "path": post["path"]}
    )


def serialize_subscription(conn: sqlite3.Connection, sub: sqlite3.Row) -> dict[str, Any]:
    data = dict(sub)
    data["kind_label"] = "follow_branch" if sub["kind"] == "branch" else "board_only"
    if sub["kind"] == "branch":
        anchor = conn.execute(
            "SELECT id, title, body, group_id, topic_id, version FROM posts WHERE id=?",
            (sub["anchor_post_id"],),
        ).fetchone()
        if anchor:
            anchor_data = dict(anchor)
            if can_access_group(conn, sub["user_id"], anchor_data["group_id"]):
                data["anchor"] = anchor_data
            else:
                # Membership can be revoked after the subscription was created.
                # Never let subscription metadata become a private-body side channel.
                data["anchor"] = {
                    "id": anchor_data["id"],
                    "version": anchor_data["version"],
                    "group_id": anchor_data["group_id"],
                    "topic_id": anchor_data["topic_id"],
                    "accessible": False,
                    "title": None,
                    "body": None,
                    "access_required": True,
                    "apply_url": f"/groups/{anchor_data['group_id']}/access-requests",
                }
        else:
            data["anchor"] = None
    else:
        group = conn.execute("SELECT id, slug, title, visibility FROM groups WHERE id=?", (sub["group_id"],)).fetchone()
        if group and can_access_group(conn, sub["user_id"], group["id"]):
            data["group"] = dict(group)
        elif group:
            data["group"] = {
                "id": group["id"],
                "visibility": group["visibility"],
                "title": None,
                "accessible": False,
                "apply_url": f"/groups/{group['id']}/access-requests",
            }
        else:
            data["group"] = None
    return data


def create_page_token(
    conn: sqlite3.Connection,
    user: sqlite3.Row,
    sub_id: str | None,
    context: str,
    shown: list[tuple[sqlite3.Row, bool]],
    next_cursor: str | None,
) -> str:
    token = new_id("pg")
    snapshot = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM posts").fetchone()["s"]
    conn.execute(
        """
        INSERT INTO page_tokens(token, user_id, subscription_id, context, snapshot_seq, next_cursor, created_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (token, user["id"], sub_id, context, snapshot, next_cursor, now_iso()),
    )
    for ordinal, (post, accessible) in enumerate(shown, start=1):
        conn.execute(
            """
            INSERT INTO page_items(token, ordinal, item_kind, post_id, displayed_version, order_key)
            VALUES (?,?,?,?,?,?)
            """,
            (
                token,
                ordinal,
                "post" if accessible else "access_required",
                post["id"],
                post["version"] if accessible else None,
                json.dumps(order_key_for_type(context, post), ensure_ascii=False),
            ),
        )
    return token


def order_key_for_type(context: str, post: sqlite3.Row) -> list[Any]:
    if context.startswith("board:"):
        return [post["board_arrival_seq"], post["path"]]
    return [post["path"]]


class Handler(BaseHTTPRequestHandler):
    server_version = "ForumBackend/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (now_iso(), fmt % args))

    def send_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiError(400, "invalid_json", "请求体不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
        return value

    def authenticate(self, conn: sqlite3.Connection) -> sqlite3.Row:
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else self.headers.get("X-User-Token")
        user = get_user_by_token(conn, token)
        if not user:
            raise ApiError(401, "login_required", "请先登录并提供 Bearer token")
        return user

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        conn = db()
        try:
            self.route_get(conn, path, query)
            conn.commit()
        except ApiError as err:
            conn.rollback()
            self.send_json(err.status, {"error": err.code, "message": err.message, **err.extra})
        except Exception as err:  # noqa: BLE001
            conn.rollback()
            self.send_json(500, {"error": "internal_error", "message": str(err)})
        finally:
            conn.close()

    def do_POST(self) -> None:  # noqa: N802
        self.write_route("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self.write_route("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self.write_route("DELETE")

    def write_route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        conn = db()
        try:
            body = self.read_json() if method in {"POST", "PATCH"} else {}
            self.route_write(conn, method, path, body)
            conn.commit()
        except ApiError as err:
            conn.rollback()
            self.send_json(err.status, {"error": err.code, "message": err.message, **err.extra})
        except Exception as err:  # noqa: BLE001
            conn.rollback()
            self.send_json(500, {"error": "internal_error", "message": str(err)})
        finally:
            conn.close()

    def route_get(self, conn: sqlite3.Connection, path: str, query: dict[str, list[str]]) -> None:
        if path == "/health":
            seq = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM events").fetchone()["s"]
            self.send_json(200, {"ok": True, "service": "local-forum", "event_seq": seq, "db": DB_PATH})
            return

        user = self.authenticate(conn)

        if path == "/me":
            self.send_json(200, {"user": public_user(user)})
            return
        if path == "/groups":
            rows = conn.execute("SELECT * FROM groups ORDER BY created_seq").fetchall()
            self.send_json(200, {"groups": [self.group_list_json(conn, user, r) for r in rows]})
            return
        if path == "/subscriptions":
            rows = conn.execute("SELECT * FROM subscriptions WHERE user_id=? ORDER BY created_at", (user["id"],)).fetchall()
            self.send_json(200, {"subscriptions": [serialize_subscription(conn, r) for r in rows]})
            return
        if path == "/me/continue":
            self.send_json(200, self.continue_reading(conn, user))
            return

        match = re.fullmatch(r"/topics/([^/]+)", path)
        if match:
            topic_id = match.group(1)
            topic = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
            if not topic:
                raise ApiError(404, "topic_not_found", "主题不存在")
            if not can_access_group(conn, user["id"], topic["group_id"]):
                raise ApiError(403, "access_required", "需要申请该私组")
            rows = conn.execute("SELECT * FROM posts WHERE topic_id=? ORDER BY path", (topic_id,)).fetchall()
            self.send_json(200, {
                "topic": dict(topic),
                "posts": [post_json(conn, user, p) for p in rows],
                "note": "这里只列当前地址在本主题的帖子；稳定 parent_id 保留跨组搬迁脉络。",
            })
            return

        match = re.fullmatch(r"/subscriptions/([^/]+)", path)
        if match:
            sub = get_subscription(conn, user, match.group(1))
            self.send_json(200, {"subscription": serialize_subscription(conn, sub)})
            return

        match = re.fullmatch(r"/subscriptions/([^/]+)/pages", path)
        if match:
            sub = get_subscription(conn, user, match.group(1))
            self.subscription_page(conn, user, sub, query)
            return

        match = re.fullmatch(r"/subscriptions/([^/]+)/unread", path)
        if match:
            sub = get_subscription(conn, user, match.group(1))
            self.subscription_unread(conn, user, sub)
            return

        match = re.fullmatch(r"/posts/([^/]+)/screen", path)
        if match:
            self.post_screen(conn, user, match.group(1), query)
            return

        match = re.fullmatch(r"/posts/([^/]+)", path)
        if match:
            post = self.require_post(conn, match.group(1))
            if not can_access_group(conn, user["id"], post["group_id"]):
                raise ApiError(403, "access_required", "你不是该私组成员", {"apply_url": f"/groups/{post['group_id']}/access-requests"})
            self.send_json(200, {"post": post_json(conn, user, post)})
            return

        match = re.fullmatch(r"/old-addresses/posts/([^/]+)", path)
        if match:
            self.old_address(conn, user, match.group(1))
            return

        match = re.fullmatch(r"/read-receipts/([^/]+)", path)
        if match:
            row = conn.execute("SELECT * FROM receipts WHERE id=? AND user_id=?", (match.group(1), user["id"])).fetchone()
            if not row:
                raise ApiError(404, "receipt_not_found", "回单不存在")
            items = conn.execute("SELECT post_id, displayed_version FROM receipt_items WHERE receipt_id=? ORDER BY post_id", (row["id"],)).fetchall()
            payload = dict(row)
            payload["items"] = [dict(i) for i in items]
            self.send_json(200, {"receipt": payload})
            return

        self.send_json(404, {"error": "not_found", "message": f"没有路径 {path}"})

    def route_write(self, conn: sqlite3.Connection, method: str, path: str, body: dict[str, Any]) -> None:
        if method == "POST" and path == "/users":
            self.create_user(conn, body)
            return
        if method == "POST" and path == "/auth/login":
            self.login(conn, body)
            return

        user = self.authenticate(conn)

        if method == "POST" and path == "/groups":
            self.create_group(conn, user, body)
            return
        if method == "POST" and path == "/topics":
            self.create_topic(conn, user, body)
            return
        if method == "POST" and path == "/posts":
            self.create_post(conn, user, body)
            return
        if method == "POST" and path == "/subscriptions":
            self.create_subscription(conn, user, body)
            return
        if method == "POST" and path == "/read-receipts":
            self.create_receipt(conn, user, body)
            return

        match = re.fullmatch(r"/groups/([^/]+)", path)
        if method == "PATCH" and match:
            self.update_group(conn, user, match.group(1), body)
            return

        match = re.fullmatch(r"/groups/([^/]+)/members", path)
        if method == "POST" and match:
            self.add_member(conn, user, match.group(1), body)
            return

        match = re.fullmatch(r"/groups/([^/]+)/members/([^/]+)", path)
        if method == "DELETE" and match:
            self.remove_member(conn, user, match.group(1), match.group(2))
            return

        match = re.fullmatch(r"/groups/([^/]+)/access-requests", path)
        if method == "POST" and match:
            self.create_access_request(conn, user, match.group(1), body)
            return

        match = re.fullmatch(r"/access-requests/([^/]+)/approve", path)
        if method == "POST" and match:
            self.approve_access_request(conn, user, match.group(1))
            return

        match = re.fullmatch(r"/posts/([^/]+)", path)
        if method == "PATCH" and match:
            self.edit_post(conn, user, match.group(1), body)
            return

        match = re.fullmatch(r"/posts/([^/]+)/move", path)
        if method == "POST" and match:
            self.move_post(conn, user, match.group(1), body)
            return

        raise ApiError(404, "not_found", f"没有路由 {method} {path}")

    # ---- handlers -----------------------------------------------------

    def create_user(self, conn: sqlite3.Connection, body: dict[str, Any]) -> None:
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        display_name = str(body.get("display_name") or username).strip()
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{2,40}", username):
            raise ApiError(400, "invalid_username", "username 需为 2-40 位字母数字 _ . -")
        if len(password) < 4:
            raise ApiError(400, "weak_password", "演示环境密码至少 4 位")
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ApiError(409, "user_exists", "用户已存在")
        user_id = body.get("id") or new_id("u")
        token = new_id("tok")
        conn.execute(
            "INSERT INTO users(id, username, display_name, password_digest, api_token, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, username, display_name, password_digest(password), token, now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        self.send_json(201, {"user": public_user(row), "token": token, "login": {"method": "POST", "url": "/auth/login"}})

    def login(self, conn: sqlite3.Connection, body: dict[str, Any]) -> None:
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row or not hmac.compare_digest(row["password_digest"], password_digest(password)):
            raise ApiError(401, "bad_credentials", "用户名或密码错误")
        self.send_json(200, {"user": public_user(row), "token": row["api_token"]})

    def group_list_json(self, conn: sqlite3.Connection, user: sqlite3.Row, group: sqlite3.Row) -> dict[str, Any]:
        member = conn.execute(
            "SELECT role FROM group_members WHERE group_id=? AND user_id=?", (group["id"], user["id"])
        ).fetchone()
        data = {
            "id": group["id"],
            "slug": group["slug"],
            "visibility": group["visibility"],
            "created_at": group["created_at"],
            "my_role": member["role"] if member else None,
            "can_read": can_access_group(conn, user["id"], group["id"]),
        }
        if data["can_read"]:
            data["title"] = group["title"]
        else:
            data["title"] = None
            data["access"] = "request_membership"
        return data

    def create_group(self, conn: sqlite3.Connection, user: sqlite3.Row, body: dict[str, Any]) -> None:
        slug = str(body.get("slug", "")).strip()
        title = str(body.get("title", "")).strip()
        visibility = str(body.get("visibility", "public"))
        if not slug or not title:
            raise ApiError(400, "missing_fields", "slug 和 title 必填")
        if visibility not in {"public", "private"}:
            raise ApiError(400, "invalid_visibility", "visibility 只能是 public/private")
        group_id = body.get("id") or new_id("g")
        with conn:
            seq = add_event(conn, "group.created", user["id"], {"id": group_id, "slug": slug})
            conn.execute(
                "INSERT INTO groups(id, slug, title, visibility, created_by, created_seq, created_at) VALUES (?,?,?,?,?,?,?)",
                (group_id, slug, title, visibility, user["id"], seq, now_iso()),
            )
            conn.execute(
                "INSERT INTO group_members(group_id, user_id, role, created_at) VALUES (?,?,?,?)",
                (group_id, user["id"], "owner", now_iso()),
            )
        row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        self.send_json(201, {"group": dict(row)})

    def update_group(self, conn: sqlite3.Connection, user: sqlite3.Row, group_id: str, body: dict[str, Any]) -> None:
        group = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if not group:
            raise ApiError(404, "group_not_found", "小组不存在")
        require_group_role(conn, user["id"], group_id)
        visibility = body.get("visibility", group["visibility"])
        if visibility not in {"public", "private"}:
            raise ApiError(400, "invalid_visibility", "visibility 只能是 public/private")
        with conn:
            add_event(conn, "group.updated", user["id"], {"group_id": group_id, "visibility": visibility})
            conn.execute("UPDATE groups SET visibility=? WHERE id=?", (visibility, group_id))
        row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        self.send_json(200, {"group": dict(row)})

    def resolve_user_ref(self, conn: sqlite3.Connection, ref: dict[str, Any]) -> sqlite3.Row:
        if ref.get("user_id"):
            row = conn.execute("SELECT * FROM users WHERE id=?", (ref["user_id"],)).fetchone()
        elif ref.get("username"):
            row = conn.execute("SELECT * FROM users WHERE username=?", (ref["username"],)).fetchone()
        else:
            raise ApiError(400, "missing_user", "需要 user_id 或 username")
        if not row:
            raise ApiError(404, "user_not_found", "用户不存在")
        return row

    def add_member(self, conn: sqlite3.Connection, user: sqlite3.Row, group_id: str, body: dict[str, Any]) -> None:
        group = conn.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone()
        if not group:
            raise ApiError(404, "group_not_found", "小组不存在")
        require_group_role(conn, user["id"], group_id)
        target = self.resolve_user_ref(conn, body)
        role = str(body.get("role", "member"))
        if role not in {"owner", "moderator", "member"}:
            raise ApiError(400, "invalid_role", "角色非法")
        conn.execute(
            """
            INSERT INTO group_members(group_id, user_id, role, created_at)
            VALUES (?,?,?,?)
            ON CONFLICT(group_id,user_id) DO UPDATE SET role=excluded.role
            """,
            (group_id, target["id"], role, now_iso()),
        )
        add_event(conn, "group.member_added", user["id"], {"group_id": group_id, "user_id": target["id"], "role": role})
        self.send_json(200, {"membership": {"group_id": group_id, "user_id": target["id"], "role": role}})

    def remove_member(self, conn: sqlite3.Connection, user: sqlite3.Row, group_id: str, target_user_id: str) -> None:
        require_group_role(conn, user["id"], group_id)
        target = conn.execute("SELECT * FROM users WHERE id=?", (target_user_id,)).fetchone()
        if not target:
            raise ApiError(404, "user_not_found", "用户不存在")
        member = conn.execute("SELECT role FROM group_members WHERE group_id=? AND user_id=?", (group_id, target_user_id)).fetchone()
        if not member:
            raise ApiError(404, "membership_not_found", "成员关系不存在")
        if member["role"] == "owner":
            raise ApiError(409, "cannot_remove_owner", "不能移除组长")
        conn.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (group_id, target_user_id))
        add_event(conn, "group.member_removed", user["id"], {"group_id": group_id, "user_id": target_user_id})
        self.send_json(200, {"removed": {"group_id": group_id, "user_id": target_user_id}})

    def create_access_request(self, conn: sqlite3.Connection, user: sqlite3.Row, group_id: str, body: dict[str, Any]) -> None:
        group = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if not group:
            raise ApiError(404, "group_not_found", "小组不存在")
        existing_member = conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (group_id, user["id"])).fetchone()
        if existing_member:
            raise ApiError(409, "already_member", "你已是该组成员")
        req = conn.execute(
            "SELECT * FROM access_requests WHERE group_id=? AND user_id=? AND status='pending'",
            (group_id, user["id"]),
        ).fetchone()
        if req:
            request_id = req["id"]
        else:
            request_id = new_id("ar")
            conn.execute(
                "INSERT INTO access_requests(id, group_id, user_id, status, created_at) VALUES (?,?,?,'pending',?)",
                (request_id, group_id, user["id"], now_iso()),
            )
            add_event(conn, "access.requested", user["id"], {"id": request_id, "group_id": group_id})
        self.send_json(201, {"access_request": {"id": request_id, "group_id": group_id, "status": "pending"}, "message": "申请已记录；通过前不会返回私组标题或正文。"})

    def approve_access_request(self, conn: sqlite3.Connection, user: sqlite3.Row, request_id: str) -> None:
        req = conn.execute("SELECT * FROM access_requests WHERE id=?", (request_id,)).fetchone()
        if not req:
            raise ApiError(404, "request_not_found", "申请不存在")
        require_group_role(conn, user["id"], req["group_id"])
        conn.execute(
            "INSERT INTO group_members(group_id, user_id, role, created_at) VALUES (?,?, 'member', ?) ON CONFLICT DO NOTHING",
            (req["group_id"], req["user_id"], now_iso()),
        )
        conn.execute("UPDATE access_requests SET status='approved', decided_at=? WHERE id=?", (now_iso(), request_id))
        add_event(conn, "access.approved", user["id"], {"id": request_id})
        self.send_json(200, {"approved": request_id, "group_id": req["group_id"], "user_id": req["user_id"]})

    def create_topic(self, conn: sqlite3.Connection, user: sqlite3.Row, body: dict[str, Any]) -> None:
        group_id = str(body.get("group_id", ""))
        title = str(body.get("title", "")).strip()
        group = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if not group or not title:
            raise ApiError(400, "missing_fields", "group_id 和 title 必填，且小组必须存在")
        if not can_access_group(conn, user["id"], group_id):
            raise ApiError(403, "access_required", "不能在无权访问的小组建主题")
        topic_id = body.get("id") or new_id("t")
        with conn:
            seq = add_event(conn, "topic.created", user["id"], {"id": topic_id, "group_id": group_id})
            conn.execute(
                "INSERT INTO topics(id, group_id, title, created_seq, created_at) VALUES (?,?,?,?,?)",
                (topic_id, group_id, title, seq, now_iso()),
            )
        self.send_json(201, {"topic": {"id": topic_id, "group_id": group_id, "title": title}})

    def require_post(self, conn: sqlite3.Connection, post_id: str) -> sqlite3.Row:
        post = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not post:
            raise ApiError(404, "post_not_found", "帖子不存在")
        return post

    def create_post(self, conn: sqlite3.Connection, user: sqlite3.Row, body: dict[str, Any]) -> None:
        text = str(body.get("body", "")).strip()
        if not text:
            raise ApiError(400, "body_required", "body 不能为空")
        requested_topic = body.get("topic_id")
        parent_id = body.get("parent_id")
        location_resolution: dict[str, Any] = {}

        with conn:
            conn.execute("BEGIN IMMEDIATE")
            event_seq = add_event(conn, "post.created.received", user["id"], {
                "client_post_id": body.get("id"),
                "parent_id": parent_id,
                "topic_id": requested_topic,
            })
            if parent_id:
                parent = conn.execute("SELECT * FROM posts WHERE id=?", (parent_id,)).fetchone()
                if not parent:
                    raise ApiError(404, "parent_not_found", "回复的父帖不存在")
                # A stable post id is also its old address. New replies inherit
                # the parent's currently true location, not a stale old topic.
                group_id = parent["group_id"]
                topic_id = parent["topic_id"]
                if requested_topic and requested_topic != topic_id:
                    old_topic = conn.execute("SELECT group_id FROM topics WHERE id=?", (requested_topic,)).fetchone()
                    location_resolution = {
                        "stale_topic_id": requested_topic,
                        "parent_current_topic_id": topic_id,
                        "parent_current_group_id": group_id,
                        "rule": "reply_attached_to_current_true_parent",
                        "requested_topic_was_old": bool(old_topic and old_topic["group_id"] != group_id),
                    }
            else:
                topic_id = str(requested_topic or "")
                topic = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
                if not topic:
                    raise ApiError(400, "invalid_topic", "topic_id 不存在")
                group_id = str(body.get("group_id") or topic["group_id"])
                if topic["group_id"] != group_id:
                    raise ApiError(400, "invalid_topic", "topic_id 不属于 group_id")

            if not can_access_group(conn, user["id"], group_id):
                raise ApiError(403, "access_required", "不能在无权访问的小组发帖")

            post_id = str(body.get("id") or new_id("p"))
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", post_id):
                raise ApiError(400, "invalid_post_id", "id 仅可包含字母数字 _ . : - 且至多 64 位")
            if conn.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone():
                raise ApiError(409, "post_exists", "帖子 id 已存在")

            now = now_iso()
            path = (parent["path"] if parent_id else "") + f"/{event_seq:020d}"
            root_id = parent["root_id"] if parent_id else post_id
            depth = parent["depth"] + 1 if parent_id else 0
            title = body.get("title")
            conn.execute(
                """
                INSERT INTO posts(id, seq, path, topic_id, group_id, parent_id, root_id, depth,
                                  author_id, title, body, version, board_arrival_seq, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
                """,
                (post_id, event_seq, path, topic_id, group_id, parent_id, root_id, depth,
                 user["id"], title, text, event_seq, now, now),
            )
            conn.execute(
                """
                INSERT INTO post_versions(post_id, version, title, body, editor_id, event_seq, created_at)
                VALUES (?,1,?,?,?,?,?)
                """,
                (post_id, title, text, user["id"], event_seq, now),
            )
            add_event(conn, "post.created", user["id"], {"post_id": post_id, "seq": event_seq})
            row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        payload = {"post": post_json(conn, user, row)}
        if location_resolution:
            payload["location_resolution"] = location_resolution
        self.send_json(201, payload)

    def edit_post(self, conn: sqlite3.Connection, user: sqlite3.Row, post_id: str, body: dict[str, Any]) -> None:
        post = self.require_post(conn, post_id)
        if not can_access_group(conn, user["id"], post["group_id"]):
            raise ApiError(403, "access_required", "你当前不能访问承载该帖的小组")
        if post["author_id"] != user["id"]:
            require_group_role(conn, user["id"], post["group_id"])
        expected = body.get("expected_version")
        if expected is not None:
            try:
                expected_int = int(expected)
            except (TypeError, ValueError) as exc:
                raise ApiError(400, "invalid_version", "expected_version 必须是整数") from exc
        else:
            expected_int = None

        with conn:
            # Acquire the write lock before reading the current version. This
            # makes simultaneous edit/move requests serialize on event seq.
            conn.execute("BEGIN IMMEDIATE")
            current = self.require_post(conn, post_id)
            if expected_int is not None and expected_int != current["version"]:
                raise ApiError(409, "version_conflict", "帖子已被其他人修改，请基于当前版本重试", {
                    "current_version": current["version"],
                    "your_expected_version": expected_int,
                })
            new_body = body.get("body", current["body"])
            new_title = body.get("title", current["title"])
            if not str(new_body).strip():
                raise ApiError(400, "body_required", "body 不能为空")
            seq = add_event(conn, "post.edited", user["id"], {"post_id": post_id, "old_version": current["version"]})
            next_version = current["version"] + 1
            now = now_iso()
            updated = conn.execute(
                "UPDATE posts SET title=?, body=?, version=?, updated_at=? WHERE id=? AND version=?",
                (new_title, new_body, next_version, now, post_id, current["version"]),
            )
            if updated.rowcount != 1:
                raise ApiError(409, "version_conflict", "版本已变化，请重试")
            conn.execute(
                """
                INSERT INTO post_versions(post_id, version, title, body, editor_id, event_seq, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (post_id, next_version, new_title, new_body, user["id"], seq, now),
            )
        row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        self.send_json(200, {"post": post_json(conn, user, row), "note": "旧回单只证明旧版本；当前版本未被回单覆盖时仍算未读。"})

    def move_post(self, conn: sqlite3.Connection, user: sqlite3.Row, post_id: str, body: dict[str, Any]) -> None:
        post = self.require_post(conn, post_id)
        target_group_id = str(body.get("target_group_id", ""))
        target_topic_id = str(body.get("target_topic_id", ""))
        target_group = conn.execute("SELECT * FROM groups WHERE id=?", (target_group_id,)).fetchone()
        target_topic = conn.execute("SELECT * FROM topics WHERE id=?", (target_topic_id,)).fetchone()
        if not target_group or not target_topic or target_topic["group_id"] != target_group_id:
            raise ApiError(400, "invalid_target", "目标 group/topic 不存在或不匹配")
        require_group_role(conn, user["id"], post["group_id"])
        require_group_role(conn, user["id"], target_group_id)

        with conn:
            conn.execute("BEGIN IMMEDIATE")
            seq = add_event(conn, "post.move.requested", user["id"], {"post_id": post_id})
            subtree_rows = conn.execute(
                """
                WITH RECURSIVE subtree(id) AS (
                    SELECT id FROM posts WHERE id=?
                    UNION ALL
                    SELECT p.id FROM posts p JOIN subtree s ON p.parent_id=s.id
                )
                SELECT id FROM subtree ORDER BY id
                """,
                (post_id,),
            ).fetchall()
            ids = [r["id"] for r in subtree_rows]
            from_group_id = post["group_id"]
            from_topic_id = post["topic_id"]
            placeholders = ",".join("?" for _ in ids)
            # The move event sequence is the target board-arrival marker.
            # Source-board queries immediately stop seeing these rows because
            # current group_id changes; target-board queries gain them.
            conn.execute(
                f"UPDATE posts SET group_id=?, topic_id=?, board_arrival_seq=? WHERE id IN ({placeholders})",
                [target_group_id, target_topic_id, seq, *ids],
            )
            move_id = new_id("mov")
            conn.execute(
                """
                INSERT INTO moves(id, root_post_id, post_id, from_group_id, from_topic_id,
                                  to_group_id, to_topic_id, subtree_json, event_seq, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (move_id, post_id, post_id, from_group_id, from_topic_id, target_group_id, target_topic_id,
                 json.dumps(ids), seq, now_iso()),
            )
            add_event(conn, "post.moved", user["id"], {
                "move_id": move_id,
                "root_post_id": post_id,
                "subtree": ids,
                "event_seq": seq,
            })

        updated = self.require_post(conn, post_id)
        self.send_json(200, {
            "move": {
                "id": move_id,
                "event_seq": seq,
                "moved_post_ids": ids,
                "from": {"group_id": from_group_id, "topic_id": from_topic_id},
                "to": {"group_id": target_group_id, "topic_id": target_topic_id},
                "old_address_lookup": f"/old-addresses/posts/{post_id}",
            },
            "root_post": post_json(conn, user, updated) if can_access_group(conn, user["id"], target_group_id) else None,
            "consistency": "move and edit are serialized by one SQLite IMMEDIATE transaction/event seq; no subtree or version is lost.",
        })

    def old_address(self, conn: sqlite3.Connection, user: sqlite3.Row, post_id: str) -> None:
        post = self.require_post(conn, post_id)
        moves = conn.execute("SELECT * FROM moves WHERE post_id=? ORDER BY event_seq", (post_id,)).fetchall()
        accessible = can_access_group(conn, user["id"], post["group_id"])
        history = []
        for move in moves:
            to_access = can_access_group(conn, user["id"], move["to_group_id"])
            item: dict[str, Any] = {
                "move_id": move["id"],
                "moved_at": move["created_at"],
                "event_seq": move["event_seq"],
                "from": {"group_id": move["from_group_id"], "topic_id": move["from_topic_id"]},
                "to_group_id": move["to_group_id"],
                "to_topic_id": move["to_topic_id"],
                "destination_accessible": to_access,
            }
            if to_access:
                g = conn.execute("SELECT slug,title FROM groups WHERE id=?", (move["to_group_id"],)).fetchone()
                t = conn.execute("SELECT title FROM topics WHERE id=?", (move["to_topic_id"],)).fetchone()
                item["to_group_slug"] = g["slug"]
                item["to_group_title"] = g["title"]
                item["to_topic_title"] = t["title"]
                item["destination_url"] = f"/posts/{post_id}"
            else:
                item["to_group_title"] = None
                item["to_topic_title"] = None
                item["apply_url"] = f"/groups/{move['to_group_id']}/access-requests"
            history.append(item)
        self.send_json(200, {
            "old_address": f"/old-addresses/posts/{post_id}",
            "stable_post_id": post_id,
            "currently_in_group_id": post["group_id"],
            "currently_in_topic_id": post["topic_id"],
            "current_address_accessible": accessible,
            "destination_url": f"/posts/{post_id}" if accessible else None,
            "moves": history,
        })

    def post_screen(self, conn: sqlite3.Connection, user: sqlite3.Row, post_id: str, query: dict[str, list[str]]) -> None:
        post = self.require_post(conn, post_id)
        if not can_access_group(conn, user["id"], post["group_id"]):
            raise ApiError(403, "access_required", "无权限查看该帖")
        # This screen shows only the requested post. Direct replies are collapsed
        # metadata and deliberately are not inserted into page_items.
        children = conn.execute("SELECT * FROM posts WHERE parent_id=? ORDER BY path", (post_id,)).fetchall()
        collapsed = []
        for child in children:
            if can_access_group(conn, user["id"], child["group_id"]):
                collapsed.append({
                    "post_id": child["id"],
                    "collapsed": True,
                    "reply_count": conn.execute("SELECT COUNT(*) c FROM posts WHERE parent_id=?", (child["id"],)).fetchone()["c"],
                    "body_visible": False,
                    "title_visible": False,
                    "read": read_state(conn, user["id"], child),
                    "expand_url": f"/posts/{child['id']}/screen",
                })
            else:
                collapsed.append(masked_post_tile(conn, user, child))
        token = create_page_token(conn, user, None, f"post-screen:{post_id}", [(post, True)], None)
        conn.commit()
        self.send_json(200, {
            "screen": "post",
            "post": post_json(conn, user, post),
            "collapsed_replies": collapsed,
            "displayed_items": [{"post_id": post["id"], "displayed_version": post["version"]}],
            "page_token": token,
            "ack": {"method": "POST", "url": "/read-receipts", "json": {"page_token": token}},
            "rule": "回单只会确认 page_token 中记录的真实展示版本；折叠回复不在其中。",
        })

    def create_subscription(self, conn: sqlite3.Connection, user: sqlite3.Row, body: dict[str, Any]) -> None:
        kind = str(body.get("kind") or body.get("type") or "")
        receipt_id = body.get("baseline_receipt_id")
        if receipt_id:
            receipt = conn.execute("SELECT 1 FROM receipts WHERE id=? AND user_id=?", (receipt_id, user["id"])).fetchone()
            if not receipt:
                raise ApiError(400, "bad_receipt", "baseline_receipt_id 不属于当前用户")
        sub_id = body.get("id") or new_id("sub")
        if kind in {"branch", "thread"}:
            anchor_id = body.get("anchor_post_id") or body.get("post_id")
            anchor = self.require_post(conn, str(anchor_id))
            if not can_access_group(conn, user["id"], anchor["group_id"]):
                raise ApiError(403, "access_required", "不能订阅无权访问的分枝")
            conn.execute(
                """
                INSERT INTO subscriptions(id,user_id,kind,anchor_post_id,group_id,baseline_seq,baseline_receipt_id,created_at)
                VALUES (?,?,'branch',?,NULL,0,?,?)
                """,
                (sub_id, user["id"], anchor["id"], receipt_id, now_iso()),
            )
        elif kind in {"board", "group"}:
            group_id = str(body.get("group_id", ""))
            group = conn.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone()
            if not group:
                raise ApiError(404, "group_not_found", "小组不存在")
            if not can_access_group(conn, user["id"], group_id):
                raise ApiError(403, "access_required", "不能订阅无权访问的小组")
            mode = body.get("baseline_mode", "future")
            if mode == "all":
                baseline_seq = 0
            else:
                baseline_seq = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM events").fetchone()["s"]
            conn.execute(
                """
                INSERT INTO subscriptions(id,user_id,kind,anchor_post_id,group_id,baseline_seq,baseline_receipt_id,created_at)
                VALUES (?,?,'board',NULL,?,?,?,?)
                """,
                (sub_id, user["id"], group_id, baseline_seq, receipt_id, now_iso()),
            )
        else:
            raise ApiError(400, "invalid_kind", "kind 必须是 branch 或 board")
        add_event(conn, "subscription.created", user["id"], {"subscription_id": sub_id, "kind": kind})
        row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        self.send_json(201, {"subscription": serialize_subscription(conn, row)})

    def subscription_page(self, conn: sqlite3.Connection, user: sqlite3.Row, sub: sqlite3.Row, query: dict[str, list[str]]) -> None:
        try:
            limit = min(max(int(query.get("limit", ["2"])[0]), 1), 20)
        except ValueError as exc:
            raise ApiError(400, "invalid_limit", "limit 必须是整数") from exc
        cursor_payload = decode_cursor(query.get("cursor", [None])[0])
        focus = query.get("focus", [None])[0]

        rows = candidate_posts(conn, sub)
        if focus:
            focus_index = next((i for i, r in enumerate(rows) if r["id"] == focus), None)
            if focus_index is None:
                raise ApiError(404, "focus_not_in_subscription", "定位帖不属于此订阅")
            rows = rows[focus_index:]
        else:
            rows = [r for r in rows if cursor_matches_after(cursor_payload, sub, r)]

        paged = rows[: limit + 1]
        has_more = len(paged) > limit
        shown_rows = paged[:limit]
        shown: list[tuple[sqlite3.Row, bool]] = []
        items: list[dict[str, Any]] = []
        for post in shown_rows:
            accessible = can_access_group(conn, user["id"], post["group_id"])
            shown.append((post, accessible))
            items.append(post_json(conn, user, post) if accessible else masked_post_tile(conn, user, post))

        next_cursor = make_next_cursor(sub, shown_rows[-1]) if has_more and shown_rows else None
        token = create_page_token(conn, user, sub["id"], f"{sub['kind']}:{sub['id']}", shown, next_cursor)
        conn.commit()
        self.send_json(200, {
            "subscription_id": sub["id"],
            "kind": sub["kind"],
            "limit": limit,
            "page_token": token,
            "ack_endpoint": "/read-receipts",
            "ack_hint": "POST {\"page_token\": page_token}；只确认本页可展示帖的当前记录版本。",
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "restart_safe": "page_token 与 cursor 依据均持久化在 SQLite；重启后可继续。",
        })

    def subscription_unread(self, conn: sqlite3.Connection, user: sqlite3.Row, sub: sqlite3.Row) -> None:
        rows = candidate_posts(conn, sub)
        unread = []
        blocked = []
        for post in rows:
            if not can_access_group(conn, user["id"], post["group_id"]):
                blocked.append(masked_post_tile(conn, user, post))
                continue
            data = post_json(conn, user, post)
            if data["read"]["needs_read"]:
                unread.append(data)
        self.send_json(200, {
            "subscription": serialize_subscription(conn, sub),
            "unread_count": len(unread),
            "blocked_count": len(blocked),
            "unread": unread,
            "blocked": blocked,
            "basis": {
                "subscription_id": sub["id"],
                "baseline_receipt_id": sub["baseline_receipt_id"],
                "computed_by": "current subscription membership + current group ACL + MAX(versioned receipt)",
                "global_broadcast": False,
            },
        })

    def continue_reading(self, conn: sqlite3.Connection, user: sqlite3.Row) -> dict[str, Any]:
        subs = conn.execute("SELECT * FROM subscriptions WHERE user_id=? ORDER BY created_at", (user["id"],)).fetchall()
        positions = []
        for sub in subs:
            rows = candidate_posts(conn, sub)
            next_post = None
            blocked_post = None
            unread_count = 0
            for post in rows:
                accessible = can_access_group(conn, user["id"], post["group_id"])
                if not accessible:
                    if blocked_post is None:
                        blocked_post = post
                    continue
                state = read_state(conn, user["id"], post)
                if state["needs_read"]:
                    unread_count += 1
                    if next_post is None:
                        next_post = post
            state_for_basis = read_state(conn, user["id"], next_post) if next_post else None
            effective_id = state_for_basis["effective_receipt_id"] if state_for_basis else None
            if next_post and state_for_basis and state_for_basis["state"] == "updated_after_read":
                explanation = (
                    f"回单 {effective_id} 只确认 v{state_for_basis['acked_version']}，"
                    f"当前为 v{state_for_basis['current_version']}，补充内容仍待阅"
                )
            elif next_post:
                explanation = "没有覆盖该帖当前版本的阅读回单；折叠或未翻到的屏幕不会产生回单"
            else:
                explanation = "所有有权帖子的当前版本均已有回单；无权分枝只显示申请入口"
            basis = {
                "subscription_id": sub["id"],
                "subscription_kind": sub["kind"],
                "subscription_label": "跟着分枝走" if sub["kind"] == "branch" else "只收本版",
                "commitment_receipt_id": sub["baseline_receipt_id"],
                "effective_receipt_id": effective_id,
                "explanation": explanation,
                "rule": "无当前版本回单，或回单版本低于当前版本，即为可继续阅读位置",
            }
            item: dict[str, Any] = {
                "subscription_id": sub["id"],
                "kind": sub["kind"],
                "unread_count": unread_count,
                "because": basis,
            }
            if next_post:
                p = self.require_post(conn, next_post["id"])
                item["continue_position"] = {
                    "post_id": p["id"],
                    "topic_id": p["topic_id"],
                    "group_id": p["group_id"],
                    "parent_id": p["parent_id"],
                    "depth": p["depth"],
                    "path": p["path"],
                    "title": p["title"],
                    "read": read_state(conn, user["id"], p),
                    "open": {
                        "method": "GET",
                        "url": f"/subscriptions/{sub['id']}/pages?focus={p['id']}&limit=2",
                    },
                }
            else:
                item["continue_position"] = None
            if blocked_post:
                item["access_required_position"] = masked_post_tile(conn, user, blocked_post)
            positions.append(item)
        return {"user_id": user["id"], "positions": positions}

    def create_receipt(self, conn: sqlite3.Connection, user: sqlite3.Row, body: dict[str, Any]) -> None:
        token = str(body.get("page_token", ""))
        page = conn.execute("SELECT * FROM page_tokens WHERE token=?", (token,)).fetchone()
        if not page or page["user_id"] != user["id"]:
            raise ApiError(404, "page_token_not_found", "屏幕分页不存在或不属于当前用户")
        existing = conn.execute("SELECT * FROM receipts WHERE page_token=?", (token,)).fetchone()
        if existing:
            items = conn.execute("SELECT post_id, displayed_version FROM receipt_items WHERE receipt_id=?", (existing["id"],)).fetchall()
            payload = dict(existing)
            payload["items"] = [dict(i) for i in items]
            payload["idempotent_replay"] = True
            self.send_json(200, {"receipt": payload, "note": "该屏幕回单已存在；晚到的重复全部读完不会扩大确认范围。"})
            return

        requested_ids = body.get("post_ids")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            seq = add_event(conn, "receipt.created", user["id"], {"page_token": token})
            receipt_id = new_id("rcp")
            intent = str(body.get("intent") or "mark_all_read_on_recorded_screen")
            conn.execute(
                "INSERT INTO receipts(id,user_id,page_token,intent,event_seq,created_at) VALUES (?,?,?,?,?,?)",
                (receipt_id, user["id"], token, intent, seq, now_iso()),
            )
            page_items = conn.execute(
                "SELECT * FROM page_items WHERE token=? AND displayed_version IS NOT NULL ORDER BY ordinal",
                (token,),
            ).fetchall()
            saved = []
            for pi in page_items:
                if requested_ids is not None and pi["post_id"] not in set(requested_ids):
                    continue
                conn.execute(
                    "INSERT INTO receipt_items(receipt_id,user_id,post_id,displayed_version) VALUES (?,?,?,?)",
                    (receipt_id, user["id"], pi["post_id"], pi["displayed_version"]),
                )
                saved.append({"post_id": pi["post_id"], "displayed_version": pi["displayed_version"]})
        row = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        self.send_json(201, {
            "receipt": {**dict(row), "items": saved},
            "scope": "only_post_versions_actually_recorded_on_screen",
            "late_mark_all_cannot_ack_unseen": True,
        })


def main() -> None:
    init_db()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", str(PORT)))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"forum backend listening on http://{host}:{port} (db={DB_PATH})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()




