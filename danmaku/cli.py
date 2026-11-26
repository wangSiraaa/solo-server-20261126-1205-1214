"""本机命令行：主持人、买家、播控/请求方、审计回放共用一个可执行入口。

用法示例见 README。所有数据落在 --dir 指定目录，重启只需重新 open。
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import Engine
from .player import Player, replay
from .store import Store, replay_dir


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _open(dirpath: str) -> Engine:
    return Engine(Store.open(dirpath))


def cmd_init(args) -> None:
    store = Store.create(args.dir, args.show_id, args.screen_id,
                         args.width, args.tracks, args.mode)
    _print({"created": True, "dir": args.dir,
            "state": store.state.to_dict()})


def cmd_occlude(args) -> None:
    eng = _open(args.dir)
    _print(eng.add_occlusion(args.occ_id, args.from_beat, args.to_beat,
                             args.track, args.left, args.right))


def cmd_unocclude(args) -> None:
    eng = _open(args.dir)
    _print(eng.remove_occlusion(args.occ_id))


def cmd_buy(args) -> None:
    eng = _open(args.dir)
    tracks = args.tracks or None
    if eng.s.mode == "presale":
        _print(eng.place_presale_order(
            args.order_id, args.buyer, args.width, args.speed,
            args.window_start, args.window_end,
            allowed_tracks=tracks, note=args.note))
    else:
        _print(eng.place_pool_order(
            args.order_id, args.buyer, args.width, args.speed,
            args.window_start, args.window_end, args.quantity,
            allowed_tracks=tracks, note=args.note))


def cmd_dispatch(args) -> None:
    eng = _open(args.dir)
    _print(eng.dispatch_presale(args.order_id))


def cmd_claim(args) -> None:
    eng = _open(args.dir)
    _print(Player(eng).claim())


def cmd_receipt(args) -> None:
    eng = _open(args.dir)
    _print(eng.receipt(args.receipt_id, args.instr_id, args.beat))


def cmd_progress(args) -> None:
    eng = _open(args.dir)
    _print(eng.progress(args.beat))


def cmd_stop(args) -> None:
    eng = _open(args.dir)
    _print(eng.stop_playback(args.instr_id, args.beat, args.reason))


def cmd_owed(args) -> None:
    eng = _open(args.dir)
    _print(eng.owed_report())


def cmd_frame(args) -> None:
    eng = _open(args.dir)
    print(json.dumps(eng.grid_view(args.beat), ensure_ascii=False,
                     indent=2, sort_keys=True))


def cmd_replay(args) -> None:
    eng = _open(args.dir)
    if args.text:
        print(replay(eng, args.from_beat, args.to_beat))
        return
    replayed, events, disk = replay_dir(args.dir)
    _print({
        "event_count": len(events),
        "replay_equals_disk": replayed.to_dict() == disk.to_dict(),
        "events": [e["type"] for e in events],
    })


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="danmaku",
                                description="付费弹幕履约/排期本机工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("init", help="开播：建节目目录")
    q.add_argument("--dir", required=True)
    q.add_argument("--show-id", required=True)
    q.add_argument("--screen-id", required=True)
    q.add_argument("--width", type=int, required=True)
    q.add_argument("--tracks", type=int, required=True)
    q.add_argument("--mode", choices=["presale", "pool"], required=True)
    q.set_defaults(func=cmd_init)

    q = sub.add_parser("occlude", help="主持人加不可遮盖字幕")
    q.add_argument("--dir", required=True)
    q.add_argument("--occ-id", required=True)
    q.add_argument("--from-beat", type=int, required=True)
    q.add_argument("--to-beat", type=int, required=True)
    q.add_argument("--track", type=int, required=True)
    q.add_argument("--left", type=int, required=True)
    q.add_argument("--right", type=int, required=True)
    q.set_defaults(func=cmd_occlude)

    q = sub.add_parser("unocclude", help="主持人撤字幕并自动重排")
    q.add_argument("--dir", required=True)
    q.add_argument("--occ-id", required=True)
    q.set_defaults(func=cmd_unocclude)

    q = sub.add_parser("buy", help="买家下单（预售票或候播池）")
    q.add_argument("--dir", required=True)
    q.add_argument("--order-id", required=True)
    q.add_argument("--buyer", required=True)
    q.add_argument("--width", type=int, required=True)
    q.add_argument("--speed", type=int, required=True)
    q.add_argument("--window-start", type=int, required=True)
    q.add_argument("--window-end", type=int, required=True)
    q.add_argument("--quantity", type=int, default=1)
    q.add_argument("--tracks", type=int, nargs="*")
    q.add_argument("--note", default="")
    q.set_defaults(func=cmd_buy)

    q = sub.add_parser("dispatch", help="播控领取预售票的播出指令")
    q.add_argument("--dir", required=True)
    q.add_argument("--order-id", required=True)
    q.set_defaults(func=cmd_dispatch)

    q = sub.add_parser("claim", help="请求方取走出件箱指令")
    q.add_argument("--dir", required=True)
    q.set_defaults(func=cmd_claim)

    q = sub.add_parser("receipt", help="该屏回传开播回条")
    q.add_argument("--dir", required=True)
    q.add_argument("--receipt-id", required=True)
    q.add_argument("--instr-id", required=True)
    q.add_argument("--beat", type=int, required=True)
    q.set_defaults(func=cmd_receipt)

    q = sub.add_parser("progress", help="回传真实播放进度并续派/结清")
    q.add_argument("--dir", required=True)
    q.add_argument("--beat", type=int, required=True)
    q.set_defaults(func=cmd_progress)

    q = sub.add_parser("stop", help="中途停播一条在途指令")
    q.add_argument("--dir", required=True)
    q.add_argument("--instr-id", required=True)
    q.add_argument("--beat", type=int, required=True)
    q.add_argument("--reason", default="manual")
    q.set_defaults(func=cmd_stop)

    q = sub.add_parser("owed", help="查询实际还欠谁几个完整展示")
    q.add_argument("--dir", required=True)
    q.set_defaults(func=cmd_owed)

    q = sub.add_parser("frame", help="查看某一拍屏幕快照")
    q.add_argument("--dir", required=True)
    q.add_argument("--beat", type=int, required=True)
    q.set_defaults(func=cmd_frame)

    q = sub.add_parser("replay", help="事件日志重放核对 / 文本回放")
    q.add_argument("--dir", required=True)
    q.add_argument("--from-beat", type=int, default=1)
    q.add_argument("--to-beat", type=int)
    q.add_argument("--text", action="store_true",
                   help="输出逐拍屏幕文本而非事件核对")
    q.set_defaults(func=cmd_replay)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
