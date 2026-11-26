"""Minimal JSON-over-HTTP front end for the crossword arena (stdlib only).

Endpoints (all POST unless noted, JSON body):

  GET  /health
  POST /scenario                 create/normalise a scenario spec
  POST /sessions                 {"scenario": {...}, "teams": [...]}  -> new board
  GET  /sessions/<sid>           session info (revisions, log length)
  POST /sessions/<sid>/teams     {"team": "..."}   register a late team
  POST /sessions/<sid>/draft     {"team","entry","letters":{"r,c":"A"}}
  POST /sessions/<sid>/hint      {"team","entry","cell":[r,c],"action_id"?}
  POST /sessions/<sid>/handin    {"team","entry","word","action_id"?}
  POST /sessions/<sid>/seal      {"team","entry","word","action_id"?}
  POST /sessions/<sid>/reveal    {"team"?}         sealed-mode round reveal
  POST /sessions/<sid>/judge     {"entry","accepted":[...],"note"?,"revision"?}
  POST /sessions/<sid>/revision  {"label"}
  POST /sessions/<sid>/replay    {"event": {...}}  replay a hand-in verbatim
  POST /sessions/<sid>/restart   reload from disk & re-fold
  GET  /sessions/<sid>/board?revision=&viewer=
  GET  /sessions/<sid>/diff?from=&to=
  GET  /sessions/<sid>/log?revision=

Run:  python -m xword.server --port 8765 --store ./data
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import scenarios
from .arena import Arena
from .model import XwordError

ARENA: Optional[Arena] = None


def _json(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing ----------------------------------------------------------
    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _route(self, method: str) -> None:
        assert ARENA is not None
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        q = parse_qs(parsed.query)
        try:
            if method == "GET" and parts == ["health"]:
                return _json(self, 200, {"ok": True})
            if method == "POST" and parts == ["scenario"]:
                return _json(self, 200, ARENA.create_scenario(self._body()))
            if method == "POST" and parts == ["sessions"]:
                body = self._body()
                spec = body.get("scenario")
                if spec is None:
                    preset = body.get("preset", "shan-shui")
                    mode = body.get("mode", "race")
                    overrides = body.get("overrides", {})
                    spec = (scenarios.shan_shui_board(mode, **overrides)
                            if preset == "shan-shui"
                            else scenarios.four_entry_board(mode, **overrides))
                info = ARENA.create_session(spec, body.get("teams"))
                return _json(self, 201, info)

            if len(parts) >= 2 and parts[0] == "sessions":
                sid = parts[1]
                if method == "GET" and len(parts) == 2:
                    return _json(self, 200, ARENA.session_info(sid))
                if method == "POST":
                    return self._session_action(sid, parts[2:], self._body())
                if method == "GET" and len(parts) == 3:
                    if parts[2] == "board":
                        rev = _first(q, "revision")
                        viewer = _first(q, "viewer")
                        return _json(self, 200,
                                     ARENA.scoreboard(sid, rev, viewer))
                    if parts[2] == "diff":
                        return _json(self, 200, ARENA.diff(
                            sid, _first(q, "from", "r0"),
                            _first(q, "to", "head")))
                    if parts[2] == "log":
                        return _json(self, 200,
                                     {"events": ARENA.log(
                                         sid, _first(q, "revision"))})
            return _json(self, 404, {"error": "not found", "path": self.path})
        except XwordError as exc:
            return _json(self, 400, {"error": str(exc)})
        except KeyError as exc:
            return _json(self, 400, {"error": f"missing field {exc}"})
        except Exception as exc:  # pragma: no cover - defensive
            return _json(self, 500, {"error": repr(exc)})

    def _session_action(self, sid: str, rest, body) -> None:
        assert ARENA is not None
        if not rest:
            return _json(self, 404, {"error": "no action"})
        action = rest[0]
        if action == "teams":
            return _json(self, 200, ARENA.add_team(sid, body["team"]))
        if action == "draft":
            return _json(self, 200, ARENA.draft(
                sid, body["team"], body["entry"], body["letters"]))
        if action == "hint":
            return _json(self, 200, ARENA.request_hint(
                sid, body["team"], body["entry"], body["cell"],
                body.get("action_id")))
        if action == "handin":
            return _json(self, 200, ARENA.handin(
                sid, body["team"], body["entry"], body["word"],
                body.get("action_id")))
        if action == "seal":
            return _json(self, 200, ARENA.seal(
                sid, body["team"], body["entry"], body["word"],
                body.get("action_id")))
        if action == "reveal":
            return _json(self, 200, ARENA.reveal(sid, body.get("team")))
        if action == "judge":
            return _json(self, 200, ARENA.judge_revise(
                sid, body["entry"], body["accepted"], body.get("note", ""),
                body.get("revision")))
        if action == "score_table":
            return _json(self, 200, ARENA.revise_score_table(
                sid, body["cell_points"], body.get("note", ""),
                body.get("revision")))
        if action == "revision":
            return _json(self, 200,
                         {"revision": ARENA.make_revision(sid, body["label"])})
        if action == "replay":
            return _json(self, 200, ARENA.replay_handin(sid, body["event"]))
        if action == "restart":
            return _json(self, 200, ARENA.restart(sid))
        return _json(self, 404, {"error": f"unknown action {action}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def _first(q: Dict[str, list], *names: str) -> Optional[str]:
    for n in names:
        if n in q and q[n]:
            return q[n][0]
    return None


def main(argv=None) -> int:
    global ARENA
    ap = argparse.ArgumentParser(description="crossword arena server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--store", default=os.environ.get("XWORD_STORE", "./data"))
    args = ap.parse_args(argv)
    ARENA = Arena(args.store)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"crossword arena on http://{args.host}:{args.port} "
          f"(store={os.path.abspath(args.store)})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
