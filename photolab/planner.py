"""固定遮片排程器（纯函数，不碰数据库）。

固定遮片暗房的规则：
- 开批前按有限片材预算刻制若干"通光格形状"（遮片上开孔的格子集合），批次内重复使用；
- 开工后不再补刻；一张片能翻动到哪些位置，全部以备料登记为准；
- 每一拍选一张已登记遮片、放到已登记偏移位置，开孔格透光、其余格不动；
- 光量只累加。排程器要回答：在片材/拍数预算内，是否存在一组拍法，
  让每格的"最终累计光量"落在顾客接受的色阶里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Optional

from .domain import Cell, StepCard, ValidationError

# 枚举上限：格子数过多时不再枚举全部子集，改用受限形状族（单格/整行/整列/整版/相邻对）
MAX_FULL_SUBSET_CELLS = 10
# 单纸保留的可行方案数上限（去重后）
MAX_VARIANTS = 200


@dataclass(frozen=True)
class Placement:
    """一张遮片在纸上的一个可登记位置。"""

    shape: tuple[Cell, ...]          # 遮片通光格的规范形状（相对坐标）
    offset: Cell                     # 翻动偏移 (dr, dc)
    openings: frozenset[Cell]        # 该位置实际开孔的绝对格子

    def key(self) -> tuple:
        return (self.shape, self.offset)


@dataclass
class ShotSpec:
    """洗印单上的一拍。"""

    mask_shape: tuple[Cell, ...]
    offset: Cell
    exposures: dict[str, int]        # 本拍透光格 -> 透进光量

    def multiset_key(self) -> tuple:
        return (self.mask_shape, self.offset)


@dataclass
class PaperPlan:
    paper_id: str
    shots: list[ShotSpec]
    final_lights: dict[str, int]
    final_levels: dict[str, Optional[str]]


@dataclass
class BatchPlan:
    feasible: bool
    reason: Optional[str] = None
    masks: list[tuple[Cell, ...]] = field(default_factory=list)   # 需要刻制的去重形状
    sheets_used: int = 0
    shot_total: int = 0
    paper_plans: list[PaperPlan] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "reason": self.reason,
            "sheets_used": self.sheets_used,
            "shot_total": self.shot_total,
            "masks": [[list(c) for c in shape] for shape in self.masks],
            "papers": [
                {
                    "paper_id": paper_plan.paper_id,
                    "shots": [
                        {
                            "mask": [list(c) for c in s.mask_shape],
                            "offset": list(s.offset),
                            "exposures": s.exposures,
                        }
                        for s in paper_plan.shots
                    ],
                    "final_lights": paper_plan.final_lights,
                    "final_levels": paper_plan.final_levels,
                }
                for paper_plan in self.paper_plans
            ],
        }


def _norm(shape: frozenset[Cell]) -> tuple[Cell, ...]:
    """形状规范化：平移到以 (0,0) 为左上角，便于同形去重。"""
    mr = min(r for r, _ in shape)
    mc = min(c for _, c in shape)
    return tuple(sorted((r - mr, c - mc) for r, c in shape))


def candidate_shapes(rows: int, cols: int) -> list[tuple[Cell, ...]]:
    """枚举开批前可刻的通光格形状（非空子集）。"""
    cells = [(r, c) for r in range(rows) for c in range(cols)]
    shapes: set[tuple[Cell, ...]] = set()
    if rows * cols <= MAX_FULL_SUBSET_CELLS:
        for size in range(1, len(cells) + 1):
            for combo in combinations(cells, size):
                shapes.add(_norm(frozenset(combo)))
    else:
        # 大格子：受限形状族，仍覆盖单格遮挡、整行/整列压暗、整版通光等常见手法
        shapes.add(_norm(frozenset(cells)))
        for cell in cells:
            shapes.add(_norm(frozenset([cell])))
        for r in range(rows):
            shapes.add(_norm(frozenset((r, c) for c in range(cols))))
        for c in range(cols):
            shapes.add(_norm(frozenset((r, c) for r in range(rows))))
        for r in range(rows):
            for c in range(cols - 1):
                shapes.add(_norm(frozenset([(r, c), (r, c + 1)])))
    return sorted(shapes)


def placements_for(shape: tuple[Cell, ...], rows: int, cols: int) -> list[Placement]:
    """一张刻好的遮片能翻动登记到哪些位置（开孔必须全部落在纸面内）。"""
    max_r = max(r for r, _ in shape)
    max_c = max(c for _, c in shape)
    out: list[Placement] = []
    # 偏移上界 = 行数-形状高度；range 上界是开区间，故 +1
    for dr in range(rows - max_r):
        for dc in range(cols - max_c):
            openings = frozenset((r + dr, c + dc) for r, c in shape)
            out.append(Placement(shape=shape, offset=(dr, dc), openings=openings))
    return out


def _allowed_finals(
    cell: Cell,
    role: str,
    target_level: str,
    start: int,
    card: StepCard,
    cap: int,
) -> list[int]:
    """求一格被允许的最终累计光量集合（光只能累加，故隐式要求 >= start）。

    脸（必须保住）：最终光量只能落在指定色阶自己的区间内，不接受滑到更深阶；
    背景（可以加深）：至少达到目标阶起始量，目标阶及其更深的光量都接受。
    末阶上无界时以 cap（起点 + 拍数预算）封顶。
    """
    target = card.level_by_name(target_level)
    idx = card.levels.index(target)
    if role == "face":
        return sorted(set(card.lights_in_level(target_level, cap)))
    if role != "background":
        raise ValidationError(f"格子 {cell} 的角色只能是 face/background，收到 {role!r}")
    return [x for x in range(target.lower, cap + 1)]


def diagnose(
    rows: int,
    cols: int,
    start_lights: dict[Cell, int],
    requirements: list[dict],
    card: StepCard,
    shot_budget: int,
    beam_per_cell: Optional[dict[Cell, int]] = None,
) -> Optional[str]:
    """在排程前给出"肉眼即可判断"的拒收理由（如脸已经过阶）。"""
    beam_per_cell = beam_per_cell or {}
    for req in requirements:
        cell = req["cell"]
        start = start_lights.get(cell, 0)
        beam = beam_per_cell.get(cell, 1)
        cap = start + shot_budget * beam
        allowed = _allowed_finals(
            cell, req["role"], req["level"], start, card, cap
        )
        current = card.level_of(start)
        target = card.level_by_name(req["level"])
        label = "脸" if req["role"] == "face" else "背景"
        # 拍数预算内连目标阶下限都够不到（允许集为空）
        if cap < target.lower:
            return (
                f"格 {cell} {label}当前累计光量 {start}，{shot_budget} 拍后最多 {cap}，"
                f"到不了 {req['level']} 的起始光量 {target.lower}"
            )
        # 光只能累加：起点已高于本格允许的最高最终光量，才是硬性不可行
        if start > max(allowed):
            return (
                f"格 {cell} {label}当前累计光量 {start}（色阶 "
                f"{current.name if current else '未入阶'}），目标阶 {req['level']}；"
                "已吃进去的光不能擦掉，无法回到目标阶"
            )
    return None


def plan_paper_variants(
    paper_id: str,
    rows: int,
    cols: int,
    start_lights: dict[Cell, int],
    beam_per_cell: dict[Cell, int],
    requirements: list[dict],
    card: StepCard,
    shot_budget: int,
) -> list[PaperPlan]:
    """枚举一张纸在拍数预算内的全部去重可行拍法。"""
    def as_cell(key) -> Cell:
        if isinstance(key, str):
            r, c = key.split(",")
            return int(r), int(c)
        return tuple(key)

    start_lights = {as_cell(k): v for k, v in start_lights.items()}
    beam_per_cell = {as_cell(k): v for k, v in beam_per_cell.items()}
    requirements = [{**req, "cell": as_cell(req["cell"])} for req in requirements]

    reason = diagnose(rows, cols, start_lights, requirements, card,
                      shot_budget, beam_per_cell)
    if reason:
        return []

    # 每格允许的最终光量；未圈选的格子不设约束
    allowed: dict[Cell, set[int]] = {}
    for req in requirements:
        cell = req["cell"]
        start = start_lights.get(cell, 0)
        beam = beam_per_cell.get(cell, 1)
        allowed[cell] = set(
            _allowed_finals(cell, req["role"], req["level"], start, card,
                            start + shot_budget * beam)
        )

    placements: list[Placement] = []
    for shape in candidate_shapes(rows, cols):
        placements.extend(placements_for(shape, rows, cols))

    # 逐拍深度优先搜索：每拍选一个已登记位置，或直接收工。
    # 用多重集签名去重（同一位置集合的不同排列只算一种操作方案）。
    lights = dict(start_lights)
    chosen: list[Placement] = []
    seen: set[tuple] = set()
    results: list[PaperPlan] = []
    marked = list(allowed.keys())

    def reachable_ceiling(cell: Cell, shots_left: int) -> int:
        """剩余拍数内，单格最多还能加多少光（每拍取该格可被照到的最大透光量）。"""
        beam = beam_per_cell.get(cell, 1)
        return shots_left * beam

    def still_possible(shots_left: int) -> bool:
        for cell in marked:
            cur = lights.get(cell, 0)
            if cur > max(allowed[cell]):
                return False
            if cur + reachable_ceiling(cell, shots_left) < min(allowed[cell]):
                return False
        return True

    def emit() -> None:
        # 多重集签名：同一位置组合的不同排列只算一种操作方案
        counts_by_key: dict[tuple, int] = {}
        for p in chosen:
            counts_by_key[p.key()] = counts_by_key.get(p.key(), 0) + 1
        sig = tuple(sorted(counts_by_key.items()))
        if sig in seen:
            return
        seen.add(sig)
        shots = [
            ShotSpec(
                mask_shape=p.shape,
                offset=p.offset,
                exposures={
                    f"{r},{c}": beam_per_cell.get((r, c), 1)
                    for r, c in sorted(p.openings)
                },
            )
            for p in chosen
        ]
        final_levels = {
            f"{r},{c}": (lv.name if (lv := card.level_of(v)) else None)
            for (r, c), v in sorted(lights.items())
        }
        results.append(
            PaperPlan(
                paper_id=paper_id,
                shots=shots,
                final_lights={f"{r},{c}": v for (r, c), v in sorted(lights.items())},
                final_levels=final_levels,
            )
        )

    def recurse(shots_left: int) -> None:
        if len(results) >= MAX_VARIANTS:
            return
        # 收工：所有圈选格都落在允许光量内即成方案
        if all(lights.get(cell, 0) in allowed[cell] for cell in marked):
            emit()
        if shots_left == 0:
            return
        if not still_possible(shots_left):
            return
        for p in placements:
            beam = {cell: beam_per_cell.get(cell, 1) for cell in p.openings}
            # 本拍会把某格顶出允许上界，跳过
            if any(lights.get(cell, 0) + amount > max(allowed[cell])
                   for cell, amount in beam.items() if cell in allowed):
                continue
            for cell, amount in beam.items():
                lights[cell] = lights.get(cell, 0) + amount
            chosen.append(p)
            recurse(shots_left - 1)
            chosen.pop()
            for cell, amount in beam.items():
                lights[cell] -= amount
                if lights[cell] == 0 and cell not in start_lights:
                    del lights[cell]

    recurse(shot_budget)
    # 拍数少的方案优先（师傅操作最短）
    results.sort(key=lambda pp: len(pp.shots))
    return results


def plan_batch(
    papers: list[dict],
    requirements_by_paper: dict[str, list[dict]],
    card: StepCard,
    sheet_budget: int,
    shot_budget: int,
) -> BatchPlan:
    """整批排程：逐纸求可行拍法，合并去重遮片形状，核对片材/拍数预算。

    papers: [{id, rows, cols, start_lights: {cell: int}, beam_per_cell: {cell: int}}]
    """
    chosen: list[PaperPlan] = []
    total_shots = 0
    all_shapes: set[tuple[Cell, ...]] = set()

    for paper in papers:
        pid = paper["id"]
        reqs = [
            {**req, "cell": tuple(req["cell"])}
            for req in requirements_by_paper.get(pid, [])
        ]

        def as_cell(key):
            if isinstance(key, str):
                r, c = key.split(",")
                return int(r), int(c)
            return tuple(key)

        starts = {as_cell(k): v for k, v in paper["start_lights"].items()}
        beams = {as_cell(k): v for k, v in paper.get("beam_per_cell", {}).items()}
        variants = plan_paper_variants(
            pid, paper["rows"], paper["cols"], starts, beams, reqs, card, shot_budget
        )
        if not variants:
            reason = diagnose(paper["rows"], paper["cols"], starts, reqs, card,
                              shot_budget, beams)
            return BatchPlan(
                feasible=False,
                reason=reason or f"纸 {pid} 在 {shot_budget} 拍预算内找不到可行遮片拍法",
            )
        # 优先省片材、其次省拍数
        best = min(variants, key=lambda pp: (len({s.mask_shape for s in pp.shots}),
                                             len(pp.shots)))
        chosen.append(best)
        total_shots += len(best.shots)
        all_shapes.update(s.mask_shape for s in best.shots)

    if len(all_shapes) > sheet_budget:
        return BatchPlan(
            feasible=False,
            reason=(
                f"完成整批需刻制 {len(all_shapes)} 张遮片，"
                f"超过片材预算 {sheet_budget} 张"
            ),
        )
    if total_shots > shot_budget:
        return BatchPlan(
            feasible=False,
            reason=f"整批需 {total_shots} 拍，超过现场拍数预算 {shot_budget}",
        )

    ordered_masks = sorted(all_shapes)
    return BatchPlan(
        feasible=True,
        masks=ordered_masks,
        sheets_used=len(ordered_masks),
        shot_total=total_shots,
        paper_plans=chosen,
    )
