"""JSON-over-HTTP entry point, using only the standard library.

    python3 -m journal.http_server --db desk.db --port 8080

    # register / decide
    curl -XPOST localhost:8080/issues   -d '{"issue_id":"island","mode":"rolling"}'
    # reader query (works after a process restart because state is in SQLite)
    curl localhost:8080/issues/island
    # replay-safe opening
    curl -XPOST localhost:8080/issues/island/open-piece \
         -d '{"request_id":"r1","piece":"island"}'

Every route maps one-to-one onto :class:`journal.service.JournalService`, so
the HTTP layer adds no business logic of its own.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .service import JournalError, JournalService, NeedsEditorDecision


class Handler(BaseHTTPRequestHandler):
    service: JournalService = None  # injected

    def _send(self, code: int, body) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self) -> None:
        path = urlparse(self.path).path.strip("/").split("/")
        try:
            if len(path) == 2 and path[0] == "issues":
                return self._send(200, self.service.query_issue(path[1]))
            if len(path) == 3 and path[0] == "issues" and path[2] == "candidates":
                return self._send(200, self.service.list_candidates(path[1]))
            if len(path) == 2 and path[0] == "receipts":
                return self._send(200, self.service.get_receipt(path[1]))
            self._send(404, {"error": "not_found"})
        except JournalError as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.strip("/").split("/")
        body = self._body()
        try:
            if path == ["issues"]:
                mode = body.get("mode", "joint")
                return self._send(201, self.service.create_issue(
                    body["issue_id"], body.get("title", ""), mode))
            if len(path) >= 3 and path[0] == "issues":
                issue, action = path[1], path[2]
                if action == "translator":
                    return self._send(201, self.service.add_translator(
                        body["translator_id"], body.get("name", ""),
                        body.get("slots", [])))
                if action == "piece":
                    return self._send(201, self.service.add_piece(
                        issue, body["piece_id"], body.get("title", ""),
                        body["points"], body.get("duration", 1),
                        body.get("eligible", []), body.get("wish_points", 0)))
                if action == "pledge":
                    return self._send(201, self.service.pledge(
                        issue, body["pledge_id"], body["supporter"], body["points"],
                        body.get("accepts", []), body.get("requires", []),
                        body.get("supporter_cap")))
                if action == "withdraw":
                    return self._send(200, self.service.withdraw_pledge(
                        issue, body["pledge_id"]))
                if action == "open-piece":
                    return self._send(200, self.service.open_piece(
                        issue, body["request_id"], body.get("piece")))
                if action == "open-joint":
                    return self._send(200, self.service.open_joint(
                        issue, body["request_id"], body.get("selection")))
            self._send(404, {"error": "not_found"})
        except NeedsEditorDecision as exc:
            self._send(409, {"error": "needs_editor_decision",
                             "issue_id": exc.issue_id,
                             "candidates": exc.candidates})
        except JournalError as exc:
            self._send(400, {"error": str(exc)})

    def log_message(self, fmt, *args):  # quieter default logging
        pass


def make_server(db: str, port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    service = JournalService(db)

    class Bound(Handler):
        pass

    Bound.service = service
    httpd = ThreadingHTTPServer((host, port), Bound)
    httpd.service = service  # keep a reference alive for tests
    return httpd


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="journal.http_server")
    p.add_argument("--db", default="desk.db")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args(argv)
    httpd = make_server(args.db, args.port, args.host)
    print(f"serving on http://{args.host}:{httpd.server_address[1]} (db={args.db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
