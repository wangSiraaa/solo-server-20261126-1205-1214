"""Standard three-township world used by the demo and self-tests."""
from __future__ import annotations

try:
    from .service import LunshiService
except ImportError:  # pragma: no cover - direct script/tool compatibility
    from service import LunshiService  # type: ignore

CONVENTION = "LUN-SANXIANG"

DAYS = {
    1: "2026-11-30",  # 跨月：11 月的最后一天
    2: "2026-12-01",
    3: "2026-12-02",
    4: "2026-12-03",  # 甲的补集日
    5: "2026-12-04",  # 乙的备用/补轮日
}

CATEGORY_LABELS = {
    "book": "书摊",
    "umbrella": "修伞摊",
    "tea": "茶摊",
    "herb": "草药摊",
}


def seed_world(svc: LunshiService) -> None:
    # 三乡轮次序号：甲 1、乙 2、丙 3。
    svc.create_township("A", "甲乡", 1)
    svc.create_township("B", "乙乡", 2)
    svc.create_township("C", "丙乡", 3)

    # 各乡申报的可办集日子。故意不让所有难办集都堆在最后：
    # 甲 D1 先办，乙 D2 就要满足相连双摊，丙 D3 轮办。
    svc.declare_session("A", 1, 1, DAYS[1], "甲乡冬月头集")
    svc.declare_session("A", 4, 1, DAYS[4], "甲乡雨后补集")
    svc.declare_session("B", 2, 1, DAYS[2], "乙乡廊市")
    svc.declare_session("B", 5, 1, DAYS[5], "乙乡备用集")
    svc.declare_session("C", 3, 1, DAYS[3], "丙乡榕树下集")

    # 场内允许摊类。
    common_ab = ["book", "umbrella", "tea", "herb"]
    svc.add_spot("A1", "A", "甲一", ["book", "umbrella", "tea"], "老槐坪北侧")
    svc.add_spot("A2", "A", "甲二", ["book", "umbrella", "tea"], "老槐坪南侧")
    svc.add_spot("A3", "A", "甲三", ["tea", "herb"], "老槐坪茶棚")
    svc.set_spots_linked("A1", "A2", True)

    svc.add_spot("B1", "B", "乙一", common_ab, "老圩廊东格")
    svc.add_spot("B2", "B", "乙二", common_ab, "老圩廊中格")
    svc.add_spot("B3", "B", "乙三", common_ab, "老圩廊西格")
    svc.set_spots_linked("B1", "B2", True)
    # B3 is usable but deliberately not linked to the original B1/B2 pair.

    svc.add_spot("B9", "B", "借九", common_ab, "东坪备用借地格")

    svc.add_spot("C1", "C", "丙一", ["book", "umbrella", "herb"], "榕树头东格")
    svc.add_spot("C2", "C", "丙二", ["book", "umbrella", "herb"], "榕树头西格")
    svc.set_spots_linked("C1", "C2", True)

    # 开局约定：同一天要有分别真实开张的书摊、修伞摊。
    svc.open_convention(
        CONVENTION,
        "甲乙丙三乡轮市约",
        cycle=["A", "B", "C"],
        success_rule={
            "same_day": True,
            "same_slot": True,
            "distinct_vendors": True,
            "required_categories": ["book", "umbrella"],
        },
        method="dynamic_call",
        makeup_policy="同一轮不换轮次号；已受理/已召集而未办成者，下一合用日优先补集",
    )

    # 文墨和骨青都在甲乙两乡登记；系统仍强制同一时段只能在一处报开。
    svc.register_vendor("V-WEN", "文墨书摊", ["book"], ["A", "B", "C"], "文掌柜")
    svc.register_vendor("V-GU", "骨青修伞", ["umbrella"], ["A", "B", "C"], "骨师傅")
    svc.register_vendor("V-LAN", "兰亭旧书", ["book"], ["C"], "兰掌柜")
    svc.register_vendor("V-JIANG", "江记修伞", ["umbrella"], ["C"], "江师傅")
    svc.register_vendor("V-SHAN", "山药篮子", ["herb"], ["A", "B"], "山婶")
    svc.register_vendor("V-CHA", "半山茶摊", ["tea"], ["A", "B"], "茶伯")
    svc.set_today(1)
