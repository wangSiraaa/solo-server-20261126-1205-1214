#!/usr/bin/env python3
"""Focused invariant tests for the studio core.

The HTTP restart path is covered by demo.py; these tests keep the important
state rules fast and explicit.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from studio_core import MAX_LINES, MAX_TAKES_PER_LINE, ApiError, StudioService


class StudioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.sqlite3")
        self.svc = StudioService(self.db_path)
        self.director = self.svc.create_session("导演", "director")
        self.jia = self.svc.create_session("甲", "jia")
        self.yi = self.svc.create_session("乙", "yi")
        self.bing = self.svc.create_session("丙", "bing")
        self.studio = self.svc.create_studio("测试棚", self.director["token"])

    def tearDown(self) -> None:
        self.svc.close()
        self.tmp.cleanup()

    def script(self, lines=None):
        if lines is None:
            lines = [
                {"role": "jia"},
                {"role": "yi", "hearing": {"startIndex": 2, "endIndex": 4}},
                {"role": "bing", "hearing": {"startIndex": 1, "endIndex": 3}},
            ]
        return self.svc.create_script(self.studio["studioId"], {"lines": lines},
                                      self.director["token"])

    def take(self, session, script_id, line, words):
        started = self.svc.start_recording(
            {"scriptId": script_id, "lineIndex": line}, session["token"])
        return self.svc.complete_recording(
            started["recordingId"], {"words": words}, session["token"])

    def hear(self, director_token, actor, recording, start, end, key):
        delivery = self.svc.send_delivery({
            "recordingId": recording["recordingId"],
            "targetSessionId": actor["sessionId"],
        }, director_token)
        return self.svc.confirm_playback({
            "deliveryId": delivery["deliveryId"],
            "confirmationKey": key,
            "startIndex": start,
            "endIndex": end,
        }, actor["token"])

    def test_script_and_take_limits(self):
        with self.assertRaises(ApiError) as ctx:
            self.script([{"role": "jia"} for _ in range(MAX_LINES + 1)])
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.code, "too_many_lines")

        script = self.script([{"role": "jia"}])
        for i in range(MAX_TAKES_PER_LINE):
            self.take(self.jia, script["scriptId"], 0, [f"词{i}"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.start_recording({"scriptId": script["scriptId"], "lineIndex": 0},
                                     self.jia["token"])
        self.assertEqual(ctx.exception.code, "take_limit_exceeded")

    def test_send_and_self_report_are_not_evidence(self):
        script = self.script()
        a1 = self.take(self.jia, script["scriptId"], 0,
                       ["长亭", "雨歇", "风满袖", "归"])
        a2 = self.take(self.jia, script["scriptId"], 0,
                       ["长亭", "雨歇", "风满袖", "别"])
        self.hear(self.director["token"], self.yi, a1, 2, 4, "hear-a1")
        self.svc.send_delivery({"recordingId": a2["recordingId"],
                                "targetSessionId": self.yi["sessionId"]},
                               self.director["token"])
        self.svc.create_self_report({"heardRecordingId": a2["recordingId"]},
                                    self.yi["token"])
        started = self.svc.start_recording({"scriptId": script["scriptId"], "lineIndex": 1},
                                          self.yi["token"])
        self.assertEqual(len(started["listeningSnapshot"]["confirmations"]), 1)
        b = self.svc.complete_recording(started["recordingId"], {"words": ["乙"]},
                                        self.yi["token"])
        validation = self.svc.validate_recording_ids(
            self.studio["studioId"], script["scriptId"],
            [a2["recordingId"], b["recordingId"], "missing-c"])
        self.assertFalse(validation["signable"])
        self.assertEqual(validation["hearingChecks"][0]["status"], "fail")

    def test_same_words_do_not_imply_equivalence(self):
        script = self.script([
            {"role": "jia"},
            {"role": "yi", "hearing": {"startIndex": 0, "endIndex": 2}},
        ])
        a1 = self.take(self.jia, script["scriptId"], 0, ["同", "词"])
        a2 = self.take(self.jia, script["scriptId"], 0, ["同", "词"])
        self.hear(self.director["token"], self.yi, a1, 0, 2, "same-a1")
        started = self.svc.start_recording({"scriptId": script["scriptId"], "lineIndex": 1},
                                           self.yi["token"])
        b = self.svc.complete_recording(started["recordingId"], {"words": ["乙"]},
                                        self.yi["token"])
        validation = self.svc.validate_recording_ids(
            self.studio["studioId"], script["scriptId"],
            [a2["recordingId"], b["recordingId"]])
        self.assertFalse(validation["signable"])

        self.svc.create_recognition(self.studio["studioId"], {
            "left": {"recordingId": a1["recordingId"], "startIndex": 0, "endIndex": 2},
            "right": {"recordingId": a2["recordingId"], "startIndex": 0, "endIndex": 2},
        }, self.director["token"])
        validation = self.svc.validate_recording_ids(
            self.studio["studioId"], script["scriptId"],
            [a2["recordingId"], b["recordingId"]])
        self.assertTrue(validation["signable"])

    def test_late_confirmation_is_excluded(self):
        script = self.script([
            {"role": "jia"},
            {"role": "yi", "hearing": {"startIndex": 0, "endIndex": 2}},
        ])
        a = self.take(self.jia, script["scriptId"], 0, ["前", "词"])
        delivery = self.svc.send_delivery({
            "recordingId": a["recordingId"], "targetSessionId": self.yi["sessionId"]},
            self.director["token"])
        started = self.svc.start_recording({"scriptId": script["scriptId"], "lineIndex": 1},
                                           self.yi["token"])
        self.assertEqual(started["listeningSnapshot"]["confirmations"], [])
        self.svc.confirm_playback({
            "deliveryId": delivery["deliveryId"], "confirmationKey": "late",
            "startIndex": 0, "endIndex": 2,
        }, self.yi["token"])
        b = self.svc.complete_recording(started["recordingId"], {"words": ["乙"]},
                                        self.yi["token"])
        validation = self.svc.validate_recording_ids(
            self.studio["studioId"], script["scriptId"],
            [a["recordingId"], b["recordingId"]])
        self.assertFalse(validation["signable"])
        self.assertEqual(
            len(validation["hearingChecks"][0]["lateConfirmationsNotCounted"]), 1)

    def test_duplicate_confirmation_has_one_seq_and_restart_keeps_signed_sheet(self):
        script = self.script([
            {"role": "jia"},
            {"role": "yi", "hearing": {"startIndex": 0, "endIndex": 1}},
        ])
        a = self.take(self.jia, script["scriptId"], 0, ["前词"])
        delivery = self.svc.send_delivery({
            "recordingId": a["recordingId"], "targetSessionId": self.yi["sessionId"]},
            self.director["token"])
        _, first = self.svc.confirm_playback({
            "deliveryId": delivery["deliveryId"], "confirmationKey": "dup",
            "startIndex": 0, "endIndex": 1,
        }, self.yi["token"])
        status, second = self.svc.confirm_playback({
            "deliveryId": delivery["deliveryId"], "confirmationKey": "dup",
            "startIndex": 0, "endIndex": 1,
        }, self.yi["token"])
        self.assertEqual(status, 200)
        self.assertTrue(second["idempotentReplay"])
        self.assertEqual(first["confirmation"]["confirmationId"],
                         second["confirmation"]["confirmationId"])
        started = self.svc.start_recording({"scriptId": script["scriptId"], "lineIndex": 1},
                                           self.yi["token"])
        self.assertEqual(len(started["listeningSnapshot"]["confirmations"]), 1)
        b = self.svc.complete_recording(started["recordingId"], {"words": ["乙"]},
                                        self.yi["token"])
        sheet = self.svc.create_sheet({
            "studioId": self.studio["studioId"], "scriptId": script["scriptId"],
            "recordingIds": [a["recordingId"], b["recordingId"]],
        }, self.director["token"])
        signed = self.svc.sign_sheet(sheet["sheetId"], self.director["token"])
        self.assertEqual(signed["status"], "signed")
        digest = signed["canonicalHash"]

        self.svc.close()
        reopened = StudioService(self.db_path)
        try:
            got = reopened.sheet_dict(reopened.get_sheet_row(sheet["sheetId"]))
            self.assertEqual(got["canonicalHash"], digest)
            self.assertEqual(got["status"], "signed")
        finally:
            reopened.close()

    def test_signed_sheet_survives_recognition_revocation(self):
        script = self.script([
            {"role": "jia"},
            {"role": "yi", "hearing": {"startIndex": 0, "endIndex": 2}},
        ])
        a1 = self.take(self.jia, script["scriptId"], 0, ["同", "词"])
        a2 = self.take(self.jia, script["scriptId"], 0, ["同", "词"])
        self.hear(self.director["token"], self.yi, a1, 0, 2, "hear-a1-signed")
        started = self.svc.start_recording({"scriptId": script["scriptId"], "lineIndex": 1},
                                           self.yi["token"])
        b = self.svc.complete_recording(started["recordingId"], {"words": ["乙"]},
                                        self.yi["token"])
        recognition = self.svc.create_recognition(self.studio["studioId"], {
            "left": {"recordingId": a1["recordingId"], "startIndex": 0, "endIndex": 2},
            "right": {"recordingId": a2["recordingId"], "startIndex": 0, "endIndex": 2},
        }, self.director["token"])
        sheet = self.svc.create_sheet({
            "studioId": self.studio["studioId"], "scriptId": script["scriptId"],
            "recordingIds": [a2["recordingId"], b["recordingId"]],
        }, self.director["token"])
        signed = self.svc.sign_sheet(sheet["sheetId"], self.director["token"])
        self.assertEqual(signed["status"], "signed")

        self.svc.revoke_recognition(recognition["mutualRecognitionId"],
                                    self.director["token"])
        got = self.svc.sheet_dict(self.svc.get_sheet_row(sheet["sheetId"]))
        self.assertEqual(got["canonicalHash"], signed["canonicalHash"])
        basis = got["validation"]["hearingChecks"][0]["basis"]
        self.assertEqual(basis["type"], "mutual_recognition")

        unsigned = self.svc.create_sheet({
            "studioId": self.studio["studioId"], "scriptId": script["scriptId"],
            "recordingIds": [a2["recordingId"], b["recordingId"]],
        }, self.director["token"])
        self.assertEqual(unsigned["status"], "pending")

    def test_pickup_optimizer_uses_input_costs(self):
        script = self.script()
        a1 = self.take(self.jia, script["scriptId"], 0,
                       ["长亭", "雨歇", "风满袖", "归"])
        a2 = self.take(self.jia, script["scriptId"], 0,
                       ["长亭", "雨歇", "风满袖", "别"])
        self.hear(self.director["token"], self.yi, a1, 2, 4, "b-a1")
        b_started = self.svc.start_recording(
            {"scriptId": script["scriptId"], "lineIndex": 1}, self.yi["token"])
        b = self.svc.complete_recording(b_started["recordingId"],
                                        {"words": ["b0", "b1", "b2", "b3"]},
                                        self.yi["token"])
        self.hear(self.director["token"], self.bing, b, 1, 3, "c-b")
        c_started = self.svc.start_recording(
            {"scriptId": script["scriptId"], "lineIndex": 2}, self.bing["token"])
        c = self.svc.complete_recording(c_started["recordingId"], {"words": ["c0", "c1", "c2"]},
                                        self.bing["token"])
        baseline = [a2["recordingId"], b["recordingId"], c["recordingId"]]

        plan = self.svc.create_pickup_plan({
            "studioId": self.studio["studioId"], "scriptId": script["scriptId"],
            "baselineRecordingIds": baseline, "rerecordCosts": [80, 20, 100],
        }, self.director["token"])
        self.assertEqual(plan["plan"]["best"]["pickupLineIndexes"], [1])
        self.assertEqual(plan["plan"]["totalCost"], 20)
        fulfilled = self.svc.simulate_pickup_plan(plan["planId"], self.director["token"])
        self.assertEqual(fulfilled["state"], "completed")
        sheet = self.svc.create_sheet_from_plan(plan["planId"], self.director["token"])
        signed = self.svc.sign_sheet(sheet["sheetId"], self.director["token"])
        self.assertEqual(signed["status"], "signed")

        cheap_cue = self.svc.create_pickup_plan({
            "studioId": self.studio["studioId"], "scriptId": script["scriptId"],
            "baselineRecordingIds": baseline, "rerecordCosts": [5, 20, 100],
        }, self.director["token"])
        self.assertEqual(cheap_cue["plan"]["best"]["pickupLineIndexes"], [0])
        self.assertEqual(cheap_cue["plan"]["totalCost"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
