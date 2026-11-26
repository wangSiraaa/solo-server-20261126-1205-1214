"""Local-only JSON/HTTP backend.

Run:  python3 -m bingo serve --port 8765
Then open two terminal windows and use ``python3 -m bingo shell ...``.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core import Backend, BingoError


def build_server(host: str, port: int, state_path: str | Path) -> ThreadingHTTPServer:
    backend = Backend(state_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalBingo/1.0"

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send_json(200, {"ok": True, "service": "local-bingo"})
                return
            if parsed.path == "/":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "usage": "POST /api/act with {action, params}; see README.md",
                    },
                )
                return
            self._send_json(404, {"ok": False, "error": {"code": "NOT_FOUND"}})

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/act":
                self._send_json(404, {"ok": False, "error": {"code": "NOT_FOUND"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                envelope = json.loads(raw.decode("utf-8"))
                action = str(envelope["action"])
                params = envelope.get("params") or {}
                if not isinstance(params, dict):
                    raise BingoError("BAD_PARAMS", "params 必须是 JSON 对象")
                data = backend.act(action, params)
                self._send_json(200, {"ok": True, "data": data})
            except BingoError as exc:
                self._send_json(
                    exc.status,
                    {
                        "ok": False,
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            "details": exc.details,
                        },
                    },
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                self._send_json(400, {"ok": False, "error": {"code": "BAD_REQUEST", "message": str(exc)}})
            except Exception as exc:  # pragma: no cover - guard rail for local operator
                self._send_json(500, {"ok": False, "error": {"code": "INTERNAL", "message": repr(exc)}})

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep demo output readable; errors are still returned to terminals.
            if "/health" not in (self.path or ""):
                super().log_message(fmt, *args)

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.backend = backend  # type: ignore[attr-defined]
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local bingo sticker backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state", default="data/state.json")
    args = parser.parse_args(argv)

    Path(args.state).parent.mkdir(parents=True, exist_ok=True)
    httpd = build_server(args.host, args.port, args.state)
    print(f"[server] http://{args.host}:{args.port}  state={args.state}")
    print("[server] only this machine can connect; Ctrl+C stops it.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] stopped; state already saved after every mutation.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
