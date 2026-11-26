from __future__ import annotations

"""End-to-end acceptance scenario from the bead-shop brief.

Run:
    python -m beadworks.demo
    python -m beadworks.demo --db data/demo.json
"""

import argparse
import os
import tempfile
from typing import Any, Dict

from .service import Beadworks, RemovalNotFeasible

FRIDAY = "2026-11-27"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


def build_green_inquiry(
    svc: Beadworks,
    suffix: str,
    ring_width: float,
    clasp_kind: str,
    sealed_holder_id: str | None = None,
) -> str:
    piece_id = f"green-{suffix}"
    clasp_id = f"clasp-{suffix}"
    chain_id = f"chain-{suffix}"
    ring_id = f"ring-{suffix}"
    svc.receive_piece(
        piece_id=piece_id,
        name="青珠",
        owner_id="p-lin",
        hole_width=3,
        color="青",
        temp_removal_allowed=True,
        intake_note="续接旧半成品：左端封死，只能往右",
    )
    svc.add_clasp(
        clasp_id=clasp_id,
        name=f"最右出口扣-{suffix}",
        size=3,
        kind=clasp_kind,
        required_holder_id=sealed_holder_id,
        fitted=True,
        source="clerk_registered",
    )
    svc.create_chain(
        chain_id=chain_id,
        label=f"青珠询价案 {suffix}",
        style="old_semi_finished",
        segments=[
            {
                "id": "only-rope",
                "left": {"type": "closed", "openable": False},
                "right": {"type": "port", "claspId": clasp_id},
                "track": [
                    {"type": "bead", "pieceId": piece_id},
                    {"type": "ring", "ringId": ring_id, "width": ring_width, "label": "唯一途中固定圈"},
                ],
            }
        ],
    )
    return chain_id


