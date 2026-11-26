"""Thin HTTP server around :func:`badges.http_api.handle`, with call recording.

Two ways to use it:

* :class:`LiveServer` -- a real ``ThreadingHTTPServer`` for the local fake live.
* :class:`Recorder` -- runs calls in-process and appends
  ``{method, path, body, status, response}`` to a JSONL file that
  :class:`Replayer` can feed back through a fresh service.  This is the
  "可回放的网络调用" the demo emits.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

from .http_api import handle
from .service import Service


class Recorder:
    """Executes API calls in-process and records them for faithful replay."""

    def __init__(self, service: Optional[Service] = None, path: Optional[str] = None):
        self.service = service or Service()
        self.path = path
        self.calls: List[dict] = []
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            open(path, "w", encoding="utf-8").close()

    def call(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        status, resp = handle(self.service, method, path, body)
        entry = {
            "method": method.upper(),
            "path": path,
            "body": body,
            "status": status,
            "response": resp,
        }
        self.calls.append(entry)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return resp

    # convenience -----------------------------------------------------
    def get(self, path: str) -> Dict[str, Any]:
        return self.call("GET", path)

    def post(self, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        return self.call("POST", path, body or {})


class Replayer:
    """Replays a recorded JSONL call log against a fresh service.

    Replay is *result-checking*: each recorded (status, response) must equal the
    live result for the same request, proving the service is deterministic and
    that the log captures everything needed.
    """

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.entries: List[dict] = []
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.entries.append(json.loads(line))

    def run(self, service: Optional[Service] = None) -> Dict[str, Any]:
        service = service or Service()
        mismatches = []
        for i, e in enumerate(self.entries):
            status, resp = handle(service, e["method"], e["path"], e.get("body"))
            if status != e["status"] or resp != e["response"]:
                mismatches.append(
                    {
                        "index": i,
                        "request": {"method": e["method"], "path": e["path"], "body": e.get("body")},
                        "expected": {"status": e["status"], "response": e["response"]},
                        "actual": {"status": status, "response": resp},
                    }
                )
        return {
            "ok": not mismatches,
            "entries": len(self.entries),
            "mismatches": mismatches,
            "service": service,
        }


class _Handler(BaseHTTPRequestHandler):
    service: Service  # set on the class by make_server

    def log_message(self, fmt, *args):  # quieter
        pass

    def _read_body(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return None
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else None

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        status, payload = handle(self.service, "GET", self.path, None)
        self._send(status, payload)

    def do_POST(self):  # noqa: N802
        try:
            body = self._read_body()
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": f"bad json: {e}"})
            return
        status, payload = handle(self.service, "POST", self.path, body)
        self._send(status, payload)

    def do_DELETE(self):  # noqa: N802
        status, payload = handle(self.service, "DELETE", self.path, None)
        self._send(status, payload)


def make_server(host: str = "127.0.0.1", port: int = 0, service: Optional[Service] = None):
    svc = service or Service()
    handler = type("BoundHandler", (_Handler,), {"service": svc})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd, svc
