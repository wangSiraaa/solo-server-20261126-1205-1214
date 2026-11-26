from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .seed import DEMO_IDS as I

T0 = "2026-11-26T09:00:00+00:00"
T_PREP = "2026-11-26T10:00:00+00:00"
T_EXIT = "2026-11-26T10:05:00+00:00"
T_LATER = "2026-11-26T10:20:00+00:00"


class ApiFailure(RuntimeError):
    def __init__(self, status: int, code: str, message: str, payload: Dict[str, Any]):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.payload = payload


class Client:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"message": raw}
            raise ApiFailure(
                exc.code,
                payload.get("error", "http_error"),
                payload.get("message", raw),
                payload,
            ) from exc

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Dict[str, Any]) -> Any:
        return self.request("POST", path, body)


class Report:
    def __init__(self) -> None:
        self.checks: List[Dict[str, Any]] = []

    def check(self, name: str, ok: bool, detail: Any = None) -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}")
        if detail is not None and (not ok or "--verbose" in sys.argv):
            print("       " + json.dumps(detail, ensure_ascii=False))
        if not ok:
            raise AssertionError(name)

    def expect_error(self, name: str, expected_code: str, fn: Callable[[], Any]) -> ApiFailure:
        try:
            fn()
        except ApiFailure as exc:
            ok = exc.code == expected_code or expected_code in json.dumps(exc.payload, ensure_ascii=False)
            self.check(name, ok, {"expected": expected_code, "actual": exc.code, "message": str(exc)})
            if not ok:
                raise AssertionError(f"{name}: expected {expected_code}, got {exc.code}")
            return exc
        self.check(name, False, {"expected": expected_code, "actual": "request succeeded"})
        raise AssertionError(name)

    def summary(self) -> Dict[str, Any]:
        passed = sum(1 for c in self.checks if c["ok"])
        return {"passed": passed, "total": len(self.checks), "ok": passed == len(self.checks)}


def gate_in(client: Client, band: str, session: str, at: str = T0) -> None:
    client.post("/api/scans/gate-entry", {
        "session_id": session,
        "wristband_id": band,
        "zone_id": I["shallow"],
        "staff_id": "STAFF-01",
        "at": at,
        "allow_duplicate_scan": True,
    })


def main_flow(client: Client, rep: Report) -> Dict[str, Any]:
    sid = "SESS-MAIN"
    client.post("/api/sessions", {
        "id": sid,
        "name": "主复演：预编家庭替班",
        "operating_mode": "prearranged",
        "starts_at": "2026-11-26T09:00:00+00:00",
        "ends_at": "2026-11-26T12:00:00+00:00",
    })
    client.post("/api/plan-segments", {
        "id": "SEG-MAIN-SIS",
        "session_id": sid,
        "family_id": I["family_jia"],
        "child_id": I["sister"],
        "activity": "妹妹浅区第一段",
        "zone_id": I["shallow"],
        "starts_at": "2026-11-26T09:00:00+00:00",
        "ends_at": "2026-11-26T12:00:00+00:00",
        "primary_adult_id": I["a"],
        "backup_adult_id": I["b"],
        "window_starts_at": "2026-11-26T09:50:00+00:00",
        "window_ends_at": "2026-11-26T11:00:00+00:00",
    })
    for band in (I["a"], I["b"], I["sister"]):
        gate_in(client, band, sid)

    client.post("/api/care/assignments", {
        "id": "CARE-A-SIS-MAIN",
        "session_id": sid,
        "child_id": I["sister"],
        "adult_id": I["a"],
        "kind": "primary",
        "purpose": "own_care",
        "at": T0,
    })

    ho = client.post("/api/handovers/prepare", {
        "id": "HO-MAIN",
        "session_id": sid,
        "departing_adult_id": I["a"],
        "receiving_adult_id": I["b"],
        "child_ids": [I["sister"]],
        "source": "plan",
        "segment_id": "SEG-MAIN-SIS",
        "at": T_PREP,
        "idempotency_key": "prepare-main",
    })
    rep.check("乙被登记为这一刻的有效备陪/接替人", ho["status"] == "prepared" and ho["source"] == "plan", ho)
    rep.check("乙有且仅有一个空名额并已为妹妹预备占用", ho["children"][0]["slot_no"] == 1, ho["children"])

    permit = client.post("/api/permits", {
        "id": "PM-MAIN",
        "session_id": sid,
        "wristband_id": I["a"],
        "handover_id": "HO-MAIN",
        "at": T_PREP,
        "idempotency_key": "permit-main",
    })
    rep.check("甲的本次过闸许可已签发", permit["status"] == "issued", permit)

    consumed = client.post("/api/permits/PM-MAIN/consume", {
        "staff_id": "GATE-01",
        "at": T_EXIT,
        "idempotency_key": "cross-main",
    })
    rep.check("许可可兑现并已消费", consumed["permit"]["status"] == "consumed", consumed)
    rep.check("甲真正出闸", consumed["location"]["location_kind"] == "outside", consumed["location"])
    rep.check("交接在过闸后成为 effective", consumed["handover"]["status"] == "effective", consumed["handover"])

    answer = client.get(f"/api/answer?session_id={sid}")
    sister = next(c for c in answer["children"] if c["child_id"] == I["sister"])
    rep.check("系统回答：实际接下妹妹的是乙", sister["actual_receiver_ids"] == [I["b"]], sister)
    a_loc = next(p for p in answer["people_locations"] if p["wristband_id"] == I["a"])
    b_loc = next(p for p in answer["people_locations"] if p["wristband_id"] == I["b"])
    rep.check("系统回答：甲在闸外、乙仍在浅区", a_loc["location_kind"] == "outside" and b_loc["zone_id"] == I["shallow"], {
        "a": a_loc, "b": b_loc
    })
    rep.check("系统记录了甲的 gate_exit 离开", any(e["id"] == consumed["crossing_event_id"] for e in answer["gate_exits"]), answer["gate_exits"])
    return answer


