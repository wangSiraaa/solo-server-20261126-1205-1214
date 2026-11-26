"""单元与集成测试：python -m unittest -v"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from badminton import rules
from badminton.http_app import build_handler
from badminton.service import Service, ServiceError
from badminton.store import Store


def base_setup(game_id="T1", target_score=21, positions=None, server_id=None):
    body = {
        "game_id": game_id,
        "target_score": target_score,
        "serving_team": "A",
        "sides": {"right": "A", "left": "B"},
        "teams": {
            "A": {
                "name": "甲队",
                "players": [{"id": "A1"}, {"id": "A2"}],
                "positions": positions or {"right": "A1", "left": "A2"},
            },
            "B": {
                "name": "乙队",
                "players": [{"id": "B1"}, {"id": "B2"}],
                "positions": {"right": "B1", "left": "B2"},
            },
        },
    }
    if server_id:
        body["server_id"] = server_id
    return body


def play(svc, game_id, winner, prefix):
    permit = svc.get_game(game_id)["current_permit"]
    pid = permit["permit_id"]
    svc.serve(game_id, pid, permit["server_id"], f"{prefix}-serve")
    svc.submit_report(game_id, pid, "J1", winner, f"{prefix}-j1")
    svc.submit_report(game_id, pid, "J2", winner, f"{prefix}-j2")
    return svc.confirm_rally(game_id, pid, f"{prefix}-confirm")


class RuleTests(unittest.TestCase):
    def test_court_for_score(self):
        self.assertEqual(rules.court_for_score(0), "right")
        self.assertEqual(rules.court_for_score(3), "left")
        self.assertEqual(rules.court_for_score(4), "right")

    def test_validate_setup_rejects_number_order_server(self):
        # 右区是 A2 却硬点 A1 开局，必须拒绝
        setup = base_setup(positions={"right": "A2", "left": "A1"}, server_id="A1")
        with self.assertRaises(ValueError):
            rules.validate_setup(setup)

    def test_validate_setup_accepts_right_court_server(self):
        setup = base_setup(positions={"right": "A2", "left": "A1"}, server_id="A2")
        _, server = rules.validate_setup(setup)
        self.assertEqual(server, "A2")

    def test_serving_team_win_swaps_positions(self):
        state = rules.new_state(base_setup())
        permit = next(iter(state["permits"].values()))
        permit["status"] = "served"
        rules.apply_confirmation(state, permit, "A")
        self.assertEqual(state["teams"]["A"]["positions"],
                         {"right": "A2", "left": "A1"})
        # 1 分奇数 -> 左区，左区现在是 A1
        self.assertEqual(state["serving_team"], "A")
        new_permit = state["permits"][state["current_permit_id"]]
        self.assertEqual((new_permit["server_id"], new_permit["court"]),
                         ("A1", "left"))

    def test_receiving_team_win_keeps_positions(self):
        state = rules.new_state(base_setup())
        permit = next(iter(state["permits"].values()))
        permit["status"] = "served"
        rules.apply_confirmation(state, permit, "B")
        self.assertEqual(state["teams"]["B"]["positions"],
                         {"right": "B1", "left": "B2"})
        self.assertEqual(state["serving_team"], "B")
        new_permit = state["permits"][state["current_permit_id"]]
        # B 分数 1 奇数 -> 左区球员 B2
        self.assertEqual((new_permit["server_id"], new_permit["court"]),
                         ("B2", "left"))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.svc = Service(self.dir)

    def test_three_named_scenarios(self):
        # 情形1
        st = self.svc.create_game(base_setup("S1"), "k-s1")
        self.assertEqual((st["current_permit"]["server_id"],
                          st["current_permit"]["court"]), ("A1", "right"))
        st = play(self.svc, "S1", "A", "s1r1")
        self.assertEqual((st["current_permit"]["server_id"],
                          st["current_permit"]["court"]), ("A1", "left"))
        # 情形2
        self.svc.create_game(
            base_setup("S2", positions={"right": "A2", "left": "A1"}), "k-s2")
        pid = self.svc.get_game("S2")["current_permit"]["permit_id"]
        with self.assertRaises(ServiceError):
            self.svc.serve("S2", pid, "A1", "wrong")
        st = play(self.svc, "S2", "A", "s2r1")
        self.assertEqual((st["current_permit"]["server_id"],
                          st["current_permit"]["court"]), ("A2", "left"))
        # 情形3
        self.svc.create_game(base_setup("S3"), "k-s3")
        st = play(self.svc, "S3", "B", "s3r1")
        self.assertEqual((st["current_permit"]["server_id"],
                          st["current_permit"]["court"]), ("B2", "left"))

    def test_confirmation_gate_blocks_next_rally(self):
        self.svc.create_game(base_setup("G"), "k")
        permit = self.svc.get_game("G")["current_permit"]
        # 未开球不能报分
        with self.assertRaises(ServiceError):
            self.svc.submit_report("G", permit["permit_id"], "J1", "A", "r1")
        self.svc.serve("G", permit["permit_id"], "A1", "s")
        self.svc.submit_report("G", permit["permit_id"], "J1", "A", "j1")
        # 只来一份报分，确认条件不满足，不能放行
        with self.assertRaises(ServiceError):
            self.svc.confirm_rally("G", permit["permit_id"], "c")
        self.svc.submit_report("G", permit["permit_id"], "J2", "A", "j2")
        st = self.svc.confirm_rally("G", permit["permit_id"], "c")
        self.assertEqual(st["rally_count"], 1)

    def test_dispute_objection_adjudication_recovery(self):
        self.svc.create_game(base_setup("D"), "k")
        pid = self.svc.get_game("D")["current_permit"]["permit_id"]
        self.svc.serve("D", pid, "A1", "s")
        self.svc.submit_report("D", pid, "J1", "A", "j1")
        self.svc.submit_report("D", pid, "J2", "B", "j2")
        self.assertEqual(self.svc.get_permit("D", pid)["status"], "disputed")
        with self.assertRaises(ServiceError):
            self.svc.confirm_rally("D", pid, "c")
        # 球员与边裁分别在确认界线前举证
        self.svc.file_objection("D", pid, "player", "A1", "wrong_server",
                                "发球人正确", "o1")
        self.svc.file_objection("D", pid, "judge", "J1", "score_call",
                                "坚持甲胜", "o2")
        self.svc.adjudicate("D", pid, "A", "adj")
        st = self.svc.confirm_rally("D", pid, "c")
        self.assertEqual(st["teams"]["A"]["score"], 1)
        self.assertEqual(
            len(self.svc.get_permit("D", pid)["objections"]), 2)

    def test_timeout_blocks_serve_and_late_claim_on_old_permit(self):
        self.svc.create_game(base_setup("TO"), "k")
        play(self.svc, "TO", "A", "r1")  # 拿到左发单 R2-1
        pid = self.svc.get_game("TO")["current_permit"]["permit_id"]
        self.assertEqual(
            (self.svc.get_permit("TO", pid)["server_id"],
             self.svc.get_permit("TO", pid)["court"]),
            ("A1", "left"))
        self.svc.claim_timeout("TO", pid, "A2", "t")
        with self.assertRaises(ServiceError):  # 合法发球人也不能开拍
            self.svc.serve("TO", pid, "A1", "blocked")
        with self.assertRaises(ServiceError):  # 旧单迟报不占新单
            self.svc.claim_timeout("TO", "R1-1", "A1", "late")
        self.svc.release_timeout("TO", pid, "rel")
        st = play(self.svc, "TO", "A", "r2")
        self.assertEqual(st["timeouts"]["A"]["used"], True)

    def test_playable_path_to_game_end_and_rally_provenance(self):
        self.svc.create_game(base_setup("F", target_score=3), "k")
        # 甲拿 3 分即完赛：第 1、2 拍甲胜（含一次换区），第 3 拍乙胜，
        # 再由甲夺回第 4 拍到 3 分，保证换边与换发球权都被走过。
        for i, w in enumerate(["A", "A", "B", "A"], start=1):
            st = play(self.svc, "F", w, f"r{i}")
        self.assertEqual(st["status"], "completed")
        self.assertIsNone(st["current_permit"])
        rallies = self.svc.get_rallies("F")
        self.assertEqual(len(rallies), 4)
        for rally in rallies.values():
            self.assertIn("original_server_id", rally)

    def test_restart_replay_and_idempotent_resend(self):
        self.svc.create_game(base_setup("R"), "setup")
        pid = self.svc.get_game("R")["current_permit"]["permit_id"]
        self.svc.serve("R", pid, "A1", "serve")
        self.svc.submit_report("R", pid, "J1", "A", "j1")
        before = len(self.svc.replay_events("R"))

        # 重启：全新 Service 实例只靠日志还原
        svc2 = Service(self.dir)
        st = svc2.submit_report("R", pid, "J1", "A", "j1")  # 同一幂等键
        after = len(svc2.replay_events("R"))
        self.assertEqual(before, after)  # 不多打一拍
        self.assertEqual(st["rally_count"], 0)

        # 重放日志与在线状态一致
        replayed = Store(self.dir).load_state("R")
        self.assertEqual(
            replayed["permits"][pid]["reports"],
            svc2.get_permit("R", pid)["reports"])

    def test_void_and_reissue_before_serve(self):
        self.svc.create_game(base_setup("V"), "k")
        old = self.svc.get_game("V")["current_permit"]["permit_id"]
        st = self.svc.void_and_reissue("V", old, "v")
        self.assertEqual(self.svc.get_permit("V", old)["status"], "void")
        self.assertEqual(st["current_permit"]["permit_id"], "R1-2")
        # 旧单不能再开球
        with self.assertRaises(ServiceError):
            self.svc.serve("V", old, "A1", "old")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.handler = build_handler(Service(self.dir))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
            return json.loads(resp.read())

    def test_role_entrypoints_and_dispute_flow(self):
        setup = base_setup("H")
        status, st = self._post("/api/desk/games", setup)
        self.assertEqual(status, 200)
        pid = st["current_permit"]["permit_id"]

        # 球员入口开球
        self.assertEqual(
            self._post("/api/player/games/H/serve",
                       {"permit_id": pid, "player_id": "A1",
                        "idempotency_key": "s"})[0], 200)
        # 边裁入口各报一分，报反
        self._post("/api/judge/games/H/reports",
                   {"permit_id": pid, "judge_id": "J1", "winner": "A"})
        self._post("/api/judge/games/H/reports",
                   {"permit_id": pid, "judge_id": "J2", "winner": "B"})
        disputed = self._get(f"/api/games/H/permits/{pid}")
        self.assertEqual(disputed["status"], "disputed")
        # 球员入口举证，记分台入口裁定并确认
        self._post("/api/player/games/H/objections",
                   {"permit_id": pid, "player_id": "A1", "kind": "score_call",
                    "evidence": "球在线内"})
        self._post("/api/desk/games/H/adjudicate",
                   {"permit_id": pid, "winner": "A"})
        _, st = self._post("/api/desk/games/H/confirm",
                           {"permit_id": pid})
        self.assertEqual(st["teams"]["A"]["score"], 1)

        # 重放接口与留痕
        replay = self._get("/api/games/H/replay")
        self.assertGreaterEqual(len(replay), 5)
        rallies = self._get("/api/games/H/rallies")["rallies"]
        self.assertEqual(rallies[0]["original_server_id"], "A1")

    def test_role_isolation(self):
        self._post("/api/desk/games", base_setup("X"))
        # 边裁入口不能干记分台的活
        code, _ = self._post("/api/judge/games/X/confirm",
                             {"permit_id": "R1-1"})
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
