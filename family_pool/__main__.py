from __future__ import annotations

import argparse
import sys

from .http_app import run_server
from .service import FamilyPoolService
from .seed import seed_demo_world


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="家庭戏水馆中途替班本地后台")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 HTTP 后台")
    serve.add_argument("--db", default="data/family-pool.db")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--seed-demo", action="store_true", help="仅在空库中播种三区五腕带")

    init = sub.add_parser("init-db", help="初始化/播种演示数据")
    init.add_argument("--db", default="data/family-pool.db")
    init.add_argument("--seed-demo", action="store_true", help="幂等播种演示数据")

    args = parser.parse_args(argv)
    if args.command in (None, "serve"):
        run_server(args.db, args.host, args.port, seed=getattr(args, "seed_demo", False))
        return 0
    if args.command == "init-db":
        svc = FamilyPoolService(args.db)
        if args.seed_demo:
            result = seed_demo_world(svc)
            print(result)
        else:
            print(f"initialized {args.db}")
        svc.close()
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
