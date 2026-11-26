"""Theme / style / surface / viewing-mode model and badge rendering.

A *theme* bundles:

* ``styles``  -- named badge designs.  Each style has a ``shape`` name and an
  ``edge`` and ``fill`` colour reference.
* ``surfaces`` -- overlays (room, subtitle bar, danmaku overlay, ...).  A
  surface may override the edge/fill colour of a style, either for one
  ``(mode, style)`` pair or as a default for a whole mode.
* ``modes``   -- the simplified viewing modes.  Each mode carries a colour
  table mapping a *colour code* to the actual colour block name the viewer
  sees.
* ``contexts`` -- the ``(surface, mode)`` pairs that are actually supported.

A colour reference is either an explicit colour code or an *alias* that borrows
another style's edge/fill.  Aliases are resolved at the style level (a surface
override that aliases a style borrows that style's own design colour), and
cycles are detected.

Rendering a style in a context yields ``(shape, edge_block, fill_block)`` using
names only -- no pixels anywhere in the service.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Colour references
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColorRef:
    """An explicit colour code or an alias to another style's edge/fill."""

    kind: str  # "explicit" | "alias"
    code: Optional[str] = None
    style: Optional[str] = None
    part: Optional[str] = None  # "edge" | "fill"

    def __post_init__(self) -> None:
        if self.kind not in ("explicit", "alias"):
            raise ValueError(f"unknown ColorRef kind {self.kind!r}")
        if self.kind == "explicit" and self.code is None:
            raise ValueError("explicit ColorRef needs a code")
        if self.kind == "alias":
            if self.style is None or self.part not in ("edge", "fill"):
                raise ValueError("alias ColorRef needs style and part in {edge,fill}")

    @staticmethod
    def explicit(code: str) -> "ColorRef":
        return ColorRef("explicit", code=code)

    @staticmethod
    def alias(style: str, part: str) -> "ColorRef":
        return ColorRef("alias", style=style, part=part)

    def to_json(self) -> dict:
        if self.kind == "explicit":
            return {"kind": "explicit", "code": self.code}
        return {"kind": "alias", "style": self.style, "part": self.part}

    @staticmethod
    def from_json(d: dict) -> "ColorRef":
        if d["kind"] == "explicit":
            return ColorRef.explicit(d["code"])
        return ColorRef.alias(d["style"], d["part"])


# ---------------------------------------------------------------------------
# Theme components
# ---------------------------------------------------------------------------


@dataclass
class StyleSpec:
    name: str
    shape: str
    edge: ColorRef
    fill: ColorRef

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "shape": self.shape,
            "edge": self.edge.to_json(),
            "fill": self.fill.to_json(),
        }

    @staticmethod
    def from_json(d: dict) -> "StyleSpec":
        return StyleSpec(
            name=d["name"],
            shape=d["shape"],
            edge=ColorRef.from_json(d["edge"]),
            fill=ColorRef.from_json(d["fill"]),
        )


@dataclass
class SurfaceSpec:
    """An overlay that can rewrite colours of styles.

    ``defaults[mode][part]`` applies to every style in that mode; a key in
    ``overrides[(mode, style)][part]`` wins over the default.
    """

    name: str
    defaults: Dict[str, Dict[str, ColorRef]] = field(default_factory=dict)
    overrides: Dict[Tuple[str, str], Dict[str, ColorRef]] = field(default_factory=dict)

    def ref_for(self, mode: str, style: str, part: str) -> Optional[ColorRef]:
        ov = self.overrides.get((mode, style))
        if ov and part in ov:
            return ov[part]
        d = self.defaults.get(mode)
        if d and part in d:
            return d[part]
        return None

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "defaults": {
                m: {p: r.to_json() for p, r in parts.items()}
                for m, parts in self.defaults.items()
            },
            "overrides": {
                f"{m}|{s}": {p: r.to_json() for p, r in parts.items()}
                for (m, s), parts in self.overrides.items()
            },
        }

    @staticmethod
    def from_json(d: dict) -> "SurfaceSpec":
        overrides: Dict[Tuple[str, str], Dict[str, ColorRef]] = {}
        for key, parts in d.get("overrides", {}).items():
            m, s = key.split("|", 1)
            overrides[(m, s)] = {p: ColorRef.from_json(r) for p, r in parts.items()}
        return SurfaceSpec(
            name=d["name"],
            defaults={
                m: {p: ColorRef.from_json(r) for p, r in parts.items()}
                for m, parts in d.get("defaults", {}).items()
            },
            overrides=overrides,
        )


