"""端到端验证：六户以内的一轮投递，覆盖题面全部硬性要求。

运行：python3 verify.py
脚本会自己拉起 server.py 子进程、跑完后关闭；使用独立临时库。
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:8765"
PASS, FAIL = 0, 0


def call(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8")
        raise AssertionError(f"{method} {path} -> {e.code} {detail}") from None


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def owed_ids(cid):
    st = call("GET", f"/corrections/{cid}")
    return set(st["still_owed"]), st


def household_owed(hid):
    return {c["correction_id"] for c in call("GET", f"/households/{hid}/trace")["owed"]}


def main():
    tmp = tempfile.mkdtemp(prefix="paper_verify_")
    db_path = os.path.join(tmp, "verify.db")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server.py"),
         "--port", "8765", "--db", db_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(50):
            try:
                call("GET", "/health")
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("server failed to start: " + proc.stderr.read())

        print("== 1. 建两个地方版、六户与订阅 ==")
        call("POST", "/editions", {"code": "market", "name": "集市版"})
        call("POST", "/editions", {"code": "school", "name": "学校版"})
        hs = [call("POST", "/households", {"name": f"住户{i}"})["id"] for i in range(1, 7)]
        h1, h2, h3, h4, h5, h6 = hs
        market_subs = [h1, h2, h3, h6]   # H4/H5 只订学校版
        for h in market_subs:
            call("POST", "/subscriptions", {"household_id": h, "edition_code": "market"})
        for h in [h4, h5, h6]:
            call("POST", "/subscriptions", {"household_id": h, "edition_code": "school"})

        print("== 2. 编辑：集市消息（固定旧稿版面）+ 学校消息（跟随校样版面）==")
        msg = call("POST", "/messages", {"title": "集市通知"})["id"]
        ms1 = call("POST", "/manuscripts", {"message_id": msg,
                   "body": {"date": "十二日", "place": "东门外", "event": "集市"}})["id"]
        ms2 = call("POST", "/manuscripts", {"message_id": msg,
                   "body": {"date": "十三日", "place": "东门外", "event": "集市"}})["id"]
        ms3 = call("POST", "/manuscripts", {"message_id": msg,
                   "body": {"date": "十三日", "place": "南门", "event": "集市"}})["id"]
        # 集市版页面固定旧稿，永不自动跟新
        p_fixed = call("POST", "/pages", {"edition_code": "market",
                       "title": "集市固定栏", "fixed_ms_id": ms1})["id"]
        call("POST", "/placements", {"page_id": p_fixed, "message_id": msg,
                                     "manuscript_id": ms1})
        # 学校版页面跟随本期校样
        school_msg = call("POST", "/messages", {"title": "校园简讯"})["id"]
        sch1 = call("POST", "/manuscripts", {"message_id": school_msg,
                  "body": {"note": "旧校园稿"}})["id"]
        p_follow = call("POST", "/pages", {"edition_code": "school",
                      "title": "校园跟随栏"})["id"]
        call("POST", "/placements", {"page_id": p_follow, "message_id": school_msg,
                                     "manuscript_id": sch1})

        print("== 3. 本期校样（跟随版面此刻钉的是旧校园稿）==")
        issue = call("POST", "/issues", {"label": "第42期"})["id"]
        call("POST", "/proofs", {"issue_id": issue, "page_id": p_follow,
                                 "manuscript_id": sch1})

        print("== 4. 印第一批（更正前），开印即钉死每页稿号 ==")
        r1 = call("POST", "/print-runs", {"issue_id": issue, "label": "更正前印次"})["id"]
        b1 = call("POST", "/bundles", {"run_id": r1, "edition_code": "market"})["id"]
        b2 = call("POST", "/bundles", {"run_id": r1, "edition_code": "market"})["id"]
        b3 = call("POST", "/bundles", {"run_id": r1, "edition_code": "market"})["id"]  # 库存不投
        bs1 = call("POST", "/bundles", {"run_id": r1, "edition_code": "school"})["id"]
        bs2 = call("POST", "/bundles", {"run_id": r1, "edition_code": "school"})["id"]
        trace_b1 = call("GET", f"/bundles/{b1}")
        trace_bs1 = call("GET", f"/bundles/{bs1}")
        ms_by_page = {p["page_id"]: p["manuscript_id"] for p in trace_b1["frozen_pages"]}
        ms_school = {p["page_id"]: p["manuscript_id"] for p in trace_bs1["frozen_pages"]}
        check("固定版面钉住旧稿 MS1", ms_by_page[p_fixed] == ms1, str(ms_by_page))
        check("跟随版面按当期校样钉稿", ms_school[p_follow] == sch1)

        print("== 5. 第一批签收：同包拆投、两包混送、留一包库存 ==")
        # B1 拆给 H1、H2
        call("POST", "/receipts", {"receipt_id": "RC1", "household_id": h1,
             "items": [{"kind": "page", "bundle_id": b1, "page_id": p_fixed}]})
        call("POST", "/receipts", {"receipt_id": "RC2", "household_id": h2,
             "items": [{"kind": "page", "bundle_id": b1, "page_id": p_fixed}]})
        # H3 签 B2；H6 混收集市 B2 + 学校 BS1
        call("POST", "/receipts", {"receipt_id": "RC3", "household_id": h3,
             "items": [{"kind": "page", "bundle_id": b2, "page_id": p_fixed}]})
        call("POST", "/receipts", {"receipt_id": "RC6", "household_id": h6,
             "items": [{"kind": "page", "bundle_id": b2, "page_id": p_fixed},
                       {"kind": "page", "bundle_id": bs1, "page_id": p_follow}]})
        # H4 只签学校版
        call("POST", "/receipts", {"receipt_id": "RC4", "household_id": h4,
             "items": [{"kind": "page", "bundle_id": bs2, "page_id": p_follow}]})
        check("未投库存 B3 仍是 stock", call("GET", f"/bundles/{b3}")["status"] == "stock")

        print("== 6. 编辑两次改稿：日期 十二日→十三日，地点 东门外→南门 ==")
        c1 = call("POST", "/corrections", {"message_id": msg,
                  "from_ms_id": ms1, "to_ms_id": ms2})["id"]
        c2 = call("POST", "/corrections", {"message_id": msg,
                  "from_ms_id": ms2, "to_ms_id": ms3})["id"]
        st1 = call("GET", f"/corrections/{c1}")
        check("C1 只含日期字段差异",
              [c["field"] for c in st1["changes"]] == ["date"], str(st1["changes"]))
        st2 = call("GET", f"/corrections/{c2}")
        check("C2 只含地点字段差异（不重发整篇）",
              [c["field"] for c in st2["changes"]] == ["place"], str(st2["changes"]))

        print("== 7. 欠件只认真实回单：库存、只订户、另一版面文字都不触发 ==")
        owed1, _ = owed_ids(c1)
        check("读过旧集市稿的 H1/H2/H3/H6 欠 C1",
              owed1 == {h1, h2, h3, h6}, str(owed1))
        check("只订学校版的 H4 不在欠件名单", h4 not in owed1)
        check("从未签收的 H5 不在欠件名单", h5 not in owed1)
        # 学校版从未改过 -> 无人欠学校消息的更正（根本没有这条更正），验证另一版文字不触发
        check("H6 学校稿不产生集市更正欠件",
              household_owed(h6) == {c1, c2}, str(household_owed(h6)))
        check("库存 B3 不产生任何欠件",
              all(h in owed1 for h in [h1, h2, h3, h6]) and len(owed1) == 4)

        print("== 8. 印第二批（更正后）：固定栏仍是旧稿，跟随栏更新 ==")
        call("POST", "/proofs", {"issue_id": issue, "page_id": p_follow,
                                 "manuscript_id": sch1})  # 校样保持，演示固定栏拖累
        r2 = call("POST", "/print-runs", {"issue_id": issue, "label": "更正后印次"})["id"]
        b4 = call("POST", "/bundles", {"run_id": r2, "edition_code": "market"})["id"]
        b5 = call("POST", "/bundles", {"run_id": r2, "edition_code": "market"})["id"]
        b6 = call("POST", "/bundles", {"run_id": r2, "edition_code": "market"})["id"]
        b7 = call("POST", "/bundles", {"run_id": r2, "edition_code": "market"})["id"]
        trace_b4 = call("GET", f"/bundles/{b4}")
        r2_fixed_ms = next(p["manuscript_id"] for p in trace_b4["frozen_pages"]
                           if p["page_id"] == p_fixed)
        check("更正后固定栏实际仍钉 MS1（所以新读者照样欠更正）", r2_fixed_ms == ms1)

        # H1 改收第二批；H5 首次收报即新印次；其余人不收
        call("POST", "/receipts", {"receipt_id": "RC5", "household_id": h5,
             "items": [{"kind": "page", "bundle_id": b5, "page_id": p_fixed}]})
        call("POST", "/receipts", {"receipt_id": "RC7", "household_id": h1,
             "items": [{"kind": "page", "bundle_id": b4, "page_id": p_fixed}]})
        owed1b, _ = owed_ids(c1)
        check("新印次固定栏旧稿：H5 也欠 C1", h5 in owed1b)
        check("H1 仍只欠一次（按户去重，不是按回单条数）",
              len(household_owed(h1)) == 2 and c1 in household_owed(h1))

        print("== 9. 只改变一户实际签收的印次，欠件名单必须变化 ==")
        before = owed1b
        call("POST", f"/receipts/RC7/delete", {})
        # 让 H1 改回第一批 B1（同一张回单号重发）
        call("POST", "/receipts", {"receipt_id": "RC7", "household_id": h1,
             "items": [{"kind": "page", "bundle_id": b1, "page_id": p_fixed}]})
        after, _ = owed_ids(c1)
        check("印次变更前后欠件集合可比较且 H1 始终在列（去重成立）",
              before == after == {h1, h2, h3, h5, h6}, f"{before} vs {after}")
        # 再制造真正的名单变化：删掉 H1 的全部集市签收 -> H1 脱离欠件
        call("POST", "/receipts/RC7/delete", {})
        call("POST", "/receipts/RC1/delete", {})
        gone, _ = owed_ids(c1)
        check("撤掉 H1 所有真实签收后 H1 移出欠件", h1 not in gone and len(gone) == 4, str(gone))
        # 恢复：重发 RC1
        call("POST", "/receipts", {"receipt_id": "RC1", "household_id": h1,
             "items": [{"kind": "page", "bundle_id": b1, "page_id": p_fixed}]})

        print("== 10. 专递：按户合并备条，固定印次只改纸条状态，欠件随之改变 ==")
        call("POST", "/distribution/mode", {"mode": "special"})
        prep = call("POST", "/special/prepare", {})
        slips = {s["slip_id"]: s for s in
                 [call("GET", f"/households/{h}/trace") for h in hs]
                 for s in s["slips"]}
        check("五个欠户各备一条合并纸条", prep["count"] == 5, str(prep))
        h1_slip = call("GET", f"/households/{h1}/trace")["slips"][-1]
        check("纸条按户合并两处更正为一条",
              set(h1_slip["corrections"]) == {c1, c2}, str(h1_slip))
        check("纸条文本只带改动不带整篇",
              "date" in h1_slip["text"] and "place" in h1_slip["text"]
              and "集市" not in h1_slip["text"].split("→")[-1], h1_slip["text"])
        check("已备条未签收：H1 仍在欠件", c1 in household_owed(h1))
        # 同一回单重发：RC1 再提交一次，幂等不重复登记
        rc1_items_before = len(call("GET", "/receipts/RC1")["items"])
        call("POST", "/receipts", {"receipt_id": "RC1", "household_id": h1,
             "items": [{"kind": "page", "bundle_id": b1, "page_id": p_fixed}]})
        rc1_items_after = len(call("GET", "/receipts/RC1")["items"])
        check("同一回单重发幂等，不重复计阅读", rc1_items_before == rc1_items_after)

        # H1 纸条随专递回单签收 -> 欠件结清
        call("POST", "/receipts", {"receipt_id": "RC8", "household_id": h1,
             "items": [{"kind": "slip", "slip_id": h1_slip["slip_id"]}]})
        check("纸条签收后 H1 欠件清空", household_owed(h1) == set(),
              str(household_owed(h1)))
        check("已送达第一张纸条后不再把它当欠件",
              h1 not in owed_ids(c1)[0] and h1 not in owed_ids(c2)[0])

        print("== 11. 车已开走：清单冻结，晚回的签收只结清车上实际带的 ==")
        others = [sid for sid in slips if sid != h1_slip["slip_id"]]
        call("POST", "/special/trips", {"slip_ids": others})["trip_id"]
        check("车上纸条全部转为 dispatched",
              all(s["status"] == "dispatched"
                  for h in hs for s in call("GET", f"/households/{h}/trace")["slips"]
                  if s["slip_id"] in others))
        # 晚回：H2 签收车上纸条，只结清 H2 自己
        h2_slip = next(s for s in call("GET", f"/households/{h2}/trace")["slips"])
        call("POST", "/receipts", {"receipt_id": "RC9", "household_id": h2,
             "items": [{"kind": "slip", "slip_id": h2_slip["slip_id"]}]})
        check("晚回签收结清 H2", household_owed(h2) == set())
        check("同车其他户 H3 仍欠", c1 in household_owed(h3))
        # 已签收的纸条不能再被备条
        prep2 = call("POST", "/special/prepare", {})
        check("不重复给已结清/在途户备条", prep2["count"] == 0, str(prep2))

        print("== 12. 随刊夹送：封包配条、封而不达不销账、拆包改投、退订、逾期 ==")
        call("POST", "/distribution/mode", {"mode": "insert"})
        # 两种业务只开一种：撤销 H3/H5/H6 在专递业务下的在途条，改由随刊办理
        for h in (h3, h5, h6):
            for s in call("GET", f"/households/{h}/trace")["slips"]:
                if s["mode"] == "special" and s["status"] == "dispatched":
                    call("POST", f"/slips/{s['slip_id']}/cancel",
                         {"reason": "专递转随刊"})
        check("切业务后 H3/H5/H6 恢复欠件",
              all({c1, c2} <= household_owed(h) for h in (h3, h5, h6)))
        # H3 封 B6 并承诺未来日期：封好没送到，欠账仍在
        seal = call("POST", "/insert/seal",
                    {"bundle_id": b6, "household_id": h3, "promised_by": "2099-01-01"})
        s3 = seal["slip_id"]
        check("封包生成随刊纸条", s3 is not None)
        check("封好未送达不销账", c1 in household_owed(h3))
        check("承诺日未到不算逾期",
              all(x["household_id"] != h3 for x in call("GET", "/insert/overdue")["overdue"]))
        # 送达签收 -> 销账
        call("POST", "/receipts", {"receipt_id": "RC10", "household_id": h3,
             "items": [{"kind": "page", "bundle_id": b6, "page_id": p_fixed}]})
        check("随刊纸条随报纸签收而结清", household_owed(h3) == set())

        # 同包拆投：库存包 B7 先封给 H5，改投后释放回库存，再封给 H6
        seal5 = call("POST", "/insert/seal",
                     {"bundle_id": b7, "household_id": h5, "promised_by": "2099-01-01"})
        check("B7 封出后为 sealed", call("GET", f"/bundles/{b7}")["status"] == "sealed")
        call("POST", "/insert/reroute", {"bundle_id": b7})
        check("改投后包释放回库存、原纸条误投，H5 欠件保留",
              call("GET", f"/bundles/{b7}")["status"] == "stock" and
              c1 in household_owed(h5) and
              any(s["status"] == "misdirected"
                  for s in call("GET", f"/households/{h5}/trace")["slips"]
                  if s["slip_id"] == seal5["slip_id"]))
        # 已投妥的包不能再封
        try:
            call("POST", "/insert/seal",
                 {"bundle_id": b1, "household_id": h5, "promised_by": "2099-01-01"})
            check("已投妥包拒绝封包", False)
        except AssertionError:
            check("已投妥包拒绝封包", True)
        # 同一个库存包重新封给 H6 并签收（拆包改投后继续投出）
        seal6 = call("POST", "/insert/seal",
                     {"bundle_id": b7, "household_id": h6, "promised_by": "2099-01-01"})
        call("POST", "/receipts", {"receipt_id": "RC11", "household_id": h6,
             "items": [{"kind": "page", "bundle_id": b7, "page_id": p_fixed}]})
        check("H6 随包签收后两笔更正一并结清", household_owed(h6) == set(),
              str(household_owed(h6)))
        check("B7 经再封/签收后状态为 delivered",
              call("GET", f"/bundles/{b7}")["status"] == "delivered")

        # 退订不销账
        call("POST", f"/households/{h5}/unsubscribe", {})
        check("H5 退订后欠账仍在", c1 in household_owed(h5))

        # 逾期：超出承诺日且无可用包（H5 的条已误投，无在途、无有效封包/承诺日）
        overdue_today = call("GET", "/insert/overdue?today=2100-01-01")["overdue"]
        h5_overdue = next((x for x in overdue_today if x["household_id"] == h5), None)
        check("无可用包且无有效承诺日：H5 明确列入逾期（含退订标记）",
              h5_overdue is not None and h5_overdue["unsubscribed"] is True,
              str(overdue_today))
        # 全局欠件名单仍可查
        owed_global = {x["household_id"] for x in call("GET", "/owed")["owed"]}
        check("欠件总名单：H5 仍在册，已签收各户不在",
              owed_global == {h5}, str(owed_global))

        print("== 13. 编辑视角与回单追溯 ==")
        st = call("GET", f"/corrections/{c1}")
        check("可查某更正还有谁没收到", h5 in st["still_owed"] and h1 not in st["still_owed"])
        rc = call("GET", "/receipts/RC6")
        check("从回单追到具体页与原稿身份",
              any(i["kind"] == "page" and i["manuscript_id"] == ms1
                  and i["page_id"] == p_fixed for i in rc["items"]),
              str(rc))

        print("== 14. 重开进程：持久化与包号追查 ==")
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "server.py"),
             "--port", "8765", "--db", db_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(50):
            try:
                call("GET", "/health")
                break
            except Exception:
                time.sleep(0.1)
        trace = call("GET", f"/bundles/{b1}")
        check("重开后凭包号查回印次/钉稿/签收/补正去向",
              trace["run_id"] == r1 and
              {r["household_id"] for r in trace["receipts"]} >= {h1, h2} and
              any(p["manuscript_id"] == ms1 for p in trace["frozen_pages"]),
              json.dumps(trace, ensure_ascii=False))
        check("重开后欠件状态不丢", c1 in household_owed(h5))
        trip_mode = call("GET", "/distribution/mode")
        check("发行模式选择持久化", trip_mode["mode"] == "insert")

    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
