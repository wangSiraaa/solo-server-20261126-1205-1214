"""Minimal JSON HTTP API for the rental/rework service (stdlib only).

Design goals:
* every state-changing request is persisted before the response leaves, so a
  restart (process kill / power loss) does not lose accepted work;
* the tenant-facing endpoints never expose another tenant's boards;
* claim endpoints are idempotent on ``screen_token``.

Run:  python -m app.http_api --db var/service.db --port 8000
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from .engine import Service, ServiceError
from .store import Store


class Router:
    def __init__(self):
        self.routes: Dict[Tuple[str, str], Callable] = {}

    def add(self, method: str, path: str, fn: Callable) -> None:
        self.routes[(method, path)] = fn

    def resolve(self, method: str, path: str):
        if (method, path) in self.routes:
            return self.routes[(method, path)], {}
        # simple single-segment wildcard: /x/{id}
        parts = path.strip("/").split("/")
        for (m, tmpl), fn in self.routes.items():
            if m != method:
                continue
            tparts = tmpl.strip("/").split("/")
            if len(tparts) != len(parts):
                continue
            params = {}
            ok = True
            for tp, pp in zip(tparts, parts):
                if tp.startswith("{") and tp.endswith("}"):
                    params[tp[1:-1]] = pp
                elif tp != pp:
                    ok = False
                    break
            if ok:
                return fn, params
        return None, {}


def make_server(service: Service, *, host: str = "127.0.0.1",
                port: int = 0) -> ThreadingHTTPServer:
    router = build_router(service)
    lock = threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet by default
            pass

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise ServiceError("request body must be valid JSON")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            fn, params = router.resolve(method, parsed.path)
            if fn is None:
                self._send(404, {"error": "not found", "path": parsed.path})
                return
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                body = self._read_body()
                with lock:
                    result = fn(body=body, query=query, **params)
                self._send(200, result)
            except ServiceError as e:
                self._send(e.status, {"error": e.message})
            except Exception as e:  # pragma: no cover - defensive
                self._send(500, {"error": "internal error", "detail": str(e)})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _send(self, status: int, obj) -> None:
            payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer((host, port), Handler)


def build_router(service: Service) -> Router:
    r = Router()
    svc = service

    def _need(body, key, default=None):
        if key not in body and default is None:
            raise ServiceError(f"missing field '{key}'")
        return body.get(key, default)

    # -- setup --------------------------------------------------------------
    def upsert_piece(body, query, **kw):
        return svc.upsert_piece(
            piece_id=_need(body, "piece_id"),
            kind=_need(body, "kind"),
            nominal_bias=body.get("nominal_bias", 0),
            true_bias=body.get("true_bias"),
        )
    r.add("POST", "/pieces", upsert_piece)

    def get_piece(body, query, piece_id=None, **kw):
        return svc.get_piece(piece_id)
    r.add("GET", "/pieces/{piece_id}", get_piece)

    def create_ruler(body, query, **kw):
        return svc.create_ruler(ruler_id=_need(body, "ruler_id"))
    r.add("POST", "/rulers", create_ruler)

    def create_box(body, query, **kw):
        return svc.create_box(
            box_id=_need(body, "box_id"),
            piece_ids=_need(body, "piece_ids"),
            tenant_id=body.get("tenant_id"),
        )
    r.add("POST", "/boxes", create_box)

    def create_board(body, query, **kw):
        return svc.create_board(
            board_id=_need(body, "board_id"),
            tenant_id=_need(body, "tenant_id"),
            min_len=body.get("min_len"),
            max_len=body.get("max_len"),
            ref_point_id=_need(body, "ref_point_id"),
            ref_reachable=body.get("ref_reachable", True),
            box_id=body.get("box_id"),
            lease_id=body.get("lease_id"),
        )
    r.add("POST", "/boards", create_board)

    def get_board(body, query, board_id=None, **kw):
        return svc.get_board(board_id)
    r.add("GET", "/boards/{board_id}", get_board)

    def set_ref(body, query, board_id=None, **kw):
        return svc.set_ref_reachable(
            board_id=board_id, reachable=bool(_need(body, "reachable")))
    r.add("POST", "/boards/{board_id}/ref", set_ref)

    def set_req(body, query, board_id=None, **kw):
        return svc.set_requirement(
            board_id=board_id, min_len=body.get("min_len"),
            max_len=body.get("max_len"))
    r.add("POST", "/boards/{board_id}/requirement", set_req)

    # -- usage receipts -----------------------------------------------------
    def submit_receipt(body, query, **kw):
        return svc.submit_usage_receipt(body)
    r.add("POST", "/usage-receipts", submit_receipt)

    def replace_receipt(body, query, board_id=None, **kw):
        payload = dict(body)
        payload["board_id"] = board_id
        return svc.replace_usage_receipt(
            board_id=board_id, payload=payload,
            swap_reason=body.get("swap_reason", "on-site piece swap"))
    r.add("POST", "/boards/{board_id}/usage-receipt", replace_receipt)

    def piece_event(body, query, **kw):
        return svc.record_piece_event(
            event_type=_need(body, "event_type"),
            piece_id=_need(body, "piece_id"),
            ruler_id=body.get("ruler_id"),
            box_id=body.get("box_id"),
            board_id=body.get("board_id"))
    r.add("POST", "/piece-events", piece_event)

    # -- calibration / evaluation ------------------------------------------
    def add_calib(body, query, **kw):
        return svc.add_calibration(
            piece_biases=_need(body, "piece_biases"),
            note=body.get("note"),
            calibration_id=body.get("calibration_id"))
    r.add("POST", "/calibrations", add_calib)

    def dispatch(body, query, calibration_id=None, **kw):
        return svc.dispatch_calibration(
            calibration_id=calibration_id,
            box_id=body.get("box_id"),
            tenant_id=body.get("tenant_id"),
            board_ids=body.get("board_ids"),
            slot_capacity=body.get("slot_capacity"))
    r.add("POST", "/calibrations/{calibration_id}/dispatch", dispatch)

    def get_plan(body, query, calibration_id=None, **kw):
        return svc.plan(calibration_id=calibration_id)
    r.add("GET", "/calibrations/{calibration_id}/plan", get_plan)

    def evaluate(body, query, board_id=None, **kw):
        return svc.evaluate_board(board_id)
    r.add("GET", "/boards/{board_id}/evaluation", evaluate)

    # -- work orders --------------------------------------------------------
    def list_wos(body, query, **kw):
        return {"work_orders": svc.list_work_orders(
            tenant_id=query.get("tenant_id"),
            calibration_id=query.get("calibration_id"),
            status=query.get("status"))}
    r.add("GET", "/work-orders", list_wos)

    def claim(body, query, work_order_id=None, **kw):
        return svc.claim(
            work_order_id=work_order_id,
            worker_id=_need(body, "worker_id"),
            screen_token=_need(body, "screen_token"))
    r.add("POST", "/work-orders/{work_order_id}/claim", claim)

    def chain(body, query, work_order_id=None, **kw):
        return svc.get_measurement_chain(work_order_id=work_order_id)
    r.add("GET", "/work-orders/{work_order_id}/measurement-chain", chain)

    # -- completions --------------------------------------------------------
    def complete_recheck(body, query, board_id=None, **kw):
        payload = dict(body)
        return svc.complete_recheck(
            board_id=board_id, payload=payload,
            worker_id=body.get("worker_id"))
    r.add("POST", "/boards/{board_id}/recheck", complete_recheck)

    def complete_workshop(body, query, board_id=None, **kw):
        payload = dict(body)
        return svc.complete_workshop_remeasure(
            board_id=board_id, payload=payload,
            worker_id=body.get("worker_id"))
    r.add("POST", "/boards/{board_id}/workshop-remeasure", complete_workshop)

    # -- tenant dispositions ------------------------------------------------
    def list_disp(body, query, **kw):
        vo = query.get("valid_only", "true").lower() != "false"
        return {"dispositions": svc.list_dispositions(
            tenant_id=query.get("tenant_id"),
            work_order_id=query.get("work_order_id"),
            valid_only=vo)}
    r.add("GET", "/dispositions", list_disp)

    def health(body, query, **kw):
        return {"ok": True}
    r.add("GET", "/health", health)

    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="var/service.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    svc = Service(Store(args.db))
    httpd = make_server(svc, host=args.host, port=args.port)
    host, port = httpd.server_address
    print(f"listening on http://{host}:{port}  (db={args.db})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
