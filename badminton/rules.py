"""纯规则模块：开局校验、发球区判定、轮换与确认入账。

本模块不接触 HTTP 和磁盘，全部是可以单测的纯函数，便于边裁/记分台
对照纸面规则复核。
"""

RIGHT = "right"
LEFT = "left"
COURTS = (RIGHT, LEFT)
JUDGES = ("J1", "J2")


def court_for_score(score: int) -> str:
    """新发球方按自己的分数选站区：偶数右区、奇数左区。"""
    return RIGHT if score % 2 == 0 else LEFT


def validate_setup(setup: dict):
    """校验登记信息，返回 (目标分, 开局合法发球人)。"""
    teams = setup.get("teams")
    if not isinstance(teams, dict) or len(teams) != 2:
        raise ValueError("teams 必须且只能包含两支球队")

    sides = setup.get("sides")
    if (
        not isinstance(sides, dict)
        or set(sides.keys()) != set(COURTS)
        or set(sides.values()) != set(teams.keys())
    ):
        raise ValueError("sides 必须把左右物理区各分配给一支参赛球队")

    serving_team = setup.get("serving_team")
    if serving_team not in teams:
        raise ValueError("serving_team 必须是参赛球队之一")

    for team_id, team in teams.items():
        players = team.get("players", [])
        player_ids = [p.get("id") for p in players]
        if len(player_ids) != 2 or len(set(player_ids)) != 2:
            raise ValueError(f"球队 {team_id} 必须登记两名不同号码的球员")
        positions = team.get("positions", {})
        if set(positions.keys()) != set(COURTS) or set(positions.values()) != set(
            player_ids
        ):
            raise ValueError(f"球队 {team_id} 必须让两名球员各占一个初始区")

    target_score = int(setup.get("target_score", 21))
    if target_score <= 0:
        raise ValueError("target_score 必须是正整数")

    # 开局比分 0:0，发球方分数为偶数，只能由右区球员发球。
    right_server = teams[serving_team]["positions"][RIGHT]
    explicit_server = setup.get("server_id")
    if explicit_server is not None and explicit_server != right_server:
        raise ValueError(
            f"开局比分 0（偶数），合法发球人是右区的 {right_server}，"
            f"不能只按号码顺序指定 {explicit_server}"
        )
    return target_score, right_server


def new_state(setup: dict) -> dict:
    """根据登记信息构造一局的初始状态，并签发第一张开拍单。"""
    target_score, _ = validate_setup(setup)
    teams = {}
    for team_id, team in setup["teams"].items():
        teams[team_id] = {
            "name": team.get("name", team_id),
            "players": list(team["players"]),
            "positions": dict(team["positions"]),
            "score": 0,
        }

    state = {
        "game_id": setup["game_id"],
        "target_score": target_score,
        "status": "open",
        "teams": teams,
        "sides": dict(setup["sides"]),
        "serving_team": setup["serving_team"],
        "rally_count": 0,
        "rallies": {},  # rally_no -> 已确认回合的终局记录
        "permits": {},  # permit_id -> 开拍单
        "current_permit_id": None,
        "timeouts": {
            team_id: {"used": False, "permit_id": None} for team_id in teams
        },
        "idempotency": {},
        "event_count": 0,
    }
    issue_permit(state, rally_no=1, seq=1)
    return state


def issue_permit(state: dict, rally_no: int, seq: int) -> dict:
    """按当前分数和站位签发一张指明人与站区的开拍单。"""
    team_id = state["serving_team"]
    court = court_for_score(state["teams"][team_id]["score"])
    server_id = state["teams"][team_id]["positions"][court]
    permit_id = f"R{rally_no}-{seq}"
    permit = {
        "permit_id": permit_id,
        "rally_no": rally_no,
        "seq": seq,
        "team": team_id,
        "server_id": server_id,
        "court": court,
        # issued -> served -> (disputed -> adjudicated) -> confirmed
        # issued 阶段也可被 void 后重发
        "status": "issued",
        "served_by": None,
        "reports": {},
        "objections": [],
        "timeout_hold": None,
        "adjudicated_winner": None,
        "final_winner": None,
    }
    state["permits"][permit_id] = permit
    state["current_permit_id"] = permit_id
    return permit


def apply_confirmation(state: dict, permit: dict, winner: str) -> None:
    """确认一拍到账：加分、轮换、留痕，并签发下一张开拍单（或结束本局）。"""
    if winner not in state["teams"]:
        raise ValueError("胜方球队不存在")
    if permit["status"] not in ("served", "adjudicated"):
        raise ValueError("该开拍单当前不允许确认")

    rally_no = permit["rally_no"]
    team = state["teams"][winner]
    score_before = team["score"]
    team["score"] += 1

    # 赢方获得发球权；原发球方赢球才交换左右区，接发方夺分站位不动。
    if winner == state["serving_team"]:
        positions = team["positions"]
        positions[RIGHT], positions[LEFT] = positions[LEFT], positions[RIGHT]
    state["serving_team"] = winner

    permit["status"] = "confirmed"
    permit["final_winner"] = winner
    state["rallies"][str(rally_no)] = {
        "rally_no": rally_no,
        "permit_id": permit["permit_id"],
        "original_server_id": permit["served_by"],
        "original_server_court": permit["court"],
        "serving_team_at_serve": permit["team"],
        "winner": winner,
        "score_before": score_before,
        "score_after": team["score"],
    }
    state["rally_count"] = rally_no

    if team["score"] >= state["target_score"]:
        state["status"] = "completed"
        state["current_permit_id"] = None
    else:
        issue_permit(state, rally_no + 1, seq=1)
