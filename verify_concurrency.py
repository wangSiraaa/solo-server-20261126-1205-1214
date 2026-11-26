#!/usr/bin/env python3
"""Network-level consistency checks for concurrent edit/move arrival.

This is not part of the six-post product demo; it starts a temporary service
and hammers the HTTP API so SQLite's write transaction and optimistic version
contract are exercised over real sockets.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("CONCURRENCY_PORT", "8812"))
BASE = f"http://127.0.0.1:{PORT}"


def request(method: str, path: str, token: str | None = None, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def wait_health() -> None:
    for _ in range(100):
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "concurrency.db"
        env = os.environ.copy()
        env["FORUM_DB"] = str(db)
        env["PORT"] = str(PORT)
        proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            wait_health()
            request("POST", "/users", payload={"id": "u1", "username": "conc", "password": "pass1234"})
            _, login = request("POST", "/auth/login", payload={"username": "conc", "password": "pass1234"})
            token = login["token"]
            request("POST", "/groups", token, {"id": "g1", "slug": "g1", "title": "G1"})
            request("POST", "/groups", token, {"id": "g2", "slug": "g2", "title": "G2"})
            request("POST", "/topics", token, {"id": "t1", "group_id": "g1", "title": "T1"})
            request("POST", "/topics", token, {"id": "t2", "group_id": "g2", "title": "T2"})
            request("POST", "/posts", token, {"id": "moving", "topic_id": "t1", "body": "before"})
            request("POST", "/posts", token, {"id": "race-edit", "topic_id": "t1", "body": "race"})

            barrier = threading.Barrier(2)
            results: dict[str, tuple[int, dict]] = {}

            def move() -> None:
                barrier.wait()
                results["move"] = request("POST", "/posts/moving/move", token,
                                          {"target_group_id": "g2", "target_topic_id": "t2"})

            def edit() -> None:
                barrier.wait()
                results["edit"] = request("PATCH", "/posts/moving", token,
                                          {"expected_version": 1, "body": "important supplement"})

            t1 = threading.Thread(target=move)
            t2 = threading.Thread(target=edit)
            t1.start(); t2.start(); t1.join(); t2.join()

            move_status, move_body = results["move"]
            edit_status, edit_body = results["edit"]
            assert move_status == 200, results["move"]
            assert edit_status == 200, results["edit"]
            status, fetched = request("GET", "/posts/moving", token)
            post = fetched["post"]
            assert post["group_id"] == "g2", post
            assert post["version"] == 2, post
            assert post["body"] == "important supplement", post
            assert len(move_body["move"]["moved_post_ids"]) == 1

            # Ten writers all believe they are editing v1. Exactly one may win;
            # every loser gets a structured 409 and no update is lost silently.
            race_barrier = threading.Barrier(10)
            race_results: list[tuple[int, dict]] = []
            lock = threading.Lock()

            def racing_edit(i: int) -> None:
                race_barrier.wait()
                result = request("PATCH", "/posts/race-edit", token,
                                 {"expected_version": 1, "body": f"writer {i}"})
                with lock:
                    race_results.append(result)

            threads = [threading.Thread(target=racing_edit, args=(i,)) for i in range(10)]
            for t in threads: t.start()
            for t in threads: t.join()
            wins = [r for r in race_results if r[0] == 200]
            conflicts = [r for r in race_results if r[0] == 409]
            server_errors = [r for r in race_results if r[0] >= 500]
            assert len(wins) == 1, race_results
            assert len(conflicts) == 9, race_results
            assert not server_errors, server_errors
            _, final = request("GET", "/posts/race-edit", token)
            assert final["post"]["version"] == 2
            assert final["post"]["body"].startswith("writer ")

            print("PASS: simultaneous move/edit both commit; optimistic edit races have one win and nine 409s")
        finally:
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
            if proc.returncode not in (0, -15):
                print(out)


if __name__ == "__main__":
    main()
