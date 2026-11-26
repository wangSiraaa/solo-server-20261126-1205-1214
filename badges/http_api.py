"""Pure request handler: ``handle(service, method, path, body) -> (status, json)``.

Keeping the HTTP surface as a pure function makes it trivial to record, replay
and test without opening sockets.  :mod:`badges.server` is a thin socket
wrapper around this function.

Every response is a JSON-serialisable ``(status, dict)``.  Errors raised by the
service are mapped to 4xx; unexpected errors to 5xx.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .model import Theme, default_theme, collapsed_theme
from .planning import SessionSpec
from .service import Service, ServiceError

Response = Tuple[int, Dict[str, Any]]


def _theme_from_body(body: dict) -> Theme:
    if "theme" in body and body["theme"] is not None:
        return Theme.from_json(body["theme"])
    kind = body.get("theme_kind", "default")
    if kind == "collapsed":
        return collapsed_theme()
    if kind == "default":
        return default_theme()
    raise ServiceError(f"未知主题类型 {kind!r}")


def _sessions_from_body(body: dict):
    return [
        SessionSpec(s["id"], s.get("label", ""), tuple(s.get("guests", [])))
        for s in body.get("sessions", [])
    ]


def handle(service: Service, method: str, path: str, body: Optional[dict] = None) -> Response:
    body = body or {}
    parsed = urlparse(path)
    p = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)

    try:
        return _route(service, method.upper(), p, body, query)
    except ServiceError as e:
        return e.status, {"error": str(e)}
    except KeyError as e:
        return 404, {"error": f"缺少字段 {e}"}
    except ValueError as e:
        return 400, {"error": str(e)}


def _route(service: Service, method: str, p: str, body: dict, query) -> Response:
    # --- seasons -----------------------------------------------------
    if p == "/seasons" and method == "GET":
        return 200, {"seasons": service.all_seasons(), "active": service.active_season_id}

    if p == "/seasons" and method == "POST":
        theme = _theme_from_body(body)
        season = service.open_season(theme, body.get("season_id"))
        return 201, {"season_id": season.season_id, "theme": season.theme.name}

    m = re.fullmatch(r"/seasons/([^/]+)", p)
    if m:
        sid = m.group(1)
        if method == "GET":
            return 200, service.season_snapshot(sid)
        if method == "DELETE" or method == "POST":
            service.use_season(sid)
            return 200, {"active": sid}

    m = re.fullmatch(r"/seasons/([^/]+)/theme", p)
    if m and method == "POST":
        theme = _theme_from_body(body)
        season = service.set_theme(theme, m.group(1))
        return 200, {"season_id": season.season_id, "theme": season.theme.name}

    # --- planning / signing -----------------------------------------
    m = re.fullmatch(r"/seasons/([^/]+)/plan", p)
    if m and method == "POST":
        sessions = _sessions_from_body(body)
        result = service.plan(
            sessions,
            allowances=body.get("allowances"),
            simpcasts=body.get("simulcasts"),
            season_id=m.group(1),
        )
        return 200, result.to_json()

    m = re.fullmatch(r"/seasons/([^/]+)/sign", p)
    if m and method == "POST":
        sig = service.sign(
            body["guest"], body["style"], body.get("sessions", []), season_id=m.group(1)
        )
        return 201, sig.to_json()

    m = re.fullmatch(r"/seasons/([^/]+)/signatures", p)
    if m and method == "GET":
        return 200, {"signatures": service.list_signatures(m.group(1))}

    m = re.fullmatch(r"/seasons/([^/]+)/check-pair", p)
    if m and method == "POST":
        season = service.get_season(m.group(1))
        from .render import distinguishable, explain_indistinguishable

        a, b = body["a"], body["b"]
        ok = distinguishable(season.theme, a, b)
        return 200, {
            "distinguishable": ok,
            "explanation": explain_indistinguishable(season.theme, a, b),
        }

    # --- receipts / speeches ----------------------------------------
    m = re.fullmatch(r"/seasons/([^/]+)/receipts", p)
    if m and method == "POST":
        rec = service.issue_receipt(
            body["guest"],
            body["session_id"],
            body.get("speech_numbers", []),
            season_id=m.group(1),
        )
        return 201, rec.to_json()

    m = re.fullmatch(r"/seasons/([^/]+)/receipts/([^/]+)", p)
    if m and method == "GET":
        season = service.get_season(m.group(1))
        rec = season.receipts.get(m.group(2))
        if rec is None:
            raise ServiceError("未知回单", 404)
        return 200, rec.to_json()

    m = re.fullmatch(r"/seasons/([^/]+)/speeches", p)
    if m and method == "POST":
        rec = service.record_speech(
            guest=body.get("guest"),
            style=body.get("style"),
            session_id=body.get("session_id", ""),
            speech_no=body.get("speech_no"),
            receipt_id=body.get("receipt_id"),
            season_id=m.group(1),
        )
        return 201, rec.to_json()

    m = re.fullmatch(r"/seasons/([^/]+)/speeches/(\d+)", p)
    if m and method == "GET":
        rec = service.attribute(int(m.group(2)), season_id=m.group(1))
        return 200, rec.to_json()

    m = re.fullmatch(r"/speeches/(\d+)", p)
    if m and method == "GET":
        rec = service.attribute_any(int(m.group(1)))
        return 200, rec.to_json()

    m = re.fullmatch(r"/seasons/([^/]+)/sessions/start", p)
    if m and method == "POST":
        return 201, service.start_session(
            body["session_id"],
            body["guests"],
            speech_numbers=body.get("speech_numbers"),
            season_id=m.group(1),
        )

    # --- live --------------------------------------------------------
    m = re.fullmatch(r"/seasons/([^/]+)/live/enter", p)
    if m and method == "POST":
        return 200, service.enter_live(
            body["guest"], body["style"], body.get("room", ""), season_id=m.group(1)
        )

    m = re.fullmatch(r"/seasons/([^/]+)/live/leave", p)
    if m and method == "POST":
        return 200, service.leave(body["guest"], season_id=m.group(1))

    m = re.fullmatch(r"/seasons/([^/]+)/live/assign", p)
    if m and method == "POST":
        return 200, service.assign_live(
            body["guests"], body["pools"], body.get("room", ""), season_id=m.group(1)
        )

    m = re.fullmatch(r"/seasons/([^/]+)/live/merge", p)
    if m and method == "POST":
        return 200, service.merge_screen(
            body["room_present"],
            allowance=body.get("allowance"),
            rooms=body.get("rooms", []),
            season_id=m.group(1),
        )

    # --- introspection ----------------------------------------------
    m = re.fullmatch(r"/seasons/([^/]+)/state", p)
    if m and method == "GET":
        return 200, service.season_snapshot(m.group(1))

    m = re.fullmatch(r"/seasons/([^/]+)/events", p)
    if m and method == "GET":
        return 200, {"events": service.get_season(m.group(1)).events}

    if p == "/events" and method == "GET":
        evs = []
        for sid in service.season_order:
            evs.extend(service.seasons[sid].events)
        return 200, {"events": evs}

    if p == "/health":
        return 200, {"ok": True}

    return 404, {"error": f"未知路由 {method} {p}"}
