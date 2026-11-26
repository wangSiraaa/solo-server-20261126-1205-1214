#!/usr/bin/env python3
"""Local HTTP API for the remote dubbing-stage evidence service.

Run:
    python3 server.py --port 8787 --db data/studio.sqlite3

The server intentionally has no third-party dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from studio_core import ApiError, StudioService

SERVICE: StudioService | None = None


def get_service() -> StudioService:
    assert SERVICE is not None
    return SERVICE


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalDubStudio/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep test/demo output readable; errors are still returned as JSON.
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- response/request helpers ----------

    def send_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, err: ApiError) -> None:
        self.send_json(err.status, {
            "error": {"code": err.code, "message": err.message, "details": err.details}
        })

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "invalid_json", f"请求体不是合法 JSON: {exc}")
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
        return value

    def token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return self.headers.get("X-Session-Token")

    def handle_api(self, method: str) -> None:
        try:
            self.route(method)
        except ApiError as err:
            self.send_error_json(err)
        except Exception as exc:  # noqa: BLE001 - local API should return JSON
            self.send_json(500, {"error": {"code": "internal_error",
                                           "message": str(exc),
                                           "type": type(exc).__name__}})

    def do_GET(self) -> None:  # noqa: N802
        self.handle_api("GET")

    def do_POST(self) -> None:  # noqa: N802
        self.handle_api("POST")

    # ---------- routing ----------

    def route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        svc = get_service()

        if method == "GET" and path == "/health":
            self.send_json(200, {"ok": True, "service": "remote-dub-studio"})
            return
        if method == "GET" and path == "/":
            self.send_json(200, self.api_index())
            return

        if method == "POST" and path == "/api/sessions":
            body = self.read_json()
            self.send_json(201, svc.create_session(str(body.get("name") or ""),
                                                   str(body.get("role") or "")))
            return

        if method == "POST" and path == "/api/studios":
            body = self.read_json()
            self.send_json(201, svc.create_studio(str(body.get("name") or "未命名棚"),
                                                 self.token() or ""))
            return

        m = re.fullmatch(r"/api/studios/([^/]+)", path)
        if method == "GET" and m:
            row = svc.get_studio_or_404(m.group(1))
            self.send_json(200, svc.studio_dict(row))
            return

        m = re.fullmatch(r"/api/studios/([^/]+)/overview", path)
        if method == "GET" and m:
            self.send_json(200, self.overview(svc, m.group(1)))
            return

        m = re.fullmatch(r"/api/studios/([^/]+)/script", path)
        if method == "POST" and m:
            body = self.read_json()
            self.send_json(201, svc.create_script(m.group(1), body, self.token()))
            return

        m = re.fullmatch(r"/api/studios/([^/]+)/rehearsals", path)
        if method == "POST" and m:
            body = self.read_json()
            self.send_json(201, svc.create_rehearsal(m.group(1), body, self.token()))
            return

        m = re.fullmatch(r"/api/studios/([^/]+)/mutual-recognitions", path)
        if method == "POST" and m:
            body = self.read_json()
            self.send_json(201, svc.create_recognition(m.group(1), body, self.token()))
            return

        m = re.fullmatch(r"/api/rehearsals/([^/]+)/close", path)
        if method == "POST" and m:
            self.send_json(200, svc.close_rehearsal(m.group(1), self.token()))
            return

        m = re.fullmatch(r"/api/rehearsals/([^/]+)", path)
        if method == "GET" and m:
            self.send_json(200, svc.rehearsal_dict(svc.get_rehearsal_row(m.group(1))))
            return

        if method == "POST" and path == "/api/recordings/start":
            body = self.read_json()
            self.send_json(201, svc.start_recording(body, self.token()))
            return

        m = re.fullmatch(r"/api/recordings/([^/]+)/complete", path)
        if method == "POST" and m:
            body = self.read_json()
            self.send_json(200, svc.complete_recording(m.group(1), body, self.token()))
            return

        m = re.fullmatch(r"/api/recordings/([^/]+)", path)
        if method == "GET" and m:
            self.send_json(200, svc.recording_dict(svc.get_recording_row(m.group(1))))
            return

        if method == "POST" and path == "/api/deliveries":
            body = self.read_json()
            self.send_json(201, svc.send_delivery(body, self.token()))
            return

        m = re.fullmatch(r"/api/deliveries/([^/]+)", path)
        if method == "GET" and m:
            self.send_json(200, svc.delivery_dict(svc.get_delivery_row(m.group(1))))
            return

        if method == "POST" and path == "/api/playback-confirmations":
            body = self.read_json()
            key = self.headers.get("Idempotency-Key")
            status, payload = svc.confirm_playback(body, self.token(), key)
            self.send_json(status, payload)
            return

        if method == "POST" and path == "/api/self-reports":
            body = self.read_json()
            self.send_json(201, svc.create_self_report(body, self.token()))
            return

        m = re.fullmatch(r"/api/mutual-recognitions/([^/]+)/revoke", path)
        if method == "POST" and m:
            self.send_json(200, svc.revoke_recognition(m.group(1), self.token()))
            return

        if method == "POST" and path == "/api/sheets/validate":
            body = self.read_json()
            self.send_json(200, svc.validate_only(body, self.token()))
            return

        if method == "POST" and path == "/api/sheets":
            body = self.read_json()
            self.send_json(201, svc.create_sheet(body, self.token()))
            return

        m = re.fullmatch(r"/api/sheets/([^/]+)/sign", path)
        if method == "POST" and m:
            self.send_json(200, svc.sign_sheet(m.group(1), self.token()))
            return

        m = re.fullmatch(r"/api/sheets/([^/]+)/verify", path)
        if method == "GET" and m:
            self.send_json(200, svc.verify_sheet(m.group(1)))
            return

        m = re.fullmatch(r"/api/sheets/([^/]+)", path)
        if method == "GET" and m:
            self.send_json(200, svc.sheet_dict(svc.get_sheet_row(m.group(1))))
            return

        if method == "POST" and path == "/api/pickup-plans":
            body = self.read_json()
            self.send_json(201, svc.create_pickup_plan(body, self.token()))
            return

        m = re.fullmatch(r"/api/pickup-plans/([^/]+)/simulate", path)
        if method == "POST" and m:
            body = self.read_json()
            self.send_json(200, svc.simulate_pickup_plan(m.group(1), self.token(),
                                                        body.get("wordsByLine")))
            return

        m = re.fullmatch(r"/api/pickup-plans/([^/]+)/sheet", path)
        if method == "POST" and m:
            body = self.read_json()
            self.send_json(201, svc.create_sheet_from_plan(
                m.group(1), self.token(), body.get("title")))
            return

        m = re.fullmatch(r"/api/pickup-plans/([^/]+)", path)
        if method == "GET" and m:
            self.send_json(200, svc.plan_dict(svc.get_plan_row(m.group(1))))
            return

        m = re.fullmatch(r"/api/scripts/([^/]+)/casting-matrix", path)
        if method == "GET" and m:
            self.send_json(200, svc.casting_matrix(m.group(1), self.token()))
            return

        m = re.fullmatch(r"/api/scripts/([^/]+)", path)
        if method == "GET" and m:
            self.send_json(200, svc.get_script(m.group(1)))
            return

        self.send_json(404, {"error": {"code": "route_not_found",
                                       "message": f"没有 {method} {path} 路由"}})

    @staticmethod
    def api_index() -> dict[str, Any]:
        return {
            "name": "remote-dub-studio",
            "maxLinesPerAct": 4,
            "maxTakesPerLine": 3,
            "endpoints": [
                "POST /api/sessions",
                "POST /api/studios",
                "POST /api/studios/{studioId}/script",
                "POST /api/studios/{studioId}/rehearsals",
                "POST /api/rehearsals/{rehearsalId}/close",
                "POST /api/recordings/start",
                "POST /api/recordings/{recordingId}/complete",
                "POST /api/deliveries",
                "POST /api/playback-confirmations",
                "POST /api/self-reports",
                "POST /api/studios/{studioId}/mutual-recognitions",
                "POST /api/mutual-recognitions/{id}/revoke",
                "POST /api/sheets/validate",
                "POST /api/sheets",
                "POST /api/sheets/{sheetId}/sign",
                "GET  /api/sheets/{sheetId}",
                "POST /api/pickup-plans",
                "POST /api/pickup-plans/{planId}/simulate",
                "GET  /api/scripts/{scriptId}/casting-matrix",
                "GET  /api/studios/{studioId}/overview",
            ],
        }

    @staticmethod
    def overview(svc: StudioService, studio_id: str) -> dict[str, Any]:
        svc.get_studio_or_404(studio_id)
        scripts = svc.query_all("SELECT id FROM scripts WHERE studio_id=?", (studio_id,))
        sheets = svc.query_all("SELECT id FROM sheets WHERE studio_id=? ORDER BY created_seq",
                               (studio_id,))
        recordings = svc.query_all(
            "SELECT id,line_index,take_no,status,rehearsal_id FROM recordings WHERE studio_id=? ORDER BY line_index,take_no",
            (studio_id,),
        )
        return {
            "studioId": studio_id,
            "scriptIds": [r["id"] for r in scripts],
            "sheetIds": [r["id"] for r in sheets],
            "recordings": [dict(r) for r in recordings],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Local remote dubbing studio HTTP service")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    parser.add_argument("--db", default=os.environ.get("STUDIO_DB", "data/studio.sqlite3"))
    args = parser.parse_args()

    global SERVICE
    SERVICE = StudioService(args.db)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"remote-dub-studio listening on http://{args.host}:{args.port} db={args.db}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        SERVICE.close()


if __name__ == "__main__":
    main()
