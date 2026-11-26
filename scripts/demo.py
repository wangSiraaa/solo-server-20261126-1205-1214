"""End-to-end narrated demo of the exhibition-workshop service.

Run:  PYTHONPATH=. python3 scripts/demo.py

Prints, step by step, what the tenant sees after a calibration is backfilled --
including the edited-receipt flip, the tightened requirement, the two-box "do
not recall the whole box" check, and the stale-screen / restart claim dedupe.
"""

from __future__ import annotations

import os
import tempfile

from app.engine import Service
from app.store import Store


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_plan(svc, cid):
    plan = svc.plan(calibration_id=cid)
    print("  已交付 release :", plan["released_boards"])
    print("  现场复核 recheck:", plan["recheck_boards"],
          " slots:", [(s["slot"], s["board_ids"]) for s in plan["recheck_slots"]])
    print("  召回 recall    :", plan["recall_boards"])
    print("  换件 replace   :", plan["replace_pieces"])
    print("  须重测 must_retest:", plan["retest"]["must_retest"])
    return plan


def main():
    d = tempfile.mkdtemp(prefix="exhibit-demo-")
    db = os.path.join(d, "demo.db")
    svc = Service(Store(db))
    svc.upsert_piece(piece_id="S2", kind="start", nominal_bias=0, true_bias=2)
    svc.upsert_piece(piece_id="S0", kind="start", nominal_bias=0, true_bias=0)
    svc.upsert_piece(piece_id="E1", kind="extension", nominal_bias=0, true_bias=1)
    svc.create_ruler(ruler_id="R1")
    svc.create_box(box_id="BOX-A", piece_ids=["S2", "S0", "E1"], tenant_id="T1")

    hr("1. 小例：起点片 +2，直接读 12 -> 真 10；同片相减 22-12 -> 真 10")
    svc.create_board(board_id="A", tenant_id="T1", min_len=10, max_len=10,
                     ref_point_id="A-ref", box_id="BOX-A")
    svc.submit_usage_receipt({"board_id": "A", "ruler_id": "R1",
                              "method": "direct", "config": {"start_piece": "S2"},
                              "reading": 12})
    svc.create_board(board_id="B", tenant_id="T1", min_len=10, max_len=10,
                     ref_point_id="B-ref", box_id="BOX-A")
    svc.submit_usage_receipt({"board_id": "B", "ruler_id": "R1",
                              "method": "subtract", "config": {"start_piece": "S2"},
                              "reading_a": 12, "reading_b": 22})
    print("  A 真长:", svc.evaluate_board("A")["true_length"],
          " B 真长:", svc.evaluate_board("B")["true_length"])

    hr("2. 补入校准（S2=+2, E1=+1）；两块板都仍合格 -> 正常交付")
    cal = svc.add_calibration(piece_biases={"S2": 2, "E1": 1})
    svc.dispatch_calibration(calibration_id=cal["id"], box_id="BOX-A")
    show_plan(svc, cal["id"])

    hr("3. 把第一笔使用回执改成装了无偏置替换片 S0（仍读 12）")
    svc.replace_usage_receipt(
        board_id="A",
        payload={"board_id": "A", "ruler_id": "R1", "method": "direct",
                 "config": {"start_piece": "S0"}, "reading": 12})
    print("  A 真长:", svc.evaluate_board("A")["true_length"],
          " 动作:", svc.evaluate_board("A")["action"])
    svc.dispatch_calibration(calibration_id=cal["id"], box_id="BOX-A")
    show_plan(svc, cal["id"])

    hr("4. 还原回执（S2，读 12 -> 真 10），但装配净隙改成至少 11")
    svc.replace_usage_receipt(
        board_id="A",
        payload={"board_id": "A", "ruler_id": "R1", "method": "direct",
                 "config": {"start_piece": "S2"}, "reading": 12})
    svc.set_requirement(board_id="A", min_len=11, max_len=None)
    svc.dispatch_calibration(calibration_id=cal["id"], box_id="BOX-A")
    plan = show_plan(svc, cal["id"])
    wo_a = [w for w in plan["work_orders"].values()
            if w["board_id"] == "A"][0]
    print("  A 的工单:", wo_a["id"], wo_a["kind"],
          "要求", wo_a["requirement"])

    hr("5. 现场复核：A 重测读 13（真 11）-> 满足 >=11，工单从复核翻成放行")
    res = svc.complete_recheck(
        board_id="A",
        payload={"method": "direct", "ruler_id": "R1",
                 "config": {"start_piece": "S2"}, "reading": 13},
        worker_id="W-recheck")
    print("  changed_from:", res["changed_from"], "-> kind:",
          res["work_order"]["kind"], " status:", res["work_order"]["status"])

    hr("6. 同箱不同片两组 + 一块确能复核放行的小板（不整箱召回）")
    # 校准前只知道标称偏置 0；真相由这次校准揭示。
    svc.upsert_piece(piece_id="G1", kind="start", nominal_bias=0)
    svc.upsert_piece(piece_id="G2", kind="start", nominal_bias=0)
    svc.upsert_piece(piece_id="G3", kind="start", nominal_bias=0)
    svc.create_box(box_id="BOX-B", piece_ids=["G1", "G2", "G3"], tenant_id="T1")
    # G1 直接读数、真长 10、要求 12 -> 校准后不合格
    svc.create_board(board_id="B1", tenant_id="T1", min_len=12, max_len=12,
                     ref_point_id="B1-ref", box_id="BOX-B")
    svc.submit_usage_receipt({"board_id": "B1", "ruler_id": "R1",
                              "method": "direct", "config": {"start_piece": "G1"},
                              "reading": 12})
    # G1 相减 -> 偏置抵消，真长 10，满足 10
    svc.create_board(board_id="B2", tenant_id="T1", min_len=10, max_len=10,
                     ref_point_id="B2-ref", box_id="BOX-B")
    svc.submit_usage_receipt({"board_id": "B2", "ruler_id": "R1",
                              "method": "subtract", "config": {"start_piece": "G1"},
                              "reading_a": 12, "reading_b": 22})
    # G2 确实无偏置，直接读数 10 -> 真 10，满足 10
    svc.create_board(board_id="B3", tenant_id="T1", min_len=10, max_len=10,
                     ref_point_id="B3-ref", box_id="BOX-B")
    svc.submit_usage_receipt({"board_id": "B3", "ruler_id": "R1",
                              "method": "direct", "config": {"start_piece": "G2"},
                              "reading": 10})
    # 小板：回执误记成 G3（其实用的是 G2），真长 6，要求 6
    svc.create_board(board_id="PLATE", tenant_id="T1", min_len=6, max_len=6,
                     ref_point_id="PLATE-ref", box_id="BOX-B")
    svc.submit_usage_receipt({"board_id": "PLATE", "ruler_id": "R1",
                              "method": "direct", "config": {"start_piece": "G3"},
                              "reading": 6})
    print("  校准前各板判定:",
          {b: svc.evaluate_board(b)["action"] for b in ("B1", "B2", "B3", "PLATE")})
    cal2 = svc.add_calibration(piece_biases={"G1": 2, "G2": 0, "G3": 2})
    svc.dispatch_calibration(calibration_id=cal2["id"], box_id="BOX-B")
    show_plan(svc, cal2["id"])
    print("  仅 B1、PLATE 须处理；B2/B3 直接正常交付，未整箱召回。")
    # 小板现场复核：用独立尺读数 6 -> 真 6 -> 当场放行
    res_plate = svc.complete_recheck(
        board_id="PLATE",
        payload={"method": "direct", "ruler_id": "R1",
                 "config": {"start_piece": "G2"}, "reading": 6},
        worker_id="W-recheck")
    print("  小板现场复核后:", res_plate["work_order"]["kind"],
          res_plate["work_order"]["status"],
          " 真长", res_plate["evaluation"]["true_length"])
    show_plan(svc, cal2["id"])

    hr("7. 两名工人从同一旧屏幕申领同一次复核；关机重开后只见一个有效处置")
    # force B1 into recheck by giving it a reachable ref (it is) -- already recheck
    plan_b = svc.plan(calibration_id=cal2["id"])
    wo_b1 = [w for w in plan_b["work_orders"].values()
             if w["board_id"] == "B1"][0]
    c1 = svc.claim(work_order_id=wo_b1["id"], worker_id="W1",
                   screen_token="old-screen-7")
    c2 = svc.claim(work_order_id=wo_b1["id"], worker_id="W2",
                   screen_token="old-screen-7")
    print("  工单", wo_b1["id"], " kind:", wo_b1["kind"],
          " slot:", wo_b1["slot"])
    print("  工人1 处置:", c1["id"], " dedup:", c1["deduplicated"])
    print("  工人2 处置:", c2["id"], " dedup:", c2["deduplicated"])
    # restart
    svc = Service(Store(db))
    c3 = svc.claim(work_order_id=wo_b1["id"], worker_id="W2",
                   screen_token="old-screen-7")
    print("  重开后工人2 再申领:", c3["id"], " dedup:", c3["deduplicated"])
    disps = svc.list_dispositions(tenant_id="T1")
    print("  租户端有效处置数:", len(disps))
    chain = svc.get_measurement_chain(work_order_id=wo_b1["id"])
    print("  测量链 真长:", chain["chain"]["true_length"],
          " 要求:", chain["chain"]["requirement"],
          " 处置:", chain["disposition_id"])
    print("\nDB:", db)


if __name__ == "__main__":
    main()
