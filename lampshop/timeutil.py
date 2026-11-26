"""Time helpers.

All persisted timestamps are normalised to UTC and formatted as
``YYYY-MM-DDTHH:MM:SSZ`` so that lexicographic comparison equals
chronological comparison.  Business dates are plain ``YYYY-MM-DD``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def parse_dt(value: str) -> datetime:
    """Parse an ISO-8601-ish timestamp into an aware UTC datetime."""
    if not isinstance(value, str):
        raise ValueError(f"expected a timestamp string, got {type(value)!r}")
    s = value.strip()
    if not s:
        raise ValueError("empty timestamp")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Accept a space separator as well as 'T'.
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_dt(value: str) -> str:
    return fmt_dt(parse_dt(value))


def now_str() -> str:
    return fmt_dt(datetime.now(timezone.utc))


def parse_date(value: str) -> date:
    return date.fromisoformat(value.strip())


def norm_date(value: str) -> str:
    return parse_date(value).isoformat()


def date_of(timestamp: str) -> str:
    return parse_dt(timestamp).date().isoformat()


def overlap_minutes(a_start: str, a_end: str, b_start: str, b_end: str) -> float:
    start = max(parse_dt(a_start), parse_dt(b_start))
    end = min(parse_dt(a_end), parse_dt(b_end))
    if end <= start:
        return 0.0
    return (end - start).total_seconds() / 60.0


def duration_minutes(start: str, end: str) -> float:
    return max(0.0, (parse_dt(end) - parse_dt(start)).total_seconds() / 60.0)


def dt_leq(a: str, b: str) -> bool:
    """True when timestamp ``a`` is at or before timestamp ``b``."""
    return parse_dt(a) <= parse_dt(b)
