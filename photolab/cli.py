"""命令行入口：python -m photolab.cli serve --db lab.db --port 8080"""

from __future__ import annotations

import argparse

from .server import make_server


def main() -> None:
    parser = argparse.ArgumentParser(description="胶片照相馆本地接件放样后端（固定遮片暗房）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="启动本地 HTTP 服务")
    serve.add_argument("--db", default="lab.db")
    serve.add_argument("--products", default="products")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.cmd == "serve":
        httpd = make_server(args.db, args.products, args.host, args.port)
        print(f"暗房后端已启动: http://{args.host}:{args.port}  (db={args.db})")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.service.close()
            print("已关闭")


if __name__ == "__main__":
    main()