def invisible_branch(client: Client, rep: Report) -> None:
    sid = "SESS-INVISIBLE"
    client.post("/api/sessions", {"id": sid, "name": "反例：回条在看不见的另一侧", "operating_mode": "prearranged"})
    client.post("/api/plan-segments", {
        "id": "SEG-INVIS", "session_id": sid, "family_id": I["family_jia"], "child_id": I["sister"],
        "activity": "浅区段落", "zone_id": I["shallow"],
        "starts_at": T0, "ends_at": "2026-11-26T12:00:00+00:00",
        "primary_adult_id": I["a"], "backup_adult_id": I["b"],
        "window_starts_at": T0, "window_ends_at": "2026-11-26T12:00:00+00:00",
    })
    for band in (I["a"], I["b"], I["sister"]):
        gate_in(client, band, sid)
    client.post("/api/care/assignments", {
        "id": "CARE-A-INVIS", "session_id": sid, "child_id": I["sister"], "adult_id": I["a"], "at": T0
    })
    client.post("/api/handovers/prepare", {
        "id": "HO-INVIS", "session_id": sid,
        "departing_adult_id": I["a"], "receiving_adult_id": I["b"],
        "child_ids": [I["sister"]], "source": "plan", "segment_id": "SEG-INVIS", "at": T_PREP,
    })
    client.post("/api/permits", {
        "id": "PM-INVIS", "session_id": sid, "wristband_id": I["a"],
        "handover_id": "HO-INVIS", "at": T_PREP,
    })

    changed = client.post("/api/receipts/correct", {
        "session_id": sid,
        "wristband_id": I["b"],
        "zone_id": I["invisible"],
        "reason": "复演：只把乙的实际入区回条改为彼此看不见的滑梯另一侧",
        "staff_id": "AUDIT",
        "at": T_EXIT,
    })
    rep.check("乙的回条已改到看不见的另一侧", changed["zone_id"] == I["invisible"], changed)

    err = rep.expect_error(
        "看不见另一侧时原过闸不予兑现",
        "gate_not_released",
        lambda: client.post("/api/permits/PM-INVIS/consume", {"staff_id": "GATE-01", "at": T_EXIT}),
    )
    reasons = json.dumps(err.payload, ensure_ascii=False)
    rep.check("拒绝原因包含 receiver_not_visible", "receiver_not_visible" in reasons, err.payload.get("details"))

    permit = client.get("/api/permits/PM-INVIS")
    ho = client.get("/api/handovers/HO-INVIS")
    rep.check("拒绝后许可未消费、委托仍保持 prepared", permit["status"] == "issued" and ho["status"] == "prepared")
    rep.check("拒绝后乙的名额仍保持占用入账", ho["receiver_used_slots"] == [1], ho["receiver_used_slots"])

    client.post("/api/receipts/correct", {
        "session_id": sid, "wristband_id": I["b"], "zone_id": I["shallow"],
        "reason": "复演：恢复同区", "staff_id": "AUDIT", "at": T_LATER,
    })
    ok = client.post("/api/permits/PM-INVIS/consume", {
        "staff_id": "GATE-01", "at": T_LATER, "idempotency_key": "cross-invis-restored"
    })
    rep.check("恢复同区后同一许可可兑现", ok["permit"]["status"] == "consumed", ok)


