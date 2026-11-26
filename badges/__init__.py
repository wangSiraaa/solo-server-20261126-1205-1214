"""Local service for managing live-stream guest badges.

The package implements the model described in ``README.md``:

* themes, styles (with edge/fill colour aliases), surfaces/overlays and the
  per-viewing-mode colour tables;
* a solver that assigns badge tokens to guests so that every pair of guests
  that can share a live layout is distinguishable in *every* supported viewing
  mode;
* seasons as isolated snapshots (a theme change forks a new season, old
  speeches and signatures keep the agreement of their own season);
* immutable speech attribution (speech number -> who spoke, forever) plus
  re-issuable appearance receipts;
* a small HTTP API whose calls can be recorded and replayed, and a fake live
  renderer used by the demo.
"""

from .model import (
    Theme,
    StyleSpec,
    SurfaceSpec,
    ModeSpec,
    ColorRef,
    default_theme,
    collapsed_theme,
)
from .render import render_token, distinguishable, explain_indistinguishable
from .planning import SessionSpec, PlanResult, plan_schedule, solve_complete
from .service import Service, Season, ServiceError

__all__ = [
    "Theme",
    "StyleSpec",
    "SurfaceSpec",
    "ModeSpec",
    "ColorRef",
    "default_theme",
    "collapsed_theme",
    "render_token",
    "distinguishable",
    "explain_indistinguishable",
    "SessionSpec",
    "PlanResult",
    "plan_schedule",
    "solve_complete",
    "Service",
    "Season",
    "ServiceError",
]
