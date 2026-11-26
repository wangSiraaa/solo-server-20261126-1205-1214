#!/usr/bin/env python3
"""Replay a recorded call log against a fresh service and report drift.

Usage::

    python3 -m badges.replay demo/calls.jsonl

Exit code 0 when every replayed call reproduces exactly, 1 otherwise.
"""

from __future__ import annotations

import json
import sys

from .server import Replayer
from .service import Service


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print("usage: python3 -m badges.replay <calls.jsonl>", file=sys.stderr)
        return 2
    result = Replayer(argv[0]).run(Service())
    print(
        json.dumps(
            {
                "ok": result["ok"],
                "entries": result["entries"],
                "mismatch_count": len(result["mismatches"]),
                "first_mismatch": result["mismatches"][0] if result["mismatches"] else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
