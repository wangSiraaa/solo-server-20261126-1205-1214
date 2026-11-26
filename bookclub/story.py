"""A small fictional story with a fork, a turn point, and two endings.

Structure (six segments)::

    ch1  opening  ──┬── ch2a  "bridge, via the letter"   (甲支: the letter)
                    └── ch2b  "bridge, via the tape"     (乙支: the tape)
                          both disclose ``key_under_bridge``
                              │
                             ch3  turn point  (requires the key)
                              │
                     ┌────────┴────────┐
                    e1                e2
               "reunion"          "empty house"
             (both require the key)

Two branches reveal the *same* fact with the *same* wording ("the key is under
the bridge").  The same segment titles are reused deliberately: matching readers
by title would be wrong, matching by fact id is correct.
"""

from __future__ import annotations

from typing import List

from .store import Store

FACTS = {
    "key_under_bridge": "钥匙藏在桥下（the key is under the bridge）",
    "letter_name": "信封上的名字（a name written on the letter）",
    "tape_confession": "磁带里的忏悔（a confession on the tape）",
    "dawn_choice": "天亮前的决定（the choice before dawn）",
    "brother_alive": "哥哥仍活着（the brother is still alive）",
    "brother_gone": "哥哥早已离开（the brother had already gone）",
    "vault_code": "从未被任何人揭晓的保险箱密码（a code no segment ever reveals）",
}

# The six fictional segments of edition v1.
V1_SEGMENTS: List[dict] = [
    {
        "id": "ch1", "title": "一 · 雨夜归人",
        "text": ("雨下了整夜。阿禾拎着一只旧皮箱回到镇口，路灯把水洼照成碎玻璃。"
                 "有人说过，桥就在镇子东头，可没人告诉她，桥下究竟压着什么。"),
        "describes": ["ch2a", "ch2b"],
    },
    {
        "id": "ch2a", "title": "二 · 桥下（信）",
        "text": ("她在门缝里摸到一封信，邮票是十年前的。信纸泛黄，末尾一行字迹发抖："
                 "「钥匙藏在桥下。」落款是一个她以为早已不在人世的名字。"),
        "discloses": ["key_under_bridge", "letter_name"],
    },
    {
        "id": "ch2b", "title": "二 · 桥下（录音）",
        "text": ("录音机卡了一下，磁带转起来。沙沙声里，一个男人的声音低低地说："
                 "「钥匙藏在桥下，别问是谁放的。」那声音她认得，却不敢承认。"),
        "discloses": ["key_under_bridge", "tape_confession"],
    },
    {
        "id": "ch3", "title": "三 · 天亮之前",
        "text": ("她把钥匙攥在手心，坐到天亮。走哪一条路，得在天亮前决定。"),
        "requires": ["key_under_bridge"], "discloses": ["dawn_choice"],
    },
    {
        "id": "e1", "title": "结局 · 重逢",
        "text": ("门开了。哥哥站在门后，比记忆里老了许多，手里也是一把锈钥匙。"
                 "他们没有说话，只是把两把钥匙并在一起。"),
        "requires": ["key_under_bridge"], "discloses": ["brother_alive"],
    },
    {
        "id": "e2", "title": "结局 · 空屋",
        "text": ("门开了，屋里没有人。桌上留着一杯凉透的茶，和一张写着「对不起」的纸。"
                 "她把钥匙留在桌上，转身走进清晨。"),
        "requires": ["key_under_bridge"], "discloses": ["brother_gone"],
    },
]


# Edition v2: the tape's reveal moves one segment later (into ``ch2c``).  The
# letter branch ``ch2a`` is identical, and the editor declares it unchanged.
V2_SEGMENTS: List[dict] = [
    {"id": "ch1", "title": "一 · 雨夜归人",
     "text": "雨下了整夜。桥就在镇子东头。", "describes": ["ch2a", "ch2b", "ch2c"]},
    {"id": "ch2a", "title": "二 · 桥下（信）",
     "text": "信纸末尾：钥匙藏在桥下。",
     "discloses": ["key_under_bridge", "letter_name"]},
    {"id": "ch2b", "title": "二 · 桥下（录音）",
     "text": "磁带沙沙作响，只留下一句忏悔，没有再说别的。",
     "discloses": ["tape_confession"]},
    {"id": "ch2c", "title": "二 · 桥下（录音·续）",
     "text": "录音的第二面，男人才低声补上一句：钥匙藏在桥下。",
     "discloses": ["key_under_bridge"], "requires": ["tape_confession"]},
    {"id": "ch3", "title": "三 · 天亮之前",
     "text": "她把钥匙攥在手心，坐到天亮。",
     "requires": ["key_under_bridge"], "discloses": ["dawn_choice"]},
    {"id": "e1", "title": "结局 · 重逢",
     "text": "门开了。哥哥站在门后。",
     "requires": ["key_under_bridge"], "discloses": ["brother_alive"]},
    {"id": "e2", "title": "结局 · 空屋",
     "text": "门开了，屋里没有人。",
     "requires": ["key_under_bridge"], "discloses": ["brother_gone"]},
]


def build_v1(store: Store, work_id: str = "bridge", title: str = "桥下的钥匙") -> str:
    """Create facts, the work, and edition v1.  Returns the turn id."""
    for fid, desc in FACTS.items():
        store.define_fact(fid, desc)
    store.define_work(work_id, title)
    turn = store.publish_turn(work_id, "v1 · 初版", V1_SEGMENTS)
    return turn.id


def build_v2_moved(store: Store, work_id: str, revision_of: str) -> str:
    """Publish v2 (reveal of the tape branch moved one segment later)."""
    turn = store.publish_turn(
        work_id, "v2 · 录音的揭晓后移", V2_SEGMENTS,
        revision_of=revision_of,
        # ch2a (the letter) is unchanged.  ch2b (the recording) is *also*
        # declared unchanged because its confession still holds -- but the key
        # reveal moved out of it into ch2c, so declaring ch2b unchanged must NOT
        # smuggle that moved reveal across.  The segment-scoped carry check
        # enforces exactly that.
        unchanged_segments=["ch2a", "ch2b"],
        note="把录音里『钥匙在桥下』的揭晓移到后一段（ch2c）；忏悔本身未改。",
    )
    return turn.id
