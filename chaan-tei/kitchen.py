#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
茶餐厅后厨服务 —— 逐份跟单模式（per-portion tracking）

角色与端：
  服务员端：开台食客、下单（一次 N 份 = N 张独立点菜单）、改菜/撤菜、
            拆分食客、把一道菜移给另一桌、食客换桌、查桌账
  炒档端：拉待炒队列、喊炒（同做法可同锅，每份钉点菜单号）、报熟/少报
  传菜端：按盘号派盘（只派真做完的锅）、查余菜

关键不变量：
  1. 一锅对应一种做法；喊炒后每份生成 portion 并钉死 slip_id，撤单/换桌都删不掉它
  2. 已 DELIVERED 的盘不能撤单退款；余菜 LEFTOVER 不能再派
  3. 派盘用 CAS（WHERE status='READY'），并发抢同一盘只有一个成功
  4. 少报成品 -> 该份 slip 退回队列重炒，不会凭空派出第二盘
  5. 桌账只取实际派到该桌的 delivery 记录

仅依赖标准库：http.server + sqlite3 + threading
"""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs

DB_PATH = os.environ.get(
    "CHAAN_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "chaan.db"),
)

# 所有请求在同一把锁内串行写库，配合 SQLite 保证并发派盘的 CAS 语义
_LOCK = threading.RLock()
_CONN = None


# ----------------------------- 存储层 -----------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS diners(
  diner_id   TEXT PRIMARY KEY,   -- D001...
  name       TEXT NOT NULL,
  table_name TEXT NOT NULL,
  seated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slips(
  id           TEXT PRIMARY KEY, -- M0001 点菜单号（一份一单）
  diner_id     TEXT NOT NULL,
  prep         TEXT NOT NULL,    -- 做法，同做法才能同锅
  status       TEXT NOT NULL,    -- ENTERED / WOK_LOCKED / CANCELLED / DELIVERED
  qty          INTEGER NOT NULL DEFAULT 1,
  note         TEXT,
  revised_from TEXT,             -- 改菜新单指向旧单
  created_at   TEXT NOT NULL,
  FOREIGN KEY(diner_id) REFERENCES diners(diner_id)
);
CREATE TABLE IF NOT EXISTS woks(
  id         TEXT PRIMARY KEY,   -- W0001
  prep       TEXT NOT NULL,
  status     TEXT NOT NULL,      -- WOKKING / DONE
  created_at TEXT NOT NULL,
  done_at    TEXT
);
CREATE TABLE IF NOT EXISTS portions(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  wok_id  TEXT NOT NULL,
  slip_id TEXT NOT NULL,        -- 逐份跟单：这份钉死哪张点菜单
  status  TEXT NOT NULL,        -- COOKED / SHORT
  FOREIGN KEY(wok_id) REFERENCES woks(id),
  FOREIGN KEY(slip_id) REFERENCES slips(id)
);
CREATE TABLE IF NOT EXISTS plates(
  plate_id        TEXT PRIMARY KEY, -- P0001 盘号
  portion_id      INTEGER NOT NULL,
  wok_id          TEXT NOT NULL,
  origin_slip_id  TEXT NOT NULL,    -- 盘号来路：始终可追到原菜单
  prep            TEXT NOT NULL,
  status          TEXT NOT NULL,    -- READY / DELIVERED / LEFTOVER
  delivered_table TEXT,
  FOREIGN KEY(portion_id) REFERENCES portions(id)
);
CREATE TABLE IF NOT EXISTS deliveries(
  id         TEXT PRIMARY KEY,      -- V0001
  plate_id   TEXT NOT NULL UNIQUE,  -- 一盘只派一次
  slip_id    TEXT NOT NULL,
  diner_id   TEXT NOT NULL,
  table_name TEXT NOT NULL,         -- 实际交给哪桌
  idem_key   TEXT,
  at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  at      TEXT NOT NULL,
  kind    TEXT NOT NULL,
  ref     TEXT,
  detail  TEXT
);
CREATE TABLE IF NOT EXISTS idem_store(
  idem_key      TEXT PRIMARY KEY,
  response_json TEXT NOT NULL,
  at            TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    global _CONN
    if _CONN is None:
        # 进程启动时若残留上一进程的 -shm/-wal，可能出现盐值失配，
        # 导致新进程把整段 WAL 判无效而读空。先做一次 checkpoint 把
        # 已提交帧回写主库，再清掉陈旧索引，让 SQLite 按 WAL 重建。
        try:
            boot = sqlite3.connect(DB_PATH, timeout=10)
            boot.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            boot.commit()
            boot.close()
        except sqlite3.Error:
            pass
        for suffix in ("-shm", "-wal"):
            stale = DB_PATH + suffix
            if os.path.exists(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass
        _CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.execute("PRAGMA synchronous=FULL")
        _CONN.execute("PRAGMA foreign_keys=ON")
        _CONN.executescript(SCHEMA)
        _CONN.commit()
    return _CONN


def checkpoint(c):
    # 每次写请求后把 WAL 回写主库，保证随时可凭盘/锅/桌号查回
    try:
        c.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def next_id(c, table, col, prefix, width):
    row = c.execute(f"SELECT MAX({col}) AS m FROM {table}").fetchone()
    n = 1 if row["m"] is None else int(row["m"][len(prefix):]) + 1
    return f"{prefix}{n:0{width}d}"


def record(c, kind, ref, detail):
    c.execute(
        "INSERT INTO events(at, kind, ref, detail) VALUES(?,?,?,?)",
        (now(), kind, ref, json.dumps(detail, ensure_ascii=False)),
    )


class ApiError(Exception):
    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


def row_to_dict(r):
    return {k: r[k] for k in r.keys()}


# ----------------------------- 业务操作 -----------------------------

def seat_diner(c, body):
    table = body.get("table")
    name = body.get("name")
    if not table or not name:
        raise ApiError(400, "BAD_REQUEST", "需要 table 与 name")
    did = next_id(c, "diners", "diner_id", "D", 3)
    c.execute(
        "INSERT INTO diners(diner_id,name,table_name,seated_at) VALUES(?,?,?,?)",
        (did, name, table, now()),
    )
    record(c, "DINER_SEATED", did, {"table": table, "name": name})
    return {"diner_id": did, "name": name, "table": table}


def create_order(c, body):
    """一次下 qty 份 -> qty 张互相独立的点菜单，可分别改动其中一份。"""
    did = body.get("diner_id")
    prep = body.get("prep")
    qty = int(body.get("qty", 1))
    if not did or not prep or qty < 1:
        raise ApiError(400, "BAD_REQUEST", "需要 diner_id、prep、qty>=1")
    if not c.execute("SELECT 1 FROM diners WHERE diner_id=?", (did,)).fetchone():
        raise ApiError(404, "NO_DINER", f"食客 {did} 不存在")
    slips = []
    for _ in range(qty):
        sid = next_id(c, "slips", "id", "M", 4)
        c.execute(
            "INSERT INTO slips(id,diner_id,prep,status,qty,created_at) "
            "VALUES(?,?,?, 'ENTERED', 1, ?)",
            (sid, did, prep, now()),
        )
        record(c, "SLIP_ENTERED", sid, {"diner_id": did, "prep": prep})
        slips.append(sid)
    return {"prep": prep, "slip_ids": slips, "count": len(slips)}


def _load_slip(c, sid):
    r = c.execute("SELECT * FROM slips WHERE id=?", (sid,)).fetchone()
    if not r:
        raise ApiError(404, "NO_SLIP", f"点菜单 {sid} 不存在")
    return r


def modify_slip(c, body):
    """喊炒前改做法：仅 ENTERED 可改，不动别的单。"""
    sid = body.get("slip_id")
    new_prep = body.get("new_prep")
    if not sid or not new_prep:
        raise ApiError(400, "BAD_REQUEST", "需要 slip_id 与 new_prep")
    slip = _load_slip(c, sid)
    if slip["status"] != "ENTERED":
        raise ApiError(
            409, "TOO_LATE",
            f"点菜单 {sid} 状态为 {slip['status']}，已不能直接改；"
            "已喊炒请用 /slips/revise 留锅另开新单",
            {"current_status": slip["status"]},
        )
    c.execute("UPDATE slips SET prep=? WHERE id=?", (new_prep, sid))
    record(c, "SLIP_MODIFIED", sid, {"old": slip["prep"], "new": new_prep})
    return {"slip_id": sid, "prep": new_prep}


def revise_slip(c, body):
    """
    喊炒后改菜（如改成免葱而锅里已有葱）：
    原份已钉死在锅里，熟品按余菜处理；给同一食客另开一张新做法的单，等下一锅。
    """
    sid = body.get("slip_id")
    new_prep = body.get("new_prep")
    if not sid or not new_prep:
        raise ApiError(400, "BAD_REQUEST", "需要 slip_id 与 new_prep")
    slip = _load_slip(c, sid)
    if slip["status"] == "DELIVERED":
        raise ApiError(409, "ALREADY_DELIVERED",
                       f"{sid} 已派盘上桌，不能改菜退款", {})
    if slip["status"] == "CANCELLED":
        raise ApiError(409, "ALREADY_CANCELLED", f"{sid} 已撤", {})
    if slip["status"] == "ENTERED":
        raise ApiError(409, "NOT_LOCKED",
                       f"{sid} 还没喊炒，直接用 /slips/modify 即可", {})
    # 1) 旧单撤下；若成品已起锅，盘转余菜
    c.execute("UPDATE slips SET status='CANCELLED' WHERE id=?", (sid,))
    flipped = c.execute(
        "UPDATE plates SET status='LEFTOVER' "
        "WHERE origin_slip_id=? AND status='READY'", (sid,),
    ).rowcount
    # 2) 同一食客另开新单
    new_sid = next_id(c, "slips", "id", "M", 4)
    c.execute(
        "INSERT INTO slips(id,diner_id,prep,status,qty,note,revised_from,created_at) "
        "VALUES(?,?,?, 'ENTERED', 1, '改菜另开', ?, ?)",
        (new_sid, slip["diner_id"], new_prep, sid, now()),
    )
    record(c, "SLIP_REVISED", sid,
           {"new_slip": new_sid, "new_prep": new_prep,
            "leftover_plates": flipped,
            "reason": "原锅已下料，原份留锅按余菜处理"})
    return {
        "old_slip_id": sid,
        "old_slip_status": "CANCELLED",
        "leftover_plates": flipped,
        "new_slip_id": new_sid,
        "new_prep": new_prep,
        "explanation": "这份已喊炒并下锅，原锅不能给改后的做法；"
                       "熟品转余菜，新单等下一锅炒",
    }


def cancel_slip(c, body):
    sid = body.get("slip_id")
    if not sid:
        raise ApiError(400, "BAD_REQUEST", "需要 slip_id")
    slip = _load_slip(c, sid)
    if slip["status"] == "DELIVERED":
        raise ApiError(409, "ALREADY_DELIVERED",
                       f"{sid} 已派盘上桌，撤单不能退款", {})
    if slip["status"] == "CANCELLED":
        raise ApiError(409, "ALREADY_CANCELLED", f"{sid} 已撤过", {})
    c.execute("UPDATE slips SET status='CANCELLED' WHERE id=?", (sid,))
    # 已起锅的熟品留下当余菜；还在锅里的那份等起锅时自动成余菜
    flipped = c.execute(
        "UPDATE plates SET status='LEFTOVER' "
        "WHERE origin_slip_id=? AND status='READY'", (sid,),
    ).rowcount
    record(c, "SLIP_CANCELLED", sid,
           {"was": slip["status"], "leftover_plates": flipped})
    return {"slip_id": sid, "status": "CANCELLED", "leftover_plates": flipped}


def move_diner_table(c, body):
    """食客换桌：未派的原份跟着食客走，锅里喊过的份数一份不少。"""
    did = body.get("diner_id")
    to_table = body.get("to_table")
    if not did or not to_table:
        raise ApiError(400, "BAD_REQUEST", "需要 diner_id 与 to_table")
    r = c.execute("SELECT * FROM diners WHERE diner_id=?", (did,)).fetchone()
    if not r:
        raise ApiError(404, "NO_DINER", f"食客 {did} 不存在")
    c.execute("UPDATE diners SET table_name=? WHERE diner_id=?",
              (to_table, did))
    record(c, "DINER_MOVED", did,
           {"from": r["table_name"], "to": to_table})
    return {"diner_id": did, "table": to_table}


def transfer_slip(c, body):
    """把一道菜（点菜单）移给另一桌的另一位食客。"""
    sid = body.get("slip_id")
    to_diner = body.get("to_diner_id")
    if not sid or not to_diner:
        raise ApiError(400, "BAD_REQUEST", "需要 slip_id 与 to_diner_id")
    slip = _load_slip(c, sid)
    if not c.execute("SELECT 1 FROM diners WHERE diner_id=?",
                     (to_diner,)).fetchone():
        raise ApiError(404, "NO_DINER", f"目标食客 {to_diner} 不存在")
    if slip["status"] in ("DELIVERED", "CANCELLED"):
        raise ApiError(409, "SLIP_NOT_MOVABLE",
                       f"{sid} 状态 {slip['status']}，不能再移",
                       {"current_status": slip["status"]})
    c.execute("UPDATE slips SET diner_id=? WHERE id=?", (to_diner, sid))
    record(c, "SLIP_TRANSFERRED", sid,
           {"from_diner": slip["diner_id"], "to_diner": to_diner})
    return {"slip_id": sid, "diner_id": to_diner}


def queue_view(c, query):
    prep = query.get("prep", [None])[0]
    if prep:
        rows = c.execute(
            "SELECT s.*, d.table_name, d.name AS diner_name FROM slips s "
            "JOIN diners d ON d.diner_id=s.diner_id "
            "WHERE s.status='ENTERED' AND s.prep=? ORDER BY s.id", (prep,),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT s.*, d.table_name, d.name AS diner_name FROM slips s "
            "JOIN diners d ON d.diner_id=s.diner_id "
            "WHERE s.status='ENTERED' ORDER BY s.prep, s.id",
        ).fetchall()
    return {"queue": [row_to_dict(r) for r in rows]}


def call_wok(c, body):
    """
    炒档喊炒。两种入参：
      {"slip_ids":["M0001","M0002"]}  指定单
      {"prep":"...","qty":2}          自动拉最老的 ENTERED 单
    同做法进同一口在炒的锅；每份钉点菜单号。
    """
    ids = body.get("slip_ids")
    if not ids:
        prep = body.get("prep")
        qty = int(body.get("qty", 1))
        if not prep or qty < 1:
            raise ApiError(400, "BAD_REQUEST",
                           "需要 slip_ids，或 prep+qty 自动拉单")
        rows = c.execute(
            "SELECT id FROM slips WHERE status='ENTERED' AND prep=? "
            "ORDER BY id LIMIT ?", (prep, qty),
        ).fetchall()
        if len(rows) < qty:
            raise ApiError(409, "NOT_ENOUGH_SLIPS",
                           f"做法 {prep} 待炒只有 {len(rows)} 份，要 {qty} 份")
        ids = [r["id"] for r in rows]

    slips = [_load_slip(c, sid) for sid in ids]
    preps = {s["prep"] for s in slips}
    if len(preps) != 1:
        raise ApiError(409, "PREP_MISMATCH",
                       f"同锅必须同做法，收到：{sorted(preps)}")
    for s in slips:
        if s["status"] != "ENTERED":
            raise ApiError(409, "SLIP_NOT_ENTERED",
                           f"{s['id']} 状态 {s['status']}，不能喊炒",
                           {"current_status": s["status"]})
    prep = slips[0]["prep"]

    wok = c.execute(
        "SELECT * FROM woks WHERE prep=? AND status='WOKKING' ORDER BY id",
        (prep,),
    ).fetchone()
    if wok:
        wid = wok["id"]
    else:
        wid = next_id(c, "woks", "id", "W", 4)
        c.execute(
            "INSERT INTO woks(id,prep,status,created_at) "
            "VALUES(?,?, 'WOKKING', ?)", (wid, prep, now()),
        )
        record(c, "WOK_OPENED", wid, {"prep": prep})

    portions = []
    for s in slips:
        cur = c.execute(
            "INSERT INTO portions(wok_id,slip_id,status) VALUES(?,?,'COOKED')",
            (wid, s["id"]),
        )
        c.execute("UPDATE slips SET status='WOK_LOCKED' WHERE id=?",
                  (s["id"],))
        portions.append({"portion_id": cur.lastrowid, "slip_id": s["id"]})
        record(c, "PORTION_LOCKED", wid,
               {"slip_id": s["id"], "portion_id": cur.lastrowid})
    return {"wok_id": wid, "prep": prep, "locked_portions": portions}


def finish_wok(c, body):
    """
    报熟。reported 为实际出锅的 slip_id 列表；缺报的份记 SHORT，点菜单退回队列。
    已撤单的份若真炒出来 -> 余菜；缺报则什么都不产生。
    """
    wid = body.get("wok_id")
    reported = body.get("reported")  # None 表示全熟
    wok = c.execute("SELECT * FROM woks WHERE id=?", (wid,)).fetchone()
    if not wok:
        raise ApiError(404, "NO_WOK", f"锅 {wid} 不存在")
    if wok["status"] != "WOKKING":
        raise ApiError(409, "WOK_DONE", f"锅 {wid} 已起锅", {})
    portions = c.execute(
        "SELECT p.*, s.status AS slip_status FROM portions p "
        "JOIN slips s ON s.id=p.slip_id WHERE p.wok_id=?", (wid,),
    ).fetchall()
    reported_set = set(reported) if reported is not None else \
        {p["slip_id"] for p in portions}

    plates, shorts = [], []
    for p in portions:
        sid = p["slip_id"]
        if sid not in reported_set:
            c.execute("UPDATE portions SET status='SHORT' WHERE id=?",
                      (p["id"],))
            # 没做出来：有效单退回队列重炒；已撤单就直接了结
            if p["slip_status"] != "CANCELLED":
                c.execute(
                    "UPDATE slips SET status='ENTERED' WHERE id=?", (sid,))
            shorts.append({"slip_id": sid, "portion_id": p["id"]})
            record(c, "PORTION_SHORTED", wid, {"slip_id": sid})
            continue
        plate_no = next_id(c, "plates", "plate_id", "P", 4)
        status = "LEFTOVER" if p["slip_status"] == "CANCELLED" else "READY"
        c.execute(
            "INSERT INTO plates(plate_id,portion_id,wok_id,origin_slip_id,"
            "prep,status) VALUES(?,?,?,?,?,?)",
            (plate_no, p["id"], wid, sid, wok["prep"], status),
        )
        plates.append({"plate_id": plate_no, "slip_id": sid, "status": status})
        record(c, "PLATE_READY", plate_no,
               {"wok_id": wid, "slip_id": sid, "status": status})
    c.execute("UPDATE woks SET status='DONE', done_at=? WHERE id=?",
              (now(), wid))
    record(c, "WOK_DONE", wid,
           {"plates": len(plates), "shorted": len(shorts)})
    return {"wok_id": wid, "plates": plates, "shorted": shorts}


def deliver_plate(c, body, idem_key):
    """传菜员按盘号派盘。"""
    if idem_key:
        old = c.execute(
            "SELECT response_json FROM idem_store WHERE idem_key=?",
            (idem_key,),
        ).fetchone()
        if old:
            return json.loads(old["response_json"]) | {"idempotent_replay": True}

    pid = body.get("plate_id")
    table = body.get("table")
    if not pid or not table:
        raise ApiError(400, "BAD_REQUEST", "需要 plate_id 与 table")
    plate = c.execute("SELECT * FROM plates WHERE plate_id=?",
                      (pid,)).fetchone()
    if not plate:
        raise ApiError(404, "NO_PLATE", f"盘 {pid} 不存在")
    if plate["status"] == "LEFTOVER":
        raise ApiError(409, "LEFTOVER_NOT_DELIVERABLE",
                       f"{pid} 是余菜，不能派盘", {})
    if plate["status"] == "DELIVERED":
        d = c.execute("SELECT * FROM deliveries WHERE plate_id=?",
                      (pid,)).fetchone()
        raise ApiError(409, "PLATE_TAKEN",
                       f"{pid} 已派给 {d['table_name']} 桌",
                       {"delivery_id": d["id"], "table": d["table_name"]})
    slip = _load_slip(c, plate["origin_slip_id"])
    diner = c.execute(
        "SELECT * FROM diners WHERE diner_id=?", (slip["diner_id"],),
    ).fetchone()
    if diner["table_name"] != table:
        raise ApiError(
            409, "TABLE_MISMATCH",
            f"盘 {pid} 属于食客 {diner['diner_id']}，其当前在 "
            f"{diner['table_name']} 桌，不是 {table} 桌",
            {"correct_table": diner["table_name"]},
        )
    # CAS：并发抢盘只有一个请求能把 READY 改成 DELIVERED
    cas = c.execute(
        "UPDATE plates SET status='DELIVERED', delivered_table=? "
        "WHERE plate_id=? AND status='READY'", (table, pid),
    ).rowcount
    if cas != 1:
        d = c.execute("SELECT * FROM deliveries WHERE plate_id=?",
                      (pid,)).fetchone()
        raise ApiError(409, "PLATE_TAKEN",
                       f"{pid} 刚被另一请求派走",
                       {"delivery_id": d["id"] if d else None})
    vid = next_id(c, "deliveries", "id", "V", 4)
    c.execute(
        "INSERT INTO deliveries(id,plate_id,slip_id,diner_id,table_name,"
        "idem_key,at) VALUES(?,?,?,?,?,?,?)",
        (vid, pid, slip["id"], diner["diner_id"], table, idem_key, now()),
    )
    c.execute("UPDATE slips SET status='DELIVERED' WHERE id=?", (slip["id"],))
    result = {
        "delivery_id": vid, "plate_id": pid, "table": table,
        "diner_id": diner["diner_id"], "slip_id": slip["id"],
        "prep": plate["prep"],
    }
    record(c, "PLATE_DELIVERED", pid, result)
    if idem_key:
        c.execute(
            "INSERT INTO idem_store(idem_key,response_json,at) VALUES(?,?,?)",
            (idem_key, json.dumps(result, ensure_ascii=False), now()),
        )
    return result


def table_bill(c, table):
    rows = c.execute(
        "SELECT v.id AS delivery_id, v.plate_id, v.slip_id, v.diner_id, "
        "v.table_name, v.at, p.prep, p.wok_id "
        "FROM deliveries v JOIN plates p ON p.plate_id=v.plate_id "
        "WHERE v.table_name=? ORDER BY v.at", (table,),
    ).fetchall()
    items = [row_to_dict(r) for r in rows]
    return {"table": table, "items": items, "count": len(items)}


def plate_view(c, pid):
    p = c.execute("SELECT * FROM plates WHERE plate_id=?", (pid,)).fetchone()
    if not p:
        raise ApiError(404, "NO_PLATE", f"盘 {pid} 不存在")
    d = c.execute("SELECT * FROM deliveries WHERE plate_id=?",
                  (pid,)).fetchone()
    s = c.execute(
        "SELECT s.*, dn.name AS diner_name, dn.table_name "
        "FROM slips s JOIN diners dn ON dn.diner_id=s.diner_id "
        "WHERE s.id=?", (p["origin_slip_id"],),
    ).fetchone()
    return {
        "plate": row_to_dict(p),
        "origin_slip": row_to_dict(s),
        "delivery": row_to_dict(d) if d else None,
    }


def wok_view(c, wid):
    w = c.execute("SELECT * FROM woks WHERE id=?", (wid,)).fetchone()
    if not w:
        raise ApiError(404, "NO_WOK", f"锅 {wid} 不存在")
    portions = c.execute(
        "SELECT p.id AS portion_id, p.status, p.slip_id, s.diner_id, "
        "s.status AS slip_status, pl.plate_id, pl.status AS plate_status "
        "FROM portions p JOIN slips s ON s.id=p.slip_id "
        "LEFT JOIN plates pl ON pl.portion_id=p.id WHERE p.wok_id=?",
        (wid,),
    ).fetchall()
    return {"wok": row_to_dict(w),
            "portions": [row_to_dict(r) for r in portions]}


def leftovers(c):
    rows = c.execute(
        "SELECT p.plate_id, p.wok_id, p.origin_slip_id, p.prep, "
        "s.diner_id, d.table_name "
        "FROM plates p JOIN slips s ON s.id=p.origin_slip_id "
        "JOIN diners d ON d.diner_id=s.diner_id "
        "WHERE p.status='LEFTOVER' ORDER BY p.plate_id",
    ).fetchall()
    return {"leftovers": [row_to_dict(r) for r in rows]}


def state_overview(c):
    def cnt(sql, *a):
        return c.execute(sql, a).fetchone()[0]
    return {
        "diners": cnt("SELECT COUNT(*) FROM diners"),
        "slips_by_status": dict(c.execute(
            "SELECT status, COUNT(*) FROM slips GROUP BY status").fetchall()),
        "woks_by_status": dict(c.execute(
            "SELECT status, COUNT(*) FROM woks GROUP BY status").fetchall()),
        "plates_by_status": dict(c.execute(
            "SELECT status, COUNT(*) FROM plates GROUP BY status").fetchall()),
        "deliveries": cnt("SELECT COUNT(*) FROM deliveries"),
    }


def reset_all(c):
    for t in ("deliveries", "plates", "portions", "woks", "slips",
              "diners", "events", "idem_store"):
        c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM sqlite_sequence")
    record(c, "SHOP_RESET", None, {})
    return {"reset": True}


# ----------------------------- HTTP 层 -----------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ChaanTei/1.0"

    def log_message(self, fmt, *args):
        pass  # 演示脚本自己打印请求，保持输出干净

    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ok(self, data):
        self._send(200, {"ok": True, "data": data})

    def _fail(self, e):
        self._send(e.status,
                   {"ok": False,
                    "error": {"code": e.code, "message": e.message,
                              **e.extra}})

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ApiError(400, "BAD_JSON", "请求体不是合法 JSON")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            with _LOCK:
                c = get_db()
                if path == "/healthz":
                    self._ok({"service": "chaan-tei-kitchen",
                              "mode": "per-portion-tracking"})
                elif path == "/queue":
                    self._ok(queue_view(c, query))
                elif path == "/leftovers":
                    self._ok(leftovers(c))
                elif path == "/state":
                    self._ok(state_overview(c))
                elif path == "/events":
                    rows = c.execute(
                        "SELECT * FROM events ORDER BY id").fetchall()
                    self._ok({"events": [row_to_dict(r) for r in rows]})
                elif path.startswith("/plates/"):
                    self._ok(plate_view(c, path.split("/")[2]))
                elif path.startswith("/wok/"):
                    self._ok(wok_view(c, path.split("/")[2]))
                elif path.startswith("/tables/") and path.endswith("/bill"):
                    self._ok(table_bill(c, path.split("/")[2]))
                elif path == "/":
                    self._ok({"routes": [
                        "POST /diners", "POST /orders",
                        "POST /slips/modify", "POST /slips/revise",
                        "POST /slips/cancel", "POST /slips/transfer",
                        "POST /diners/move", "GET /queue?prep=",
                        "POST /wok/call", "POST /wok/finish",
                        "GET /wok/{id}", "POST /plates/deliver",
                        "GET /plates/{id}", "GET /tables/{t}/bill",
                        "GET /leftovers", "GET /state", "GET /events",
                        "POST /admin/reset",
                    ]})
                else:
                    raise ApiError(404, "NO_ROUTE", f"没有 {path} 这个路由")
        except ApiError as e:
            self._fail(e)
        except Exception as e:  # noqa: BLE001
            self._fail(ApiError(500, "INTERNAL", str(e)))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        idem_key = self.headers.get("Idempotency-Key")
        body = self._read_body()
        try:
            with _LOCK:
                c = get_db()
                if path == "/admin/reset":
                    result = reset_all(c)
                elif path == "/diners":
                    result = seat_diner(c, body)
                elif path == "/orders":
                    result = create_order(c, body)
                elif path == "/slips/modify":
                    result = modify_slip(c, body)
                elif path == "/slips/revise":
                    result = revise_slip(c, body)
                elif path == "/slips/cancel":
                    result = cancel_slip(c, body)
                elif path == "/slips/transfer":
                    result = transfer_slip(c, body)
                elif path == "/diners/move":
                    result = move_diner_table(c, body)
                elif path == "/wok/call":
                    result = call_wok(c, body)
                elif path == "/wok/finish":
                    result = finish_wok(c, body)
                elif path == "/plates/deliver":
                    result = deliver_plate(c, body, idem_key)
                else:
                    raise ApiError(404, "NO_ROUTE", f"没有 {path} 这个路由")
                checkpoint(c)  # 提交并回写主库，重启即可凭号查回
                self._ok(result)
        except ApiError as e:
            try:
                get_db().rollback()
            except sqlite3.Error:
                pass
            self._fail(e)
        except Exception as e:  # noqa: BLE001
            try:
                get_db().rollback()
            except sqlite3.Error:
                pass
            self._fail(ApiError(500, "INTERNAL", str(e)))


def main():
    import argparse
    import signal
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CHAAN_PORT", "8617")))
    args = ap.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True

    def _checkpoint(signum, frame):
        # 退出前把 WAL 全部回写主库；不在信号处理器里调 shutdown()，
        # 那会与占用主线程的 serve_forever() 互等死锁。
        try:
            c = get_db()
            c.commit()
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        if signum is not None:  # 信号路径：落盘后结束进程
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, _checkpoint)
    signal.signal(signal.SIGINT, _checkpoint)
    print(f"茶餐厅后厨开门：http://127.0.0.1:{args.port}  库={DB_PATH}",
          flush=True)
    try:
        httpd.serve_forever()
    finally:
        _checkpoint(None, None)


if __name__ == "__main__":
    main()
