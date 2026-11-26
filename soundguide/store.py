"""本地存储：加载标记文件与运行态（许可撤回、通路关闭、借出账、播放账）。

标记文件 data/markers.json 为志愿者/编辑维护的只读基础数据；
运行态 data/state.json 记录后台营业期间发生的真实变更。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .models import Leak, License, Route, Segment, Slot, Station

ALL = "*"


class GuideError(Exception):
    """导览后台业务错误（无法兑现、走错路、越权播放等）。"""


def weekday_of(day_iso: str) -> str:
    return date.fromisoformat(day_iso).strftime("%A").lower()


class GuideStore:
    def __init__(self, markers_path: str | Path, state_path: str | Path | None = None):
        self.markers_path = Path(markers_path)
        self.state_path = Path(state_path) if state_path else self.markers_path.parent / "state.json"
        raw = json.loads(self.markers_path.read_text(encoding="utf-8"))
        self.global_open_days: tuple[str, ...] = tuple(raw["open_days"])
        self.stations: dict[str, Station] = {
            s["id"]: Station(
                id=s["id"],
                name=s["name"],
                open_days=tuple(s["open_days"]),
                required_slot=Slot(**s["required_slot"]) if s.get("required_slot") else None,
            )
            for s in raw["stations"]
        }
        self.routes: dict[str, Route] = {
            r["id"]: Route(id=r["id"], name=r["name"], stations=tuple(r["stations"]))
            for r in raw["routes"]
        }
        self.segments: dict[str, Segment] = {
            s["id"]: Segment(
                id=s["id"],
                station_id=s["station_id"],
                kind=s["kind"],
                track=s["track"],
                holder=s["holder"],
                start=float(s["start"]),
                end=float(s["end"]),
                leaks=tuple(Leak(**lk) for lk in s.get("leaks", [])),
            )
            for s in raw["segments"]
        }
        self.licenses: dict[str, License] = {
            l["id"]: License(
                id=l["id"],
                person=l["person"],
                stations=tuple(l["stations"]),
                open_days=tuple(l["open_days"]),
                modes=tuple(l["modes"]),
                valid_from=l["valid_from"],
                valid_to=l["valid_to"],
            )
            for l in raw["licenses"]
        }
        self._state = self._load_state()

    # ---------- 运行态 ----------
    def _load_state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"withdrawn_licenses": [], "closed_edges": [], "loans": [], "ledger": [], "seq": 0}

    def save(self) -> None:
        self.state_path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @property
    def withdrawn_licenses(self) -> set[str]:
        return set(self._state["withdrawn_licenses"])

    def withdraw_license(self, license_id: str) -> None:
        if license_id not in self.licenses:
            raise GuideError(f"未知许可证：{license_id}")
        if license_id not in self._state["withdrawn_licenses"]:
            self._state["withdrawn_licenses"].append(license_id)
        self.save()

    def restore_license(self, license_id: str) -> None:
        if license_id in self._state["withdrawn_licenses"]:
            self._state["withdrawn_licenses"].remove(license_id)
        self.save()

    def close_edge(self, station_a: str, station_b: str, reason: str) -> None:
        edge = {"a": station_a, "b": station_b, "reason": reason}
        if edge not in self._state["closed_edges"]:
            self._state["closed_edges"].append(edge)
        self.save()

    def reopen_edge(self, station_a: str, station_b: str) -> None:
        self._state["closed_edges"] = [
            e for e in self._state["closed_edges"]
            if not (e["a"] == station_a and e["b"] == station_b)
        ]
        self.save()

    def edge_open(self, station_a: str, station_b: str) -> bool:
        return not any(
            e["a"] == station_a and e["b"] == station_b for e in self._state["closed_edges"]
        )

    # ---------- 查标记 ----------
    def segments_for(self, station_id: str) -> list[Segment]:
        return [s for s in self.segments.values() if s.station_id == station_id]

    def get_station(self, station_id: str) -> Station:
        if station_id not in self.stations:
            raise GuideError(f"未知站点：{station_id}")
        return self.stations[station_id]

    def get_route(self, route_id: str) -> Route:
        if route_id not in self.routes:
            raise GuideError(f"未知路线：{route_id}")
        return self.routes[route_id]

    def active_licenses_for(self, person: str) -> list[License]:
        return [
            lic for lic in self.licenses.values()
            if lic.person == person and lic.id not in self.withdrawn_licenses
        ]

    # ---------- 借出账与播放账 ----------
    def next_seq(self) -> int:
        self._state["seq"] += 1
        self.save()
        return self._state["seq"]

    def save_loan(self, loan: dict) -> None:
        self._state["loans"].append(loan)
        self.save()

    def get_loan(self, issue_id: int) -> dict | None:
        return next((l for l in self._state["loans"] if l["issue_id"] == issue_id), None)

    def mark_loan_returned(self, issue_id: int) -> None:
        loan = self.get_loan(issue_id)
        if loan:
            loan["returned"] = True
            self.save()

    def append_ledger(self, entry: dict) -> None:
        self._state["ledger"].append(entry)
        self.save()

    def ledger_for(self, issue_id: int, station_id: str | None = None) -> list[dict]:
        return [
            e for e in self._state["ledger"]
            if e["issue_id"] == issue_id and (station_id is None or e["station_id"] == station_id)
        ]
