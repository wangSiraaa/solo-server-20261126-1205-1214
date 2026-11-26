#!/usr/bin/env python3
"""Micro demo: sequential sessions, a merge request, a theme change, receipt re-issue.

Everything goes through the recorded HTTP surface (``Recorder``), so the whole
run is reproducible from ``demo/calls.jsonl`` by ``Replayer``.  The fake live
renderer turns each started session into a JSON "frame" a viewer can replay --
no pixels, just shape + colour-block names.

Story
-----
Season 1 (distinguishable theme):

* ``阿青`` / ``阿白`` are the two round badges (deep / light edge).  Ordinary
  and large modes both separate them, so they can share a picture.
* ``E1a`` / ``E1b`` start *sequentially* (each alone in its own room).
* The host then asks to **merge** the two rooms -- feasible in season 1.
* ``阿青`` leaves; the freed deep badge is lent to ``阿紫``, and a new
  appearance receipt is re-issued.  The earlier speech number still points at
  ``阿青``.

Season 2 (theme tweaked: large-subtitle edge alias collapsed):

* The same round pair can no longer share one picture -- starting ``E3`` with
  both is refused.  The service does *not* stop everyone.
* Live-borrow guests with a shaped badge available are still separable, showing
  we use every distinguishable pattern rather than halting the stream.

Season 3 (theme restored, third guest lawfully holds the only deep badge):

* ``第三人`` is on-stage using ``round_dark``; the pair's merge has only the
  light badge left, so it is refused.  (The third guest carrying a shaped spare
  would make it feasible again -- the solver uses it if allowed.)
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from badges.model import collapsed_theme, default_theme
from badges.server import Recorder, Replayer
from badges.live import render_screen
from badges.service import Service

HERE = os.path.dirname(os.path.abspath(__file__))
CALL_LOG = os.path.join(HERE, "calls.jsonl")


def show(title: str, obj) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    rec = Recorder(path=CALL_LOG)

    # ---------------- Season 1: distinguishable theme ----------------
    rec.post("/seasons", {"theme_kind": "default", "season_id": "S1"})
    for guest, style in (("阿青", "round_dark"), ("阿白", "round_light")):
        rec.post("/seasons/S1/sign", {"guest": guest, "style": style})

    # 先后分场：每位来宾先各自单独开播。
    e1 = rec.post(
        "/seasons/S1/sessions/start",
        {"session_id": "E1a", "guests": ["阿青"], "speech_numbers": {"阿青": 1}},
    )
    e2 = rec.post(
        "/seasons/S1/sessions/start",
        {"session_id": "E1b", "guests": ["阿白"], "speech_numbers": {"阿白": 2}},
    )
    show("S1 分场开播 E1a（阿青单独）", e1)
    show("S1 分场开播 E1b（阿白单独）", e2)

    t1 = default_theme()
    show("假直播画面 E1a", render_screen(t1, ["阿青"], e1["styles"], [{"guest": "阿青", "speech_no": 1}]))
    show("假直播画面 E1b", render_screen(t1, ["阿白"], e2["styles"], [{"guest": "阿白", "speech_no": 2}]))

    # 再请求并屏。
    merged = rec.post(
        "/seasons/S1/live/merge",
        {"room_present": {"E1a": {"阿青": "round_dark"}, "E1b": {"阿白": "round_light"}},
         "rooms": ["E1a", "E1b"]},
    )
    show("S1 请求并屏（可辨，成功）", merged)
    show("假直播画面 并屏", render_screen(t1, ["阿青", "阿白"], merged["present"]))

    # 阿青退场 -> 深边牌空出 -> 借给阿紫 -> 重发登场回单。
    rec.post("/seasons/S1/live/leave", {"guest": "阿青"})
    rec.post("/seasons/S1/live/enter", {"guest": "阿紫", "style": "round_dark", "room": "E1c"})
    new_receipt = rec.post(
        "/seasons/S1/receipts",
        {"guest": "阿紫", "session_id": "E1c", "speech_numbers": [3]},
    )
    rec.post(
        "/seasons/S1/speeches",
        {"receipt_id": new_receipt["receipt_id"], "session_id": "E1c", "speech_no": 3},
    )
    show("S1 退场后重发登场回单给阿紫（借出深边牌）", new_receipt)
    show("S1 发言号 1 仍指向阿青（已公开截图不变）", rec.get("/seasons/S1/speeches/1"))
    show("S1 发言号 3 指向阿紫", rec.get("/seasons/S1/speeches/3"))

    # ---------------- Season 2: theme change = new season ----------------
    rec.post("/seasons", {"theme_kind": "collapsed", "season_id": "S2"})
    for guest, style in (("阿青", "round_dark"), ("阿白", "round_light")):
        rec.post("/seasons/S2/sign", {"guest": guest, "style": style})
    refused = rec.post(
        "/seasons/S2/sessions/start",
        {"session_id": "E3", "guests": ["阿青", "阿白"], "speech_numbers": {"阿青": 1, "阿白": 2}},
    )
    show("S2 大字边色塌缩：拒绝圆边对同场（不搞全停）", refused)

    # 现场借牌：一对新来宾、有方边牌可借时，仍能把两人分开，不必停播。
    borrow = rec.post(
        "/seasons/S2/live/assign",
        {"guests": ["阿丙", "阿丁"],
         "pools": {"阿丙": ["round_dark"], "阿丁": ["round_light", "square_dark"]}},
    )
    show("S2 现场借方边牌后一组新来宾仍可同场", borrow)
    show("假直播画面 S2 借牌同场",
         render_screen(collapsed_theme(), ["阿丙", "阿丁"], borrow["present"]))

    # ---------------- Season 3: theme restored, deep badge pinned ----------------
    rec.post("/seasons", {"theme_kind": "default", "season_id": "S3"})
    # 第三人合法预先拿到并正在使用唯一的深边牌。
    rec.post("/seasons/S3/sign", {"guest": "第三人", "style": "round_dark"})
    rec.post("/seasons/S3/live/enter", {"guest": "第三人", "style": "round_dark", "room": "甲"})
    # 前两人各自独立开播；并屏时只剩浅边牌可用（深边被第三人占着）。
    only_light = rec.post(
        "/seasons/S3/live/merge",
        {"room_present": {"甲": {"第三人": "round_dark"},
                          "乙": {"阿青": "round_light"},
                          "丙": {"阿白": "round_light"}},
         "allowance": {"阿青": ["round_light"], "阿白": ["round_light"]},
         "rooms": ["甲", "乙", "丙"]},
    )
    show("S3 深边牌在第三人手上：前两人只剩浅边牌，并屏被拒（不搞全停）", only_light)

    # ---------------- Replay the recorded calls ----------------
    rep = Replayer(CALL_LOG)
    replay = rep.run(Service())
    show("回放结果（应与现场一致）",
         {"ok": replay["ok"], "entries": replay["entries"], "mismatches": replay["mismatches"][:1]})

    print(f"\n调用日志已写入 {CALL_LOG}")
    return 0 if replay["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
