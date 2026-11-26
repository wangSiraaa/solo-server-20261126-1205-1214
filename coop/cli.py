"""Command line entrypoint. Invoke once per physical/local service window."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .client import LocalHttpClient


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One local cooperative business client")
    parser.add_argument("--client", default="local-cli", help="stable local terminal/client id")
    parser.add_argument("--base-url", default="http://127.0.0.1:8931")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="check local backend health")

    call = sub.add_parser("call", help="call an RPC method with a JSON object")
    call.add_argument("method")
    call.add_argument("params_json", nargs="?", default="{}")
    call.add_argument("--idem-key", default=None)

    args = parser.parse_args(argv)
    client = LocalHttpClient(args.client, args.base_url)

    if args.command == "health":
        response = client.call("health")
    else:
        try:
            params = json.loads(args.params_json)
            if not isinstance(params, dict):
                raise ValueError("params JSON must be an object")
        except (json.JSONDecodeError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": {"message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
            return 2
        response = client.call(args.method, params, idempotency_key=args.idem_key)

    print_json(response)
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
