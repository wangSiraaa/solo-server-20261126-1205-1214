#!/usr/bin/env python3
"""通过真实 HTTP 请求核对题目中的三格水洼、两种约定、教案约束与重启持久化。

运行：
    python3 scripts/e2e.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Client:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AssertionError(f"{method} {path} -> {exc.code}: {detail}") from exc

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("POST", path, body or {})

    def patch(self, path: str, body: Dict[str, Any]) -> Any:
        return self.request("PATCH", path, body)


def wait_health(client: Client, proc: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        try:
            if client.get("/health").get("ok"):
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.15)
    raise RuntimeError(f"server did not become healthy: {last}")


def start_server(data_file: Path, port: int, log_file: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["WHITEBOARD_HOST"] = "127.0.0.1"
    env["PORT"] = str(port)
    env["WHITEBOARD_DATA"] = str(data_file)
    log = log_file.open("ab")
    return subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def cells_of(board: Dict[str, Any]) -> Any:
    return board["cells"]


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)
    print(f"  ✅ {message}")


def make_jia_yi(api: Client, sid: str, prefix: str = "") -> None:
    api.post(f"/api/sessions/{sid}/groups", {"id": f"{prefix}jia_group", "name": "甲组"})
    api.post(f"/api/sessions/{sid}/groups", {"id": f"{prefix}yi_group", "name": "乙组"})
    api.post(
        f"/api/sessions/{sid}/drafts",
        {
            "id": f"{prefix}jia",
            "group_id": f"{prefix}jia_group",
            "title": "甲：从第一格涂蓝",
            "author": "甲",
            "score": 5,
            "steps": [{"op": "paint", "at": [0, 0], "color": "blue"}],
        },
    )
    api.post(
        f"/api/sessions/{sid}/drafts",
        {
            "id": f"{prefix}yi",
            "group_id": f"{prefix}yi_group",
            "title": "乙：去掉二、三格挡线",
            "author": "乙",
            "score": 3,
            "steps": [{"op": "remove_wall", "edge": [[0, 1], [0, 2]]}],
        },
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="whiteboard-e2e-") as td:
        root = Path(td)
        data_file = root / "state" / "whiteboard.json"
        log_file = root / "server.log"
        port = free_port()
        api = Client(f"http://127.0.0.1:{port}")
        proc = start_server(data_file, port, log_file)
        try:
            wait_health(api, proc)
            print("\n[1/6] 老师会话与“乙在甲开板前已合入”")
            teacher = api.post("/api/teacher-sessions", {"name": "王老师"})
            check(bool(teacher["id"]), "创建第一位老师会话")

            before = api.post(
                "/api/sessions",
                {
                    "id": "before_open",
                    "teacher_id": teacher["id"],
                    "title": "缺口先于甲开板",
                    "convention": "replay",
                    "rows": 1,
                    "cols": 3,
                    "walls": [[[0, 1], [0, 2]]],
                },
            )
            check(before["current_board"]["cells"] == [["white", "white", "white"]], "初始一行三格均为白色")
            check(before["current_board"]["walls"]["0,1|0,2"] is True, "初始第二、三格之间有挡线")

            api.post("/api/sessions/before_open/groups", {"id": "b_before_group", "name": "乙组"})
            api.post(
                "/api/sessions/before_open/drafts",
                {
                    "id": "b_before",
                    "group_id": "b_before_group",
                    "score": 3,
                    "steps": [{"op": "remove_wall", "edge": "0,1|0,2"}],
                },
            )
            early = api.post("/api/sessions/before_open/merge/publish", {"draft_ids": ["b_before"]})
            check(early["published_board"]["walls"]["0,1|0,2"] is False, "乙的缺口已正式合入当前总板")

            api.post("/api/sessions/before_open/groups", {"id": "a_after_group", "name": "甲组"})
            preview = api.post(
                "/api/groups/a_after_group/preview",
                {"steps": [{"op": "paint", "at": [0, 0], "color": "blue"}]},
            )
            check(
                cells_of(preview["preview_board"]) == [["blue", "blue", "blue"]],
                "甲在缺口之后开板：预览中的蓝色水洼漫过三格",
            )
            api.post(
                "/api/sessions/before_open/drafts",
                {
                    "id": "a_after",
                    "group_id": "a_after_group",
                    "score": 5,
                    "steps": [{"op": "paint", "at": [0, 0], "color": "blue"}],
                },
            )
            done_before = api.post("/api/sessions/before_open/merge/publish", {"draft_ids": ["a_after"]})
            check(
                cells_of(done_before["published_board"]) == [["blue", "blue", "blue"]],
                "正式合班依据是甲开板时已无挡线的新总板",
            )

            print("\n[2/6] 甲交稿后乙才改边界：小组预览仍停在自己的所见")
            api.post(
                "/api/sessions",
                {
                    "id": "patch_lesson",
                    "teacher_id": teacher["id"],
                    "title": "照样贴图合班",
                    "convention": "patch",
                    "rows": 1,
                    "cols": 3,
                    "walls": [[[0, 1], [0, 2]]],
                },
            )
            make_jia_yi(api, "patch_lesson")
            jia_group = api.get("/api/groups/jia_group")
            check(cells_of(jia_group["board"]) == [["blue", "blue", "white"]], "甲小组预览只见蓝一、二格，第三格仍白")
            yi_group = api.get("/api/groups/yi_group")
            check(cells_of(yi_group["board"]) == [["white", "white", "white"]], "乙小组看不到甲未发表的蓝色")

            print("\n[3/6] 尚未入选的乙稿不能提前改变总板")
            plan_jia_only = api.post("/api/sessions/patch_lesson/merge/plan", {"draft_ids": ["jia"]})
            check(plan_jia_only["best"]["board"]["walls"]["0,1|0,2"] is True, "只推演甲稿时，计划板仍保留挡线")
            master_after_plan = api.get("/api/sessions/patch_lesson")["current_board"]
            check(master_after_plan["walls"]["0,1|0,2"] is True, "plan 是只读推演，老师当前总板的挡线仍在")

            print("\n[4/6] 贴图约定的最优合法合班与真实改动")
            patch_plan = api.post("/api/sessions/patch_lesson/merge/plan", {})
            check(patch_plan["best"]["score"] == 8, "甲5分+乙3分=8分，为最高总分")
            check(patch_plan["best"]["draft_ids"] == ["jia", "yi"], "两份稿件同时入选")
            check(
                cells_of(patch_plan["best"]["board"]) == [["blue", "blue", "white"]],
                "贴图保留甲亲眼所见的成果：乙去掉挡线，但蓝色不回溯漫入第三格",
            )
            jia_report = next(r for r in patch_plan["best"]["draft_reports"] if r["draft_id"] == "jia")
            check(
                set(jia_report["real_changes"]["cells"]) == {"0,0", "0,1"},
                "甲稿真实改变第一、二格",
            )
            yi_report = next(r for r in patch_plan["best"]["draft_reports"] if r["draft_id"] == "yi")
            check("0,1|0,2" in yi_report["real_changes"]["walls"], "乙稿真实去掉第二、三格挡线")

            published = api.post("/api/sessions/patch_lesson/merge/publish", {})
            published_board_id = published["published_board_id"] if "published_board_id" in published else published["current_board_id"]
            merge_id = published["merge"]["id"]
            check(
                cells_of(published["published_board"]) == [["blue", "blue", "white"]],
                "签字发表的贴图总板为蓝、蓝、白且无挡线",
            )
            check(
                all(d["decision"] == "accepted" for d in published["dispositions"]),
                "两份稿件去向均为 accepted",
            )

            later_group = api.post("/api/sessions/patch_lesson/groups", {"name": "后来组"})
            check(later_group["base_board_id"] == published_board_id, "后来的小组必须从新签下的总板编号开课")

            print("\n[5/6] 同一套稿件在重演约定与教案禁令下的差异")
            api.post(
                "/api/sessions",
                {
                    "id": "patch_constraints",
                    "title": "贴图约定禁令对照",
                    "convention": "patch",
                    "rows": 1,
                    "cols": 3,
                    "walls": [[[0, 1], [0, 2]]],
                },
            )
            make_jia_yi(api, "patch_constraints", prefix="pc_")
            api.post(
                "/api/sessions",
                {
                    "id": "replay_lesson",
                    "title": "重演笔法合班",
                    "convention": "replay",
                    "rows": 1,
                    "cols": 3,
                    "walls": [[[0, 1], [0, 2]]],
                    "allowed_orders": [["rp_yi", "rp_jia"], ["rp_jia", "rp_yi"]],
                },
            )
            make_jia_yi(api, "replay_lesson", prefix="rp_")
            ba = api.post("/api/sessions/replay_lesson/merge/plan", {"order": ["rp_yi", "rp_jia"]})["best"]
            ab = api.post("/api/sessions/replay_lesson/merge/plan", {"order": ["rp_jia", "rp_yi"]})["best"]
            check(cells_of(ba["board"]) == [["blue", "blue", "blue"]], "先重演乙开缺口、再重演甲涂色：水洼漫过第三格")
            check(cells_of(ab["board"]) == [["blue", "blue", "white"]], "先重演甲涂色、再开缺口：第三格仍白")

            api.patch(
                "/api/sessions/replay_lesson/lesson",
                {"forbidden": [{"cell": [0, 2], "color": "blue"}]},
            )
            banned_ba = api.post(
                "/api/sessions/replay_lesson/merge/plan",
                {"allowed_orders": [["rp_yi", "rp_jia"]]},
            )
            check(
                banned_ba["best"] is not None and banned_ba["best"]["draft_ids"] == ["rp_jia"],
                "第三格禁蓝且规定乙先甲后：双稿方案违法，最高分只剩甲稿5分",
            )
            check(banned_ba["best"]["score"] == 5, "重演约定受第三格禁蓝影响")

            api.patch(
                "/api/sessions/patch_constraints/lesson",
                {"forbidden": [{"cell": [0, 2], "color": "blue"}]},
            )
            patch_banned3 = api.post("/api/sessions/patch_constraints/merge/plan", {})
            check(
                patch_banned3["best"] is not None and patch_banned3["best"]["score"] == 8,
                "贴图甲稿本来没涂第三格：第三格禁蓝不影响其贴图组合",
            )

            api.patch("/api/sessions/patch_constraints/lesson", {"forbidden": []})
            restored = api.post("/api/sessions/patch_constraints/merge/plan", {})
            check(restored["best"]["score"] == 8, "还原第三格教案后，甲、乙双稿重新合法")
            api.patch(
                "/api/sessions/patch_constraints/lesson",
                {"forbidden": [{"cell": [0, 1], "color": "blue"}]},
            )
            banned_cell2 = api.post("/api/sessions/patch_constraints/merge/plan", {})
            check(
                banned_cell2["best"] is not None
                and banned_cell2["best"]["draft_ids"] == ["pc_yi"],
                "第二格禁蓝后，甲原来涂蓝一、二格的成果不能照发，只能选乙稿",
            )
            check(banned_cell2["best"]["score"] == 3, "合法最高总分为乙稿3分")

            print("\n[6/6] 重启服务，并用另一位老师会话核对")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            proc = start_server(data_file, port, log_file)
            wait_health(api, proc)

            teacher2 = api.post("/api/teacher-sessions", {"name": "复查老师"})
            check(teacher2["id"] != teacher["id"], "重启后创建了另一位老师会话")
            persisted_board = api.get(f"/api/boards/{published_board_id}")
            check(cells_of(persisted_board) == [["blue", "blue", "white"]], "重启后已发表总板图像仍在")
            check(persisted_board["walls"]["0,1|0,2"] is False, "重启后乙去掉的挡线状态仍在")
            persisted_merge = api.get(f"/api/merges/{merge_id}")
            check(
                persisted_merge["selected_draft_ids"] == ["jia", "yi"],
                "重启后合班记录仍写明甲、乙两稿入选",
            )
            draft_jia = api.get("/api/drafts/jia")
            check(draft_jia["status"] == "published", "甲稿去向仍为 published")
            draft_yi = api.get("/api/drafts/yi")
            check(draft_yi["published_board_id"] == published_board_id, "乙稿可追溯到签字总板编号")
            sessions = api.get("/api/sessions")
            check(any(s["id"] == "patch_lesson" for s in sessions), "另一位老师能列出重启前的课节")

            print("\n🎉 E2E 通过：水洼、缺口、两种约定、教案禁令、最优分数、稿件去向和重启持久化均已核对。")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
