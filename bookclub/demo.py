"""Re-runnable local demo for the bookstore reading service.

Run::

    python3 -m bookclub.demo            # fresh state, full walkthrough
    python3 -m bookclub.demo --keep     # resume the saved state file

The demo drives the service **through its HTTP entry points** -- exactly what a
bookstore's staff screen would call -- rather than poking the store directly.  It
is safe to run repeatedly: a fresh run clears ``.demo-state/state.json`` first;
``--keep`` resumes whatever is on disk (useful for reviewing after a restart).

Scenario (follows the brief):

  1. the author publishes edition v1: six segments, a fork, a turn point, two
     endings;
  2. A reads the *letter* branch and B reads the *recording* branch -- each
     independently learns "钥匙藏在桥下";
  3. a served session is prepared against one locked edition and locked; both
     readers can enter the shared finale;
  4. first comparison, from the new session's un-locked intake point: publish v2
     that moves the recording's reveal one segment later -> B is temporarily
     shut out, A is not;
  5. second comparison from the same origin: restore v1 and remove B's earlier
     mis-tap that was never actually finished -> B is temporarily shut out (the
     phantom record must not count as knowledge);
  6. both now genuinely hold the fact; a second joint session is booked -- but
     the only clerk who can lead the finale is already serving an overlapping
     session, so the desk must rebook (改约) even though the reader facts are
     unchanged;
  7. a completed make-up read (补读) is carried into the next session.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

from .api import serve_http
from .store import Store

STATE_DIR = os.path.join(os.getcwd(), ".demo-state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

A = "阿禾"   # takes the letter branch (甲支)
B = "阿岚"   # takes the recording branch (乙支)


def q(name: str) -> str:
    return urllib.parse.quote(name, safe="")


class Client:
    def __init__(self, base: str):
        self.base = base

    def call(self, method: str, path: str, body: dict | None = None,
             *, expect_error: bool = False):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            if not expect_error:
                raise
            return exc.code, payload


def hr(title: str) -> None:
    print("\n" + "=" * 76)
    print(title)
    print("=" * 76)


def show(label: str, value) -> None:
    print(f"  {label}: {json.dumps(value, ensure_ascii=False)}")


def run_demo(base: str, *, restart: bool = False) -> None:
    c = Client(base)

    hr("0. 服务与示例故事")
    _, snap = c.call("GET", "/snapshot")
    if not snap["works"]:
        from .story import FACTS, V1_SEGMENTS
        for fid, desc in FACTS.items():
            c.call("POST", "/facts", {"id": fid, "description": desc})
        c.call("POST", "/works", {"id": "bridge", "title": "桥下的钥匙"})
        _, t = c.call("POST", "/works/bridge/turns",
                      {"label": "v1 · 初版", "segments": V1_SEGMENTS})
        t1 = t["turn"]["id"]
        print(f"  已发布 v1（{t1}），共 {len(V1_SEGMENTS)} 段："
              "一·雨夜归人 / 二·桥下(信) / 二·桥下(录音) / 三·天亮之前 / 结局·重逢 / 结局·空屋")
    else:
        t1 = snap["works"]["bridge"]["turns"][0]["id"]
        print(f"  复用磁盘上的状态文件；v1 = {t1}")
    if restart:
        print("  （复查重启：状态从磁盘恢复，下面的读者都是既有身份）")

    hr("1. 甲支与乙支：两条路各自揭晓同一件事「钥匙藏在桥下」")
    c.call("POST", "/read", {"reader": A, "work": "bridge", "turn": t1, "segment": "ch2a"})
    c.call("POST", "/read", {"reader": B, "work": "bridge", "turn": t1, "segment": "ch2b"})
    ra = c.call("GET", f"/readers/{q(A)}")[1]
    rb = c.call("GET", f"/readers/{q(B)}")[1]
    show("阿禾（信）在 v1 的 key 来源", ra["reads"][t1].get("key_under_bridge"))
    show("阿岚（录音）在 v1 的 key 来源", rb["reads"][t1].get("key_under_bridge"))

    hr("2. 会前备一条连续领读路线并锁稿")
    _, staff = c.call("POST", "/staff", {"name": "店员·程"})
    clerk = staff["staff"]["id"]
    _, sess1 = c.call("POST", "/sessions", {
        "title": "周五共读（会前备稿）", "work": "bridge", "kind": "served",
        "goals": ["e1", "e2"],
    })
    s1 = sess1["session"]["id"]
    for who in (A, B):
        c.call("POST", f"/sessions/{s1}/register", {"reader": who})
    _, served = c.call("POST", f"/sessions/{s1}/serve",
                       {"staff": clerk, "start": 0.0, "end": 60.0})
    show("锁定版本", served["turn"])
    show("领读路线（一段接一段）", served["line"]["route"]["segments"])
    show("需要会前补读的人", served["line"]["route"]["needs_remediation"])
    _, locked = c.call("POST", f"/sessions/{s1}/lock", {})
    show("场次状态", locked["status"])

    hr("3. 两人一起进入末段：各自支线都已持有这条事实")
    for who in (A, B):
        can = c.call("GET", f"/turns/{t1}/can-enter?reader={q(who)}&segment=e1")[1]
        show(f"{who} 能否进入 e1", can["allowed"])
        assert can["allowed"] is True

    hr("4. 第一遍比较：起点不变，只改读者拿到的版本（录音揭晓后移）")
    from .story import V2_SEGMENTS
    _, v2 = c.call("POST", "/works/bridge/turns", {
        "label": "v2 · 录音的揭晓后移", "segments": V2_SEGMENTS,
        "revision_of": t1, "unchanged_segments": ["ch2a", "ch2b"],
    })
    t2 = v2["turn"]["id"]
    cmp_a = c.call("POST", "/compare-intake", {
        "reader": A, "fact": "key_under_bridge", "turn_a": t1, "turn_b": t2,
        "segment": "e1"})[1]["comparison"]
    cmp_b = c.call("POST", "/compare-intake", {
        "reader": B, "fact": "key_under_bridge", "turn_a": t1, "turn_b": t2,
        "segment": "e1"})[1]["comparison"]
    show("阿禾 v1→v2", {"v1": cmp_a["turn_a"]["status"],
                       "v2": cmp_a["turn_b"]["status"], "regressed": cmp_a["regressed"]})
    show("阿岚 v1→v2", {"v1": cmp_b["turn_a"]["status"],
                       "v2": cmp_b["turn_b"]["status"], "regressed": cmp_b["regressed"]})
    assert cmp_a["regressed"] is False   # letter branch declared unchanged
    assert cmp_b["regressed"] is True    # tape reveal moved to an unread segment
    can_b_v2 = c.call("GET", f"/turns/{t2}/can-enter?reader={q(B)}&segment=e1")[1]
    show("阿岚在 v2 能否进入 e1（暂时进不了末段）", can_b_v2["allowed"])
    assert can_b_v2["allowed"] is False

    hr("5. 第二遍比较：还原原文，只撤去乙先前误点、从未读完的记录")
    c.call("POST", f"/turns/{t1}/revert", {})
    # Counterfactual repro from the same intake point: suppose B's *only* record
    # for the key were a tap she never actually finished.  Removing it must not
    # leave her able to enter the finale -- a never-finished read is not
    # knowledge.  (In the demo B's lived history has the real ch2b read; to
    # exercise the desk's rule without rewriting history we stage the phantom on
    # a throwaway reader with the same intake position.)
    phantom = "阿岚(误点复现)"
    c.call("POST", f"/sessions/{s1}/register", {"reader": phantom})
    c.call("POST", "/read", {"reader": phantom, "work": "bridge",
                             "turn": t1, "segment": "ch2b"})   # the mis-tap
    c.call("POST", "/revoke", {"reader": phantom, "fact": "key_under_bridge",
                               "turn": t1, "segment": "ch2b"})  # never finished
    can_ph = c.call("GET", f"/turns/{t1}/can-enter?reader={q(phantom)}&segment=e1")[1]
    show("撤去误点后（同起点复现）能否进入 e1（暂时进不了末段）", can_ph["allowed"])
    assert can_ph["allowed"] is False
    # Reading a real segment is the only thing that legitimately restores access.
    denied = c.call("POST", "/read", {"reader": phantom, "work": "bridge",
                                      "turn": t1, "segment": "e1"}, expect_error=True)
    show("试图绕过前置事实直接进 e1", denied[1]["error"]["code"])

    hr("6. 两人都真正具备事实后，接一笔要两人一起开读的新场")
    # B genuinely re-reads the recording; now both really hold the key.
    c.call("POST", "/read", {"reader": B, "work": "bridge", "turn": t1, "segment": "ch2b"})
    for who in (A, B):
        assert c.call("GET", f"/turns/{t1}/can-enter?reader={q(who)}&segment=e1")[1]["allowed"]
    _, sess2 = c.call("POST", "/sessions", {
        "title": "第二场（两人同开读）", "work": "bridge", "kind": "served",
        "goals": ["e1"]})
    s2 = sess2["session"]["id"]
    for who in (A, B):
        c.call("POST", f"/sessions/{s2}/register", {"reader": who})
    # Same clerk, overlapping with session 1 (0-60): facts identical, slot is not.
    clash = c.call("POST", f"/sessions/{s2}/serve",
                   {"staff": clerk, "start": 30.0, "end": 90.0}, expect_error=True)
    show("时段冲突时", {"code": clash[1]["error"]["code"],
                       "recommendation": clash[1]["error"].get("recommendation")})
    assert clash[1]["error"]["code"] == "staff_double_booked"

    hr("7. 前台区分「该补读」还是「该改约」")
    # Facts are fine; only the slot is the problem -> 改约.
    desk_slot = c.call("POST", f"/sessions/{s2}/front-desk", {
        "reader": B, "fact": "key_under_bridge", "staff": clerk,
        "start": 30.0, "end": 90.0})[1]["decision"]
    show("阿岚（事实已具备，但时段冲突）", {
        "action": desk_slot["action"], "fact": desk_slot["fact_state"]["status"],
        "reasons": desk_slot["reasons"]})
    assert desk_slot["action"] == "rebook"
    assert desk_slot["fact_state"]["status"] == "has"
    # A fact that some segment on the edition discloses -> 补读.
    desk_fact = c.call("POST", f"/sessions/{s2}/front-desk", {
        "reader": B, "fact": "key_under_bridge", "turn": t2})[1]["decision"]
    show("阿岚（比对 v2，缺事实但有可读来源）", {
        "action": desk_fact["action"], "recommendation": desk_fact["recommendation"]})
    assert desk_fact["action"] == "catch_up"
    # A fact no segment ever discloses must be 改约, never a fabricated read.
    ghost = c.call("POST", f"/sessions/{s2}/front-desk", {
        "reader": B, "fact": "vault_code"})[1]["decision"]
    show("阿岚（vault_code 从未被揭晓）", ghost["action"])
    assert ghost["action"] == "rebook"
    refused = c.call("POST", "/practices", {"reader": B, "session": s2,
                                            "fact": "vault_code"}, expect_error=True)
    show("为 vault_code 开补读被拒", refused[1]["error"]["code"])

    hr("8. 补读完成后，参加下一场可以沿用它")
    # 阿岚 on v2 lacks the key (its reveal moved to ch2c).  A make-up read of the
    # short segment ch2c -- which builds on the confession she already has --
    # supplies it on v2.
    _, prac = c.call("POST", "/practices", {"reader": B, "session": s2,
                                            "fact": "key_under_bridge", "turn": t2})
    pid = prac["practice"]["id"]
    show("补读短段", {"turn": prac["practice"]["turn"],
                     "segment": prac["practice"]["segment"],
                     "title": prac["practice"]["segment_title"]})
    assert prac["practice"]["segment"] == "ch2c"
    c.call("POST", f"/practices/{pid}/complete", {})
    carried = c.call("GET", f"/practices/{pid}/facts")[1]
    show("阿岚经补读获得的事实（按版本）", carried["facts_by_turn"].get("key_under_bridge"))
    assert t2 in carried["facts_by_turn"]["key_under_bridge"]

    # The next session is pinned to v2; 阿岚's eligibility there is supplied by
    # the completed make-up read, proving it carries forward.
    _, sess3 = c.call("POST", "/sessions", {
        "title": "下一场（沿用补读，锁 v2）", "work": "bridge", "kind": "served",
        "turn": t2, "goals": ["e1"]})
    s3 = sess3["session"]["id"]
    c.call("POST", f"/sessions/{s3}/register", {"reader": A})
    c.call("POST", f"/sessions/{s3}/register", {"reader": B})
    verdict = c.call("POST", f"/sessions/{s3}/assess", {
        "reader": B, "fact": "key_under_bridge"})[1]["assessment"]
    show("下一场阿岚的事实状态", {"status": verdict["status"],
                             "来源": c.call("GET", f"/readers/{q(B)}")[1]["reads"][t2]["key_under_bridge"]})
    assert verdict["status"] == "has"
    _, served3 = c.call("POST", f"/sessions/{s3}/serve",
                        {"staff": clerk, "start": 60.0, "end": 120.0})
    show("下一场开读（补读已沿用）", {"status": served3["status"],
                                 "待补读": served3["line"]["route"]["needs_remediation"]})
    assert served3["line"]["route"]["needs_remediation"] == []

    hr("9. 改约到不重叠时段后可开读；随后复查快照")
    # The clash above was only about time.  Move session 2 to a window that is
    # free for the clerk -> it can now be served, facts untouched.
    _, ok = c.call("POST", f"/sessions/{s2}/serve",
                   {"staff": clerk, "start": 120.0, "end": 180.0})
    show("第二场改约后开读", {"status": ok["status"], "turn": ok["turn"]})
    # The carry-forward session already opened on v2 with the clerk.
    _, final = c.call("GET", "/snapshot")
    print(f"  事实 {len(final['facts'])} 条 · 版本 {len(final['turns'])} 个 · "
          f"场次 {len(final['sessions'])} 个 · 补读 {len(final['practices'])} 条")
    print(f"  状态文件: {STATE_FILE}")
    print("  可 Ctrl-C 后运行 `python3 -m bookclub.demo --keep --review` 从磁盘复查。")


def review(base: str) -> None:
    """Read-only review of a previously saved run (simulates a process restart)."""
    c = Client(base)
    _, snap = c.call("GET", "/snapshot")
    hr("复查 · 从磁盘状态重启后的只读快照")
    if not snap["works"]:
        print("  状态文件里没有作品；先运行一次 `python3 -m bookclub.demo`。")
        return
    work = c.call("GET", "/works/bridge")[1]["work"]
    print("  作品《桥下的钥匙》，版本：")
    for turn in work["turns"]:
        rev = f"（修订自 {turn['revision_of']}）" if turn["revision_of"] else ""
        print(f"    - {turn['id']} {turn['label']} {rev} "
              f"unchanged={turn['unchanged_segments']} carry={turn['carry_facts']}")
    print(f"  现行版本: {work['current']}")
    print("  已记录的读者事实：")
    for reader, turns in snap["reads"].items():
        for turn, facts in turns.items():
            for fact, srcs in facts.items():
                print(f"    {reader} @ {turn}: {fact} <- {srcs}")
    print("  场次：")
    for sess in snap["sessions"]:
        print(f"    {sess['id']} [{sess['kind']}/{sess['status']}] 《{sess['title']}》 "
              f"版本={sess['turn']} 读者={sess['joined']}")
        if sess.get("line") and sess["line"].get("route"):
            print(f"        领读路线={sess['line']['route'].get('segments')} "
                  f"待补读={sess['line']['route'].get('needs_remediation')}")
    print("  补读记录：")
    for p in snap["practices"]:
        state = "已完成" if p["done"] else "未完成"
        print(f"    {p['id']} {p['reader']} 版本={p['turn']} 段={p['segment']} [{state}]")
    print("\n  以上全部来自磁盘；进程重启不丢失。")


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    os.makedirs(STATE_DIR, exist_ok=True)
    keep = "--keep" in args
    review_mode = "--review" in args
    restarting = keep and os.path.exists(STATE_FILE)
    if not keep and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    if review_mode and not os.path.exists(STATE_FILE):
        print("没有可复查的状态文件；先运行 `python3 -m bookclub.demo`。")
        return 1
    store = Store(STATE_FILE)
    httpd = serve_http(store, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"demo HTTP server: {base}")
    try:
        if review_mode:
            review(base)
        else:
            run_demo(base, restart=restarting)
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
