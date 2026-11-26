#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
插画工作台后端（本地启动、仅 Python 标准库、无外部依赖）。

运营模式：可移植动作谱（题面中的第一种工作室）。
  - 录制台：手柄原始按放事件 -> 按录制手柄的默认布局解析为「操作意图」，动作谱只存意图。
  - 交接：把意图拿到目标手柄的指定布局上做安全分析，找到确实能做完、且同一组合
    不会误触别的操作的按法，才签为安全按法（signed plan）。
  - 模拟工作台：客户领走指派给本手柄的作业，按签名谱在本地实际打 press/release，
    引擎按该手柄的真实按放、面板转移解释，落笔进画格台账（ledger）。

不接受任何用户脚本：客户端只能发手柄事件和受控的建单/签名请求。
"""

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 常量与默认数据
# ---------------------------------------------------------------------------

HOLD_MS = 180          # 按住达到此时长 => 长按手势
SETTLE_MS = 250        # 全部放开后的静默窗，窗内无新键 => 短/长手势落定
VALID_BUTTONS = ("SIDE", "B", "LAYER")
PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#f59e0b"]  # 蓝 红 绿 黄
DEFAULT_COLOR = "#2563eb"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BENCH_DATA", os.path.join(BASE_DIR, "data"))
STATE_FILE = os.path.join(DATA_DIR, "state.json")

STATE_LOCK = threading.RLock()
SESSIONS = {}          # exec_id -> GestureSession（运行态，不跨重启）
STATE = None           # 持久化状态


def default_layout():
    """左手手柄的默认布局 L1：同一颗 SIDE 在不同面板/层键/时长下含义不同。"""
    return {
        "id": "L1",
        "name": "默认左手布局",
        "bindings": [
            {"panel": "canvas",  "input": {"buttons": ["SIDE"], "kind": "tap"},
             "action": "CELL_NEXT"},
            {"panel": "canvas",  "input": {"buttons": ["SIDE"], "kind": "hold"},
             "action": "FILL"},
            {"panel": "canvas",  "input": {"buttons": ["LAYER", "SIDE"], "kind": "tap"},
             "action": "CELL_PREV"},
            {"panel": "canvas",  "input": {"buttons": ["B"], "kind": "tap"},
             "action": "OPEN_PALETTE"},
            {"panel": "palette", "input": {"buttons": ["SIDE"], "kind": "tap"},
             "action": "COLOR_NEXT"},
            {"panel": "palette", "input": {"buttons": ["B"], "kind": "tap"},
             "action": "OPEN_CANVAS"},
        ],
    }


def alt_layout():
    """交接人换的 L2 布局：故意带冲突与缺漏，用来演示签名被拒。"""
    return {
        "id": "L2",
        "name": "备用布局（含冲突）",
        "bindings": [
            # 同一个组合 SIDE 短按被绑到两个动作 => 必误触，不安全
            {"panel": "canvas",  "input": {"buttons": ["SIDE"], "kind": "tap"},
             "action": "CELL_NEXT"},
            {"panel": "canvas",  "input": {"buttons": ["SIDE"], "kind": "tap"},
             "action": "FILL"},
            {"panel": "canvas",  "input": {"buttons": ["B"], "kind": "tap"},
             "action": "OPEN_PALETTE"},
            {"panel": "palette", "input": {"buttons": ["SIDE"], "kind": "tap"},
             "action": "COLOR_NEXT"},
            {"panel": "palette", "input": {"buttons": ["B"], "kind": "tap"},
             "action": "OPEN_CANVAS"},
            # 缺 CELL_PREV、缺 SIDE 长按 FILL 的正规绑法
        ],
    }


def seed_state():
    return {
        "works": {
            "W1": {
                "id": "W1",
                "title": "相邻两格上色练习",
                "cells": [
                    {"id": "c1", "color": None, "sealed_by": None},
                    {"id": "c2", "color": None, "sealed_by": None},
                    {"id": "c3", "color": None, "sealed_by": None},
                    {"id": "c4", "color": None, "sealed_by": None},
                ],
            }
        },
        "artists": {
            "A1": {"id": "A1", "name": "夜班画师"},
            "A2": {"id": "A2", "name": "下班同事"},
            "A3": {"id": "A3", "name": "封稿画师"},
        },
        "controllers": {
            "C1": {
                "id": "C1",
                "name": "借来的左手手柄",
                "layouts": {"L1": default_layout(), "L2": alt_layout()},
                "default_layout": "L1",
            }
        },
        "recordings": {},
        "jobs": [],
        "ledger": [],          # 全局落笔台账
        "counters": {"job": 0, "recording": 0, "ledger": 0},
    }


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def load_state():
    global STATE
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE = json.load(f)
    else:
        STATE = seed_state()
        save_state()


def save_state():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# 手势引擎：录制台与模拟工作台共用同一套按放解释逻辑
# ---------------------------------------------------------------------------

def _input_key(buttons, kind, panel):
    return panel, tuple(sorted(buttons)), kind


def find_binding(layout, panel, buttons, kind):
    """按 面板+组合+长短 找绑定；返回绑定列表（多于 1 条即说明布局有冲突）。"""
    key = _input_key(buttons, kind, panel)
    return [b for b in layout["bindings"]
            if _input_key(b["input"]["buttons"], b["input"]["kind"], b["panel"]) == key]


class GestureSession:
    """
    一次录制或一次作业执行的解释器。

    落定规则：
      press  -> 进入在飞手势；若上一手正处在静默窗，则取消窗并并笔。
      release-> 全部键放开后开启 250ms 静默窗。
      tick   -> 到点落定：按住 >=180ms 算 hold，否则 tap。
      disconnect: 仍有键按住或未走完静默窗的在飞手势整笔丢弃（已落定的不受影响），
                  面板与选区复位。
    """

    def __init__(self, mode, controller_id, layout, work, job=None, recording_id=None,
                 start_cell=0):
        assert mode in ("record", "run")
        self.mode = mode
        self.controller_id = controller_id
        self.layout = layout
        self.work = work
        self.job = job
        self.recording_id = recording_id

        self.held = {}                 # button -> press_ts
        self.buffer = []               # 当前在飞手势的原始事件
        self.gesture_panel = None      # 起手势时所在面板
        self.settle_deadline = None
        self.panel = "canvas"
        self.selection = start_cell    # 当前选中画格下标
        self.start_cell = start_cell
        self.color = DEFAULT_COLOR
        self.gesture_seq = 0
        self.intents = []              # record 模式产物
        self.results = []              # run 模式产物
        self.last_ts = 0
        self.connected = True

    # -- 公共入口 ----------------------------------------------------------

    def push(self, event):
        etype = event["type"]
        ts = event.get("ts", self.last_ts)
        if ts < self.last_ts:
            raise ValueError("事件时间戳必须单调不减")
        self.last_ts = ts

        if etype == "tick":
            return self._flush_due(ts)
        if etype == "connect":
            self.connected = True
            return [{"effect": "CONNECT", "ts": ts}]
        if etype == "disconnect":
            return self._on_disconnect(ts)

        button = event.get("button")
        if button not in VALID_BUTTONS:
            raise ValueError(f"未知按键: {button}")

        if etype == "press":
            if not self.connected:
                raise ValueError("手柄未连接，不能产生 press")
            # 上一笔已全部松开、正在静默窗里：新 press 立即落定上一笔，
            # 随后开启新的一笔（静默窗内并笔只发生在仍有键按住时）
            if self.buffer and not self.held and self.settle_deadline is not None:
                due = self._flush_due(ts, force=True)
            else:
                due = self._flush_due(ts)
            if not self.held:
                self.gesture_panel = self.panel
            self.held[button] = ts
            self.buffer.append(event)
            self.settle_deadline = None
            return due
        if etype == "release":
            if button not in self.held:
                raise ValueError(f"收到 {button} 的 release，但没有对应 press")
            self.held.pop(button)
            self.buffer.append(event)
            if not self.held:
                self.settle_deadline = ts + SETTLE_MS
            return self._flush_due(ts)
        raise ValueError(f"未知事件类型: {etype}")

    def force_flush(self):
        """结束录制/执行时，无条件把静默窗里最后一笔立刻落定。"""
        return self._flush_due(self.last_ts + SETTLE_MS + 1, force=True)

    # -- 内部 --------------------------------------------------------------

    def _on_disconnect(self, ts):
        out = []
        if self.buffer:
            # 仍有键按住或静默窗未走完：在飞手势整笔丢弃，不做任何解释
            out.append({"effect": "GESTURE_DISCARDED", "ts": ts,
                        "events": len(self.buffer),
                        "reason": "设备断开，在飞手势未落定"})
        self.held.clear()
        self.buffer = []
        self.settle_deadline = None
        self.connected = False
        self.panel = "canvas"
        self.selection = self.start_cell
        out.append({"effect": "DISCONNECT", "ts": ts,
                    "note": "在飞手势丢弃，面板/选区复位；已落定落笔保留"})
        return out

    def _flush_due(self, ts, force=False, discard_inflight=False):
        out = []
        if self.settle_deadline is None and not (force and self.buffer):
            return out
        if not force and ts < self.settle_deadline:
            return out
        if self.buffer:
            if discard_inflight or self.held:
                # 静默窗未走完/仍有键按住（掉线）：整笔丢弃
                out.append({"effect": "GESTURE_DISCARDED", "ts": ts,
                            "events": len(self.buffer)})
            else:
                out.extend(self._settle(ts))
        self.buffer = []
        self.settle_deadline = None
        return out

    def _settle(self, ts):
        presses, releases = {}, {}
        for ev in self.buffer:
            b = ev["button"]
            if ev["type"] == "press":
                presses[b] = ev["ts"]
            else:
                releases[b] = ev["ts"]
        if set(presses) != set(releases):
            return [{"effect": "GESTURE_DISCARDED", "ts": ts,
                     "reason": "存在未放开的键"}]

        start = min(presses.values())
        end = max(releases.values())
        duration = end - start
        buttons = sorted(presses)
        kind = "hold" if duration >= HOLD_MS else "tap"
        panel = self.gesture_panel or self.panel
        matches = find_binding(self.layout, panel, buttons, kind)
        seq = self.gesture_seq
        self.gesture_seq += 1

        base = {
            "gesture_seq": seq, "ts": start, "settled_ts": ts,
            "panel_at_press": panel, "buttons": buttons, "kind": kind,
            "duration_ms": duration, "layer_held": "LAYER" in buttons,
        }

        if len(matches) > 1:
            base.update({"effect": "REJECTED",
                         "reason": f"同一组合在 {panel} 面板命中 {len(matches)} 个绑定，布局冲突"})
            if self.mode == "run":
                self.results.append(base)
            return [base]
        if not matches:
            base.update({"effect": "UNBOUND", "reason": "该组合在当前面板无绑定"})
            if self.mode == "run":
                self.results.append(base)
            return [base]

        action = matches[0]["action"]
        return self._apply_action(base, action, end)

    def _apply_action(self, base, action, ts):
        cells = self.work["cells"]

        # 录制与执行共用同一套状态转移（面板/选区/取色），保证后续手势按
        # 转移后的真实面板解释；录制只产出意图，执行才查授权并写台账。
        if action == "CELL_NEXT":
            self.selection = (self.selection + 1) % len(cells)
            base.update(cell=cells[self.selection]["id"])
        elif action == "CELL_PREV":
            self.selection = (self.selection - 1) % len(cells)
            base.update(cell=cells[self.selection]["id"])
        elif action == "OPEN_PALETTE":
            self.panel = "palette"
        elif action == "OPEN_CANVAS":
            self.panel = "canvas"
        elif action == "COLOR_NEXT":
            idx = (PALETTE.index(self.color) + 1) % len(PALETTE) if self.color in PALETTE else 0
            self.color = PALETTE[idx]
            base.update(color=self.color)

        if self.mode == "record":
            intent = {"seq": len(self.intents), "action": action,
                      "panel": base["panel_at_press"]}
            if action == "FILL":
                intent["params"] = {"color": self.color}
            self.intents.append(intent)
            base.update(effect="INTENT_RECORDED", action=action)
            return [base]

        # ---- run 模式：在模拟工作台上真正落笔 ----
        result = dict(base, action=action)
        if action == "CELL_NEXT" or action == "CELL_PREV":
            result.update(effect="SELECT", cell=base["cell"])
        elif action == "OPEN_PALETTE" or action == "OPEN_CANVAS":
            result.update(effect="PANEL_CHANGE", panel=self.panel)
        elif action == "COLOR_NEXT":
            result.update(effect="COLOR_PICK", color=base["color"])
        elif action == "FILL":
            target = cells[self.selection]
            result["cell"] = target["id"]
            result["color"] = self.color
            allowed = self.job["allowed_cells"]
            if target["id"] not in allowed:
                result.update(effect="REJECTED",
                              reason=f"作业 {self.job['job_no']} 无权改 {target['id']}")
            elif target["sealed_by"]:
                result.update(effect="REJECTED",
                              reason=f"{target['id']} 已由 {target['sealed_by']} 正式封稿")
            else:
                target["color"] = self.color
                entry = append_ledger(
                    work_id=self.work["id"], job_id=self.job["id"],
                    job_no=self.job["job_no"], artist=self.job["artist"],
                    controller_id=self.controller_id, cell_id=target["id"],
                    color=self.color, gesture_seq=base["gesture_seq"], ts=ts)
                result.update(effect="FILLED", ledger_id=entry["id"])
        else:
            result.update(effect="REJECTED", reason=f"未知动作 {action}")
        self.results.append(result)
        return [result]


# ---------------------------------------------------------------------------
# 签名安全分析：把意图谱落到目标手柄的具体布局上
# ---------------------------------------------------------------------------

def sign_recording(recording, controller, layout_id):
    layout = controller["layouts"][layout_id]
    entries, findings, safe = [], [], True
    for intent in recording["intents"]:
        wanted = intent["action"]
        panel = intent["panel"]
        candidates = [b for b in layout["bindings"]
                      if b["action"] == wanted and b["panel"] == panel]
        if not candidates:
            safe = False
            findings.append({"seq": intent["seq"], "intent": wanted, "safe": False,
                             "reason": f"布局 {layout_id} 在 {panel} 面板没有能完成 {wanted} 的绑定"})
            entries.append({"seq": intent["seq"], "intent": wanted, "panel": panel,
                            "input": None, "safe": False})
            continue
        entry_safe, chosen, reasons = True, None, []
        for cand in candidates:
            hits = find_binding(layout, panel,
                                cand["input"]["buttons"], cand["input"]["kind"])
            other = [h["action"] for h in hits if h["action"] != wanted]
            if other:
                entry_safe = False
                reasons.append(f"组合 {cand['input']} 同时会误做 {other}")
            elif chosen is None:
                chosen = cand["input"]
        if not entry_safe or chosen is None:
            safe = False
            findings.append({"seq": intent["seq"], "intent": wanted, "safe": False,
                             "reason": "；".join(reasons) or "无无冲突按法"})
            entries.append({"seq": intent["seq"], "intent": wanted, "panel": panel,
                            "input": chosen, "safe": False})
        else:
            findings.append({"seq": intent["seq"], "intent": wanted, "safe": True,
                             "reason": "按法唯一且无误触"})
            entries.append({"seq": intent["seq"], "intent": wanted, "panel": panel,
                            "input": chosen, "safe": True,
                            "params": intent.get("params", {})})
    return {"layout_id": layout_id, "safe": safe, "entries": entries,
            "findings": findings}


def append_ledger(work_id, job_id, job_no, artist, controller_id,
                  cell_id, color, gesture_seq, ts):
    STATE["counters"]["ledger"] += 1
    entry = {
        "id": f"L-{STATE['counters']['ledger']:04d}",
        "work_id": work_id, "job_id": job_id, "job_no": job_no,
        "artist": artist, "controller_id": controller_id,
        "cell_id": cell_id, "color": color,
        "gesture_seq": gesture_seq, "ts": ts,
    }
    STATE["ledger"].append(entry)
    return entry


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "IllustBench/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"请求体不是合法 JSON: {e}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def do_GET(self):
        try:
            with STATE_LOCK:
                self._route_get(urlparse(self.path).path.rstrip("/") or "/")
        except HttpError as e:
            self._err(e.code, e.message)
        except Exception as e:  # noqa: BLE001
            self._err(500, f"服务器内部错误: {e}")

    def do_POST(self):
        try:
            data = self._read_json()
            with STATE_LOCK:
                self._route_post(urlparse(self.path).path.rstrip("/") or "/", data)
        except HttpError as e:
            self._err(e.code, e.message)
        except ValueError as e:
            self._err(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._err(500, f"服务器内部错误: {e}")

    # -- GET 路由 ----------------------------------------------------------

    def _route_get(self, path):
        if path == "/health":
            return self._send(200, {"ok": True, "service": "illust-workbench"})
        if path == "/works":
            return self._send(200, list(STATE["works"].values()))
        if path == "/controllers":
            return self._send(200, list(STATE["controllers"].values()))
        if path == "/recordings":
            return self._send(200, list(STATE["recordings"].values()))
        if path == "/jobs":
            return self._send(200, STATE["jobs"])

        parts = path.split("/")[1:]
        if parts[0] == "works" and len(parts) == 2:
            return self._send(200, self._work(parts[1]))
        if parts[0] == "works" and len(parts) == 4 and parts[3] == "ledger":
            work = self._work(parts[1])
            entries = [e for e in STATE["ledger"] if e["work_id"] == work["id"]]
            if parts[2] != "-":
                entries = [e for e in entries if e["job_id"] == parts[2]]
            return self._send(200, entries)
        if parts[0] == "controllers" and len(parts) == 3 and parts[2] == "jobs":
            cid = parts[1]
            if cid not in STATE["controllers"]:
                raise HttpError(404, f"未知手柄 {cid}")
            jobs = [j for j in STATE["jobs"]
                    if j["controller_id"] == cid and j["status"] == "assigned"]
            return self._send(200, {"controller_id": cid, "jobs": jobs})
        if parts[0] == "recordings" and len(parts) == 2:
            rid = parts[1]
            if rid not in STATE["recordings"]:
                raise HttpError(404, f"未知录制 {rid}")
            return self._send(200, STATE["recordings"][rid])
        if parts[0] == "jobs" and len(parts) == 2:
            return self._send(200, self._job(parts[1]))
        if parts[0] == "jobs" and len(parts) == 3 and parts[2] == "ledger":
            job = self._job(parts[1])
            return self._send(200, [e for e in STATE["ledger"]
                                    if e["job_id"] == job["id"]])
        if parts[0] == "execution" and len(parts) == 2:
            eid = parts[1]
            if eid not in SESSIONS:
                raise HttpError(404, f"未知执行会话 {eid}（重启后运行态不保留）")
            s = SESSIONS[eid]
            return self._send(200, {
                "exec_id": eid, "connected": s.connected, "panel": s.panel,
                "selection": s.work["cells"][s.selection]["id"],
                "color": s.color, "results": s.results,
                "gesture_count": s.gesture_seq})
        raise HttpError(404, f"无此路径: {path}")

    # -- POST 路由 ---------------------------------------------------------

    def _route_post(self, path, data):
        parts = path.split("/")[1:]

        if parts == ["recordings"]:
            return self._create_recording(data)
        if parts == ["jobs"]:
            return self._create_job(data)
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "sign":
            return self._sign_job(parts[1], data)
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "execution":
            return self._start_execution(parts[1], data)
        if len(parts) == 5 and parts[0] == "works" and parts[2] == "cells" and parts[4] == "seal":
            return self._seal_cell(parts[1], data, cell_id=parts[3])
        if len(parts) == 3 and parts[0] == "execution" and parts[2] == "events":
            return self._push_events(parts[1], data)
        if len(parts) == 3 and parts[0] == "execution" and parts[2] == "end":
            return self._end_execution(parts[1])
        raise HttpError(404, f"无此路径: {path}")

    # -- 业务处理 ----------------------------------------------------------

    def _work(self, wid):
        if wid not in STATE["works"]:
            raise HttpError(404, f"未知作品 {wid}")
        return STATE["works"][wid]

    def _job(self, key):
        for j in STATE["jobs"]:
            if j["id"] == key or j["job_no"] == key:
                return j
        raise HttpError(404, f"未知作业 {key}")

    def _create_recording(self, data):
        cid = data.get("controller_id", "C1")
        wid = data.get("work_id", "W1")
        if cid not in STATE["controllers"]:
            raise HttpError(404, f"未知手柄 {cid}")
        work = self._work(wid)
        events = data.get("events")
        if not isinstance(events, list) or not events:
            raise HttpError(400, "events 必须是非空手柄事件数组（仅 press/release/tick/disconnect/connect）")
        controller = STATE["controllers"][cid]
        record_layout = controller["layouts"][controller["default_layout"]]
        engine = GestureSession("record", cid, record_layout, work,
                                recording_id=None)
        parse_log = []
        for ev in events:
            parse_log.extend(engine.push(ev))
        parse_log.extend(engine.force_flush())
        if not engine.intents:
            raise HttpError(400, "没有从事件带解析出任何操作意图，录制不成立")
        STATE["counters"]["recording"] += 1
        rid = f"R-{STATE['counters']['recording']:03d}"
        recording = {
            "id": rid,
            "label": data.get("label", ""),
            "controller_id": cid,
            "work_id": wid,
            "intents": engine.intents,
            "raw_events": events,
            "parse_log": parse_log,
        }
        STATE["recordings"][rid] = recording
        save_state()
        self._send(201, recording)

    def _create_job(self, data):
        rid = data.get("recording_id")
        cid = data.get("controller_id", "C1")
        wid = data.get("work_id", "W1")
        artist = data.get("artist", "A2")
        allowed = data.get("allowed_cells")
        if rid not in STATE["recordings"]:
            raise HttpError(404, f"未知录制 {rid}")
        if cid not in STATE["controllers"]:
            raise HttpError(404, f"未知手柄 {cid}")
        work = self._work(wid)
        cell_ids = [c["id"] for c in work["cells"]]
        if not isinstance(allowed, list) or not allowed:
            raise HttpError(400, "allowed_cells 必须是非空画格数组（合法作业必须声明授权范围）")
        bad = [c for c in allowed if c not in cell_ids]
        if bad:
            raise HttpError(400, f"授权画格不存在: {bad}")
        if artist not in STATE["artists"]:
            raise HttpError(400, f"未知画师 {artist}（不能凭空造身份）")
        recording = STATE["recordings"][rid]
        if recording["work_id"] != wid:
            raise HttpError(400, "录制与作业不属于同一作品")
        start_cell_id = data.get("start_cell", cell_ids[0])
        if start_cell_id not in cell_ids:
            raise HttpError(400, f"起始画格不存在: {start_cell_id}")
        start_cell = cell_ids.index(start_cell_id)
        STATE["counters"]["job"] += 1
        job = {
            "id": f"JOB-{uuid.uuid4().hex[:8]}",
            "job_no": f"J-{STATE['counters']['job']:04d}",
            "recording_id": rid,
            "controller_id": cid,
            "work_id": wid,
            "artist": artist,
            "allowed_cells": allowed,
            "start_cell_id": start_cell_id,
            "start_cell": start_cell,
            "status": "assigned",          # assigned -> running -> completed
            "signed_plan": None,
            "results": [],
            "exec_events": 0,
        }
        STATE["jobs"].append(job)
        save_state()
        self._send(201, job)

    def _sign_job(self, job_key, data):
        job = self._job(job_key)
        if job["status"] != "assigned":
            raise HttpError(409, f"作业 {job['job_no']} 已开始执行，不能重新签名")
        controller = STATE["controllers"][job["controller_id"]]
        layout_id = data.get("layout_id", controller["default_layout"])
        if layout_id not in controller["layouts"]:
            raise HttpError(404, f"手柄 {job['controller_id']} 上没有布局 {layout_id}")
        recording = STATE["recordings"][job["recording_id"]]
        plan = sign_recording(recording, controller, layout_id)
        plan["job_no"] = job["job_no"]
        if plan["safe"]:
            job["signed_plan"] = plan
            save_state()
            self._send(200, plan)
        else:
            # 不安全的布局不签名、不改任何持久状态
            self._send(409, {"error": "该布局无法安全完成动作谱，拒绝签名",
                             "plan": plan})

    def _seal_cell(self, wid, data, cell_id=None):
        work = self._work(wid)
        cell_id = cell_id or data.get("cell_id")
        artist = data.get("artist")
        target = next((c for c in work["cells"] if c["id"] == cell_id), None)
        if target is None:
            raise HttpError(404, f"作品 {wid} 没有画格 {cell_id}")
        if artist not in STATE["artists"]:
            raise HttpError(400, f"未知画师 {artist}")
        sealed = data.get("sealed", True)
        if sealed:
            target["sealed_by"] = artist
        else:
            # 只有当初封稿的画师本人能撤回封稿
            if target["sealed_by"] is None:
                raise HttpError(400, f"{cell_id} 本就没有封稿")
            if target["sealed_by"] != artist:
                raise HttpError(409, f"{cell_id} 由 {target['sealed_by']} 封稿，"
                                     f"{artist} 无权撤回")
            target["sealed_by"] = None
        save_state()
        self._send(200, {"work_id": wid, "cell_id": cell_id,
                         "sealed_by": target["sealed_by"]})

    def _start_execution(self, job_key, data):
        job = self._job(job_key)
        if job["status"] == "completed":
            raise HttpError(409, f"作业 {job['job_no']} 已完成")
        if not job["signed_plan"]:
            raise HttpError(409, "作业尚未拿到安全签名按法，不能开工")
        cid = job["controller_id"]
        if any(s.job["id"] == job["id"] for s in SESSIONS.values()):
            raise HttpError(409, "该作业已有进行中的执行会话")
        if any(s.controller_id == cid and s.connected for s in SESSIONS.values()):
            raise HttpError(409, f"手柄 {cid} 正被另一张作业占用")
        controller = STATE["controllers"][cid]
        layout = controller["layouts"][job["signed_plan"]["layout_id"]]
        engine = GestureSession("run", cid, layout, STATE["works"][job["work_id"]],
                                job=job, start_cell=job["start_cell"])
        eid = f"EXEC-{uuid.uuid4().hex[:8]}"
        SESSIONS[eid] = engine
        job["status"] = "running"
        save_state()
        self._send(201, {"exec_id": eid, "job_no": job["job_no"],
                         "controller_id": cid,
                         "guide": job["signed_plan"]["entries"],
                         "note": "按 guide 里的 input 依次发 press/release 事件"})

    def _push_events(self, eid, data):
        if eid not in SESSIONS:
            raise HttpError(404, f"未知执行会话 {eid}")
        events = data.get("events")
        if not isinstance(events, list) or not events:
            raise HttpError(400, "events 必须是非空数组")
        engine = SESSIONS[eid]
        out = []
        for ev in events:
            if not isinstance(ev, dict) or "type" not in ev:
                raise HttpError(400, "每个事件必须含 type")
            out.extend(engine.push(ev))
        engine.job["exec_events"] += len(events)
        save_state()
        self._send(200, {"exec_id": eid, "effects": out,
                         "panel": engine.panel,
                         "selection": engine.work["cells"][engine.selection]["id"],
                         "connected": engine.connected})

    def _end_execution(self, eid):
        if eid not in SESSIONS:
            raise HttpError(404, f"未知执行会话 {eid}")
        engine = SESSIONS[eid]
        tail = engine.force_flush()
        engine.job["results"] = engine.results
        engine.job["status"] = "completed"
        save_state()
        del SESSIONS[eid]
        self._send(200, {"exec_id": eid, "tail_effects": tail,
                         "job_no": engine.job["job_no"],
                         "results": engine.results,
                         "work": engine.work})


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def main():
    load_state()
    port = int(os.environ.get("BENCH_PORT", "8806"))
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"插画工作台后端已启动: http://127.0.0.1:{port}  数据目录: {DATA_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
