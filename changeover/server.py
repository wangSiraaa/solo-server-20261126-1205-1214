"""HTTP surface.  Thin JSON layer over ``Engine``.

Visitor-facing devices only hit the ``/screens/{id}/...`` receive/buffer/display/
mask endpoints; everything else is the back-office.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ApiError


def _read_json(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError("invalid JSON body", code="bad_json")


class Router:
    def __init__(self, engine):
        self.engine = engine
        self.routes = []
        self._build()

    def add(self, method, pattern, fn):
        parts = [p for p in pattern.strip("/").split("/") if p]
        self.routes.append((method, parts, fn))

    def _build(self):
        e = self.engine
        self.add("GET", "/health", lambda req: {"ok": True})
        self.add("GET", "/state/topology", lambda req: e.topology())
        self.add("GET", "/state/screens", lambda req: {"screens": e.screens_view()})

        # topology editing (back-office setup)
        self.add("POST", "/zones", lambda req: e.add_zone(req["id"], req.get("name", req["id"]), req["kind"]))
        self.add(
            "POST",
            "/doors",
            lambda req: e.add_door(
                req["id"], req["a"], req["b"], req.get("direction", "both"), req.get("open", False)
            ),
        )
        self.add("POST", "/screens", lambda req: e.add_screen(req["id"], req["zone"]))
        self.add(
            "POST",
            "/activities",
            lambda req: e.register_activity(
                req["id"], req.get("name", req["id"]), req["zones"], req.get("allow_seen", True), req.get("host", "")
            ),
        )
        self.add("POST", "/activities/{aid}/end", lambda req: e.end_activity(req["aid"]))

        # field / devices
        self.add(
            "POST",
            "/doors/{did}/field",
            lambda req: e.door_field(req["did"], req["open"], req.get("changeover")),
        )
        self.add(
            "POST",
            "/screens/{sid}/copy",
            lambda req: e.send_copy(req["sid"], req["target"], req.get("door"), req.get("version")),
        )
        self.add(
            "POST",
            "/screens/{sid}/received",
            lambda req: e.device_state(req["sid"], "received", req.get("version")),
        )
        self.add(
            "POST",
            "/screens/{sid}/buffered",
            lambda req: e.device_state(req["sid"], "buffered", req.get("version")),
        )
        self.add(
            "POST",
            "/screens/{sid}/display",
            lambda req: e.device_state(req["sid"], "displayed", req.get("version")),
        )
        self.add("POST", "/screens/{sid}/mask", lambda req: e.set_mask(req["sid"], req["masked"]))
        self.add("POST", "/screens/{sid}/online", lambda req: e.set_online(req["sid"], req["online"]))
        self.add("GET", "/screens/{sid}", lambda req: e.screen_view(req["sid"]))

        # changeovers
        self.add("POST", "/changeovers", self._start_changeover)
        self.add("GET", "/changeovers/{cid}", lambda req: e.status(req["cid"]))
        self.add("GET", "/changeovers/{cid}/plan", lambda req: e.plan(req["cid"]))
        self.add(
            "POST", "/changeovers/{cid}/switch", lambda req: e.switch_entrance(req["cid"], req["entrance"], req.get("door"))
        )
        self.add(
            "POST",
            "/changeovers/{cid}/open",
            lambda req: e.attempt_open(req["cid"], req.get("entrances")),
        )
        self.add(
            "POST",
            "/changeovers/{cid}/field",
            lambda req: e.changeover_field(req["cid"], req["door"]),
        )

        # openings / receipts
        self.add("POST", "/openings", lambda req: e.open_direct(req["activity"], req["entrance"]))
        self.add("GET", "/openings", lambda req: {"openings": e.openings_view(req["query"].get("status"))})
        self.add("GET", "/openings/{n}", lambda req: e.opening_view(int(req["n"])))
        self.add("GET", "/receipts", lambda req: {"receipts": e.receipts_view()})

        # visitor trial walk + offline inspection
        self.add("POST", "/walk", lambda req: e.walk(req["entrance"], req["target"]))
        self.add("GET", "/guidance", self._guidance)

    def _start_changeover(self, req):
        return self.engine.start_changeover(
            req["mode"],
            req["from_activity"],
            req["to_activity"],
            req["entrance_screens"],
            req.get("signage_screens"),
            req.get("doors_to_open"),
            req.get("doors_to_partition"),
            req.get("id"),
        )

    def _guidance(self, req):
        return self.engine.left_guidance(req["query"].get("entrance"), req["query"].get("target"))

    def dispatch(self, method, path, query, req):
        parts = [p for p in path.strip("/").split("/") if p]
        allowed = False
        for m, pat, fn in self.routes:
            if len(pat) != len(parts):
                continue
            params = {}
            ok = True
            for pseg, aseg in zip(pat, parts):
                if pseg.startswith("{") and pseg.endswith("}"):
                    params[pseg[1:-1]] = aseg
                elif pseg != aseg:
                    ok = False
                    break
            if not ok:
                continue
            if m != method:
                allowed = True
                continue
            req = dict(req)
            req.update(params)
            req["query"] = query
            return fn(req), 200
        if allowed:
            raise ApiError("method not allowed", status=405, code="method_not_allowed")
        raise ApiError("not found", status=404, code="not_found")


class Handler(BaseHTTPRequestHandler):
    router = None

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            body = _read_json(self)
        except ApiError as exc:
            self._send(exc.status, exc.to_dict())
            return
        try:
            result, status = self.router.dispatch(method, parsed.path, query, body)
            self._send(status, result)
        except ApiError as exc:
            self._send(exc.status, exc.to_dict())
        except Exception as exc:  # avoid leaking tracebacks to screens
            self._send(500, {"error": "internal", "message": str(exc)})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def make_server(engine, host="127.0.0.1", port=0):
    router = Router(engine)
    handler = type("BoundHandler", (Handler,), {"router": router})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
