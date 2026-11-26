"""Narrative acceptance demo.

Run ``python -m sofa_service.demo``.  It exercises the same service methods as
the HTTP/JSON API and prints a compact audit trail.
"""

from __future__ import annotations

from .scenarios import narrow_order, small_order
from .service import ShopService
from .verifier import replay_step_sheet


def _complete_all(service: ShopService, oid: str, token: str) -> None:
    total = len(service.get_order(oid)["stepSheet"]["steps"])
    for i in range(1, total + 1):
        result = service.complete_step(oid, i, {"claimToken": token})
        if i < total and not result.get("canIssueNext"):
            raise RuntimeError(f"step {i} did not issue next: {result.get('code')}")


def main() -> None:
    service = ShopService()

    print("== 1. 小单：电梯/门口/走廊/装配区全程顺利 ==")
    q = service.create_quote(
        {"customerId": "demo-small", "floorPlanId": "small-smooth", "order": small_order("auto")}
    )
    print(f"报价 {q['quoteId']}：选择 {q['selectedMode']}，¥{q['price']['total']}，备选 {q['alternativeModesFeasible']}")
    oid = service.confirm_quote(q["quoteId"], {"customerId": "demo-small"})["orderId"]
    claim = service.claim_order(oid, {"masterId": "师傅·赵"})
    _complete_all(service, oid, claim["claimToken"])
    print(f"订单 {oid} 状态：{service.get_order(oid)['status']}")

    print("\n== 2. 窄廊：平放入门，在 [2,1] 支点顺时针转向 ==")
    q = service.create_quote(
        {
            "customerId": "demo-narrow",
            "floorPlanId": "narrow-l-turn",
            "measurementVersionId": "m-door-v1",
            "order": narrow_order("piecewise"),
        }
    )
    sheet = q["stepSheet"]
    turn = next(
        (i, s)
        for i, s in enumerate(sheet["steps"], 1)
        if s["kind"] == "rotate" and s.get("pivot") == [2, 1] and s.get("direction") == "cw"
    )
    print(f"报价 {q['quoteId']}：{q['selectedMode']}，{len(sheet['steps'])} 步，第 {turn[0]} 步为窄廊转向")
    oid = service.confirm_quote(q["quoteId"], {"customerId": "demo-narrow"})["orderId"]
    claim = service.claim_order(oid, {"masterId": "师傅·钱"})
    token = claim["claimToken"]

    print("\n-- 物业封掉旋转模板中间格 [3,2] --")
    service.set_blocks("narrow-l-turn", [[3, 2]], reason="新增鞋柜凸出一格")
    invalid = service.revalidate_order(oid)
    bad = invalid["violations"][0]
    print(f"原步单重新验：valid={invalid['valid']}，第 {invalid['firstInvalidStepIndex']} 步 {bad['code']} {bad['message']}")
    first = service.complete_step(oid, 1, {"claimToken": token})
    print(f"第1步报步已记录；是否签发下一步：canIssueNext={first['canIssueNext']}")

    print("\n-- 物业还原该格 --")
    fp = service.restore_floorplan("narrow-l-turn")
    valid = service.revalidate_order(oid)
    print(f"还原后几何哈希={fp['geometryHash']}，原报价哈希={q['floorPlanHashAtQuote']}，整单复验={valid['valid']}")
    # Step 1 was reported above; report the remaining prefix through the bad
    # turn and then the whole sheet.
    total = len(sheet["steps"])
    for i in range(2, total + 1):
        r = service.complete_step(oid, i, {"claimToken": token})
        if i < total and not r.get("canIssueNext", True):
            raise RuntimeError(f"unexpected stop at {i}: {r}")
    print(f"订单 {oid} 状态：{service.get_order(oid)['status']}")

    print("\n== 3. 顾客改订带凸耳扶手：旧步单必须重验，新单可另找走法 ==")
    lug_request = narrow_order(left_arm="arm-lug", orientation="left")
    stale = service.generic_replay(
        {
            "stepSheet": sheet,
            "floorPlanId": "narrow-l-turn",
            "orderRequest": lug_request,
            "catalog": service.catalog,
        }
    )
    print(f"标准扶手旧步单对凸耳订单复验：valid={stale['valid']}，首错第 {stale['firstInvalidStepIndex']} 步")
    lug_quote = service.create_quote(
        {"customerId": "demo-lug", "floorPlanId": "narrow-l-turn", "order": lug_request}
    )
    lug_replay = replay_step_sheet(
        lug_quote["stepSheet"], service.floorplans["narrow-l-turn"], service.catalog, lug_request
    )
    print(
        f"凸耳新报价 {lug_quote['quoteId']}：{lug_quote['selectedMode']}，"
        f"{len(lug_quote['stepSheet']['steps'])} 步，回放={lug_replay['valid']}"
    )

    print("\n== 4. 接合腾空面改版：旧组装次序失效，新单让靠背先于会占新空格的左扶手 ==")
    service.change_clearance("yunwu-two-seat", [[2, 0]], "back-to-seat")
    changed_request = narrow_order()
    revised = service.create_quote(
        {"customerId": "demo-craft", "floorPlanId": "narrow-l-turn", "order": changed_request}
    )
    print(f"新工艺装配顺序：{revised['stepSheet']['assemblyOrder']}")
    replay = replay_step_sheet(
        revised["stepSheet"], service.floorplans["narrow-l-turn"], service.catalog, changed_request
    )
    print(f"新步单回放：{replay['valid']}")

    print("\n== 5. 两版量尺与迟到确认 ==")
    fresh = ShopService()
    q1 = fresh.create_quote(
        {
            "customerId": "survey",
            "floorPlanId": "narrow-l-turn",
            "measurementVersionId": "m-door-v1",
            "order": narrow_order(),
        }
    )
    o1 = fresh.confirm_quote(q1["quoteId"], {"customerId": "survey"})
    fresh.claim_order(o1["orderId"], {"masterId": "师傅·孙"})
    q2 = fresh.create_quote(
        {
            "customerId": "survey",
            "floorPlanId": "narrow-l-turn",
            "measurementVersionId": "m-door-v2",
            "order": narrow_order(),
        }
    )
    try:
        fresh.confirm_quote(q2["quoteId"], {"customerId": "survey"})
    except Exception as exc:  # noqa: BLE001
        print(f"迟到的 v2 报价确认被拒：{getattr(exc, 'code', type(exc).__name__)}")
    prov = fresh.get_provenance(o1["orderId"])
    print(f"已领订单 {o1['orderId']} 依据量尺：{prov['measurementVersionId']} / {prov['measurement']['surveyor']} / {prov['measurement']['widthCm']}cm")

    print("\n全部演示通过。")


if __name__ == "__main__":
    main()
