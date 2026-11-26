"""借出包的逐站模拟播放与回查。

账目纪律：
  * 只认发放号对应快照里的站点与声段；游客在不在路线上的站请求播放 → 走错路拒绝；
  * 重领同一包拿到的是原发放号，播放账按 (发放号, 站) 幂等，不会重复记片段；
  * A 站播过的旁白不会记到 B 站头上——按站号回查只返回本站真实收到的片段。
"""

from __future__ import annotations

from .loans import Loan, load_loan
from .rendering import AudibleInterval, render_timeline
from .store import GuideError, GuideStore


def _clip_record(loan: Loan, station_id: str) -> dict:
    rec = next((c for c in loan.clips if c["station_id"] == station_id), None)
    if rec is None:
        raise GuideError(
            f"发放号 {loan.issue_id} 的路线 {loan.route_id} 不含站点 {station_id}，"
            "游客走错路：该站声音不能算作本路线已讲"
        )
    return rec


def play_station(
    store: GuideStore, issue_id: int, station_id: str
) -> dict:
    """模拟游客到达某站播放。重复播放返回首账，不重复发放。"""
    loan = load_loan(store, issue_id)
    clip = _clip_record(loan, station_id)

    already = store.ledger_for(issue_id, station_id)
    if already:
        return already[0]

    segments = [store.segments[sid] for sid in clip["included_segments"]]
    timeline = render_timeline(segments, frozenset(clip["muted_tracks"]))
    entry = {
        "issue_id": issue_id,
        "visitor_id": loan.visitor_id,
        "station_id": station_id,
        "narration_segment_id": clip["narration_segment_id"],
        "narration_holder": clip["narration_holder"],
        "uses_fallback": clip["uses_fallback"],
        "played_segments": list(clip["included_segments"]),
        "muted_tracks": list(clip["muted_tracks"]),
        "audible_timeline": [
            {
                "person": iv.person,
                "start": iv.start,
                "end": iv.end,
                "source_track": iv.source_track,
                "via": iv.via,
            }
            for iv in timeline
        ],
    }
    store.append_ledger(entry)
    return entry


def query_delivery(store: GuideStore, issue_id: int, station_id: str) -> dict:
    """用站号和发放号查回真实收到过的片段；未播过则明确说未兑现。"""
    loan = load_loan(store, issue_id)
    _clip_record(loan, station_id)  # 走错路同样拒绝
    entries = store.ledger_for(issue_id, station_id)
    if not entries:
        return {
            "issue_id": issue_id,
            "station_id": station_id,
            "delivered": False,
            "message": f"发放号 {issue_id} 在 {station_id} 尚无播放记录",
        }
    return {"delivered": True, **entries[0]}


def simulate_walk(store: GuideStore, issue_id: int) -> list[dict]:
    """按包内路线顺序逐站播放，返回每站的真实播放账。"""
    loan = load_loan(store, issue_id)
    route = store.get_route(loan.route_id)
    return [play_station(store, issue_id, sid) for sid in route.stations]


def timeline_of(store: GuideStore, issue_id: int, station_id: str) -> list[AudibleInterval]:
    """直接查看某站快照渲染出的人声时码（不落账，用于演示消音结果）。"""
    loan = load_loan(store, issue_id)
    clip = _clip_record(loan, station_id)
    segments = [store.segments[sid] for sid in clip["included_segments"]]
    return render_timeline(segments, frozenset(clip["muted_tracks"]))
