"""Rendering helpers re-exported for convenience."""

from .model import (
    ColorRef,
    SurfaceSpec,
    StyleSpec,
    ModeSpec,
    Theme,
    render_token,
    distinguishable,
    indistinguishable_contexts,
    explain_indistinguishable,
)

__all__ = [
    "ColorRef",
    "SurfaceSpec",
    "StyleSpec",
    "ModeSpec",
    "Theme",
    "render_token",
    "distinguishable",
    "indistinguishable_contexts",
    "explain_indistinguishable",
]
