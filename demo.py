"""端到端演示：直接在进程内跑通题目要求的全部情形。

运行: python demo.py
每个情形使用独立局号，事件日志落在 ./data 下，可再用 replay CLI 重放。
"""

import shutil
import tempfile

from badminton.service import Service, ServiceError
from badminton.replay import main as replay_main

DATA_DIR = tempfile.mkdtemp(prefix="badminton-demo-")
svc = Service(DATA_DIR)


def setup(game_id, server_id=None, target_score=21):
    body = {
        "game_id": game_id,
        "target_score": target_score,
        "serving_team": "A",
        "sides": {"right": "A", "left": "B"},
        "teams": {
            "A": {
                "name": "甲队",
                "players": [{"id": "A1", "name": "甲一"}, {"id": "A2", "name": "甲二"}],
                "positions": {"right": "A1", "left": "A2"},
            },
            "B": {
                "name": "乙队",
                "players": [{"id": "B1", "name": "乙一"}, {"id": "B2", "name": "乙二"}],
                "positions": {"right": "B1", "left": "B2"},
            },
        },
    }
    if server_id:
        body["server_id"] = server_id
    return svc.create_game(body, idempotency_key=f"setup-{game_id}")


def play(game_id, winner, idem_prefix):
    """打一拍：球员开球 -> 两边裁一致报分 -> 记分台确认。"""
    permit = svc.get_game(game_id)["current_permit"]
    pid = permit["permit_id"]
    svc.serve(game_id, pid, permit["server_id"], f"{idem_prefix}-serve")
    svc.submit_report(game_id, pid, "J1", winner, f"{idem_prefix}-j1")
    svc.submit_report(game_id, pid, "J2", winner, f"{idem_prefix}-j2")
    state = svc.confirm_rally(game_id, pid, f"{idem_prefix}-confirm")
    return state


def title(text):
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


def expect_error(label, fn):
    try:
        fn()
    except ServiceError as exc:
        print(f"  [按预期拒绝] {label}: {exc}")
        return
    raise AssertionError(f"{label} 本应被拒绝")


# 情形 1：标准开局，甲一右区发球，甲拿下第一拍 -> 下一拍甲一在左发
title("情形1 甲一右区开局，甲首胜 -> 下一拍甲一在左区发球")
st = setup("G1")
p = st["current_permit"]
print("  首张单:", p["permit_id"], p["server_id"], p["court"])
assert (p["server_id"], p["court"]) == ("A1", "right")
st = play("G1", "A", "g1r1")
p = st["current_permit"]
print("  第二张单:", p["permit_id"], p["server_id"], p["court"],
      "比分 A:B =", st["teams"]["A"]["score"], ":", st["teams"]["B"]["score"])
assert (p["server_id"], p["court"]) == ("A1", "left")

# 情形 2：只对调甲队开局站位，由甲二（右区）开局；同样甲首胜 -> 甲二在左发
title("情形2 对调甲队开局站位，右区甲二开局，同一胜负记录 -> 甲二在左发")
setup_body = {
    "game_id": "G2",
    "serving_team": "A",
    "sides": {"right": "A", "left": "B"},
    "teams": {
        "A": {
            "name": "甲队",
            "players": [{"id": "A1"}, {"id": "A2"}],
            "positions": {"right": "A2", "left": "A1"},  # 只对调甲队
        },
        "B": {
            "name": "乙队",
            "players": [{"id": "B1"}, {"id": "B2"}],
            "positions": {"right": "B1", "left": "B2"},
        },
    },
}
svc.create_game(setup_body, idempotency_key="setup-G2")
p = svc.get_game("G2")["current_permit"]
print("  首张单:", p["permit_id"], p["server_id"], p["court"])
assert (p["server_id"], p["court"]) == ("A2", "right")
# 只照号码轮着发（硬点 A1）必须报错人
expect_error(
    "号码顺序点甲一发球",
    lambda: svc.serve("G2", p["permit_id"], "A1", "g2-wrong-person"),
)
st = play("G2", "A", "g2r1")
p = st["current_permit"]
print("  第二张单:", p["permit_id"], p["server_id"], p["court"])
assert (p["server_id"], p["court"]) == ("A2", "left")

# 情形 3：还原标准站位，首拍判给乙 -> 下一拍是乙队左区球员发球
title("情形3 标准站位但首拍乙夺分 -> 双方站位不动，乙左区球员发球")
st = setup("G3")
st = play("G3", "B", "g3r1")
p = st["current_permit"]
print("  第二张单:", p["permit_id"], p["server_id"], p["court"],
      "比分 A:B =", st["teams"]["A"]["score"], ":", st["teams"]["B"]["score"])
# 乙分数 1 为奇数 -> 左区；站位没动，乙左区是 B2
assert (p["server_id"], p["court"]) == ("B2", "left")
assert st["teams"]["B"]["positions"] == {"right": "B1", "left": "B2"}

