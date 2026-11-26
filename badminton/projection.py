"""事件溯源：所有写操作只追加一条事件，重放即可还原整局。

事件列表：
  game.setup        登记初始站位，创建本局
  permit.issued     签发开拍单（含第一拍与每拍确认后的新单）
  permit.served     球员持有效单实际开球
  permit.voided     开拍前作废旧单（例如发现喊错人）
  report.submitted  边裁临时报分
  permit.disputed   两位边裁报反，锁定确认
  objection.filed   确认界线前的异议（含证据）
  permit.adjudicated 记分台裁定争议拍
  timeout.claimed   球员在有效单上领用暂停
  timeout.released  记分台放行暂停
  rally.confirmed   一拍确认入账，分数/站位/发球权落定
"""

from . import rules

EVENT_TYPES = (
    "game.setup",
    "permit.issued",
    "permit.served",
    "permit.voided",
    "report.submitted",
    "permit.disputed",
    "objection.filed",
    "permit.adjudicated",
    "timeout.claimed",
    "timeout.released",
    "rally.confirmed",
)


def initial_state():
    return None


def replay(events):
    """按事件顺序重放，返回最终状态。"""
    state = None
    for event in events:
        state = apply_event(state, event)
    return state


def apply_event(state, event):
    etype = event["type"]
    data = event["data"]

    if etype == "game.setup":
        state = rules.new_state(data["setup"])
        # new_state 内已含第一张开拍单，事件流里会随后用 permit.issued 表达；
        # 这里先把它摘掉，保证事件流是状态的唯一真相来源。
        state["permits"].clear()
        state["current_permit_id"] = None
        state["event_count"] = 1
        return state

    if state is None:
        raise ValueError("缺少 game.setup 事件，无法重放")
    state["event_count"] += 1

    if etype == "permit.issued":
        permit = {
            "permit_id": data["permit_id"],
            "rally_no": data["rally_no"],
            "seq": data["seq"],
            "team": data["team"],
            "server_id": data["server_id"],
            "court": data["court"],
            "status": "issued",
            "served_by": None,
            "reports": {},
            "objections": [],
            "timeout_hold": None,
            "adjudicated_winner": None,
            "final_winner": None,
        }
        state["permits"][permit["permit_id"]] = permit
        state["current_permit_id"] = permit["permit_id"]

    elif etype == "permit.served":
        permit = state["permits"][data["permit_id"]]
        permit["status"] = "served"
        permit["served_by"] = data["player_id"]

    elif etype == "permit.voided":
        permit = state["permits"][data["permit_id"]]
        permit["status"] = "void"
        state["current_permit_id"] = None

    elif etype == "report.submitted":
        permit = state["permits"][data["permit_id"]]
        permit["reports"][data["judge_id"]] = data["winner"]

    elif etype == "permit.disputed":
        permit = state["permits"][data["permit_id"]]
        permit["status"] = "disputed"

    elif etype == "objection.filed":
        permit = state["permits"][data["permit_id"]]
        permit["objections"].append(
            {
                "objection_id": data["objection_id"],
                "filer": data["filer"],
                "kind": data["kind"],
                "evidence": data["evidence"],
            }
        )

    elif etype == "permit.adjudicated":
        permit = state["permits"][data["permit_id"]]
        permit["status"] = "adjudicated"
        permit["adjudicated_winner"] = data["winner"]

    elif etype == "timeout.claimed":
        permit = state["permits"][data["permit_id"]]
        permit["timeout_hold"] = {
            "team": data["team"],
            "claimed_by": data["player_id"],
        }
        hold = state["timeouts"][data["team"]]
        hold["used"] = True
        hold["permit_id"] = data["permit_id"]

    elif etype == "timeout.released":
        permit = state["permits"][data["permit_id"]]
        permit["timeout_hold"] = None

    elif etype == "rally.confirmed":
        permit = state["permits"][data["permit_id"]]
        rules.apply_confirmation(state, permit, data["winner"])

    else:
        raise ValueError(f"未知事件类型: {etype}")

    return state
