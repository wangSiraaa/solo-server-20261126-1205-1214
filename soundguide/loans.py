"""离线声景包借出：出发站一次性签出，签出后为不可变快照。

承诺规则：
  * 覆盖路线的全部相邻通路与全部站点开放日；
  * 只装入许可证能承诺到【归还日】的声部（收听日与归还日双重校验）；
  * 签出之后当事人再撤回同意、站点再改道，都不能远程改动已借出的包，
    新的许可/通路状态只影响此后的新签出与新重领。
同一游客对同一请求重领同一包时，返回原发放号（幂等），不产生新片段账。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .models import PlanRequest
from .planner import Plan, plan_route
from .store import GuideError, GuideStore


@dataclass
class Loan:
    issue_id: int
    visitor_id: str
    route_id: str
    mode: str
    listen_day: str
    loan_from: str
    loan_to: str
    mutes: frozenset[str]
    clips: list[dict]
    total_duration: float
    fingerprint: str
    returned: bool = False
    plan: Plan | None = field(default=None, compare=False)


def _fingerprint(store: GuideStore, req: PlanRequest, plan: Plan) -> str:
    payload = {
        "visitor": req.visitor_id,
        "route": req.route_id,
        "mode": req.mode,
        "listen_day": req.listen_day,
        "loan_to": req.loan_to,
        "budget": req.headphone_budget_s,
        "mutes": sorted(req.mutes),
        "require_original": req.require_original_station,
        "clips": [
            {
                "station": c.station_id,
                "segments": list(c.included_segments),
                "muted": sorted(c.muted_tracks),
                "narration": c.narration_segment_id,
            }
            for c in plan.clips
        ],
        # 基础数据版本纳入指纹：标记文件变更后重领会得到新发放号
        "markers": store.markers_path.read_bytes().decode("utf-8"),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def checkout(store: GuideStore, req: PlanRequest) -> Loan:
    plan = plan_route(store, req)
    if not plan.ok:
        raise GuideError("无法签出离线包：" + "；".join(plan.reasons))

    fingerprint = _fingerprint(store, req, plan)
    existing = next(
        (l for l in store._state["loans"]
         if l["visitor_id"] == req.visitor_id and l["fingerprint"] == fingerprint),
        None,
    )
    if existing is not None:
        return _to_loan(existing, plan)

    issue_id = store.next_seq()
    record = {
        "issue_id": issue_id,
        "visitor_id": req.visitor_id,
        "route_id": req.route_id,
        "mode": req.mode,
        "listen_day": req.listen_day,
        "loan_from": req.loan_from,
        "loan_to": req.loan_to,
        "allowed_mutes": sorted(req.mutes),
        "require_original_station": req.require_original_station,
        "headphone_budget_s": req.headphone_budget_s,
        "fingerprint": fingerprint,
        "returned": False,
        "clips": [
            {
                "station_id": c.station_id,
                "included_segments": list(c.included_segments),
                "muted_tracks": sorted(c.muted_tracks),
                "narration_segment_id": c.narration_segment_id,
                "narration_holder": c.narration_holder,
                "uses_fallback": c.uses_fallback,
                "audible_persons": sorted(c.audible_persons),
                "duration": c.duration,
            }
            for c in plan.clips
        ],
    }
    store.save_loan(record)
    return _to_loan(record, plan)


def _to_loan(record: dict, plan: Plan | None) -> Loan:
    return Loan(
        issue_id=record["issue_id"],
        visitor_id=record["visitor_id"],
        route_id=record["route_id"],
        mode=record["mode"],
        listen_day=record["listen_day"],
        loan_from=record["loan_from"],
        loan_to=record["loan_to"],
        mutes=frozenset(record.get("allowed_mutes", [])),
        clips=record["clips"],
        total_duration=sum(c["duration"] for c in record["clips"]),
        fingerprint=record["fingerprint"],
        returned=record["returned"],
        plan=plan,
    )


def load_loan(store: GuideStore, issue_id: int) -> Loan:
    record = store.get_loan(issue_id)
    if record is None:
        raise GuideError(f"发放号 {issue_id} 不存在")
    return _to_loan(record, None)