def capacity_branch(client: Client, rep: Report) -> None:
    sid = "SESS-CAPACITY"
    client.post("/api/sessions", {"id": sid, "name": "反例：最后名额更早被占用", "operating_mode": "onsite"})
    group = client.post("/api/aid-groups", {
        "id": "GROUP-CAP",
        "session_id": sid,
        "name": "甲家—林家现场互助团",
        "family_ids": [I["family_jia"], I["family_lin"]],
    })
    rep.check("柜台已完成组团", group["family_ids"] == [I["family_jia"], I["family_lin"]], group)
    for band in (I["a"], I["b"], I["sister"], I["c"], I["dou"]):
        gate_in(client, band, sid, "2026-11-26T08:50:00+00:00")

    # An earlier cross-family on-site handover gives B's only slot to Xiao Dou.
    client.post("/api/care/assignments", {
        "id": "CARE-C-DOU-CAP", "session_id": sid, "child_id": I["dou"], "adult_id": I["c"], "at": "2026-11-26T09:00:00+00:00"
    })
    early = client.post("/api/handovers/prepare", {
        "id": "HO-EARLY-DOU", "session_id": sid, "aid_group_id": "GROUP-CAP",
        "departing_adult_id": I["c"], "receiving_adult_id": I["b"],
        "child_ids": [I["dou"]], "source": "onsite", "receiver_confirmed": True,
        "at": "2026-11-26T09:20:00+00:00",
        "idempotency_key": "early-dou",
    })
    rep.check("较早受理的小豆已占用乙的最后名额", early["receiver_used_slots"] == [1], early)
    client.post("/api/permits", {
        "id": "PM-EARLY-C", "session_id": sid, "wristband_id": I["c"],
        "handover_id": "HO-EARLY-DOU", "at": "2026-11-26T09:20:00+00:00",
    })
    client.post("/api/permits/PM-EARLY-C/consume", {"at": "2026-11-26T09:25:00+00:00"})

    client.post("/api/care/assignments", {
        "id": "CARE-A-SIS-CAP", "session_id": sid, "child_id": I["sister"], "adult_id": I["a"], "at": "2026-11-26T09:00:00+00:00"
    })
    rep.expect_error(
        "乙最后名额已被占用时不能再登记妹妹交接",
        "no_free_slot",
        lambda: client.post("/api/handovers/prepare", {
            "id": "HO-LATE-SIS", "session_id": sid,
            "departing_adult_id": I["a"], "receiving_adult_id": I["b"],
            "child_ids": [I["sister"]], "source": "onsite",
            "receiver_confirmed": True, "at": T_PREP,
        }),
    )
    rep.expect_error(
        "没有有效接替时甲的独自离区许可也不能签发",
        "handover_required",
        lambda: client.post("/api/permits", {
            "id": "PM-A-CAP-BLOCKED", "session_id": sid, "wristband_id": I["a"], "at": T_PREP
        }),
    )
    answer = client.get(f"/api/answer?session_id={sid}")
    a_loc = next(p for p in answer["people_locations"] if p["wristband_id"] == I["a"])
    rep.check("容量反例中甲仍在浅区、没有离开记录", a_loc["zone_id"] == I["shallow"] and not any(
        e["wristband_id"] == I["a"] for e in answer["gate_exits"]
    ), a_loc)


def presence_branch(client: Client, rep: Report) -> None:
    sid = "SESS-PRESENCE"
    client.post("/api/sessions", {"id": sid, "name": "反例：接替成人仍在闸外", "operating_mode": "onsite"})
    gate_in(client, I["a"], sid)
    gate_in(client, I["sister"], sid)
    client.post("/api/care/assignments", {
        "id": "CARE-A-SIS-PRESENCE", "session_id": sid,
        "child_id": I["sister"], "adult_id": I["a"], "at": T0,
    })
    rep.expect_error(
        "乙的腕带/票虽已售出，但没有入区回条时不能登记为在场接替人",
        "receiver_not_in_water",
        lambda: client.post("/api/handovers/prepare", {
            "id": "HO-QUEUE", "session_id": sid,
            "departing_adult_id": I["a"], "receiving_adult_id": I["b"],
            "child_ids": [I["sister"]], "source": "onsite",
            "receiver_confirmed": True, "at": T_PREP,
        }),
    )
    gate_in(client, I["b"], sid, "2026-11-26T09:55:00+00:00")
    ho = client.post("/api/handovers/prepare", {
        "id": "HO-PRESENCE-OK", "session_id": sid,
        "departing_adult_id": I["a"], "receiving_adult_id": I["b"],
        "child_ids": [I["sister"]], "source": "onsite",
        "receiver_confirmed": True, "at": T_PREP,
    })
    rep.check("乙逐人刷卡取得实际入区回条后，交接才可预备", ho["status"] == "prepared", ho)


