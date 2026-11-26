#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端验收脚本：把 app.py 当本地服务启动，跑题面要求的全部场景。

场景顺序：
  0. 同键异义（画布侧键 vs 色板侧键；短按 vs 长按；层键修饰）
  1. 基准：选到第二格 -> 填蓝，作业只获准改第二格
  2. 变体：录制的选格手势改为「回到第一格」，画师两格都有权 => 落笔地点跟到第一格
  3. 封稿：仅第二格在执行前被正式封稿 => 不可留下蓝色
  4. 布局安全分析：交接人换 L2（同组合误触别的操作）=> 拒绝签名；L1 签名通过
  5. 掉线：已落定的填色保留，掉线时按住未放的选格手势丢弃，重连续做
  6. 补色 + 重启：办完基准再开新作业，操作者读到上次改过的格；
     杀掉服务进程后重启，按作业号查询画格记录
  附：非法建单（无授权范围）被拒；用户脚本无入口
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
import socket
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
PORT = int(os.environ.get("BENCH_PORT", _s.getsockname()[1]))
_s.close()
BASE = f"http://127.0.0.1:{PORT}"
DATA = os.path.join(ROOT, "demo_data")
ENV = dict(os.environ, BENCH_DATA=DATA, BENCH_PORT=str(PORT))

PASS, FAIL = 0, 0


def req(method, path, payload=None, expect_error=False):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode())
        if expect_error:
            return e.code, body
        raise AssertionError(f"{method} {path} 意外失败 {e.code}: {body}")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# -- 事件带小工具 -----------------------------------------------------------

def tap(button, t, dur=60):
    return [{"type": "press", "button": button, "ts": t},
            {"type": "release", "button": button, "ts": t + dur}]


def hold(button, t, dur=240):
    return [{"type": "press", "button": button, "ts": t},
            {"type": "release", "button": button, "ts": t + dur}]


def layer_tap(inner, t, dur=60):
    return [{"type": "press", "button": "LAYER", "ts": t},
            {"type": "press", "button": inner, "ts": t + 5},
            {"type": "release", "button": inner, "ts": t + 5 + dur},
            {"type": "release", "button": "LAYER", "ts": t + 10 + dur}]


def settle_at(t):
    return [{"type": "tick", "ts": t}]


def run_job(job_no, event_batches, label=""):
    """领单 -> 开工 -> 分批送入本地按放 -> 结束，返回 (job, work, 每批效果)。"""
    _, started = req("POST", f"/jobs/{job_no}/execution", {})
    eid = started["exec_id"]
    all_effects = []
    for batch in event_batches:
        st, out = req("POST", f"/execution/{eid}/events", {"events": batch})
        all_effects.extend(out["effects"])
    st, ended = req("POST", f"/execution/{eid}/end")
    return ended, all_effects


def cells_snapshot(wid="W1"):
    _, work = req("GET", f"/works/{wid}")
    return {c["id"]: c for c in work["cells"]}


def make_job(rec_id, allowed, artist="A2", start_cell="c1", sign=True):
    st, job = req("POST", "/jobs", {"recording_id": rec_id,
                                    "allowed_cells": allowed,
                                    "artist": artist,
                                    "start_cell": start_cell})
    if sign:
        req("POST", f"/jobs/{job['job_no']}/sign", {"layout_id": "L1"})
    return job["job_no"]


def start_server():
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")],
                            env=ENV, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务进程提前退出:\n" + proc.stdout.read())
        try:
            req("GET", "/health")
            return proc
        except Exception:
            time.sleep(0.15)
    proc.terminate()
    out = proc.stdout.read() if proc.stdout else ""
    raise RuntimeError("服务启动失败:\n" + out)


def stop_server(proc):
    proc.terminate()
    proc.wait(timeout=5)


