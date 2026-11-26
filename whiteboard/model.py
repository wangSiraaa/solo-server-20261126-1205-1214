"""课堂涂格白板的核心领域逻辑。

本模块不依赖 HTTP 或持久化，方便单独测试。
坐标一律使用从零开始的 ``[row, col]``；挡线用两端坐标生成的规范 edge key 表示。
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from itertools import permutations
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import re

MAX_ROWS = 5
MAX_COLS = 5
MAX_DRAFTS_PER_SESSION = 6
MAX_STEPS_PER_DRAFT = 4

CELL_RE = re.compile(r"^(-?\d+)\s*,\s*(-?\d+)$")


class WhiteboardError(ValueError):
    """输入不合法或当前状态不允许该操作。"""


class NotFoundError(KeyError):
    """请求的资源不存在。"""


# ---------------------------------------------------------------------------
# 坐标与棋盘
# ---------------------------------------------------------------------------

def cell_key(cell: Sequence[Any] | str | Dict[str, Any]) -> str:
    r, c = normalize_cell(cell)
    return f"{r},{c}"


def normalize_cell(cell: Sequence[Any] | str | Dict[str, Any]) -> Tuple[int, int]:
    if isinstance(cell, dict):
        if "cell" in cell:
            return normalize_cell(cell["cell"])
        if "at" in cell:
            return normalize_cell(cell["at"])
        if "r" in cell and "c" in cell:
            return int(cell["r"]), int(cell["c"])
        if "row" in cell and "col" in cell:
            return int(cell["row"]), int(cell["col"])
        raise WhiteboardError(f"无法识别格坐标: {cell!r}")
    if isinstance(cell, str):
        m = CELL_RE.match(cell.strip())
        if not m:
            raise WhiteboardError(f"格坐标应为 'row,col': {cell!r}")
        return int(m.group(1)), int(m.group(2))
    if isinstance(cell, (Sequence, tuple)) and len(cell) == 2:
        return int(cell[0]), int(cell[1])
    raise WhiteboardError(f"无法识别格坐标: {cell!r}")


def edge_key(a: Any, b: Any) -> str:
    ka = cell_key(a)
    kb = cell_key(b)
    ra, ca = normalize_cell(a)
    rb, cb = normalize_cell(b)
    if (ra, ca) <= (rb, cb):
        return f"{ka}|{kb}"
    return f"{kb}|{ka}"


def parse_edge(edge: Any) -> Tuple[Tuple[int, int], Tuple[int, int], str]:
    if isinstance(edge, dict):
        if "edge" in edge:
            return parse_edge(edge["edge"])
        for alias in ("between", "endpoints", "adjacent", "cells", "pair"):
            if alias in edge:
                return parse_edge(edge[alias])
        if "a" in edge and "b" in edge:
            a, b = normalize_cell(edge["a"]), normalize_cell(edge["b"])
        elif "from" in edge and "to" in edge:
            a, b = normalize_cell(edge["from"]), normalize_cell(edge["to"])
        else:
            raise WhiteboardError(f"无法识别挡线: {edge!r}")
    elif isinstance(edge, str):
        parts = edge.split("|")
        if len(parts) != 2:
            raise WhiteboardError(f"挡线应为 'r,c|r,c': {edge!r}")
        a, b = normalize_cell(parts[0]), normalize_cell(parts[1])
    elif isinstance(edge, Sequence) and len(edge) == 2:
        # 两种常见写法：[[r,c],[r,c]] 或 [[[r,c],[r,c]]] 由上层解包。
        a, b = normalize_cell(edge[0]), normalize_cell(edge[1])  # type: ignore[index]
    else:
        raise WhiteboardError(f"无法识别挡线: {edge!r}")
    return a, b, edge_key(a, b)


def adjacent_edges(rows: int, cols: int) -> Iterable[Tuple[Tuple[int, int], Tuple[int, int], str]]:
    for r in range(rows):
        for c in range(cols):
            if c + 1 < cols:
                a, b = (r, c), (r, c + 1)
                yield a, b, edge_key(a, b)
            if r + 1 < rows:
                a, b = (r, c), (r + 1, c)
                yield a, b, edge_key(a, b)


def validate_dimensions(rows: int, cols: int) -> None:
    if not isinstance(rows, int) or not isinstance(cols, int):
        raise WhiteboardError("rows/cols 必须是整数")
    if rows < 1 or cols < 1:
        raise WhiteboardError("棋盘至少为 1x1")
    if rows > MAX_ROWS or cols > MAX_COLS:
        raise WhiteboardError(f"棋盘不超过 {MAX_ROWS}x{MAX_COLS}")


def make_board(
    rows: int,
    cols: int,
    base_color: str = "white",
    walls: Optional[Iterable[Any] | Dict[str, bool]] = None,
) -> Dict[str, Any]:
    validate_dimensions(rows, cols)
    board: Dict[str, Any] = {
        "rows": rows,
        "cols": cols,
        "base_color": base_color,
        "cells": [[base_color for _ in range(cols)] for _ in range(rows)],
        "walls": {},
    }
    for _a, _b, key in adjacent_edges(rows, cols):
        board["walls"][key] = False

    if walls is None:
        wall_items: Iterable[Any] = []
    elif isinstance(walls, dict):
        for edge, has_wall in walls.items():
            if has_wall:
                set_wall(board, edge, None, bool(has_wall))  # type: ignore[arg-type]
        wall_items = []
    else:
        wall_items = walls

    for item in wall_items:
        if isinstance(item, dict) and ("edge" in item or "a" in item or "from" in item):
            has_wall = item.get("wall", item.get("has_wall", True))
            set_wall(board, item.get("edge", item), None, bool(has_wall))
        else:
            # 简写：edge 本身，或 [edge, bool]
            if (
                isinstance(item, Sequence)
                and len(item) == 2
                and not isinstance(item[0], (str, int))
            ):
                # 可能是 [a, b]
                try:
                    _a, _b, key = parse_edge(item)  # type: ignore[arg-type]
                    set_wall(board, None, None, True, key=key)
                    continue
                except WhiteboardError:
                    pass
            if isinstance(item, Sequence) and len(item) == 2 and isinstance(item[1], bool):
                set_wall(board, item[0], None, item[1])  # type: ignore[index]
            else:
                set_wall(board, item, None, True)  # type: ignore[arg-type]
    return board


def board_in_bounds(board: Dict[str, Any], cell: Tuple[int, int]) -> bool:
    r, c = cell
    return 0 <= r < board["rows"] and 0 <= c < board["cols"]


def assert_cell(board: Dict[str, Any], cell: Any) -> Tuple[int, int]:
    rc = normalize_cell(cell)
    if not board_in_bounds(board, rc):
        raise WhiteboardError(f"格 {list(rc)} 超出 {board['rows']}x{board['cols']} 棋盘")
    return rc


def set_wall(
    board: Dict[str, Any],
    a: Any = None,
    b: Any = None,
    has_wall: bool = True,
    key: Optional[str] = None,
) -> str:
    if key is None:
        if b is None:
            _a, _b, key = parse_edge(a)
        else:
            aa, bb = normalize_cell(a), normalize_cell(b)
            key = edge_key(aa, bb)
            _a, _b = aa, bb
    else:
        parsed_a, parsed_b, parsed_key = parse_edge(key)
        if parsed_key != key:
            key = parsed_key
        _a, _b = parsed_a, parsed_b

    if not board_in_bounds(board, _a) or not board_in_bounds(board, _b):
        raise WhiteboardError(f"挡线端点越界: {key}")
    if abs(_a[0] - _b[0]) + abs(_a[1] - _b[1]) != 1:
        raise WhiteboardError(f"只能在共边相邻格之间设置挡线: {key}")
    if key not in board["walls"]:
        board["walls"][key] = bool(has_wall)
    else:
        board["walls"][key] = bool(has_wall)
    return key


def has_wall_between(board: Dict[str, Any], a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    key = edge_key(a, b)
    return bool(board["walls"].get(key, False))


def clone_board(board: Dict[str, Any]) -> Dict[str, Any]:
    return deepcopy(board)


# ---------------------------------------------------------------------------
# 操作与涂色扩散
# ---------------------------------------------------------------------------

def _neighbors(board: Dict[str, Any], r: int, c: int) -> Iterable[Tuple[int, int]]:
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nr, nc = r + dr, c + dc
        if 0 <= nr < board["rows"] and 0 <= nc < board["cols"]:
            if not has_wall_between(board, (r, c), (nr, nc)):
                yield nr, nc


def paint_region(board: Dict[str, Any], at: Any, color: str) -> Dict[str, Any]:
    """从 at 起，把可通行且等于起点原色的连通格涂成 color。"""
    if not isinstance(color, str) or not color.strip():
        raise WhiteboardError("颜色必须是非空字符串")
    color = color.strip()
    r, c = assert_cell(board, at)
    origin_color = board["cells"][r][c]

    component: List[Tuple[int, int]] = []
    if origin_color == color:
        return {
            "op": "paint",
            "at": [r, c],
            "color": color,
            "from_color": origin_color,
            "painted": [],
            "component": [],
            "changed": False,
        }

    seen = {(r, c)}
    queue = deque([(r, c)])
    while queue:
        cr, cc = queue.popleft()
        component.append((cr, cc))
        for nr, nc in _neighbors(board, cr, cc):
            if (nr, nc) in seen:
                continue
            # 只沿仍是起点原色的格扩散。
            if board["cells"][nr][nc] == origin_color:
                seen.add((nr, nc))
                queue.append((nr, nc))

    for pr, pc in component:
        board["cells"][pr][pc] = color

    return {
        "op": "paint",
        "at": [r, c],
        "color": color,
        "from_color": origin_color,
        "painted": [[pr, pc] for pr, pc in component],
        # component 与 painted 相同；保留别名方便不同阅读习惯。
        "component": [[pr, pc] for pr, pc in component],
        "changed": True,
    }


def apply_step(board: Dict[str, Any], step: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(step, dict):
        raise WhiteboardError(f"操作必须是对象: {step!r}")
    op = step.get("op") or step.get("type") or step.get("action")
    if op in {"paint", "fill", "color", "涂色"}:
        at = step.get("at", step.get("cell", step.get("start")))
        color = step.get("color", step.get("colour"))
        return paint_region(board, at, color)

    if op in {"set_wall", "wall", "set_edge", "挡线"}:
        has_wall = bool(step.get("wall", step.get("has_wall", True)))
        edge = step.get("edge", step.get("edge_key", step))
        if edge is step and "a" not in step and "from" not in step:
            edge = step.get("edge")
        key = set_wall(board, edge, step.get("b"), has_wall)
        old = not has_wall  # 仅用于事件；真实 old 由调用前快照 diff 更可靠。
        return {"op": "set_wall", "edge": key, "wall": has_wall, "old_assumed": old}

    if op in {"add_wall", "block", "加挡线"}:
        edge = step.get("edge", step)
        key = set_wall(board, edge, step.get("b"), True)
        return {"op": "set_wall", "edge": key, "wall": True}

    if op in {"remove_wall", "open", "unblock", "去挡线", "开缺口"}:
        edge = step.get("edge", step)
        key = set_wall(board, edge, step.get("b"), False)
        return {"op": "set_wall", "edge": key, "wall": False}

    if op in {"toggle_wall"}:
        edge = step.get("edge", step)
        _a, _b, key = parse_edge(edge)
        new_value = not board["walls"].get(key, False)
        set_wall(board, key=key, has_wall=new_value)
        return {"op": "set_wall", "edge": key, "wall": new_value}

    raise WhiteboardError(f"未知操作类型: {op!r}")


def simulate(initial_board: Dict[str, Any], steps: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if len(steps) > MAX_STEPS_PER_DRAFT:
        raise WhiteboardError(f"每稿至多 {MAX_STEPS_PER_DRAFT} 步，收到 {len(steps)} 步")
    board = clone_board(initial_board)
    trace: List[Dict[str, Any]] = []
    for index, step in enumerate(steps):
        try:
            event = apply_step(board, step)
            event["step_index"] = index
            trace.append(event)
        except WhiteboardError as exc:
            raise WhiteboardError(f"第 {index + 1} 步不合法: {exc}") from exc
    return {"board": board, "trace": trace}


# ---------------------------------------------------------------------------
# Diff / 贴图
# ---------------------------------------------------------------------------

def diff_boards(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    cell_changes: Dict[str, Dict[str, Any]] = {}
    for r in range(before["rows"]):
        for c in range(before["cols"]):
            old = before["cells"][r][c]
            new = after["cells"][r][c]
            if old != new:
                cell_changes[cell_key((r, c))] = {"from": old, "to": new}

    wall_changes: Dict[str, Dict[str, Any]] = {}
    for key in sorted(set(before.get("walls", {})) | set(after.get("walls", {}))):
        old = before.get("walls", {}).get(key, False)
        new = after.get("walls", {}).get(key, False)
        if old != new:
            wall_changes[key] = {"from": old, "to": new}

    return {"cells": cell_changes, "walls": wall_changes}


def apply_patch(board: Dict[str, Any], patch: Dict[str, Any]) -> None:
    for key, change in (patch.get("cells") or {}).items():
        r, c = normalize_cell(key)
        assert_cell(board, (r, c))
        board["cells"][r][c] = change["to"] if isinstance(change, dict) else change
    for key, change in (patch.get("walls") or {}).items():
        value = change["to"] if isinstance(change, dict) else change
        set_wall(board, key=key, has_wall=bool(value))


# ---------------------------------------------------------------------------
# 教案约束
# ---------------------------------------------------------------------------

def _rule_cells(rule: Dict[str, Any]) -> List[Tuple[int, int]]:
    raw_cells: List[Any] = []
    if "cell" in rule:
        raw_cells.append(rule["cell"])
    if "at" in rule:
        raw_cells.append(rule["at"])
    if "cells" in rule:
        raw_cells.extend(rule["cells"] if isinstance(rule["cells"], list) else [rule["cells"]])
    return [normalize_cell(x) for x in raw_cells]


def _rule_matches(rule: Dict[str, Any], r: int, c: int, color: Optional[str] = None) -> bool:
    cells = _rule_cells(rule)
    if cells and (r, c) not in cells:
        return False
    if color is None:
        return True
    banned_color = rule.get("color", rule.get("colour", "*"))
    if banned_color in {"*", "any", None}:
        return True
    return banned_color == color


def validate_final(board: Dict[str, Any], lesson: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    lesson = lesson or {}
    violations: List[Dict[str, Any]] = []

    for i, rule in enumerate(lesson.get("must_keep", []) or []):
        required = rule.get("color", rule.get("colour", board.get("base_color", "white")))
        for r, c in _rule_cells(rule):
            if not board_in_bounds(board, (r, c)):
                continue
            actual = board["cells"][r][c]
            if actual != required:
                violations.append(
                    {
                        "kind": "must_keep",
                        "rule_index": i,
                        "cell": [r, c],
                        "required": required,
                        "actual": actual,
                        "message": f"必留格 {r},{c} 应为 {required}，实际为 {actual}",
                    }
                )

    for i, rule in enumerate(lesson.get("forbidden", lesson.get("forbidden_paint", [])) or []):
        banned = rule.get("color", rule.get("colour", "*"))
        for r, c in _rule_cells(rule):
            if not board_in_bounds(board, (r, c)):
                continue
            actual = board["cells"][r][c]
            if banned in {"*", "any", None}:
                bad = actual != board.get("base_color", "white")
                expected_desc = "教案底色"
            else:
                bad = actual == banned
                expected_desc = f"颜色 {banned}"
            if bad:
                violations.append(
                    {
                        "kind": "forbidden_paint",
                        "rule_index": i,
                        "cell": [r, c],
                        "forbidden": banned,
                        "actual": actual,
                        "message": f"禁涂格 {r},{c} 不得出现{expected_desc}，实际为 {actual}",
                    }
                )
    return violations


def validate_trace(
    trace: Sequence[Dict[str, Any]],
    lesson: Optional[Dict[str, Any]],
    base_color: str = "white",
) -> List[Dict[str, Any]]:
    """检查“禁涂”是否在重演过程中被画笔触发过。"""
    lesson = lesson or {}
    violations: List[Dict[str, Any]] = []
    forbidden_rules = lesson.get("forbidden", lesson.get("forbidden_paint", [])) or []
    keep_rules = lesson.get("must_keep", []) or []

    for event in trace:
        if event.get("op") != "paint":
            continue
        color = event.get("color")
        for cell_value in event.get("painted", []):
            r, c = normalize_cell(cell_value)
            for i, rule in enumerate(forbidden_rules):
                banned = rule.get("color", rule.get("colour", "*"))
                if _rule_matches(rule, r, c, color):
                    wildcard = banned in {"*", "any", None}
                    if wildcard or color == banned:
                        violations.append(
                            {
                                "kind": "forbidden_paint_during_replay",
                                "rule_index": i,
                                "cell": [r, c],
                                "forbidden": banned,
                                "color": color,
                                "step_index": event.get("step_index"),
                                "message": f"禁涂格 {r},{c} 在第 {event.get('step_index', 0) + 1} 步被涂成 {color}",
                            }
                        )
            for i, rule in enumerate(keep_rules):
                required = rule.get("color", rule.get("colour", base_color))
                if _rule_matches(rule, r, c, color) and color != required:
                    violations.append(
                        {
                            "kind": "must_keep_during_replay",
                            "rule_index": i,
                            "cell": [r, c],
                            "required": required,
                            "color": color,
                            "step_index": event.get("step_index"),
                            "message": f"必留格 {r},{c} 在重演中被涂成 {color}，应保持 {required}",
                        }
                    )
    return violations


# ---------------------------------------------------------------------------
# 稿件与合班优化
# ---------------------------------------------------------------------------

def draft_score(draft: Dict[str, Any]) -> int | float:
    score = draft.get("score", 0)
    try:
        value = float(score)
        return int(value) if value.is_integer() else value
    except (TypeError, ValueError):
        return 0


def _subset(ids: Sequence[str], mask: int) -> List[str]:
    return [draft_id for i, draft_id in enumerate(ids) if mask & (1 << i)]


def _score_ids(drafts: Dict[str, Dict[str, Any]], selected: Sequence[str]) -> float:
    return sum(draft_score(drafts[d]) for d in selected)


def patch_conflicts(
    drafts: Dict[str, Dict[str, Any]], selected: Sequence[str]
) -> List[Dict[str, Any]]:
    cell_writers: Dict[str, List[Tuple[str, Any]]] = {}
    wall_writers: Dict[str, List[Tuple[str, Any]]] = {}
    for draft_id in selected:
        patch = drafts[draft_id].get("patch") or {}
        for key, change in (patch.get("cells") or {}).items():
            value = change["to"] if isinstance(change, dict) else change
            cell_writers.setdefault(key, []).append((draft_id, value))
        for key, change in (patch.get("walls") or {}).items():
            value = change["to"] if isinstance(change, dict) else change
            wall_writers.setdefault(key, []).append((draft_id, value))

    conflicts: List[Dict[str, Any]] = []
    for key, writers in cell_writers.items():
        values = {value for _id, value in writers}
        if len(values) > 1:
            conflicts.append(
                {
                    "type": "cell",
                    "key": key,
                    "writers": [{"draft_id": d, "to": v} for d, v in writers],
                    "message": f"格 {key} 被各稿改成不同颜色",
                }
            )
    for key, writers in wall_writers.items():
        values = {bool(value) for _id, value in writers}
        if len(values) > 1:
            conflicts.append(
                {
                    "type": "wall",
                    "key": key,
                    "writers": [{"draft_id": d, "wall": v} for d, v in writers],
                    "message": f"挡线 {key} 被各稿改成不同状态",
                }
            )
    return conflicts


def _patch_reports(
    master: Dict[str, Any],
    candidate_board: Dict[str, Any],
    drafts: Dict[str, Dict[str, Any]],
    selected: Sequence[str],
) -> List[Dict[str, Any]]:
    final_diff = diff_boards(master, candidate_board)
    reports = []
    for draft_id in selected:
        patch = drafts[draft_id].get("patch") or {}
        real_cells: Dict[str, Any] = {}
        real_walls: Dict[str, Any] = {}
        for key, change in (patch.get("cells") or {}).items():
            to_value = change["to"] if isinstance(change, dict) else change
            if key in final_diff["cells"] and final_diff["cells"][key]["to"] == to_value:
                real_cells[key] = final_diff["cells"][key]
        for key, change in (patch.get("walls") or {}).items():
            to_value = change["to"] if isinstance(change, dict) else change
            if key in final_diff["walls"] and final_diff["walls"][key]["to"] == to_value:
                real_walls[key] = final_diff["walls"][key]
        reports.append(
            {
                "draft_id": draft_id,
                "score": draft_score(drafts[draft_id]),
                "original_preview_changes": patch,
                "real_changes": {"cells": real_cells, "walls": real_walls},
            }
        )
    return reports


def evaluate_patch(
    master_board: Dict[str, Any],
    drafts: Dict[str, Dict[str, Any]],
    selected: Sequence[str],
    lesson: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    conflicts = patch_conflicts(drafts, selected)
    board = clone_board(master_board)
    if not conflicts:
        for draft_id in selected:
            apply_patch(board, drafts[draft_id].get("patch") or {})

    violations = validate_final(board, lesson)
    trace_violations: List[Dict[str, Any]] = []
    for draft_id in selected:
        native = drafts[draft_id].get("native_trace") or []
        for v in validate_trace(native, lesson, master_board.get("base_color", "white")):
            v = dict(v)
            v["draft_id"] = draft_id
            trace_violations.append(v)

    legal = not conflicts and not violations and not trace_violations
    return {
        "convention": "patch",
        "draft_ids": list(selected),
        "order": list(selected),
        "score": _score_ids(drafts, selected),
        "legal": legal,
        "conflicts": conflicts,
        "violations": violations + trace_violations,
        "board": board,
        "draft_reports": _patch_reports(master_board, board, drafts, selected) if not conflicts else [],
    }


def order_is_allowed(
    order: Sequence[str],
    allowed_orders: Optional[List[List[str]]] = None,
    precedence: Optional[List[Sequence[str]]] = None,
) -> bool:
    positions = {draft_id: i for i, draft_id in enumerate(order)}

    # 显式 allowed_orders 是“几份笔法”，满足其中任意一份的相对先后即可；
    # 规则中没有提到的稿件允许插在任意位置。
    if allowed_orders:
        matched_any = False
        for rule in allowed_orders:
            rule_positions = [positions[d] for d in rule if d in positions]
            if len(rule_positions) <= 1 or all(
                earlier < later for earlier, later in zip(rule_positions, rule_positions[1:])
            ):
                matched_any = True
                break
        if not matched_any:
            return False

    # precedence 则是必须全部满足的硬先后约束，每项形如 [before, after]。
    for pair in precedence or []:
        if len(pair) < 2:
            continue
        before, after = pair[0], pair[1]
        if before in positions and after in positions:
            if positions[before] >= positions[after]:
                return False
    return True


def evaluate_replay_order(
    master_board: Dict[str, Any],
    drafts: Dict[str, Dict[str, Any]],
    order: Sequence[str],
    lesson: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    board = clone_board(master_board)
    reports: List[Dict[str, Any]] = []
    all_violations: List[Dict[str, Any]] = []

    for draft_id in order:
        before = clone_board(board)
        draft = drafts[draft_id]
        try:
            result = simulate(board, draft.get("steps") or [])
        except WhiteboardError as exc:
            return {
                "convention": "replay",
                "draft_ids": list(order),
                "order": list(order),
                "score": _score_ids(drafts, order),
                "legal": False,
                "violations": [{"kind": "invalid_step", "message": str(exc), "draft_id": draft_id}],
                "board": board,
                "draft_reports": reports,
            }
        board = result["board"]
        real_changes = diff_boards(before, board)
        original_patch = draft.get("patch") or diff_boards(
            draft.get("observed_board") or before, draft.get("preview_board") or board
        )
        changed_during_merge = (
            real_changes != original_patch
        )
        reports.append(
            {
                "draft_id": draft_id,
                "score": draft_score(draft),
                "original_preview_changes": original_patch,
                "real_changes": real_changes,
                "changed_because_of_merge_context": changed_during_merge,
            }
        )
        for v in validate_trace(result["trace"], lesson, master_board.get("base_color", "white")):
            v = dict(v)
            v["draft_id"] = draft_id
            all_violations.append(v)

    final_violations = validate_final(board, lesson)
    legal = not all_violations and not final_violations
    return {
        "convention": "replay",
        "draft_ids": list(order),
        "order": list(order),
        "score": _score_ids(drafts, order),
        "legal": legal,
        "violations": all_violations + final_violations,
        "board": board,
        "draft_reports": reports,
    }


def best_replay_permutation(
    master_board: Dict[str, Any],
    drafts: Dict[str, Dict[str, Any]],
    selected: Sequence[str],
    lesson: Optional[Dict[str, Any]] = None,
    allowed_orders: Optional[List[List[str]]] = None,
    precedence: Optional[List[Sequence[str]]] = None,
) -> Dict[str, Any]:
    """对已经确定要发表的稿件集合，只选择合法顺序，不再丢弃稿件。"""
    ids = list(selected)
    if not ids:
        candidate = evaluate_replay_order(master_board, drafts, [], lesson)
        return {"best": candidate if candidate["legal"] else None, "attempts": [candidate]}

    attempts: List[Dict[str, Any]] = []
    for order in permutations(ids):
        if order_is_allowed(order, allowed_orders, precedence):
            attempts.append(evaluate_replay_order(master_board, drafts, list(order), lesson))
    legal = [candidate for candidate in attempts if candidate.get("legal")]
    if not legal:
        return {"best": None, "attempts": attempts}
    return {"best": max(legal, key=_candidate_key), "attempts": attempts}


def _candidate_key(candidate: Dict[str, Any]) -> Tuple[float, int, Tuple[str, ...]]:
    # 分数越高越好；同分时稿数越多越好；最后用 ID 顺序保证确定性。
    return (
        candidate["score"],
        len(candidate["draft_ids"]),
        tuple(sorted(candidate["draft_ids"])),
    )


def plan_merge(
    convention: str,
    master_board: Dict[str, Any],
    drafts: Dict[str, Dict[str, Any]],
    candidate_ids: Sequence[str],
    lesson: Optional[Dict[str, Any]] = None,
    allowed_orders: Optional[List[List[str]]] = None,
    precedence: Optional[List[Sequence[str]]] = None,
    requested_order: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    convention = normalize_convention(convention)
    ids = list(candidate_ids)
    if len(ids) > MAX_DRAFTS_PER_SESSION:
        raise WhiteboardError(f"单节至多 {MAX_DRAFTS_PER_SESSION} 稿")
    missing = [d for d in ids if d not in drafts]
    if missing:
        raise NotFoundError(f"稿件不存在: {', '.join(missing)}")

    evaluations: List[Dict[str, Any]] = []

    if convention == "patch":
        for mask in range(1 << len(ids)):
            selected = _subset(ids, mask)
            evaluations.append(evaluate_patch(master_board, drafts, selected, lesson))
    else:
        if requested_order is not None:
            requested = list(requested_order)
            if set(requested) - set(ids):
                raise WhiteboardError("指定顺序包含不在候选集中的稿件")
            # 允许只指定部分先后；补全剩余候选的所有位置没有必要。
            # 这里把 requested_order 当作本次要评估的完整选择/顺序。
            if any(d not in drafts for d in requested):
                raise NotFoundError("顺序中含不存在的稿件")
            requested_candidate = evaluate_replay_order(
                master_board, drafts, requested, lesson
            )
            if not order_is_allowed(requested, allowed_orders, precedence):
                requested_candidate["legal"] = False
                requested_candidate.setdefault("violations", []).append(
                    {"kind": "order_not_allowed", "message": "指定顺序不在老师允许的笔法先后中"}
                )
            evaluations.append(requested_candidate)
        else:
            for mask in range(1 << len(ids)):
                selected = _subset(ids, mask)
                if not selected:
                    evaluations.append(evaluate_replay_order(master_board, drafts, [], lesson))
                    continue
                for order in permutations(selected):
                    if order_is_allowed(order, allowed_orders, precedence):
                        evaluations.append(
                            evaluate_replay_order(master_board, drafts, list(order), lesson)
                        )

    legal = [e for e in evaluations if e.get("legal")]
    if not legal:
        return {
            "convention": convention,
            "best": None,
            "legal_count": 0,
            "evaluated_count": len(evaluations),
            "candidate_ids": ids,
            "allowed_orders": allowed_orders or [],
        }

    best = max(legal, key=_candidate_key)
    # 同一分数可能有多个合法方案；按约定分数相等可任选，这里额外返回前几个便于讲解。
    ranked = sorted(legal, key=_candidate_key, reverse=True)
    alternatives = []
    for cand in ranked[1:6]:
        alternatives.append(
            {
                "draft_ids": cand["draft_ids"],
                "order": cand.get("order", cand["draft_ids"]),
                "score": cand["score"],
            }
        )
    return {
        "convention": convention,
        "best": best,
        "alternatives": alternatives,
        "legal_count": len(legal),
        "evaluated_count": len(evaluations),
        "candidate_ids": ids,
        "allowed_orders": allowed_orders or [],
        "precedence": precedence or [],
        "tie_policy": "score desc, draft count desc, lexicographic ids; equal scores may choose any legal plan",
    }


def normalize_convention(value: str) -> str:
    if value in {"patch", "sticker", "照样贴图", "贴图", "patchwork"}:
        return "patch"
    if value in {"replay", "stroke", "重演笔法", "重演", "笔法"}:
        return "replay"
    raise WhiteboardError(f"未知合班约定: {value!r}；应为 patch/照样贴图 或 replay/重演笔法")
