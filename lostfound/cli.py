from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import services
from .db import connect
from .seed import seed_dolphins


def _json_arg(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    return json.loads(value)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="失物处本地命令行入口")
    p.add_argument("--db", default=os.environ.get("LOSTFOUND_DB", "lostfound.db"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="写入海豚壶示例")
    s.add_argument("--reset", action="store_true")

    sub.add_parser("stock-check", help="检查凭条、交接与库房状态")

    s = sub.add_parser("round", help="创建轮次")
    s.add_argument("json", help="JSON 字符串，或 @path/to/file.json")

    s = sub.add_parser("item", help="受理实物")
    s.add_argument("json", help="JSON 字符串，或 @file.json")

    s = sub.add_parser("claim", help="受理申领")
    s.add_argument("json", help="JSON 字符串，或 @file.json")

    s = sub.add_parser("reconcile", help="核对某一轮")
    s.add_argument("round_id")

    s = sub.add_parser("sign-determinate", help="签出一轮中所有当前确定申领")
    s.add_argument("round_id")
    s.add_argument("--window", default="desk")

    s = sub.add_parser("sign", help="为一份申领签单")
    s.add_argument("--claim", required=True)
    s.add_argument("--item")
    s.add_argument("--window", default="sign-desk")

    s = sub.add_parser("inspect", help="保管员执行一次真实开袋检查")
    s.add_argument("--item", required=True)
    s.add_argument("--inspector", default="custodian")

    s = sub.add_parser("plan-inspection")
    s.add_argument("round_id")

    s = sub.add_parser("revisit", help="登记复访的两选一约束回答")
    s.add_argument("--claim", required=True)
    s.add_argument("--answer", required=True)
    s.add_argument("--feature", default=None)
    s.add_argument("--window", default="revisit-desk")

    s = sub.add_parser("outbound", help="凭条出库")
    s.add_argument("--voucher", required=True)
    s.add_argument("--window", required=True)
    s.add_argument("--request-id", default="")

    s = sub.add_parser("track", help="按申领/凭条/实物/检查编号复查")
    s.add_argument("number")

    s = sub.add_parser("record", help="查看申领封存说明和按序复访笔录")
    s.add_argument("claim_no")

    s = sub.add_parser("no-match", help="显式登记无失物虚报")
    s.add_argument("claim_no")
    s.add_argument("--note", default="")

    s = sub.add_parser("unclaimed", help="显式登记无人认领物")
    s.add_argument("item_no")
    s.add_argument("--note", default="")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect(args.db)
    try:
        if args.cmd == "seed":
            result: Any = seed_dolphins(conn, reset=args.reset)
        elif args.cmd == "stock-check":
            result = services.stock_check(conn)
        elif args.cmd == "round":
            result = services.create_round(conn, _json_arg(args.json))
        elif args.cmd == "item":
            result = services.create_item(conn, _json_arg(args.json))
        elif args.cmd == "claim":
            result = services.create_claim(conn, _json_arg(args.json))
        elif args.cmd == "reconcile":
            result = services.reconcile_round(conn, args.round_id)
        elif args.cmd == "sign-determinate":
            result = services.sign_determinate(conn, args.round_id, args.window)
        elif args.cmd == "sign":
            result = services.create_voucher(conn, {
                "claim_no": args.claim,
                "item_no": args.item,
                "window_id": args.window,
            })
        elif args.cmd == "inspect":
            result = services.create_inspection(conn, {
                "item_no": args.item,
                "inspector": args.inspector,
            })
        elif args.cmd == "plan-inspection":
            result = services.plan_inspections(conn, args.round_id)
        elif args.cmd == "revisit":
            result = services.answer_revisit(conn, {
                "claim_no": args.claim,
                "answer": args.answer,
                "feature_key": args.feature,
                "window_id": args.window,
            })
        elif args.cmd == "outbound":
            result = services.outbound(conn, {
                "voucher_no": args.voucher,
                "window_id": args.window,
                "request_id": args.request_id,
            })
        elif args.cmd == "track":
            result = services.track_number(conn, args.number)
        elif args.cmd == "record":
            result = services.claim_record(conn, args.claim_no)
        elif args.cmd == "no-match":
            result = services.mark_claim_no_match(conn, args.claim_no, args.note)
        elif args.cmd == "unclaimed":
            result = services.mark_item_unclaimed(conn, args.item_no, args.note)
        else:  # pragma: no cover - argparse requires a known command
            return 2

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except services.ApiError as exc:
        print(json.dumps(
            {"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            ensure_ascii=False,
            indent=2,
        ), file=sys.stderr)
        return 1 if exc.status >= 500 else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
