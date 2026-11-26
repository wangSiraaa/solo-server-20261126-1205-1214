"""领域模型：标记文件里的站、路线、声段、许可。

所有时间均为秒（本地演示用，不采集真实声音）。
一个声段属于一条轨（track）；漏入（leak）表示该轨在某时码段
还带出了另一个人的人声，单独关掉当事人的麦轨并不能消除漏入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Leak:
    """现场轨漏入的人声：即使本人麦轨静音，这段仍会被听到。"""

    person: str
    start: float
    end: float
    source_track: str
    note: str = ""

    def overlaps(self, start: float, end: float) -> bool:
        return self.start < end and start < self.end


@dataclass(frozen=True)
class Segment:
    id: str
    station_id: str
    kind: str  # narration | music | ambience
    track: str
    holder: str  # 许可证当事人
    start: float
    end: float
    leaks: tuple[Leak, ...] = field(default_factory=tuple)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Slot:
    """站点必讲旁白位：优先原声，必要时可换备选旁白。"""

    id: str
    prefer: str
    fallback: Optional[str]


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    open_days: tuple[str, ...]
    required_slot: Optional[Slot]


@dataclass(frozen=True)
class Route:
    id: str
    name: str
    stations: tuple[str, ...]


@dataclass(frozen=True)
class License:
    """录音当事人授予的收听许可。

    "*" 表示全部站点或全部开放日。许可只在 [valid_from, valid_to] 内成立。
    speaker=广场外放，headphone=单人耳机点播，两者可以不同。
    """

    id: str
    person: str
    stations: tuple[str, ...]
    open_days: tuple[str, ...]
    modes: tuple[str, ...]
    valid_from: str
    valid_to: str

    def covers_station(self, station_id: str) -> bool:
        return "*" in self.stations or station_id in self.stations

    def covers_day(self, day: str) -> bool:
        return "*" in self.open_days or day in self.open_days

    def covers_mode(self, mode: str) -> bool:
        return mode in self.modes

    def covers_date(self, day_iso: str) -> bool:
        return self.valid_from <= day_iso <= self.valid_to


@dataclass(frozen=True)
class PlanRequest:
    """一次路线试配请求。

    mutes: 允许静音的轨（规划器只可在这个集合内静音）。
    listen_day: 游客实际收听日；loan_to: 离线包归还日，
    许可证必须能承诺到归还日，不能假定路中远程删除。
    """

    visitor_id: str
    route_id: str
    mode: str  # speaker | headphone
    listen_day: str
    loan_from: str
    loan_to: str
    headphone_budget_s: float = float("inf")
    mutes: frozenset[str] = frozenset()
    require_original_station: Optional[str] = None  # 委托指定必须原声的站
