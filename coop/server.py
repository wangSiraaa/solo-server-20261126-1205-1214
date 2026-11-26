"""Standard-library localhost JSON RPC server for the cooperative backend."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .db import connect, init_db
from .service import CoopError, CoopService, _json_dumps


class CoopServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: CoopService):
        super().__init__(address, CoopRequestHandler)
        self.service = service


class CoopRequestHandler(BaseHTTPRequestHandler):
    server: CoopServer

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep demo output clean; server lifecycle logs go to the demo log.
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send_json(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"ok": True, "service": "coop-local"})
            return
        self._send_json(404, {"ok": False, "error": {"message": "use POST /rpc/<method>"}})

    def do_POST(self) -> None:  # noqa: N802
        prefix = "/rpc/"
        if not self.path.startswith(prefix):
            self._send_json(404, {"ok": False, "error": {"message": "unknown route"}})
            return

        method = self.path[len(prefix):].strip("/")
        client_id = self.headers.get("X-Client-Id", "local-anonymous")
        idempotency_key = self.headers.get("Idempotency-Key")

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            params = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(params, dict):
                raise ValueError("JSON params must be an object")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"ok": False, "error": {"code": "bad_json", "message": str(exc)}})
            return

        service = self.server.service
        request_hash = hashlib.sha256(
            _json_dumps({"method": method, "client_id": client_id, "params": params}).encode("utf-8")
        ).hexdigest()

        if idempotency_key:
            with service.lock:
                row = service.conn.execute(
                    "SELECT * FROM idempotent_requests WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    self._send_json(
                        409,
                        {
                            "ok": False,
                            "error": {
                                "code": "idempotency_conflict",
                                "message": "same Idempotency-Key used with different request",
                            },
                        },
                    )
                    return
                payload = json.loads(row["response_json"])
                payload["idempotent_replay"] = True
                self._send_json(
                    row["status_code"],
                    payload,
                    {"Idempotency-Replay": "true"},
                )
                return

        try:
            func = getattr(service, method, None)
            if func is None or method.startswith("_"):
                raise CoopError(f"unknown RPC method: {method}")
            # The authenticated/local terminal identity comes from the header;
            # tolerate demo bodies that repeat it for readable traces.
            params.pop("client_id", None)
            result = func(**params, client_id=client_id)
            payload: dict[str, Any] = {"ok": True, "method": method, "result": result}
            status = 200
        except CoopError as exc:
            payload = {
                "ok": False,
                "method": method,
                "error": {"code": exc.__class__.__name__, "message": exc.message, "details": exc.details},
            }
            status = exc.status_code
        except TypeError as exc:
            payload = {"ok": False, "method": method, "error": {"code": "invalid_arguments", "message": str(exc)}}
            status = 400
        except sqlite3.Error as exc:
            payload = {"ok": False, "method": method, "error": {"code": "database_error", "message": str(exc)}}
            status = 500

        if idempotency_key and status < 500:
            try:
                with service.lock:
                    service.conn.execute(
                        """INSERT INTO idempotent_requests
                           (idempotency_key, client_id, request_hash,
                            response_json, status_code)
                           VALUES (?, ?, ?, ?, ?)""",
                        (idempotency_key, client_id, request_hash, _json_dumps(payload), status),
                    )
                    service.conn.commit()
            except sqlite3.IntegrityError:
                # Concurrent same-key request won; the retry semantics are still
                # backed by renewal_receipts.receipt_id for receipt redelivery.
                pass

        self._send_json(status, payload)


def build_server(db_path: str, host: str, port: int) -> CoopServer:
    conn = connect(db_path)
    init_db(conn)
    service = CoopService(conn)
    return CoopServer((host, port), service)


def main() -> None:
    parser = argparse.ArgumentParser(description="Broadband cooperative local RPC server")
    parser.add_argument("--db", default=None, help="SQLite path; defaults to COOP_DB or demo/coop.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8931)
    args = parser.parse_args()

    from .db import default_db_path

    server = build_server(args.db or default_db_path(), args.host, args.port)
    print(f"coop server listening on http://{args.host}:{args.port} db={args.db or default_db_path()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.service.conn.close()


if __name__ == "__main__":
    main()
