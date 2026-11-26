#!/usr/bin/env python3
"""End-to-end HTTP demonstration for the dubbing-studio task.

The script starts a real server subprocess, exercises the exact signing
scenario, kills/restarts the server, and checks that duplicate player
confirmations do not create extra heard words.

Usage:
    python3 demo.py
    python3 demo.py --port 8799 --db data/demo.sqlite3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


class Http:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def call(self, method: str, path: str, body: Any = None, token: str | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if token:
            req.add_header("X-Session-Token", token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return exc.code, payload

    def ok(self, method: str, path: str, body: Any = None, **kwargs: Any) -> dict[str, Any]:
        status, payload = self.call(method, path, body=body, **kwargs)
        if status >= 400:
            raise AssertionError(f"{method} {path} -> {status}: {json.dumps(payload, ensure_ascii=False)}")
        return payload


def wait_health(http: Http, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        try:
            status, body = http.call("GET", "/health")
            if status == 200 and body.get("ok"):
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.15)
    raise RuntimeError(f"server did not become healthy: {last}")


def start_server(port: int, db: Path) -> subprocess.Popen:
    db.parent.mkdir(parents=True, exist_ok=True)
    log = open(db.with_suffix(".server.log"), "ab")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--host", "127.0.0.1",
         "--port", str(port), "--db", str(db)],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
    )
    wait_health(Http(f"http://127.0.0.1:{port}"), proc)
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def make_session(http: Http, name: str, role: str) -> dict[str, Any]:
    return http.ok("POST", "/api/sessions", {"name": name, "role": role})


def record_take(http: Http, tokens: dict[str, str], script_id: str, role: str,
                line_index: int, words: list[str], rehearsal_id: str | None = None) -> dict[str, Any]:
    started = http.ok("POST", "/api/recordings/start", {
        "scriptId": script_id,
        "lineIndex": line_index,
        "rehearsalId": rehearsal_id,
    }, token=tokens[role])
    return http.ok("POST", f"/api/recordings/{started['recordingId']}/complete",
                   {"words": words}, token=tokens[role])


def confirm(http: Http, tokens: dict[str, str], role: str, delivery_id: str,
            key: str, start: int, end: int) -> tuple[int, dict[str, Any]]:
    return http.call("POST", "/api/playback-confirmations", {
        "deliveryId": delivery_id,
        "startIndex": start,
        "endIndex": end,
    }, token=tokens[role], headers={"Idempotency-Key": key})


def main_scenario(http: Http) -> dict[str, Any]:
    print("== 1. 建会话/建场/三句台本（每幕上限 4 句） ==")
    director = make_session(http, "导演", "director")
    jia = make_session(http, "甲", "jia")
    yi = make_session(http, "乙", "yi")
    bing = make_session(http, "丙", "bing")
    tokens = {"director": director["token"], "jia": jia["token"],
              "yi": yi["token"], "bing": bing["token"]}
    sessions = {"jia": jia, "yi": yi, "bing": bing}

    studio = http.ok("POST", "/api/studios", {"name": "同棚连排示范"}, token=director["token"])
    studio_id = studio["studioId"]
    script = http.ok("POST", f"/api/studios/{studio_id}/script", {
        "title": "夜台三句",
        "lines": [
            {"role": "jia", "label": "甲"},
            {"role": "yi", "label": "乙", "hearing": {"cueLineIndex": 0, "startIndex": 2, "endIndex": 4}},
            {"role": "bing", "label": "丙", "hearing": {"cueLineIndex": 1, "startIndex": 1, "endIndex": 3}},
        ],
    }, token=director["token"])
    script_id = script["scriptId"]
    assert script["spec"]["maxLines"] == 4 and script["spec"]["maxTakesPerLine"] == 3

    run = http.ok("POST", f"/api/studios/{studio_id}/rehearsals", {
        "scriptId": script_id,
        "roster": {role: sessions[role]["sessionId"] for role in ("jia", "yi", "bing")},
        "initialCue": {"lineIndex": 0, "policy": "导演发送具体录音；中途再发即切换耳返"},
    }, token=director["token"])
    run_id = run["rehearsalId"]
    print(f"studio={studio_id} script={script_id} rehearsal={run_id}")

    print("== 2. 甲录两遍：开头相同，尾词不同 ==")
    a1 = record_take(http, tokens, script_id, "jia", 0,
                     ["长亭", "雨歇", "风满袖", "归"], run_id)
    a2 = record_take(http, tokens, script_id, "jia", 0,
                     ["长亭", "雨歇", "风满袖", "别"], run_id)
    print(f"A1={a1['recordingId']} words={a1['words']}")
    print(f"A2={a2['recordingId']} words={a2['words']}")

    print("== 3. 乙只实听第一遍末尾区间 [2,4)；第二遍只有发送通知和自报 ==")
    del1 = http.ok("POST", "/api/deliveries", {
        "recordingId": a1["recordingId"], "targetSessionId": yi["sessionId"],
    }, token=director["token"])
    status, conf1 = confirm(http, tokens, "yi", del1["deliveryId"], "a1-tail", 2, 4)
    assert status == 201, conf1

    # Director sends take 2 before B starts: send record must not be evidence.
    del2 = http.ok("POST", "/api/deliveries", {
        "recordingId": a2["recordingId"], "targetSessionId": yi["sessionId"],
    }, token=director["token"])
    assert del2["supersededDeliveryIds"] == [del1["deliveryId"]]
    self_report = http.ok("POST", "/api/self-reports", {
        "studioId": studio_id, "heardRecordingId": a2["recordingId"], "allWords": True,
    }, token=yi["token"])
    assert self_report["admissible"] is False

    b1_started = http.ok("POST", "/api/recordings/start", {
        "scriptId": script_id, "lineIndex": 1, "rehearsalId": run_id,
    }, token=yi["token"])
    b1_snapshot_count = len(b1_started["listeningSnapshot"]["confirmations"])
    b1 = http.ok("POST", f"/api/recordings/{b1_started['recordingId']}/complete",
                 {"words": ["乙起", "乙承", "乙转"]}, token=yi["token"])
    assert b1_snapshot_count == 1

    c_del1 = http.ok("POST", "/api/deliveries", {
        "recordingId": b1["recordingId"], "targetSessionId": bing["sessionId"],
    }, token=director["token"])
    status, _ = confirm(http, tokens, "bing", c_del1["deliveryId"], "c1-b", 1, 3)
    assert status == 201
    c1 = record_take(http, tokens, script_id, "bing", 2,
                     ["丙一", "丙二", "丙三", "丙四"], run_id)

    bad = http.ok("POST", "/api/sheets/validate", {
        "studioId": studio_id, "scriptId": script_id,
        "recordingIds": [a2["recordingId"], b1["recordingId"], c1["recordingId"]],
    }, token=director["token"])
    assert bad["signable"] is False
    b_check = bad["hearingChecks"][0]
    assert b_check["status"] == "fail"
    assert b_check["selectedCueRecordingId"] == a2["recordingId"]
    print("核第二遍：", b_check["status"], "|", b_check["reasons"][0])

    print("== 4. 登记导演互认后，另一张候选单可签；随后撤掉唯一互认 ==")
    mrec = http.ok("POST", f"/api/studios/{studio_id}/mutual-recognitions", {
        "left": {"recordingId": a1["recordingId"], "startIndex": 2, "endIndex": 4},
        "right": {"recordingId": a2["recordingId"], "startIndex": 2, "endIndex": 4},
        "note": "两遍尾段演法经导演确认互认",
    }, token=director["token"])
    alt = http.ok("POST", "/api/sheets", {
        "studioId": studio_id, "scriptId": script_id, "rehearsalId": run_id,
        "collectionMode": "rehearsal", "requireClosed": False,
        "title": "依赖互认的候选三句单",
        "recordingIds": [a2["recordingId"], b1["recordingId"], c1["recordingId"]],
    }, token=director["token"])
    assert alt["status"] == "ready" and alt["signable"] is True
    print("撤互认前候选单：", alt["status"])
    revoked = http.ok("POST", f"/api/mutual-recognitions/{mrec['mutualRecognitionId']}/revoke",
                      {}, token=director["token"])
    assert revoked["state"] == "revoked"
    alt_after = http.ok("GET", f"/api/sheets/{alt['sheetId']}")
    assert alt_after["status"] == "pending" and alt_after["signable"] is False
    print("撤互认后候选单：", alt_after["status"])

    print("== 5. 改为实听第二遍；重复送达同一确认，由调用方稍后重启验持久化 ==")
    status, first_tail = confirm(http, tokens, "yi", del2["deliveryId"], "a2-tail", 2, 4)
    assert status == 201, first_tail
    status, duplicate_tail = confirm(http, tokens, "yi", del2["deliveryId"], "a2-tail", 2, 4)
    assert status == 200 and duplicate_tail["idempotentReplay"] is True
    assert (first_tail["confirmation"]["confirmationId"]
            == duplicate_tail["confirmation"]["confirmationId"])
    assert (first_tail["confirmation"]["receivedSeq"]
            == duplicate_tail["confirmation"]["receivedSeq"])
    http.ok("POST", "/api/playback-confirmations", {
        "deliveryId": del2["deliveryId"], "startIndex": 0, "endIndex": 2,
    }, token=yi["token"], headers={"Idempotency-Key": "a2-head"})
    print("重复确认返回同一 seq=", duplicate_tail["confirmation"]["receivedSeq"])

    # Return a continuation closure so the restart can happen before B2 starts.
    def after_restart() -> dict[str, Any]:
        b2_started = http.ok("POST", "/api/recordings/start", {
            "scriptId": script_id, "lineIndex": 1, "rehearsalId": run_id,
        }, token=yi["token"])
        a2_confirmations = [
            c for c in b2_started["listeningSnapshot"]["confirmations"]
            if c["recordingId"] == a2["recordingId"]
        ]
        # One [0,2) and one [2,4); the redelivered tail has the same id/seq.
        assert len(a2_confirmations) == 2, a2_confirmations
        b2 = http.ok("POST", f"/api/recordings/{b2_started['recordingId']}/complete",
                     {"words": ["乙新起", "乙新承", "乙新转"]}, token=yi["token"])

        c_del2 = http.ok("POST", "/api/deliveries", {
            "recordingId": b2["recordingId"], "targetSessionId": bing["sessionId"],
        }, token=director["token"])
        status, _ = confirm(http, tokens, "bing", c_del2["deliveryId"], "c2-b", 1, 3)
        assert status == 201
        c2 = record_take(http, tokens, script_id, "bing", 2,
                         ["丙新一", "丙新二", "丙新三", "丙新四"], run_id)

        closed = http.ok("POST", f"/api/rehearsals/{run_id}/close", {}, token=director["token"])
        assert closed["state"] == "closed"
        main_sheet = http.ok("POST", "/api/sheets", {
            "studioId": studio_id, "scriptId": script_id, "rehearsalId": run_id,
            "collectionMode": "rehearsal", "title": "能签的三句单",
            "recordingIds": [a2["recordingId"], b2["recordingId"], c2["recordingId"]],
        }, token=director["token"])
        assert main_sheet["status"] == "ready"
        signed = http.ok("POST", f"/api/sheets/{main_sheet['sheetId']}/sign", {},
                         token=director["token"])
        assert signed["status"] == "signed"
        queried = http.ok("GET", f"/api/sheets/{signed['sheetId']}")
        assert queried["canonicalHash"] == signed["canonicalHash"]
        matrix = http.ok("GET", f"/api/scripts/{script_id}/casting-matrix",
                         token=tokens["director"])
        assert len(matrix["matrix"]) == 3
        assert any(take["againstCueTakes"] for line in matrix["matrix"] if line["lineIndex"] == 1
                   for take in line["takes"])
        assert queried["recordingIdsInScriptOrder"] == [
            a2["recordingId"], b2["recordingId"], c2["recordingId"]
        ]
        print("能签并已签：", signed["sheetId"], "hash=", signed["canonicalHash"][:16])
        print("交片单引用：", queried["recordingIdsInScriptOrder"])
        return {"studioId": studio_id, "scriptId": script_id, "sheetId": signed["sheetId"],
                "altSheetId": alt["sheetId"], "runId": run_id,
                "hash": signed["canonicalHash"]}

    return {"before_restart": {
        "studioId": studio_id, "scriptId": script_id, "altSheetId": alt["sheetId"],
        "duplicateSeq": duplicate_tail["confirmation"]["receivedSeq"],
    }, "after_restart": after_restart}


def late_confirmation_scenario(http: Http) -> None:
    print("== 8. 迟来确认不能补成开录前经历 ==")
    director = make_session(http, "导演2", "director")
    yi = make_session(http, "乙2", "yi")
    studio = http.ok("POST", "/api/studios", {"name": "迟到确认棚"}, token=director["token"])
    script = http.ok("POST", f"/api/studios/{studio['studioId']}/script", {
        "lines": [
            {"role": "jia"},
            {"role": "yi", "hearing": {"startIndex": 0, "endIndex": 2}},
        ],
    }, token=director["token"])
    jia = make_session(http, "甲2", "jia")
    a = record_take(http, {"director": director["token"], "jia": jia["token"]},
                    script["scriptId"], "jia", 0, ["迟到前词一", "迟到前词二"])
    delivery = http.ok("POST", "/api/deliveries", {
        "recordingId": a["recordingId"], "targetSessionId": yi["sessionId"],
    }, token=director["token"])
    started = http.ok("POST", "/api/recordings/start", {
        "scriptId": script["scriptId"], "lineIndex": 1,
    }, token=yi["token"])
    assert started["listeningSnapshot"]["confirmations"] == []
    status, late = confirm(http, {"yi": yi["token"]}, "yi",
                           delivery["deliveryId"], "late-key", 0, 2)
    assert status == 201
    b = http.ok("POST", f"/api/recordings/{started['recordingId']}/complete",
                {"words": ["乙应答"]}, token=yi["token"])
    validation = http.ok("POST", "/api/sheets/validate", {
        "studioId": studio["studioId"], "scriptId": script["scriptId"],
        "recordingIds": [a["recordingId"], b["recordingId"]],
    }, token=director["token"])
    check = validation["hearingChecks"][0]
    assert validation["signable"] is False
    assert len(check["lateConfirmationsNotCounted"]) == 1
    print("开录快照仍为空；迟到确认 seq=", late["confirmation"]["receivedSeq"], "已排除")


def cross_session_scenario(http: Http) -> None:
    print("== 9. 跨场补录：不同排练挑片，按逐句成本选最低可行清单 ==")
    director = make_session(http, "跨场导演", "director")
    jia = make_session(http, "跨场甲", "jia")
    yi = make_session(http, "跨场乙", "yi")
    bing = make_session(http, "跨场丙", "bing")
    tokens = {"director": director["token"], "jia": jia["token"],
              "yi": yi["token"], "bing": bing["token"]}
    studio = http.ok("POST", "/api/studios", {"name": "跨场补录棚"}, token=director["token"])
    script = http.ok("POST", f"/api/studios/{studio['studioId']}/script", {
        "lines": [
            {"role": "jia"},
            {"role": "yi", "hearing": {"startIndex": 2, "endIndex": 4}},
            {"role": "bing", "hearing": {"startIndex": 1, "endIndex": 3}},
        ],
    }, token=director["token"])
    sid = script["scriptId"]
    run1 = http.ok("POST", f"/api/studios/{studio['studioId']}/rehearsals", {
        "scriptId": sid,
        "roster": {"jia": jia["sessionId"], "yi": yi["sessionId"], "bing": bing["sessionId"]},
    }, token=director["token"])
    run2 = http.ok("POST", f"/api/studios/{studio['studioId']}/rehearsals", {
        "scriptId": sid, "roster": {"jia": jia["sessionId"]},
    }, token=director["token"])

    a1 = record_take(http, tokens, sid, "jia", 0,
                     ["长亭", "雨歇", "风满袖", "归"], run1["rehearsalId"])
    a2 = record_take(http, tokens, sid, "jia", 0,
                     ["长亭", "雨歇", "风满袖", "别"], run2["rehearsalId"])
    d1 = http.ok("POST", "/api/deliveries", {
        "recordingId": a1["recordingId"], "targetSessionId": yi["sessionId"],
    }, token=director["token"])
    status, _ = confirm(http, tokens, "yi", d1["deliveryId"], "cross-b-hear-a1", 2, 4)
    assert status == 201
    b = record_take(http, tokens, sid, "yi", 1,
                    ["乙起", "乙承", "乙转", "乙合"], run1["rehearsalId"])
    d2 = http.ok("POST", "/api/deliveries", {
        "recordingId": b["recordingId"], "targetSessionId": bing["sessionId"],
    }, token=director["token"])
    status, _ = confirm(http, tokens, "bing", d2["deliveryId"], "cross-c-hear-b", 1, 3)
    assert status == 201
    c = record_take(http, tokens, sid, "bing", 2,
                    ["丙一", "丙二", "丙三"], run1["rehearsalId"])

    status, plan = http.call("POST", "/api/pickup-plans", {
        "studioId": studio["studioId"], "scriptId": sid,
        "baselineRecordingIds": [a2["recordingId"], b["recordingId"], c["recordingId"]],
        "rerecordCosts": [80, 20, 100],
    }, token=director["token"])
    assert status == 201, plan
    assert plan["plan"]["best"]["pickupLineIndexes"] == [1]
    assert plan["plan"]["totalCost"] == 20
    print("低成本方案：", plan["plan"]["lowestCostPickupList"], "保留第 3 句", c["recordingId"])

    status, early = http.call("POST", f"/api/pickup-plans/{plan['planId']}/sheet", {},
                              token=director["token"])
    assert status == 409 and early["error"]["code"] == "pickups_incomplete"
    print("未录齐时交片：", early["error"]["code"])

    fulfilled = http.ok("POST", f"/api/pickup-plans/{plan['planId']}/simulate", {},
                        token=director["token"])
    assert fulfilled["state"] == "completed"
    cross_sheet = http.ok("POST", f"/api/pickup-plans/{plan['planId']}/sheet",
                          {"title": "跨场最低成本补录单"}, token=director["token"])
    assert cross_sheet["status"] == "ready"
    signed = http.ok("POST", f"/api/sheets/{cross_sheet['sheetId']}/sign", {},
                     token=director["token"])
    assert signed["status"] == "signed"
    print("补录录齐并签出：", signed["sheetId"],
          "搭桥互认=", fulfilled["bridgeRecognitionIds"])

    cheaper_cue = http.ok("POST", "/api/pickup-plans", {
        "studioId": studio["studioId"], "scriptId": sid,
        "baselineRecordingIds": [a2["recordingId"], b["recordingId"], c["recordingId"]],
        "rerecordCosts": [5, 20, 100],
    }, token=director["token"])
    assert cheaper_cue["plan"]["best"]["pickupLineIndexes"] == [0]
    assert cheaper_cue["plan"]["totalCost"] == 5
    print("若第 1 句重录成本改为 5：最低成本清单变为",
          cheaper_cue["plan"]["best"]["pickupLineIndexes"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--db", default=str(ROOT / "data" / "demo-http.sqlite3"))
    parser.add_argument("--keep-server", action="store_true")
    args = parser.parse_args()

    db = Path(args.db)
    cleanup_paths = [db, Path(str(db) + "-wal"), Path(str(db) + "-shm"),
                     db.with_suffix(".server.log")]
    for p in cleanup_paths:
        if p.exists():
            p.unlink()
    proc = start_server(args.port, db)
    http = Http(f"http://127.0.0.1:{args.port}")
    try:
        scenario = main_scenario(http)
        before = scenario["before_restart"]
        print("== 7. 重启 HTTP 服务和 SQLite，再继续开录/查询 ==")
        stop_server(proc)
        proc = start_server(args.port, db)
        wait_health(http, proc)
        summary = scenario["after_restart"]()
        late_confirmation_scenario(http)
        cross_session_scenario(http)
        queried_again = http.ok("GET", f"/api/sheets/{summary['sheetId']}")
        assert queried_again["canonicalHash"] == summary["hash"]
        alt_again = http.ok("GET", f"/api/sheets/{summary['altSheetId']}")
        assert alt_again["status"] == "pending"
        print("重启后已签单原样可查；另一张候选单仍为", alt_again["status"])
        print("\nDEMO_OK " + json.dumps(summary, ensure_ascii=False))
        if args.keep_server:
            print(f"server kept running at http://127.0.0.1:{args.port}", flush=True)
            # Intentionally return while child remains; caller asked to keep it.
            return 0
    finally:
        if not args.keep_server:
            stop_server(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
