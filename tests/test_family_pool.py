from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from family_pool.http_app import create_server
from family_pool.replay import Client, run_all
from family_pool.seed import DEMO_IDS as I
from family_pool.service import FamilyPoolService


class FullReplayHttpTests(unittest.TestCase):
    server = None
    thread = None
    base_url = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        db = str(Path(cls.tmp.name) / "replay.db")
        cls.server = create_server(db, host="127.0.0.1", port=0)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.service.close()
        cls.tmp.cleanup()

    def test_story_and_all_counterexamples(self) -> None:
        summary = run_all(self.base_url)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["failed"] if "failed" in summary else 0, 0)


class PersistenceTests(unittest.TestCase):
    def test_sqlite_file_can_be_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "restart.db")
            svc = FamilyPoolService(db)
            svc.reset_and_seed_demo()
            svc.open_session({"id": "SESS-X", "name": "持久化", "operating_mode": "hybrid"})
            svc.gate_entry({
                "session_id": "SESS-X",
                "wristband_id": I["b"],
                "zone_id": I["shallow"],
                "at": "2026-11-26T09:00:00+00:00",
            })
            svc.close()

            reopened = FamilyPoolService(db)
            try:
                loc = reopened.location_of(I["b"], "SESS-X")
                self.assertTrue(loc["present_in_water"])
                self.assertEqual(loc["zone_id"], I["shallow"])
                answer = reopened.answer("SESS-X")
                self.assertEqual(answer["session"]["id"], "SESS-X")
            finally:
                reopened.close()


class TwoStepOnsiteTests(unittest.TestCase):
    def test_proposal_confirmation_allocation_and_gate(self) -> None:
        svc = FamilyPoolService(":memory:")
        self.addCleanup(svc.close)
        svc.reset_and_seed_demo()
        sid = "SESS-ONSITE"
        svc.open_session({"id": sid, "name": "现场互助", "operating_mode": "onsite"})
        for band in (I["a"], I["b"], I["sister"]):
            svc.gate_entry({"session_id": sid, "wristband_id": band, "zone_id": I["shallow"]})
        svc.assign_care({
            "session_id": sid,
            "child_id": I["sister"],
            "adult_id": I["a"],
        })
        ho = svc.create_handover({
            "id": "HO-2STEP",
            "session_id": sid,
            "departing_adult_id": I["a"],
            "receiving_adult_id": I["b"],
            "child_ids": [I["sister"]],
            "source": "onsite",
        })
        self.assertEqual(ho["status"], "proposed")
        confirmed = svc.confirm_handover("HO-2STEP", {"confirmed_by": I["b"]})
        self.assertEqual(confirmed["status"], "confirmed")
        prepared = svc.prepare_existing_handover("HO-2STEP", {})
        self.assertEqual(prepared["status"], "prepared")
        permit = svc.issue_permit({
            "id": "PM-2STEP",
            "session_id": sid,
            "wristband_id": I["a"],
            "handover_id": "HO-2STEP",
        })
        result = svc.consume_permit(permit["id"], {"staff_id": "GATE"})
        self.assertEqual(result["permit"]["status"], "consumed")
        answer = svc.answer(sid)
        sister = next(c for c in answer["children"] if c["child_id"] == I["sister"])
        self.assertEqual(sister["actual_receiver_ids"], [I["b"]])


class VenueRulesTests(unittest.TestCase):
    def test_visibility_is_separate_from_marked_port(self) -> None:
        svc = FamilyPoolService(":memory:")
        self.addCleanup(svc.close)
        svc.reset_and_seed_demo()
        self.assertFalse(svc.can_see(I["shallow"], I["invisible"]))
        self.assertTrue(svc.can_see(I["shallow"], I["warm"]))

        sid = "SESS-PORT"
        svc.open_session({"id": sid, "name": "口岸", "operating_mode": "hybrid"})
        svc.gate_entry({"session_id": sid, "wristband_id": I["a"], "zone_id": I["shallow"]})
        crossed = svc.cross_port({
            "session_id": sid,
            "wristband_id": I["a"],
            "port_id": I["port_invisible"],
        })
        self.assertEqual(crossed["location"]["zone_id"], I["invisible"])
        with self.assertRaises(Exception) as ctx:
            svc.cross_port({
                "session_id": sid,
                "wristband_id": I["a"],
                "port_id": I["port_warm"],
            })
        self.assertEqual(ctx.exception.code, "no_marked_port")

    def test_child_can_have_two_joint_caregivers(self) -> None:
        svc = FamilyPoolService(":memory:")
        self.addCleanup(svc.close)
        svc.reset_and_seed_demo()
        sid = "SESS-JOINT"
        svc.open_session({"id": sid, "name": "共同陪同", "operating_mode": "hybrid"})
        for band in (I["a"], I["b"], I["sister"]):
            svc.gate_entry({"session_id": sid, "wristband_id": band, "zone_id": I["shallow"]})
        svc.assign_care({"session_id": sid, "child_id": I["sister"], "adult_id": I["a"], "kind": "primary"})
        svc.assign_care({"session_id": sid, "child_id": I["sister"], "adult_id": I["b"], "kind": "joint"})
        answer = svc.answer(sid)
        sister = next(c for c in answer["children"] if c["child_id"] == I["sister"])
        self.assertIn(I["a"], sister["active_caregiver_ids"])
        self.assertIn(I["b"], sister["active_caregiver_ids"])

    def test_child_can_request_exit_with_authorized_pickup(self) -> None:
        svc = FamilyPoolService(":memory:")
        self.addCleanup(svc.close)
        svc.reset_and_seed_demo()
        sid = "SESS-CHILD"
        svc.open_session({"id": sid, "name": "儿童离区", "operating_mode": "hybrid"})
        svc.gate_entry({"session_id": sid, "wristband_id": I["sister"], "zone_id": I["shallow"]})
        permit = svc.issue_permit({
            "session_id": sid,
            "wristband_id": I["sister"],
            "permit_type": "child_pickup",
            "pickup_name": "授权接领人 丙",
        })
        self.assertEqual(permit["status"], "issued")


if __name__ == "__main__":
    unittest.main()
