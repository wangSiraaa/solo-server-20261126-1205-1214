"""Automated two-terminal / server-restart demonstration.

It starts the real HTTP server as a subprocess, drives it as terminals A and B,
races two claims for one last sticker, then kills and restarts the server.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .client import ClientError, request
from .scenarios import late_seven_config, main_config, race_config

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("BINGO_E2E_PORT", "8791"))
URL = f"http://127.0.0.1:{PORT}"


def wait_health(timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(URL + "/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.15)
    raise RuntimeError(f"server did not become healthy: {last!r}")


def start_server(state: Path, log: Any) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "bingo", "serve", "--port", str(PORT), "--state", str(state)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    wait_health()
    return proc


def act(action: str, **params: Any) -> Any:
    return request(URL, action, params)


def expect_error(code: str, fn, label: str) -> ClientError:
    try:
        fn()
    except ClientError as exc:
        if not str(exc).startswith(code):
            raise AssertionError(f"{label}: 期望 {code}，实际 {exc}") from exc
        print(f"  ✓ {label} -> {code}")
        return exc
    raise AssertionError(f"{label}: 应当被拒绝 {code}，但成功了")


def reveal_stamp(gid: str, card: str, pid: str, n: int) -> None:
    revealed = act("reveal.next", game_id=gid)
    assert revealed["number"] == n, (revealed, n)
    act("stamp", game_id=gid, card_id=card, participant_id=pid, number=n, terminal_id=f"terminal-{pid}")
    print(f"  第 {revealed['beat']} 拍揭 {n}，{pid} 已在对应格盖戳")


def run_main(gid: str) -> None:
    print("\n=== 1) 基准场：主对角 2-5-7，阿甲先领，阿乙领走最后一枚 ===")
    act("game.create", **main_config(gid))
    act("window.open", game_id=gid, window_id="W1")

    for n in (2, 5, 7):
        reveal_stamp(gid, "card-A", "pA", n)

    call_a = act(
        "call.create",
        game_id=gid,
        card_id="card-A",
        participant_id="pA",
        line_id="D1",
        terminal_id="terminal-A",
    )
    prize_a = act(
        "claim.issue",
        game_id=gid,
        call_id=call_a["id"],
        participant_id="pA",
        claim_code="2468",
        terminal_id="terminal-A",
    )
    print(f"  ✓ 阿甲在第 {call_a['beat']} 拍喊 D1，领到真实编号贴 {prize_a['sticker']['id']}")

    print("\n=== 2) 换角/旧版：换角前的章与旧卡不能凑线 ===")
    act("stamp", game_id=gid, card_id="card-B", participant_id="pB", number=2)
    act("stamp", game_id=gid, card_id="card-B", participant_id="pB", number=5)
    swap = act(
        "corner.swap",
        game_id=gid,
        card_id="card-B",
        participant_id="pB",
        position="TR",
        block_id="CB-3",
        terminal_id="terminal-B",
    )
    print(f"  ✓ 阿乙把 TR 旧角 2 换成给定角块 3；旧章 {swap['swap']['voided_stamp_id']} 作废")
    expect_error(
        "CARD_VERSION_STALE",
        lambda: act(
            "call.create",
            game_id=gid,
            card_id="card-B",
            participant_id="pB",
            card_version=1,
            line_id="D2",
        ),
        "拿换角前 v1 旧版喊线",
    )
    expect_error(
        "RETIRED_CORNER_NUMBER",
        lambda: act("stamp", game_id=gid, card_id="card-B", participant_id="pB", number=2),
        "给换下的 2 回旧角补章",
    )

    reveal_stamp(gid, "card-B", "pB", 1)
    reveal = act("reveal.next", game_id=gid)
    assert reveal["number"] == 3
    act("stamp", game_id=gid, card_id="card-B", participant_id="pB", number=3)
    print(f"  第 {reveal['beat']} 拍揭 3，新 TR 角盖妥；换角前未移动的中宫 5 仍在原斜线 D2 上")

    call_b = act(
        "call.create",
        game_id=gid,
        card_id="card-B",
        participant_id="pB",
        line_id="D2",
        terminal_id="terminal-B",
    )
    prize_b = act(
        "claim.issue",
        game_id=gid,
        call_id=call_b["id"],
        participant_id="pB",
        claim_code="1357",
        terminal_id="terminal-B",
    )
    print(f"  ✓ 阿乙用当前 v2 卡喊 D2，领走最后一枚 {prize_b['sticker']['id']}")
    report = act("stickers.list", game_id=gid)
    assert report["remaining"] == 0
    print("  奖箱报告：" + json.dumps({s["id"]: s["participant_id"] for s in report["stickers"]}, ensure_ascii=False))


def run_late7(gid: str) -> None:
    print("\n=== 3) 反例场：7 延后；窗关后才喊，线成立也不能记该窗已领 ===")
    act("game.create", **late_seven_config(gid))
    act("window.open", game_id=gid)
    for n in (2, 5, 1):
        reveal_stamp(gid, "card-L", "pA", n)

    err = expect_error(
        "LINE_INCOMPLETE_AT_CALL",
        lambda: act(
            "call.create",
            game_id=gid,
            card_id="card-L",
            participant_id="pA",
            line_id="D1",
        ),
        "7 尚未揭出时喊 D1",
    )
    print(f"    缺格事实：{json.dumps(err.details['missing'], ensure_ascii=False)}")
    act("window.close", game_id=gid)
    reveal_stamp(gid, "card-L", "pA", 7)
    audit = act("card.audit", game_id=gid, card_id="card-L")
    assert audit["lines"]["D1"]["complete"]
    expect_error(
        "WINDOW_CLOSED",
        lambda: act(
            "call.create",
            game_id=gid,
            card_id="card-L",
            participant_id="pA",
            line_id="D1",
        ),
        "三格盖妥后才在关闭的窗外喊线",
    )
    print("  → 可给未得贴者看：窗闭时间/beat、当时缺 7、现在虽 complete 但无 redemption")


def run_race(gid: str) -> None:
    print("\n=== 4) 同拍争最后一枚：两终端屏障齐发，后台锁内串行 ===")
    act("game.create", **race_config(gid))
    act("window.open", game_id=gid)
    for n in (1, 2, 3):
        reveal = act("reveal.next", game_id=gid)
        assert reveal["number"] == n
        for card, pid in (("card-R1", "pA"), ("card-R2", "pB")):
            act("stamp", game_id=gid, card_id=card, participant_id=pid, number=n)
    c1 = act("call.create", game_id=gid, card_id="card-R1", participant_id="pA", line_id="R1")
    c2 = act("call.create", game_id=gid, card_id="card-R2", participant_id="pB", line_id="C2")

    barrier = threading.Barrier(2)

    def claim(call_id: str, pid: str, code: str):
        barrier.wait()
        return request(URL, "claim.issue", {
            "game_id": gid,
            "call_id": call_id,
            "participant_id": pid,
            "claim_code": code,
            "request_id": f"e2e-{call_id}",
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, c1["id"], "pA", "2468"), pool.submit(claim, c2["id"], "pB", "1357")]
        won: list[str] = []
        lost: list[str] = []
        for fut in futures:
            try:
                result = fut.result(timeout=8)
                won.append(f"{result['sticker']['id']}->{result['sticker']['participant_id']}")
            except ClientError as exc:
                lost.append(str(exc))
    assert len(won) == 1 and len(lost) == 1, (won, lost)
    print(f"  ✓ 得贴：{won[0]}")
    print(f"  ✓ 未得贴原因：{lost[0].splitlines()[0]}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bingo-e2e-") as td:
        state = Path(td) / "state.json"
        log_path = Path(td) / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            proc = start_server(state, log)
            try:
                run_main("demo-main")
                main_report_before = act("stickers.list", game_id="demo-main")

                print("\n=== 5) 杀掉并重启后台，核对每枚贴的持有人 ===")
                proc.terminate()
                proc.wait(timeout=5)
                proc = start_server(state, log)
                report = act("stickers.list", game_id="demo-main")
                assert report == main_report_before
                for s in report["stickers"]:
                    print(f"  ✓ 重启后 {s['id']} -> {s['participant_id']} / {s['redemption']['call_id']}")

                # Old version remains rejected after restart.
                expect_error(
                    "CARD_VERSION_STALE",
                    lambda: act(
                        "call.create",
                        game_id="demo-main",
                        card_id="card-B",
                        participant_id="pB",
                        card_version=1,
                        line_id="D2",
                    ),
                    "重启后再拿 v1 旧版试兑",
                )

                run_late7("demo-late7")
                run_race("demo-race")

                print("\n=== 6) 完场封存带重播 ===")
                replay = act("game.finish", game_id="demo-main")
                assert replay["commitment_valid"] is True
                assert replay["sealed_tape"] == main_config("demo-main")["tape"]
                again = act("tape.replay", game_id="demo-main")
                assert again["beat"] == replay["beat"]
                print(f"  ✓ 封存哈希有效：{again['tape_commitment'][:16]}…")
                print(f"  ✓ 重播揭号轨迹 {again['revealed_numbers']}，beat 不增加")
                print("\n全部 E2E 断言通过。")
                return 0
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
