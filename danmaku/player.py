"""请求方播放器与本机回放。

``Player`` 站在屏幕一侧：从出件箱领取真正下发的指令，按拍推进时回传
开播回条与真实播放进度，并可模拟中途停播。它不持有排期状态，只忠实
回放引擎下发的路径，因此能检验“观众那一屏是否真的把整句话滚完”。
"""

from __future__ import annotations

from typing import Any, Optional

from .engine import Engine
from .model import INSTR_SENT, INSTR_STARTED


class Player:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.screen_id = engine.s.screen_id
        # instr_id -> 已确认在屏上滚到的拍
        self.playing: dict[str, int] = {}
        # 重启后从历史回条恢复序号，避免新回条与旧回条撞号。
        self.receipt_seq = 0
        for instr in engine.s.instructions.values():
            if instr.receipt_id and instr.receipt_id.startswith(
                    f"R-{self.screen_id}-"):
                try:
                    self.receipt_seq = max(
                        self.receipt_seq,
                        int(instr.receipt_id.rsplit("-", 1)[1]))
                except ValueError:
                    continue

    def claim(self) -> list[dict[str, Any]]:
        """领取播控真正发给本屏的指令。"""
        return self.engine.claim_outbox()

    def _next_receipt(self, instr_id: str) -> str:
        self.receipt_seq += 1
        return f"R-{self.screen_id}-{self.receipt_seq:04d}"

    def advance(self, to_beat: int, *, stop: Optional[dict[str, int]] = None) -> dict[str, Any]:
        """把屏幕推进到 to_beat：先对到达入口拍的指令回开播回条，
        再上报真实进度。stop={instr_id: at_beat} 可模拟中途停播。"""
        # 1) 入口拍已到、尚未开播的指令，回传开播回条。
        for instr in list(self.engine.s.instructions.values()):
            if (instr.screen_id == self.screen_id
                    and instr.status == INSTR_SENT
                    and instr.start <= to_beat):
                receipt_id = self._next_receipt(instr.instr_id)
                self.engine.receipt(receipt_id, instr.instr_id, instr.start)
                self.playing[instr.instr_id] = instr.start
        # 2) 中途停播（在进度上报前生效）。
        stopped_info = []
        if stop:
            for instr_id, at_beat in stop.items():
                stopped_info.append(
                    self.engine.stop_playback(instr_id, at_beat,
                                              reason="simulated_stop"))
                self.playing.pop(instr_id, None)
        # 3) 上报该屏真实播放进度，引擎据此结清/续派。
        result = self.engine.progress(to_beat)
        result["stopped"] = stopped_info or result["stopped"]
        return result

    def resend_last_receipt(self) -> dict[str, Any]:
        """跨重启重传同一张开播回条，验证幂等只结一次。"""
        target = next(
            (i for i in self.engine.s.instructions.values()
             if i.status == INSTR_STARTED and i.receipt_id),
            None,
        )
        if target is None:
            raise RuntimeError("没有已开播的指令可供重传回条")
        return self.engine.receipt(target.receipt_id, target.instr_id,
                                   target.start)


def render_frame(engine: Engine, beat: int) -> str:
    """把某一拍的屏幕渲染成文本，用于人眼回放核对。"""
    view = engine.grid_view(beat)
    width = engine.s.width
    lines = [f"第 {beat} 拍（屏幕 {engine.s.screen_id}，{width} 列 x "
             f"{engine.s.tracks} 轨）"]
    for track in range(engine.s.tracks, 0, -1):
        cells = view["tracks"][track]
        body = "|".join(
            (" " if c is None else (c[:6] + "…" if len(str(c)) > 7 else str(c)))
            .center(7) for c in cells
        )
        lines.append(f"轨{track} |{body}|")
    lines.append("     " + "".join(f"{c:^8}" for c in range(1, width + 1)))
    return "\n".join(lines)


def replay(engine: Engine, from_beat: int = 1,
           to_beat: Optional[int] = None) -> str:
    """逐拍渲染整段节目，输出可回放文本。"""
    if to_beat is None:
        beats = [s.beat for i in engine.s.instructions.values()
                 if i.trajectory for s in i.trajectory.steps]
        to_beat = max(beats, default=from_beat)
    return "\n\n".join(render_frame(engine, b)
                       for b in range(from_beat, to_beat + 1))
