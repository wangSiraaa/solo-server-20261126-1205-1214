"""Command line and two-terminal interactive client."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from .scenarios import late_seven_config, main_config, race_config, swapped_diagonal_config

SCENARIOS = {
    "main": main_config,
    "late7": late_seven_config,
    "race": race_config,
    "swapdiag": swapped_diagonal_config,
}


class ClientError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def request(url: str, action: str, params: dict[str, Any] | None = None) -> Any:
    body = json.dumps({"action": action, "params": params or {}}, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api/act",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            envelope = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode())
        err = payload.get("error", {})
        raise ClientError(
            f"{err.get('code')}: {err.get('message')}\n  details={json.dumps(err.get('details') or {}, ensure_ascii=False)}",
            code=err.get("code"),
            details=err.get("details") or {},
        ) from exc
    except urllib.error.URLError as exc:
        raise ClientError(f"连不上后台 {url}：{exc.reason}") from exc
    if not envelope.get("ok"):
        raise ClientError(str(envelope.get("error")))
    return envelope["data"]


def show(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _params(args: argparse.Namespace) -> dict[str, Any]:
    return json.loads(args.json_params) if getattr(args, "json_params", None) else {}


def _add_game(p: argparse.ArgumentParser) -> None:
    p.add_argument("--url", default="http://127.0.0.1:8765", help="后台地址")
    p.add_argument("--game", default="main", help="场次 ID")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bingo", description="本地云联欢贴后台终端")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动本地 JSON/HTTP 后台")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--state", default="data/state.json")

    p_do = sub.add_parser("do", help="发送任意 action")
    p_do.add_argument("--url", default="http://127.0.0.1:8765")
    p_do.add_argument("action")
    p_do.add_argument("--params", default="{}", help="JSON 对象")

    p_seal = sub.add_parser("seal-tape", help="在报名/建场前封存报号带")
    p_seal.add_argument("--url", default="http://127.0.0.1:8765")
    p_seal.add_argument("--tape-id", required=True)
    p_seal.add_argument("--numbers", required=True, help="逗号分隔，例如 2,5,7,1,3")

    p_seed = sub.add_parser("seed", help="写入示范场次")
    p_seed.add_argument("--url", default="http://127.0.0.1:8765")
    p_seed.add_argument("--scenario", choices=sorted(SCENARIOS), default="main")
    p_seed.add_argument("--game-id")

    for name, action in [
        ("open-window", "window.open"),
        ("close-window", "window.close"),
        ("reveal", "reveal.next"),
        ("replay", "tape.replay"),
        ("finish", "game.finish"),
        ("stickers", "stickers.list"),
        ("attempts", "attempts.list"),
        ("calls", "calls.list"),
        ("state", "state.get"),
    ]:
        p = sub.add_parser(name)
        _add_game(p)
        p.set_defaults(simple_action=action)
        if name in {"open-window", "close-window"}:
            p.add_argument("--window-id", default="W1")

    p_audit = sub.add_parser("audit")
    _add_game(p_audit)
    p_audit.add_argument("--card", required=True)

    p_stamp = sub.add_parser("stamp")
    _add_game(p_stamp)
    p_stamp.add_argument("--card", required=True)
    p_stamp.add_argument("--participant", required=True)
    p_stamp.add_argument("--number", type=int)
    p_stamp.add_argument("--row", type=int)
    p_stamp.add_argument("--col", type=int)
    p_stamp.add_argument("--card-version", type=int)
    p_stamp.add_argument("--terminal", default="cli")
    p_stamp.add_argument("--offline-note", action="store_true")

    p_swap = sub.add_parser("swap")
    _add_game(p_swap)
    p_swap.add_argument("--card", required=True)
    p_swap.add_argument("--participant", required=True)
    p_swap.add_argument("--position", required=True, choices=["TL", "TR", "BL", "BR"])
    p_swap.add_argument("--block", required=True)
    p_swap.add_argument("--card-version", type=int)
    p_swap.add_argument("--terminal", default="cli")

    p_call = sub.add_parser("call")
    _add_game(p_call)
    p_call.add_argument("--card", required=True)
    p_call.add_argument("--participant", required=True)
    p_call.add_argument("--line", required=True)
    p_call.add_argument("--card-version", type=int)
    p_call.add_argument("--terminal", default="cli")
    p_call.add_argument("--request-id")

    p_claim = sub.add_parser("claim")
    _add_game(p_claim)
    p_claim.add_argument("--call", required=True)
    p_claim.add_argument("--participant", required=True)
    p_claim.add_argument("--code", required=True)
    p_claim.add_argument("--terminal", default="cli")
    p_claim.add_argument("--request-id")

    p_shell = sub.add_parser("shell", help="交互式模拟终端")
    p_shell.add_argument("--url", default="http://127.0.0.1:8765")
    p_shell.add_argument("--game", default="main")
    p_shell.add_argument("--participant", required=True)
    p_shell.add_argument("--card", required=True)
    p_shell.add_argument("--terminal", default="terminal-shell")
    return parser


def run_simple(args: argparse.Namespace) -> None:
    params: dict[str, Any] = {"game_id": args.game}
    if getattr(args, "window_id", None):
        params["window_id"] = args.window_id
    show(request(args.url, args.simple_action, params))


def run_stamp(args: argparse.Namespace) -> None:
    if args.number is None and (args.row is None or args.col is None):
        raise ClientError("盖章需要 --number N，或 --row R --col C")
    params = {
        "game_id": args.game,
        "card_id": args.card,
        "participant_id": args.participant,
        "terminal_id": args.terminal,
    }
    if args.number is not None:
        params["number"] = args.number
    else:
        params.update(row=args.row, col=args.col)
    if args.card_version is not None:
        params["card_version"] = args.card_version
    if args.offline_note:
        params["source"] = "offline_note"
    show(request(args.url, "stamp", params))


def run_swap(args: argparse.Namespace) -> None:
    params = {
        "game_id": args.game,
        "card_id": args.card,
        "participant_id": args.participant,
        "position": args.position,
        "block_id": args.block,
        "terminal_id": args.terminal,
    }
    if args.card_version is not None:
        params["card_version"] = args.card_version
    show(request(args.url, "corner.swap", params))


def run_call(args: argparse.Namespace) -> None:
    params = {
        "game_id": args.game,
        "card_id": args.card,
        "participant_id": args.participant,
        "line_id": args.line.upper(),
        "terminal_id": args.terminal,
    }
    for key in ("card_version", "request_id"):
        value = getattr(args, key, None)
        if value is not None:
            params[key] = value
    show(request(args.url, "call.create", params))


def run_claim(args: argparse.Namespace) -> None:
    params = {
        "game_id": args.game,
        "call_id": args.call,
        "participant_id": args.participant,
        "claim_code": args.code,
        "terminal_id": args.terminal,
    }
    if args.request_id:
        params["request_id"] = args.request_id
    show(request(args.url, "claim.issue", params))


def _compact_card(audit: dict[str, Any]) -> None:
    print(f"card={audit['card_id']} version=v{audit['current_version']} owner={audit['participant_id']}")
    for row in audit["grid"]:
        print("  " + " ".join(f"{n:>2}" for n in row))
    complete = []
    locked = []
    for line_id, line in audit["lines"].items():
        if line["locked"]:
            locked.append(line_id)
        elif line["complete"]:
            complete.append(line_id)
    print(f"  已成线未锁: {', '.join(complete) or '无'}")
    print(f"  已兑奖锁定: {', '.join(locked) or '无'}")


def run_shell(args: argparse.Namespace) -> None:
    print(f"模拟终端 {args.terminal}：game={args.game} participant={args.participant} card={args.card}")
    print("输入 help 看命令；所有时间点以后端 beat/接收顺序为准。")
    while True:
        try:
            text = input(f"[{args.participant}@{args.game}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return
        if not text:
            continue
        parts = text.split()
        cmd, rest = parts[0].lower(), parts[1:]
        try:
            if cmd in {"exit", "quit"}:
                return
            if cmd == "help":
                print(
                    "sync|audit                      查看当前卡/线\n"
                    "stamp <号>                      按听到的号盖章\n"
                    "pos <row0-2> <col0-2>           按格位盖章\n"
                    "swap <TL|TR|BL|BR> <blockId>    一次换角\n"
                    "oldcall <D1|R1...>              用旧版 v1 试喊\n"
                    "call <D1|R1...>                 用当前有效版喊线\n"
                    "claim <callId> <code>           办领奖手续\n"
                    "calls|stickers|replay           查询\n"
                    "reveal|open|close               值班员操作"
                )
            elif cmd in {"sync", "audit"}:
                data = request(args.url, "card.audit", {"game_id": args.game, "card_id": args.card})
                _compact_card(data)
            elif cmd == "stamp" and rest:
                show(request(args.url, "stamp", {
                    "game_id": args.game, "card_id": args.card,
                    "participant_id": args.participant, "terminal_id": args.terminal,
                    "number": int(rest[0]),
                }))
            elif cmd == "pos" and len(rest) == 2:
                show(request(args.url, "stamp", {
                    "game_id": args.game, "card_id": args.card,
                    "participant_id": args.participant, "terminal_id": args.terminal,
                    "row": int(rest[0]), "col": int(rest[1]),
                }))
            elif cmd == "swap" and len(rest) == 2:
                show(request(args.url, "corner.swap", {
                    "game_id": args.game, "card_id": args.card,
                    "participant_id": args.participant, "terminal_id": args.terminal,
                    "position": rest[0].upper(), "block_id": rest[1],
                }))
            elif cmd == "call" and rest:
                show(request(args.url, "call.create", {
                    "game_id": args.game, "card_id": args.card,
                    "participant_id": args.participant, "terminal_id": args.terminal,
                    "line_id": rest[0].upper(),
                }))
            elif cmd == "oldcall" and rest:
                show(request(args.url, "call.create", {
                    "game_id": args.game, "card_id": args.card,
                    "participant_id": args.participant, "terminal_id": args.terminal,
                    "card_version": 1, "line_id": rest[0].upper(),
                }))
            elif cmd == "claim" and len(rest) == 2:
                show(request(args.url, "claim.issue", {
                    "game_id": args.game, "call_id": rest[0],
                    "participant_id": args.participant, "claim_code": rest[1],
                    "terminal_id": args.terminal,
                }))
            elif cmd == "calls":
                show(request(args.url, "calls.list", {"game_id": args.game}))
            elif cmd == "stickers":
                show(request(args.url, "stickers.list", {"game_id": args.game}))
            elif cmd == "replay":
                show(request(args.url, "tape.replay", {"game_id": args.game}))
            elif cmd == "reveal":
                show(request(args.url, "reveal.next", {"game_id": args.game}))
            elif cmd == "open":
                show(request(args.url, "window.open", {"game_id": args.game}))
            elif cmd == "close":
                show(request(args.url, "window.close", {"game_id": args.game}))
            else:
                print("不认识或参数不足，输入 help")
        except ClientError as exc:
            print(f"[拒绝] {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .server import main as server_main

            return server_main(["--host", args.host, "--port", str(args.port), "--state", args.state])
        if args.command == "shell":
            run_shell(args)
            return 0
        if args.command == "do":
            show(request(args.url, args.action, json.loads(args.params)))
            return 0
        if args.command == "seal-tape":
            numbers = [int(x.strip()) for x in args.numbers.split(",") if x.strip()]
            show(request(args.url, "tape.seal", {"tape_id": args.tape_id, "order": numbers}))
            return 0
        if args.command == "seed":
            factory = SCENARIOS[args.scenario]
            params = factory(args.game_id) if args.game_id else factory()
            show(request(args.url, "game.create", params))
            return 0
        if hasattr(args, "simple_action"):
            run_simple(args)
            return 0
        if args.command == "audit":
            show(request(args.url, "card.audit", {"game_id": args.game, "card_id": args.card}))
            return 0
        if args.command == "stamp":
            run_stamp(args)
            return 0
        if args.command == "swap":
            run_swap(args)
            return 0
        if args.command == "call":
            run_call(args)
            return 0
        if args.command == "claim":
            run_claim(args)
            return 0
        parser.error("unhandled command")
    except ClientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0
