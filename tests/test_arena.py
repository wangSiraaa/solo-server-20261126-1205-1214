"""Behavioural tests for the crossword arena, mirroring the brief.

Run:  python3 -m pytest tests/ -q      (or)  python3 tests/test_arena.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xword import Arena, scenarios
from xword.scoring import Scorer, score_diff


def fresh(mode="race", teams=("A",), store=True, **ov):
    store_dir = tempfile.mkdtemp() if store else None
    a = Arena(store_dir)
    sid = a.create_session(scenarios.shan_shui_board(mode, **ov),
                           list(teams))["session_id"]
    return a, sid


class TestBasics(unittest.TestCase):
    def test_board_shape(self):
        _, sid = fresh()
        a, _ = None, None
        # independent cells are the three endpoints, no interior-only cell
        spec = scenarios.shan_shui_board("race")
        ents = {e["id"]: e["cells"] for e in spec["entries"]}
        self.assertEqual(ents["shan_shui"], [[1, 0], [1, 1]])
        self.assertEqual(ents["shui_che"], [[1, 1], [2, 1]])

    def test_solo_max_three(self):
        a, sid = fresh()
        a.handin(sid, "A", "shan_shui", "山水", "1")
        a.handin(sid, "A", "shui_che", "水车", "2")
        self.assertEqual(a.scoreboard(sid)["totals"]["A"], 3)

    def test_hint_then_two_words_is_two(self):
        a, sid = fresh()
        a.request_hint(sid, "A", "shan_shui", [1, 1], "h")
        a.handin(sid, "A", "shan_shui", "山水", "1")
        a.handin(sid, "A", "shui_che", "水车", "2")
        self.assertEqual(a.scoreboard(sid)["totals"]["A"], 2)
        cells = a.scoreboard(sid)["teams"]["A"]["cells"]
        self.assertEqual(cells["1,1"]["provenance"], "hint")
        self.assertEqual(cells["1,1"]["earned"], 0)

    def test_same_letter_other_cell_does_not_share_credit(self):
        # 水 appears in (1,1) only here, but the rule is keyed by cell; two
        # endpoints that happen to hold identical text still score separately
        a, sid = fresh()
        a.handin(sid, "A", "shan_shui", "山水", "1")
        a.handin(sid, "A", "shui_che", "水车", "2")
        cells = a.scoreboard(sid)["teams"]["A"]["cells"]
        # only 3 cells exist; each earned once, total 3, never 4
        self.assertEqual(sum(c["earned"] for c in cells.values()), 3)

    def test_invalid_first_then_correct(self):
        a, sid = fresh()
        a.handin(sid, "A", "shan_shui", "云水", "1")
        a.handin(sid, "A", "shui_che", "水车", "2")
        b = a.scoreboard(sid)
        self.assertEqual(b["totals"]["A"], 2)
        self.assertNotIn("1,0", b["teams"]["A"]["cells"])  # 山 never earned
        self.assertEqual(b["teams"]["A"]["cells"]["1,1"]["earned"], 1)

    def test_zero_point_cell_lowers_total(self):
        a, sid = fresh(cell_points={"1,0": 0})
        a.handin(sid, "A", "shan_shui", "山水", "1")
        a.handin(sid, "A", "shui_che", "水车", "2")
        self.assertEqual(a.scoreboard(sid)["totals"]["A"], 2)

    def test_score_table_revision_lowers_total(self):
        a, sid = fresh()
        a.handin(sid, "A", "shan_shui", "山水", "1")
        a.handin(sid, "A", "shui_che", "水车", "2")
        a.make_revision(sid, "base")
        a.revise_score_table(sid, {"1,0": 0}, new_revision="zero-shan")
        self.assertEqual(a.scoreboard(sid, "base")["totals"]["A"], 3)
        self.assertEqual(a.scoreboard(sid, "zero-shan")["totals"]["A"], 2)
        d = a.diff(sid, "base", "zero-shan")
        self.assertEqual(d["teams"]["A"]["change"], -1)


class TestRace(unittest.TestCase):
    def test_first_correct_wins_cell(self):
        a, sid = fresh(teams=("A", "B"))
        a.handin(sid, "A", "shui_che", "水车", "a")
        a.handin(sid, "B", "shui_che", "水车", "b")
        b = a.scoreboard(sid)
        self.assertEqual(b["totals"], {"A": 2, "B": 0})
        self.assertEqual(b["cell_owner"]["1,1"], "A")
        self.assertEqual(b["cell_owner"]["2,1"], "A")

    def test_public_hint_disqualifies_for_all(self):
        a, sid = fresh(teams=("A", "B"))
        a.request_hint(sid, "A", "shan_shui", [1, 1], "h")
        a.handin(sid, "B", "shan_shui", "山水", "b1")
        a.handin(sid, "B", "shui_che", "水车", "b2")
        b = a.scoreboard(sid)
        # B only gets 山 (1,0); 水 was publicly revealed, 车 dragged from
        # the revealed crossing? No: 车 is B's own endpoint -> earned.
        self.assertEqual(b["totals"]["B"], 2)
        self.assertEqual(b["teams"]["B"]["cells"]["1,1"]["provenance"],
                         "cross")
        self.assertEqual(b["teams"]["B"]["cells"]["1,1"]["earned"], 0)

    def test_restart_preserves_totals(self):
        a, sid = fresh(teams=("A", "B"))
        a.handin(sid, "A", "shui_che", "水车", "a")
        before = a.scoreboard(sid)["totals"]
        info = a.restart(sid)
        self.assertEqual(info["totals"], before)

    def test_replay_original_adds_nothing(self):
        a, sid = fresh(teams=("A", "B"))
        a.handin(sid, "A", "shui_che", "水车", "a")
        ev = [e for e in a.log(sid) if e.get("action_id") == "a"][0]
        before = a.scoreboard(sid)["totals"]
        res = a.replay_handin(sid, ev)
        self.assertEqual(res["added_points"], 0)
        self.assertEqual(a.scoreboard(sid)["totals"], before)


class TestJudgeRevision(unittest.TestCase):
    def test_rerejudicate_original(self):
        a, sid = fresh()
        a.request_hint(sid, "A", "shan_shui", [1, 1], "h")
        a.handin(sid, "A", "shan_shui", "云水", "orig")
        a.handin(sid, "A", "shui_che", "水车", "orig2")
        before = a.scoreboard(sid)["totals"]["A"]
        self.assertEqual(before, 1)
        a.judge_revise(sid, "shan_shui", ["云水"], new_revision="fix1")
        after = a.scoreboard(sid)["totals"]["A"]
        self.assertEqual(after, 2)
        # original action is still in the log
        kept = [e for e in a.log(sid) if e.get("word") == "云水"]
        self.assertEqual(len(kept), 1)
        # judge can trace the delta between revisions
        d = a.diff(sid, "r0", "fix1")
        self.assertEqual(d["teams"]["A"]["change"], 2)


class TestSealed(unittest.TestCase):
    def test_conceal_then_reveal(self):
        a, sid = fresh(mode="sealed", teams=("A", "B"))
        a.seal(sid, "A", "shan_shui", "山水", "a1")
        a.seal(sid, "A", "shui_che", "水车", "a2")
        a.seal(sid, "B", "shan_shui", "山水", "b1")
        a.seal(sid, "B", "shui_che", "水车", "b2")
        view = a.scoreboard(sid, viewer="A")
        self.assertTrue(view["concealed"])
        self.assertEqual(set(view["teams"].keys()), {"A"})
        a.reveal(sid)
        b = a.scoreboard(sid)
        self.assertEqual(b["totals"], {"A": 3, "B": 3})
        self.assertFalse(b["concealed"])

    def test_sealed_replay_no_extra_points(self):
        a, sid = fresh(mode="sealed", teams=("A",))
        a.seal(sid, "A", "shui_che", "水车", "s1")
        a.reveal(sid)
        before = a.scoreboard(sid)["totals"]
        ev = [e for e in a.log(sid) if e.get("action_id") == "s1"][0]
        res = a.replay_handin(sid, ev)
        self.assertEqual(res["added_points"], 0)
        self.assertEqual(a.scoreboard(sid)["totals"], before)

    def test_tiebreak_ranking_stable(self):
        a, sid = fresh(mode="sealed", teams=("A", "B"))
        a.seal(sid, "A", "shan_shui", "山水", "a1")
        a.seal(sid, "B", "shui_che", "水车", "b1")
        a.reveal(sid)
        b = a.scoreboard(sid)
        self.assertEqual(b["totals"], {"A": 2, "B": 2})
        # equal score/solved/hints -> earlier last_final_seq wins
        ranks = {r["team"]: r["rank"] for r in b["ranking"]}
        self.assertEqual(ranks["A"], 1)


class TestServer(unittest.TestCase):
    def test_http_end_to_end(self):
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from xword import server

        server.ARENA = Arena(tempfile.mkdtemp())
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()

        def post(path, body):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(body).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())

        def get(path):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
                return json.loads(r.read())

        try:
            info = post("/sessions", {"preset": "shan-shui", "mode": "race",
                                      "teams": ["A", "B"]})
            sid = info["session_id"]
            post(f"/sessions/{sid}/hint",
                 {"team": "A", "entry": "shan_shui", "cell": [1, 1],
                  "action_id": "h"})
            post(f"/sessions/{sid}/handin",
                 {"team": "A", "entry": "shan_shui", "word": "山水",
                  "action_id": "x1"})
            post(f"/sessions/{sid}/handin",
                 {"team": "A", "entry": "shui_che", "word": "水车",
                  "action_id": "x2"})
            board = get(f"/sessions/{sid}/board")
            self.assertEqual(board["totals"]["A"], 2)
            rev = post(f"/sessions/{sid}/revision", {"label": "final"})
            self.assertEqual(rev["revision"], "final")
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
