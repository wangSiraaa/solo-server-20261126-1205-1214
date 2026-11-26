"""本地导览后台命令行。

角色视角：
  录音志愿者/站点编辑：markers、licenses、withdraw、restore、close-edge、reopen-edge
  游客：plan、checkout、play、walk、query、loans
无网络服务，全部状态落 data/ 下 JSON 文件。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .loans import checkout, load_loan
from .models import PlanRequest
from .player import play_station, query_delivery, simulate_walk, timeline_of
from .planner import plan_route
from .store import GuideError, GuideStore

ROOT = Path(__file__).resolve().parent.parent
MARKERS = ROOT / "data" / "markers.json"
STATE = ROOT / "data" / "state.json"


def _store(args: argparse.Namespace) -> GuideStore:
    return GuideStore(args.markers, args.state)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=list))


def _request(args: argparse.Namespace) -> PlanRequest:
    return PlanRequest(
        visitor_id=args.visitor,
        route_id=args.route,
        mode=args.mode,
        listen_day=args.listen_day,
        loan_from=args.loan_from,
        loan_to=args.loan_to,
        headphone_budget_s=args.budget,
        mutes=frozenset(args.mute or []),
        require_original_station=args.require_original,
    )


def cmd_plan(args: argparse.Namespace) -> None:
    store = _store(args)
    plan = plan_route(store, _request(args))
    if not plan.ok:
        print("✗ 无法兑现：")
        for r in plan.reasons:
            print("  -", r)
        raise SystemExit(2)
    _print({
        "ok": True,
        "mutes": sorted(plan.mutes),
        "total_duration_s": plan.total_duration,
        "clips": [
            {
                "station": c.station_id,
                "narration": c.narration_segment_id,
                "holder": c.narration_holder,
                "fallback": c.uses_fallback,
                "segments": list(c.included_segments),
                "audible_persons": sorted(c.audible_persons),
            }
            for c in plan.clips
        ],
    })


def cmd_checkout(args: argparse.Namespace) -> None:
    store = _store(args)
    loan = checkout(store, _request(args))
    print(f"✓ 已签出离线包，发放号 #{loan.issue_id}（指纹 {loan.fingerprint}）")
    _print({"clips": loan.clips, "total_duration_s": loan.total_duration})


def cmd_play(args: argparse.Namespace) -> None:
    store = _store(args)
    _print(play_station(store, args.issue_id, args.station))


def cmd_walk(args: argparse.Namespace) -> None:
    store = _store(args)
    for entry in simulate_walk(store, args.issue_id):
        _print(entry)


def cmd_query(args: argparse.Namespace) -> None:
    store = _store(args)
    _print(query_delivery(store, args.issue_id, args.station))


def cmd_timeline(args: argparse.Namespace) -> None:
    store = _store(args)
    for iv in timeline_of(store, args.issue_id, args.station):
        print(f"{iv.start:4.1f}-{iv.end:4.1f}s  {iv.person:<6} via={iv.via:<6} 轨={iv.source_track}")


def cmd_loans(args: argparse.Namespace) -> None:
    store = _store(args)
    _print(store._state["loans"])


def cmd_markers(args: argparse.Namespace) -> None:
    store = _store(args)
    _print({
        "stations": list(store.stations),
        "routes": {rid: list(r.stations) for rid, r in store.routes.items()},
        "segments": [
            {"id": s.id, "station": s.station_id, "track": s.track, "holder": s.holder,
             "leaks": [{"person": l.person, "window": f"{l.start}-{l.end}", "via": l.source_track}
                       for l in s.leaks]}
            for s in store.segments.values()
        ],
        "withdrawn": sorted(store.withdrawn_licenses),
        "closed_edges": store._state["closed_edges"],
    })


def cmd_licenses(args: argparse.Namespace) -> None:
    store = _store(args)
    _print([
        {
            "id": l.id, "person": l.person, "stations": list(l.stations),
            "open_days": list(l.open_days), "modes": list(l.modes),
            "valid": f"{l.valid_from}..{l.valid_to}",
            "active": l.id not in store.withdrawn_licenses,
        }
        for l in store.licenses.values()
    ])


def cmd_withdraw(args: argparse.Namespace) -> None:
    _store(args).withdraw_license(args.license_id)
    print(f"✓ 已撤回 {args.license_id}（不影响已签出的离线包）")


def cmd_restore(args: argparse.Namespace) -> None:
    _store(args).restore_license(args.license_id)
    print(f"✓ 已恢复 {args.license_id}")


def cmd_close_edge(args: argparse.Namespace) -> None:
    _store(args).close_edge(args.a, args.b, args.reason)
    print(f"✓ 通路 {args.a}->{args.b} 已关闭：{args.reason}")


def cmd_reopen_edge(args: argparse.Namespace) -> None:
    _store(args).reopen_edge(args.a, args.b)
    print(f"✓ 通路 {args.a}->{args.b} 已恢复")


def _add_request_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--visitor", required=True)
    p.add_argument("--route", required=True)
    p.add_argument("--mode", choices=["speaker", "headphone"], default="headphone")
    p.add_argument("--listen-day", required=True)
    p.add_argument("--loan-from", required=True)
    p.add_argument("--loan-to", required=True)
    p.add_argument("--budget", type=float, default=float("inf"), help="耳机总时长预算（秒）")
    p.add_argument("--mute", action="append", help="允许静音的轨，可重复")
    p.add_argument("--require-original", help="委托指定必须原声的站号")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="沿河声景本地导览后台（离线包借出服务）")
    parser.add_argument("--markers", default=str(MARKERS))
    parser.add_argument("--state", default=str(STATE))
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="试配路线（不签出）")
    _add_request_args(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("checkout", help="借出离线声景包")
    _add_request_args(p)
    p.set_defaults(func=cmd_checkout)

    p = sub.add_parser("play", help="模拟到达某站播放")
    p.add_argument("issue_id", type=int)
    p.add_argument("station")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("walk", help="按包内路线逐站模拟播放")
    p.add_argument("issue_id", type=int)
    p.set_defaults(func=cmd_walk)

    p = sub.add_parser("query", help="按发放号+站号回查真实收到的片段")
    p.add_argument("issue_id", type=int)
    p.add_argument("station")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("timeline", help="查看某站渲染后的人声时码")
    p.add_argument("issue_id", type=int)
    p.add_argument("station")
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("loans", help="列出全部借出包")
    p.set_defaults(func=cmd_loans)

    p = sub.add_parser("markers", help="查看标记（站/路线/声段/漏入）")
    p.set_defaults(func=cmd_markers)

    p = sub.add_parser("licenses", help="查看许可证及生效状态")
    p.set_defaults(func=cmd_licenses)

    p = sub.add_parser("withdraw", help="撤回许可证（当事人收回同意）")
    p.add_argument("license_id")
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("restore", help="恢复许可证")
    p.add_argument("license_id")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("close-edge", help="关闭两站间通路（改道）")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--reason", default="施工改道")
    p.set_defaults(func=cmd_close_edge)

    p = sub.add_parser("reopen-edge", help="恢复通路")
    p.add_argument("a")
    p.add_argument("b")
    p.set_defaults(func=cmd_reopen_edge)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except GuideError as exc:
        print(f"✗ {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
