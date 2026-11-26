"""Standard-library HTTP JSON service.

Run:
    python -m sofa_service.server --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .planner import plan_delivery
from .render import sheet_ascii_frames
from .service import ServiceError, ShopService

SERVICE = ShopService(persist_path=os.environ.get("SOFA_SERVICE_DATA"))


def _json_default(obj: Any):
    if isinstance(obj, set):
        return sorted(obj)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return str(obj)


class Handler(BaseHTTPRequestHandler):
    server_version = "SofaDelivery/1.0"

    @property
    def service(self) -> ShopService:
        return SERVICE

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep test output clean; errors are still returned to clients.
        if os.environ.get("SOFA_HTTP_DEBUG"):
            super().log_message(fmt, *args)

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Claim-Token, X-Master-Id")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, body: str, content_type: str = "text/markdown; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {"value": data}
        except json.JSONDecodeError as exc:
            raise ServiceError("INVALID_JSON", str(exc), 400)

    def _normalize_path(self, raw_path: str) -> str:
        path = urlparse(raw_path).path.rstrip("/") or "/"
        if path.startswith("/api/"):
            path = path[4:]
        elif path == "/api":
            path = "/"
        return path

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, {})

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = self._normalize_path(self.path)
            self._route_get(path)
        except ServiceError as exc:
            self._send(exc.status, {"error": exc.code, "message": exc.message, "details": exc.details})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "INTERNAL", "message": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = self._normalize_path(self.path)
            body = self._body()
            self._route_post(path, body)
        except ServiceError as exc:
            self._send(exc.status, {"error": exc.code, "message": exc.message, "details": exc.details})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "INTERNAL", "message": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            path = self._normalize_path(self.path)
            if m := re.fullmatch(r"/floorplans/([^/]+)/blocks", path):
                result = self.service.restore_floorplan(m.group(1))
                self._send(200, result)
                return
            self._send(404, {"error": "NOT_FOUND", "path": path})
        except ServiceError as exc:
            self._send(exc.status, {"error": exc.code, "message": exc.message, "details": exc.details})

    def _route_get(self, path: str) -> None:
        if path == "/docs":
            self._send_text(
                200,
                "<!doctype html><meta charset='utf-8'><title>沙发服务</title>"
                "<body style='font-family:sans-serif;max-width:760px;margin:40px auto'>"
                "<h1>沙发送装格图服务</h1>"
                "<p>JSON API. 完整说明见 <a href='/README.md'>/README.md</a>。</p>"
                "<ul><li><a href='/health'>/health</a></li>"
                "<li><a href='/floorplans'>/floorplans</a></li>"
                "<li><a href='/catalog'>/catalog</a></li></ul>"
                "<p>命令行验收：<code>python -m sofa_service.demo</code></p></body>",
                "text/html; charset=utf-8",
            )
            return
        if path == "/README.md":
            readme = Path(__file__).resolve().parent.parent / "README.md"
            self._send_text(200, readme.read_text(encoding="utf-8"))
            return
        if path == "/":
            self._send(
                200,
                {
                    "service": "sofa-delivery-order-service",
                    "docs": "/README.md",
                    "endpoints": [
                        "GET /health",
                        "GET /catalog",
                        "GET /floorplans",
                        "GET /floorplans/{id}",
                        "POST /quotes",
                        "GET /quotes/{id}",
                        "POST /quotes/{id}/confirm",
                        "POST /orders/{id}/claim",
                        "GET /orders/{id}",
                        "GET /orders/{id}/provenance",
                        "POST /orders/{id}/steps/{stepRef}/complete",
                        "POST /planner/plan",
                        "POST /planner/replay",
                    ],
                },
            )
        elif path in ("/health", "/healthz"):
            self._send(200, self.service.health())
        elif path == "/catalog":
            self._send(200, self.service.get_catalog())
        elif path == "/floorplans":
            self._send(200, {"items": self.service.list_floorplans()})
        elif m := re.fullmatch(r"/floorplans/([^/]+)", path):
            self._send(200, self.service.get_floorplan(m.group(1)))
        elif m := re.fullmatch(r"/survey/doors/([^/]+)/measurements", path):
            self._send(200, {"doorId": m.group(1), "items": self.service.list_measurements(m.group(1))})
        elif m := re.fullmatch(r"/measurements/([^/]+)", path):
            self._send(200, self.service.get_measurement(m.group(1)))
        elif m := re.fullmatch(r"/quotes/([^/]+)", path):
            self._send(200, self.service.get_quote(m.group(1)))
        elif m := re.fullmatch(r"/orders/([^/]+)/frames", path):
            order = self.service.get_order(m.group(1))
            fp = self.service.get_floorplan(order["floorPlanId"])
            self._send(200, {"orderId": m.group(1), "frames": sheet_ascii_frames(order["stepSheet"], fp)})
        elif m := re.fullmatch(r"/orders/([^/]+)/provenance", path):
            self._send(200, self.service.get_provenance(m.group(1)))
        elif m := re.fullmatch(r"/orders/([^/]+)", path):
            self._send(200, self.service.get_order(m.group(1)))
        else:
            self._send(404, {"error": "NOT_FOUND", "path": path})

    def _route_post(self, path: str, body: Dict[str, Any]) -> None:
        if path in ("/admin/reset", "/reset"):
            self._send(200, self.service.reset())
        elif path == "/floorplans":
            self._send(201, self.service.upsert_floorplan(body))
        elif m := re.fullmatch(r"/floorplans/([^/]+)/blocks", path):
            result = self.service.set_blocks(
                m.group(1),
                body.get("cells", []),
                block=bool(body.get("block", True)),
                reason=body.get("reason", ""),
            )
            self._send(200, result)
        elif m := re.fullmatch(r"/floorplans/([^/]+)/restore", path):
            self._send(200, self.service.restore_floorplan(m.group(1)))
        elif m := re.fullmatch(r"/survey/doors/([^/]+)/measurements", path):
            self._send(201, self.service.add_measurement(m.group(1), body))
        elif m := re.fullmatch(r"/admin/catalog/models/([^/]+)/clearance", path):
            result = self.service.change_clearance(
                m.group(1), body.get("clearCells", []), body.get("jointId", "back-to-seat")
            )
            self._send(200, result)
        elif path in ("/quotes", "/quote"):
            try:
                quote = self.service.create_quote(body)
                self._send(201, quote)
            except ServiceError as exc:
                if exc.code == "NO_FEASIBLE_PLAN":
                    self._send(exc.status, {"error": exc.code, "message": exc.message, "details": exc.details})
                else:
                    raise
        elif m := re.fullmatch(r"/quotes/([^/]+)/confirm", path):
            self._send(200, self.service.confirm_quote(m.group(1), body))
        elif m := re.fullmatch(r"/orders/([^/]+)/claim", path):
            self._send(200, self.service.claim_order(m.group(1), body))
        elif m := re.fullmatch(r"/orders/([^/]+)/issue-next", path):
            headers = {k.lower(): v for k, v in self.headers.items()}
            self._send(200, self.service.issue_next_step(m.group(1), body, headers))
        elif m := re.fullmatch(r"/orders/([^/]+)/revalidate", path):
            self._send(200, self.service.revalidate_order(m.group(1)))
        elif m := re.fullmatch(r"/orders/([^/]+)/steps/next", path):
            headers = {k.lower(): v for k, v in self.headers.items()}
            self._send(200, self.service.issue_next_step(m.group(1), body, headers))
        elif m := re.fullmatch(r"/orders/([^/]+)/steps/([^/]+)/complete", path):
            headers = {k.lower(): v for k, v in self.headers.items()}
            self._send(200, self.service.complete_step(m.group(1), m.group(2), body, headers))
        elif m := re.fullmatch(r"/orders/([^/]+)/report", path):
            headers = {k.lower(): v for k, v in self.headers.items()}
            step_ref = body.get("stepId") or body.get("stepIndex")
            self._send(200, self.service.complete_step(m.group(1), step_ref, body, headers))
        elif path == "/planner/plan":
            catalog = body.get("catalog") or self.service.catalog
            fp = body.get("floorPlan") or self.service.get_floorplan(body.get("floorPlanId", "narrow-l-turn"))
            req = body.get("order") or body
            self._send(200, plan_delivery(req, catalog, fp))
        elif path == "/planner/render":
            sheet = body.get("stepSheet")
            if not sheet and body.get("quoteId"):
                sheet = self.service.get_quote(body["quoteId"])["stepSheet"]
            if not sheet:
                raise ServiceError("SHEET_REQUIRED", "provide stepSheet or quoteId")
            fp_id = body.get("floorPlanId") or sheet.get("floorPlanId")
            fp = body.get("floorPlan") or self.service.get_floorplan(fp_id)
            self._send(200, {"frames": sheet_ascii_frames(sheet, fp)})
        elif path == "/planner/replay":
            self._send(200, self.service.generic_replay(body))
        else:
            self._send(404, {"error": "NOT_FOUND", "path": path})


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Sofa delivery order service")
    parser.add_argument("--host", default=os.environ.get("SOFA_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", os.environ.get("SOFA_PORT", "8000"))),
    )
    parser.add_argument("--no-extra-ports", action="store_true")
    args = parser.parse_args(argv)

    extra_env = "" if args.no_extra_ports else os.environ.get("EXTRA_PORTS", "3000,5000")
    ports = [args.port]
    for value in extra_env.split(","):
        value = value.strip()
        if value:
            p = int(value)
            if p not in ports:
                ports.append(p)

    servers = []
    threads = []
    for port in ports:
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
        except OSError as exc:
            print(f"warning: could not listen on {args.host}:{port}: {exc}", flush=True)
            continue
        servers.append(httpd)
        print(f"sofa-delivery service on http://{args.host}:{port}", flush=True)
        if port == args.port:
            continue
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        threads.append(t)

    if not servers:
        raise SystemExit("no available HTTP ports")
    primary = servers[0]
    try:
        primary.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for httpd in servers:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    main()
