from __future__ import annotations

"""Dependency-free JSON HTTP interface for Beadworks."""

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .service import Beadworks, BeadworksError


def create_handler(service: Beadworks):
    class BeadworksHandler(BaseHTTPRequestHandler):
        server_version = "Beadworks/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            if os.environ.get("BEADWORKS_HTTP_LOG"):
                super().log_message(fmt, *args)

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise BeadworksError("请求体不是合法 JSON", {"detail": str(exc)}) from exc
            if not isinstance(data, dict):
                raise BeadworksError("请求体必须是 JSON 对象")
            # Accept both the Python-ish snake_case API and common camelCase JSON.
            # Only top-level keyword names are normalized; nested design documents
            # intentionally retain pieceId/claspId/ringId keys for the chain DSL.
            normalized: Dict[str, Any] = {}
            for key, value in data.items():
                snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower().replace("-", "_")
                normalized[snake] = value
            return normalized

        def _call(self, fn, body: Optional[Dict[str, Any]] = None):
            try:
                return 200, fn(**(body or {}))
            except BeadworksError as exc:
                return exc.status, exc.to_dict()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                if path == "/":
                    self._send(200, self._index())
                    return
                if path == "/health":
                    self._send(200, {"ok": True, "service": "beadworks", "version": "1.0.0"})
                    return
                if path == "/api/state":
                    self._send(200, service.state())
                    return

                m = re.fullmatch(r"/api/(people|pieces|clasps|chains|orders|quotes|visits|consents|removals|rentals)", path)
                if m:
                    collection = m.group(1)
                    self._send(200, service._copy(service.db[collection]))
                    return

                m = re.fullmatch(r"/api/people/([^/]+)", path)
                if m:
                    self._send(200, service._copy(service._require("people", m.group(1))))
                    return
                m = re.fullmatch(r"/api/pieces/([^/]+)", path)
                if m:
                    self._send(200, service.get_piece(m.group(1)))
                    return
                m = re.fullmatch(r"/api/pieces/([^/]+)/location", path)
                if m:
                    self._send(200, service.locate_piece(m.group(1)))
                    return
                m = re.fullmatch(r"/api/clasps/([^/]+)", path)
                if m:
                    self._send(200, service._copy(service._require("clasps", m.group(1))))
                    return
                m = re.fullmatch(r"/api/chains/([^/]+)", path)
                if m:
                    self._send(200, service.get_chain_view(m.group(1)))
                    return
                m = re.fullmatch(r"/api/chains/([^/]+)/report", path)
                if m:
                    self._send(200, service.remaining_chain_report(m.group(1)))
                    return
                m = re.fullmatch(r"/api/orders/([^/]+)", path)
                if m:
                    self._send(200, service.get_order(m.group(1)))
                    return
                m = re.fullmatch(r"/api/quotes/([^/]+)", path)
                if m:
                    self._send(200, service._copy(service._require("quotes", m.group(1))))
                    return
                m = re.fullmatch(r"/api/removals/([^/]+)", path)
                if m:
                    self._send(200, service.get_removal(m.group(1)))
                    return
                m = re.fullmatch(r"/api/rentals/([^/]+)", path)
                if m:
                    self._send(200, service._copy(service._require("rentals", m.group(1))))
                    return
                self._send(404, {"error": "route_not_found", "path": path})
            except BeadworksError as exc:
                self._send(exc.status, exc.to_dict())

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            try:
                body = self._body()
                status, payload = self._dispatch_post(path, body)
                self._send(status, payload)
            except BeadworksError as exc:
                self._send(exc.status, exc.to_dict())
            except TypeError as exc:
                self._send(400, {"error": "invalid_arguments", "message": str(exc)})

        def _dispatch_post(self, path: str, body: Dict[str, Any]):
            if path == "/api/people":
                return self._call(service.create_person, body)
            if path == "/api/pieces":
                return self._call(service.receive_piece, body)
            if path == "/api/clasps":
                return self._call(service.add_clasp, body)
            if path == "/api/chains":
                return self._call(service.create_chain, body)
            if path == "/api/try-fit":
                return self._call(service.try_fit_piece if body.get("pieceId") else service.try_fit, body)
            if path == "/api/quotes/removal":
                return self._call(service.quote_removal, body)
            if path == "/api/orders/segmented":
                return self._call(service.create_segmented_order, body)
            if path == "/api/visits":
                return self._call(service.schedule_visit, body)
            if path == "/api/consents":
                return self._call(service.grant_consent, body)
            if path == "/api/removals":
                return self._call(service.remove_and_return_piece, body)
            if path == "/api/chain-returns":
                return self._call(service.return_whole_chain, body)
            if path == "/api/rentals":
                return self._call(service.create_rental, body)

            m = re.fullmatch(r"/api/chains/([^/]+)/install", path)
            if m:
                body = dict(body)
                body["chain_id"] = m.group(1)
                if body.get("left_segment_id") and body.get("right_segment_id"):
                    return self._call(service.install_connecting_clasp, body)
                return self._call(service.install_clasp, body)
            m = re.fullmatch(r"/api/chains/([^/]+)/receive", path)
            if m:
                return self._call(service.receive_chain, {"chain_id": m.group(1), **body})
            m = re.fullmatch(r"/api/clasps/([^/]+)/open", path)
            if m:
                return self._call(service.open_clasp, {**body, "clasp_id": m.group(1)})
            m = re.fullmatch(r"/api/pieces/([^/]+)/handling-permission", path)
            if m:
                return self._call(service.record_handling_permission, {**body, "piece_id": m.group(1)})
            m = re.fullmatch(r"/api/visits/([^/]+)/check-in", path)
            if m:
                return self._call(service.check_in_visit, {"visit_id": m.group(1)})
            m = re.fullmatch(r"/api/visits/([^/]+)/absent", path)
            if m:
                return self._call(service.mark_visit_absent, {"visit_id": m.group(1)})
            m = re.fullmatch(r"/api/rentals/([^/]+)/handover", path)
            if m:
                return self._call(service.hand_over_rental, {"rental_id": m.group(1)})
            m = re.fullmatch(r"/api/rentals/([^/]+)/return", path)
            if m:
                return self._call(service.return_rental, {"rental_id": m.group(1)})

            return 404, {"error": "route_not_found", "path": path}

        @staticmethod
        def _index() -> Dict[str, Any]:
            return {
                "service": "beadworks",
                "endpoints": [
                    "POST /api/people",
                    "POST /api/pieces",
                    "POST /api/clasps",
                    "POST /api/chains",
                    "POST /api/chains/{id}/install",
                    "POST /api/try-fit",
                    "POST /api/quotes/removal",
                    "POST /api/orders/segmented",
                    "POST /api/visits",
                    "POST /api/visits/{id}/check-in",
                    "POST /api/visits/{id}/absent",
                    "POST /api/consents",
                    "POST /api/removals",
                    "POST /api/chain-returns",
                    "POST /api/rentals",
                    "POST /api/rentals/{id}/handover",
                    "POST /api/rentals/{id}/return",
                    "GET  /api/pieces/{id}/location",
                    "GET  /api/chains/{id}/report",
                    "GET  /api/state",
                ],
            }

    return BeadworksHandler


def serve(db_path: str, host: str = "0.0.0.0", port: int = 8000) -> ThreadingHTTPServer:
    service = Beadworks(db_path)
    httpd = ThreadingHTTPServer((host, port), create_handler(service))
    httpd.beadworks = service  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Beadworks counter service")
    parser.add_argument("--db", default=os.environ.get("BEADWORKS_DB", "data/beadworks.json"))
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    httpd = serve(args.db, args.host, args.port)
    print(f"Beadworks listening on http://{args.host}:{args.port} (db={args.db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
