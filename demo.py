#!/usr/bin/env python3
"""End-to-end walkthrough of every behaviour requested in the brief.

Run:  python3 demo.py

It prints, step by step, the numbers the brief asks for:
  3 points solo, 2 points after the crossing hint, the 云水 rejection, the
  zero-山 score-table revision, the race first-arrival rule, shared-sealed
  tolerance, judge re-adjudication, and restart + replay-with-original-number.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager

from xword import Arena, scenarios


def show(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def totals(a, sid, rev=None, viewer=None):
    return a.scoreboard(sid, rev, viewer=viewer)["totals"]


def main():
    store = tempfile.mkdtemp(prefix="xword-demo-")

    # 1. solo, no hints -> 3 ------------------------------------------------
    show("1) 独立解答：两词都自己答出 => 3 分（三板各一分）")
    a, sid = new_race(store, ["A"])
    a.handin(sid, "A", "shan_shui", "山水", "s1")
    a.handin(sid, "A", "shui_che", "水车", "s2")
    print("totals:", totals(a, sid))
    print("cells :", json.dumps(a.scoreboard(sid)["teams"]["A"]["cells"],
                               ensure_ascii=False))

    # 2. crossing hint first, then both words -> 2 -------------------------
    show("2) 先求交叉处提示(水)，再连交两词 => 最多 2 分")
    a, sid = new_race(store, ["A"])
    hint = a.request_hint(sid, "A", "shan_shui", [1, 1], "h1")["hint"]
    print("hint teaches:", hint["teaches"], "letter:", hint["letter"])
    a.handin(sid, "A", "shan_shui", "山水", "s1")
    a.handin(sid, "A", "shui_che", "水车", "s2")
    print("totals:", totals(a, sid))
    print("水 cell:", a.scoreboard(sid)["teams"]["A"]["cells"]["1,1"])

    # 3. same cells/ops but horizontal handin is rejected 云水 -------------
    show("3) 同一盘、同余下操作，仅横词提交改为不被接受的『云水』=> 横端点失分")
    a, sid = new_race(store, ["A"])
    a.request_hint(sid, "A", "shan_shui", [1, 1], "h1")
    a.handin(sid, "A", "shan_shui", "云水", "s1")   # rejected
    a.handin(sid, "A", "shui_che", "水车", "s2")
    print("totals:", totals(a, sid), "(2 -> 1, 山 never earned)")
    print("cells :", json.dumps(a.scoreboard(sid)["teams"]["A"]["cells"],
                               ensure_ascii=False))

    # 4. restore correct handin, then revise score table: drop 山 to 0 -----
    show("4) 恢复正确提交；无提示基准下，单改格分表把『山』降为 0，总分下降")
    a, sid = new_race(store, ["A"])
    a.handin(sid, "A", "shan_shui", "山水", "s1")
    a.handin(sid, "A", "shui_che", "水车", "s2")
    a.make_revision(sid, "base")
    print("base totals:", totals(a, sid, "base"))
    res = a.revise_score_table(sid, {"1,0": 0}, note="把山的奖励降为零",
                               new_revision="zero-shan")
    print("zero-shan totals:", totals(a, sid, "zero-shan"))
    print("judge diff  :", json.dumps(
        a.diff(sid, "base", "zero-shan")["teams"]["A"], ensure_ascii=False))

    # 5. race: two clients contend for the same word -----------------------
    show("5) 抢答场：两队争同一词，最早以正确整词合法封交者得格")
    a, sid = new_race(store, ["A", "B"])
    print("A first:", a.handin(sid, "A", "shui_che", "水车", "a")["outcome"])
    print("B later:", a.handin(sid, "B", "shui_che", "水车", "b")["outcome"])
    b = a.scoreboard(sid)
    print("totals:", b["totals"], "owners:", b["cell_owner"])
    print("B cells:", json.dumps(b["teams"]["B"]["cells"], ensure_ascii=False))

    # 6. lost response -> replay with original number, no new points -------
    show("6) 回包丢失：重启后重放一份带原号的封交，不得添出新分")
    a, sid = new_race(store, ["A", "B"])
    a.handin(sid, "A", "shui_che", "水车", "A-che")
    ev = [e for e in a.log(sid) if e.get("action_id") == "A-che"][0]
    before = totals(a, sid)
    info = a.restart(sid)
    print("after restart:", info["totals"], "(log len", info["log_len"], ")")
    rep = a.replay_handin(sid, ev)
    print("replay added points:", rep["added_points"], "delta:", rep["delta"])
    print("before:", before, "after:", totals(a, sid), "-> conserved")

    # 6b. genuinely-earlier submission replayed after a later one ----------
    show("6b) 公平顺序可重放：B 实为更早到达(原号1)，A 先被记录；重放 B 后 B 得格")
    a, sid = new_race(store, ["A", "B"])
    a.handin(sid, "A", "shui_che", "水车", "A-che")
    print("after A only:", a.scoreboard(sid)["cell_owner"])
    earlier = {"type": "handin", "team": "B", "entry": "shui_che",
               "word": "水车", "action_id": "B-che", "seq": 1}
    rep = a.replay_handin(sid, earlier)
    print("after replaying B(#1):", a.scoreboard(sid)["cell_owner"],
          totals(a, sid))

    # 7. judge re-adjudicates an original handin ---------------------------
    show("7) 裁判改掉被误录的可接受拼法：重审原封交，保留原始动作")
    a, sid = new_race(store, ["A"])
    a.request_hint(sid, "A", "shan_shui", [1, 1], "h")
    a.handin(sid, "A", "shan_shui", "云水", "orig")
    a.handin(sid, "A", "shui_che", "水车", "orig2")
    print("before judge:", totals(a, sid))
    a.judge_revise(sid, "shan_shui", ["云水"], new_revision="fix1")
    print("after judge :", totals(a, sid))
    print("原始 云水 动作仍在日志:", any(
        e.get("word") == "云水" for e in a.log(sid)))

    # 8. sealed: concealment, then shared tie tolerance --------------------
    show("8) 密卷场：截止前互不展示；两队各自密封、各得同分；轮末按同分约定排名")
    a, sid = new_sealed(store, ["A", "B"])
    a.seal(sid, "A", "shan_shui", "山水", "a1")
    a.seal(sid, "A", "shui_che", "水车", "a2")
    a.seal(sid, "B", "shan_shui", "山水", "b1")
    a.seal(sid, "B", "shui_che", "水车", "b2")
    v = a.scoreboard(sid, viewer="A")
    print("A 视角(封卷中): totals", v["totals"], "concealed=", v["concealed"])
    a.reveal(sid)
    b = a.scoreboard(sid)
    print("轮末 totals:", b["totals"])
    print("ranking:", [(r["team"], r["score"], r["rank"]) for r in b["ranking"]])

    print("\nstore kept at", store)


def new_race(store, teams):
    a = Arena(store)
    sid = a.create_session(scenarios.shan_shui_board("race"), teams)["session_id"]
    return a, sid


def new_sealed(store, teams):
    a = Arena(store)
    sid = a.create_session(scenarios.shan_shui_board("sealed"), teams)["session_id"]
    return a, sid


if __name__ == "__main__":
    main()
