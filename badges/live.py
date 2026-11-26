"""Fake local live stream: given styles/guests, "render" the picture as names.

Why do this at all?  The task stresses that the *same badge* can look totally
different across overlays, and that two simplified viewing modes each map a
colour code to an actual colour block.  A fake renderer that consumes exactly
the plan the service produced lets a demo (and the tests) check that the two
co-present guests are actually distinguishable on *every* supported overlay in
*every* supported mode -- without any pixel code.

The output is plain JSON a "viewer" can replay, so the stream is reproducible
from the call log alone.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from .model import Theme


def render_screen(
    theme: Theme,
    guests: Sequence[str],
    styles: Dict[str, str],
    captions: Sequence[dict] = (),
) -> dict:
    """Build a fake live "frame": per overlay/mode, each guest's badge.

    ``captions`` is a list of ``{"guest": ..., "speech_no": ...}`` used to lay
    out the subtitle bar; the badge shown next to a caption is looked up from
    ``styles``.
    """
    overlays = []
    for surf, mode in theme.contexts:
        cells = []
        for g in guests:
            shape, edge, fill = theme.render(styles[g], surf, mode)
            cells.append(
                {"guest": g, "style": styles[g], "shape": shape, "edge": edge, "fill": fill}
            )
        overlays.append({"surface": surf, "mode": mode, "badges": cells})

    subtitle = []
    for cap in captions:
        g = cap["guest"]
        # Badges next to captions use the subtitle-bar overlay; show both modes.
        block = {}
        for surf, mode in theme.contexts:
            if surf != "字幕条":
                continue
            shape, edge, fill = theme.render(styles[g], surf, mode)
            block[mode] = {"shape": shape, "edge": edge, "fill": fill}
        subtitle.append(
            {
                "speech_no": cap.get("speech_no"),
                "guest": g,
                "style": styles[g],
                "badge": block,
            }
        )

    # A viewer-facing check embedded in the frame.
    separable = all(
        len({(c["shape"], c["edge"], c["fill"]) for c in o["badges"]}) == len(o["badges"])
        for o in overlays
    )

    return {
        "theme": theme.name,
        "guests": list(guests),
        "overlays": overlays,
        "subtitle": subtitle,
        "all_separable": separable,
    }
