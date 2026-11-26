#!/usr/bin/env python3
"""End-to-end demo driven by two real CLI/HTTP local clients.

Run with:  PYTHONPATH=/workspace python3 -m coop.demo
It starts/stops the localhost server twice to prove SQLite persistence.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .client import LocalHttpClient

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo"
DB_PATH = DEMO_DIR / "coop-demo.sqlite3"
TRACE_PATH = DEMO_DIR / "demo-output.json"
SERVER_LOG = DEMO_DIR / "server.log"


class CliClient:
    """A distinct local client process for every RPC invocation."""

    def __init__(self, name: str, base_url: str, trace: list[dict[str, Any]]):
        self.name = name
        self.base_url = base_url
        self.trace = trace

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        echo: bool = True,
    ) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if k != "client_id"}
        cmd = [
            sys.executable,
            "-m",
            "coop.cli",
            "--client",
            self.name,
            "--base-url",
            self.base_url,
            "call",
            method,
            json.dumps(params, ensure_ascii=False),
        ]
        if idempotency_key:
            cmd += ["--idem-key", idempotency_key]

        if echo:
            print(f"\n$ {self.name}> {method} {json.dumps(params, ensure_ascii=False)}")

        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            timeout=20,
        )
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr, file=sys.stderr)
            raise RuntimeError(f"CLI call failed via {self.name}: {method}")

        envelope = json.loads(proc.stdout)
        self.trace.append(
            {
                "client": self.name,
                "method": method,
                "params": params,
                "idempotency_key": idempotency_key,
                "response": envelope,
            }
        )
        if not envelope.get("ok"):
            raise RuntimeError(f"RPC failed via {self.name}: {json.dumps(envelope, ensure_ascii=False)}")
        return envelope["result"]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_health(base_url: str, timeout: float = 10.0) -> None:
    client = LocalHttpClient("demo-health", base_url)
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        response = client.call("health")
        if response.get("ok"):
            return
        last_error = RuntimeError(str(response))
        time.sleep(0.15)
    raise RuntimeError(f"server did not become healthy: {last_error}")


def start_server(port: int) -> tuple[subprocess.Popen[bytes], Any]:
    DEMO_DIR.mkdir(exist_ok=True)
    log = SERVER_LOG.open("ab")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "coop.server",
            "--db",
            str(DB_PATH),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    wait_for_health(f"http://127.0.0.1:{port}")
    return proc, log


def stop_server(proc: subprocess.Popen[bytes], log: Any) -> None:
    # SIGINT lets coop.server execute its close/checkpoint path.
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    log.close()


def clean_db() -> None:
    DEMO_DIR.mkdir(exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(DB_PATH) + suffix)
        if path.exists():
            path.unlink()


def setup_dual_point_case(
    c1: CliClient,
    c2: CliClient,
    slug: str,
    *,
    relay_duplicate: bool = True,
) -> dict[str, str]:
    """Create the canonical old [4,8), new [6,7) example."""
    customer = f"C-{slug}"
    old_line = f"OLD-{slug}"
    new_line = f"NEW-{slug}"
    old_key = f"{slug}-OLD-4-8"
    new_key = f"{slug}-NEW-6-7"
    case_id = f"CASE-{slug}"

    c1.call(
        "create_customer",
        {"customer_id": customer, "name": f"{slug} 住户", "client_id": c1.name},
    )
    c1.call(
        "create_line",
        {
            "line_id": old_line,
            "customer_id": customer,
            "label": "old",
            "address": f"{slug} 旧屋",
            "opened_at": 0,
            "returned_at": 9,
            "client_id": c1.name,
        },
    )
    c2.call(
        "create_line",
        {
            "line_id": new_line,
            "customer_id": customer,
            "label": "new",
            "address": f"{slug} 新套间",
            "opened_at": 3,
            "client_id": c2.name,
        },
    )
    c1.call(
        "add_commitment",
        {
            "commitment_id": f"CMT-{slug}-OLD",
            "line_id": old_line,
            "start_minute": 0,
            "end_minute": 9,
            "kind": "promised",
            "contract_id": f"K-{slug}",
            "client_id": c1.name,
        },
    )
    c2.call(
        "add_commitment",
        {
            "commitment_id": f"CMT-{slug}-NEW",
            "line_id": new_line,
            "start_minute": 4,
            "end_minute": 12,
            "kind": "promised",
            "contract_id": f"K-{slug}",
            "client_id": c2.name,
        },
    )
    c1.call(
        "record_fault_report",
        {
            "report_id": f"RPT-{slug}-OLD",
            "line_id": old_line,
            "fault_key": old_key,
            "start_minute": 4,
            "end_minute": 8,
            "reporter": "subscriber-terminal-1",
            "client_id": c1.name,
        },
    )
    c2.call(
        "record_fault_report",
        {
            "report_id": f"RPT-{slug}-NEW",
            "line_id": new_line,
            "fault_key": new_key,
            "start_minute": 6,
            "end_minute": 7,
            "reporter": "subscriber-terminal-2",
            "client_id": c2.name,
        },
    )
    if relay_duplicate:
        duplicate = c2.call(
            "record_fault_report",
            {
                "report_id": f"RPT-{slug}-OLD-RELAYED-BY-2",
                "line_id": old_line,
                "fault_key": old_key,
                "start_minute": 4,
                "end_minute": 8,
                "reporter": "same subscriber, second local client relay",
                "client_id": c2.name,
            },
        )
        print(
            f"  ↳ 同故障异单转发：duplicate_fault={duplicate['duplicate_fault']}，"
            f"canonical outage={duplicate['outage']['id']}"
        )

    c1.call(
        "run_compensation",
        {
            "case_id": case_id,
            "customer_id": customer,
            "window_start": 0,
            "window_end": 12,
            "client_id": c1.name,
        },
    )
    return {
        "customer": customer,
        "old_line": old_line,
        "new_line": new_line,
        "old_outage": f"OUT-{old_key}",
        "new_outage": f"OUT-{new_key}",
        "case_id": case_id,
    }


def print_minute_decisions(case: dict[str, Any], version_no: int = 1) -> None:
    version = case["versions"][version_no - 1]
    print(
        "  minute decisions: "
        + " ".join(
            f"{row['minute']}:{'欠' if row['owed'] else '—'}"
            for row in version["minute_rows"]
            if 3 <= row["minute"] <= 8
        )
    )
    print(f"  owed_intervals={version['calculation']['owed_intervals']} expected={version['expected_minutes']}")


def renewal_brief(renewal: dict[str, Any]) -> dict[str, Any]:
    order = renewal["order"]
    return {
        "order_id": order["id"],
        "receipt_id": order["receipt_id"],
        "delivery_count": renewal["receipt"]["delivery_count"],
        "service_interval": [order["service_start"], order["service_end"]],
        "list": order["list_amount_minor"],
        "voucher_minutes": order["voucher_minutes_applied"],
        "discount": order["discount_minor"],
        "amount_due": order["amount_due_minor"],
        "paid": order["paid_amount_minor"],
        "balance": renewal["balance_minor"],
        "duplicate_receipt": renewal["duplicate_receipt"],
        "ledger": [(entry["kind"], entry["amount_minor"]) for entry in renewal["account_entries"]],
    }


def main() -> int:
    clean_db()
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    trace: list[dict[str, Any]] = []
    c1 = CliClient("terminal-1", base_url, trace)
    c2 = CliClient("terminal-2", base_url, trace)

    print("=" * 78)
    print("宽带合作社：迁址 / 重叠接入点 SLA / 时券兑现 / 晚到证据更正")
    print(f"两个本地客户端：{c1.name}（旧屋与营业员甲）, {c2.name}（新址与续期窗口乙）")
    print(f"SQLite：{DB_PATH}")
    print("=" * 78)

    proc, log = start_server(port)
    artifacts: dict[str, Any] = {"db_path": str(DB_PATH), "clients": [c1.name, c2.name]}
    try:
        # ---------- Main redeem-then-correct story ----------
        print("\n【1】原始双点合同：旧 [4,8) 中断，新 [6,7) 中断")
        ids = setup_dual_point_case(c1, c2, "MAIN")
        main_case = c2.call("get_case", {"case_id": ids["case_id"], "client_id": c2.name})
        print_minute_decisions(main_case)
        voucher_id = main_case["vouchers"][0]["id"]
        print(f"  ↳ 真实可查时券：{voucher_id} / {main_case['vouchers'][0]['code']}")
        options = c1.call(
            "list_renewal_options",
            {
                "customer_id": ids["customer"],
                "voucher_id": voucher_id,
                "client_id": c1.name,
            },
        )
        chosen = next(x for x in options["options"] if x["package_code"] == "coop-30-minutes")
        print(
            "  ↳ 客户选择可办理续期："
            f"{chosen['package_code']} list={chosen['list_amount_minor']} "
            f"voucher_discount={chosen['discount_minor']} due={chosen['amount_due_minor']}"
        )

        print("\n【2】下一次续期使用时券；同一笔续期回执重到")
        renewal_params = {
            "receipt_id": "RCPT-MAIN-2026-11",
            "customer_id": ids["customer"],
            "order_id": "RNW-MAIN-001",
            "package_code": "coop-30-minutes",
            "service_minutes": 30,
            "rate_minor_per_minute": 1,
            "service_start": 10,
            "voucher_id": voucher_id,
            "voucher_minutes": 1,
            "client_id": c2.name,
        }
        first = c2.call("receive_renewal_receipt", renewal_params)
        print(json.dumps(renewal_brief(first), ensure_ascii=False, indent=2))

        redelivered = c1.call("receive_renewal_receipt", renewal_params, echo=True)
        print("  ↳ 回执重到结果（不得二次抵扣/二次收款）：")
        print(json.dumps(renewal_brief(redelivered), ensure_ascii=False, indent=2))

        print("\n【3】第一次重启后端：SQLite 中的案件、券、回执次数和账仍可查询")
        stop_server(proc, log)
        proc, log = start_server(port)
        after_restart = c2.call(
            "get_renewal",
            {"order_or_receipt_id": "RNW-MAIN-001", "client_id": c2.name},
        )
        print(json.dumps(renewal_brief(after_restart), ensure_ascii=False, indent=2))

        print("\n【4】晚到的修复/遥测证据：新址实际上全程可用，补偿 1 -> 0")
        c1.call(
            "add_evidence",
            {
                "evidence_id": "EV-MAIN-LATE-FULL",
                "customer_id": ids["customer"],
                "kind": "late_repair_and_telemetry_review",
                "summary": "6-7 分钟为自测误报，用户面全程可用，无实际修复换件",
                "payload": {"telemetry": "new-access=up", "repair_ticket": "REP-NEW-LATE-01"},
                "client_id": c1.name,
            },
        )
        c1.call(
            "correct_outage",
            {
                "outage_id": ids["new_outage"],
                "evidence_id": "EV-MAIN-LATE-FULL",
                "new_status": "void",
                "client_id": c1.name,
            },
        )
        corrected_case = c2.call(
            "recalculate_compensation",
            {
                "case_id": ids["case_id"],
                "evidence_id": "EV-MAIN-LATE-FULL",
                "as_of_minute": 12,
                "client_id": c2.name,
            },
        )
        print_minute_decisions(corrected_case, version_no=2)
        print(
            "  voucher after correction: "
            + json.dumps(
                [
                    {
                        "id": v["id"],
                        "granted": v["granted_minutes"],
                        "redeemed": v["redeemed_minutes"],
                        "cancelled": v["cancelled_minutes"],
                        "clawed_back": v["clawed_back_minutes"],
                        "status": v["status"],
                    }
                    for v in corrected_case["vouchers"]
                ],
                ensure_ascii=False,
            )
        )

        corrected_renewal = c2.call(
            "get_renewal",
            {
                "order_or_receipt_id": "RNW-MAIN-001",
                "as_of_minute": 12,
                "client_id": c2.name,
            },
        )
        print("  ↳ 晚到证据改变的是已用券对应的同一期续期挂账：")
        print(json.dumps(renewal_brief(corrected_renewal), ensure_ascii=False, indent=2))
        print("  ↳ 已消费部分交代：" + json.dumps(corrected_renewal["consumption_as_of"], ensure_ascii=False))
        notices = c1.call(
            "list_notifications",
            {"customer_id": ids["customer"], "client_id": c1.name},
        )["notifications"]
        for notice in notices[-2:]:
            print(f"  通知 {notice['id']}《{notice['title']}》：{notice['body']}")

        evidence_trace = c2.call(
            "trace_evidence",
            {"evidence_id": "EV-MAIN-LATE-FULL", "client_id": c2.name},
        )
        artifacts["main_evidence_trace"] = evidence_trace
        print(
            "  ↳ 证据追账链："
            f"evidence -> cases={evidence_trace['changed_case_ids']} -> "
            f"renewals={[r['order']['id'] for r in evidence_trace['affected_renewal_orders']]} -> "
            f"entries={[(e['kind'], e['amount_minor']) for e in evidence_trace['account_entries']]}"
        )

        print("\n【5】更正后同一续期回执再次重到；随后结清差额并再次重启查询")
        third = c2.call("receive_renewal_receipt", renewal_params, echo=True)
        brief_third = renewal_brief(third)
        print(json.dumps(brief_third, ensure_ascii=False, indent=2))
        assert brief_third["delivery_count"] == 3
        assert brief_third["balance"] == 1
        assert len(brief_third["ledger"]) == 4

        paid = c1.call(
            "pay_outstanding_balance",
            {
                "renewal_order_id": "RNW-MAIN-001",
                "payment_id": "PAY-MAIN-CLAWBACK",
                "client_id": c1.name,
            },
        )
        print(f"  ↳  synthetic settlement payment posted; balance={paid['balance_minor']}")

        stop_server(proc, log)
        proc, log = start_server(port)
        final_main_renewal = c2.call(
            "get_renewal",
            {"order_or_receipt_id": "RCPT-MAIN-2026-11", "client_id": c2.name},
        )
        final_main_ledger = c2.call(
            "get_customer_ledger",
            {"customer_id": ids["customer"], "client_id": c2.name},
        )
        artifacts["main_final_renewal_after_second_restart"] = final_main_renewal
        artifacts["main_full_customer_ledger"] = final_main_ledger
        print(
            "  ↳ restarted query by receipt id works; final balance="
            f"{final_main_renewal['balance_minor']}, service interval remains "
            f"{final_main_renewal['order']['service_start']}.."
            f"{final_main_renewal['order']['service_end']}"
        )

        # ---------- Four-minute test-only variant ----------
        print("\n【6】保持线路事实，新址 [4,8) 改为仅供测试：补偿升为 4 分钟")
        test_ids = setup_dual_point_case(c1, c2, "TEST")
        c1.call(
            "add_evidence",
            {
                "evidence_id": "EV-TEST-TESTONLY",
                "customer_id": test_ids["customer"],
                "kind": "commitment_correction",
                "summary": "新址 4-8 分钟为内部测试，未对住户承诺日常可用；第 8 分钟才切换",
                "client_id": c1.name,
            },
        )
        c1.call(
            "correct_commitment_to_test",
            {
                "line_id": test_ids["new_line"],
                "start_minute": 4,
                "end_minute": 8,
                "evidence_id": "EV-TEST-TESTONLY",
                "correction_id": "CMT-TEST-4-8-TEST",
                "client_id": c1.name,
            },
        )
        test_case = c2.call(
            "recalculate_compensation",
            {
                "case_id": test_ids["case_id"],
                "evidence_id": "EV-TEST-TESTONLY",
                "client_id": c2.name,
            },
        )
        print_minute_decisions(test_case, version_no=2)
        artifacts["test_case"] = test_case
        assert test_case["versions"][-1]["expected_minutes"] == 4

        # ---------- Zero variant ----------
        print("\n【7】从原始双点合同出发，只更正新址全程可用：补偿为 0")
        zero_ids = setup_dual_point_case(c1, c2, "ZERO")
        c1.call(
            "add_evidence",
            {
                "evidence_id": "EV-ZERO-FULL",
                "customer_id": zero_ids["customer"],
                "kind": "availability_correction",
                "summary": "网管与迟到修复记录复核：新址全程可用，6-7 分钟事件不成立",
                "client_id": c1.name,
            },
        )
        c1.call(
            "correct_outage",
            {
                "outage_id": zero_ids["new_outage"],
                "evidence_id": "EV-ZERO-FULL",
                "new_status": "void",
                "client_id": c1.name,
            },
        )
        zero_case = c2.call(
            "recalculate_compensation",
            {
                "case_id": zero_ids["case_id"],
                "evidence_id": "EV-ZERO-FULL",
                "client_id": c2.name,
            },
        )
        print_minute_decisions(zero_case, version_no=2)
        print(
            "  voucher status: "
            + json.dumps(
                [
                    {"granted": v["granted_minutes"], "cancelled": v["cancelled_minutes"], "status": v["status"]}
                    for v in zero_case["vouchers"]
                ],
                ensure_ascii=False,
            )
        )
        artifacts["zero_case"] = zero_case
        assert zero_case["versions"][-1]["expected_minutes"] == 0

        artifacts["trace"] = trace
        TRACE_PATH.write_text(json.dumps(artifacts, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n" + "=" * 78)
        print("DEMO OK")
        print(f"完整调用/响应/分钟级账据：{TRACE_PATH}")
        print(f"可继续用客户端查询，例如：COOP base url was {base_url}")
        print("重启真实服务：python3 -m coop.server --db /workspace/demo/coop-demo.sqlite3 --port 8931")
        print("=" * 78)
        return 0
    finally:
        stop_server(proc, log)


if __name__ == "__main__":
    raise SystemExit(main())
