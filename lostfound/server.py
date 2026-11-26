from __future__ import annotations

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import services
from .db import connect
from .seed import seed_dolphins

WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "LostFoundLocal/1.0"

    @property
    def db_path(self) -> str:
        return self.server.db_path  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep test output readable; errors are still returned as JSON.
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise services.ApiError(413, "body_too_large", "请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise services.ApiError(400, "bad_json", f"JSON 解析失败：{exc}") from exc
        if not isinstance(data, dict):
            raise services.ApiError(400, "bad_json_object", "请求体必须是 JSON 对象")
        return data

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        parts = [p for p in path.split("/") if p]
        query = dict(pair.split("=", 1) for pair in parsed.query.split("&") if "=" in pair)

        if method == "GET" and path in ("/", "/index.html"):
            self._send_html(WEB_INDEX)
            return
        if method == "GET" and path in ("/health", "/api/health"):
            self._send_json({"ok": True, "service": "lostfound-local"})
            return

        conn = connect(self.db_path)
        try:
            body = self._read_json() if method in ("POST", "PATCH", "PUT") else {}
            result, status = self._route(conn, method, parts, query, body)
            self._send_json(result, status)
        except services.ApiError as exc:
            self._send_json(
                {"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
                exc.status,
            )
        except Exception as exc:  # noqa: BLE001 - local JSON API boundary
            self._send_json({"error": {"code": "internal_error", "message": str(exc)}}, 500)
        finally:
            conn.close()

    def _route(
        self,
        conn: Any,
        method: str,
        parts: list[str],
        query: dict[str, str],
        body: dict[str, Any],
    ) -> tuple[Any, int]:
        # /api/...
        if not parts or parts[0] != "api":
            if method == "GET" and parts == ["favicon.ico"]:
                return {"error": "no icon"}, 204
            raise services.ApiError(404, "not_found", "入口不存在；打开 / 使用控制台")

        rest = parts[1:]

        if rest == ["stock-check"] and method == "GET":
            return services.stock_check(conn), 200

        if rest == ["admin", "seed-dolphins"] and method == "POST":
            return seed_dolphins(conn, reset=bool(body.get("reset", False))), 200

        if rest == ["rounds"] and method == "POST":
            return services.create_round(conn, body), 201
        if rest == ["rounds"] and method == "GET":
            rows = conn.execute("SELECT round_id,title,window_mode,complete_pairing,allow_unfound FROM rounds ORDER BY created_at,round_id").fetchall()
            return {"rounds": [dict(r) for r in rows]}, 200

        if len(rest) == 2 and rest[0] == "rounds" and method == "GET":
            return services.get_round(conn, rest[1]), 200
        if len(rest) == 3 and rest[0] == "rounds" and rest[2] == "registration" and method == "PATCH":
            return services.update_registration(conn, rest[1], body), 200
        if len(rest) == 3 and rest[0] == "rounds" and rest[2] == "confirmed-pairs" and method == "POST":
            body["round_id"] = rest[1]
            return services.import_confirmed_pairs(conn, body), 201
        if rest == ["confirmed-pairs"] and method == "POST":
            return services.import_confirmed_pairs(conn, body), 201
        if len(rest) == 3 and rest[0] == "rounds" and rest[2] == "reconcile" and method in ("GET", "POST"):
            if method == "POST" and body.get("auto_sign"):
                return services.sign_determinate(
                    conn, rest[1], str(body.get("window_id") or "desk")
                ), 200
            return services.reconcile_round(conn, rest[1]), 200
        if len(rest) == 3 and rest[0] == "rounds" and rest[2] == "sign-determinate" and method == "POST":
            return services.sign_determinate(
                conn, rest[1], str(body.get("window_id") or "desk")
            ), 200
        if len(rest) == 3 and rest[0] == "rounds" and rest[2] == "plan-inspection" and method == "GET":
            return services.plan_inspections(conn, rest[1]), 200
        if len(rest) == 3 and rest[0] == "rounds" and rest[2] == "next-question" and method == "GET":
            claim_no = query.get("claim_no")
            return services.recommend_question(conn, rest[1], claim_no), 200

        if rest == ["items"] and method == "POST":
            return services.create_item(conn, body), 201
        if len(rest) == 2 and rest[0] == "items" and method == "GET":
            return services.public_item_view(conn, rest[1]), 200
        if len(rest) == 3 and rest[0] == "items" and rest[2] == "unclaimed" and method == "POST":
            return services.mark_item_unclaimed(
                conn, rest[1], str(body.get("note") or "")
            ), 200

        if rest == ["claims"] and method == "POST":
            return services.create_claim(conn, body), 201
        if len(rest) == 2 and rest[0] == "claims" and method == "GET":
            return services.owner_view(conn, rest[1]), 200
        if len(rest) == 3 and rest[0] == "claims" and rest[2] == "record" and method == "GET":
            return services.claim_record(conn, rest[1]), 200
        if len(rest) == 3 and rest[0] == "claims" and rest[2] == "no-match" and method == "POST":
            return services.mark_claim_no_match(
                conn, rest[1], str(body.get("note") or "")
            ), 200
        if len(rest) == 3 and rest[0] == "claims" and rest[2] == "revisit" and method == "POST":
            body["claim_no"] = rest[1]
            return services.answer_revisit(conn, body), 200

        if rest == ["inspections"] and method == "POST":
            return services.create_inspection(conn, body), 201

        if rest == ["vouchers"] and method == "POST":
            return services.create_voucher(conn, body), 201
        if len(rest) == 2 and rest[0] == "vouchers" and method == "GET":
            return services.voucher_view(conn, rest[1]), 200

        if rest == ["outbound"] and method == "POST":
            return services.outbound(conn, body), 200

        if len(rest) == 2 and rest[0] in ("track", "numbers") and method == "GET":
            return services.track_number(conn, rest[1]), 200

        if rest == ["shifts"] and method == "POST":
            return services.create_shift(conn, body), 201
        if len(rest) == 2 and rest[0] == "shifts" and method == "GET":
            return services.get_shift(conn, rest[1]), 200
        if len(rest) == 3 and rest[0] == "shifts" and rest[2] == "prepare" and method == "POST":
            return services.prepare_shift(
                conn, rest[1], body.get("item_nos") or []
            ), 200

        raise services.ApiError(404, "route_not_found", f"没有这个入口：{'/'.join(rest)}")

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    db_path = Path(db_path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.db_path = str(db_path)  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="社区运动馆失物本地受理/核对/签单/出库/复查服务")
    parser.add_argument("--db", default=os.environ.get("LOSTFOUND_DB", "lostfound.db"), help="SQLite 数据库路径")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--seed-demo", action="store_true", help="写入海豚壶示例")
    parser.add_argument("--reset-demo", action="store_true", help="重置并写入海豚壶示例")
    parser.add_argument("--seed-only", action="store_true", help="写完示例后退出，不启动 HTTP 服务")
    args = parser.parse_args()

    if args.seed_demo or args.reset_demo:
        conn = connect(args.db)
        try:
            print(json.dumps(seed_dolphins(conn, reset=args.reset_demo), ensure_ascii=False, indent=2))
        finally:
            conn.close()
        if args.seed_only:
            return

    # Startup consistency check makes restart/storage problems visible early.
    conn = connect(args.db)
    try:
        check = services.stock_check(conn)
        if not check["consistent"]:
            print("WARNING stock mismatch:", json.dumps(check, ensure_ascii=False))
        else:
            print(f"Storage OK: {args.db}; handoffs={check['handoff_count']}")
    finally:
        conn.close()

    httpd = create_server(args.db, args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"Lost-and-found console: {url}")
    print("API health: /api/health")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nclosing ...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
