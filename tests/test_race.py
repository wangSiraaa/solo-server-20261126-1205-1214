from __future__ import annotations

import json
import threading
import unittest
import urllib.request

from kite_race.client import RaceClient
from kite_race.core import (
    Arc,
    FieldGraph,
    Mark,
    RaceError,
    RaceMode,
    RaceService,
    RouteAssignment,
    RouteOption,
    reduce_segment,
    _ArcEvent,
)
from kite_race.scenarios import (
    adjacent_receiver_track,
    branching_graph,
    briefing_sheet,
    cancellation_track,
    closed_briefing_sheet,
    program,
    race_config,
    replay_track,
    standard_graph,
    valid_track,
)
from kite_race.server import build_server


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = RaceService()
        self.race = self.service.create_race(
            "unit",
            standard_graph(),
            ["甲", "乙"],
            program(),
            {"P"},
            RaceMode.BRIEFING,
        )
        self.race_id = self.race.race_id

    def test_credit_goes_to_confirmed_pair_not_first_flag_rounder(self) -> None:
        replay_track(self.service, self.race_id, valid_track())
        token_a = self.service.token_for(self.race_id, "甲")
        token_b = self.service.token_for(self.race_id, "乙")
        first = self.service.confirm_handoff(self.race_id, token_a, "P", 3)
        second = self.service.confirm_handoff(self.race_id, token_b, "P", 3)
        self.assertIs(first, second)
        result = self.service.finalize(self.race_id)
        handoff = result["handoff"]
        self.assertTrue(handoff["confirmed"])
        self.assertTrue(handoff["scored"])
        self.assertEqual(handoff["score_pair"], ["甲", "乙"])
        self.assertEqual(result["formation"][0]["score_pair"], ["甲", "乙"])
        self.assertNotIn("person", result["formation"][0])

    def test_immediate_original_edge_round_trip_cancels_red_front(self) -> None:
        replay_track(self.service, self.race_id, cancellation_track())
        token_a = self.service.token_for(self.race_id, "甲")
        token_b = self.service.token_for(self.race_id, "乙")
        self.service.confirm_handoff(self.race_id, token_a, "P", 4)
        self.service.confirm_handoff(self.race_id, token_b, "P", 4)
        result = self.service.finalize(self.race_id)
        self.assertFalse(result["handoff"]["scored"])
        self.assertIn("red", result["handoff"]["refusal_reason"])

    def test_one_way_rope_cannot_be_reversed(self) -> None:
        replay_track(self.service, self.race_id, valid_track())
        with self.assertRaisesRegex(RaceError, "no directed arc"):
            self.service.report_position(self.race_id, "乙", 6, "Q")

    def test_adjacent_receiver_lone_arrival_creates_no_scored_joint(self) -> None:
        replay_track(self.service, self.race_id, adjacent_receiver_track())
        token_a = self.service.token_for(self.race_id, "甲")
        token_b = self.service.token_for(self.race_id, "乙")
        self.service.confirm_handoff(self.race_id, token_a, "P", 3)
        with self.assertRaisesRegex(RaceError, "confirmation requires a referee report"):
            self.service.confirm_handoff(self.race_id, token_b, "P", 3)
        with self.assertRaisesRegex(RaceError, "no separately confirmed joint"):
            self.service.finalize(self.race_id)
        self.assertEqual(len(self.service._race(self.race_id).handoffs), 1)

    def test_late_single_arrival_and_duplicate_confirmation_do_not_add_joint(self) -> None:
        replay_track(self.service, self.race_id, valid_track())
        token_a = self.service.token_for(self.race_id, "甲")
        token_b = self.service.token_for(self.race_id, "乙")
        self.service.confirm_handoff(self.race_id, token_a, "P", 3)
        self.service.confirm_handoff(self.race_id, token_b, "P", 3)
        # A redelivered confirmation is the same joint, not another joint.
        duplicate = self.service.confirm_handoff(self.race_id, token_b, "P", 3)
        self.assertEqual(len(self.service._race(self.race_id).handoffs), 1)
        self.assertEqual(duplicate.confirmations["乙"], 3)
        # A late unilateral "I have arrived" at another tick is rejected; it
        # must not manufacture a second joint.
        late_race = self.service.create_race(
            "late", standard_graph(), ["甲", "乙"], program(), {"P"}, RaceMode.BRIEFING
        )
        late_tracks = {
            "甲": [(0, "S"), (1, "R"), (2, "X"), (3, "P"), (4, "P")],
            "乙": [(0, "P"), (1, "P"), (2, "P"), (3, "P"), (4, "P")],
        }
        replay_track(self.service, late_race.race_id, late_tracks)
        late_a = self.service.token_for(late_race.race_id, "甲")
        late_b = self.service.token_for(late_race.race_id, "乙")
        self.service.confirm_handoff(late_race.race_id, late_a, "P", 3)
        self.service.confirm_handoff(late_race.race_id, late_b, "P", 3)
        with self.assertRaisesRegex(RaceError, "only one handoff"):
            self.service.confirm_handoff(late_race.race_id, late_b, "P", 4)

    def test_cancellation_does_not_cross_confirmed_handoff(self) -> None:
        # Same rope on each side of a joint: the two opposite traversals must
        # not be paired because the handoff separates the two people's tracks.
        graph = standard_graph()
        race = self.service.create_race(
            "barrier", graph, ["甲", "乙"], program(), {"P"}, RaceMode.BRIEFING
        )
        tracks = {
            "甲": [(0, "S"), (1, "R"), (2, "X"), (3, "P")],
            "乙": [(0, "P"), (1, "P"), (2, "P"), (3, "P"), (4, "X"), (5, "R"), (6, "S")],
        }
        replay_track(self.service, race.race_id, tracks)
        token_a = self.service.token_for(race.race_id, "甲")
        token_b = self.service.token_for(race.race_id, "乙")
        self.service.confirm_handoff(race.race_id, token_a, "P", 3)
        self.service.confirm_handoff(race.race_id, token_b, "P", 3)
        result = self.service.finalize(race.race_id)
        jia_segment = next(
            item for item in result["segments"] if item["person"] == "甲" and item["handoff_after"]
        )
        self.assertTrue(any(mark["flag"] == "red" for mark in jia_segment["marks"]))

    def test_reverse_then_forward_same_rope_is_not_original_edge_turnback(self) -> None:
        graph = standard_graph()
        reverse = graph.arc_between("X", "R")
        forward = graph.arc_between("R", "X")
        segment = reduce_segment(
            "甲",
            0,
            None,
            None,
            [_ArcEvent(reverse, 1, []), _ArcEvent(forward, 2, [])],
        )
        self.assertEqual([arc.arc_id for arc in segment.arcs], ["r2_b", "r2_f"])

    def test_reverse_arc_without_opposite_paper_arc_is_closed(self) -> None:
        graph = FieldGraph({"A", "B"}, [Arc("only_f", "only", "A", "B", +1)])
        self.assertIsNone(graph.arc_between("B", "A"))
        with self.assertRaisesRegex(RaceError, "no directed paper rope arc"):
            graph.validate_path(("A", "B", "A"))

    def test_reduce_only_immediate_same_rope_reverse(self) -> None:
        graph = standard_graph()
        out = graph.arc_between("S", "R")
        back = graph.arc_between("R", "S")
        other = graph.arc_between("R", "X")
        segment = reduce_segment(
            "甲",
            0,
            None,
            None,
            [
                _ArcEvent(out, 1, [Mark("red", "front", "R", 1)]),
                _ArcEvent(back, 2, [Mark("red", "back", "S", 2)]),
                _ArcEvent(other, 3, []),
            ],
        )
        self.assertEqual([arc.arc_id for arc in segment.arcs], ["r2_f"])
        self.assertEqual(segment.marks, [])

    def test_briefing_accepts_multiple_private_branches_and_rejects_closed_branch(self) -> None:
        race = self.service.create_race(
            "branches", branching_graph(), ["甲", "乙"], program(), {"P"}, RaceMode.BRIEFING
        )
        briefing = self.service.issue_briefing(race.race_id, "P", _assignments(briefing_sheet()))
        self.assertEqual(len(briefing.assignments["甲"].options), 2)
        with self.assertRaisesRegex(RaceError, "no directed paper rope arc"):
            self.service.issue_briefing(race.race_id, "P", _assignments(closed_briefing_sheet()))

    def test_live_invitation_cannot_backfill_receiver_presence(self) -> None:
        race = self.service.create_race(
            "live", standard_graph(), ["甲", "乙"], program(), {"P"}, RaceMode.LIVE
        )
        replay_track(self.service, race.race_id, valid_track())
        token_a = self.service.token_for(race.race_id, "甲")
        with self.assertRaisesRegex(RaceError, "cannot be backfilled"):
            self.service.request_live_handoff(
                race.race_id,
                token_a,
                "P",
                3,
                "main-red",
                ("P", "P"),
            )

    def test_live_detour_invitation_and_refund(self) -> None:
        graph = branching_graph()
        race = self.service.create_race(
            "live-branch", graph, ["甲", "乙"], program(), {"P"}, RaceMode.LIVE
        )
        # 乙 starts away from P, so the invitation at tick 3 cannot backfill.
        tracks = {
            "甲": [(0, "S"), (1, "R"), (2, "X"), (3, "A1"), (4, "P"), (5, "P")],
            "乙": [
                (0, "X"), (1, "A2"), (2, "A2"), (3, "A2"), (4, "A2"),
                (5, "P"), (6, "B"), (7, "F"),
            ],
        }
        replay_track(self.service, race.race_id, tracks)
        token_a = self.service.token_for(race.race_id, "甲")
        token_b = self.service.token_for(race.race_id, "乙")
        invitation = self.service.request_live_handoff(
            race.race_id, token_a, "P", 4, "detour-a1", ("P", "P")
        )
        self.assertEqual(invitation.status, "offered")
        self.service.accept_invitation(race.race_id, token_b, invitation.invitation_id)
        self.service.confirm_handoff(race.race_id, token_a, "P", 5, invitation.invitation_id)
        self.service.confirm_handoff(race.race_id, token_b, "P", 5, invitation.invitation_id)
        result = self.service.finalize(race.race_id)
        self.assertTrue(result["handoff"]["scored"])

        # A later request for a branch that misses red-front is immediately
        # refunded and cannot become an accepted invitation.
        refund_tracks = {
            "甲": [(0, "S"), (1, "Q"), (2, "P"), (3, "P"), (4, "P")],
            "乙": [(0, "X"), (1, "A1"), (2, "A1"), (3, "A1"), (4, "A1"), (5, "P")],
        }
        race2 = self.service.create_race(
            "refund", graph, ["甲", "乙"], program(), {"P"}, RaceMode.LIVE
        )
        replay_track(self.service, race2.race_id, refund_tracks)
        with self.assertRaisesRegex(RaceError, "misses the leader's flag cue"):
            self.service.request_live_handoff(
                race2.race_id,
                self.service.token_for(race2.race_id, "甲"),
                "P",
                4,
                "missing-cue",
                ("P", "B", "P"),
            )
        refunded = next(item for item in self.service._race(race2.race_id).invitations)
        self.assertEqual(refunded.status, "refunded")


