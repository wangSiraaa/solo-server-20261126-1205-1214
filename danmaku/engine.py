"""排期与履约引擎。

占用模型
========
引擎维护一张“已占格”索引，来源有三类：

1. 不可遮盖字幕（所有拍/轨/列永久占用，直到字幕被撤）；
2. 已锁定的预售轨迹（票卖出即占住，新单不可挤掉）；
3. 已实际下发并收到开播回条、尚未滚完的在途指令（物理占用）。

一条轨迹被接受当且仅当它的每个可见格都不与上述索引相撞，且能完整
经过第 W..1 列。已下发但未开播的预售指令同样保留其锁定占用。

事件溯源
========
所有变更都先写事件再改状态；``apply_log_events`` 是重启/重放入口。
"""

from __future__ import annotations

from typing import Any, Optional

from .model import (
    ORDER_BROKEN,
    ORDER_DISPATCHED,
    ORDER_EXPIRED,
    ORDER_LOCKED,
    ORDER_PENDING,
    ORDER_PLAYING,
    ORDER_QUEUED,
    ORDER_SETTLED,
    INSTR_DONE,
    INSTR_PLANNED,
    INSTR_SENT,
    INSTR_STARTED,
    INSTR_STOPPED,
    Instruction,
    Occlusion,
    Order,
    OutboxItem,
    Trajectory,
    build_trajectory,
    cell_key,
    trajectory_is_complete,
)
from .store import Store


