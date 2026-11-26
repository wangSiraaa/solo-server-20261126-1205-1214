#!/usr/bin/env python3
"""本地端到端演示：开约、街讯、报开、欠轮/补集、墙隔、重复回单、跨月与重启。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

from lunshi.scenario import CATEGORY_LABELS, CONVENTION, DAYS, seed_world
from lunshi.service import ApiError, LunshiService

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "data/lunshi.sqlite3"


def bar(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def mini(title: str) -> None:
    print(f"\n-- {title} " + "-" * 60)


def say(payload) -> None:
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


def reset_db(path: str) -> None:
    if path != ":memory:" and os.path.exists(path):
        os.remove(path)
    for suffix in ("-wal", "-shm"):
        p = Path(path + suffix)
        if p.exists():
            p.unlink()


def issue_and_open(svc: LunshiService, app_id: str, vendor: str, township: str, day: int,
                   category: str, spot: str, receipt_no: str, convention: str = CONVENTION):
    app = svc.submit_application(
        app_id, vendor, township, day, category, convention_id=convention
    )
    permit = svc.issue_permit(app["id"], spot)
    receipt = svc.report_open(receipt_no, permit["permit_no"])
    return app, permit, receipt


def prepared_a_world(path: str, issue_b_pair: bool = True):
    reset_db(path)
    svc = LunshiService(path)
    seed_world(svc)
    svc.call_next(CONVENTION, day_no=1)
    _, pa, ra = issue_and_open(svc, "APP-A-BOOK", "V-WEN", "A", 1, "book", "A1", "RCPT-A-BOOK")
    _, pu, ru = issue_and_open(svc, "APP-A-UMB", "V-GU", "A", 1, "umbrella", "A2", "RCPT-A-UMB")
    expect(ru["settlement"]["status"] == "succeeded", "书摊、修伞摊同场真实报开，甲乡完成第 1 轮")
    bcall = svc.call_next(CONVENTION, day_no=2)
    svc.create_adjacent_pair(
        "GRP-B-PAIR", "B", 2,
        [
            {"vendor_id": "V-WEN", "category": "book", "application_id": "APP-B-BOOK"},
            {"vendor_id": "V-GU", "category": "umbrella", "application_id": "APP-B-UMB"},
        ],
        convention_id=CONVENTION,
    )
    issued = None
    if issue_b_pair:
        issued = svc.issue_group_permits("GRP-B-PAIR")
    return svc, bcall, issued


def happy_path() -> str:
    bar("一、开约与三乡申报")
    reset_db(DB_PATH)
    svc = LunshiService(DB_PATH)
    seed_world(svc)
    conv = svc.list_conventions()[0]
    say({
        "约名": conv["name"],
        "办法": "dynamic_call（现场召集 + 同一轮欠轮优先补集）",
        "轮次": [f"{i+1}.{tid}" for i, tid in enumerate(conv["cycle"])],
        "办成条件": conv["success_rule"],
        "补轮政策": conv["makeup_policy"],
    })
    expect(conv["cycle"] == ["A", "B", "C"], "约定甲、乙、丙依次轮流，首轮甲")

    bar("二、街坊看街讯：日序 1 跨到公历 11 月 30 日")
    news = svc.street_news(day_no=1)
    d1 = news["days"][0]
    say({"整数日序": d1["day_no"], "公历": d1["iso_date"],
         "今日集": [f"{x['township_name']}：{x['title']}（fair_open={x['fair_open']}）" for x in d1["sessions"]]})
    expect(d1["iso_date"] == "2026-11-30", "第 1 个墟日落在 11 月 30 日")

    bar("三、甲乡第一日：领摆位单、到场报开")
    call_a = svc.call_next(CONVENTION, day_no=1)
    expect(call_a["round_no"] == 1 and call_a["township_id"] == "A", "首轮召集甲乡")
    app_book = svc.submit_application("APP-A-BOOK", "V-WEN", "A", 1, "book", convention_id=CONVENTION)
    app_umb = svc.submit_application("APP-A-UMB", "V-GU", "A", 1, "umbrella", convention_id=CONVENTION)
    permit_book = svc.issue_permit(app_book["id"], "A1")
    permit_umb = svc.issue_permit(app_umb["id"], "A2")
    mini("有效摆位单，写明地点和邻摊")
    for p in (permit_book, permit_umb):
        print("  ·", p["placement_text"])
    r_book = svc.report_open("RCPT-A-BOOK", permit_book["permit_no"])
    r_umb = svc.report_open("RCPT-A-UMB", permit_umb["permit_no"])
    say(r_umb["settlement"])
    expect(r_umb["settlement"]["status"] == "succeeded", "书摊与修伞摊分别真实开张，第 1 轮由甲用掉")

    mini("重复回单：同一 receipt_no 再投一次")
    duplicate = svc.report_open("RCPT-A-BOOK", permit_book["permit_no"])
    expect(duplicate.get("duplicate") is True, "重复回单只返回原回执，不产生第二次开张")
    expect(len(svc.ledger(CONVENTION)["effective_rounds"]) == 1, "重复回单没有多记一轮")

    bar("四、甲真办成后，下一次合用场乙先：第二日相连双摊摆位单")
    call_b = svc.call_next(CONVENTION, from_day=2)
    expect(call_b["round_no"] == 2 and call_b["township_id"] == "B" and call_b["day_no"] == 2,
           "第 2 轮先召集乙乡，且难办的相连双摊就排在第 2 日，不推到末日")
    pair = svc.create_adjacent_pair(
        "GRP-B-PAIR", "B", 2,
        [
            {"vendor_id": "V-WEN", "category": "book", "application_id": "APP-B-BOOK", "stall_name": "文墨书摊"},
            {"vendor_id": "V-GU", "category": "umbrella", "application_id": "APP-B-UMB", "stall_name": "骨青修伞"},
        ],
        convention_id=CONVENTION,
    )
    issued = svc.issue_group_permits("GRP-B-PAIR")
    mini("连摊单/两张互为邻摊的摆位单")
    for p in issued["permits"]:
        full = svc.get_permit(p["permit_no"])
        print("  ·", full["permit_no"], full["placement_text"])
    expect(len(issued["permits"]) == 2, "乙乡 B1/B2 相连，原双摊领到有效连摊摆位单")

    bar("五、乙、丙真实报开，三乡各办成一轮")
    rb1 = svc.report_open("RCPT-B-BOOK", issued["permits"][0]["permit_no"])
    rb2 = svc.report_open("RCPT-B-UMB", issued["permits"][1]["permit_no"])
    expect(rb2["settlement"]["status"] == "succeeded" and rb2["settlement"]["usage"]["round_no"] == 2,
           "乙乡双摊报开后用掉第 2 轮")

    call_c = svc.call_next(CONVENTION, from_day=3)
    expect(call_c["round_no"] == 3 and call_c["township_id"] == "C" and call_c["day_no"] == 3,
           "第 3 轮轮到丙乡")
    _, _, rc2 = issue_and_open(svc, "APP-C-BOOK", "V-LAN", "C", 3, "book", "C1", "RCPT-C-BOOK")
    # The helper only opens one category; add umbrella separately for readable IDs.
    app_cu = svc.submit_application("APP-C-UMB", "V-JIANG", "C", 3, "umbrella", convention_id=CONVENTION)
    pc_u = svc.issue_permit(app_cu["id"], "C2")
    rc_u = svc.report_open("RCPT-C-UMB", pc_u["permit_no"])
    expect(rc_u["settlement"]["status"] == "succeeded", "丙乡书摊、修伞摊真实开张，用掉第 3 轮")

    effective = svc.ledger(CONVENTION)["effective_rounds"]
    say(effective)
    expect([(x["round_no"], x["township_id"]) for x in effective] == [(1, "A"), (2, "B"), (3, "C")],
           "三乡真办成一轮：R1=甲、R2=乙、R3=丙")

    bar("六、跨月查街讯与跨月重放回单")
    nov = svc.street_news(month="2026-11")
    dec = svc.street_news(month="2026-12")
    say({
        "2026-11": [d["day_no"] for d in nov["days"]],
        "2026-12": [d["day_no"] for d in dec["days"]],
    })
    expect([d["day_no"] for d in nov["days"]] == [1], "11 月只能查到第 1 日甲集")
    expect([d["day_no"] for d in dec["days"]] == [2, 3, 4, 5], "12 月可查到乙、丙及备用/补集日")
    replay = svc.report_open("RCPT-A-UMB", permit_umb["permit_no"], idempotency_key="external-carrier-msg-001")
    expect(replay.get("duplicate") is True, "跨月后重复回单仍被识别，轮次账不变")

    bar("七、重启服务/数据库后查轮次")
    del svc
    restarted = LunshiService(DB_PATH)
    ledger = restarted.ledger(CONVENTION)
    say(ledger["effective_rounds"])
    expect([(x["round_no"], x["township_id"]) for x in ledger["effective_rounds"]] == [(1, "A"), (2, "B"), (3, "C")],
           "重启后仍查清哪个乡有效用掉哪一轮")
    fetched = restarted.get_permit(issued["permits"][0]["permit_no"])
    expect("邻摊" in fetched["placement_text"], "重启后摆位单地点与邻摊仍可查")
    return DB_PATH


def withdrawal_demo() -> None:
    bar("八、故障演示一：仅把甲的修伞回执改为撤摊")
    path = os.path.join(tempfile.gettempdir(), "lunshi-withdrawal.sqlite3")
    svc, bcall, issued = prepared_a_world(path, issue_b_pair=True)

    result = svc.withdraw_receipt("RCPT-A-UMB", "雨落散场，修伞摊撤摊")
    expect(result["revalidated_usages"][0]["status"] == "reversed", "原 R1/甲 usage 保留痕迹但冲正，不再有效")
    ledger = svc.ledger(CONVENTION)
    expect(ledger["effective_rounds"] == [], "甲已办成不再是有效事实；乙不能凭旧状态当继任")
    expect(ledger["owed_rounds"][-1]["status"] == "owed", "甲的第 1 轮记为欠轮，保留再办机会")

    nxt = svc.call_next(CONVENTION, from_day=2)
    say({"下一次召集": {k: nxt[k] for k in ("round_no", "township_id", "kind", "day_no")}})
    expect((nxt["round_no"], nxt["township_id"], nxt["kind"], nxt["day_no"]) == (1, "A", "makeup", 4),
           "下一场先召集甲在第 4 日补集，而不是把乙记成第 2 轮继任")

    group = svc.get_group("GRP-B-PAIR")
    expect(len(group["permits"]) == 2 and group["applications"][0]["status"] == "permit_issued",
           "乙的场地、双摊报名和原摆位单均保持，不做删除")

    rb1 = svc.report_open("RCPT-B-BOOK", issued["permits"][0]["permit_no"])
    rb2 = svc.report_open("RCPT-B-UMB", issued["permits"][1]["permit_no"])
    say(rb2["settlement"])
    expect(rb2["settlement"]["status"] == "blocked"
           and rb2["settlement"]["code"] == "PREDECESSOR_NOT_COMPLETE",
           "即使乙摊真实开张，甲轮未有效前也不能抢走第 2 轮")

    issue_and_open(svc, "APP-MAKE-BOOK", "V-WEN", "A", 4, "book", "A1", "RCPT-MAKE-BOOK")
    _, _, rm_u = issue_and_open(svc, "APP-MAKE-UMB", "V-GU", "A", 4, "umbrella", "A2", "RCPT-MAKE-UMB")
    expect(rm_u["settlement"]["status"] == "succeeded", "甲第 4 日补集办成，新 R1/甲 usage 生效")
    b_result = svc.evaluate_call(bcall["id"])
    expect(b_result["status"] == "succeeded" and b_result["usage"]["round_no"] == 2,
           "前驱补齐后，乙的实际开张才可结算为第 2 轮；同一轮仍只有一条有效 usage")
    effective = svc.ledger(CONVENTION)["effective_rounds"]
    say(effective)
    expect([(x["round_no"], x["township_id"]) for x in effective] == [(1, "A"), (2, "B")],
           "冲正、欠轮、补集和乙继任链条均可审计")


def wall_demo() -> None:
    bar("九、故障演示二：恢复甲首轮，乙原两格被墙隔开")
    path = os.path.join(tempfile.gettempdir(), "lunshi-wall.sqlite3")
    svc, _, _ = prepared_a_world(path, issue_b_pair=False)
    svc.withdraw_receipt("RCPT-A-UMB", "先模拟回执更正")
    restored = svc.restore_receipt("RCPT-A-UMB", "查实为误报撤摊，恢复真实开张")
    expect(restored.get("usage", {}).get("status") == "effective", "恢复回执后，甲第 1 轮重新有效")

    svc.set_spots_linked("B1", "B2", False, "连夜砌墙")
    readiness = svc.venue_readiness("B", 2, 1, CONVENTION)
    say({
        "乙办集资格": readiness["qualified_to_host"],
        "原相邻双格可用": readiness["adjacent_required_pair_available"],
        "缺类": readiness["missing_categories"],
    })
    expect(readiness["qualified_to_host"] is True, "乙仍有办集资格")
    expect(readiness["adjacent_required_pair_available"] is False, "但 B1/B2 已不构成可相邻成组的两格")

    try:
        svc.issue_group_permits("GRP-B-PAIR", ["B1", "B2"])
        raise AssertionError("墙隔后不应开出连摊单")
    except ApiError as exc:
        say({"code": exc.code, "message": exc.message})
        expect(exc.code == "ADJACENCY_REQUIRED", "系统拒绝给原双摊开那张连摊单")
    expect(svc.get_group("GRP-B-PAIR")["permits"] == [], "失败签发保持原子性：没有产生半张连摊单或占格")


def conflict_loan_demo() -> None:
    bar("十、同时段赶场冲突、借地不重复许诺、转场不抢轮")
    path = os.path.join(tempfile.gettempdir(), "lunshi-loan.sqlite3")
    svc, _, _ = prepared_a_world(path, issue_b_pair=False)

    svc.declare_session("C", 1, 1, "2026-11-30", "丙乡同日临时集")
    app_c = svc.submit_application("APP-C-SLOT", "V-WEN", "C", 1, "book", convention_id=CONVENTION)
    permit_c = svc.issue_permit(app_c["id"], "C1")
    try:
        svc.report_open("RCPT-C-CONFLICT", permit_c["permit_no"])
        raise AssertionError("同一摊主同一时段不应能在 C 再报开")
    except ApiError as exc:
        expect(exc.code == "VENDOR_TIME_CONFLICT", "文墨虽在甲丙两地登记，同一时段只能在甲真实开张")

    svc.create_loan("LOAN-B9-A", "B9", "A", 4, 1, "甲乡借乙乡东坪分流")
    try:
        svc.create_loan("LOAN-B9-C", "B9", "C", 4, 1)
        raise AssertionError("同一借地不能许诺两次")
    except ApiError as exc:
        expect(exc.code == "LAND_ALREADY_PROMISED", "一块借地不会被排给两个乡")

    app_m = svc.submit_application("APP-A4-UMB", "V-GU", "A", 4, "umbrella", convention_id=CONVENTION)
    permit_m = svc.issue_permit(app_m["id"], "A2")
    transfer = svc.transfer_booking("APP-A4-UMB", "B9", "把一半摊迁到邻乡借地")
    moved_receipt = svc.report_open("RCPT-A4-UMB", transfer["new_permit"]["permit_no"])
    say(transfer["new_permit"]["placement_text"])
    expect(moved_receipt["host_township_id"] == "A" and moved_receipt["spot_township_id"] == "B",
           "转场回单保留主办乡甲、地皮乡乙两个归属")
    ledger = svc.ledger(CONVENTION)
    expect([(x["round_no"], x["township_id"]) for x in ledger["effective_rounds"]] == [(1, "A")],
           "场地借出和转场回单本身没有让乙或丙抢走任何一轮")


def http_smoke() -> None:
    bar("十一、HTTP 服务冒烟（真实起停，模拟重启外部访问）")
    from lunshi.server import serve

    httpd = serve(DB_PATH, "127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as resp:
            health = json.loads(resp.read().decode())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/street/today", timeout=5) as resp:
            today = json.loads(resp.read().decode())
        expect(health["ok"] is True, "HTTP /api/health 可访问")
        expect(today["days"][0]["day_no"] == 1, "HTTP 街讯可读到当前日")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
    expect(LunshiService(DB_PATH).ledger(CONVENTION)["effective_rounds"][-1]["township_id"] == "C",
           "HTTP 关停后 SQLite 持久化仍在，等同重启")


def main() -> int:
    Path("data").mkdir(exist_ok=True)
    happy_path()
    withdrawal_demo()
    wall_demo()
    conflict_loan_demo()
    http_smoke()
    bar("演示完成")
    print("数据库：", os.path.abspath(DB_PATH))
    print("启动角色网页/API：python3 -m lunshi.server --db", DB_PATH, "--port 8765")
    print("然后打开 http://127.0.0.1:8765/ ；自检：python3 selftest.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
