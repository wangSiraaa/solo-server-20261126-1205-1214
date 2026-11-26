"""领域对象与异常。

核心物理事实（两种暗房都必须遵守）：
1. 一张纸被划成很小的格子 (row, col)，每一拍底片向每个格子透过一个整数光量；
2. 光只能累加，不能擦掉 —— 每格的累计光量单调不减；
3. 阶梯样卡把"累计光量区间"映射到顾客肉眼可区分的色阶：
   阶 k 覆盖 [lower_k, lower_{k+1})，最后一阶向上无界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple


class LabError(Exception):
    """业务错误基类。"""

    status = 400


class ValidationError(LabError):
    """输入不合法。"""

    status = 422


class NotFoundError(LabError):
    """订单/纸/批次不存在。"""

    status = 404


class ConflictError(LabError):
    """状态冲突：纸已曝光、批次已开工、回条迟到等。"""

    status = 409


Cell = Tuple[int, int]


def cell_key(cell: Cell) -> str:
    """格子的稳定字符串键，用作 JSON / SQLite 键。"""
    r, c = cell
    return f"{r},{c}"


def parse_cell(key: str) -> Cell:
    try:
        r, c = key.split(",")
        return int(r), int(c)
    except (ValueError, AttributeError):
        raise ValidationError(f"非法格子坐标: {key!r}，应为 'row,col'")


@dataclass(frozen=True)
class StepLevel:
    """一个肉眼色阶：名称 name，覆盖 [lower, next_lower) 的累计光量。"""

    name: str
    lower: int


class StepCard:
    """阶梯样卡：累计光量区间 -> 色阶。

    levels 按 lower 升序；level i 的区间是 [lower_i, lower_{i+1})，
    最后一阶上无界。例如 [(阶二, 2), (阶三, 3)]：
      光量 0,1 不落在任何登记色阶（未入阶）；2 属于阶二；3+ 属于阶三。
    """

    def __init__(self, levels: Iterable[StepLevel]):
        ls = sorted(levels, key=lambda x: x.lower)
        if not ls:
            raise ValidationError("样卡至少需要一个色阶")
        if len({lv.lower for lv in ls}) != len(ls):
            raise ValidationError("样卡各色阶的起始光量不得重复")
        if any(lv.lower < 0 for lv in ls):
            raise ValidationError("色阶起始光量不能为负")
        self.levels = tuple(ls)

    @classmethod
    def from_pairs(cls, pairs: Iterable[Tuple[str, int]]) -> "StepCard":
        return cls(StepLevel(name=n, lower=v) for n, v in pairs)

    def level_of(self, light: int) -> StepLevel | None:
        """累计光量 light 落在哪一阶；低于最低起始量时返回 None（未入阶）。"""
        result: StepLevel | None = None
        for lv in self.levels:
            if light >= lv.lower:
                result = lv
            else:
                break
        return result

    def level_by_name(self, name: str) -> StepLevel:
        for lv in self.levels:
            if lv.name == name:
                return lv
        raise ValidationError(f"样卡中没有色阶 {name!r}")

    def lights_in_level(self, name: str, cap: int) -> list[int]:
        """该色阶覆盖的、不超过 cap 的全部整数光量。"""
        lv = self.level_by_name(name)
        idx = self.levels.index(lv)
        upper = self.levels[idx + 1].lower if idx + 1 < len(self.levels) else cap + 1
        return [x for x in range(lv.lower, min(upper, cap + 1))]

    def as_dict(self) -> list[dict]:
        return [{"name": lv.name, "lower": lv.lower} for lv in self.levels]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StepCard) and other.levels == self.levels