@dataclass
class ModeSpec:
    name: str
    color_map: Dict[str, str]  # colour code -> colour-block name

    def block(self, code: str) -> str:
        # Unlisted codes fall back to themselves so partial maps still render.
        return self.color_map.get(code, code)

    def to_json(self) -> dict:
        return {"name": self.name, "color_map": dict(self.color_map)}

    @staticmethod
    def from_json(d: dict) -> "ModeSpec":
        return ModeSpec(name=d["name"], color_map=dict(d["color_map"]))


@dataclass
class Theme:
    name: str
    styles: Dict[str, StyleSpec]
    surfaces: Dict[str, SurfaceSpec]
    modes: Dict[str, ModeSpec]
    contexts: List[Tuple[str, str]]  # (surface, mode)

    # -- serialisation ----------------------------------------------------
    def to_json(self) -> dict:
        return {
            "name": self.name,
            "styles": {n: s.to_json() for n, s in self.styles.items()},
            "surfaces": {n: s.to_json() for n, s in self.surfaces.items()},
            "modes": {n: m.to_json() for n, m in self.modes.items()},
            "contexts": [list(c) for c in self.contexts],
        }

    @staticmethod
    def from_json(d: dict) -> "Theme":
        return Theme(
            name=d["name"],
            styles={n: StyleSpec.from_json(s) for n, s in d["styles"].items()},
            surfaces={n: SurfaceSpec.from_json(s) for n, s in d["surfaces"].items()},
            modes={n: ModeSpec.from_json(m) for n, m in d["modes"].items()},
            contexts=[tuple(c) for c in d["contexts"]],
        )

    def clone(self, name: Optional[str] = None) -> "Theme":
        t = copy.deepcopy(self)
        if name is not None:
            t.name = name
        return t

    # -- colour resolution ------------------------------------------------
    def _resolve_ref(self, ref: ColorRef, seen: frozenset) -> str:
        if ref.kind == "explicit":
            return ref.code  # type: ignore[return-value]
        key = (ref.style, ref.part)
        if key in seen:
            raise ValueError(f"colour alias cycle at {key}")
        style = self.styles[ref.style]
        base = style.edge if ref.part == "edge" else style.fill
        return self._resolve_ref(base, seen | {key})

    def resolve_color(
        self, style: str, part: str, surface: str, mode: str
    ) -> str:
        """Resolve the colour *code* a style shows in a context."""
        if style not in self.styles:
            raise KeyError(f"unknown style {style!r}")
        surf = self.surfaces[surface]
        ref = surf.ref_for(mode, style, part)
        if ref is None:
            st = self.styles[style]
            ref = st.edge if part == "edge" else st.fill
        return self._resolve_ref(ref, frozenset())

    def render(self, style: str, surface: str, mode: str) -> Tuple[str, str, str]:
        """Render one style in a context as ``(shape, edge_block, fill_block)``."""
        shape = self.styles[style].shape
        edge_code = self.resolve_color(style, "edge", surface, mode)
        fill_code = self.resolve_color(style, "fill", surface, mode)
        m = self.modes[mode]
        return shape, m.block(edge_code), m.block(fill_code)

    def render_all(self, style: str) -> Dict[str, Tuple[str, str, str]]:
        return {
            f"{surf}@{mode}": self.render(style, surf, mode)
            for surf, mode in self.contexts
        }


# ---------------------------------------------------------------------------
# Distinguishability
# ---------------------------------------------------------------------------


