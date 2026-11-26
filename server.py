# -*- coding: utf-8 -*-
"""
长截图分享服务（本地 HTTP 版）。

建模要点：
1. 每屏 = 独立吸顶栏 header + 固定行数的正文 body。吸顶栏不参与滚动对齐。
2. 每个正文槽位是一个 cell(screen, slot)，文字 text 只是“长相”，不是身份。
   服务通过滚动对齐 + 补拍定位记号推断 cell 背后的记录标识 record_id。
3. 滚动距离给出的是范围 [min, max]，因此相邻两屏可能有多个候选对齐：
   - 候选必须与重叠区正文文字全部一致；
   - 候选还必须与全部补拍记号一致（同记号同名、异记号异名、P{n} 名落在第 n 位）。
4. 一个 cell 在所有存活候选里指向同一记录 => certain；指向多个 => uncertain。
   某条转缝没有任何存活候选 => 断缝(broken)，后一屏重新生根，两侧记录互不相认。
5. 客户点选具体 cell 给出角色：voucher（公开凭证）/ note（私人备注）。
   角色沿“确定的同一记录”传播到后续屏的同一行；再按文字兜底，防止断缝后
   同字私注漏遮。私注永远遮盖；凭证按授权记录决定可见或遮盖。
6. 分享产物是分屏凭条：保持原屏顺序、注明来源屏、只就该页确知的内容遮盖，
   收件人接口永远拿不到制作台里的未遮盖原屏。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DATA_PATH = os.environ.get("LSHOT_DATA", os.path.join(os.path.dirname(__file__), "data", "lshot.json"))
HOST = os.environ.get("LSHOT_HOST", "127.0.0.1")
PORT = int(os.environ.get("LSHOT_PORT", "8321"))

MAX_ALIGNMENT_PATHS = 256  # 候选组合爆炸时的保守上限

_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #
def _empty_store() -> dict:
    return {"rec_seq": 0, "share_seq": 0, "recordings": {}, "shares": {}}


def load_store() -> dict:
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            store = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty_store()
    # 启动时重算确定性分析，避免落盘冗余状态。
    for rec in store["recordings"].values():
        rec["analysis"] = analyze(rec)
    return store


def save_store(store: dict) -> None:
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_PATH)


# 注意：STORE 在文件末尾初始化，因为 load_store 依赖下方定义的 analyze()。


# --------------------------------------------------------------------------- #
# 对齐与记录身份推断
# --------------------------------------------------------------------------- #
def _candidate_consistent(prev_body: list[str], body: list[str], d: int, n: int) -> bool:
    """滚动 d 行时，重叠的 n-d 个正文格文字必须完全一致。"""
    overlap = n - d
    return all(prev_body[d + t] == body[t] for t in range(overlap))


def analyze(rec: dict) -> dict:
    """推断转缝候选、每个 cell 的记录身份与确定性。"""
    n = int(rec["viewport_rows"])
    screens = rec["screens"]
    lo, hi = rec["scroll_range"]
    marks = rec.get("marks", [])

    for idx, sc in enumerate(screens):
        if len(sc["body"]) != n:
            raise ValueError(f"屏 {idx} 正文行数 {len(sc['body'])} 与视口 {n} 不一致")

    # 记号索引：(screen, slot) -> record 名
    mark_at = {(m["screen"], m["slot"]): m["record"] for m in marks}

    # 每条转缝的文字一致候选
    transition_candidates: list[list[int]] = []
    transitions: list[dict] = []
    for i in range(1, len(screens)):
        ds = [d for d in range(lo, hi + 1) if 1 <= d <= n
              and _candidate_consistent(screens[i - 1]["body"], screens[i]["body"], d, n)]
        transition_candidates.append(ds)
        transitions.append({
            "from_screen": i - 1,
            "to_screen": i,
            "scroll_range": [lo, hi],
            "text_consistent_candidates": ds,
            "surviving_candidates": ds,
            "broken": not ds,
        })

    # 分段：断缝之间各自生根。文字层先断一次；记号层否决某转缝时再递归拆。
    text_segments: list[list[int]] = [[0]]
    for tr, ds in zip(transitions, transition_candidates):
        if not ds:
            text_segments.append([tr["to_screen"]])
        else:
            text_segments[-1].append(tr["to_screen"])

    # cell -> 记录 key 集合；key = (root, global_pos)，多 key 即不确定
    cell_keys: dict[tuple[int, int], set] = {}
    marked_names: dict[tuple, str] = {}  # key -> 记号赋予的记录名
    surviving_per_transition: dict[int, list[int]] = {}
    root_counter = 0

    def process_segment(seg: list[int], root: int) -> None:
        nonlocal root_counter
        seg_trans = list(range(seg[0], seg[-1]))
        cand_lists = [transition_candidates[i - 1] for i in seg_trans]

        paths: list[tuple[int, ...]] = [()]
        for cands in cand_lists:
            paths = [p + (d,) for p in paths for d in cands]
            if len(paths) > MAX_ALIGNMENT_PATHS:
                paths = []  # 组合过多：保守起见全部视为不确定
                break

        kept_paths: list[tuple[int, ...]] = []
        for path in paths:
            offsets, off = {}, 0
            for seg_j, screen_i in enumerate(seg):
                offsets[screen_i] = off
                if seg_j < len(path):
                    off += path[seg_j]
            name_to_keys: dict[str, set] = {}
            key_to_names: dict[tuple, set] = {}
            for (ms, mp), name in mark_at.items():
                if ms not in seg:
                    continue
                key = (root, offsets[ms] + mp)
                name_to_keys.setdefault(name, set()).add(key)
                key_to_names.setdefault(key, set()).add(name)
            valid = all(len(names) == 1 for names in key_to_names.values())
            for name, keys in name_to_keys.items():
                if len(keys) > 1:
                    valid = False
                if name.startswith("P") and name[1:].isdigit() and int(name[1:]) not in {k[1] for k in keys}:
                    valid = False
            if valid:
                kept_paths.append(path)

        if not kept_paths:
            # 记号否决了全部文字候选（典型：同字收据实为另一处记录）。
            # 从段内第一条转缝拆开各自生根，再递归处理。
            if len(seg) == 1:
                for p in range(n):
                    cell_keys.setdefault((seg[0], p), set())
                return
            cut = seg_trans[0]
            left, right = seg[: seg.index(cut + 1)], seg[seg.index(cut + 1):]
            # 被记号否决的转缝（cut 即 from_screen）：无存活候选，两侧各自生根
            surviving_per_transition[cut] = []
            process_segment(left, root)
            root_counter += 1
            process_segment(right, root_counter)
            return

        # 存活候选只在确有路径时写回，避免覆盖拆段时记录的“否决”状态
        for seg_j, screen_i in enumerate(seg_trans):
            surviving = sorted({p[seg_j] for p in kept_paths})
            surviving_per_transition[screen_i] = surviving

        for path in kept_paths:
            off = 0
            for seg_j, screen_i in enumerate(seg):
                for p in range(n):
                    cell_keys.setdefault((screen_i, p), set()).add((root, off + p))
                if seg_j < len(path):
                    off += path[seg_j]

        off = 0
        for seg_j, screen_i in enumerate(seg):
            for p in range(n):
                if (screen_i, p) in mark_at:
                    marked_names[(root, off + p)] = mark_at[(screen_i, p)]
            if seg_j < len(kept_paths[0]):
                off += kept_paths[0][seg_j]

    for seg in text_segments:
        process_segment(seg, root_counter)
        root_counter += 1

    for tr in transitions:
        # 未被任何段处理（单子屏）或被记号否决的转缝：无存活候选
        tr["surviving_candidates"] = surviving_per_transition.get(
            tr["from_screen"], [])
        if not tr["surviving_candidates"]:
            tr["broken"] = True

    def record_name(key: tuple[int, int]) -> str:
        if key in marked_names:
            return marked_names[key]
        root, g = key
        return f"P{g}" if root == 0 else f"s{root}P{g}"

    cells = []
    for i, sc in enumerate(screens):
        for p, text in enumerate(sc["body"]):
            keys = cell_keys.get((i, p), set())
            certain = len(keys) == 1
            cells.append({
                "cell_id": f"s{i}p{p}",
                "screen": i,
                "slot": p,
                "text": text,
                "certain": certain,
                "record_id": record_name(next(iter(keys))) if certain else None,
                "candidate_records": sorted(record_name(k) for k in keys),
                "role": "plain",
            })

    by_id = {c["cell_id"]: c for c in cells}

    # 客户点选：沿确定的同一记录传播角色
    selections = rec.get("selections", [])
    for sel in selections:
        cell = by_id[sel["cell_id"]]
        cell["role"] = sel["role"]
        if cell["certain"]:
            for other in cells:
                if other is not cell and other["certain"] and other["record_id"] == cell["record_id"]:
                    # 凭证/备注角色不互相覆盖；备注传播优先级最高
                    if sel["role"] == "note" or other["role"] == "plain":
                        other["role"] = sel["role"]

    # 文字兜底：断缝或不确定区里，同字私注/凭证仍按成分处理（私注不漏遮）
    voucher_texts = {c["text"] for c in cells if c["role"] == "voucher"}
    note_texts = {c["text"] for c in cells if c["role"] == "note"}
    for c in cells:
        if c["role"] == "plain":
            if c["text"] in note_texts:
                c["role"] = "note"
            elif c["text"] in voucher_texts:
                c["role"] = "voucher"

    return {
        "viewport_rows": n,
        "transitions": transitions,
        "cells": cells,
        "headers": [{"screen": i, "header": sc.get("header")} for i, sc in enumerate(screens)],
    }


# --------------------------------------------------------------------------- #
# 分屏凭条（分享产物）
# --------------------------------------------------------------------------- #
def _voucher_visible(cell: dict, entitlements: set[str]) -> tuple[bool, str | None]:
    if not cell["certain"]:
        return False, "identity_uncertain"
    if cell["record_id"] in entitlements:
        return True, None
    return False, "not_authorized"


def build_share(rec: dict, spec: dict) -> dict:
    analysis = rec["analysis"]
    cells = analysis["cells"]
    n = analysis["viewport_rows"]
    screens = rec["screens"]
    pages_wanted = sorted(spec.get("pages", list(range(len(screens)))))

    # 请求页面若跨越断缝（文字无候选或被补拍记号否决），拼接不确定，整组不能发：
    # 不能把后一屏同字内容签成前一屏的那笔，也不允许靠涂黑蒙混。
    for tr in analysis["transitions"]:
        if tr["broken"] and tr["from_screen"] in pages_wanted and tr["to_screen"] in pages_wanted:
            raise ShareError(
                409, "UNCERTAIN_STITCH_CANNOT_SEND",
                f"屏 {tr['from_screen']} -> {tr['to_screen']} 的拼接已被滚动证据/补拍记号否决，"
                "不能放在同一份分享里；请分开出具、补拍定位记号或缩小授权范围")

    selected_voucher_records = {
        c["record_id"] for c in cells
        if c["role"] == "voucher" and c["certain"]
        and any(s["cell_id"] == c["cell_id"] and s["role"] == "voucher" for s in rec.get("selections", []))
    }
    entitlements = set(spec.get("entitlements", sorted(selected_voucher_records)))
    certify = set(spec.get("certify", sorted(entitlements)))

    pages = []
    for i in pages_wanted:
        body_cells = []
        for c in (x for x in cells if x["screen"] == i):
            item = {"cell_id": c["cell_id"], "source_screen": i, "slot": c["slot"],
                    "certain": c["certain"], "role": c["role"]}
            if c["role"] == "note":
                item.update(masked=True, mask_reason="private_note")
            elif c["role"] == "voucher":
                visible, reason = _voucher_visible(c, entitlements)
                if visible:
                    item.update(masked=False, text=c["text"], record_id=c["record_id"],
                                certified=c["record_id"] in certify)
                else:
                    item.update(masked=True, mask_reason=reason)
            else:
                item.update(masked=False, text=c["text"],
                            record_id=c["record_id"] if c["certain"] else None)
            body_cells.append(item)

        # 不得整张涂黑：一页正文至少要有一个可见格
        if all(x["masked"] for x in body_cells):
            raise ShareError(409, "REFUSE_FULL_BLACKOUT",
                             f"来源屏 {i} 全部正文格都需遮盖，拒绝出具整页涂黑的凭条")
        pages.append({
            "source_screen": i,
            "header": screens[i].get("header"),
            "cells": body_cells,
        })

    # 必须认证的记录，要在某页上以“确定且授权的凭证”真实出现
    visible_certified = {
        x["record_id"] for pg in pages for x in pg["cells"]
        if not x["masked"] and x.get("certified")
    }
    missing = certify - visible_certified
    if missing:
        raise ShareError(
            409, "UNCERTAIN_STITCH_CANNOT_SEND",
            f"待认证记录 {sorted(missing)} 没有在任何页面上以确定身份出现，"
            "该拼接尚不确定，不能发送；请先补拍定位记号或缩小授权范围")

    return {
        "entitlements": sorted(entitlements),
        "certify": sorted(certify),
        "pages": pages,
    }


class ShareError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def build_claim(rec: dict, parent_share: dict, cell_id: str) -> dict:
    """申领只含指定行的第二张凭条（如“只含丙”）。"""
    cell = next((c for c in rec["analysis"]["cells"] if c["cell_id"] == cell_id), None)
    if cell is None:
        raise ShareError(404, "NO_SUCH_CELL", f"单元格 {cell_id} 不存在")
    if cell["role"] == "note":
        raise ShareError(409, "REFUSE_PRIVATE_CLAIM", "私人备注不能对外出具凭条")
    if not cell["certain"]:
        raise ShareError(409, "IDENTITY_UNCERTAIN",
                         "该行身份在现有滚动证据下不确定，请先补拍定位记号再申领")
    page = {
        "source_screen": cell["screen"],
        "header": rec["screens"][cell["screen"]].get("header"),
        "cells": [{
            "cell_id": cell["cell_id"],
            "source_screen": cell["screen"],
            "slot": cell["slot"],
            "certain": True,
            "role": "voucher_slip",
            "masked": False,
            "text": cell["text"],
            "record_id": cell["record_id"],
            "certified": True,
        }],
    }
    return {"entitlements": [cell["record_id"]], "certify": [cell["record_id"]],
            "pages": [page], "claimed_from": parent_share["share_id"]}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "LongShot/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _send(self, status: int, payload: dict | list):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise ShareError(400, "BAD_JSON", "请求体不是合法 JSON")

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if parts == ["health"]:
                return self._send(200, {"ok": True})
            if parts == ["recordings"]:
                with _LOCK:
                    return self._send(200, [{"recording_id": rid, "name": r["name"]}
                                            for rid, r in STORE["recordings"].items()])
            if len(parts) == 2 and parts[0] == "recordings":
                with _LOCK:
                    rec = STORE["recordings"].get(parts[1])
                    if not rec:
                        raise ShareError(404, "NO_SUCH_RECORDING", "录屏不存在")
                    return self._send(200, _recording_view(rec))
            if len(parts) == 2 and parts[0] == "shares":
                qs = parse_qs(parsed.query)
                token = (qs.get("token") or [""])[0]
                with _LOCK:
                    share = STORE["shares"].get(parts[1])
                    if not share:
                        raise ShareError(404, "NO_SUCH_SHARE", "分享号不存在")
                    if not token or token != share["token"]:
                        raise ShareError(403, "BAD_TOKEN", "领取口令无效")
                    # 收件人视图：只有脱敏后的页面，绝无原屏文字
                    return self._send(200, {
                        "share_id": share["share_id"],
                        "parent": share.get("parent"),
                        "claimed_from": share.get("claimed_from"),
                        "pages": share["pages"],
                    })
            raise ShareError(404, "NO_ROUTE", "路径不存在")
        except ShareError as e:
            return self._send(e.status, {"error": e.code, "message": str(e)})
        except Exception as e:  # 兜底：任何意外都返回结构化 500，不断连
            return self._send(500, {"error": "INTERNAL", "message": str(e)})

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            if parts == ["recordings"]:
                return self._create_recording()
            if len(parts) == 3 and parts[0] == "recordings" and parts[2] == "marks":
                return self._add_marks(parts[1])
            if len(parts) == 3 and parts[0] == "recordings" and parts[2] == "select":
                return self._select(parts[1])
            if len(parts) == 3 and parts[0] == "recordings" and parts[2] == "shares":
                return self._create_share(parts[1])
            if len(parts) == 3 and parts[0] == "shares" and parts[2] == "claim":
                return self._claim(parts[1])
            raise ShareError(404, "NO_ROUTE", "路径不存在")
        except ShareError as e:
            return self._send(e.status, {"error": e.code, "message": str(e)})
        except ValueError as e:
            return self._send(400, {"error": "BAD_INPUT", "message": str(e)})
        except Exception as e:
            return self._send(500, {"error": "INTERNAL", "message": str(e)})

    def _create_recording(self):
        body = self._body()
        for key in ("viewport_rows", "screens", "scroll_range"):
            if key not in body:
                raise ShareError(400, "MISSING_FIELD", f"缺少字段 {key}")
        if not isinstance(body["screens"], list) or not body["screens"]:
            raise ShareError(400, "BAD_SCREENS", "screens 必须是非空数组")
        for sc in body["screens"]:
            if "body" not in sc or not isinstance(sc["body"], list):
                raise ShareError(400, "BAD_SCREENS", "每屏需要 body 数组")
        lo, hi = body["scroll_range"]
        if not (1 <= lo <= hi <= body["viewport_rows"]):
            raise ShareError(400, "BAD_RANGE", "滚动范围需满足 1 <= min <= max <= 视口行数")
        with _LOCK:
            STORE["rec_seq"] += 1
            rid = f"REC-{STORE['rec_seq']}"
            rec = {
                "recording_id": rid,
                "name": body.get("name", rid),
                "viewport_rows": body["viewport_rows"],
                "scroll_range": [lo, hi],
                "screens": body["screens"],
                "marks": [],
                "selections": [],
            }
            rec["analysis"] = analyze(rec)
            STORE["recordings"][rid] = rec
            save_store(STORE)
            return self._send(201, _recording_view(rec))

    def _add_marks(self, rid: str):
        body = self._body()
        marks = body.get("marks")
        if not isinstance(marks, list):
            raise ShareError(400, "BAD_MARKS", "marks 必须是数组")
        with _LOCK:
            rec = self._require_recording(rid)
            for m in marks:
                if not all(k in m for k in ("screen", "slot", "record")):
                    raise ShareError(400, "BAD_MARK", "记号需要 screen/slot/record")
                if not (0 <= m["screen"] < len(rec["screens"])
                        and 0 <= m["slot"] < rec["viewport_rows"]):
                    raise ShareError(400, "BAD_MARK", "记号位置越界")
            # 同一位置重复记号以最后一次为准（补拍修正）
            for m in marks:
                rec["marks"] = [x for x in rec["marks"]
                                if (x["screen"], x["slot"]) != (m["screen"], m["slot"])]
                rec["marks"].append(m)
            rec["analysis"] = analyze(rec)
            save_store(STORE)
            return self._send(200, _recording_view(rec))

    def _select(self, rid: str):
        body = self._body()
        cell_id = body.get("cell_id")
        role = body.get("role")
        if role not in ("voucher", "note"):
            raise ShareError(400, "BAD_ROLE", "role 只能是 voucher 或 note")
        with _LOCK:
            rec = self._require_recording(rid)
            if not any(c["cell_id"] == cell_id for c in rec["analysis"]["cells"]):
                raise ShareError(404, "NO_SUCH_CELL", f"单元格 {cell_id} 不存在")
            rec["selections"] = [s for s in rec["selections"] if s["cell_id"] != cell_id]
            rec["selections"].append({"cell_id": cell_id, "role": role})
            rec["analysis"] = analyze(rec)
            save_store(STORE)
            return self._send(200, _recording_view(rec))

    def _create_share(self, rid: str):
        spec = self._body()
        with _LOCK:
            rec = self._require_recording(rid)
            snapshot = build_share(rec, spec)
            STORE["share_seq"] += 1
            sid = f"SH-{STORE['share_seq']}"
            share = {
                "share_id": sid,
                "recording_id": rid,
                "token": str(uuid.uuid4()),
                "pages": snapshot["pages"],
                "entitlements": snapshot["entitlements"],
                "certify": snapshot["certify"],
            }
            STORE["shares"][sid] = share
            save_store(STORE)
            return self._send(201, _share_view(share))

    def _claim(self, sid: str):
        body = self._body()
        token = body.get("token", "")
        cell_id = body.get("cell_id")
        with _LOCK:
            parent = STORE["shares"].get(sid)
            if not parent:
                raise ShareError(404, "NO_SUCH_SHARE", "分享号不存在")
            if token != parent["token"]:
                raise ShareError(403, "BAD_TOKEN", "领取口令无效")
            rec = STORE["recordings"][parent["recording_id"]]
            snapshot = build_claim(rec, parent, cell_id)
            STORE["share_seq"] += 1
            child_id = f"SH-{STORE['share_seq']}"
            child = {
                "share_id": child_id,
                "recording_id": parent["recording_id"],
                "token": str(uuid.uuid4()),
                "parent": sid,
                "claimed_from": sid,
                "pages": snapshot["pages"],
                "entitlements": snapshot["entitlements"],
                "certify": snapshot["certify"],
            }
            STORE["shares"][child_id] = child
            save_store(STORE)
            return self._send(201, _share_view(child))

    def _require_recording(self, rid: str) -> dict:
        rec = STORE["recordings"].get(rid)
        if not rec:
            raise ShareError(404, "NO_SUCH_RECORDING", "录屏不存在")
        return rec


def _recording_view(rec: dict) -> dict:
    """制作台视图：含原屏文字，仅店员/客户侧使用，收件人接口不返回此结构。"""
    return {
        "recording_id": rec["recording_id"],
        "name": rec["name"],
        "viewport_rows": rec["viewport_rows"],
        "scroll_range": rec["scroll_range"],
        "screens": rec["screens"],
        "marks": rec["marks"],
        "selections": rec["selections"],
        "analysis": rec["analysis"],
    }


def _share_view(share: dict) -> dict:
    return {
        "share_id": share["share_id"],
        "token": share["token"],
        "parent": share.get("parent"),
        "claimed_from": share.get("claimed_from"),
        "entitlements": share["entitlements"],
        "certify": share["certify"],
        "pages": share["pages"],
    }


STORE = load_store()


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"long-shot service on http://{HOST}:{PORT} data={DATA_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
