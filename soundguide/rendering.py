"""消音渲染：给定静音轨集合，计算游客实际会听到的人声时码段。

关键规则：漏入声挂在另一条现场轨上。静音当事人的麦轨只能去掉其直录音，
漏入声随源轨播放，源轨不静音就仍然可闻。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Segment


@dataclass(frozen=True)
class AudibleInterval:
    person: str
    start: float
    end: float
    source_track: str
    via: str  # direct=本人麦轨直录；leak=现场轨漏入


def render_timeline(segments: list[Segment], muted_tracks: frozenset[str]) -> list[AudibleInterval]:
    """返回游客实际听到的全部人声时码段（已按开始时间排序）。"""
    heard: list[AudibleInterval] = []
    for seg in segments:
        if seg.track in muted_tracks:
            continue  # 整条轨被静音，直录与该轨上的漏入都不播
        if seg.kind == "narration":
            heard.append(
                AudibleInterval(seg.holder, seg.start, seg.end, seg.track, "direct")
            )
        for leak in seg.leaks:
            if leak.source_track in muted_tracks:
                continue
            heard.append(
                AudibleInterval(leak.person, leak.start, leak.end, leak.source_track, "leak")
            )
    return sorted(heard, key=lambda iv: (iv.start, iv.end, iv.person))


def audible_persons(segments: list[Segment], muted_tracks: frozenset[str]) -> set[str]:
    return {iv.person for iv in render_timeline(segments, muted_tracks)}


def person_audible_intervals(
    segments: list[Segment], muted_tracks: frozenset[str], person: str
) -> list[AudibleInterval]:
    return [
        iv for iv in render_timeline(segments, muted_tracks) if iv.person == person
    ]