def decode_condition(payload):
    if payload is None:
        return None
    return Mark(payload["flag"], payload["face"], payload["node"], payload["tick"])


def _assignments(payload):
    assignments = {}
    for item in payload["assignments"]:
        assignments[item["person"]] = RouteAssignment(
            person=item["person"],
            start=item["start"],
            handoff_node=item["handoff_node"],
            before_handoff=item["before_handoff"],
            options=[
                RouteOption(
                    option["option_id"],
                    tuple(option["nodes"]),
                    decode_condition(option.get("condition")),
                )
                for option in item["options"]
            ],
        )
    return assignments


class HTTPTest(unittest.TestCase):
    def test_separate_client_processes_and_redelivered_confirmation_are_idempotent(self) -> None:
        server = build_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            admin = RaceClient(base)
            created = admin.create_race(race_config(), request_id="create-once")
            race_id = created["race_id"]
            # Simulate cross-process retry: a new client instance with the same
            # request_id must receive the stored response and not make a race.
            retry_created = RaceClient(base).create_race(race_config(), request_id="create-once")
            self.assertEqual(retry_created["race_id"], race_id)

            client_a = RaceClient(base, created["tokens"]["甲"])
            client_b = RaceClient(base, created["tokens"]["乙"])
            for person, client in (("甲", client_a), ("乙", client_b)):
                for tick, node in valid_track()[person]:
                    client.position(race_id, person, tick, node, request_id=f"pos-{person}-{tick}")
                    if node in {"R", "B"}:
                        flag = "red" if node == "R" else "blue"
                        face = "front" if node == "R" else "back"
                        client.mark(race_id, person, flag, face, node, tick)
            client_a.confirm(race_id, "P", 3, request_id="a-confirm")
            client_b.confirm(race_id, "P", 3, request_id="b-confirm")
            client_b.confirm(race_id, "P", 3, request_id="b-confirm")
            result = client_a.finalize(race_id, request_id="final")
            self.assertEqual(result["formation"][0]["score_pair"], ["甲", "乙"])
            view_a = client_a.me(race_id)
            view_b = client_b.me(race_id)
            self.assertTrue(view_a["result"]["handoff"]["formation_segment_id"].startswith("formation_"))
            self.assertEqual(view_b["handoffs"][0]["score_pair"], ["甲", "乙"])
            # Private briefing absent until issued; both clients still only see
            # their own invitations/results.
            self.assertIsNone(view_a["briefing"])
            # An idempotent key also preserves rule errors; it cannot turn a
            # later retry into an accepted mutation.
            bad_body = json.dumps(
                {"person": "甲", "tick": 1, "node": "UNKNOWN", "request_id": "bad-position"}
            ).encode("utf-8")

            def post_bad_position():
                request = urllib.request.Request(
                    f"{base}/races/{race_id}/positions",
                    data=bad_body,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                try:
                    urllib.request.urlopen(request)
                except Exception as exc:  # HTTPError exposes code and body.
                    return getattr(exc, "code", None), getattr(exc, "read", lambda: b"")()

            self.assertEqual(post_bad_position(), post_bad_position())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
