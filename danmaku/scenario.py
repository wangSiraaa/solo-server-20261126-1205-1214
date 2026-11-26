"""题目要求的端到端故事，全部在本机用文件状态跑，无需视频流。

故事 A（一轨三播出位，预售）
  A1 基准：宽 1、速 1，窗口要求连续三拍占第 3、2、1 列 → 下单锁定、
     实际照播三拍 → 结清。
  A2 回到下单前，只加中拍字幕遮住第 2 列 → 原约无法完整展示，合同
     被改坏，可查到受影响买家。
  A3 恢复字幕，另一买家的老指令已实际发往该屏并收到开播回条，尚待
     滚完的轨迹占住唯一三拍 → 新单（预售/候播）都不许相撞轨迹。

故事 B（两轨，现场候播）
  B1 一张靠挪轨才能办成的单。
  B2 一次中途停播，下一次请求读到实际欠账；跨重启重传同一回条只结一次。
"""

from __future__ import annotations

import json
import os
import shutil

from .engine import Engine
from .player import Player, replay, render_frame
from .store import Store, replay_dir

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data")


def _fresh(dirname: str) -> str:
    path = os.path.join(BASE, dirname)
    if os.path.exists(path):
        shutil.rmtree(path)
    return path


def _hr(title: str) -> str:
    bar = "=" * 72
    return f"\n{bar}\n{title}\n{bar}"


