"""零依赖 JSON HTTP 服务。"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .service import Service
from .store import Store

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_server(host: str = "0.0.0.0", port: int = 8000, data_path: Optional[str] = None):
    store = Store(data_path)
    service = Service(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ClassroomWhiteboard/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            # 保持测试输出干净；需要调试时可设置 WHITEBOARD_HTTP_LOG=1。
            if os.environ.get("WHITEBOARD_HTTP_LOG"):
                super().log_message(fmt, *args)

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Group-Token, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, DELETE, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _send_static_file(self, status: int, path: str, content_type: str) -> None:
            body = Path(path).read_bytes()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if not raw.strip():
                return {}
            try:
                value = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
            if value is None:
                return {}
            if not isinstance(value, dict):
                raise ValueError("JSON 请求体必须是对象")
            return value

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, DELETE, OPTIONS")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PATCH(self) -> None:  # noqa: N802
            self._handle("PATCH")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                payload = self._read_json() if method in {"POST", "PATCH", "PUT"} else {}
                result, status = route(service, method, path, payload)
                if isinstance(result, dict) and result.get("__static_file__"):
                    self._send_static_file(
                        status, result["__static_file__"], result.get("content_type", "text/plain")
                    )
                else:
                    self._send_json(status, result)
            except KeyError as exc:
                self._send_json(404, {"error": "not_found", "message": str(exc).strip("'")})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": "bad_request", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": "internal", "message": str(exc)})

        def do_GET_static(self, path: str) -> bool:
            return False

    return ThreadingHTTPServer((host, port), Handler), service


def route(service: Service, method: str, path: str, payload: Dict[str, Any]):
    # Basic / health / docs
    if method == "GET" and path in {"/health", "/healthz", "/api/health"}:
        return {"ok": True, "service": "classroom-whiteboard"}, 200
    if method == "GET" and path == "/api":
        return {
            "endpoints": [
                "POST /api/teacher-sessions",
                "POST /api/sessions",
                "GET /api/sessions",
                "GET /api/sessions/{sid}",
                "PATCH /api/sessions/{sid}/lesson",
                "POST /api/sessions/{sid}/groups",
                "GET /api/groups/{gid}",
                "POST /api/groups/{gid}/preview",
                "POST /api/groups/{gid}/operations",
                "POST /api/groups/{gid}/drafts",
                "POST /api/sessions/{sid}/drafts",
                "GET /api/sessions/{sid}/drafts",
                "GET /api/drafts/{did}",
                "PATCH /api/drafts/{did}",
                "POST /api/sessions/{sid}/merge/plan",
                "POST /api/sessions/{sid}/merge/publish",
                "GET /api/merges/{mid}",
                "GET /api/boards/{bid}",
            ]
        }, 200
    if method == "GET" and path == "/":
        index = STATIC_DIR / "index.html"
        if index.exists():
            # The handler's JSON writer is not used here; route returns special marker.
            return {"__static_file__": str(index), "content_type": "text/html; charset=utf-8"}, 200
        return {"service": "classroom-whiteboard", "docs": "/api", "readme": "README.md"}, 200

    # Teacher sessions
    if method == "POST" and path == "/api/teacher-sessions":
        return service.create_teacher_session(payload), 201
    if method == "GET" and path == "/api/teacher-sessions":
        return list(service.store.state.get("teacher_sessions", {}).values()), 200
    m = re.fullmatch(r"/api/teacher-sessions/([^/]+)", path)
    if method == "GET" and m:
        return service.store.get("teacher_sessions", m.group(1)), 200

    # Class sessions
    if method == "POST" and path == "/api/sessions":
        return service.create_class(payload), 201
    if method == "GET" and path == "/api/sessions":
        return service.list_sessions(), 200
    m = re.fullmatch(r"/api/sessions/([^/]+)", path)
    if method == "GET" and m:
        return service.get_class(m.group(1)), 200

    m = re.fullmatch(r"/api/sessions/([^/]+)/lesson", path)
    if method in {"PATCH", "PUT", "POST"} and m:
        return service.update_lesson(m.group(1), payload), 200

    # Groups
    m = re.fullmatch(r"/api/sessions/([^/]+)/groups", path)
    if method == "POST" and m:
        return service.create_group(m.group(1), payload), 201

    m = re.fullmatch(r"/api/groups/([^/]+)", path)
    if method == "GET" and m:
        return service.get_group(m.group(1)), 200

    m = re.fullmatch(r"/api/groups/([^/]+)/(preview|previews)", path)
    if method == "POST" and m:
        return service.preview_group(m.group(1), payload), 200
    m = re.fullmatch(r"/api/groups/([^/]+)/operations", path)
    if method == "POST" and m:
        return service.apply_group_operations(m.group(1), payload), 200

    m = re.fullmatch(r"/api/groups/([^/]+)/(drafts|submit|submissions)", path)
    if method == "POST" and m:
        group = service.get_group(m.group(1))
        body = dict(payload or {})
        body.setdefault("group_id", m.group(1))
        return service.create_draft(group["session_id"], body), 201

    # Drafts
    m = re.fullmatch(r"/api/sessions/([^/]+)/drafts", path)
    if method == "POST" and m:
        return service.create_draft(m.group(1), payload), 201
    if method == "GET" and m:
        return service.list_drafts(m.group(1)), 200

    m = re.fullmatch(r"/api/drafts/([^/]+)", path)
    if method == "GET" and m:
        return service.get_draft(m.group(1)), 200
    if method in {"PATCH", "PUT"} and m:
        return service.update_draft(m.group(1), payload), 200

    # Merge
    m = re.fullmatch(r"/api/sessions/([^/]+)/merge/plan", path)
    if method == "POST" and m:
        return service.plan_merge(m.group(1), payload), 200
    m = re.fullmatch(r"/api/sessions/([^/]+)/merge/publish", path)
    if method == "POST" and m:
        return service.publish_merge(m.group(1), payload), 201

    m = re.fullmatch(r"/api/sessions/([^/]+)/merges", path)
    if method == "GET" and m:
        sid = m.group(1)
        sess = service.store.get("sessions", sid)
        return [service.store.get("merges", mid) for mid in sess.get("merge_ids", [])], 200

    m = re.fullmatch(r"/api/merges/([^/]+)", path)
    if method == "GET" and m:
        return service.get_merge(m.group(1)), 200

    m = re.fullmatch(r"/api/boards/([^/]+)", path)
    if method == "GET" and m:
        return service.get_board(m.group(1)), 200

    raise KeyError(f"no route for {method} {path}")


def main() -> None:
    host = os.environ.get("WHITEBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("WHITEBOARD_PORT", "8000")))
    data_path = os.environ.get("WHITEBOARD_DATA", "/workspace/data/whiteboard.json")
    httpd, _service = create_server(host, port, data_path)
    print(f"classroom-whiteboard listening on http://{host}:{port} (data={data_path})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
