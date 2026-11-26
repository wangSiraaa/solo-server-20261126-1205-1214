"""状态持久化、事件日志与重放。

每个变更命令先追加一条 JSONL 事件，再落一份完整 state.json。
重启后直接读 state.json；``replay`` 从空状态按事件顺序重放，
应得到字节级一致的状态，用于本机回放核对。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from .model import Instruction, Occlusion, Order, OutboxItem


@dataclass
class ShowState:
    show_id: str
    screen_id: str
    width: int
    tracks: int
    mode: str                                   # "presale" | "pool"
    occlusions: list[Occlusion] = field(default_factory=list)
    orders: dict[str, Order] = field(default_factory=dict)
    instructions: dict[str, Instruction] = field(default_factory=dict)
    outbox: list[OutboxItem] = field(default_factory=list)
    seen_receipts: list[str] = field(default_factory=list)
    current_beat: int = 0
    order_seq: int = 0
    counters: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "show_id": self.show_id,
            "screen_id": self.screen_id,
            "width": self.width,
            "tracks": self.tracks,
            "mode": self.mode,
            "occlusions": [o.to_dict() for o in self.occlusions],
            "orders": {k: o.to_dict() for k, o in self.orders.items()},
            "instructions": {
                k: i.to_dict() for k, i in self.instructions.items()
            },
            "outbox": [x.to_dict() for x in self.outbox],
            "seen_receipts": list(self.seen_receipts),
            "current_beat": self.current_beat,
            "order_seq": self.order_seq,
            "counters": dict(self.counters),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ShowState":
        return cls(
            show_id=d["show_id"],
            screen_id=d["screen_id"],
            width=d["width"],
            tracks=d["tracks"],
            mode=d["mode"],
            occlusions=[Occlusion.from_dict(x) for x in d["occlusions"]],
            orders={k: Order.from_dict(v) for k, v in d["orders"].items()},
            instructions={
                k: Instruction.from_dict(v) for k, v in d["instructions"].items()
            },
            outbox=[OutboxItem.from_dict(x) for x in d["outbox"]],
            seen_receipts=list(d["seen_receipts"]),
            current_beat=d["current_beat"],
            order_seq=d["order_seq"],
            counters=dict(d["counters"]),
        )


class Store:
    """单个节目的目录状态库。"""

    STATE_NAME = "state.json"
    LOG_NAME = "events.jsonl"

    def __init__(self, state: ShowState, base_dir: str, replay: bool = False):
        self.state = state
        self.base_dir = base_dir
        self.replay = replay
        os.makedirs(base_dir, exist_ok=True)

    # ---- 生命周期 -------------------------------------------------------

    @classmethod
    def create(cls, base_dir: str, show_id: str, screen_id: str,
               width: int, tracks: int, mode: str) -> "Store":
        if os.path.exists(os.path.join(base_dir, cls.STATE_NAME)):
            raise ValueError(f"{base_dir} 下已有节目状态，不能重复开播")
        state = ShowState(
            show_id=show_id,
            screen_id=screen_id,
            width=width,
            tracks=tracks,
            mode=mode,
        )
        store = cls(state, base_dir)
        store.record(
            "show_created",
            {
                "show_id": show_id,
                "screen_id": screen_id,
                "width": width,
                "tracks": tracks,
                "mode": mode,
            },
        )
        return store

    @classmethod
    def open(cls, base_dir: str) -> "Store":
        path = os.path.join(base_dir, cls.STATE_NAME)
        if not os.path.exists(path):
            raise FileNotFoundError(f"{path} 不存在，先用 init 开播")
        with open(path, "r", encoding="utf-8") as f:
            return cls(ShowState.from_dict(json.load(f)), base_dir)

    # ---- 事件与落盘 -----------------------------------------------------

    def record(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {"type": event_type, "payload": payload}
        if not self.replay:
            with open(os.path.join(self.base_dir, self.LOG_NAME),
                      "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            self._flush()
        return event

    def _flush(self) -> None:
        tmp = os.path.join(self.base_dir, self.STATE_NAME + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state.to_dict(), f, ensure_ascii=False,
                      indent=2, sort_keys=True)
        os.replace(tmp, os.path.join(self.base_dir, self.STATE_NAME))

    def next_id(self, prefix: str) -> str:
        n = self.state.counters.get(prefix, 0) + 1
        self.state.counters[prefix] = n
        return f"{prefix}-{n:04d}"

    def read_log(self) -> list[dict[str, Any]]:
        path = os.path.join(self.base_dir, self.LOG_NAME)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


def replay_dir(base_dir: str) -> tuple[ShowState, list[dict[str, Any]], ShowState]:
    """从事件日志重放，返回 (重放态, 事件列表, 磁盘态) 供比对。"""

    from .engine import Engine  # 局部导入避免环依赖

    events = []
    path = os.path.join(base_dir, Store.LOG_NAME)
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))
    if not events or events[0]["type"] != "show_created":
        raise ValueError("事件日志缺少 show_created 起点")

    first = events[0]["payload"]
    state = ShowState(
        show_id=first["show_id"],
        screen_id=first["screen_id"],
        width=first["width"],
        tracks=first["tracks"],
        mode=first["mode"],
    )
    replayed = Store(state, base_dir, replay=True)
    engine = Engine(replayed)
    engine.apply_log_events(events)

    disk = Store.open(base_dir)
    return replayed.state, events, disk.state
