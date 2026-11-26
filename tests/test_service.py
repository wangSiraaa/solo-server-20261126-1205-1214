"""端到端场景测试：开单 → 备料 → 洗印单 → 曝光回条 → 接样 → 取件。

对应题目验收：
1. 两相邻格最小生意，恰好一种"只照背景两拍"的操作，可交件；
2. 换样卡（背景目标阶从光量四开始）且只剩两拍 -> 不能交；
3. 恢复原样卡、脸已超过目标阶 -> 不能交；
4. 程序重开后接另一张起点不同的纸，算出所需操作；
5. 曝光回条迟到/重复，不能把同一张纸当新纸再曝光；
6. 开工后补刻遮片被拒；
7. 旧试印沿用与库存不足；
8. HTTP JSON 接口全链路。
"""

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from photolab.domain import ConflictError, LabError
from photolab.server import make_server
from photolab.service import PhotoLabService

CARD = [["阶二", 2], ["阶三", 3]]
CARD_FROM_FOUR = [["阶二", 2], ["阶三", 4]]


def intake_payload(batch_id="B1", card=CARD, papers=None, reuse=0):
    papers = papers or [
        {"id": "P1", "start": {"0,0": 2, "0,1": 1}, "beam_per_cell": {}},
    ]
    return {
        "batch_id": batch_id, "rows": 1, "cols": 2, "card": card,
        "sheet_budget": 5, "shot_budget": 2,
        "papers": papers,
        "order": {
            "id": "O1", "customer": "订户", "reuse_count": reuse,
            "requirements": [
                {"paper_id": "P1", "cell": "0,0", "role": "face", "level": "阶二"},
                {"paper_id": "P1", "cell": "0,1", "role": "background", "level": "阶三"},
            ],
        },
    }


class ServiceScenarioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.svc = PhotoLabService(root / "lab.db", root / "products")

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def _open_order(self, payload=None):
        payload = payload or intake_payload()
        self.svc.intake_batch(**{
            "batch_id": payload["batch_id"], "rows": payload["rows"],
            "cols": payload["cols"], "card_pairs": payload["card"],
            "sheet_budget": payload["sheet_budget"],
            "shot_budget": payload["shot_budget"],
            "papers": payload["papers"], "order": payload["order"],
        })
        prep = self.svc.prepare("B1")
        self.assertTrue(prep["feasible"], prep.get("reason"))
        self.svc.open_batch("B1")
        return prep

    def test_happy_path_exactly_one_background_only_operation(self):
        prep = self._open_order()
        plan = prep["plan"]
        self.assertEqual(plan["shot_total"], 2)
        self.assertEqual(plan["sheets_used"], 1)
        # 唯一遮片形状就是单格；每拍只照背景格 (0,1)
        self.assertEqual(plan["masks"], [[[0, 0]]])
        shots = plan["papers"][0]["shots"]
        self.assertEqual(len(shots), 2)
        for shot in shots:
            self.assertEqual(shot["mask"], [[0, 0]])
            self.assertEqual(shot["offset"], [0, 1])
            self.assertEqual(shot["exposures"], {"0,1": 1})

        first = self.svc.confirm_shot("P1")
        self.assertFalse(first["completed"])
        self.assertEqual(first["accumulated"], {"0,0": 2, "0,1": 2})
        second = self.svc.confirm_shot("P1")
        self.assertTrue(second["completed"])
        self.assertEqual(second["accumulated"], {"0,0": 2, "0,1": 3})

        sample = self.svc.receive_sample("S1", "P1", note="新样色阶符合")
        self.assertEqual(sample["observed"], {"0,0": "阶二", "0,1": "阶三"})
        delivery = self.svc.deliver("O1")
        self.assertEqual(delivery["status"], "delivered")
        product_files = list(Path(self.tmp.name).glob("products/*"))
        self.assertTrue(any(str(p).endswith(".svg") for p in product_files))
        self.assertTrue(any(str(p).endswith(".pgm") for p in product_files))

    def test_changed_card_target_starts_at_four_cannot_deliver(self):
        payload = intake_payload(card=CARD_FROM_FOUR)
        self.svc.intake_batch(
            payload["batch_id"], payload["rows"], payload["cols"], payload["card"],
            payload["sheet_budget"], payload["shot_budget"],
            payload["papers"], payload["order"],
        )
        prep = self.svc.prepare("B1")
        self.assertFalse(prep["feasible"])
        # 两拍最多把背景从 1 推到 3，到不了光量 4 才开始的阶三
        with self.assertRaises(ConflictError):
            self.svc.open_batch("B1")

    def test_face_over_target_level_cannot_deliver(self):
        payload = intake_payload(papers=[
            {"id": "P1", "start": {"0,0": 3, "0,1": 1}, "beam_per_cell": {}},
        ])
        self.svc.intake_batch(
            payload["batch_id"], payload["rows"], payload["cols"], payload["card"],
            payload["sheet_budget"], payload["shot_budget"],
            payload["papers"], payload["order"],
        )
        prep = self.svc.prepare("B1")
        self.assertFalse(prep["feasible"])
        self.assertIn("不能擦掉", prep["reason"])

    def test_restart_picks_up_other_paper_with_different_start(self):
        prep = self._open_order()
        self.svc.confirm_shot("P1")
        self.svc.confirm_shot("P1")
        db = Path(self.tmp.name) / "lab.db"
        products = Path(self.tmp.name) / "products"
        self.svc.close()

        # 程序重开：数据库里的批次与累计光量原样接回
        svc2 = PhotoLabService(db, products)
        status = svc2.status("B1")
        self.assertEqual(status["papers"][0]["accumulated"], {"0,0": 2, "0,1": 3})
        svc2.close()

        # 同一店内再接一张起点不同的纸（背景从未入阶起步，需要三拍）
        svc3 = PhotoLabService(db, products)
        payload = intake_payload(
            batch_id="B2",
            papers=[{"id": "P2", "start": {"0,0": 2, "0,1": 0}, "beam_per_cell": {}}],
        )
        payload["order"]["id"] = "O2"
        payload["shot_budget"] = 3
        for req in payload["order"]["requirements"]:
            req["paper_id"] = "P2"
        svc3.intake_batch(
            payload["batch_id"], payload["rows"], payload["cols"], payload["card"],
            payload["sheet_budget"], payload["shot_budget"],
            payload["papers"], payload["order"],
        )
        prep2 = svc3.prepare("B2")
        self.assertTrue(prep2["feasible"])
        self.assertEqual(prep2["plan"]["shot_total"], 3)
        svc3.open_batch("B2")
        for _ in range(3):
            svc3.confirm_shot("P2")
        self.assertEqual(svc3.status("B2")["papers"][0]["accumulated"],
                         {"0,0": 2, "0,1": 3})
        svc3.receive_sample("S2", "P2")
        svc3.deliver("O2")
        svc3.close()

    def test_late_receipt_cannot_reexpose_same_paper(self):
        self._open_order()
        self.svc.confirm_shot("P1")
        self.svc.confirm_shot("P1")  # 洗印单两拍全部落账
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm_shot("P1")  # 迟到的第三拍回条
        self.assertIn("迟到", str(ctx.exception))
        # 重复提交同一拍也被唯一约束挡住
        with self.assertRaises(ConflictError):
            self.svc.confirm_shot("P1")

    def test_no_new_mask_after_open(self):
        self._open_order()
        with self.assertRaises(ConflictError):
            self.svc.store.register_mask("B1", [[0, 0]], [0, 0], [[0, 0]])
        with self.assertRaises(ConflictError):
            self.svc.prepare("B1")

    def test_shot_must_match_registered_mask_and_print_sheet(self):
        self._open_order()
        # 回条光量与洗印单不符 -> 拒收且不留账
        with self.assertRaises(ConflictError):
            self.svc.confirm_shot("P1", exposures={"0,1": 2})
        self.assertEqual(self.svc.status("B1")["papers"][0]["shots_logged"], 0)
        # 未登记的遮片位置（构造一次越表调用）
        with self.assertRaises(ConflictError):
            self.svc._assert_registered("B1", [[0, 0], [0, 1]], [0, 0])

    def test_reused_trial_needs_stock_and_skips_exposure(self):
        self.svc.store.add_stock_trial("T1", "阶三")
        payload = intake_payload(papers=[
            {"id": "P1", "start": {"0,0": 2, "0,1": 1}, "beam_per_cell": {},
             "reused": True},
        ], reuse=1)
        self.svc.intake_batch(
            payload["batch_id"], payload["rows"], payload["cols"], payload["card"],
            payload["sheet_budget"], payload["shot_budget"],
            payload["papers"], payload["order"],
        )
        prep = self.svc.prepare("B1")
        self.assertTrue(prep["feasible"])
        self.assertEqual(prep["plan"]["shot_total"], 0)  # 沿用旧试印，零新曝光
        self.svc.open_batch("B1")
        with self.assertRaises(ConflictError):
            self.svc.confirm_shot("P1")
        self.svc.receive_sample("S1", "P1", note="旧试印复核")
        self.svc.deliver("O1")

    def test_reuse_without_stock_rejected_at_intake(self):
        payload = intake_payload(papers=[
            {"id": "P1", "start": {"0,0": 2, "0,1": 1}, "reused": True},
        ], reuse=1)
        with self.assertRaises(ConflictError):
            self.svc.intake_batch(
                payload["batch_id"], payload["rows"], payload["cols"], payload["card"],
                payload["sheet_budget"], payload["shot_budget"],
                payload["papers"], payload["order"],
            )

    def test_deliver_blocked_until_printed(self):
        self._open_order()
        with self.assertRaises(ConflictError):
            self.svc.deliver("O1")


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.httpd = make_server(root / "lab.db", root / "products", port=0)
        self.port = self.httpd.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.service.close()
        self.tmp.cleanup()

    def _call(self, method: str, path: str, payload=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_over_http(self):
        code, body = self._call("POST", "/batches", intake_payload())
        self.assertEqual(code, 201)
        code, prep = self._call("POST", "/batches/B1/prepare")
        self.assertEqual(code, 200)
        self.assertTrue(prep["feasible"])
        code, _ = self._call("POST", "/batches/B1/open")
        self.assertEqual(code, 200)
        code, sheet = self._call("GET", "/batches/B1/print-sheet")
        self.assertEqual(code, 200)
        self.assertEqual(sheet["print_sheet"]["shot_total"], 2)
        code, shot1 = self._call("POST", "/papers/P1/shots", {})
        self.assertEqual(code, 200)
        self.assertFalse(shot1["completed"])
        code, shot2 = self._call("POST", "/papers/P1/shots", {})
        self.assertTrue(shot2["completed"])
        code, late = self._call("POST", "/papers/P1/shots", {})
        self.assertEqual(code, 409)
        code, sample = self._call("POST", "/papers/P1/samples",
                                  {"sample_id": "S1", "note": "接样"})
        self.assertEqual(code, 201)
        self.assertEqual(sample["observed"], {"0,0": "阶二", "0,1": "阶三"})
        code, delivery = self._call("POST", "/orders/O1/deliver")
        self.assertEqual(code, 200)
        self.assertEqual(delivery["status"], "delivered")

    def test_infeasible_returns_422_over_http(self):
        code, body = self._call("POST", "/batches",
                                intake_payload(card=CARD_FROM_FOUR))
        self.assertEqual(code, 201)
        code, prep = self._call("POST", "/batches/B1/prepare")
        self.assertEqual(code, 422)
        self.assertFalse(prep["feasible"])


if __name__ == "__main__":
    unittest.main()
