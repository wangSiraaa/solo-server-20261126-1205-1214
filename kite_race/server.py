"""HTTP wrapper around the in-process race desk.

Only the Python standard library is required.  All mutating endpoints accept
JSON; a supplied ``request_id`` is stored so a retried/redelivered request is
idempotent instead of creating another joint.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
import json
import threading
from urllib.parse import urlparse

from .core import (
    Briefing,
    FieldGraph,
    Mark,
    ProgramCue,
    RaceMode,
    RaceService,
    RouteAssignment,
    RouteOption,
    Arc,
    RaceError,
)


def decode_graph(payload: dict[str, Any]) -> FieldGraph:
    nodes = set(payload["nodes"])
    arcs = [
        Arc(
            arc_id=item["arc_id"],
            rope_id=item["rope_id"],
            src=item["src"],
            dst=item["dst"],
            direction=int(item["direction"]),
        )
        for item in payload["arcs"]
    ]
    flags = dict(payload.get("flags", {}))
    return FieldGraph(nodes=nodes, arcs=arcs, flags=flags)


def decode_mark(payload: dict[str, Any]) -> Mark:
    return Mark(
        flag=payload["flag"],
        face=payload["face"],
        node=payload["node"],
        tick=int(payload["tick"]),
    )


def decode_program(payload: list[dict[str, Any]]) -> list[ProgramCue]:
    return [
        ProgramCue(
            person=item["person"],
            flag=item["flag"],
            face=item["face"],
            handoff_before=bool(item.get("handoff_before", False)),
            handoff_after=bool(item.get("handoff_after", False)),
        )
        for item in payload
    ]


class RequestTracker:
    """Cross-connection idempotency keyed by request_id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._responses: dict[str, tuple[int, dict[str, Any]]] = {}

    def get_or_run(
        self,
        request_id: str | None,
        scope: str,
        func: Callable[[], dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        if request_id is None:
            return 200, func()
        key = f"{scope}# {request_id}"
        with self._lock:
            if key in self._responses:
                return self._responses[key]
            try:
                response = (200, func())
            except RaceError as exc:
                response = (409, {"error": str(exc)})
            self._responses[key] = response
            return response


class RaceHTTPHandler(BaseHTTPRequestHandler):
    server_version = "PaperKiteRace/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RaceError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise RaceError("request body must be a JSON object")
        return payload

    def do_GET(self) -> None:  # noqa: N802 - stdlib dispatch name
        try:
            path = urlparse(self.path).path.rstrip("/")
            service: RaceService = self.server.service  # type: ignore[attr-defined]
            parts = [part for part in path.split("/") if part]
            if len(parts) == 3 and parts[0] == "races" and parts[2] == "roster":
                payload = service.register_roster(parts[1])
            elif len(parts) == 3 and parts[0] == "races" and parts[2] == "me":
                payload = service.query_person(parts[1], self.headers["X-Auth-Token"])
            elif len(parts) == 3 and parts[0] == "races" and parts[2] == "result":
                race = service._race(parts[1])
                race.token_owner(self.headers["X-Auth-Token"])
                if not race.finalized or race.result is None:
                    raise RaceError("race has not been finalized")
                payload = race.result
            else:
                self._send_json(404, {"error": "unknown endpoint"})
                return
            self._send_json(200, payload)
        except RaceError as exc:
            self._send_json(409, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json()
            path = urlparse(self.path).path.rstrip("/")
            service: RaceService = self.server.service  # type: ignore[attr-defined]
            tracker: RequestTracker = self.server.tracker  # type: ignore[attr-defined]
            parts = [part for part in path.split("/") if part]
            request_id = payload.get("request_id")

            def run() -> dict[str, Any]:
                if len(parts) == 2 and parts == ["races", "create"]:
                    return self._create(service, payload)
                if len(parts) == 3 and parts[0] == "races" and parts[2] == "positions":
                    service.report_position(
                        parts[1],
                        payload["person"],
                        int(payload["tick"]),
                        payload["node"],
                    )
                    return {"accepted": True}
                if len(parts) == 3 and parts[0] == "races" and parts[2] == "marks":
                    service.report_mark(parts[1], payload["person"], decode_mark(payload["mark"]))
                    return {"accepted": True}
                if len(parts) == 3 and parts[0] == "races" and parts[2] == "briefing":
                    return {"briefing": self._briefing(service, parts[1], payload).to_dict()}
                if len(parts) == 3 and parts[0] == "races" and parts[2] == "handoff-request":
                    invitation = service.request_live_handoff(
                        parts[1],
                        self.headers["X-Auth-Token"],
                        payload["node"],
                        int(payload["tick"]),
                        payload["option_id"],
                        tuple(payload["route_nodes"]),
                    )
                    return {"invitation": invitation.to_dict()}
                if len(parts) == 3 and parts[0] == "races" and parts[2] == "invitations-accept":
                    invitation = service.accept_invitation(
                        parts[1], self.headers["X-Auth-Token"], payload["invitation_id"]
                    )
                    return {"invitation": invitation.to_dict()}
                if len(parts) == 3 and parts[0] == "races" and parts[2] == "handoff-confirm":
                    record = service.confirm_handoff(
                        parts[1],
                        self.headers["X-Auth-Token"],
                        payload["node"],
                        int(payload["tick"]),
                        payload.get("invitation_id"),
                        payload.get("handoff_id"),
                    )
                    return {"handoff": record.to_dict()}
                if len(parts) == 3 and parts[0] == "races" and parts[2] == "finalize":
                    return service.finalize(parts[1])
                raise RaceError("unknown endpoint")

            status, response = tracker.get_or_run(request_id, path, run)
            self._send_json(status, response)
        except RaceError as exc:
            self._send_json(409, {"error": str(exc)})

    @staticmethod
    def _create(service: RaceService, payload: dict[str, Any]) -> dict[str, Any]:
        race = service.create_race(
            name=payload["name"],
            graph=decode_graph(payload["graph"]),
            roster=list(payload["roster"]),
            program=decode_program(payload["program"]),
            handoff_nodes=set(payload["handoff_nodes"]),
            mode=RaceMode(payload.get("mode", RaceMode.BRIEFING.value)),
        )
        return {"race_id": race.race_id, "tokens": dict(race.tokens)}

    @staticmethod
    def _briefing(service: RaceService, race_id: str, payload: dict[str, Any]) -> Briefing:
        assignments: dict[str, RouteAssignment] = {}
        for item in payload["assignments"]:
            options = [
                RouteOption(
                    option_id=option["option_id"],
                    nodes=tuple(option["nodes"]),
                    condition=None if option.get("condition") is None else decode_mark(option["condition"]),
                )
                for option in item["options"]
            ]
            assignments[item["person"]] = RouteAssignment(
                person=item["person"],
                start=item["start"],
                handoff_node=item.get("handoff_node"),
                before_handoff=bool(item["before_handoff"]),
                options=options,
                required_marks=[decode_mark(mark) for mark in item.get("required_marks", [])],
            )
        return service.issue_briefing(race_id, payload["handoff_node"], assignments)


def build_server(host: str = "127.0.0.1", port: int = 0, verbose: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), RaceHTTPHandler)
    server.service = RaceService()  # type: ignore[attr-defined]
    server.tracker = RequestTracker()  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the paper kite race desk HTTP service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    server = build_server(args.host, args.port, args.verbose)
    print(f"race desk listening on http://{args.host}:{server.server_address[1]}")
    server.serve_forever()


if __name__ == "__main__":
    main()
