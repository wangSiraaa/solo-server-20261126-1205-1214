"""端到端场景演示：四站、十声段标记上的完整营业流程。

运行：python -m soundguide.demo
每个场景都带断言，失败即非零退出。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .loans import checkout
from .models import PlanRequest
from .player import play_station, query_delivery, simulate_walk
from .planner import plan_route
from .rendering import person_audible_intervals
from .store import GuideError, GuideStore

MARKERS = Path(__file__).resolve().parent.parent / "data" / "markers.json"

# 收听日 2026-11-28 为周六（开放日），借用至周日归还
LISTEN_DAY = "2026-11-28"
LOAN_FROM = "2026-11-26"
LOAN_TO = "2026-11-29"
SHORT_ROUTE = "R1"  # T1 望江台 -> T2 河湾（拐弯）-> T3 柳岸


def _req(store: GuideStore, *, mutes=frozenset(), budget=float("inf"),
         require_original=None, visitor="V001", mode="headphone") -> PlanRequest:
    return PlanRequest(
        visitor_id=visitor,
        route_id=SHORT_ROUTE,
        mode=mode,
        listen_day=LISTEN_DAY,
        loan_from=LOAN_FROM,
        loan_to=LOAN_TO,
        headphone_budget_s=budget,
        mutes=frozenset(mutes),
        require_original_station=require_original,
    )


def _p_intervals(store, loan, station="T2"):
    """从借出包快照渲染河湾站，返回游客实际听到 P 的时码段。"""
    clip = next(c for c in loan.clips if c["station_id"] == station)
    segs = [store.segments[sid] for sid in clip["included_segments"]]
    return person_audible_intervals(segs, frozenset(clip["muted_tracks"]), "P")


def _heading(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = GuideStore(MARKERS, Path(tmp) / "state.json")

        # ---------------------------------------------------------------- A
        _heading("A. 基准行程：完整许可 + 充足时长 + 短路，河湾必须是 P 的原声")
        req = _req(store, require_original="T2")
        plan = plan_route(store, req)
        assert plan.ok, plan.reasons
        t2 = plan.clip("T2")
        assert t2.narration_segment_id == "S05" and not t2.uses_fallback, "河湾必须交付 P 的原声 S05"
        assert plan.clip("T1").narration_holder == "P"
        assert plan.clip("T3").narration_holder == "R"
        loan = checkout(store, req)
        print(f"基准包已签出：发放号 #{loan.issue_id}，总时长 {loan.total_duration:.0f}s")

        # 小样明示：只关 P 的麦轨，音乐轨 2-4 秒仍把 P 带出来
        mic_only = plan_route(store, _req(store, mutes={"MIC-P"})).clip("T2")
        segs = [store.segments[s] for s in mic_only.included_segments]
        leaked = person_audible_intervals(segs, frozenset({"MIC-P"}), "P")
        assert len(leaked) == 1 and (leaked[0].start, leaked[0].end) == (2.0, 4.0)
        print(f"[小样] 仅静音 MIC-P 后，河湾仍可闻 P：{leaked[0].start}-{leaked[0].end}s "
              f"（via=leak，源轨 MIC-MUSIC）——单独关麦无效")

        walk = simulate_walk(store, loan.issue_id)
        assert [e["station_id"] for e in walk] == ["T1", "T2", "T3"]
        t2_heard = _p_intervals(store, loan)
        assert {(iv.start, iv.end) for iv in t2_heard} == {(1.0, 10.0), (2.0, 4.0)}
        print(f"[逐站播放] 河湾实际听到 P 的时码："
              f"{[(iv.start, iv.end, iv.via) for iv in t2_heard]}")

        # ---------------------------------------------------------------- B
        _heading("B. 仅撤回 P 在河湾的许可：试配允许静音/备选，重放不得带出这两秒人声")
        store.withdraw_license("L-P-T2")
        blocked = plan_route(store, _req(store, require_original="T2"))
        assert not blocked.ok
        assert any("原声" in r for r in blocked.reasons)
        print("原声委托无法再兑现，原因：")
        for r in blocked.reasons:
            print("  -", r)

        # 只允许关 MIC-P：漏入仍在 → 无许可 → 失败，并明确指出漏入无法消除
        no_mute_help = plan_route(store, _req(store, mutes={"MIC-P"}))
        assert not no_mute_help.ok
        assert any("漏入" in r and "MIC-MUSIC" in r for r in no_mute_help.reasons)
        print("只允许静音 MIC-P 时：", "；".join(no_mute_help.reasons))

        # 允许静音音乐轨并接受备选旁白 → 可签出，且 P 完全不可闻
        fallback_req = _req(store, mutes={"MIC-MUSIC"})
        loan2 = checkout(store, fallback_req)
        t2b = next(c for c in loan2.clips if c["station_id"] == "T2")
        assert t2b["uses_fallback"] and t2b["narration_segment_id"] == "S06"
        assert _p_intervals(store, loan2) == [], "重放结果不能带出 2-4 秒人声"
        t1b = next(c for c in loan2.clips if c["station_id"] == "T1")
        assert t1b["narration_segment_id"] == "S02", "T1 的 P 许可未撤回，旁白照常保留"
        print(f"备选包 #{loan2.issue_id}：河湾改用 Q 的备选旁白 S06，MIC-MUSIC 整轨静音；"
              f"河湾可闻者={t2b['audible_persons']}，P 时码为空；"
              f"T1 许可未受波及，仍播 S02")

        # ---------------------------------------------------------------- C
        _heading("C. 恢复 P 许可，仅关闭通向河湾的通路：原路线不能交")
        store.restore_license("L-P-T2")
        store.close_edge("T1", "T2", reason="河湾步道施工")
        rerouted = plan_route(store, _req(store, require_original="T2"))
        assert not rerouted.ok
        assert any("T1->T2" in r for r in rerouted.reasons)
        try:
            checkout(store, _req(store, require_original="T2"))
            raise AssertionError("改道后不应能签出原路线")
        except GuideError as exc:
            print("签出被拒：", exc)
        store.reopen_edge("T1", "T2")
        assert plan_route(store, _req(store, require_original="T2")).ok
        print("通路恢复后，原路线重新可签出")

        # ---------------------------------------------------------------- D
        _heading("D. 站号+发放号回查；走错路与重领都不能把别站声音算作已讲")
        # T4 不在 R1 包内：拒绝播放，不产生账目
        try:
            play_station(store, loan.issue_id, "T4")
            raise AssertionError("路线外站点必须拒绝")
        except GuideError as exc:
            print("走错路拒绝：", exc)
        try:
            query_delivery(store, loan.issue_id, "T4")
            raise AssertionError("路线外站点回查必须拒绝")
        except GuideError as exc:
            print("走错路回查拒绝：", exc)
        assert store.ledger_for(loan.issue_id, "T4") == []
        # 河湾已讲的是 S05，不会记到柳岸头上
        t3_q = query_delivery(store, loan.issue_id, "T3")
        assert t3_q["delivered"] and t3_q["narration_segment_id"] == "S08"
        t2_q = query_delivery(store, loan.issue_id, "T2")
        assert t2_q["narration_segment_id"] == "S05"
        # 重复播放幂等：仍只有一条账，片段不变
        again = play_station(store, loan.issue_id, "T2")
        assert again["narration_segment_id"] == "S05"
        assert len(store.ledger_for(loan.issue_id, "T2")) == 1
        # 重领同一包：同一发放号、同一指纹，不新发片段
        reloan = checkout(store, _req(store, require_original="T2"))
        assert reloan.issue_id == loan.issue_id and reloan.fingerprint == loan.fingerprint
        print(f"重领返回原发放号 #{reloan.issue_id}（指纹 {reloan.fingerprint}）；"
              f"河湾账目仍为 S05，柳岸为 S08，互不串账")

        # ---------------------------------------------------------------- E
        _heading("E. 离线包不可远程删除 + 耳机预算 + 外放/耳机许可差异")
        # 已借出的基准包不受撤回影响：快照里 P 仍在，这正是离线包的真实语义
        store.withdraw_license("L-P-T2")
        old_t2 = query_delivery(store, loan.issue_id, "T2")
        assert old_t2["narration_segment_id"] == "S05"
        assert _p_intervals(store, loan), "已借出的包不能被远程删声"
        print(f"撤回后已借出的 #{loan.issue_id} 仍按快照播放河湾 S05；"
              "新签出才受新状态约束（离线不假定远程删除）")
        store.restore_license("L-P-T2")

        # 耳机预算过紧：明确报预算而非静默砍旁白
        tight = plan_route(store, _req(store, budget=10))
        assert not tight.ok and any("预算" in r for r in tight.reasons)
        print("预算不足：", "；".join(tight.reasons))

        # R 只授耳机：广场外放模式签不出 T3
        speaker = plan_route(store, _req(store, mode="speaker"))
        assert not speaker.ok
        assert any("T3" in r and "speaker" in r for r in speaker.reasons)
        print("广场外放被 R 的耳机专属许可拦住：", "；".join(speaker.reasons))

        # 借用期限超出许可承诺期：不能签出（不允许赌路中补授权）
        over = plan_route(
            store,
            PlanRequest("V002", SHORT_ROUTE, "headphone", LISTEN_DAY,
                        LOAN_FROM, "2028-01-15"),
        )
        assert not over.ok and any("归还日" in r for r in over.reasons)
        print("归还日超出许可有效期：", "；".join(over.reasons))

    _heading("全部场景断言通过 ✓")


if __name__ == "__main__":
    main()