def story_a() -> str:
    out: list[str] = []

    # ---- A1 基准：完整滚完并结清 ------------------------------------
    d = _fresh("A1_baseline")
    store = Store.create(d, show_id="show-A1", screen_id="screen-1",
                         width=3, tracks=1, mode="presale")
    eng = Engine(store)
    out.append(_hr("A1 基准：1 轨 3 播出位，宽 1 速 1，窗口三拍占 3/2/1 列"))
    # 入口拍 1：第 1 拍最右格在第 3 列，随后 2、1 列。
    res = eng.place_presale_order("O-alice", buyer="买家Alice", width=1,
                                  speed=1, window_start=1, window_end=1)
    out.append("下单锁定: " + json.dumps(res, ensure_ascii=False))
    out.append("占用格: " + json.dumps(res["cells"]))

    eng.dispatch_presale("O-alice")
    player = Player(eng)
    claimed = player.claim()
    out.append(f"请求方实际领到指令 {len(claimed)} 条: "
               + claimed[0]["payload"]["instr_id"])

    # 逐拍回传真实进度。
    for beat in (1, 2, 3):
        r = player.advance(beat)
        out.append(f"进度到第 {beat} 拍 -> 结清 {r['settled']}, "
                   f"续派 {len(r['dispatched'])} 条")
    out.append("订单状态: " + eng.s.orders["O-alice"].status)
    out.append("欠账表: " + json.dumps(eng.owed_report(), ensure_ascii=False))
    out.append(replay(eng, 1, 3))
    assert eng.s.orders["O-alice"].status == "SETTLED"
    assert eng.owed_report() == []

    # ---- A2 中拍字幕遮住第 2 列，合同改坏 ---------------------------
    d2 = _fresh("A2_occlusion")
    store2 = Store.create(d2, show_id="show-A2", screen_id="screen-1",
                          width=3, tracks=1, mode="presale")
    eng2 = Engine(store2)
    out.append(_hr("A2 只加中拍字幕：第 2 拍第 2 列被遮，原约无法交完"))
    occ = eng2.add_occlusion("SUB-MID", from_beat=2, to_beat=2, track=1,
                             left_col=2, right_col=2)
    out.append("字幕已上: " + json.dumps(occ, ensure_ascii=False))
    res2 = eng2.place_presale_order("O-bob", buyer="买家Bob", width=1,
                                    speed=1, window_start=1, window_end=1)
    out.append("下单结果: " + json.dumps(res2, ensure_ascii=False))
    out.append("受影响/欠账买家: "
               + json.dumps(eng2.owed_report(), ensure_ascii=False))
    assert res2["accepted"] is False
    assert eng2.s.orders["O-bob"].status == "BROKEN"
    assert eng2.owed_report()[0]["buyer"] == "买家Bob"

    # 撤掉字幕，引擎自动重排，Bob 的票重新锁定（证明问题确实来自该格）。
    restored = eng2.remove_occlusion("SUB-MID")
    res2b = next(x for x in restored["affected_orders"]
                 if x["order_id"] == "O-bob")
    out.append("撤字幕自动重排: " + json.dumps(res2b, ensure_ascii=False))
    assert res2b["broken"] is False
    assert eng2.s.orders["O-bob"].status == "LOCKED"

    # ---- A3 在途老指令占住唯一三拍，新单不得相撞 --------------------
    d3 = _fresh("A3_inflight")
    store3 = Store.create(d3, show_id="show-A3", screen_id="screen-1",
                          width=3, tracks=1, mode="presale")
    eng3 = Engine(store3)
    out.append(_hr("A3 老指令已发屏并收到开播回条，在途轨迹占住三拍"))
    eng3.place_presale_order("O-carol", buyer="老买家Carol", width=1,
                             speed=1, window_start=1, window_end=1)
    eng3.dispatch_presale("O-carol")
    p3 = Player(eng3)
    p3.claim()
    r = p3.advance(1)  # 第 1 拍：开播回条已回，轨迹在途
    out.append("第 1 拍后老单状态: " + eng3.s.orders["O-carol"].status
               + "，回条: " + str(eng3.s.instructions["I-0001"].receipt_id))

    # 预售新单：唯一三拍被占，卖不出去。
    new_pre = eng3.place_presale_order("O-dave", buyer="新买家Dave", width=1,
                                       speed=1, window_start=1, window_end=1)
    out.append("预售新单: " + json.dumps(new_pre, ensure_ascii=False))
    assert new_pre["accepted"] is False

    # 同屏再开候播池也不许相撞：另建一个 pool 节目复刻同一在途局面。
    d3b = _fresh("A3_pool")
    st3b = Store.create(d3b, show_id="show-A3b", screen_id="screen-1",
                        width=3, tracks=1, mode="pool")
    eng3b = Engine(st3b)
    eng3b.place_pool_order("O-carol2", buyer="老买家Carol", width=1, speed=1,
                           window_start=1, window_end=1, quantity=1)
    p3b = Player(eng3b)
    r0 = eng3b.progress(0)  # 开播前调度拍：候播单被派给屏幕
    assert len(r0["dispatched"]) == 1
    p3b.advance(1)         # 第 1 拍：回开播回条，轨迹在途占住三拍
    # 复刻：新单的合同窗口同样是第 1 拍入口这唯一三拍（窗口 1..1），
    # 与在途轨迹相撞；候播池也不许派出相撞轨迹。
    eng3b.place_pool_order("O-dave2", buyer="新买家Dave", width=1, speed=1,
                           window_start=1, window_end=1, quantity=1)
    r3b = eng3b.progress(1)  # 同一拍再请求：唯一三拍已被在途轨迹占满
    out.append("候播池同拍再请求，续派条数: " + str(len(r3b["dispatched"]))
               + "（期望 0）")
    assert len(r3b["dispatched"]) == 0
    out.append("欠账表: " + json.dumps(eng3b.owed_report(), ensure_ascii=False))
    assert eng3b.s.orders["O-dave2"].status == "EXPIRED"
    assert eng3b.s.orders["O-carol2"].status == "PLAYING"
    return "\n".join(out)