def run(db_path: str | None = None) -> Dict[str, Any]:
    if db_path is None:
        fd, db_path = tempfile.mkstemp(prefix="beadworks-demo-", suffix=".json")
        os.close(fd)
    if os.path.exists(db_path):
        os.unlink(db_path)

    print(f"使用持久化账本: {db_path}")
    svc = Beadworks(db_path)

    print("\n1. 建人、接件与青珠三笔独立询价")
    svc.create_person(person_id="p-lin", name="林物主")
    svc.create_person(person_id="p-chen", name="陈借客")
    svc.create_person(person_id="p-wang", name="王缺席")
    svc.stock_clasp(clasp_id="clasp-not-installed", name="库存备用扣", size=9)

    base_chain = build_green_inquiry(svc, "base", 2, "shop")
    wide_chain = build_green_inquiry(svc, "wide", 4, "shop")
    sealed_chain = build_green_inquiry(svc, "sealed", 2, "sealed", "p-wang")

    visit = svc.schedule_visit(
        visit_id="visit-wang-friday", person_id="p-wang", on_date=FRIDAY, purpose="开启唯一封扣"
    )
    svc.mark_visit_absent(visit["id"])

    q_base = svc.quote_removal(quote_id="quote-base", chain_id=base_chain, piece_id="green-base", on_date=FRIDAY)
    q_wide = svc.quote_removal(quote_id="quote-wide", chain_id=wide_chain, piece_id="green-wide", on_date=FRIDAY)
    q_sealed = svc.quote_removal(
        quote_id="quote-sealed", chain_id=sealed_chain, piece_id="green-sealed", on_date=FRIDAY
    )

    _assert(q_base["feasible"] is True, "原案：宽二圈不超过珠孔三，店扣可开，周五可按时取下")
    _assert(q_base["chosenDirection"] == "right", "原案：左端不可开，规划从右端取出")
    _assert(q_base["chosenRoute"]["exitClaspId"] == "clasp-base", "原案：唯一出口是最右实配店扣")
    _assert(q_base["improvisedClaspAllowed"] is False, "询价不会临时加装库存扣")
    _assert(q_wide["feasible"] is False, "变案一：固定圈改宽四后接不下来")
    _assert(any(b["code"] == "RING_BLOCKS_HOLE" for b in q_wide["blockers"]), "变案一：阻挡原因为圈宽超过珠孔")
    _assert(q_sealed["feasible"] is False, "变案二：封扣所需持有人缺席，原日子不能交")
    _assert(any(b["code"] == "HOLDER_ABSENT" for b in q_sealed["blockers"]), "变案二：阻挡原因为持有人当天缺席")
    _assert("clasp-not-installed" not in svc.get_chain_view(wide_chain)["attachedClaspIds"], "未配上的库存扣不算出口")

    print("\n2. 青珠租借记录与柜台真实拆还")
    svc.receive_piece(
        piece_id="bead-green-real",
        name="借来的青珠",
        owner_id="p-lin",
        hole_width=3,
        temp_removal_allowed=True,
    )
    svc.add_clasp(clasp_id="clasp-green-real", size=3, kind="shop", fitted=True)
    svc.create_chain(
        chain_id="chain-green-real",
        style="old_semi_finished",
        segments=[
            {
                "id": "rope",
                "left": {"type": "closed"},
                "right": {"type": "port", "claspId": "clasp-green-real"},
                "track": [
                    {"type": "bead", "pieceId": "bead-green-real"},
                    {"type": "ring", "ringId": "ring-green-real", "width": 2},
                ],
            }
        ],
    )
    loan = svc.create_rental(
        rental_id="loan-green",
        piece_ids=["bead-green-real"],
        renter_id="p-chen",
        lender_id="p-lin",
        start_date="2026-11-20",
        due_date=FRIDAY,
    )
    svc.hand_over_rental(loan["id"])
    # 借客周五到柜接件；租借合约当天了结，随后由柜台拆还给物主。
    svc.return_rental(loan["id"])
    removal_green = svc.remove_and_return_piece(
        removal_id="removal-green",
        chain_id="chain-green-real",
        piece_id="bead-green-real",
        return_to_id="p-lin",
        on_date=FRIDAY,
    )
    green_loc = svc.locate_piece("bead-green-real")
    _assert(removal_green["wholeChainReturned"] is False, "青珠有单独拆还记录，不是整链退回")
    _assert(green_loc["holderId"] == "p-lin", "拆出的青珠实际在物主林物主手中")
    _assert(green_loc["individualReturn"] is True, "青珠可按件查询到单独归还")
    _assert(svc.get_chain_view("chain-green-real")["currentPieceIds"] == [], "青珠已从原绳轨道移除")

    print("\n3. 经营分段款：一单一交期，三位物主不同日期取私件")
    for pid, name in [("zhao", "赵师傅"), ("qian", "钱女士"), ("sun", "孙先生")]:
        svc.create_person(person_id=f"owner-{pid}", name=name)
    svc.receive_piece(piece_id="zhao-main", name="赵地主珠", owner_id="owner-zhao", hole_width=4)
    svc.receive_piece(piece_id="zhao-helper", name="赵邻珠", owner_id="owner-zhao", hole_width=5)
    svc.receive_piece(piece_id="qian-main", name="钱家珠", owner_id="owner-qian", hole_width=4)
    svc.receive_piece(piece_id="sun-main", name="孙家珠", owner_id="owner-sun", hole_width=4)
    for cid, size in [("seg-c1", 4), ("seg-c2", 4), ("seg-end", 5)]:
        svc.stock_clasp(clasp_id=cid, size=size, name=f"店配连接扣 {cid}")

    order = svc.create_segmented_order(
        order_id="order-segmented-001",
        chain_id="chain-segmented",
        label="三家独立短链分段手链",
        customer_id="owner-zhao",
        due_date="2026-11-30",
        groups=[
            {"ownerId": "owner-zhao", "pieceIds": ["zhao-main", "zhao-helper"]},
            {"ownerId": "owner-qian", "pieceIds": ["qian-main"]},
            {"ownerId": "owner-sun", "pieceIds": ["sun-main"]},
        ],
        connector_clasp_ids=["seg-c1", "seg-c2"],
        terminal_clasp_id="seg-end",
        pickup_schedule=[
            {"date": "2026-12-02", "pieceId": "zhao-main"},
            {"date": "2026-12-04", "pieceId": "qian-main"},
            {"date": "2026-12-06", "pieceId": "sun-main"},
        ],
    )
    segmented_chain = order["chainId"]
    _assert(order["status"] == "finished" and order["dueDate"] == "2026-11-30", "已做成有交期的成品单")
    _assert(len(order["installedClaspIds"]) == 3, "选用的三个扣均已实配，库存目录扣不会自动变出口")

    r_zhao = svc.remove_and_return_piece(
        chain_id=segmented_chain, piece_id="zhao-main", return_to_id="owner-zhao", on_date="2026-12-02"
    )
    _assert(r_zhao["temporaryPieceIdsClearOrder"] == ["zhao-helper"], "赵珠路径上的普通邻珠按从端口向内顺序暂存")
    _assert(len(svc.db["temp_removals"]) >= 1 and all(x["restored"] for x in svc.db["temp_removals"] if x["removalId"] == r_zhao["id"]), "邻珠均有暂存记录并已穿回")
    svc.remove_and_return_piece(
        chain_id=segmented_chain, piece_id="qian-main", return_to_id="owner-qian", on_date="2026-12-04"
    )
    svc.remove_and_return_piece(
        chain_id=segmented_chain, piece_id="sun-main", return_to_id="owner-sun", on_date="2026-12-06"
    )
    _assert(svc.locate_piece("qian-main")["holderId"] == "owner-qian", "钱女士无需其他持有人到场即可按日取走私件")
    _assert(svc.locate_piece("sun-main")["holderId"] == "owner-sun", "孙先生无需其他持有人到场即可按日取走私件")

    print("\n4. 整链退回不能冒充按件拆还")
    svc.create_person(person_id="p-whole-owner", name="整链物主")
    svc.receive_piece(piece_id="whole-bead", name="未拆珠", owner_id="p-whole-owner", hole_width=3)
    svc.add_clasp(clasp_id="whole-clasp", size=3, kind="shop", fitted=True)
    svc.create_chain(
        chain_id="whole-chain",
        segments=[{"right": {"type": "port", "claspId": "whole-clasp"}, "track": [{"type": "bead", "pieceId": "whole-bead"}]}],
    )
    svc.return_whole_chain("whole-chain", "p-whole-owner", "2026-12-07")
    whole_loc = svc.locate_piece("whole-bead")
    _assert(whole_loc["holderId"] == "p-whole-owner", "整链退回后珠随链在物主手")
    _assert(whole_loc["individualReturn"] is False, "但该珠没有单独拆还记录，不能算作柜台还珠")

    print("\n5. 另办三人合串：先租出、回柜拆还、下一位租客领走余链")
    for pid, name in [("zhou", "周设计"), ("wu", "吴寄租"), ("zheng", "郑寄租")]:
        svc.create_person(person_id=f"owner-{pid}", name=name)
    svc.create_person(person_id="renter-old", name="钱租客")
    svc.create_person(person_id="renter-next", name="冯下一位")
    svc.receive_piece(piece_id="trio-a", name="赤珠", owner_id="owner-zhou", hole_width=4)
    svc.receive_piece(
        piece_id="trio-b", name="蓝珠", owner_id="owner-wu", hole_width=4, consigned_for_rental=True
    )
    svc.receive_piece(
        piece_id="trio-c", name="紫珠", owner_id="owner-zheng", hole_width=4, consigned_for_rental=True
    )
    for cid in ("trio-c1", "trio-c2", "trio-end"):
        svc.stock_clasp(clasp_id=cid, size=4)
    trio_order = svc.create_segmented_order(
        order_id="order-trio",
        chain_id="chain-trio",
        label="柜台自行设计的三人合串",
        due_date="2026-12-08",
        groups=[
            {"ownerId": "owner-zhou", "pieceIds": ["trio-a"]},
            {"ownerId": "owner-wu", "pieceIds": ["trio-b"]},
            {"ownerId": "owner-zheng", "pieceIds": ["trio-c"]},
        ],
        connector_clasp_ids=["trio-c1", "trio-c2"],
        terminal_clasp_id="trio-end",
    )
    trio_chain = trio_order["chainId"]
    old_rent = svc.create_rental(
        rental_id="rent-old",
        chain_id=trio_chain,
        renter_id="renter-old",
        start_date="2026-12-09",
        due_date="2026-12-15",
        note="上一位租客",
    )
    svc.hand_over_rental(old_rent["id"])
    svc.return_rental(old_rent["id"])

    svc.remove_and_return_piece(
        removal_id="removal-trio-a",
        chain_id=trio_chain,
        piece_id="trio-a",
        return_to_id="owner-zhou",
        on_date="2026-12-16",
    )
    next_rent = svc.create_rental(
        rental_id="rent-next",
        chain_id=trio_chain,
        renter_id="renter-next",
        start_date="2026-12-17",
        due_date="2027-01-17",
        consigned=True,
        note="吴、郑两件签过寄租，随余链继续租给下一位",
    )
    svc.hand_over_rental(next_rent["id"])
    trio_report = svc.remaining_chain_report(trio_chain)
    _assert(trio_report["pieceIds"] == ["trio-b", "trio-c"], "赤珠拆还后，余链只剩蓝、紫两件")
    _assert(trio_report["holderId"] == "renter-next", "下一位租客已真实领走余链")
    _assert(set(trio_report["attachedClaspIds"]) == {"trio-c1", "trio-c2", "trio-end"}, "余链仍连接在三个实配扣上")

    print("\n6. 模拟服务重启：从同一 JSON 账本重新加载")
    restarted = Beadworks(db_path)
    a_loc = restarted.locate_piece("trio-a")
    restarted_report = restarted.remaining_chain_report(trio_chain)
    _assert(a_loc["holderId"] == "owner-zhou", "重启后仍可查得拆出的赤珠在周设计手中")
    _assert(restarted_report["holderId"] == "renter-next", "重启后余链持有人仍是下一位租客")
    _assert(
        set(restarted_report["attachedClaspIds"]) == {"trio-c1", "trio-c2", "trio-end"},
        "重启后仍可查得余链连接的扣",
    )

    print("\n全部验收场景通过。")
    return {"dbPath": db_path, "baseQuote": q_base, "wideQuote": q_wide, "sealedQuote": q_sealed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Beadworks acceptance scenario")
    parser.add_argument("--db", default=None, help="JSON ledger path; defaults to a temp file")
    args = parser.parse_args()
    run(args.db)
