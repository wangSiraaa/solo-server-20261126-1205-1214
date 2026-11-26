from __future__ import annotations

import json
import re
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .service import ApiError, FamilyPoolService

WEB_INDEX = Path(__file__).resolve().parent / "web" / "index.html"


class AppHandler(BaseHTTPRequestHandler):
    server_version = "FamilyPool/1.0"
    service: FamilyPoolService

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep test/demo output clean; errors are still returned as JSON.
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ApiError(400, "invalid_json_body", "请求体必须是 JSON 对象")
            return data
        except json.JSONDecodeError as exc:
            raise ApiError(400, "invalid_json", "JSON 解析失败", {"detail": str(exc)}) from exc

    def _idempotency_header(self, data: Dict[str, Any]) -> Dict[str, Any]:
        key = self.headers.get("Idempotency-Key")
        if key and not data.get("idempotency_key"):
            data["idempotency_key"] = key
        return data

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/api/health":
                self._send_json(200, {"ok": True, "service": "family-pool", "version": "1.0.0"})
                return
            if path == "/api/venue":
                self._send_json(200, self.service.get_venue())
                return
            if path == "/api/state":
                self._send_json(200, self.service.full_state(query.get("session_id")))
                return
            if path == "/api/answer":
                session_id = query.get("session_id")
                if not session_id:
                    raise ApiError(422, "session_id_required", "必须提供 session_id")
                self._send_json(200, self.service.answer(session_id))
                return
            if path == "/api/ledger":
                self._send_json(200, self.service.ledger(query.get("session_id"), int(query.get("limit", "200"))))
                return
            m = re.fullmatch(r"/api/handovers/([^/]+)", path)
            if m:
                self._send_json(200, self.service.get_handover(m.group(1)))
                return
            m = re.fullmatch(r"/api/permits/([^/]+)", path)
            if m:
                self._send_json(200, self.service.get_permit(m.group(1)))
                return
            m = re.fullmatch(r"/api/time-loans/([^/]+)", path)
            if m:
                self._send_json(200, self.service.get_time_loan(m.group(1)))
                return
            if path == "/" or path == "/index.html":
                html = WEB_INDEX.read_bytes() if WEB_INDEX.exists() else b"<h1>Family Pool API</h1>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return
            if path.startswith("/api/"):
                raise ApiError(404, "route_not_found", f"未知 API: {path}")
            # Convenience for static assets if added later.
            static_dir = WEB_INDEX.parent
            candidate = (static_dir / path.lstrip("/")).resolve()
            if str(candidate).startswith(str(static_dir.resolve())) and candidate.is_file():
                body = candidate.read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            raise ApiError(404, "not_found", path)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.code, "message": exc.message, "details": exc.details})
        except Exception as exc:  # pragma: no cover - HTTP boundary
            self._send_json(500, {"error": "internal_error", "message": str(exc)})

    def do_POST(self) -> None:  # noqa: N802,C901
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            data = self._idempotency_header(self._read_json())
            svc = self.service
            result: Any

            if path == "/api/demo/reset-and-seed":
                result = svc.reset_and_seed_demo()
            elif path == "/api/zones":
                result = svc.create_zone(data)
            elif path == "/api/ports":
                result = svc.create_port(data)
            elif path == "/api/visibility":
                result = svc.add_visibility(data)
            elif path == "/api/families":
                result = svc.create_family(data)
            elif path == "/api/family-consents":
                result = svc.add_consent(data)
            elif path == "/api/persons":
                result = svc.create_person(data)
            elif path == "/api/wristbands/issue":
                # 发腕带：登记 band_code、家庭、角色和成人陪同容量。
                result = svc.create_person(data)
            elif path == "/api/sessions":
                result = svc.open_session(data)
            elif path == "/api/aid-groups":
                result = svc.create_aid_group(data)
            elif path == "/api/plan-segments":
                result = svc.create_plan_segment(data)
            elif path == "/api/care/assignments":
                result = svc.assign_care(data)
            elif path == "/api/scans/gate-entry":
                result = svc.gate_entry(data)
            elif path == "/api/crossings/port":
                result = svc.cross_port(data)
            elif path == "/api/receipts/correct":
                result = svc.correct_receipt(data)
            elif path == "/api/handovers":
                result = svc.create_handover(data)
            elif path == "/api/handovers/prepare":
                result = svc.prepare_handover(data)
            elif path == "/api/permits":
                result = svc.issue_permit(data)
            elif path == "/api/time-accounts":
                result = svc.create_time_account(data)
            elif path == "/api/time-loans":
                result = svc.apply_time_loan(data)
            else:
                m = re.fullmatch(r"/api/handovers/([^/]+)/confirm", path)
                if m:
                    result = svc.confirm_handover(m.group(1), data)
                else:
                    m = re.fullmatch(r"/api/handovers/([^/]+)/prepare", path)
                    if m:
                        result = svc.prepare_existing_handover(m.group(1), data)
                    else:
                        m = re.fullmatch(r"/api/permits/([^/]+)/consume", path)
                        if m:
                            result = svc.consume_permit(m.group(1), data)
                        else:
                            m = re.fullmatch(r"/api/time-loans/([^/]+)/decision", path)
                            if m:
                                result = svc.decide_time_loan(m.group(1), data)
                            else:
                                m = re.fullmatch(r"/api/time-loans/([^/]+)/fulfill", path)
                                if m:
                                    result = svc.fulfill_time_loan(m.group(1), data)
                                else:
                                    raise ApiError(404, "route_not_found", f"未知 API: {path}")
            self._send_json(200, result)
        except ApiError as exc:
            self._send_json(
                exc.status,
                {"error": exc.code, "message": exc.message, "details": _jsonable(exc.details)},
            )
        except Exception as exc:  # pragma: no cover - HTTP boundary
            self._send_json(500, {"error": "internal_error", "message": str(exc)})


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)


def create_server(
    db_path: str = "data/family-pool.db",
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    seed: bool = False,
) -> ThreadingHTTPServer:
    service = FamilyPoolService(db_path)
    if seed:
        service.seed_demo_if_empty()
    handler = type("ConfiguredAppHandler", (AppHandler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.service = service  # type: ignore[attr-defined]
    return httpd


def run_server(
    db_path: str = "data/family-pool.db",
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    seed: bool = False,
) -> None:
    httpd = create_server(db_path, host, port, seed=seed)
    print(f"Family Pool backend on http://{host}:{port} (db={db_path})", flush=True)

    def _trigger_shutdown(*_args: Any) -> None:
        # shutdown() must run off the serve_forever() thread.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _trigger_shutdown)
    signal.signal(signal.SIGINT, _trigger_shutdown)
    try:
        httpd.serve_forever()
    finally:
        httpd.service.close()  # type: ignore[attr-defined]
        httpd.server_close()
