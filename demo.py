#!/usr/bin/env python3
"""Run the requested narrative scenarios without any hardware.

The script starts the real HTTP service and uses separate RaceClient instances
with different tokens to represent the two teammates' client processes.
"""

from __future__ import annotations

import json
import threading

from kite_race.client import RaceClient
from kite_race.scenarios import (
    adjacent_receiver_track,
    briefing_sheet,
    branching_graph,
    cancellation_track,
    race_config,
    valid_track,
)
from kite_race.server import build_server


def _print(title: str, payload: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _replay_http(client: RaceClient, race_id: str, tracks: dict[str, list[tuple[int, str]]]) -> None:
    for person, events in tracks.items():
        for tick, node in events:
            client.position(race_id, person, tick, node, request_id=f"{person}-{tick}-{node}")
            if node in {"R", "B"}:
                flag = "red" if node == "R" else "blue"
                face = "front" if node == "R" else "back"
                client.mark(race_id, person, flag, face, node, tick, request_id=f"{person}-mark-{tick}")


def main() -> None:
    server = build_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    admin = RaceClient(base)
    try:
        # 1. Valid two-client confirmation: red-front then blue-back.
        created = admin.create_race(race_config(), request_id="valid-create")
        race_id = created["race_id"]
        jia = RaceClient(base, created["tokens"]["甲"])
        yi = RaceClient(base, created["tokens"]["乙"])
        _replay_http(admin, race_id, valid_track())
        jia.confirm(race_id, "P", 3, request_id="valid-jia-confirm")
        yi.confirm(race_id, "P", 3, request_id="valid-yi-confirm")
        # Cross-process redelivery uses the same idempotency key.
        yi.confirm(race_id, "P", 3, request_id="valid-yi-confirm")
        valid_result = admin.finalize(race_id, request_id="valid-final")
        _print("valid joint: score is the actual confirmed pair", valid_result)

        # 2. Immediate same-rope return cancels the only red-front.
        canceled_created = admin.create_race(race_config(), request_id="cancel-create")
        canceled_id = canceled_created["race_id"]
        canceled_jia = RaceClient(base, canceled_created["tokens"]["甲"])
        canceled_yi = RaceClient(base, canceled_created["tokens"]["乙"])
        _replay_http(admin, canceled_id, cancellation_track())
        canceled_jia.confirm(canceled_id, "P", 4, request_id="cancel-jia")
        canceled_yi.confirm(canceled_id, "P", 4, request_id="cancel-yi")
        canceled_result = admin.finalize(canceled_id, request_id="cancel-final")
        _print("original-edge return: red-front is canceled and no pair score", canceled_result)

        # 3. Receiver is one edge away: a lone late arrival cannot make a joint.
        adjacent_created = admin.create_race(race_config(), request_id="adjacent-create")
        adjacent_id = adjacent_created["race_id"]
        adjacent_jia = RaceClient(base, adjacent_created["tokens"]["甲"])
        adjacent_yi = RaceClient(base, adjacent_created["tokens"]["乙"])
        _replay_http(admin, adjacent_id, adjacent_receiver_track())
        adjacent_jia.confirm(adjacent_id, "P", 3, request_id="adjacent-jia")
        try:
            adjacent_yi.confirm(adjacent_id, "P", 3, request_id="adjacent-yi")
        except RuntimeError as exc:
            _print("lone confirmation one edge away is refused", {"error": str(exc)})

        # 4. A preview/briefing race can promise multiple routes, including a
        # branch visible only under the flyer's own flag condition.
        branched_created = admin.create_race(
            race_config(graph=branching_graph()), request_id="briefing-create"
        )
        branched_id = branched_created["race_id"]
        briefing = admin.briefing(branched_id, briefing_sheet(), request_id="briefing-issue")
        _print("briefing commitment with multiple private route options", briefing)

        # Both participants can query only their own post-race view.
        _print("甲's retrieval", RaceClient(base, created["tokens"]["甲"]).me(race_id))
        _print("乙's retrieval", RaceClient(base, created["tokens"]["乙"]).me(race_id))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