def borrowing_flow(client: Client, rep: Report) -> None:
    sid = "SESS-MAIN"
    client.post("/api/time-accounts", {
        "id": "TIME-A-ORIG",
        "session_id": sid,
        "family_id": I["family_jia"],
        "adult_id": I["a"],
        "segment_id": "SEG-MAIN-SIS",
        "label": "甲原来的可借用陪同时间",
        "original_minutes": 180,
        "sellable_units": 1,
    })
    loan = client.post("/api/time-loans", {
        "id": "LOAN-LIN-1",
        "session_id": sid,
        "account_id": "TIME-A-ORIG",
        "to_family_id": I["family_lin"],
        "child_id": I["dou"],
        "units": 1,
        "starts_at": T_EXIT,
        "ends_at": "2026-11-26T11:00:00+00:00",
        "idempotency_key": "loan-lin-1",
    })
    rep.check("另一家的借用申请已入账", loan["status"] == "applied", loan)

    sold = client.post("/api/time-loans/LOAN-LIN-1/decision", {"approve": True, "staff_id": "FRONT-1"})
    rep.check("前台批准并卖出甲原来的一个陪同时间名额", sold["status"] == "approved_sold", sold)

    rep.expect_error(
        "甲已真正出闸，不能凭已售名额假装甲仍可兑现",
        "adult_not_in_water",
        lambda: client.post("/api/time-loans/LOAN-LIN-1/fulfill", {"fulfilling_adult_id": I["a"], "at": T_LATER}),
    )
    rep.expect_error(
        "林成人还在闸机外/无回条时也不能假定其在水区兑现",
        "adult_not_in_water",
        lambda: client.post("/api/time-loans/LOAN-LIN-1/fulfill", {"fulfilling_adult_id": I["c"], "at": T_LATER}),
    )

    client.post("/api/time-loans", {
        "id": "LOAN-LIN-REUSE",
        "session_id": sid,
        "account_id": "TIME-A-ORIG",
        "to_family_id": I["family_lin"],
        "child_id": I["dou"],
        "units": 1,
        "idempotency_key": "loan-reuse",
    })
    rep.expect_error(
        "同一已卖陪同时间名额不能被第二单重复使用",
        "loan_quota_exhausted",
        lambda: client.post("/api/time-loans/LOAN-LIN-REUSE/decision", {"approve": True}),
    )

    # Only after actual scans can the loan be operationally fulfilled.
    gate_in(client, I["c"], sid, "2026-11-26T10:15:00+00:00")
    gate_in(client, I["dou"], sid, "2026-11-26T10:16:00+00:00")
    fulfilled = client.post("/api/time-loans/LOAN-LIN-1/fulfill", {
        "fulfilling_adult_id": I["c"], "at": T_LATER
    })
    rep.check("林成人实际刷卡入区后，借用陪同才真正兑现", fulfilled["status"] == "fulfilled", fulfilled)


def verify_restart(client: Client, rep: Optional[Report] = None) -> Dict[str, Any]:
    own_report = rep or Report()
    answer = client.get("/api/answer?session_id=SESS-MAIN")
    loc = {p["wristband_id"]: p for p in answer["people_locations"]}
    own_report.check("重启后仍可读出甲在闸外", loc[I["a"]]["location_kind"] == "outside", loc[I["a"]])
    own_report.check("重启后仍可读出乙在浅区", loc[I["b"]]["zone_id"] == I["shallow"], loc[I["b"]])
    own_report.check("重启后仍可读出妹妹在浅区", loc[I["sister"]]["zone_id"] == I["shallow"], loc[I["sister"]])
    sister = next(c for c in answer["children"] if c["child_id"] == I["sister"])
    own_report.check("重启后仍记录实际接下妹妹的是乙", sister["actual_receiver_ids"] == [I["b"]], sister)
    own_report.check("重启后仍记录甲的离开事件", any(e["wristband_id"] == I["a"] and e["kind"] == "gate_exit" for e in answer["gate_exits"]))
    if rep is None:
        print(json.dumps(answer, ensure_ascii=False, indent=2))
    return answer


def run_all(base_url: str) -> Dict[str, Any]:
    client = Client(base_url)
    rep = Report()
    client.post("/api/demo/reset-and-seed", {})
    main_flow(client, rep)
    borrowing_flow(client, rep)
    invisible_branch(client, rep)
    capacity_branch(client, rep)
    presence_branch(client, rep)
    verify_restart(client, rep)
    summary = rep.summary()
    print(json.dumps(summary, ensure_ascii=False))
    if not summary["ok"]:
        raise SystemExit(1)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--verify-restart", action="store_true")
    args = parser.parse_args()
    client = Client(args.base_url)
    if args.verify_restart:
        rep = Report()
        verify_restart(client, rep)
        print(json.dumps(rep.summary(), ensure_ascii=False))
        return 0 if rep.summary()["ok"] else 1
    run_all(args.base_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