# 情形 4：暂停必须开球前领用；暂停中合法发球人也不能开拍；放行后才能开
title("情形4 甲队在左发单上领用暂停 -> 拒绝开拍 -> 放行后正常打完该拍")
st = setup("G4")
st = play("G4", "A", "g4r1")  # 甲首胜，拿到 R2-1：A1 在左发
permit = st["current_permit"]
pid = permit["permit_id"]
assert (permit["server_id"], permit["court"]) == ("A1", "left")
svc.claim_timeout("G4", pid, "A2", "g4-timeout")  # 队员领用，不占新单
print("  暂停已领用，单状态:", svc.get_permit("G4", pid)["timeout_hold"])
expect_error(
    "暂停未放行时甲一开拍",
    lambda: svc.serve("G4", pid, "A1", "g4-blocked-serve"),
)
# 旧单迟报不占新单：对一张根本不是当前单的单号领暂停，必须被拒
expect_error(
    "在旧单 R1-1 上迟报暂停",
    lambda: svc.claim_timeout("G4", "R1-1", "A1", "g4-late-timeout"),
)
svc.release_timeout("G4", pid, "g4-release")
st = play("G4", "A", "g4r2")
print("  放行后第二拍确认，当前单:", st["current_permit"]["permit_id"],
      st["current_permit"]["server_id"], st["current_permit"]["court"])
expect_error(
    "甲队第二次领用暂停",
    lambda: svc.claim_timeout(
        "G4", st["current_permit"]["permit_id"], "A1", "g4-second-timeout"
    ),
)

# 情形 5：确认界线前收异议；记分台与球员各走自己的入口处理下一张单
title("情形5 边裁报反 -> 异议 -> 裁定恢复；并完整打完一局（目标5分）")
st = setup("G5", target_score=5)
pid = st["current_permit"]["permit_id"]
svc.serve("G5", pid, "A1", "g5-r1-serve")
# 边裁各走 judge 入口，报反
svc.submit_report("G5", pid, "J1", "A", "g5-r1-j1")
svc.submit_report("G5", pid, "J2", "B", "g5-r1-j2")
disputed = svc.get_permit("G5", pid)
print("  报反后单状态:", disputed["status"])
assert disputed["status"] == "disputed"
expect_error("争议未裁定就确认", lambda: svc.confirm_rally("G5", pid, "g5-bad-confirm"))
# 球员走 player 入口举证；另一侧边裁走 judge 入口补充证据
svc.file_objection("G5", pid, "player", "A1", "wrong_server",
                   "我方记录发球人无误，J2 胜负记反", "g5-obj-player")
svc.file_objection("G5", pid, "judge", "J1", "score_call",
                   "落点在甲场区底线内，坚持甲胜", "g5-obj-judge")
# 记分台走 desk 入口裁定甲胜，再确认
svc.adjudicate("G5", pid, "A", "g5-adjudicate")
st = svc.confirm_rally("G5", pid, "g5-r1-confirm")
print("  裁定后确认，比分 A:B =",
      st["teams"]["A"]["score"], ":", st["teams"]["B"]["score"])

# 继续打完：A 再拿 3 分到 4:0，然后 B 追到 4:1，最后 A 到 5 分完赛
for i, winner in enumerate(["A", "A", "A", "B", "A"], start=2):
    st = play("G5", winner, f"g5-r{i}")
print("  终局状态:", st["status"],
      "比分 A:B =", st["teams"]["A"]["score"], ":", st["teams"]["B"]["score"])
assert st["status"] == "completed" and st["current_permit"] is None
print("  全部已打回合（含原发球人留痕）:")
for rally in st["rallies"]:
    print("   ", rally)
# 已完赛不能再开拍
expect_error("完赛后再开球", lambda: svc.serve(
    "G5", "R6-1", "A1", "g5-after-game"))

# 情形 6：重启后重送同一张边裁记录，不会凭空多打一拍；事件流可重放
title("情形6 模拟进程重启 + 幂等键重放 + 事件日志重放")
svc2 = Service(DATA_DIR)  # 新实例 = 重启，状态只从日志重建
before = len(svc2.replay_events("G1"))
cached = svc2.submit_report("G1", "R1-1", "J1", "A", "g1r1-j1")  # 同一幂等键
after = len(svc2.replay_events("G1"))
assert before == after, "事件条数发生变化，凭空多了一拍！"
print(f"  重送边裁记录命中幂等缓存，直接返回当前状态，未新增事件")
print(f"  事件条数前后一致: {before} 条；G1 已确认回合数 = "
      f"{cached['rally_count']}")
# 不带幂等键的重复报分同样被业务规则拦住（同一边裁不得改报）
expect_error(
    "同边裁重复报分",
    lambda: svc2.submit_report("G1", "R1-1", "J1", "B", None),
)

print("\n--- 用 replay CLI 重放 G1 ---")
replay_main([DATA_DIR, "G1"])

print("\n数据目录:", DATA_DIR)
print("全部演示通过 ✅")
shutil.rmtree(DATA_DIR, ignore_errors=True)
