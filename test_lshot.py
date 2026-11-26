# -*- coding: utf-8 -*-
"""
长截图分享服务的端到端测试。全部通过本地 HTTP 服务进行，含一次重启复查。

场景：
  A. 基准打通：四行视口、固定下滚两行、收据七/私注七在第二屏重复；
     第一张凭条含两屏（私注七处处遮盖、收据七可领），再申领只含丙的第二张；
     重启后凭分享号+口令仍能打开。
  B. 另一份会话，只改补拍证据：第二屏同字“收据七”被记号证明是另一处记录，
     不得签成第一笔；随后保持屏与定位不动、只缩小授权，连收据七也不得出现。
  C. 完全重复正文导致 d=1/d=2 两种对齐：重叠区身份不确定，不能出具该凭证；
     补拍一个定位记号后才确定，可出具。
  D. “周一”在滚动截屏里出现三次，属于三笔不同记录：签收按记录身份不按文字。
  E. 收件人侧安全：拿不到未遮盖原屏；无口令/错口令被拒；整页涂黑被服务端拒绝；
     私人备注不允许被申领。
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8417
BASE = f"http://127.0.0.1:{PORT}"


def req(method: str, path: str, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def expect(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def wait_ready(proc):
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务进程提前退出")
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("服务未在限定时间内启动")


def start_server(data_file: str):
    env = dict(os.environ, LSHOT_DATA=data_file, LSHOT_PORT=str(PORT))
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wait_ready(proc)
    return proc


def stop_server(proc):
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)


def cell(rec, cell_id):
    return next(c for c in rec["analysis"]["cells"] if c["cell_id"] == cell_id)


def page_of(share, screen):
    return next(p for p in share["pages"] if p["source_screen"] == screen)


def item(page, cell_id):
    return next(x for x in page["cells"] if x["cell_id"] == cell_id)


# --------------------------------------------------------------------------- #
def scenario_a():
    print("场景 A：四行视口打通第一笔 + 私注跨屏遮盖 + 只含丙的第二张")
    status, rec = req("POST", "/recordings", {
        "name": "基准会话",
        "viewport_rows": 4,
        "scroll_range": [2, 2],          # 固定往下两行
        "screens": [
            {"header": "售后App吸顶栏", "body": ["甲", "乙", "收据七", "私注七"]},
            {"header": "售后App吸顶栏", "body": ["收据七", "私注七", "丙", "丁"]},
        ],
    })
    expect(status == 201, "录屏创建成功")
    rid = rec["recording_id"]
    tr = rec["analysis"]["transitions"][0]
    expect(tr["surviving_candidates"] == [2], "唯一对齐 d=2，转缝确定")
    expect(cell(rec, "s0p2")["record_id"] == cell(rec, "s1p0")["record_id"],
           "两屏的收据七被识别为同一记录")
    expect(cell(rec, "s0p3")["record_id"] == cell(rec, "s1p1")["record_id"],
           "两屏的私注七被识别为同一记录")
    expect(cell(rec, "s1p2")["text"] == "丙" and cell(rec, "s1p2")["certain"],
           "丙在第二屏身份确定")

    # 客户在具体一屏点选具体行（保住具体行，而不是按文字全局匹配）
    req("POST", f"/recordings/{rid}/select", {"cell_id": "s0p2", "role": "voucher"})
    req("POST", f"/recordings/{rid}/select", {"cell_id": "s0p3", "role": "note"})
    status, rec = req("GET", f"/recordings/{rid}")
    expect(cell(rec, "s1p0")["role"] == "voucher", "凭证角色沿同一记录传播到下一屏")
    expect(cell(rec, "s1p1")["role"] == "note", "私注角色沿同一记录传播到下一屏")

    status, share = req("POST", f"/recordings/{rid}/shares", {})
    expect(status == 201, "第一张分屏凭条出具成功")
    p0, p1 = page_of(share, 0), page_of(share, 1)
    expect(item(p0, "s0p2")["masked"] is False and item(p0, "s0p2").get("certified"),
           "屏一收据七对收件人可见且已认证")
    expect(item(p0, "s0p3")["masked"] and item(p0, "s0p3")["mask_reason"] == "private_note",
           "屏一私注七遮盖")
    expect(item(p1, "s1p0")["masked"] is False, "屏二重复出现的收据七仍可读（同一笔凭证）")
    expect(item(p1, "s1p1")["masked"], "屏二重复出现的同一行私注七也遮盖，不漏")
    expect(item(p1, "s1p2")["masked"] is False and item(p1, "s1p2")["text"] == "丙",
           "无争议的正文丙保持可见，没有整图涂黑")
    expect(all(pg["header"] == "售后App吸顶栏" for pg in share["pages"]),
           "吸顶栏随分屏保留但不参与对齐认证")

    # 收件人凭分享号+口令实际打开
    status, opened = req("GET", f"/shares/{share['share_id']}?token={share['token']}")
    expect(status == 200, "收件人用分享号+口令能实际打开")
    leaked = [x for pg in opened["pages"] for x in pg["cells"]
              if not x["masked"] and x["role"] == "note"]
    expect(not leaked, "打开的内容里不存在未遮盖私注")

    # 再申领只含丙的第二张凭条
    status, claim = req("POST", f"/shares/{share['share_id']}/claim",
                        {"token": share["token"], "cell_id": "s1p2"})
    expect(status == 201, "只含丙的第二张凭条申领成功")
    expect(len(claim["pages"]) == 1 and len(claim["pages"][0]["cells"]) == 1
           and claim["pages"][0]["cells"][0]["text"] == "丙",
           "第二张凭条只有丙一行，没有把别的行涂黑凑页")
    expect(claim["parent"] == share["share_id"], "第二张凭条引用来源分享")

    # 申领私注必须被拒
    status, err = req("POST", f"/shares/{share['share_id']}/claim",
                      {"token": share["token"], "cell_id": "s1p1"})
    expect(status == 409 and err["error"] == "REFUSE_PRIVATE_CLAIM",
           "私人备注不能被申领为对外凭条")

    return share, claim


# --------------------------------------------------------------------------- #
def scenario_b():
    print("场景 B：补拍证据证明第二屏同字收据是另一处记录，禁止签成第一笔；再缩权")
    # 与基准完全同屏、同滚动证据，只有补拍记号不同
    status, rec = req("POST", "/recordings", {
        "name": "补拍会话",
        "viewport_rows": 4,
        "scroll_range": [2, 2],
        "screens": [
            {"header": "售后App吸顶栏", "body": ["甲", "乙", "收据七", "私注七"]},
            {"header": "售后App吸顶栏", "body": ["收据七", "私注七", "戊", "己"]},
        ],
    })
    rid = rec["recording_id"]
    req("POST", f"/recordings/{rid}/select", {"cell_id": "s0p2", "role": "voucher"})
    req("POST", f"/recordings/{rid}/select", {"cell_id": "s0p3", "role": "note"})

    # 补拍定位记号：屏一的收据七叫 RECORD-7，屏二同字位置叫 RECORD-7B（另一处记录）
    status, rec = req("POST", f"/recordings/{rid}/marks", {"marks": [
        {"screen": 0, "slot": 2, "record": "RECORD-7"},
        {"screen": 1, "slot": 0, "record": "RECORD-7B"},
    ]})
    expect(status == 200, "补拍记号登记成功")
    tr = rec["analysis"]["transitions"][0]
    expect(tr["broken"] and tr["surviving_candidates"] == [],
           "记号与 d=2 拼接冲突，该拼接被判为不确定/断缝，不能发")
    expect(cell(rec, "s0p2")["record_id"] == "RECORD-7",
           "第一笔收据七仍有明确定位（客户先前选过的具体行保住）")
    expect(cell(rec, "s1p0")["record_id"] is None or cell(rec, "s1p0")["record_id"] != "RECORD-7",
           "第二屏同字收据没有被签成第一笔 RECORD-7")

    # 试图把两屏一起发、且要求认证第一笔 => 必须拒绝
    status, err = req("POST", f"/recordings/{rid}/shares", {})
    expect(status == 409 and err["error"] == "UNCERTAIN_STITCH_CANNOT_SEND",
           "拼接不确定时服务端拒绝出具，而不是硬签或涂黑")

    # 只发确定的第一屏可以
    status, share1 = req("POST", f"/recordings/{rid}/shares",
                         {"pages": [0], "certify": ["RECORD-7"]})
    expect(status == 201 and item(page_of(share1, 0), "s0p2")["masked"] is False,
           "确定的第一屏仍可出具，第一笔收据七可领")
    expect(item(page_of(share1, 0), "s0p3")["masked"], "第一屏私注七遮盖")

    # 保持屏和定位不动，只缩小该收件人的授权范围：连收据七也不得出现
    status, share2 = req("POST", f"/recordings/{rid}/shares",
                         {"pages": [0], "entitlements": [], "certify": []})
    expect(status == 201, "缩小授权后的新分享仍可出具（有公开正文甲、乙可读）")
    s2p0 = page_of(share2, 0)
    expect(item(s2p0, "s0p2")["masked"] and "收据七" not in json.dumps(s2p0, ensure_ascii=False),
           "授权缩小后收据七在新分享中完全不出现")
    expect(item(s2p0, "s0p0")["masked"] is False, "普通正文甲仍可见，未整页涂黑")
    expect(item(s2p0, "s0p3")["masked"], "私注七依旧遮盖")

    # 旧分享的授权不受影响（缩小范围只作用于新分享）
    status, opened = req("GET", f"/shares/{share1['share_id']}?token={share1['token']}")
    expect(status == 200 and item(page_of(opened, 0), "s0p2")["masked"] is False,
           "先前出具的分享号仍能打开且授权不变")


# --------------------------------------------------------------------------- #
def scenario_c():
    print("场景 C：完全重复正文造成两种可能对齐，补拍后才能定")
    status, rec = req("POST", "/recordings", {
        "name": "重复正文会话",
        "viewport_rows": 4,
        "scroll_range": [1, 2],
        "screens": [
            {"header": "栏", "body": ["R1", "R1", "R1", "R1"]},
            {"header": "栏", "body": ["R1", "R1", "R1", "R1"]},
        ],
    })
    rid = rec["recording_id"]
    tr = rec["analysis"]["transitions"][0]
    expect(tr["text_consistent_candidates"] == [1, 2],
           "文字层存在 d=1 与 d=2 两个候选对齐")
    expect(all(not c["certain"] for c in rec["analysis"]["cells"] if c["screen"] == 1),
           "第二屏各格在两种对齐下身份不同，均判为不确定")

    req("POST", f"/recordings/{rid}/select", {"cell_id": "s0p3", "role": "voucher"})
    # 要求两屏一起出具并认证该笔 => 第二屏身份不确定，拒绝
    status, err = req("POST", f"/recordings/{rid}/shares",
                      {"pages": [0, 1], "certify": ["P3"]})
    expect(status == 409, "两种对齐并存时不能把不确定的重叠区签成凭证")

    # 只发确定的第一屏可以
    status, share0 = req("POST", f"/recordings/{rid}/shares", {"pages": [0]})
    expect(status == 201, "无歧义的第一屏可以单独出具")

    # 补拍一个定位记号：第二屏第 0 格就是全局第 3 行 => 唯一支持 d=3？范围只到 2，
    # 因此改用记号把第二屏第 0 格锚到全局第 2 行，唯一支持 d=2。
    status, rec = req("POST", f"/recordings/{rid}/marks", {"marks": [
        {"screen": 1, "slot": 0, "record": "P2"},
    ]})
    tr = rec["analysis"]["transitions"][0]
    expect(tr["surviving_candidates"] == [2], "补拍记号后只剩 d=2 一种对齐")
    expect(cell(rec, "s1p0")["certain"] and cell(rec, "s1p0")["record_id"] == "P2",
           "第二屏首格身份被明确定位")

    status, share1 = req("POST", f"/recordings/{rid}/shares",
                         {"entitlements": ["P2", "P3"], "certify": ["P2", "P3"]})
    expect(status == 201, "定位后两屏凭条可出具")
    expect(item(page_of(share1, 1), "s1p0")["masked"] is False
           and item(page_of(share1, 1), "s1p0")["record_id"] == "P2",
           "确定后的格对授权收件人可见，且身份正是记号锚定的 P2")


# --------------------------------------------------------------------------- #
def scenario_d():
    print("场景 D：滚动截屏里“周一”出现三次，是三笔不同记录")
    # d=2 固定滚动：每个“周一”落在不同全局位置
    status, rec = req("POST", "/recordings", {
        "name": "三个周一",
        "viewport_rows": 4,
        "scroll_range": [2, 2],
        "screens": [
            {"header": "栏", "body": ["周一", "周二", "周三", "周四"]},
            {"header": "栏", "body": ["周三", "周四", "周一", "周五"]},
            {"header": "栏", "body": ["周六", "周日", "周一", "月末"]},
        ],
    })
    rid = rec["recording_id"]
    mondays = [c for c in rec["analysis"]["cells"] if c["text"] == "周一"]
    expect(len(mondays) == 3, "正文里“周一”共出现三次")
    ids = {c["record_id"] for c in mondays}
    expect(len(ids) == 3, f"三个周一对应三个不同记录身份：{sorted(ids)}")
    # 三次出现各自落在不同全局位置，没有任何两格被并成同一记录
    expect(all(c["certain"] for c in mondays), "三次周一身份都可确定，互不混淆")

    # 顾客点选的是第二屏那笔（s1p2），授权只能给到它的记录
    req("POST", f"/recordings/{rid}/select", {"cell_id": "s1p2", "role": "voucher"})

    # 三屏之间是文字断缝（重叠区对不上），跨断缝的整本分享必须拒绝
    status, err = req("POST", f"/recordings/{rid}/shares", {"pages": [0, 1, 2]})
    expect(status == 409 and err["error"] == "UNCERTAIN_STITCH_CANNOT_SEND",
           "跨断缝不允许出在同一份分享里，杜绝把别处同字订单签成这一笔")

    # 只发确定的第二屏：那笔周一可见且认证
    status, share = req("POST", f"/recordings/{rid}/shares", {"pages": [1]})
    expect(status == 201, "含目标订单的确定单屏可出具")
    visible_mondays = [
        (pg["source_screen"], x["cell_id"])
        for pg in share["pages"] for x in pg["cells"]
        if x.get("text") == "周一" and not x["masked"]
    ]
    expect(visible_mondays == [(1, "s1p2")],
           "客户分享出去的只是第二屏那笔周一订单")
    expect(item(page_of(share, 1), "s1p2").get("certified") and
           item(page_of(share, 1), "s1p2")["record_id"] == "s1P2",
           "认证依据是记录身份 s1P2，而不是“周一”这两个字")

    # 另外两处同字“周一”所在屏单独出具时，默认不会获得该收件人的授权
    status, other = req("POST", f"/recordings/{rid}/shares",
                        {"pages": [0], "entitlements": [], "certify": []})
    expect(status == 201, "第一屏可单独出具")
    other_item = item(page_of(other, 0), "s0p0")
    expect(other_item["masked"] and other_item["mask_reason"] == "not_authorized",
           "第一屏的同名“周一”虽被识别为凭证成分，但记录身份不同且未授权，保持遮盖而非误签")
    expect("周一" not in json.dumps(other["pages"], ensure_ascii=False),
           "未授权的同名订单文字不出现在该收件人页面里")


# --------------------------------------------------------------------------- #
def scenario_e(share_a):
    print("场景 E：收件人侧安全边界")
    sid, token = share_a["share_id"], share_a["token"]

    status, _ = req("GET", f"/shares/{sid}")
    expect(status == 403, "无口令不能领取")
    status, _ = req("GET", f"/shares/{sid}?token=00000000-0000-0000-0000-000000000000")
    expect(status == 403, "错口令不能领取")
    status, _ = req("GET", f"/shares/nope?token={token}")
    expect(status == 404, "不存在的分享号返回 404")

    # 收件人接口的任何响应里都不能出现制作台原屏结构与私注原文
    status, opened = req("GET", f"/shares/{sid}?token={token}")
    raw = json.dumps(opened, ensure_ascii=False)
    expect("analysis" not in raw and "selections" not in raw and "screens" not in raw,
           "收件人响应不含制作台原屏/分析/点选结构")
    expect("私注七" not in raw, "私注原文不出现在收件人可见内容中")

    # 整页涂黑被服务端拒绝：构造一页四格全需遮盖的录屏
    status, rec = req("POST", "/recordings", {
        "name": "全黑尝试",
        "viewport_rows": 4,
        "scroll_range": [2, 2],
        "screens": [{"header": "栏", "body": ["私注七", "私注七", "私注七", "私注七"]}],
    })
    rid = rec["recording_id"]
    req("POST", f"/recordings/{rid}/select", {"cell_id": "s0p0", "role": "note"})
    status, err = req("POST", f"/recordings/{rid}/shares",
                      {"entitlements": [], "certify": []})
    expect(status == 409 and err["error"] == "REFUSE_FULL_BLACKOUT",
           "整张图涂黑交差被服务端拒绝")


# --------------------------------------------------------------------------- #
def main():
    with tempfile.TemporaryDirectory() as tmp:
        data_file = os.path.join(tmp, "lshot.json")
        proc = start_server(data_file)
        try:
            share_a, claim_a = scenario_a()
            scenario_b()
            scenario_c()
            scenario_d()
            scenario_e(share_a)

            print("\n重启服务，复查分享号在重启后仍可打开 ...")
        finally:
            stop_server(proc)

        proc = start_server(data_file)
        try:
            status, reopened = req("GET", f"/shares/{share_a['share_id']}?token={share_a['token']}")
            expect(status == 200, "重启后第一张凭条的分享号仍能打开")
            expect(len(reopened["pages"]) == 2, "重启后页面顺序与内容完整保留")
            status, reclaimed = req("GET", f"/shares/{claim_a['share_id']}?token={claim_a['token']}")
            expect(status == 200 and reclaimed["pages"][0]["cells"][0]["text"] == "丙",
                   "重启后只含丙的第二张凭条也能打开")
            status, listing = req("GET", "/recordings")
            expect(status == 200 and len(listing) >= 4, "重启后录屏记录仍在")
        finally:
            stop_server(proc)

    print("\n全部场景通过。")


if __name__ == "__main__":
    main()
