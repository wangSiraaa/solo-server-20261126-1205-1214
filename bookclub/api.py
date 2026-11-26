"""HTTP/JSON layer over :mod:`bookclub.store`.

Every operation a bookstore needs has a programmatic entry point:

    authoring   POST /facts, POST /works, POST /works/{id}/turns
    sign-up     POST /sessions, POST /sessions/{id}/register
    staff       POST /staff, POST /sessions/{id}/staff
    confirm     POST /read, POST /revoke, POST /sessions/{id}/assess
    publish     POST /works/{id}/turns (revision_of + unchanged_segments)
    serve/lock  POST /sessions/{id}/serve, POST /sessions/{id}/lock
    open join   POST /sessions/{id}/join
    make-up     POST /practices, POST /practices/{id}/complete
    rebook      POST /sessions/{id}/rebook, GET /sessions/{id}/requests
    inspect     GET /sessions/{id}, GET /sessions/{id}/state, GET /readers/{id},
                GET /plan, GET /snapshot

Run with ``python -m bookclub.api --port 8099 --state /tmp/bookclub.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .store import BookClubError, Store

Handler = Callable[[dict, dict], tuple]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class Router:
    def __init__(self):
        self.routes: List[Tuple[str, re.Pattern, Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self.routes.append((method.upper(), regex, handler))

    def match(self, method: str, path: str):
        allowed = False
        for m, regex, handler in self.routes:
            hit = regex.match(path)
            if not hit:
                continue
            if m != method:
                allowed = True
                continue
            return handler, hit.groupdict()
        if allowed:
            raise BookClubError("method_not_allowed", f"{method} not allowed on {path}", status=405)
        raise BookClubError("not_found", f"no route for {method} {path}", status=404)


def _need(body: dict, *keys: str) -> None:
    missing = [k for k in keys if body.get(k) in (None, "")]
    if missing:
        raise BookClubError("bad_request", f"missing required fields: {missing}")


def build_router(store: Store) -> Router:
    r = Router()

    # -- health / snapshot --------------------------------------------------

    def health(body, params):
        return 200, {"ok": True}

    def snapshot(body, params):
        return 200, store.take_snapshot()

    r.add("GET", "/health", health)
    r.add("GET", "/snapshot", snapshot)

    # -- authoring ----------------------------------------------------------

    def define_fact(body, params):
        _need(body, "id", "description")
        fact = store.define_fact(body["id"], body["description"])
        return 201, {"fact": fact.__dict__}

    def list_facts(body, params):
        return 200, {"facts": [f.__dict__ for f in store.facts.values()]}

    def define_work(body, params):
        _need(body, "id", "title")
        return 201, {"work": store.define_work(body["id"], body["title"])}

    def get_work(body, params):
        return 200, {"work": store.work_view(params["wid"])}

    def publish_turn(body, params):
        _need(body, "label", "segments")
        turn = store.publish_turn(
            params["wid"], body["label"], body["segments"],
            revision_of=body.get("revision_of"),
            unchanged_segments=body.get("unchanged_segments"),
            carry_facts=body.get("carry_facts"),
            note=body.get("note", ""),
        )
        return 201, {"turn": store.turn_view(turn.id)}

    def get_turn(body, params):
        return 200, {"turn": store.turn_view(params["tid"])}

    r.add("POST", "/facts", define_fact)
    r.add("GET", "/facts", list_facts)
    r.add("POST", "/works", define_work)
    r.add("GET", "/works/{wid}", get_work)
    r.add("POST", "/works/{wid}/turns", publish_turn)
    r.add("GET", "/turns/{tid}", get_turn)

    # -- staff --------------------------------------------------------------

    def add_staff(body, params):
        _need(body, "name")
        return 201, {"staff": store.add_staff(body["name"])}

    def list_staff(body, params):
        return 200, {"staff": store.staff_view()}

    def assign_staff(body, params):
        _need(body, "staff", "start", "end")
        return 200, store.schedule_staff(
            params["sid"], body["staff"], float(body["start"]), float(body["end"]),
            slot=int(body.get("slot", 0)),
        )

    r.add("POST", "/staff", add_staff)
    r.add("GET", "/staff", list_staff)
    r.add("POST", "/sessions/{sid}/staff", assign_staff)

    # -- sessions / sign-up -------------------------------------------------

    def register_session(body, params):
        _need(body, "title", "work")
        sess = store.register_session(
            body["title"], body["work"],
            kind=body.get("kind", "served"),
            turn=body.get("turn"),
            goals=body.get("goals"),
            capacity=body.get("capacity"),
        )
        return 201, {"session": store.session_view(sess.id)}

    def get_session(body, params):
        return 200, {"session": store.session_view(params["sid"])}

    def register_reader(body, params):
        _need(body, "reader")
        return 200, store.register_reader(params["sid"], body["reader"])

    def serve(body, params):
        start = body.get("start")
        end = body.get("end")
        result = store.serve(
            params["sid"], body.get("staff"),
            start=float(start) if start is not None else None,
            end=float(end) if end is not None else None,
        )
        return 200, result

    def lock(body, params):
        return 200, store.lock(params["sid"])

    def join(body, params):
        _need(body, "reader")
        return 200, store.join_open(params["sid"], body["reader"],
                                    hold_facts=body.get("hold_facts"))

    def session_state(body, params):
        return 200, {
            "session": store.session_view(params["sid"]),
            "requests": store.requests_for(params["sid"]),
        }

    r.add("POST", "/sessions", register_session)
    r.add("GET", "/sessions/{sid}", get_session)
    r.add("POST", "/sessions/{sid}/register", register_reader)
    r.add("POST", "/sessions/{sid}/serve", serve)
    r.add("POST", "/sessions/{sid}/lock", lock)
    r.add("POST", "/sessions/{sid}/join", join)
    r.add("GET", "/sessions/{sid}/state", session_state)

    # -- reading -----------------------------------------------------------

    def read_segment(body, params):
        _need(body, "reader", "work", "turn", "segment")
        return 200, store.read_segment(body["reader"], body["work"],
                                       body["turn"], body["segment"])

    def revoke(body, params):
        _need(body, "reader", "fact", "turn")
        return 200, store.revoke_read(body["reader"], body["fact"], body["turn"],
                                      segment=body.get("segment"))

    def reader_view(body, params):
        return 200, store.reader_view(params["rid"])

    def can_enter(body, params):
        reader = params.get("reader") or (body or {}).get("reader")
        segment = params.get("segment") or (body or {}).get("segment")
        if not reader or not segment:
            raise BookClubError("bad_request", "reader and segment are required")
        return 200, store.can_enter(reader, params["tid"], segment)

    def simulate_read(body, params):
        _need(body, "reader", "work", "turn", "segment")
        return 200, store.simulate_read(body["reader"], body["work"],
                                        body["turn"], body["segment"])

    def compare_intake(body, params):
        _need(body, "reader", "fact", "turn_a", "turn_b")
        return 200, {"comparison": store.compare_intake(
            body["reader"], body["fact"], turn_a=body["turn_a"],
            turn_b=body["turn_b"], segment=body.get("segment"))}

    def revert_turn(body, params):
        return 200, {"revert": store.revert_turn(params["tid"])}

    r.add("POST", "/read", read_segment)
    r.add("POST", "/revoke", revoke)
    r.add("GET", "/readers/{rid}", reader_view)
    r.add("GET", "/turns/{tid}/can-enter", can_enter)
    r.add("POST", "/turns/{tid}/simulate-read", simulate_read)
    r.add("POST", "/turns/{tid}/revert", revert_turn)
    r.add("POST", "/compare-intake", compare_intake)

    # -- assessment / remediation ------------------------------------------

    def assess(body, params):
        _need(body, "reader", "fact")
        return 200, {"assessment": store.assess(params["sid"], body["reader"],
                                                body["fact"], body.get("turn"))}

    def open_practice(body, params):
        _need(body, "reader", "session", "fact")
        p = store.open_practice(body["reader"], body["session"], body["fact"],
                                turn=body.get("turn"),
                                context_session=body.get("context_session"))
        return 201, {"practice": store.get_practice(p.id)}

    def get_practice(body, params):
        return 200, {"practice": store.get_practice(params["pid"])}

    def complete_practice(body, params):
        return 200, {"practice": store.complete_practice(params["pid"])}

    def practice_facts(body, params):
        p = store.get_practice(params["pid"])
        return 200, {"reader": p["reader"],
                     "facts_by_turn": store.reader_facts_from_practices(p["reader"])}

    def rebook(body, params):
        _need(body, "reader")
        return 200, {"request": store.rebook(params["sid"], body["reader"],
                                             reason=body.get("reason", ""))}

    def requests(body, params):
        return 200, {"requests": store.requests_for(params["sid"])}

    def front_desk(body, params):
        _need(body, "reader", "fact")
        start = body.get("start")
        end = body.get("end")
        return 200, {"decision": store.front_desk(
            params["sid"], body["reader"], body["fact"],
            staff_id=body.get("staff"),
            start=float(start) if start is not None else None,
            end=float(end) if end is not None else None,
            turn=body.get("turn"),
        )}

    def plan(body, params):
        reader = params.get("reader")
        work = params.get("work")
        if not reader or not work:
            raise BookClubError("bad_request", "reader and work are required")
        return 200, {"plan": store.plan_route(reader, work, params.get("turn"))}

    r.add("POST", "/sessions/{sid}/assess", assess)
    r.add("POST", "/sessions/{sid}/front-desk", front_desk)
    r.add("POST", "/practices", open_practice)
    r.add("GET", "/practices/{pid}", get_practice)
    r.add("POST", "/practices/{pid}/complete", complete_practice)
    r.add("GET", "/practices/{pid}/facts", practice_facts)
    r.add("POST", "/sessions/{sid}/rebook", rebook)
    r.add("GET", "/sessions/{sid}/requests", requests)
    r.add("GET", "/plan", plan)

    return r


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class App:
    def __init__(self, store: Store):
        self.store = store
        self.router = build_router(store)
        self.lock = threading.Lock()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter default logging
            pass

        def _dispatch(self, method: str):
            parsed = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise BookClubError("bad_request", "request body must be a JSON object")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send(400, {"error": {"code": "bad_json", "message": str(exc)}})
                return
            params = dict(query)
            try:
                with app.lock:
                    handler, path_params = app.router.match(method, parsed.path)
                    params.update({k: unquote(v) for k, v in path_params.items()})
                    status, payload = handler(body, params)
                    app.store.save()
            except BookClubError as exc:
                self._send(exc.status, exc.as_dict())
                return
            except Exception as exc:  # pragma: no cover - defensive
                if os.environ.get("BOOKCLUB_DEBUG"):
                    import traceback
                    traceback.print_exc()
                self._send(500, {"error": {"code": "internal", "message": str(exc)}})
                return
            self._send(status, payload)

        def _send(self, status: int, payload: dict):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve_http(store: Store, host: str = "127.0.0.1", port: int = 8099) -> ThreadingHTTPServer:
    app = App(store)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    return httpd


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Community bookstore serialised reading API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--state", default=None, help="path to a JSON state snapshot")
    args = parser.parse_args(argv)
    store = Store(args.state)
    httpd = serve_http(store, args.host, args.port)
    print(f"listening on http://{args.host}:{args.port}  state={args.state or ':memory:'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
