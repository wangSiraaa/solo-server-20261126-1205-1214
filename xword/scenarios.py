"""Ready-made tiny boards.

The canonical board from the brief:

        山 水        <- "shan_shui" (H)  answer 山水
           车        <- "shui_che"  (V)  answer 水车

Three physical cells: 山(1,0), 水(1,1), 车(2,1).  水 is the crossing cell and
is an endpoint of *both* words, but it is one physical cell and can be earned
at most once.  Every endpoint cell is worth 1 point.
"""

from __future__ import annotations

from typing import Any, Dict


def shan_shui_board(mode: str = "race", **overrides: Any) -> Dict[str, Any]:
    spec: Dict[str, Any] = {
        "id": "shan-shui-2x2",
        "name": "山水·水车",
        "mode": mode,
        "disclosure": "conceal" if mode == "sealed" else "open",
        "entries": [
            {"id": "shan_shui", "answer": "山水", "direction": "H",
             "clue": "山与水（横读）",
             "cells": [[1, 0], [1, 1]]},
            {"id": "shui_che", "answer": "水车", "direction": "V",
             "clue": "引水的车（竖读）",
             "cells": [[1, 1], [2, 1]]},
        ],
        # every independent (endpoint) cell is worth one point
        "score_table": {"shan_shui": 1, "shui_che": 1},
    }
    spec.update(overrides)
    return spec


def four_entry_board(mode: str = "race", **overrides: Any) -> Dict[str, Any]:
    """Four entries (the ceiling), two consistent crossings, two clusters.

        春 风          <- chun_feng (H) 春风
           光          <- feng_guang (V) 风光
        秋 月          <- qiu_yue (H) 秋月
           夜          <- yue_ye (V) 月夜

    Shared cells: (0,1) is 风 in both 春风/风光; (2,1) is 月 in both 秋月/月夜.
    """
    spec: Dict[str, Any] = {
        "id": "spring-autumn-4",
        "name": "四词样例盘",
        "mode": mode,
        "disclosure": "conceal" if mode == "sealed" else "open",
        "entries": [
            {"id": "chun_feng", "answer": "春风", "direction": "H",
             "clue": "春天的风", "cells": [[0, 0], [0, 1]]},
            {"id": "feng_guang", "answer": "风光", "direction": "V",
             "clue": "风景", "cells": [[0, 1], [1, 1]]},
            {"id": "qiu_yue", "answer": "秋月", "direction": "H",
             "clue": "秋天的月", "cells": [[2, 0], [2, 1]]},
            {"id": "yue_ye", "answer": "月夜", "direction": "V",
             "clue": "有月的夜", "cells": [[2, 1], [3, 1]]},
        ],
        "score_table": {"chun_feng": 1, "feng_guang": 1,
                        "qiu_yue": 1, "yue_ye": 1},
    }
    spec.update(overrides)
    return spec
