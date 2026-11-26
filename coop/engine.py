"""Half-open interval availability engine.

The compensation unit is one integer minute ``t`` represented by ``[t, t+1)``.
All intervals in this package are half-open ``[start_minute, end_minute)``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def contains(start: int, end: int | None, minute: int) -> bool:
    return start <= minute and (end is None or minute < end)


def _is_promised_at(
    line: sqlite3.Row, commitments: list[sqlite3.Row], minute: int
) -> bool:
    # Actual activation/return bound the contractual promise.
    if minute < line["opened_at"]:
        return False
    if line["returned_at"] is not None and minute >= line["returned_at"]:
        return False

    for interval in commitments:
        if interval["status"] != "active" or interval["kind"] != "promised":
            continue
        if contains(interval["start_minute"], interval["end_minute"], minute):
            return True
    return False


def _is_down_at(outages: list[sqlite3.Row], minute: int) -> bool:
    # Report rows are deliberately not used here: canonical line facts are the
    # single source for availability, so duplicate reports cannot double-count.
    for outage in outages:
        if outage["status"] not in {"open", "repaired"}:
            continue
        if contains(outage["start_minute"], outage["end_minute"], minute):
            return True
    return False


def evaluate_window(
    conn: sqlite3.Connection, customer_id: str, start: int, end: int
) -> dict[str, Any]:
    """Return a minute-level SLA decision for ``[start, end)``.

    Compensation is owed exactly when at least one access point is promised and
    every promised access point is down.  Multiple down reports on the same
    point still produce only one decision row/minute.
    """
    if end < start:
        raise ValueError("window_end must be >= window_start")

    lines = conn.execute(
        "SELECT * FROM access_lines WHERE customer_id = ? ORDER BY id",
        (customer_id,),
    ).fetchall()

    commitments_by_line: dict[str, list[sqlite3.Row]] = {}
    outages_by_line: dict[str, list[sqlite3.Row]] = {}
    for line in lines:
        commitments_by_line[line["id"]] = conn.execute(
            "SELECT * FROM commitment_intervals WHERE line_id = ? ORDER BY start_minute",
            (line["id"],),
        ).fetchall()
        outages_by_line[line["id"]] = conn.execute(
            "SELECT * FROM outages WHERE line_id = ? ORDER BY start_minute",
            (line["id"],),
        ).fetchall()

    minute_rows: list[dict[str, Any]] = []
    owed_minutes = 0

    for minute in range(start, end):
        promised: list[dict[str, str]] = []
        down_promised: list[dict[str, str]] = []

        for line in lines:
            if not _is_promised_at(
                line, commitments_by_line[line["id"]], minute
            ):
                continue
            point = {"line_id": line["id"], "label": line["label"]}
            promised.append(point)
            if _is_down_at(outages_by_line[line["id"]], minute):
                down_promised.append(point)

        if not promised:
            owed = False
            reason = "该分钟没有承诺可用的接入点"
        elif len(down_promised) == len(promised):
            owed = True
            owed_minutes += 1
            reason = "全部承诺接入点均中断"
        else:
            owed = False
            up = len(promised) - len(down_promised)
            reason = f"仍有 {up} 个承诺接入点可用"

        minute_rows.append(
            {
                "minute": minute,
                "interval": f"[{minute},{minute + 1})",
                "promised": promised,
                "down_promised": down_promised,
                "owed": owed,
                "reason": reason,
            }
        )

    owed_intervals = _merge_owed(minute_rows)
    return {
        "customer_id": customer_id,
        "window": [start, end],
        "half_open_note": "所有区间均为 [start,end)；整数 minute 表示 [minute,minute+1)",
        "expected_minutes": owed_minutes,
        "owed_intervals": owed_intervals,
        "minutes": minute_rows,
    }


def _merge_owed(rows: list[dict[str, Any]]) -> list[list[int]]:
    intervals: list[list[int]] = []
    current_start: int | None = None
    previous: int | None = None

    for row in rows:
        minute = row["minute"]
        if row["owed"]:
            if current_start is None:
                current_start = minute
            elif previous is not None and minute != previous + 1:
                intervals.append([current_start, previous + 1])
                current_start = minute
            previous = minute
        elif current_start is not None and previous is not None:
            intervals.append([current_start, previous + 1])
            current_start = None
            previous = None

    if current_start is not None and previous is not None:
        intervals.append([current_start, previous + 1])
    return intervals


def canonical_calculation_json(calculation: dict[str, Any]) -> str:
    return json.dumps(calculation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
