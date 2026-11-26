"""SQLite 持久化层。

关键不变量由数据库约束和状态机共同保证：
- shot_log 对 (paper_id, seq) 建唯一约束，同一张纸同一拍不可重复落账；
- paper 的 accumulated 只由曝光回条累加更新，没有任何清零/回退路径；
- 批次进入 prepared/open 后，遮片登记表冻结，开工后不再补刻；
- 所有状态都在本地文件里，进程重开后能原样接回。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .domain import LabError, NotFoundError, ConflictError, ValidationError

SCHEMA = """
CREATE TABLE IF NOT EXISTS batch (
    id          TEXT PRIMARY KEY,
    darkroom    TEXT NOT NULL,
    rows        INTEGER NOT NULL,
    cols        INTEGER NOT NULL,
    card_json   TEXT NOT NULL,
    sheet_budget INTEGER NOT NULL,
    shot_budget INTEGER NOT NULL,
    status      TEXT NOT NULL,              -- intake/prepared/open/exposing/closed
    plan_json   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS paper (
    id          TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batch(id),
    start_json  TEXT NOT NULL,              -- 入件时每格已吃光量
    beam_json   TEXT NOT NULL,              -- 每拍每格透光量
    accumulated_json TEXT NOT NULL,         -- 截至当前的累计光量（只增不减）
    status      TEXT NOT NULL,              -- exposed/printing/printed/delivered
    reused      INTEGER NOT NULL DEFAULT 0, -- 沿用已付费旧试印
    plan_json   TEXT,
    product_path TEXT
);
CREATE TABLE IF NOT EXISTS "order" (
    id          TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batch(id),
    customer    TEXT NOT NULL,
    reuse_count INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,              -- intake/confirmed/printing/ready/delivered
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS requirement (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT NOT NULL REFERENCES "order"(id),
    paper_id    TEXT NOT NULL REFERENCES paper(id),
    cell        TEXT NOT NULL,
    role        TEXT NOT NULL,              -- face/background
    level       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mask_registration (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL REFERENCES batch(id),
    shape_json  TEXT NOT NULL,
    offset_json TEXT NOT NULL,
    openings_json TEXT NOT NULL,
    UNIQUE(batch_id, shape_json, offset_json)
);
CREATE TABLE IF NOT EXISTS shot_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id    TEXT NOT NULL REFERENCES paper(id),
    seq         INTEGER NOT NULL,
    mask_shape_json TEXT NOT NULL,
    offset_json TEXT NOT NULL,
    exposures_json TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(paper_id, seq)
);
CREATE TABLE IF NOT EXISTS sample (
    id          TEXT PRIMARY KEY,
    paper_id    TEXT NOT NULL REFERENCES paper(id),
    observed_json TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS stock_trial (
    id          TEXT PRIMARY KEY,
    level_name  TEXT NOT NULL,
    held_by_order TEXT REFERENCES "order"(id)
);
"""


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text: Optional[str]) -> Any:
    return json.loads(text) if text is not None else None


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._closed = False
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.conn.commit()
        self.conn.close()

    # ---- 基础工具 ----
    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row:
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            raise NotFoundError(sql)
        return row

    def get_batch(self, batch_id: str) -> sqlite3.Row:
        try:
            return self._one("SELECT * FROM batch WHERE id=?", (batch_id,))
        except NotFoundError:
            raise NotFoundError(f"批次 {batch_id} 不存在")

    def get_order(self, order_id: str) -> sqlite3.Row:
        try:
            return self._one('SELECT * FROM "order" WHERE id=?', (order_id,))
        except NotFoundError:
            raise NotFoundError(f"订单 {order_id} 不存在")

    def get_paper(self, paper_id: str) -> sqlite3.Row:
        try:
            return self._one("SELECT * FROM paper WHERE id=?", (paper_id,))
        except NotFoundError:
            raise NotFoundError(f"纸 {paper_id} 不存在")

    def list_papers(self, batch_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM paper WHERE batch_id=? ORDER BY id", (batch_id,)
        ))

    def list_requirements(self, order_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM requirement WHERE order_id=? ORDER BY id", (order_id,)
        ))

    def list_masks(self, batch_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM mask_registration WHERE batch_id=? ORDER BY id", (batch_id,)
        ))

    def list_shots(self, paper_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM shot_log WHERE paper_id=? ORDER BY seq", (paper_id,)
        ))

    # ---- 建单 ----
    def create_batch(self, batch_id: str, darkroom: str, rows: int, cols: int,
                     card: list[dict], sheet_budget: int, shot_budget: int) -> None:
        try:
            self.conn.execute(
                "INSERT INTO batch(id, darkroom, rows, cols, card_json, sheet_budget,"
                " shot_budget, status) VALUES(?,?,?,?,?,?,?, 'intake')",
                (batch_id, darkroom, rows, cols, dumps(card), sheet_budget, shot_budget),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"批次 {batch_id} 已存在")
        self.conn.commit()

    def add_paper(self, paper_id: str, batch_id: str, start: dict, beam: dict,
                  reused: bool = False) -> None:
        try:
            self.conn.execute(
                "INSERT INTO paper(id, batch_id, start_json, beam_json, accumulated_json,"
                " status, reused) VALUES(?,?,?,?,?, 'exposed', ?)",
                (paper_id, batch_id, dumps(start), dumps(beam), dumps(dict(start)),
                 1 if reused else 0),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"纸号 {paper_id} 已存在，同一张纸不能重复接件")
        self.conn.commit()

    def create_order(self, order_id: str, batch_id: str, customer: str,
                     reuse_count: int, requirements: list[dict]) -> None:
        self.conn.execute(
            'INSERT INTO "order"(id, batch_id, customer, reuse_count, status)'
            " VALUES(?,?,?,?, 'intake')",
            (order_id, batch_id, customer, reuse_count),
        )
        for req in requirements:
            self.conn.execute(
                "INSERT INTO requirement(order_id, paper_id, cell, role, level)"
                " VALUES(?,?,?,?,?)",
                (order_id, req["paper_id"], req["cell"], req["role"], req["level"]),
            )
        self.conn.commit()

    def add_stock_trial(self, trial_id: str, level_name: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO stock_trial(id, level_name) VALUES(?,?)",
            (trial_id, level_name),
        )
        self.conn.commit()

    def count_free_trials(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM stock_trial WHERE held_by_order IS NULL"
        ).fetchone()["n"]

    # ---- 备料 / 开批 ----
    def set_batch_status(self, batch_id: str, status: str, plan: Optional[dict] = None) -> None:
        self.conn.execute(
            "UPDATE batch SET status=?, plan_json=? WHERE id=?",
            (status, dumps(plan) if plan is not None else self.get_batch(batch_id)["plan_json"],
             batch_id),
        )
        self.conn.commit()

    def register_mask(self, batch_id: str, shape: list[list[int]], offset: list[int],
                      openings: list[list[int]]) -> None:
        # 开工后遮片登记冻结：状态离开 intake 的任何登记都拒绝
        if self.get_batch(batch_id)["status"] != "intake":
            raise ConflictError(
                f"批次 {batch_id} 已开工，遮片开工后不再补刻"
            )
        self.conn.execute(
            "INSERT OR IGNORE INTO mask_registration(batch_id, shape_json, offset_json,"
            " openings_json) VALUES(?,?,?,?)",
            (batch_id, dumps(shape), dumps(offset), dumps(openings)),
        )
        self.conn.commit()

    def freeze_masks(self, batch_id: str) -> None:
        """开工后遮片登记冻结：再登记即冲突。"""
        status = self.get_batch(batch_id)["status"]
        if status != "intake":
            raise ConflictError(
                f"批次 {batch_id} 已处于 {status}，开工后不再补刻遮片"
            )

    def save_paper_plan(self, paper_id: str, plan: dict) -> None:
        self.conn.execute(
            "UPDATE paper SET plan_json=?, status='printing' WHERE id=?",
            (dumps(plan), paper_id),
        )
        self.conn.commit()

    # ---- 曝光回条 ----
    def next_shot_seq(self, paper_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS s FROM shot_log WHERE paper_id=?",
            (paper_id,),
        ).fetchone()
        return int(row["s"])

    def append_shot(self, paper_id: str, seq: int, mask_shape: list, offset: list,
                    exposures: dict, new_accumulated: dict) -> None:
        try:
            self.conn.execute(
                "INSERT INTO shot_log(paper_id, seq, mask_shape_json, offset_json,"
                " exposures_json) VALUES(?,?,?,?,?)",
                (paper_id, seq, dumps(mask_shape), dumps(offset), dumps(exposures)),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(f"纸 {paper_id} 第 {seq} 拍已落账，回条迟到不能当成新拍再曝光")
        # 累计光量只做加法，且由服务端按登记拍法计算后整体覆盖写入（值本身严格递增）
        self.conn.execute(
            "UPDATE paper SET accumulated_json=? WHERE id=?",
            (dumps(new_accumulated), paper_id),
        )
        self.conn.commit()

    def mark_paper_printed(self, paper_id: str, product_path: str) -> None:
        self.conn.execute(
            "UPDATE paper SET status='printed', product_path=? WHERE id=?",
            (product_path, paper_id),
        )
        self.conn.commit()

    # ---- 接样 / 取件 ----
    def add_sample(self, sample_id: str, paper_id: str, observed: dict, note: str) -> None:
        self.conn.execute(
            "INSERT INTO sample(id, paper_id, observed_json, note) VALUES(?,?,?,?)",
            (sample_id, paper_id, dumps(observed), note),
        )
        self.conn.commit()

    def set_order_status(self, order_id: str, status: str) -> None:
        self.conn.execute('UPDATE "order" SET status=? WHERE id=?', (status, order_id))
        self.conn.commit()

    def hold_trials(self, order_id: str, count: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT id FROM stock_trial WHERE held_by_order IS NULL ORDER BY id LIMIT ?",
            (count,),
        ).fetchall()
        if len(rows) < count:
            raise ConflictError(
                f"需要 {count} 张已付费旧试印，库存可沿用仅 {len(rows)} 张"
            )
        ids = [r["id"] for r in rows]
        self.conn.executemany(
            "UPDATE stock_trial SET held_by_order=? WHERE id=?",
            [(order_id, tid) for tid in ids],
        )
        self.conn.commit()
        return ids

    def deliver_paper(self, paper_id: str) -> None:
        self.conn.execute(
            "UPDATE paper SET status='delivered' WHERE id=?", (paper_id,)
        )
        self.conn.commit()
