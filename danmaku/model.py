"""领域模型：格点、轨迹、遮挡、订单、播出指令。

轨迹约定
--------
一条宽 ``width`` 格、速度 ``speed`` 格/拍 的句子，若在 ``start`` 拍把
最右格送到第 W 列（入口列），则在第 ``beat`` 拍它占用的最右列是::

    rightmost(beat) = W - (beat - start) * speed

该拍占据 ``width`` 个连续列 ``[rightmost-width+1, rightmost]``。
一条弹幕“完整展示”当且仅当：它依次经过每一个可见列（拍数为 W），
途中每个可见格都未被字幕遮挡，也没有和别的弹幕同拍同轨同格。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# 订单状态
ORDER_PENDING = "PENDING"        # 预售：尚未锁到完整路径
ORDER_LOCKED = "LOCKED"          # 预售：路径已排定并占住
ORDER_QUEUED = "QUEUED"          # 现场候播：在池内等下一次进度
ORDER_DISPATCHED = "DISPATCHED"  # 指令已发往该屏，尚未收到开播回条
ORDER_PLAYING = "PLAYING"        # 已收到开播回条，正在滚动
ORDER_SETTLED = "SETTLED"        # 约定的完整展示全部结清
ORDER_BROKEN = "BROKEN"          # 合同被改坏，无可行路径（可追溯买家）
ORDER_EXPIRED = "EXPIRED"        # 现场窗口已过，仍欠展示

# 指令状态
INSTR_PLANNED = "PLANNED"        # 预售排定，尚未下发
INSTR_SENT = "SENT"              # 已进入出件箱、发往该屏
INSTR_STARTED = "STARTED"        # 收到开播回条，轨迹在途
INSTR_DONE = "DONE"              # 实际播完，一次完整展示兑现
INSTR_STOPPED = "STOPPED"        # 中途停播，未滚完


def cell_key(beat: int, track: int, col: int) -> str:
    """占用索引的键：拍/轨/列。"""
    return f"{beat}:{track}:{col}"


@dataclass
class Step:
    """一拍上的一次占用（轨道、列区间为闭区间 [left_col, right_col]）。"""

    beat: int
    track: int
    left_col: int
    right_col: int

    def cells(self) -> list[str]:
        return [
            cell_key(self.beat, self.track, c)
            for c in range(self.left_col, self.right_col + 1)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "beat": self.beat,
            "track": self.track,
            "left_col": self.left_col,
            "right_col": self.right_col,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Step":
        return cls(d["beat"], d["track"], d["left_col"], d["right_col"])


@dataclass
class Trajectory:
    """一条弹幕的完整路径：起始拍、轨道、逐拍步长与可见性。"""

    order_id: str
    track: int
    start: int
    width: int
    speed: int
    steps: list[Step] = field(default_factory=list)
    visible_steps: list[Step] = field(default_factory=list)
    end_beat: int = 0

    def visible_cells(self) -> set[str]:
        cells: set[str] = set()
        for s in self.visible_steps:
            cells.update(s.cells())
        return cells

    def all_cells(self) -> set[str]:
        cells: set[str] = set()
        for s in self.steps:
            cells.update(s.cells())
        return cells

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "track": self.track,
            "start": self.start,
            "width": self.width,
            "speed": self.speed,
            "steps": [s.to_dict() for s in self.steps],
            "visible_steps": [s.to_dict() for s in self.visible_steps],
            "end_beat": self.end_beat,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trajectory":
        return cls(
            order_id=d["order_id"],
            track=d["track"],
            start=d["start"],
            width=d["width"],
            speed=d["speed"],
            steps=[Step.from_dict(s) for s in d["steps"]],
            visible_steps=[Step.from_dict(s) for s in d["visible_steps"]],
            end_beat=d["end_beat"],
        )


def build_trajectory(
    order_id: str,
    width: int,
    speed: int,
    screen_w: int,
    track: int,
    start: int,
) -> Trajectory:
    """按“入口拍 start、每拍左移 speed 格”构造轨迹。

    可见步长要求连续覆盖列 W..1，共 W 拍；弹幕完全离开左边界后结束。
    若速度不能整除相关跨度，最后一段可见步长可能只含部分列——那种参数
    组合无法完整展示，由调用方通过可见性检查拒绝。
    """

    traj = Trajectory(
        order_id=order_id,
        track=track,
        start=start,
        width=width,
        speed=speed,
    )
    beat = start
    # 入口：最右格落在第 W 列。句子尚未完全入场也要逐步记录，直到完全离场。
    while True:
        rightmost = screen_w - (beat - start) * speed
        leftmost = rightmost - width + 1
        step = Step(
            beat=beat,
            track=track,
            left_col=leftmost,
            right_col=rightmost,
        )
        traj.steps.append(step)
        if 1 <= leftmost <= screen_w or 1 <= rightmost <= screen_w:
            # 与可见屏幕有交集的部分记为可见步长。
            vis = Step(
                beat=beat,
                track=track,
                left_col=max(1, leftmost),
                right_col=min(screen_w, rightmost),
            )
            if vis.left_col <= vis.right_col:
                traj.visible_steps.append(vis)
        beat += 1
        if rightmost < 1:
            break
    traj.end_beat = traj.steps[-1].beat
    return traj


def trajectory_is_complete(traj: Trajectory, screen_w: int) -> bool:
    """完整展示的必要条件：W 个可见拍恰好把第 W..1 列各经过一次。"""

    seen: set[int] = set()
    for s in traj.visible_steps:
        for c in range(s.left_col, s.right_col + 1):
            seen.add(c)
    return len(traj.visible_steps) == screen_w and seen == set(range(1, screen_w + 1))


@dataclass
class Occlusion:
    """不可遮盖的字幕条：[from_beat, to_beat] 拍、轨道 track、列区间。"""

    occ_id: str
    from_beat: int
    to_beat: int
    track: int
    left_col: int
    right_col: int

    def cells(self) -> set[str]:
        out: set[str] = set()
        for b in range(self.from_beat, self.to_beat + 1):
            for c in range(self.left_col, self.right_col + 1):
                out.add(cell_key(b, self.track, c))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "occ_id": self.occ_id,
            "from_beat": self.from_beat,
            "to_beat": self.to_beat,
            "track": self.track,
            "left_col": self.left_col,
            "right_col": self.right_col,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Occlusion":
        return cls(
            d["occ_id"],
            d["from_beat"],
            d["to_beat"],
            d["track"],
            d["left_col"],
            d["right_col"],
        )


@dataclass
class Order:
    """一张弹幕票：买家买的是 ``quantity`` 次“完整看完一条”的机会。"""

    order_id: str
    buyer: str
    width: int
    speed: int
    window_start: int
    window_end: int          # 入口拍允许的最晚一拍
    quantity: int
    mode: str                # "presale" | "pool"
    allowed_tracks: list[int]
    status: str = ORDER_PENDING
    seq: int = 0             # 下单序号，决定先来后到
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "buyer": self.buyer,
            "width": self.width,
            "speed": self.speed,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "quantity": self.quantity,
            "mode": self.mode,
            "allowed_tracks": list(self.allowed_tracks),
            "status": self.status,
            "seq": self.seq,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Order":
        return cls(**d)


@dataclass
class Instruction:
    """播控真正发往某块屏的一条播出指令。"""

    instr_id: str
    order_id: str
    buyer: str
    track: int
    start: int
    width: int
    speed: int
    screen_id: str
    status: str = INSTR_PLANNED
    trajectory: Optional[Trajectory] = None
    receipt_id: Optional[str] = None
    last_played_beat: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instr_id": self.instr_id,
            "order_id": self.order_id,
            "buyer": self.buyer,
            "track": self.track,
            "start": self.start,
            "width": self.width,
            "speed": self.speed,
            "screen_id": self.screen_id,
            "status": self.status,
            "trajectory": self.trajectory.to_dict() if self.trajectory else None,
            "receipt_id": self.receipt_id,
            "last_played_beat": self.last_played_beat,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Instruction":
        return cls(
            instr_id=d["instr_id"],
            order_id=d["order_id"],
            buyer=d["buyer"],
            track=d["track"],
            start=d["start"],
            width=d["width"],
            speed=d["speed"],
            screen_id=d["screen_id"],
            status=d["status"],
            trajectory=(
                Trajectory.from_dict(d["trajectory"]) if d.get("trajectory") else None
            ),
            receipt_id=d.get("receipt_id"),
            last_played_beat=d.get("last_played_beat"),
        )


@dataclass
class OutboxItem:
    """发往请求方（该屏/播控）的指令出件记录；claim 后被取走。"""

    outbox_id: str
    screen_id: str
    instr_id: str
    payload: dict[str, Any]
    claimed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "outbox_id": self.outbox_id,
            "screen_id": self.screen_id,
            "instr_id": self.instr_id,
            "payload": self.payload,
            "claimed": self.claimed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OutboxItem":
        return cls(**d)
