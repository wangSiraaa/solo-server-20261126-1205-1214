"""记分台服务层：逐拍确认制下的全部命令与状态查询。

角色入口（HTTP 层据此分前缀）：
  desk     记分台：开回合、确认/裁定、放行暂停、作废重发
  judge    边裁：临时报分、举证异议
  player   球员：领用暂停、持单开球、举证异议
"""

from . import rules
from .store import IdempotencyError, Store


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class Service:
    def __init__(self, data_dir: str):
        self.store = Store(data_dir)

    # ---------- 基础工具 ----------

    def _load(self, game_id: str):
        state = self.store.load_state(game_id)
        if state is None:
            raise ServiceError("本局不存在", 404)
        return state

    def _current_permit(self, state: str):
        permit_id = state.get("current_permit_id")
        if not permit_id:
            raise ServiceError("当前没有待处理的开拍单", 409)
        return state["permits"][permit_id]

    def _permit(self, state: dict, permit_id: str):
        permit = state["permits"].get(permit_id)
        if permit is None:
            raise ServiceError("开拍单不存在", 404)
        return permit

    @staticmethod
    def _check_actor(permit: dict, role: str, actor: str):
        if role == "judge" and actor not in rules.JUDGES:
            raise ServiceError("只有 J1/J2 两位边裁可以操作", 403)
        if role == "player":
            all_players = set()
            # player 合法性在调用处结合具体局校验，这里只做基本约束
            if not actor:
                raise ServiceError("缺少球员标识", 403)
        if role == "desk" and actor != "desk":
            raise ServiceError("记分台入口仅 desk 可调用", 403)

    @staticmethod
    def public_state(state: dict) -> dict:
        return {
            "game_id": state["game_id"],
            "status": state["status"],
            "target_score": state["target_score"],
            "serving_team": state["serving_team"],
            "sides": state["sides"],
            "teams": {
                tid: {
                    "name": team["name"],
                    "score": team["score"],
                    "positions": team["positions"],
                    "players": team["players"],
                }
                for tid, team in state["teams"].items()
            },
            "timeouts": state["timeouts"],
            "rally_count": state["rally_count"],
            "current_permit": (
                state["permits"][state["current_permit_id"]]
                if state["current_permit_id"]
                else None
            ),
            "rallies": list(state["rallies"].values()),
        }

    # ---------- 登记开局 ----------

    def create_game(self, setup: dict, idempotency_key: str | None = None):
        game_id = setup.get("game_id")
        if not game_id:
            raise ServiceError("缺少 game_id", 400)
        cached = self._idempotent_guard(game_id, idempotency_key)
        if cached is not None:
            return cached
        if self.store.exists(game_id):
            raise ServiceError("本局已存在", 409)

        # 纯规则校验 + 构造初始状态（含第一张单），再把状态翻译成事件。
        state = rules.new_state(setup)
        first_permit = next(iter(state["permits"].values()))
        events = [
            {"type": "game.setup", "data": {"setup": setup}},
            {
                "type": "permit.issued",
                "data": {
                    "permit_id": first_permit["permit_id"],
                    "rally_no": first_permit["rally_no"],
                    "seq": first_permit["seq"],
                    "team": first_permit["team"],
                    "server_id": first_permit["server_id"],
                    "court": first_permit["court"],
                },
            },
        ]
        try:
            self.store.append_batch(game_id, events, idempotency_key)
        except IdempotencyError as exc:
            raise ServiceError(f"重复请求，首次事件 #{exc.previous_event_id}", 200)
        return self.public_state(self._load(game_id))

    # ---------- 球员：持有效单开球 ----------

    def serve(self, game_id: str, permit_id: str, player_id: str,
              idempotency_key: str | None = None):
        cached = self._idempotent_guard(game_id, idempotency_key)
        if cached is not None:
            return cached
        state = self._load(game_id)
        if state["status"] != "open":
            raise ServiceError("本局已结束，不能再开拍", 409)
        permit = self._permit(state, permit_id)
        if permit["permit_id"] != state["current_permit_id"]:
            raise ServiceError("这张开拍单已失效，请以记分台最新单为准", 409)
        if permit["status"] != "issued":
            raise ServiceError(f"开拍单状态为 {permit['status']}，不能开球", 409)
        if permit["timeout_hold"] is not None:
            raise ServiceError("该拍已领用暂停，记分台放行前不能开球", 409)
        if player_id != permit["server_id"]:
            raise ServiceError(
                f"本单指明发球人是 {permit['server_id']}，"
                f"{player_id} 不能持此单开球",
                403,
            )
        event = {
            "type": "permit.served",
            "data": {"permit_id": permit_id, "player_id": player_id},
        }
        self._append(game_id, event, idempotency_key)
        return self.public_state(self._load(game_id))

    # ---------- 边裁：临时报分 ----------

    def submit_report(self, game_id: str, permit_id: str, judge_id: str,
                      winner: str, idempotency_key: str | None = None):
        cached = self._idempotent_guard(game_id, idempotency_key)
        if cached is not None:
            return cached
        state = self._load(game_id)
        if judge_id not in rules.JUDGES:
            raise ServiceError("只有 J1/J2 两位边裁可以报分", 403)
        if winner not in state["teams"]:
            raise ServiceError("胜方球队不存在", 400)
        permit = self._permit(state, permit_id)
        if permit["permit_id"] != state["current_permit_id"]:
            raise ServiceError("这张开拍单已失效，不能再报分", 409)
        if permit["status"] not in ("served", "disputed", "adjudicated"):
            raise ServiceError(
                f"开拍单状态为 {permit['status']}，尚未实际开球，不能报分", 409
            )
        if judge_id in permit["reports"]:
            raise ServiceError(
                f"{judge_id} 已就本拍报过分，不得改报；如认为报错请走异议", 409
            )

        events = [
            {
                "type": "report.submitted",
                "data": {
                    "permit_id": permit_id,
                    "judge_id": judge_id,
                    "winner": winner,
                },
            }
        ]
        # 两位边裁都到齐且报反：本拍进入争议，确认被锁定，等记分台裁定。
        existing = dict(permit["reports"])
        existing[judge_id] = winner
        if len(existing) == 2 and len(set(existing.values())) == 2:
            events.append(
                {"type": "permit.disputed", "data": {"permit_id": permit_id}}
            )
        self._append_batch(game_id, events, idempotency_key)
        return self.public_state(self._load(game_id))

    # ---------- 边裁/球员：确认界线前举证异议 ----------

    def file_objection(self, game_id: str, permit_id: str, role: str, filer: str,
                       kind: str, evidence: str, idempotency_key: str | None = None):
        cached = self._idempotent_guard(game_id, idempotency_key)
        if cached is not None:
            return cached
        state = self._load(game_id)
        permit = self._permit(state, permit_id)
        if permit["permit_id"] != state["current_permit_id"]:
            raise ServiceError("这张开拍单已失效，异议窗口已关闭", 409)
        if permit["status"] == "confirmed":
            raise ServiceError("该拍已经确认，超过逐拍确认界线，本局不再受理", 409)
        if role == "judge":
            if filer not in rules.JUDGES:
                raise ServiceError("边裁身份只能是 J1/J2", 403)
        elif role == "player":
            registered = {
                p["id"] for team in state["teams"].values() for p in team["players"]
            }
            if filer not in registered:
                raise ServiceError("只有本局登记球员可以举证", 403)
        else:
            raise ServiceError("只有边裁和球员能提交异议", 403)
        if kind not in ("wrong_server", "score_call", "line_call", "other"):
            raise ServiceError(
                "kind 必须是 wrong_server/score_call/line_call/other", 400
            )

        objection_id = f"{permit_id}-O{len(permit['objections']) + 1}"
        event = {
            "type": "objection.filed",
            "data": {
                "permit_id": permit_id,
                "objection_id": objection_id,
                "filer": f"{role}:{filer}",
                "kind": kind,
                "evidence": evidence,
            },
        }
        self._append(game_id, event, idempotency_key)
        return self.public_state(self._load(game_id))

    # ---------- 记分台：确认一拍 ----------

    def confirm_rally(self, game_id: str, permit_id: str,
                      idempotency_key: str | None = None):
        cached = self._idempotent_guard(game_id, idempotency_key)
        if cached is not None:
            return cached
        state = self._load(game_id)
        if state["status"] != "open":
            raise ServiceError("本局已结束", 409)
        permit = self._permit(state, permit_id)
        if permit["permit_id"] != state["current_permit_id"]:
            raise ServiceError("这张开拍单已失效，不能确认", 409)

        if permit["status"] == "disputed":
            raise ServiceError(
                "两位边裁报反，本拍处于争议，记分台须先裁定才能确认", 409
            )
        if permit["status"] == "adjudicated":
            winner = permit["adjudicated_winner"]
        elif permit["status"] == "served":
            winners = set(permit["reports"].values())
            if len(permit["reports"]) < 2:
                raise ServiceError(
                    "确认条件未满足：需要两位边裁一致的临时报分", 409
                )
            if len(winners) != 1:
                raise ServiceError("边裁报分不一致，不能确认", 409)
            winner = next(iter(winners))
        else:
            raise ServiceError(
                f"开拍单状态为 {permit['status']}，确认条件未满足", 409
            )
        if permit["timeout_hold"] is not None:
            raise ServiceError("暂停尚未放行，不能确认本拍", 409)

        # 先在内存里跑一遍纯规则，拿到下一张单的内容，再整体落事件。
        rules.apply_confirmation(state, permit, winner)
        events = [
            {
                "type": "rally.confirmed",
                "data": {"permit_id": permit_id, "winner": winner},
            }
        ]
        next_permit = state["permits"].get(state["current_permit_id"])
        if next_permit is not None:
            events.append(
                {
                    "type": "permit.issued",
                    "data": {
                        "permit_id": next_permit["permit_id"],
                        "rally_no": next_permit["rally_no"],
                        "seq": next_permit["seq"],
                        "team": next_permit["team"],
                        "server_id": next_permit["server_id"],
                        "court": next_permit["court"],
                    },
                }
            )
        self._append_batch(game_id, events, idempotency_key)
        return self.public_state(self._load(game_id))

    # ---------- 记分台：裁定争议拍 ----------

    def adjudicate(self, game_id: str, permit_id: str, winner: str,
                   idempotency_key: str | None = None):
        cached = self._idempotent_guard(game_id, idempotency_key)
        if cached is not None:
            return cached
        state = self._load(game_id)
        if winner not in state["teams"]:
            raise ServiceError("胜方球队不存在", 400)
        permit = self._permit(state, permit_id)
        if permit["permit_id"] != state["current_permit_id"]:
            raise ServiceError("这张开拍单已失效", 409)
        if permit["status"] != "disputed":
            raise ServiceError("只有报反进入争议的开拍单需要裁定", 409)
        event = {
            "type": "permit.adjudicated",
            "data": {"permit_id": permit_id, "winner": winner},
        }
        self._append(game_id, event, idempotency_key)
        return self.public_state(self._load(game_id))

    # ---------- 记分台：开拍前作废旧单并重发 ----------

    def void_and_reissue(self, game_id: str, permit_id: str,
                         idempotency_key: str | None = None):
        cached = self._idempotent_guard(game_id, idempotency_key)
        if cached is not None:
            return cached
        state = self._load(game_id)
        permit = self._permit(state, permit_id)
        if permit["permit_id"] != state["current_permit_id"]:
            raise ServiceError("这张开拍单已失效", 409)
        if permit["status"] != "issued":
            raise ServiceError("只有尚未开球的开拍单可以作废重发", 409)
        if permit["timeout_hold"] is not None:
            raise ServiceError("暂停中的单先放行或结束暂停再处理", 409)

        new_seq = permit["seq"] + 1
        new_permit = rules.issue_permit(state, permit["rally_no"], new_seq)
        # issue_permit 顺手改了 current_permit_id，先恢复，事件落盘后由重放重建。
        state["current_permit_id"] = permit_id
        events = [
            {"type": "permit.voided", "data": {"permit_id": permit_id}},
            {
                "type": "permit.issued",
                "data": {
                    "permit_id": new_permit["permit_id"],
                    "rally_no": new_permit["rally_no"],
                    "seq": new_permit["seq"],
                    "team": new_permit["team"],
                    "server_id": new_permit["server_id"],
                    "court": new_permit["court"],
                },
            },
        ]
        self._append_batch(game_id, events, idempotency_key)
        return self.public_state(self._load(game_id))

    # ---------- 暂停：球员领用、记分台放行 ----------

    def claim_timeout(self, game_id: str, permit_id: str, player_id: str,
                      idempotency_key: str | None = None):
        cached = self._idempotent_guard(game_id, idempotency_key)
        if cached is not None:
            return cached
        state = self._load(game_id)
        permit = self._permit(state, permit_id)
        team_id = permit["team"]
        registered = {p["id"] for p in state["teams"][team_id]["players"]}
        if player_id not in registered:
            raise ServiceError("只有本单发球方球员能领用本队暂停", 403)
        if permit["permit_id"] != state["current_permit_id"]:
            raise ServiceError("旧单上的迟报不占新单：这张单已失效", 409)
        if permit["status"] != "issued":
            raise ServiceError("暂停必须在实际开球前领用", 409)
        if permit["timeout_hold"] is not None:
            raise ServiceError("该拍已在暂停中", 409)
        if state["timeouts"][team_id]["used"]:
            raise ServiceError(f"球队 {team_id} 本局暂停已用过", 409)

        event = {
            "type": "timeout.claimed",
            "data": {
                "permit_id": permit_id,
                "team": team_id,
                "player_id": player_id,
            },
        }
        self._append(game_id, event, idempotency_key)
        return self.public_state(self._load(game_id))

    def release_timeout(self, game_id: str, permit_id: str,
                        idempotency_key: str | None = None):
        cached = self._idempotent_guard(game_id, idempotency_key)
        if cached is not None:
            return cached
        state = self._load(game_id)
        permit = self._permit(state, permit_id)
        if permit["timeout_hold"] is None:
            raise ServiceError("该开拍单没有暂停占用", 409)
        event = {
            "type": "timeout.released",
            "data": {"permit_id": permit_id},
        }
        self._append(game_id, event, idempotency_key)
        return self.public_state(self._load(game_id))

    # ---------- 查询 ----------

    def get_game(self, game_id: str):
        return self.public_state(self._load(game_id))

    def get_permit(self, game_id: str, permit_id: str):
        state = self._load(game_id)
        return self._permit(state, permit_id)

    def get_rallies(self, game_id: str):
        """已开打回合的原发球人等终局留痕，确认后即可查。"""
        return self._load(game_id)["rallies"]

    def replay_events(self, game_id: str):
        events = self.store.load_events(game_id)
        if events is None:
            raise ServiceError("本局不存在", 404)
        return events

    # ---------- 内部 ----------

    def _idempotent_guard(self, game_id: str, idempotency_key):
        """幂等键先于业务规则检查：重放同一条记录返回首次处理结果。"""
        if not idempotency_key:
            return None
        event = self.store.find_idempotent(game_id, idempotency_key)
        if event is not None:
            return self.public_state(self._load(game_id))
        return None

    def _append(self, game_id, event, idempotency_key):
        try:
            return self.store.append(game_id, event, idempotency_key)
        except IdempotencyError as exc:
            raise ServiceError(f"重复请求，首次事件 #{exc.previous_event_id}", 200)

    def _append_batch(self, game_id, events, idempotency_key):
        try:
            return self.store.append_batch(game_id, events, idempotency_key)
        except IdempotencyError as exc:
            raise ServiceError(f"重复请求，首次事件 #{exc.previous_event_id}", 200)
