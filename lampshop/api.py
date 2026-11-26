"""Minimal JSON-over-HTTP API for the workshop.

Routes are ``POST /<action>`` with a JSON body, plus a few ``GET`` read routes.
The surface is intentionally small and maps 1:1 onto ``DomainService`` calls.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, NotFound, ValidationError
from .service import DomainService


class Api:
    def __init__(self, service: DomainService):
        self.svc = service

    # -- routing ----------------------------------------------------------
    def handle(self, method: str, path: str, query: dict, body: dict):
        parts = [p for p in path.strip("/").split("/") if p]
        handler = self._route(method, parts)
        if handler is None:
            raise NotFound("route", f"{method} {path}")
        return handler(query, body, parts)

    def _route(self, method, parts):
        if not parts:
            return None
        key = parts[0]
        table = {
            "health": self._health,
            "reset": self._reset,
            "person": self._person,
            "people": self._people,
            "craft": self._craft,
            "add_step": self._add_step,
            "product_type": self._product_type,
            "station": self._station,
            "post": self._post,
            "shift": self._shift,
            "setting": self._setting,
            "regime": self._regime,
            "seed_root": self._seed_root,
            "evidence": self._evidence,
            "evidence_complete": self._evidence_complete,
            "evidence_verify": self._evidence_verify,
            "evidence_retract": self._evidence_retract,
            "evidence_register": self._evidence_register,
            "disputed": self._disputed,
            "eligibility": self._eligibility,
            "qualification": self._qualification,
            "qualification_revoke": self._qualification_revoke,
            "qualifications": self._qualifications,
            "lineage": self._lineage,
            "product": self._product,
            "product_basis": self._product_basis,
            "sign": self._sign,
            "period": self._period,
            "period_open": self._period_open,
            "period_review": self._period_review,
            "certificates": self._certificates,
            "audit": self._audit,
        }
        fn = table.get(key)
        if fn is None:
            return None
        return fn

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _need(body: dict, *names):
        missing = [n for n in names if n not in body or body[n] in (None, "")]
        if missing:
            raise ValidationError(f"missing required field(s): {', '.join(missing)}",
                                  {"missing": missing})

    # -- handlers ---------------------------------------------------------
    def _health(self, q, b, parts):  # GET/POST
        return self.svc.health()

    def _reset(self, q, b, parts):
        return self.svc.reset()

    def _person(self, q, b, parts):
        if len(parts) >= 2:  # GET /person/<id>
            return self.svc.get_person(parts[1])
        self._need(b, "name")
        return self.svc.create_person(b["name"], b.get("id"), b.get("role", "person"))

    def _people(self, q, b, parts):
        return {"people": self.svc.list_people()}

    def _craft(self, q, b, parts):
        if len(parts) >= 2:
            return self.svc.get_craft(parts[1])
        self._need(b, "id", "name", "min_minutes")
        return self.svc.create_craft(b["id"], b["name"], b["min_minutes"], b.get("required", True))

    def _add_step(self, q, b, parts):
        self._need(b, "craft_id", "id", "name")
        return self.svc.add_craft_step(b["craft_id"], b["id"], b["name"],
                                       b.get("seq", 0), b.get("required", True))

    def _product_type(self, q, b, parts):
        self._need(b, "id", "name", "crafts")
        return self.svc.create_product_type(b["id"], b["name"], b["crafts"])

    def _station(self, q, b, parts):
        self._need(b, "id", "name")
        return self.svc.create_station(b["id"], b["name"])

    def _post(self, q, b, parts):
        self._need(b, "id", "name")
        return self.svc.create_post(b["id"], b["name"], b.get("sign_authority", False))

    def _shift(self, q, b, parts):
        self._need(b, "person_id", "post_id", "start_ts", "end_ts")
        return self.svc.create_shift(b["person_id"], b["post_id"], b["start_ts"], b["end_ts"],
                                     b.get("id"))

    def _setting(self, q, b, parts):
        self._need(b, "key", "value")
        return self.svc.set_setting(b["key"], b["value"])

    def _regime(self, q, b, parts):
        if "regime" in b:
            return self.svc.set_regime(b["regime"])
        return {"regime": self.svc.get_regime()}

    def _seed_root(self, q, b, parts):
        self._need(b, "person_id", "crafts", "at_ts")
        return self.svc.seed_root(b["person_id"], b["crafts"], b["at_ts"])

    def _evidence_register(self, q, b, parts):
        self._need(b, "craft_id", "start_ts", "end_ts", "mentors", "students")
        return self.svc.register_evidence(
            b["craft_id"], b["start_ts"], b["end_ts"], b["mentors"], b["students"],
            b.get("steps", []), b.get("station_id"), b.get("kind", "apprenticeship"),
            b.get("registered_at"), b.get("id"), b.get("note"), b.get("mentor_steps"),
        )

    def _evidence(self, q, b, parts):
        if len(parts) >= 2:
            return self.svc.get_evidence(parts[1], q.get("at_ts", [None])[0])
        return {"evidence": self.svc.list_evidence(
            q.get("person_id", [None])[0], q.get("craft_id", [None])[0],
            q.get("at_ts", [None])[0])}

    def _evidence_complete(self, q, b, parts):
        self._need(b, "evidence_id")
        return self.svc.complete_evidence(b["evidence_id"], b.get("completed_at"))

    def _evidence_verify(self, q, b, parts):
        self._need(b, "evidence_id")
        return self.svc.verify_evidence(b["evidence_id"], b.get("verified_at"))

    def _evidence_retract(self, q, b, parts):
        self._need(b, "evidence_id", "reason")
        return self.svc.retract_evidence(b["evidence_id"], b["reason"], b.get("at_ts"))

    def _disputed(self, q, b, parts):
        pid = b.get("person_id") or (parts[1] if len(parts) >= 2 else None)
        if not pid:
            raise ValidationError("person_id required")
        return self.svc.disputed_minutes(pid, b.get("at_ts"))

    def _eligibility(self, q, b, parts):
        self._need(b, "person_id", "craft_id")
        return self.svc.eligibility(b["person_id"], b["craft_id"], b.get("at_ts"))

    def _qualification(self, q, b, parts):
        self._need(b, "person_id", "craft_id", "capacity", "valid_from")
        return self.svc.grant_qualification(
            b["person_id"], b["craft_id"], b["capacity"], b["valid_from"],
            b.get("valid_until"), b.get("source_kind", "grant"), b.get("source_ref"),
            b.get("regime", "grant"), b.get("enforce_eligibility", True),
        )

    def _qualification_revoke(self, q, b, parts):
        self._need(b, "qualification_id", "reason")
        return self.svc.revoke_qualification(b["qualification_id"], b["reason"], b.get("at_ts"))

    def _qualifications(self, q, b, parts):
        return {"qualifications": self.svc.list_qualifications(
            q.get("person_id", [None])[0], q.get("craft_id", [None])[0],
            q.get("capacity", [None])[0], q.get("at_ts", [None])[0])}

    def _lineage(self, q, b, parts):
        pid = b.get("person_id") or (parts[1] if len(parts) >= 2 else None)
        cid = b.get("craft_id")
        if not pid or not cid:
            raise ValidationError("person_id and craft_id required")
        return self.svc.person_lineage(pid, cid, b.get("at_ts"))

    def _product(self, q, b, parts):
        if len(parts) >= 2:
            return self.svc.get_product(parts[1])
        self._need(b, "product_type_id")
        return self.svc.create_product(b["product_type_id"], b.get("id"),
                                       b.get("customer_session"), b.get("created_at"))

    def _product_basis(self, q, b, parts):
        pid = b.get("product_id") or (parts[1] if len(parts) >= 2 else None)
        if not pid:
            raise ValidationError("product_id required")
        return self.svc.product_basis(pid)

    def _sign(self, q, b, parts):
        self._need(b, "product_id", "signer_id", "request_id")
        return self.svc.sign_product(b["product_id"], b["signer_id"], b["request_id"],
                                     b.get("at_ts"), b.get("customer_session"))

    def _period(self, q, b, parts):
        if len(parts) >= 2:
            return self.svc.get_period(parts[1])
        self._need(b, "start_date", "end_date")
        return self.svc.create_period(b["start_date"], b["end_date"],
                                      b.get("review_policy", "honor_through_period"),
                                      b.get("id"))

    def _period_open(self, q, b, parts):
        self._need(b, "period_id", "roster")
        return self.svc.open_period(b["period_id"], b["roster"], b.get("crafts"),
                                    b.get("issued_at"))

    def _period_review(self, q, b, parts):
        self._need(b, "period_id")
        return self.svc.review_period(b["period_id"], b.get("at_ts"))

    def _certificates(self, q, b, parts):
        return {"certificates": self.svc.list_certificates(
            q.get("person_id", [None])[0], q.get("craft_id", [None])[0],
            q.get("on_date", [None])[0])}

    def _audit(self, q, b, parts):
        eid = b.get("entity_id") or (parts[1] if len(parts) >= 2 else None)
        return {"audit": self.svc.audit_trail(eid, int(b.get("limit", 200)))}


class Handler(BaseHTTPRequestHandler):
    api: Api = None  # set by make_server
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSON body: {exc}")

    def _send(self, status: int, payload: dict):
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            body = self._read_body() if method == "POST" else {}
            result = self.api.handle(method, parsed.path, query, body)
            self._send(200, {"ok": True, **(result if isinstance(result, dict) else {"result": result})})
        except DomainError as exc:
            status = 409 if exc.code in (
                "NOT_ELIGIBLE", "NO_VALID_BASIS", "NO_PRODUCTION_QUALIFICATION",
                "NO_VALID_CERTIFICATE", "POST_NOT_AUTHORIZED", "REQUEST_REUSED", "DUPLICATE",
                "RETRACTED", "NOT_COMPLETED", "NO_REQUIRED_CRAFTS") else (
                404 if exc.code == "NOT_FOUND" else 400)
            self._send(status, exc.to_dict())
        except Exception as exc:  # pragma: no cover - defensive
            self._send(500, {"ok": False, "error": "INTERNAL", "message": str(exc)})


def make_server(service: DomainService, host: str = "127.0.0.1", port: int = 0):
    api = Api(service)

    class BoundHandler(Handler):
        pass

    BoundHandler.api = api
    httpd = ThreadingHTTPServer((host, port), BoundHandler)
    httpd.daemon_threads = True
    return httpd
