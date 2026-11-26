"""用整数色阶渲染小图，代替真实显影。

输出两种本地可打开的格式：
- SVG：矢量小图，每格按所在色阶填不同灰度（阶越高、吃的光越多、纸越浅）；
- PGM（P5 二进制）：无依赖的位图格式，任何看图器都能开。
"""

from __future__ import annotations

from pathlib import Path

from .domain import StepCard

CELL_PX = 40


def _gray(level_idx: int | None, level_count: int) -> int:
    """色阶序号 -> 0..255 灰度。未入阶按最浅处理。"""
    if level_idx is None:
        return 245
    if level_count <= 1:
        return 60
    return int(60 + 175 * level_idx / (level_count - 1))


def level_indices(card: StepCard, lights: dict[str, int]) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for key, light in lights.items():
        lv = card.level_of(light)
        out[key] = card.levels.index(lv) if lv is not None else None
    return out


def render_svg(path: str | Path, rows: int, cols: int,
               indices: dict[str, int | None], level_count: int,
               title: str) -> str:
    w, h = cols * CELL_PX, rows * CELL_PX
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h + 28}">',
        f'<text x="4" y="18" font-family="monospace" font-size="14">{title}</text>',
        f'<g transform="translate(0,24)">',
    ]
    for r in range(rows):
        for c in range(cols):
            g = _gray(indices.get(f"{r},{c}"), level_count)
            parts.append(
                f'<rect x="{c * CELL_PX}" y="{r * CELL_PX}" width="{CELL_PX}"'
                f' height="{CELL_PX}" fill="rgb({g},{g},{g})" stroke="#333"/>'
            )
            idx = indices.get(f"{r},{c}")
            label = "-" if idx is None else str(idx)
            parts.append(
                f'<text x="{c * CELL_PX + CELL_PX // 2 - 4}"'
                f' y="{r * CELL_PX + CELL_PX // 2 + 5}"'
                f' font-family="monospace" font-size="16" fill="#c33">{label}</text>'
            )
    parts.append("</g></svg>")
    text = "\n".join(parts)
    Path(path).write_text(text, encoding="utf-8")
    return text


def render_pgm(path: str | Path, rows: int, cols: int,
               indices: dict[str, int | None], level_count: int) -> bytes:
    header = f"P5\n{cols} {rows}\n255\n".encode()
    body = bytearray()
    for r in range(rows):
        for c in range(cols):
            body.append(_gray(indices.get(f"{r},{c}"), level_count))
    data = header + bytes(body)
    Path(path).write_bytes(data)
    return data


def render_paper(product_dir: str | Path, paper_id: str, rows: int, cols: int,
                 card: StepCard, lights: dict[str, int], suffix: str = "成品") -> dict:
    product_dir = Path(product_dir)
    product_dir.mkdir(parents=True, exist_ok=True)
    indices = level_indices(card, lights)
    svg = product_dir / f"{paper_id}-{suffix}.svg"
    pgm = product_dir / f"{paper_id}-{suffix}.pgm"
    render_svg(svg, rows, cols, indices, len(card.levels),
               f"{paper_id} {suffix} lights={lights}")
    render_pgm(pgm, rows, cols, indices, len(card.levels))
    return {"svg": str(svg), "pgm": str(pgm)}