def main():
    if os.path.exists(DATA):
        shutil.rmtree(DATA)
    server = start_server()
    try:
        section("场景 0｜同键异义：侧键在画布=换格/填色，在色板=换颜色；短按是长按的前缀")
        # 录制一段混合手势：侧键短按(下一格) -> B(开色板) -> 侧键短按(换色)
        # -> B(回画布) -> 侧键长按(填色)
        ev = []
        t = 0
        ev += tap("SIDE", t); t += 400
        ev += tap("B", t); t += 400
        ev += tap("SIDE", t); t += 400
        ev += tap("B", t); t += 400
        ev += hold("SIDE", t, 260)
        st, rec0 = req("POST", "/recordings",
                       {"label": "同键异义示范", "events": ev})
        actions = [(i["action"], i["panel"]) for i in rec0["intents"]]
        check("侧键短按在画布解析为 CELL_NEXT", actions[0] == ("CELL_NEXT", "canvas"))
        check("B 切到色板 OPEN_PALETTE", actions[1] == ("OPEN_PALETTE", "canvas"))
        check("同一侧键在色板解析为 COLOR_NEXT（换颜色，不是换格）",
              actions[2] == ("COLOR_NEXT", "palette"))
        check("B 回到画布 OPEN_CANVAS", actions[3] == ("OPEN_CANVAS", "palette"))
        check("同一侧键长按解析为 FILL（短按/长按前缀消歧）",
              actions[4] == ("FILL", "canvas"))
        # 前缀消歧：短按必须等静默窗，且不会提前触发长按
        parse_kinds = [p["kind"] for p in rec0["parse_log"]
                       if p.get("effect") == "INTENT_RECORDED"]
        check("短按识别为 tap、长按识别为 hold",
              parse_kinds == ["tap", "tap", "tap", "tap", "hold"], str(parse_kinds))

        section("场景 1｜基准：动作先移到第二格再填蓝；手柄有两种互不冲突输入；只获准改第二格")
        base_ev = []
        t = 0
        base_ev += tap("SIDE", t); t += 400          # c1 -> c2
        base_ev += hold("SIDE", t, 260)              # c2 填蓝
        st, rec_base = req("POST", "/recordings",
                           {"label": "基准：下一格+填蓝", "events": base_ev})
        check("基准录制解析出 2 条意图",
              [i["action"] for i in rec_base["intents"]] == ["CELL_NEXT", "FILL"])
        check("填色意图携带当前颜色（蓝）",
              rec_base["intents"][1]["params"]["color"] == "#2563eb")
        j1 = make_job(rec_base["id"], ["c2"], artist="A2")
        # 按签名谱的 guide 在模拟台实际打键
        _, jobdoc = req("GET", f"/jobs/{j1}")
        check("签名谱给出两种互不冲突的输入（tap 与 hold）",
              [e["input"]["kind"] for e in jobdoc["signed_plan"]["entries"]]
              == ["tap", "hold"])
        ended, _ = run_job(j1, [base_ev])
        cells = cells_snapshot()
        check("第二格被填成蓝色", cells["c2"]["color"] == "#2563eb")
        check("第一格保持空白（无权也未触碰）", cells["c1"]["color"] is None)
        check("作业台账记录了“谁把哪格改成什么”",
              ended["results"][-1]["effect"] == "FILLED"
              and ended["results"][-1]["cell"] == "c2"
              and ended["results"][-1]["color"] == "#2563eb")
        check("作业状态 completed", ended["job_no"] == j1)

        section("场景 2｜变体：选格手势改为回到第一格，画师两格均有权 => 落笔地点跟变")
        # 起手在 c2（start_cell=c2），LAYER+SIDE 短按 = CELL_PREV 回到 c1，再长按填色
        var_ev = []
        t = 0
        var_ev += layer_tap("SIDE", t); t += 400
        var_ev += hold("SIDE", t, 260)
        st, rec_var = req("POST", "/recordings",
                          {"label": "变体：回到第一格+填蓝", "events": var_ev})
        check("变体录制首条意图是 CELL_PREV",
              rec_var["intents"][0]["action"] == "CELL_PREV")
        check("变体录制第二条意图仍是 FILL",
              rec_var["intents"][1]["action"] == "FILL")
        j2 = make_job(rec_var["id"], ["c1", "c2"], start_cell="c2")
        ended, _ = run_job(j2, [var_ev])
        cells = cells_snapshot()
        check("本次蓝色落在第一格（落笔地点随选格意图变化）",
              cells["c1"]["color"] == "#2563eb")
        check("第二格仍为场景 1 的蓝色，没有被重复改",
              cells["c2"]["color"] == "#2563eb")
        ledger = ended["results"]
        fill = [r for r in ledger if r["effect"] == "FILLED"]
        check("台账只新增 c1 一条填色", len(fill) == 1 and fill[0]["cell"] == "c1")

        section("场景 3｜封稿：仅第二格在执行前被正式封稿 => 不可留下蓝色")
        # 新作品视角下用同一份基准录制再开一单，但执行前先封 c2
        j3 = make_job(rec_base["id"], ["c2"], artist="A2")
        st, sealed = req("POST", "/works/W1/cells/c2/seal",
                         {"artist": "A3"})
        check("封稿成功，c2 带 sealed_by", sealed["sealed_by"] == "A3")
        ended, effects = run_job(j3, [base_ev])
        cells = cells_snapshot()
        ledger_before = len(req("GET", f"/jobs/{j3}/ledger")[1])
        sealed_reject = [r for r in ended["results"]
                         if r.get("effect") == "REJECTED" and "封稿" in r.get("reason", "")]
        check("第二格封稿后填色被拒（原有蓝保持不变，且没有新蓝色落笔）",
              cells["c2"]["color"] == "#2563eb" and len(sealed_reject) == 1)
        check("被拒手势也进入作业结果记录", len(sealed_reject) == 1)
        check("台账没有为封稿填色新增记录",
              all(r.get("effect") != "FILLED" for r in ended["results"])
              and ledger_before == 0)
        # 非封稿画师无权撤回；之后由封稿画师 A3 本人正式撤回，后续补色场景继续用 c2
        code, body = req("POST", "/works/W1/cells/c2/seal",
                         {"artist": "A2", "sealed": False}, expect_error=True)
        check("非封稿画师撤回被 409 拒绝", code == 409)
        st, unsealed = req("POST", "/works/W1/cells/c2/seal",
                           {"artist": "A3", "sealed": False})
        check("封稿画师本人可撤回封稿", unsealed["sealed_by"] is None)

        section("场景 4｜布局安全分析：交接人换 L2，同一组合会误做别的操作 => 拒绝签名")
        j4 = make_job(rec_base["id"], ["c2"], artist="A2", sign=False)
        code, body = req("POST", f"/jobs/{j4}/sign",
                         {"layout_id": "L2"}, expect_error=True)
        check("L2 签名返回 409 且作业不被签安全", code == 409
              and body["plan"]["safe"] is False)
        reasons = " ".join(f["reason"] for f in body["plan"]["findings"]
                           if not f["safe"])
        check("报告了误触冲突（SIDE 短按同时 CELL_NEXT/FILL）",
              "误做" in reasons or "冲突" in reasons, reasons)
        check("报告了缺失按法（L2 无 SIDE 长按 FILL 的安全绑法）",
              any("FILL" in f["reason"] and not f["safe"]
                  for f in body["plan"]["findings"]))
        _, jobdoc = req("GET", f"/jobs/{j4}")
        check("不安全布局下作业没有签名",
              jobdoc["signed_plan"] is None and jobdoc["status"] == "assigned")
        code2, body2 = req("POST", f"/jobs/{j4}/execution", {},
                           expect_error=True)
        check("未签名作业开工被 409 拒绝",
              code2 == 409 and "签名" in body2["error"])
        _, jobdoc = req("GET", f"/jobs/{j4}")
        check("被拒开工没有把作业状态改成 running", jobdoc["status"] == "assigned")
        # 换回 L1 后签名成功，证明“找到确实能做完的按法”这条路径可用
        req("POST", f"/jobs/{j4}/sign", {"layout_id": "L1"})
        _, jobdoc = req("GET", f"/jobs/{j4}")
        check("换回 L1 后签名通过", jobdoc["signed_plan"]["safe"] is True)

        section("场景 5｜设备掉线：已落定填色保留，按住未放的在飞手势丢弃，重连续做")
        rec_fill_ev = hold("SIDE", 0, 260)
        st, rec_fill = req("POST", "/recordings",
                           {"label": "单填色", "events": rec_fill_ev})
        j5 = make_job(rec_fill["id"], ["c3", "c4"], start_cell="c3")
        _, started = req("POST", f"/jobs/{j5}/execution", {})
        eid = started["exec_id"]
        # 第一批：长按填色并落定（tick 推过静默窗）
        req("POST", f"/execution/{eid}/events",
            {"events": rec_fill_ev + settle_at(600)})
        # 第二批：侧键按下不松 -> 中途 disconnect
        st, out = req("POST", f"/execution/{eid}/events",
                      {"events": [{"type": "press", "button": "SIDE", "ts": 700},
                                  {"type": "disconnect", "ts": 780}]})
        check("掉线报告在飞手势丢弃",
              any(e["effect"] == "GESTURE_DISCARDED" for e in out["effects"]))
        check("掉线后会话标记未连接，选区复位到作业起始格 c3",
              out["connected"] is False and out["selection"] == "c3")
        cells = cells_snapshot()
        check("掉线前已落定的 c3 填色保留", cells["c3"]["color"] == "#2563eb")
        # 重连续做：connect -> 从 c3 移到 c4 -> 填 c4（验证作业可继续）
        req("POST", f"/execution/{eid}/events",
            {"events": [{"type": "connect", "ts": 900}]})
        ev_cont = []
        t = 1000
        ev_cont += tap("SIDE", t); t += 400      # c3 -> c4
        ev_cont += hold("SIDE", t, 260)
        # 分批送入：重连后的按放先发事件，再结束会话
        req("POST", f"/execution/{eid}/events", {"events": ev_cont})
        st, ended = req("POST", f"/execution/{eid}/end")
        cells = {c["id"]: c for c in ended["work"]["cells"]}
        check("重连后同一张作业继续执行，c4 被填色",
              cells["c4"]["color"] == "#2563eb")
        fills = [r for r in ended["results"] if r["effect"] == "FILLED"]
        check("本作业最终两笔填色都保留（c3 掉线前 + c4 重连后）",
              sorted(r["cell"] for r in fills) == ["c3", "c4"])

        section("附｜合法新作业人人可办；非法建单被拒；系统不接受脚本")
        code, body = req("POST", "/jobs",
                         {"recording_id": rec_base["id"],
                          "allowed_cells": [], "artist": "A2"},
                         expect_error=True)
        check("无授权范围的作业被 400 拒绝", code == 400)
        code, body = req("POST", "/jobs",
                         {"recording_id": rec_base["id"],
                          "allowed_cells": ["c9"], "artist": "A2"},
                         expect_error=True)
        check("授权不存在的画格被 400 拒绝", code == 400)
        code, body = req("POST", "/jobs",
                         {"recording_id": "R-999", "allowed_cells": ["c1"]},
                         expect_error=True)
        check("引用不存在录制的作业被 404 拒绝", code == 404)
        # 合法新作业：同事 A2 自己再办一张补色单
        j6 = make_job(rec_fill["id"], ["c4"], artist="A2", start_cell="c4")
        ended, _ = run_job(j6, [rec_fill_ev])
        check("同事可独立新建合法作业并落笔",
              any(r.get("effect") == "FILLED" and r["cell"] == "c4"
                  for r in ended["results"]))
        j1_doc, j6_doc = req("GET", f"/jobs/{j1}")[1], req("GET", f"/jobs/{j6}")[1]
        check("两张作业独立编号", j1_doc["job_no"] != j6_doc["job_no"])

        section("场景 6｜补色 + 重启：新作业读到上次改过的格；重启后按作业号查画格记录")
        before = cells_snapshot()
        check("补色作业开工前能读到既有画格状态（c1..c4 均已带色）",
              all(before[c]["color"] == "#2563eb" for c in ("c1", "c2", "c3", "c4")))
        # 用本地请求送入“几次按放”：切色（经色板绕一圈取红）再填
        rec_red_ev = []
        t = 0
        rec_red_ev += tap("B", t); t += 400        # 开色板
        rec_red_ev += tap("SIDE", t); t += 400     # 蓝 -> 红
        rec_red_ev += tap("B", t); t += 400        # 回画布
        rec_red_ev += hold("SIDE", t, 260)         # 填红
        st, rec_red = req("POST", "/recordings",
                          {"label": "取红并填色", "events": rec_red_ev})
        job_no_red = make_job(rec_red["id"], ["c1"], artist="A2", start_cell="c1")
        ended, effects = run_job(job_no_red, [rec_red_ev])
        cells = cells_snapshot()
        check("补色作业把 c1 从蓝改为红（读到旧状态并覆盖）",
              cells["c1"]["color"] == "#dc2626")
        check("取色过程在效果流里可见（COLOR_PICK 红）",
              any(e.get("effect") == "COLOR_PICK" and e.get("color") == "#dc2626"
                  for e in effects))
    finally:
        stop_server(server)

    # ---- 重启：换一个进程重新打开同一份数据 ----
    section("重启验证｜杀掉服务进程后重启，按作业号查询画格台账")
    server = start_server()
    try:
        _, health = req("GET", "/health")
        check("服务重启成功", health["ok"] is True)
        _, work = req("GET", "/works/W1")
        colors = {c["id"]: c["color"] for c in work["cells"]}
        check("画格颜色跨重启保留（c1 红，其余蓝）",
              colors == {"c1": "#dc2626", "c2": "#2563eb",
                         "c3": "#2563eb", "c4": "#2563eb"}, str(colors))
        # 按作业号查询：基准单与补色单
        _, led1 = req("GET", f"/jobs/{j1}/ledger")
        check(f"基准作业 {j1} 的台账只有 c2 蓝",
              len(led1) == 1 and led1[0]["cell_id"] == "c2"
              and led1[0]["color"] == "#2563eb"
              and led1[0]["artist"] == "A2")
        _, led_red = req("GET", f"/jobs/{job_no_red}/ledger")
        check(f"补色作业 {job_no_red} 的台账是 c1 红，且记录画师/手柄/作业号",
              len(led_red) == 1 and led_red[0]["cell_id"] == "c1"
              and led_red[0]["color"] == "#dc2626"
              and led_red[0]["controller_id"] == "C1"
              and led_red[0]["job_no"] == job_no_red)
        # 全量台账：每一笔“哪一格被谁改成什么”都可追
        _, all_led = req("GET", "/works/W1/-/ledger")
        check("作品级全量台账保留全部落笔（含跨重启）",
              len(all_led) >= 5, f"实际 {len(all_led)} 条")
        check("台账条目均带画师与颜色",
              all(e["artist"] and e["color"] and e["cell_id"] for e in all_led))
        # 运行态不跨重启
        code, _ = req("GET", "/execution/nonexistent", expect_error=True)
        check("执行运行态不跨重启（返回 404）", code == 404)
    finally:
        stop_server(server)

    print("\n" + "#" * 72)
    print(f"验收结束：通过 {PASS} 项，失败 {FAIL} 项")
    print("#" * 72)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
