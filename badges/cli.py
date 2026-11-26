#!/usr/bin/env python3
"""CLI entry point.

    python3 -m badges.cli serve [--port N]      # start the local HTTP server
    python3 -m badges.cli demo                  # run the recorded micro demo
    python3 -m badges.cli replay <file.jsonl>   # replay a call log
"""

from __future__ import annotations

import argparse
import json
import sys

from .server import make_server, Replayer
from .service import Service


def cmd_serve(args) -> int:
    httpd, _ = make_server(args.host, args.port, Service())
    host, port = httpd.server_address
    print(f"listening on http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def cmd_demo(args) -> int:
    import runpy
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "demo", "run_demo.py")
    sys.argv = [path]
    runpy.run_path(path, run_name="__main__")
    return 0


def cmd_replay(args) -> int:
    result = Replayer(args.file).run(Service())
    print(json.dumps({"ok": result["ok"], "entries": result["entries"],
                      "mismatch_count": len(result["mismatches"])}, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="badges")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the local HTTP service")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    d = sub.add_parser("demo", help="run the recorded micro demo")
    d.set_defaults(func=cmd_demo)

    r = sub.add_parser("replay", help="replay a recorded call log")
    r.add_argument("file")
    r.set_defaults(func=cmd_replay)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