def story_b() -> str:
    out: list[str] = []
    d = _fresh("B_pool_twotrack")
    store = Store.create(d, show_id="show-B", screen_id="screen-9",
                         width=3, tracks=2, mode="pool")
    eng = Engine(store)
    out.append(_hr("B 两轨现场候播池：挪轨办成 + 中途停播 + 重启幂等"))

    # 两张宽 2、速 1 的单。窗口都从第 1 拍开始；轨 1 的路径与先来单
    # 相撞，后到单必须挪到轨 2 才能办成。
    eng.place_pool_order("O-e1", buyer="买家Eve", width=2, speed=1,
                         window_start=1, window_end=2, quantity=1)
    eng.place_pool_order("O-f1", buyer="买家Frank", width=2, speed=1,
                         window_start=1, window_end=2, quantity=1,
                         allowed_tracks=[1, 2])
    player = Player(eng)
    r = eng.progress(0)   # 开播前调度拍：池内排队的单在此拍派给屏幕
    out.append("调度拍派发: " + json.dumps(r["dispatched"],
                                           ensure_ascii=False))
    tracks = {x["order_id"]: x["track"] for x in r["dispatched"]}
    assert tracks["O-e1"] == 1 and tracks["O-f1"] == 2, tracks
    out.append("Frank 的单靠挪到轨 2 办成（轨 1 已被 Eve 占住）")
    player.advance(1)    # 第 1 拍：两张单分别在两轨回开播回条

    # 一张买 2 次完整展示的单，窗口允许稍晚开头；第一次播出中途停播。
    eng.place_pool_order("O-g2", buyer="买家Gina", width=1, speed=1,
                         window_start=2, window_end=9, quantity=2)
    r = eng.progress(2)
    g_instr = next(x["instr_id"] for x in r["dispatched"]
                   if x["order_id"] == "O-g2")
    out.append("Gina 第 1 条指令: " + g_instr)
    # 第 3 拍收到开播回条并滚动，第 4 拍中途停播（还没滚完 3 个可见拍）。
    player.advance(3)
    stop = eng.stop_playback(g_instr, at_beat=4, reason="signal_loss")
    out.append("中途停播: " + json.dumps(stop, ensure_ascii=False))
    out.append("停播后欠账: " + json.dumps(eng.owed_report(),
                                           ensure_ascii=False))
    assert eng.s.orders["O-g2"].quantity == 2  # 一次都没兑现，欠 2
    assert eng.s.orders["O-g2"].status == "QUEUED"

    # ---- 模拟重启：从磁盘重新打开，重传同一张开播回条 --------------
    reopened = Store.open(d)
    eng2 = Engine(reopened)
    out.append(_hr("B-重启 从 state.json 恢复，重传 Gina 的开播回条"))
    old_receipt = reopened.state.instructions[g_instr].receipt_id
    dup = eng2.receipt(old_receipt, g_instr,
                       reopened.state.instructions[g_instr].start)
    out.append("重传回条结果: " + json.dumps(dup, ensure_ascii=False))
    assert dup["duplicate"] is True

    # 下一次请求读到真实欠账，并让剩余位置继续兑现 Gina 的两次机会。
    out.append("重启后欠账表: " + json.dumps(eng2.owed_report(),
                                             ensure_ascii=False))
    player2 = Player(eng2)
    r = player2.advance(5)
    out.append("第 5 拍续派: " + json.dumps(r["dispatched"],
                                            ensure_ascii=False))
    g_second = next((x for x in r["dispatched"]
                     if x["order_id"] == "O-g2"), None)
    assert g_second is not None and g_second["start"] == 6
    r = player2.advance(8)  # 第一次补播 6/7/8 三拍滚完，腾出轨道
    assert eng2.s.orders["O-g2"].quantity == 1
    out.append("第一次完整展示兑现后，剩余欠账: "
               + json.dumps(eng2.owed_report(), ensure_ascii=False))
    r = player2.advance(11)  # 同一次请求续派出第二次机会并在 11 拍滚完
    out.append("第二次续派: " + json.dumps(
        [x for x in r["dispatched"] if x["order_id"] == "O-g2"],
        ensure_ascii=False))
    out.append("最终 Gina 订单状态: " + eng2.s.orders["O-g2"].status
               + "，欠账: " + json.dumps(eng2.owed_report(),
                                         ensure_ascii=False))
    assert eng2.s.orders["O-g2"].status == "SETTLED"

    # ---- 事件日志重放与磁盘态逐格比对 -------------------------------
    replayed, events, disk = replay_dir(d)
    same = replayed.to_dict() == disk.to_dict()
    out.append(_hr("B-回放 事件日志重放与磁盘 state 一致性"))
    out.append(f"事件数 {len(events)}，重放态 == 磁盘态: {same}")
    assert same, "重放状态与磁盘状态不一致"
    out.append(replay(eng2, 1, 8))
    return "\n".join(out)


def main() -> None:
    print(story_a())
    print(story_b())
    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
