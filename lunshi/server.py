"""Standard-library HTTP service for the rotating market.

Run:
    python3 -m lunshi.server --db data/lunshi.sqlite3 --port 8765
Then open http://127.0.0.1:8765/
"""
from __future__ import annotations

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:  # Support both `python3 -m lunshi.server` and `python3 lunshi/server.py`.
    from .scenario import seed_world
    from .service import ApiError, LunshiService
except ImportError:  # pragma: no cover - direct script execution
    from scenario import seed_world  # type: ignore
    from service import ApiError, LunshiService  # type: ignore

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def build_handler(svc: LunshiService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Lunshi/1.0"

        def log_message(self, fmt: str, *args) -> None:  # quieter local demo
            if os.environ.get("LUNSHI_HTTP_LOG"):
                super().log_message(fmt, *args)

        # ---------- response helpers ----------
        def send_json(self, status: int, payload) -> None:
            raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else {"_body": data}
            except json.JSONDecodeError as exc:
                raise ApiError(400, "BAD_JSON", "请求体不是合法 JSON") from exc

        def idem_key(self, data: dict) -> str | None:
            return self.headers.get("Idempotency-Key") or data.get("idempotency_key")

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                qs = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                if path.startswith("/api/"):
                    self.handle_api_get(path, qs)
                else:
                    self.serve_static(path)
            except ApiError as exc:
                self.send_json(exc.status, {"error": {"code": exc.code, "message": exc.message, "details": exc.details}})
            except Exception as exc:  # pragma: no cover - local server guard
                self.send_json(500, {"error": {"code": "INTERNAL", "message": str(exc)}})

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                data = self.read_json()
                self.handle_api_post(parsed.path, data)
            except ApiError as exc:
                self.send_json(exc.status, {"error": {"code": exc.code, "message": exc.message, "details": exc.details}})
            except Exception as exc:  # pragma: no cover
                self.send_json(500, {"error": {"code": "INTERNAL", "message": str(exc)}})

        # ---------- static pages ----------
        def serve_static(self, path: str) -> None:
            if path in ("", "/"):
                path = "/index.html"
            # prevent path traversal
            rel = path.lstrip("/")
            file_path = (WEB_DIR / rel).resolve()
            if WEB_DIR.resolve() not in file_path.parents and file_path != WEB_DIR / "index.html":
                self.send_error(403)
                return
            if not file_path.is_file():
                file_path = WEB_DIR / "index.html"
            raw = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_TYPES.get(file_path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        # ---------- GET API ----------
        def handle_api_get(self, path: str, qs: dict[str, str]) -> None:
            if path == "/api/health":
                self.send_json(200, {"ok": True, "service": "lunshi", "db": svc.db_path})
                return
            if path == "/api/townships":
                self.send_json(200, svc.list_townships())
                return
            if path == "/api/conventions":
                self.send_json(200, svc.list_conventions())
                return
            if path == "/api/spots":
                self.send_json(200, svc.list_spots(qs.get("township_id")))
                return
            if path == "/api/calls":
                self.send_json(200, svc.list_calls(qs.get("convention_id")))
                return
            if path == "/api/loans":
                self.send_json(200, svc.list_loans())
                return
            if path == "/api/audit":
                self.send_json(200, svc.audit_logs(int(qs.get("limit", "200"))))
                return
            if path == "/api/street":
                day = int(qs["day_no"]) if qs.get("day_no") else None
                self.send_json(200, svc.street_news(day, qs.get("iso_date"), qs.get("month")))
                return
            if path == "/api/street/today":
                day = svc.get_meta("today_day_no")
                if day is None:
                    raise ApiError(404, "NO_TODAY", "尚未约定当前日序")
                self.send_json(200, svc.street_news(day_no=int(day)))
                return
            if path == "/api/ledger":
                cid = qs.get("convention_id", "LUN-SANXIANG")
                self.send_json(200, svc.ledger(cid))
                return

            m = re.fullmatch(r"/api/vendors/([^/]+)", path)
            if m:
                self.send_json(200, svc.vendor_dossier(m.group(1)))
                return
            m = re.fullmatch(r"/api/permits/([^/]+)", path)
            if m:
                self.send_json(200, svc.get_permit(m.group(1)))
                return
            m = re.fullmatch(r"/api/groups/([^/]+)", path)
            if m:
                self.send_json(200, svc.get_group(m.group(1)))
                return
            raise ApiError(404, "API_NOT_FOUND", f"未知接口 {path}")

        # ---------- POST API ----------
        def handle_api_post(self, path: str, d: dict) -> None:
            key = self.idem_key(d)

            if path == "/api/admin/seed-standard":
                seed_world(svc)
                self.send_json(200, {"seeded": True, "convention_id": "LUN-SANXIANG"})
                return
            if path == "/api/today":
                self.send_json(200, svc.set_today(int(d["day_no"])))
                return
            if path == "/api/townships":
                self.send_json(200, svc.create_township(d["id"], d["name"], int(d["seq"])))
                return
            if path == "/api/sessions":
                self.send_json(200, svc.declare_session(
                    d["township_id"], int(d["day_no"]), int(d.get("slot_no", 1)),
                    d.get("iso_date"), d.get("title"), d.get("note")))
                return
            m = re.fullmatch(r"/api/sessions/(\d+)/rain-out", path)
            if m:
                # Numeric session id route is reserved; simpler township/day route below.
                self.send_json(200, svc.set_session_status(int(m.group(1)), "rained_out", d.get("reason", "落雨撤场")))
                return
            if path == "/api/sessions/rain-out":
                self.send_json(200, svc.rain_out_session(
                    d["township_id"], int(d["day_no"]), int(d.get("slot_no", 1)), d.get("reason", "落雨撤场")))
                return
            if path == "/api/spots":
                self.send_json(200, svc.add_spot(
                    d["id"], d["township_id"], d["code"], d.get("allowed_categories", []), d.get("label")))
                return
            if path == "/api/spots/link":
                self.send_json(200, svc.set_spots_linked(d["spot_a"], d["spot_b"], True))
                return
            if path == "/api/spots/separate":
                self.send_json(200, svc.set_spots_linked(
                    d["spot_a"], d["spot_b"], False, d.get("barrier", "wall")))
                return
            if path == "/api/conventions":
                self.send_json(200, svc.open_convention(
                    d["id"], d["name"], d.get("cycle", ["A", "B", "C"]), d.get("success_rule"),
                    d.get("method", "dynamic_call"), d.get("makeup_policy", "same_round_priority_retry")))
                return
            if path == "/api/vendors":
                self.send_json(200, svc.register_vendor(
                    d["id"], d["name"], d.get("categories", []), d.get("township_ids", []), d.get("contact")))
                return
            if path == "/api/applications":
                self.send_json(200, svc.submit_application(
                    d["id"], d["vendor_id"], d["township_id"], int(d["day_no"]), d["category"],
                    int(d.get("slot_no", 1)), d.get("convention_id"), d.get("group_id"),
                    d.get("stall_name"), bool(d.get("accepted", True)), key))
                return
            if path == "/api/groups/adjacent-pair":
                self.send_json(200, svc.create_adjacent_pair(
                    d["group_id"], d["township_id"], int(d["day_no"]), d["stalls"],
                    int(d.get("slot_no", 1)), d.get("convention_id", "LUN-SANXIANG"),
                    bool(d.get("require_adjacent", True)), d.get("note", "明确要求挨着开的双摊")))
                return
            m = re.fullmatch(r"/api/groups/([^/]+)/issue", path)
            if m:
                self.send_json(200, svc.issue_group_permits(m.group(1), d.get("spot_ids")))
                return
            if path == "/api/permits/issue":
                self.send_json(200, svc.issue_permit(d["application_id"], d["spot_id"], key))
                return
            if path == "/api/receipts":
                self.send_json(200, svc.report_open(
                    d["receipt_no"], d.get("permit_no"), key, d.get("request_hash"), d.get("actor", "vendor")))
                return
            m = re.fullmatch(r"/api/receipts/([^/]+)/withdraw", path)
            if m:
                self.send_json(200, svc.withdraw_receipt(m.group(1), d.get("reason", "撤摊"), d.get("actor", "committee")))
                return
            m = re.fullmatch(r"/api/receipts/([^/]+)/restore", path)
            if m:
                self.send_json(200, svc.restore_receipt(m.group(1), d.get("reason", "恢复真实开成")))
                return
            if path == "/api/calls/next":
                self.send_json(200, svc.call_next(
                    d.get("convention_id", "LUN-SANXIANG"), int(d.get("from_day", 1)),
                    int(d["day_no"]) if d.get("day_no") is not None else None,
                    int(d.get("slot_no", 1)), d.get("actor", "committee")))
                return
            m = re.fullmatch(r"/api/calls/([^/]+)/settle", path)
            if m:
                self.send_json(200, svc.evaluate_call(m.group(1), bool(d.get("finalize", False))))
                return
            m = re.fullmatch(r"/api/calls/([^/]+)/fail", path)
            if m:
                self.send_json(200, svc.fail_call(m.group(1), d.get("reason", "当日未办成")))
                return
            if path == "/api/loans":
                self.send_json(200, svc.create_loan(
                    d["id"], d["spot_id"], d["borrower_township_id"], int(d["day_no"]),
                    int(d.get("slot_no", 1)), d.get("purpose", "借地分流"), key))
                return
            if path == "/api/transfers":
                self.send_json(200, svc.transfer_booking(
                    d["application_id"], d["to_spot_id"], d.get("reason", "转场"),
                    int(d["to_day_no"]) if d.get("to_day_no") is not None else None,
                    int(d["to_slot_no"]) if d.get("to_slot_no") is not None else None, key))
                return
            raise ApiError(404, "API_NOT_FOUND", f"未知接口 {path}")

    return Handler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8765, seed: bool = False) -> ThreadingHTTPServer:
    svc = LunshiService(db_path)
    if seed:
        seed_world(svc)
    httpd = ThreadingHTTPServer((host, port), build_handler(svc))
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="三乡轮市服务")
    parser.add_argument("--db", default=os.environ.get("LUNSHI_DB", "data/lunshi.sqlite3"))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--seed", action="store_true", help="写入标准演示世界")
    args = parser.parse_args()
    httpd = serve(args.db, args.host, args.port, args.seed)
    print(f"轮市服务已开: http://{args.host}:{args.port}/  (db={args.db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收市，服务停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
