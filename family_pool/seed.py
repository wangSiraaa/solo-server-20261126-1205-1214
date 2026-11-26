from __future__ import annotations

from typing import Any, Dict

from .service import FamilyPoolService

DEMO_IDS = {
    "shallow": "Z-SHALLOW",
    "invisible": "Z-OTHER-SIDE",
    "warm": "Z-WARM",
    "port_invisible": "P-SHALLOW-OTHER",
    "port_warm": "P-SHALLOW-WARM",
    "family_jia": "F-JIA",
    "family_lin": "F-LIN",
    "a": "P-A-JIA",
    "b": "P-B-YI",
    "sister": "P-SISTER-MEI",
    "c": "P-C-LIN",
    "dou": "P-DOU",
}


def seed_demo_world(svc: FamilyPoolService) -> Dict[str, Any]:
    """Seed three zones, five wristbands and mutual family permissions."""
    svc.create_family({"id": DEMO_IDS["family_jia"], "name": "甲家"})
    svc.create_family({"id": DEMO_IDS["family_lin"], "name": "林家"})

    svc.add_consent({
        "family_id": DEMO_IDS["family_jia"],
        "other_family_id": DEMO_IDS["family_lin"],
        "takeover_allowed": True,
        "borrow_allowed": True,
    })
    svc.add_consent({
        "family_id": DEMO_IDS["family_lin"],
        "other_family_id": DEMO_IDS["family_jia"],
        "takeover_allowed": True,
        "borrow_allowed": True,
    })

    svc.create_zone({"id": DEMO_IDS["shallow"], "name": "浅区", "sort_no": 1})
    svc.create_zone({"id": DEMO_IDS["invisible"], "name": "滑梯另一侧", "sort_no": 2})
    svc.create_zone({"id": DEMO_IDS["warm"], "name": "暖池区", "sort_no": 3})

    # A marked mouth exists, but shallow and slide-side are NOT visible.
    svc.create_port({
        "id": DEMO_IDS["port_invisible"],
        "name": "浅区—滑梯侧标明水口",
        "from_zone_id": DEMO_IDS["shallow"],
        "to_zone_id": DEMO_IDS["invisible"],
        "bidirectional": True,
    })
    svc.create_port({
        "id": DEMO_IDS["port_warm"],
        "name": "浅区—暖池水口",
        "from_zone_id": DEMO_IDS["shallow"],
        "to_zone_id": DEMO_IDS["warm"],
        "bidirectional": True,
    })
    # Explicit sight line, independent of the port graph.
    svc.add_visibility({
        "zone_a_id": DEMO_IDS["shallow"],
        "zone_b_id": DEMO_IDS["warm"],
    })

    svc.create_person({
        "id": DEMO_IDS["a"],
        "band_code": "WB-A",
        "display_name": "甲",
        "role": "adult",
        "family_id": DEMO_IDS["family_jia"],
        "care_capacity": 2,
    })
    svc.create_person({
        "id": DEMO_IDS["b"],
        "band_code": "WB-B",
        "display_name": "乙",
        "role": "adult",
        "family_id": DEMO_IDS["family_jia"],
        "care_capacity": 1,
    })
    svc.create_person({
        "id": DEMO_IDS["sister"],
        "band_code": "WB-SISTER",
        "display_name": "妹妹",
        "role": "child",
        "family_id": DEMO_IDS["family_jia"],
        "care_capacity": 0,
    })
    svc.create_person({
        "id": DEMO_IDS["c"],
        "band_code": "WB-C",
        "display_name": "林成人",
        "role": "adult",
        "family_id": DEMO_IDS["family_lin"],
        "care_capacity": 1,
    })
    svc.create_person({
        "id": DEMO_IDS["dou"],
        "band_code": "WB-DOU",
        "display_name": "小豆",
        "role": "child",
        "family_id": DEMO_IDS["family_lin"],
        "care_capacity": 0,
    })

    return {
        "seeded": True,
        "zones": 3,
        "wristbands": 5,
        "ids": DEMO_IDS,
        "operating_rule": (
            "乙的 care_capacity=1，因此 baseline 有一个空名额；"
            "若更早受理小豆，该唯一物理名额被占用，甲不能放行。"
        ),
    }