class Engine:
    def __init__(self, engine_store: Store):
        self.store = engine_store

    @property
    def s(self):
        return self.store.state

    # ====================================================================
    # 占用索引
    # ====================================================================

    def occlusion_cells(self) -> dict[str, str]:
        """字幕格 -> 字幕 id。"""
        out: dict[str, str] = {}
        for occ in self.s.occlusions:
            for c in occ.cells():
                out[c] = occ.occ_id
        return out

    def occupied_cells(self) -> dict[str, tuple[str, str]]:
        """已占格 -> (来源类型, 来源 id)。来源类型：occ/lock/live。"""
        out: dict[str, tuple[str, str]] = {}
        for occ in self.s.occlusions:
            for c in occ.cells():
                out[c] = ("occ", occ.occ_id)
        for instr in self.s.instructions.values():
            if instr.trajectory is None:
                continue
            if instr.status in (INSTR_PLANNED, INSTR_SENT, INSTR_STARTED):
                # 锁定未播、已发在途都占着；DONE/STOPPED 已释放。
                kind = "live" if instr.status == INSTR_STARTED else "lock"
                for c in instr.trajectory.visible_cells():
                    out.setdefault(c, (kind, instr.instr_id))
        return out

    def find_path(
        self,
        order: Order,
        *,
        ignore_order_id: Optional[str] = None,
        occupied: Optional[dict[str, tuple[str, str]]] = None,
    ) -> Optional[Trajectory]:
        """在订单窗口与允许轨道内找一条完整、无碰撞的路径。

        选择策略：先按轨道（订单允许序）、再按入口拍从早到晚，确定性
        分配。``ignore_order_id`` 用于重排时忽略该订单自己的旧占用。
        """
        if occupied is None:
            occupied = self.occupied_cells()
        for track in order.allowed_tracks:
            for start in range(order.window_start, order.window_end + 1):
                traj = build_trajectory(
                    order.order_id,
                    order.width,
                    order.speed,
                    self.s.width,
                    track,
                    start,
                )
                if not trajectory_is_complete(traj, self.s.width):
                    continue
                clash = False
                for c in traj.visible_cells():
                    owner = occupied.get(c)
                    if owner is None:
                        continue
                    if ignore_order_id and owner[1].startswith("I-"):
                        # 指令 id 形如 I-xxxx，归属订单记录在指令上。
                        owner_order = self.s.instructions[owner[1]].order_id
                        if owner_order == ignore_order_id:
                            continue
                    clash = True
                    break
                if not clash:
                    return traj
        return None

    # ====================================================================
    # 预售：下单占座、字幕变更与重排
    # ====================================================================

    def place_presale_order(
        self,
        order_id: str,
        buyer: str,
        width: int,
        speed: int,
        window_start: int,
        window_end: int,
        quantity: int = 1,
        allowed_tracks: Optional[list[int]] = None,
        note: str = "",
    ) -> dict[str, Any]:
        if self.s.mode != "presale":
            raise ValueError(f"节目 {self.s.show_id} 不是预售模式")
        if order_id in self.s.orders:
            raise ValueError(f"订单 {order_id} 已存在")
        if quantity != 1:
            raise ValueError("预售单数量固定为 1 次完整展示（票）")
        tracks = allowed_tracks or list(range(1, self.s.tracks + 1))
        self._check_tracks(tracks)
        self.s.order_seq += 1
        order = Order(
            order_id=order_id,
            buyer=buyer,
            width=width,
            speed=speed,
            window_start=window_start,
            window_end=window_end,
            quantity=quantity,
            mode="presale",
            allowed_tracks=tracks,
            status=ORDER_PENDING,
            seq=self.s.order_seq,
            note=note,
        )
        self.s.orders[order_id] = order

        traj = self.find_path(order)
        result: dict[str, Any]
        if traj is None:
            order.status = ORDER_BROKEN
            result = {
                "order_id": order_id,
                "accepted": False,
                "reason": "窗口内无完整无碰撞路径，不予出售",
            }
        else:
            instr = self._lock_trajectory(order, traj)
            order.status = ORDER_LOCKED
            result = {
                "order_id": order_id,
                "accepted": True,
                "instr_id": instr.instr_id,
                "track": traj.track,
                "start": traj.start,
                "visible_beats": [s.beat for s in traj.visible_steps],
                "cells": sorted(traj.visible_cells()),
            }
        self.store.record("presale_placed", {"order": order.to_dict(),
                                             "result": result})
        return result

    def _lock_trajectory(self, order: Order, traj: Trajectory) -> Instruction:
        instr_id = self.store.next_id("I")
        instr = Instruction(
            instr_id=instr_id,
            order_id=order.order_id,
            buyer=order.buyer,
            track=traj.track,
            start=traj.start,
            width=order.width,
            speed=order.speed,
            screen_id=self.s.screen_id,
            status=INSTR_PLANNED,
            trajectory=traj,
        )
        self.s.instructions[instr_id] = instr
        return instr

    def retry_broken_order(self, order_id: str) -> dict[str, Any]:
        """字幕调整后，给曾被改坏的票重新排路径。"""
        order = self._require_order(order_id)
        if order.status != ORDER_BROKEN:
            raise ValueError(f"订单 {order_id} 状态 {order.status}，无需重试")
        traj = self.find_path(order)
        if traj is None:
            result = {"order_id": order_id, "accepted": False,
                      "reason": "仍无完整无碰撞路径"}
        else:
            instr = self._lock_trajectory(order, traj)
            order.status = ORDER_LOCKED
            result = {
                "order_id": order_id,
                "accepted": True,
                "instr_id": instr.instr_id,
                "track": traj.track,
                "start": traj.start,
                "visible_beats": [s.beat for s in traj.visible_steps],
                "cells": sorted(traj.visible_cells()),
            }
        self.store.record("order_retried",
                          {"order_id": order_id, "result": result})
        return result

    def add_occlusion(self, occ_id: str, from_beat: int, to_beat: int,
                      track: int, left_col: int, right_col: int) -> dict[str, Any]:
        if any(o.occ_id == occ_id for o in self.s.occlusions):
            raise ValueError(f"字幕 {occ_id} 已存在")
        self._check_tracks([track])
        occ = Occlusion(occ_id, from_beat, to_beat, track, left_col, right_col)
        self.s.occlusions.append(occ)
        affected = self._revalidate_after_occlusion_change()
        result = {"occ_id": occ_id, "affected_orders": affected}
        self.store.record("occlusion_added",
                          {"occlusion": occ.to_dict(), "result": result})
        return result

    def remove_occlusion(self, occ_id: str) -> dict[str, Any]:
        before = len(self.s.occlusions)
        self.s.occlusions = [o for o in self.s.occlusions
                             if o.occ_id != occ_id]
        if len(self.s.occlusions) == before:
            raise ValueError(f"字幕 {occ_id} 不存在")
        affected = self._revalidate_after_occlusion_change()
        result = {"occ_id": occ_id, "affected_orders": affected}
        self.store.record("occlusion_removed",
                          {"occ_id": occ_id, "result": result})
        return result

    def _revalidate_after_occlusion_change(self) -> list[dict[str, Any]]:
        """字幕变更后重排所有尚未实际播出的预售票。

        规则：已 SENT（未收到开播回条）的指令可重排；STARTED 的在途
        轨迹物理占用不可动；LOCKED/PLANNED 当然可重排。按下单先后
        （seq）依次重新找位，先来的票优先。找不到位的票置 BROKEN，
        并在返回值里点名具体买家。
        """
        affected: list[dict[str, Any]] = []
        movable = [
            (o, self._instr_of_order(o.order_id))
            for o in sorted(self.s.orders.values(), key=lambda x: x.seq)
            if o.mode == "presale"
            and o.status in (ORDER_LOCKED, ORDER_BROKEN)
        ]
        # 先把可移动指令的旧占用从索引里拿掉，再逐个重排。
        live = self.occupied_cells()  # 字幕已在其中
        movable_ids = {
            instr.instr_id for _, instr in movable if instr is not None
            and instr.status in (INSTR_PLANNED, INSTR_SENT)
        }
        work_index = {
            c: owner for c, owner in live.items()
            if not (owner[0] == "lock" and owner[1] in movable_ids)
        }
        for order, old_instr in movable:
            if old_instr is not None and old_instr.instr_id in movable_ids:
                del self.s.instructions[old_instr.instr_id]
            traj = self.find_path(order, ignore_order_id=order.order_id,
                                  occupied=work_index)
            if traj is None:
                order.status = ORDER_BROKEN
                affected.append({
                    "order_id": order.order_id,
                    "buyer": order.buyer,
                    "broken": True,
                    "reason": "字幕变更后窗口内无可行路径，合同被改坏",
                })
                continue
            instr = self._lock_trajectory(order, traj)
            for c in traj.visible_cells():
                work_index[c] = ("lock", instr.instr_id)
            # 安排与原指令完全相同（同轨同拍）说明这张票没被字幕碰到，
            # 不计入“受影响”名单；只有真正挪了位的票才点名。
            moved = (old_instr is None
                     or old_instr.track != traj.track
                     or old_instr.start != traj.start)
            order.status = ORDER_LOCKED
            if moved:
                affected.append({
                    "order_id": order.order_id,
                    "buyer": order.buyer,
                    "broken": False,
                    "instr_id": instr.instr_id,
                    "track": traj.track,
                    "start": traj.start,
                })
        return affected

    def _instr_of_order(self, order_id: str) -> Optional[Instruction]:
        for instr in self.s.instructions.values():
            if instr.order_id == order_id:
                return instr
        return None

    # ====================================================================
    # 现场候播池
    # ====================================================================

    def place_pool_order(
        self,
        order_id: str,
        buyer: str,
        width: int,
        speed: int,
        window_start: int,
        window_end: int,
        quantity: int,
        allowed_tracks: Optional[list[int]] = None,
        note: str = "",
    ) -> dict[str, Any]:
        if self.s.mode != "pool":
            raise ValueError(f"节目 {self.s.show_id} 不是现场候播模式")
        if order_id in self.s.orders:
            raise ValueError(f"订单 {order_id} 已存在")
        tracks = allowed_tracks or list(range(1, self.s.tracks + 1))
        self._check_tracks(tracks)
        self.s.order_seq += 1
        order = Order(
            order_id=order_id,
            buyer=buyer,
            width=width,
            speed=speed,
            window_start=window_start,
            window_end=window_end,
            quantity=quantity,
            mode="pool",
            allowed_tracks=tracks,
            status=ORDER_QUEUED,
            seq=self.s.order_seq,
            note=note,
        )
        self.s.orders[order_id] = order
        result = {"order_id": order_id, "queued": True, "owed": quantity}
        self.store.record("pool_placed", {"order": order.to_dict(),
                                          "result": result})
        return result

    def progress(self, played_beat: int) -> dict[str, Any]:
        """收到该屏真实播放进度：先结算在途，再给候播单派下一条。

        只有调用本方法（即收到真实进度）后才允许派发新指令；派发只
        选择从下一拍起仍来得及完整展示的单。
        """
        if played_beat < self.s.current_beat:
            raise ValueError(
                f"进度不能回退：当前 {self.s.current_beat}，收到 {played_beat}")
        self.s.current_beat = played_beat
        settled, stopped = self._settle_in_flight(played_beat)
        dispatched: list[dict[str, Any]] = []
        expired: list[dict[str, Any]] = []
        if self.s.mode == "pool":
            dispatched, expired = self._dispatch_pool(played_beat)
        result = {
            "played_beat": played_beat,
            "settled": settled,
            "stopped": stopped,
            "dispatched": dispatched,
            "expired": expired,
            "outbox_pending": self.pending_outbox_ids(),
        }
        self.store.record("progress", {"played_beat": played_beat,
                                       "result": _strip(result)})
        return result

    def _settle_in_flight(self, played_beat: int):
        settled, stopped = [], []
        for instr in list(self.s.instructions.values()):
            # 只有收到开播回条、真正在屏上滚的指令才能按进度结清；
            # 仅 SENT 未开播的指令不视为已展示。
            if instr.status != INSTR_STARTED:
                continue
            traj = instr.trajectory
            end_vis = traj.visible_steps[-1].beat
            if played_beat >= end_vis:
                # 该屏已滚过最后一个可见拍 → 观众看完了整句。
                instr.status = INSTR_DONE
                instr.last_played_beat = played_beat
                order = self.s.orders[instr.order_id]
                if order.mode == "pool":
                    order.quantity -= 1
                    if order.quantity <= 0:
                        order.status = ORDER_SETTLED
                    else:
                        # 还欠完整展示机会，回到候播池等下一次进度续派。
                        order.status = ORDER_QUEUED
                else:
                    order.status = ORDER_SETTLED
                settled.append({
                    "instr_id": instr.instr_id,
                    "order_id": instr.order_id,
                    "buyer": instr.buyer,
                })
            elif instr.status == INSTR_STARTED:
                instr.last_played_beat = played_beat
        return settled, stopped

    def _dispatch_pool(self, played_beat: int):
        dispatched, expired = [], []
        # 先派单：窗口边界上（window_end == played_beat+1）的单这一拍
        # 仍来得及，必须先给它找路径，不能先判过期。
        while True:
            occupied = self.occupied_cells()
            candidate = None
            candidate_traj = None
            for order in sorted(
                (o for o in self.s.orders.values()
                 if o.status == ORDER_QUEUED),
                key=lambda x: x.seq,
            ):
                # 入口拍不得早于下一拍（不能补发已经过去的拍）。
                effective = Order(**order.to_dict())
                effective.window_start = max(order.window_start,
                                             played_beat + 1)
                if effective.window_start > order.window_end:
                    continue
                traj = self.find_path(effective, occupied=occupied)
                if traj is not None:
                    candidate, candidate_traj = order, traj
                    break
            if candidate is None:
                break
            instr = self._lock_trajectory(candidate, candidate_traj)
            instr.status = INSTR_SENT
            candidate.status = ORDER_DISPATCHED
            self._push_outbox(instr)
            dispatched.append({
                "outbox_id": self.s.outbox[-1].outbox_id,
                "instr_id": instr.instr_id,
                "order_id": candidate.order_id,
                "buyer": candidate.buyer,
                "track": candidate_traj.track,
                "start": candidate_traj.start,
            })
        # 派完仍在排队、且下一拍都已晚于窗口的单，标记过期。
        for order in self.s.orders.values():
            if (order.status == ORDER_QUEUED
                    and order.window_end < played_beat + 1):
                order.status = ORDER_EXPIRED
                expired.append({
                    "order_id": order.order_id,
                    "buyer": order.buyer,
                    "owed": order.quantity,
                })
        return dispatched, expired

    # ====================================================================
    # 播出指令的下发、开播回条、中途停播
    # ====================================================================

    def _push_outbox(self, instr: Instruction,
                     outbox_id: Optional[str] = None) -> OutboxItem:
        if outbox_id is None:
            outbox_id = self.store.next_id("O")
        payload = {
            "instr_id": instr.instr_id,
            "screen_id": instr.screen_id,
            "order_id": instr.order_id,
            "buyer": instr.buyer,
            "track": instr.track,
            "start_beat": instr.start,
            "width": instr.width,
            "speed": instr.speed,
            "visible_path": [s.to_dict() for s in instr.trajectory.visible_steps],
        }
        item = OutboxItem(outbox_id, instr.screen_id, instr.instr_id, payload)
        self.s.outbox.append(item)
        return item

    def dispatch_presale(self, order_id: str) -> dict[str, Any]:
        """播控领取预售票的实际播出指令，真正播给请求方（进出件箱）。"""
        order = self._require_order(order_id)
        instr = self._instr_of_order(order_id)
        if instr is None or order.status != ORDER_LOCKED:
            raise ValueError(f"订单 {order_id} 没有可领取的锁定指令")
        if instr.status != INSTR_PLANNED:
            raise ValueError(f"指令 {instr.instr_id} 状态为 {instr.status}，不可重复领取")
        instr.status = INSTR_SENT
        order.status = ORDER_DISPATCHED
        item = self._push_outbox(instr)
        result = {"outbox_id": item.outbox_id, "instr_id": instr.instr_id,
                  "payload": item.payload}
        self.store.record("presale_dispatched",
                          {"order_id": order_id, "result": _strip(result)})
        return result

    def claim_outbox(self) -> list[dict[str, Any]]:
        """请求方取走属于该屏的全部待领指令。"""
        items = [x for x in self.s.outbox
                 if not x.claimed and x.screen_id == self.s.screen_id]
        for x in items:
            x.claimed = True
        result = [x.to_dict() for x in items]
        self.store.record("outbox_claimed", {"items": result})
        return result

    def receipt(self, receipt_id: str, instr_id: str,
                started_beat: int) -> dict[str, Any]:
        """该屏回传开播回条：指令进入在途，轨迹开始物理占用。

        同一回条重传（跨重启）只生效一次：receipt_id 去重。
        """
        if receipt_id in self.s.seen_receipts:
            result = {"receipt_id": receipt_id, "duplicate": True,
                      "instr_id": instr_id}
            self.store.record("receipt_duplicate", result)
            return result
        instr = self.s.instructions.get(instr_id)
        if instr is None:
            raise ValueError(f"未知指令 {instr_id}")
        if instr.status not in (INSTR_SENT,):
            raise ValueError(f"指令 {instr_id} 状态 {instr.status}，不能开播")
        if started_beat != instr.start:
            raise ValueError(
                f"开播拍 {started_beat} 与指令入口拍 {instr.start} 不一致")
        self.s.seen_receipts.append(receipt_id)
        instr.status = INSTR_STARTED
        instr.receipt_id = receipt_id
        order = self.s.orders[instr.order_id]
        order.status = ORDER_PLAYING
        if started_beat > self.s.current_beat:
            self.s.current_beat = started_beat
        result = {"receipt_id": receipt_id, "duplicate": False,
                  "instr_id": instr_id, "started_beat": started_beat}
        self.store.record("receipt_accepted", result)
        return result

    def stop_playback(self, instr_id: str, at_beat: int,
                      reason: str = "manual") -> dict[str, Any]:
        """中途停播：在途指令未滚完即终止，占用立即释放，欠账保留。"""
        instr = self.s.instructions.get(instr_id)
        if instr is None:
            raise ValueError(f"未知指令 {instr_id}")
        if instr.status != INSTR_STARTED:
            raise ValueError(f"指令 {instr_id} 状态 {instr.status}，未在播")
        end_vis = instr.trajectory.visible_steps[-1].beat
        if at_beat >= end_vis:
            raise ValueError("已滚完最后可见拍，应走 progress 结清而非停播")
        instr.status = INSTR_STOPPED
        instr.last_played_beat = at_beat
        order = self.s.orders[instr.order_id]
        if order.mode == "pool":
            # 一次完整展示都没兑现，quantity 不减，订单回到候播池。
            order.status = ORDER_QUEUED
        else:
            # 预售票被停播：合同受损，标记 BROKEN 等待追责/补排。
            order.status = ORDER_BROKEN
            order.note = (order.note + f" 停播于第 {at_beat} 拍（{reason}）").strip()
        self.s.current_beat = max(self.s.current_beat, at_beat)
        result = {"instr_id": instr_id, "order_id": instr.order_id,
                  "buyer": instr.buyer, "stopped_at": at_beat,
                  "owed": order.quantity}
        self.store.record("playback_stopped", result)
        return result

    # ====================================================================
    # 查询：欠账、受影响买家、回放
    # ====================================================================

    def owed_report(self) -> list[dict[str, Any]]:
        """实际还欠谁几个完整展示机会。"""
        rows = []
        for order in sorted(self.s.orders.values(), key=lambda x: x.seq):
            owed = 0
            if order.mode == "pool":
                # DISPATCHED/PLAYING 的一次机会正在兑现，只计尚未派出的欠数。
                if order.status in (ORDER_QUEUED, ORDER_EXPIRED):
                    owed = order.quantity
            else:
                if order.status in (ORDER_BROKEN, ORDER_PENDING):
                    owed = order.quantity
                elif order.status in (ORDER_LOCKED, ORDER_DISPATCHED,
                                      ORDER_PLAYING):
                    owed = 1  # 票还没变成 SETTLED
            if owed > 0:
                rows.append({
                    "order_id": order.order_id,
                    "buyer": order.buyer,
                    "mode": order.mode,
                    "status": order.status,
                    "owed_full_plays": owed,
                    "window": [order.window_start, order.window_end],
                    "allowed_tracks": order.allowed_tracks,
                })
        return rows

    def grid_view(self, beat: int) -> dict[str, Any]:
        """某一拍的屏幕快照，用于本机回放。"""
        rows: dict[int, list[Optional[str]]] = {
            t: [None] * self.s.width for t in range(1, self.s.tracks + 1)
        }
        for occ in self.s.occlusions:
            if occ.from_beat <= beat <= occ.to_beat:
                for c in range(occ.left_col, occ.right_col + 1):
                    rows[occ.track][c - 1] = f"字幕:{occ.occ_id}"
        for instr in self.s.instructions.values():
            if instr.trajectory is None or instr.status == INSTR_PLANNED:
                continue
            for step in instr.trajectory.visible_steps:
                if step.beat == beat:
                    for c in range(step.left_col, step.right_col + 1):
                        rows[step.track][c - 1] = instr.order_id
        return {"beat": beat, "tracks": rows}

    def pending_outbox_ids(self) -> list[str]:
        return [x.outbox_id for x in self.s.outbox if not x.claimed]

    # ====================================================================
    # 事件重放
    # ====================================================================

    def apply_log_events(self, events: list[dict[str, Any]]) -> None:
        for ev in events[1:] if events and events[0]["type"] == "show_created" \
                else events:
            self._apply_one(ev)
        # 纯日志重放没有经过 next_id，按现存实体序号重建计数器，
        # 保证重放态与磁盘态字节级一致。
        self.s.order_seq = max(
            (o.seq for o in self.s.orders.values()), default=0)
        for prefix, make_n in (("I", self._max_serial("I-")),
                               ("O", self._max_serial("O-"))):
            if make_n:
                self.s.counters[prefix] = make_n

    def _max_serial(self, marker: str) -> int:
        ids = ([i.instr_id for i in self.s.instructions.values()]
               if marker == "I-"
               else [x.outbox_id for x in self.s.outbox])
        best = 0
        for an_id in ids:
            if an_id.startswith(marker):
                try:
                    best = max(best, int(an_id[len(marker):]))
                except ValueError:
                    continue
        return best

    def _apply_one(self, ev: dict[str, Any]) -> None:
        t, p = ev["type"], ev["payload"]
        if t == "presale_placed":
            order = Order.from_dict(p["order"])
            self.s.orders[order.order_id] = order
            if p["result"].get("accepted"):
                row = p["result"]
                traj = build_trajectory(
                    order.order_id, order.width, order.speed,
                    self.s.width, row["track"], row["start"])
                self.s.instructions[row["instr_id"]] = Instruction(
                    instr_id=row["instr_id"], order_id=order.order_id,
                    buyer=order.buyer, track=row["track"], start=row["start"],
                    width=order.width, speed=order.speed,
                    screen_id=self.s.screen_id, trajectory=traj)
        elif t == "pool_placed":
            order = Order.from_dict(p["order"])
            self.s.orders[order.order_id] = order
        elif t == "occlusion_added":
            self.s.occlusions.append(Occlusion.from_dict(p["occlusion"]))
            self._rebuild_presale_instructions(p["result"]["affected_orders"])
        elif t == "occlusion_removed":
            occ_id = p["occ_id"]
            self.s.occlusions = [o for o in self.s.occlusions
                                 if o.occ_id != occ_id]
            self._rebuild_presale_instructions(p["result"]["affected_orders"])
        elif t == "order_retried":
            row = p["result"]
            order = self.s.orders[p["order_id"]]
            if row["accepted"]:
                traj = build_trajectory(
                    order.order_id, order.width, order.speed,
                    self.s.width, row["track"], row["start"])
                self.s.instructions[row["instr_id"]] = Instruction(
                    instr_id=row["instr_id"], order_id=order.order_id,
                    buyer=order.buyer, track=row["track"], start=row["start"],
                    width=order.width, speed=order.speed,
                    screen_id=self.s.screen_id, trajectory=traj)
                order.status = ORDER_LOCKED
        elif t == "presale_dispatched":
            instr = self.s.instructions[p["result"]["instr_id"]]
            instr.status = INSTR_SENT
            self.s.orders[instr.order_id].status = ORDER_DISPATCHED
            self._push_outbox(instr, p["result"]["outbox_id"])
        elif t == "outbox_claimed":
            claimed = {x["outbox_id"] for x in p["items"]}
            for x in self.s.outbox:
                if x.outbox_id in claimed:
                    x.claimed = True
        elif t == "receipt_accepted":
            self.s.seen_receipts.append(p["receipt_id"])
            instr = self.s.instructions[p["instr_id"]]
            instr.status = INSTR_STARTED
            instr.receipt_id = p["receipt_id"]
            self.s.orders[instr.order_id].status = ORDER_PLAYING
            self.s.current_beat = max(self.s.current_beat, p["started_beat"])
        elif t == "receipt_duplicate":
            # 去重集合在 state 中持久化，重放无需改动。
            return
        elif t == "playback_stopped":
            instr = self.s.instructions[p["instr_id"]]
            instr.status = INSTR_STOPPED
            instr.last_played_beat = p["stopped_at"]
            order = self.s.orders[instr.order_id]
            if order.mode == "pool":
                order.status = ORDER_QUEUED
            else:
                order.status = ORDER_BROKEN
            self.s.current_beat = max(self.s.current_beat, p["stopped_at"])
        elif t == "progress":
            self._apply_progress_event(p)
        else:
            raise ValueError(f"未知事件 {t}")

    def _apply_progress_event(self, p: dict[str, Any]) -> None:
        played = p["played_beat"]
        self.s.current_beat = played
        # 在途指令即使未结清，也要还原“已滚到第几拍”。
        for instr in self.s.instructions.values():
            if instr.status == INSTR_STARTED:
                instr.last_played_beat = played
        for row in p["result"].get("settled", []):
            instr = self.s.instructions[row["instr_id"]]
            instr.status = INSTR_DONE
            instr.last_played_beat = played
            order = self.s.orders[row["order_id"]]
            if order.mode == "pool":
                order.quantity -= 1
                order.status = (ORDER_SETTLED if order.quantity <= 0
                                else ORDER_QUEUED)
            else:
                order.status = ORDER_SETTLED
        for row in p["result"].get("stopped", []):
            instr = self.s.instructions[row["instr_id"]]
            instr.status = INSTR_STOPPED
        for row in p["result"].get("expired", []):
            self.s.orders[row["order_id"]].status = ORDER_EXPIRED
        for row in p["result"].get("dispatched", []):
            if row["instr_id"] not in self.s.instructions:
                # 候播池指令在本事件首次出现，按事件数据重建完整轨迹。
                order = self.s.orders[row["order_id"]]
                traj = build_trajectory(
                    order.order_id, order.width, order.speed,
                    self.s.width, row["track"], row["start"])
                self.s.instructions[row["instr_id"]] = Instruction(
                    instr_id=row["instr_id"], order_id=order.order_id,
                    buyer=order.buyer, track=row["track"],
                    start=row["start"], width=order.width,
                    speed=order.speed, screen_id=self.s.screen_id,
                    trajectory=traj)
            instr = self.s.instructions[row["instr_id"]]
            instr.status = INSTR_SENT
            order = self.s.orders[row["order_id"]]
            order.status = ORDER_DISPATCHED
            self._push_outbox(instr, row["outbox_id"])

    def _rebuild_presale_instructions(self, affected: list[dict[str, Any]]) -> None:
        """重放字幕事件时按事件里记录的 affected 重建指令集合。"""
        keep = {row.get("instr_id") for row in affected if not row["broken"]}
        # 删掉受影响订单的旧指令，按事件给出的新轨迹重建。
        affected_orders = {row["order_id"] for row in affected}
        for instr_id in [i for i in self.s.instructions
                         if self.s.instructions[i].order_id in affected_orders]:
            del self.s.instructions[instr_id]
        for row in affected:
            if row["broken"]:
                self.s.orders[row["order_id"]].status = ORDER_BROKEN
                continue
            order = self.s.orders[row["order_id"]]
            traj = build_trajectory(
                order.order_id, order.width, order.speed,
                self.s.width, row["track"], row["start"],
            )
            instr = Instruction(
                instr_id=row["instr_id"],
                order_id=order.order_id,
                buyer=order.buyer,
                track=row["track"],
                start=row["start"],
                width=order.width,
                speed=order.speed,
                screen_id=self.s.screen_id,
                status=INSTR_PLANNED,
                trajectory=traj,
            )
            self.s.instructions[row["instr_id"]] = instr
            order.status = ORDER_LOCKED

    # ====================================================================

    def _require_order(self, order_id: str) -> Order:
        order = self.s.orders.get(order_id)
        if order is None:
            raise ValueError(f"未知订单 {order_id}")
        return order

    def _check_tracks(self, tracks: list[int]) -> None:
        for t in tracks:
            if not 1 <= t <= self.s.tracks:
                raise ValueError(f"轨道 {t} 超出 1..{self.s.tracks}")


def _strip(obj: Any) -> Any:
    """出件箱 payload 已随指令持久化，事件里去掉冗余大对象。"""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items()
                if k != "payload"}
    if isinstance(obj, list):
        return [_strip(x) for x in obj]
    return obj
