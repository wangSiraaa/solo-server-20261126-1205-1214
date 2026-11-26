"""路线试配与离线包承诺校验。

规划器在三个自由度上搜索：
  1. 每个有备选旁白的站，用原声还是备选；
  2. 在游客/委托允许的静音集合内，静音哪些轨；
  3. 总耳机时长是否在预算内。

离线包的许可证必须在【收听日】和【归还日】都成立——签出之后包不联网，
不能假定路中可以远程撤回包内声部。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from .models import PlanRequest, Segment
from .rendering import audible_persons
from .store import ALL, GuideError, GuideStore, weekday_of


@dataclass(frozen=True)
class ClipPlan:
    station_id: str
    included_segments: tuple[str, ...]
    muted_tracks: frozenset[str]
    narration_segment_id: str | None
    narration_holder: str | None
    uses_fallback: bool
    audible_persons: frozenset[str]
    duration: float


@dataclass
class Plan:
    ok: bool
    request: PlanRequest
    clips: list[ClipPlan] = field(default_factory=list)
    mutes: frozenset[str] = frozenset()
    total_duration: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def clip(self, station_id: str) -> ClipPlan:
        for c in self.clips:
            if c.station_id == station_id:
                return c
        raise GuideError(f"计划中没有站点 {station_id} 的片段")


def person_licensed(
    store: GuideStore,
    person: str,
    station_id: str,
    mode: str,
    listen_day: str,
    loan_to: str,
) -> bool:
    """当事人是否对某站、某播放方式，在收听日至归还日间持续授权。"""
    weekday = weekday_of(listen_day)
    for lic in store.active_licenses_for(person):
        if (
            lic.covers_station(station_id)
            and lic.covers_day(weekday)
            and lic.covers_mode(mode)
            and lic.covers_date(listen_day)
            and lic.covers_date(loan_to)
        ):
            return True
    return False


def _station_open(store: GuideStore, station_id: str, weekday: str) -> bool:
    station = store.get_station(station_id)
    return (
        (ALL in store.global_open_days or weekday in store.global_open_days)
        and (ALL in station.open_days or weekday in station.open_days)
    )


def _narration_options(store: GuideStore, station_id: str) -> list[tuple[str, bool]]:
    """返回 [(旁白声段id, 是否备选)]，原声优先。"""
    slot = store.get_station(station_id).required_slot
    if slot is None:
        return [(None, False)]
    options: list[tuple[str, bool]] = [(slot.prefer, False)]
    if slot.fallback:
        options.append((slot.fallback, True))
    return options


def _build_clip(
    store: GuideStore,
    station_id: str,
    narration_id: str | None,
    mutes: frozenset[str],
) -> ClipPlan:
    all_segments = store.segments_for(station_id)
    segs = [s for s in all_segments if s.track not in mutes]
    playable = {s.id for s in segs}
    chosen_narr: Segment | None = None
    if narration_id is not None:
        segs = [s for s in segs if s.kind != "narration" or s.id == narration_id]
        if narration_id in playable:
            chosen_narr = store.segments[narration_id]
        else:
            narration_id = None  # 选中旁白在静音轨上 → 缺位
    segs = sorted(segs, key=lambda s: s.id)
    heard = audible_persons(segs, mutes)
    slot = store.get_station(station_id).required_slot
    return ClipPlan(
        station_id=station_id,
        included_segments=tuple(s.id for s in segs),
        muted_tracks=frozenset(mutes),
        narration_segment_id=narration_id,
        narration_holder=chosen_narr.holder if chosen_narr else None,
        uses_fallback=bool(chosen_narr and slot and chosen_narr.id != slot.prefer),
        audible_persons=frozenset(heard),
        duration=max((s.end for s in segs), default=0.0),
    )


def _clip_legal(
    store: GuideStore,
    clip: ClipPlan,
    req: PlanRequest,
    require_original: bool,
) -> str | None:
    """返回 None 表示合法；否则返回一条不可兑现原因。"""
    station = store.get_station(clip.station_id)
    if station.required_slot is not None and clip.narration_segment_id is None:
        return f"{station.id}（{station.name}）必讲旁白缺失"
    if require_original and station.required_slot is not None:
        if clip.narration_segment_id != station.required_slot.prefer:
            return f"{station.id}（{station.name}）委托指定必须使用原声 {station.required_slot.prefer}"
    # 每一个实际可闻的人都要有覆盖到归还日的许可
    for person in sorted(clip.audible_persons):
        if not person_licensed(
            store, person, clip.station_id, req.mode, req.listen_day, req.loan_to
        ):
            return (
                f"{station.id}（{station.name}）可闻当事人 {person} 缺少"
                f"{req.mode} 许可，或许可无法承诺到归还日 {req.loan_to}"
            )
    return None


def _mute_subsets(allowed: frozenset[str]) -> list[frozenset[str]]:
    items = sorted(allowed)
    out: list[frozenset[str]] = [frozenset()]
    for size in range(1, len(items) + 1):
        out.extend(frozenset(c) for c in combinations(items, size))
    return out


def _diagnose(store: GuideStore, req: PlanRequest, weekday: str, station_ids: list[str]) -> list[str]:
    """搜索失败后，逐站给出能解释失败的具体原因。"""
    reasons: list[str] = []
    for sid in station_ids:
        station = store.get_station(sid)
        if not _station_open(store, sid, weekday):
            reasons.append(f"{sid}（{station.name}）在收听日 {req.listen_day}（{weekday}）不开放")
        if req.require_original_station == sid and station.required_slot is not None:
            prefer = store.segments[station.required_slot.prefer]
            if not person_licensed(
                store, prefer.holder, sid, req.mode, req.listen_day, req.loan_to
            ):
                reasons.append(
                    f"{sid}（{station.name}）委托指定必须使用原声 {prefer.id}，"
                    f"但当事人 {prefer.holder} 已无可用 {req.mode} 许可，备选旁白不被接受"
                )
        slot = station.required_slot
        if slot is not None:
            for seg_id, is_fb in _narration_options(store, sid):
                seg = store.segments[seg_id]
                if not person_licensed(store, seg.holder, sid, req.mode, req.listen_day, req.loan_to):
                    tag = "备选旁白" if is_fb else "原声"
                    reasons.append(
                        f"{sid}（{station.name}）{tag} {seg_id} 当事人 {seg.holder} "
                        f"无可用 {req.mode} 许可（或许可不能覆盖到归还日）"
                    )
        # 漏入检查：即使允许静音，看哪些人无法被消掉
        for seg in store.segments_for(sid):
            for leak in seg.leaks:
                killable = leak.source_track in req.mutes
                if not killable and not person_licensed(
                    store, leak.person, sid, req.mode, req.listen_day, req.loan_to
                ):
                    reasons.append(
                        f"{sid}（{station.name}）{seg.id} 的 {leak.start:.0f}-{leak.end:.0f} 秒"
                        f"漏入 {leak.person}，源轨 {leak.source_track} 不在允许静音集合内且无许可"
                    )
    return reasons


def plan_route(store: GuideStore, req: PlanRequest) -> Plan:
    route = store.get_route(req.route_id)
    station_ids = list(route.stations)
    weekday = weekday_of(req.listen_day)
    plan = Plan(ok=False, request=req)

    # 1. 真实路径：相邻站之间的通路必须仍然开放
    for a, b in zip(station_ids, station_ids[1:]):
        if not store.edge_open(a, b):
            plan.reasons.append(f"通路 {a}->{b} 已关闭，路线 {route.id} 无法按真实路径兑现")
    for sid in station_ids:
        if not _station_open(store, sid, weekday):
            plan.reasons.append(
                f"{sid}（{store.get_station(sid).name}）在 {req.listen_day}（{weekday}）不开放"
            )
    if plan.reasons:
        return plan

    # 2. 笛卡尔搜索：旁白选项 × 静音子集（标记规模小，直接穷举）
    options_per_station = [_narration_options(store, sid) for sid in station_ids]
    best: Plan | None = None
    for mutes in _mute_subsets(req.mutes):
        # 旁白所在轨被静音时，该旁白实际不可闻，本轮不得作为选项
        live_options: list[list[tuple[str, bool]]] = []
        for sid, options in zip(station_ids, options_per_station):
            live = [(sid_opt, fb) for sid_opt, fb in options
                    if store.segments[sid_opt].track not in mutes]
            live_options.append(live)

        def search(idx: int, clips: list[ClipPlan]) -> Plan | None:
            if idx == len(station_ids):
                total = sum(c.duration for c in clips)
                if total > req.headphone_budget_s:
                    return None
                candidate = Plan(
                    ok=True, request=req, clips=list(clips),
                    mutes=frozenset(mutes), total_duration=total,
                )
                return candidate
            sid = station_ids[idx]
            require_original = req.require_original_station == sid
            for narration_id, _is_fb in live_options[idx]:
                clip = _build_clip(store, sid, narration_id, mutes)
                err = _clip_legal(store, clip, req, require_original)
                if err is None:
                    found = search(idx + 1, clips + [clip])
                    if found is not None:
                        return found
            return None

        found = search(0, [])
        if found is not None:
            best = found
            break  # 静音最少的可行方案优先

    if best is not None:
        return best

    plan.reasons = _diagnose(store, req, weekday, station_ids)
    if not plan.reasons and req.headphone_budget_s < float("inf"):
        minimum = sum(
            min(
                store.segments[seg_id].duration
                for seg_id, _ in _narration_options(store, sid)
            )
            for sid in station_ids
        )
        plan.reasons.append(
            f"许可与静音均可行，但最短也要 {minimum:.0f} 秒，"
            f"超过耳机预算 {req.headphone_budget_s:.0f} 秒"
        )
    if not plan.reasons:
        plan.reasons.append("在允许的旁白与静音范围内没有可行拼法")
    return plan
