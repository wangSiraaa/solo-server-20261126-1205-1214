"""Small stdlib HTTP client used by the two teammates' separate processes."""

from __future__ import annotations

from typing import Any
import argparse
import json
import os
import urllib.error
import urllib.request


class RaceClient:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body_payload = dict(payload or {})
        if request_id is not None:
            body_payload["request_id"] = request_id
        data = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=data if method == "POST" else None,
            method=method,
        )
        request.add_header("Content-Type", "application/json; charset=utf-8")
        if self.token:
            request.add_header("X-Auth-Token", self.token)
        try:
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8")
            try:
                message = json.loads(detail).get("error", detail)
            except json.JSONDecodeError:
                message = detail
            raise RuntimeError(f"HTTP {exc.code}: {message}") from exc

    def post(self, path: str, payload: dict[str, Any] | None = None, request_id: str | None = None) -> dict[str, Any]:
        return self._request("POST", path, payload, request_id)

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def create_race(self, config: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
        return self.post("/races/create", config, request_id)

    def roster(self, race_id: str) -> dict[str, Any]:
        return self.get(f"/races/{race_id}/roster")

    def position(self, race_id: str, person: str, tick: int, node: str, request_id: str | None = None) -> dict[str, Any]:
        return self.post(
            f"/races/{race_id}/positions",
            {"person": person, "tick": tick, "node": node},
            request_id,
        )

    def mark(
        self,
        race_id: str,
        person: str,
        flag: str,
        face: str,
        node: str,
        tick: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.post(
            f"/races/{race_id}/marks",
            {"person": person, "mark": {"flag": flag, "face": face, "node": node, "tick": tick}},
            request_id,
        )

    def briefing(self, race_id: str, sheet: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
        return self.post(f"/races/{race_id}/briefing", sheet, request_id)

    def request_handoff(
        self,
        race_id: str,
        node: str,
        tick: int,
        option_id: str,
        route_nodes: list[str],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.post(
            f"/races/{race_id}/handoff-request",
            {"node": node, "tick": tick, "option_id": option_id, "route_nodes": route_nodes},
            request_id,
        )

    def accept(self, race_id: str, invitation_id: str, request_id: str | None = None) -> dict[str, Any]:
        return self.post(
            f"/races/{race_id}/invitations-accept",
            {"invitation_id": invitation_id},
            request_id,
        )

    def confirm(
        self,
        race_id: str,
        node: str,
        tick: int,
        invitation_id: str | None = None,
        handoff_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.post(
            f"/races/{race_id}/handoff-confirm",
            {
                "node": node,
                "tick": tick,
                "invitation_id": invitation_id,
                "handoff_id": handoff_id,
            },
            request_id,
        )

    def finalize(self, race_id: str, request_id: str | None = None) -> dict[str, Any]:
        return self.post(f"/races/{race_id}/finalize", {}, request_id)

    def me(self, race_id: str) -> dict[str, Any]:
        return self.get(f"/races/{race_id}/me")

    def result(self, race_id: str) -> dict[str, Any]:
        return self.get(f"/races/{race_id}/result")


def _load_json(value: str) -> dict[str, Any]:
    with open(value, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> None:
    parser = argparse.ArgumentParser(description="Client for one teammate's race-desk process")
    parser.add_argument("--base", default=os.environ.get("KITE_RACE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--token", default=os.environ.get("KITE_TOKEN"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_request_id(command: argparse.ArgumentParser) -> None:
        command.add_argument("--request-id")

    create = subparsers.add_parser("create")
    create.add_argument("config")
    add_request_id(create)

    roster = subparsers.add_parser("roster")
    roster.add_argument("race_id")

    position = subparsers.add_parser("position")
    position.add_argument("race_id")
    position.add_argument("person")
    position.add_argument("tick", type=int)
    position.add_argument("node")
    add_request_id(position)

    mark = subparsers.add_parser("mark")
    mark.add_argument("race_id")
    mark.add_argument("person")
    mark.add_argument("flag")
    mark.add_argument("face", choices=["front", "back"])
    mark.add_argument("node")
    mark.add_argument("tick", type=int)
    add_request_id(mark)

    briefing = subparsers.add_parser("briefing")
    briefing.add_argument("race_id")
    briefing.add_argument("sheet")
    add_request_id(briefing)

    request = subparsers.add_parser("request-handoff")
    request.add_argument("race_id")
    request.add_argument("node")
    request.add_argument("tick", type=int)
    request.add_argument("option_id")
    request.add_argument("route_nodes", nargs="+")
    add_request_id(request)

    accept = subparsers.add_parser("accept")
    accept.add_argument("race_id")
    accept.add_argument("invitation_id")
    add_request_id(accept)

    confirm = subparsers.add_parser("confirm")
    confirm.add_argument("race_id")
    confirm.add_argument("node")
    confirm.add_argument("tick", type=int)
    confirm.add_argument("--invitation-id")
    confirm.add_argument("--handoff-id")
    add_request_id(confirm)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("race_id")
    add_request_id(finalize)

    me = subparsers.add_parser("me")
    me.add_argument("race_id")

    result = subparsers.add_parser("result")
    result.add_argument("race_id")

    args = parser.parse_args()
    client = RaceClient(args.base, args.token)
    request_id = getattr(args, "request_id", None)

    if args.command == "create":
        response = client.create_race(_load_json(args.config), request_id)
    elif args.command == "roster":
        response = client.roster(args.race_id)
    elif args.command == "position":
        response = client.position(args.race_id, args.person, args.tick, args.node, request_id)
    elif args.command == "mark":
        response = client.mark(
            args.race_id, args.person, args.flag, args.face, args.node, args.tick, request_id
        )
    elif args.command == "briefing":
        response = client.briefing(args.race_id, _load_json(args.sheet), request_id)
    elif args.command == "request-handoff":
        response = client.request_handoff(
            args.race_id, args.node, args.tick, args.option_id, args.route_nodes, request_id
        )
    elif args.command == "accept":
        response = client.accept(args.race_id, args.invitation_id, request_id)
    elif args.command == "confirm":
        response = client.confirm(
            args.race_id,
            args.node,
            args.tick,
            args.invitation_id,
            args.handoff_id,
            request_id,
        )
    elif args.command == "finalize":
        response = client.finalize(args.race_id, request_id)
    elif args.command == "me":
        response = client.me(args.race_id)
    elif args.command == "result":
        response = client.result(args.race_id)
    else:  # pragma: no cover - argparse guards this
        raise AssertionError(args.command)
    print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