def render_token(
    theme: Theme, style: str, surface: Optional[str] = None, mode: Optional[str] = None
) -> Tuple[str, str, str]:
    """Render for one context, or the default (first) supported context."""
    if surface is None:
        surface = theme.contexts[0][0]
    if mode is None:
        mode = theme.contexts[0][1]
    return theme.render(style, surface, mode)


def _contexts(theme: Theme, contexts) -> List[Tuple[str, str]]:
    return list(contexts) if contexts is not None else list(theme.contexts)


def indistinguishable_contexts(theme: Theme, a: str, b: str, contexts=None) -> List[str]:
    """Contexts in which two styles render identically (empty == distinguishable)."""
    out = []
    for surf, mode in _contexts(theme, contexts):
        if theme.render(a, surf, mode) == theme.render(b, surf, mode):
            out.append(f"{surf}@{mode}")
    return out


def distinguishable(theme: Theme, a: str, b: str, contexts=None) -> bool:
    """Two styles are distinguishable iff they differ in *every* context."""
    return not indistinguishable_contexts(theme, a, b, contexts)


def explain_indistinguishable(theme: Theme, a: str, b: str, contexts=None) -> str:
    ctx = indistinguishable_contexts(theme, a, b, contexts)
    if not ctx:
        return f"{a} 与 {b} 在所有受支持语境都可区分"
    parts = []
    for key in ctx:
        surf, mode = key.split("@", 1)
        r = theme.render(a, surf, mode)
        parts.append(f"{key}: shape={r[0]}, edge={r[1]}, fill={r[2]}")
    return (
        f"{a} 与 {b} 在以下语境无法区分: " + "; ".join(parts)
    )


# ---------------------------------------------------------------------------
# Built-in themes
# ---------------------------------------------------------------------------

DARK = "C-DARK"
LIGHT = "C-LIGHT"
MID = "C-MID"
ALT = "C-ALT"
COMMON = "C-COMMON"


def _base_surfaces(surface_names: List[str]) -> Dict[str, SurfaceSpec]:
    return {n: SurfaceSpec(n) for n in surface_names}


def _base_modes() -> Dict[str, ModeSpec]:
    return {
        "普通": ModeSpec(
            "普通",
            {DARK: "色块·深", LIGHT: "色块·浅", MID: "色块·中", ALT: "色块·青", COMMON: "色块·中"},
        ),
        "大字": ModeSpec(
            "大字",
            {
                DARK: "色块·深（大字）",
                LIGHT: "色块·浅（大字）",
                MID: "色块·中（大字）",
                ALT: "色块·青（大字）",
                COMMON: "色块·中（大字）",
            },
        ),
    }


_STYLE_FACTORY = {
    "round_dark": ("圆", DARK),
    "round_light": ("圆", LIGHT),
    "square_dark": ("方", DARK),
    "triangle_light": ("三角", LIGHT),
}


def default_theme() -> Theme:
    """The base, fully distinguishing theme.

    ``round_dark`` / ``round_light`` are the two round-edge badges (deep and
    light edge).  ``square_dark`` / ``triangle_light`` add extra charateristic
    *shapes* so a solver can always fall back on shape when colours collide.
    """
    surfaces = _base_surfaces(["房间", "字幕条", "弹幕浮层"])
    modes = _base_modes()
    styles = {}
    for name, (shape, code) in _STYLE_FACTORY.items():
        styles[name] = StyleSpec(name, shape, ColorRef.explicit(code), ColorRef.explicit(MID))
    contexts = [(s, m) for s in surfaces for m in modes]
    return Theme("可辨主题", styles, surfaces, modes, contexts)


def collapsed_theme() -> Theme:
    """Same as :func:`default_theme` but the *large-text subtitle* collapses edges.

    The subtitle-bar surface forces *every* style's edge to the deep colour when
    viewed in large-text mode, mirroring "把大字字幕的边色别名改为同一色".
    Round-deep and round-light then render identically there, so the pairing
    can no longer be signed to two co-present guests.
    """
    theme = default_theme()
    theme.name = "大字字幕塌缩主题"
    theme.surfaces["字幕条"].defaults.setdefault("大字", {})["edge"] = ColorRef.alias(
        "round_dark", "edge"
    )
    return theme
