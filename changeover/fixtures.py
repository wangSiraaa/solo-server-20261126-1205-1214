"""Baseline demo floor from the brief.

Zones (<= 6):
  east  东门   entrance  (screen: sc-east)
  west  西门   entrance  (screen: sc-west)
  hall  走廊   corridor  (screen: sc-hall)
  room  讲室   room      (screen: sc-room)
  desk  受理台 reception (screen: sc-desk)

Doors (<= 6) with explicit travel direction:
  d-east   east <-> hall   both
  d-west   west <-> hall   both
  d-side   hall <-> room   both      (the "侧门" corridor sign points through)
  d-desk   hall <-> desk   both

Two activities:
  poetry   诗会   zones = {room}   allow_seen = True   host = 诗社主持人
  lecture  讲座   zones = {room}   allow_seen = False  host = 讲座主持人  (old tenancy)

Baseline confirmed state described in the brief:
  * east entrance screen 已显示 "先入走廊，走 d-east"
  * corridor screen      真显示 "从侧门 d-side 去讲室"
  * lecture room handed over to the poetry society  => 东门可为诗会开放
"""

from .engine import build_engine


def build_baseline(path=":memory:"):
    eng = build_engine(path)
    e = eng

    e.add_zone("east", "东门", "entrance")
    e.add_zone("west", "西门", "entrance")
    e.add_zone("hall", "走廊", "corridor")
    e.add_zone("room", "讲室", "room")
    e.add_zone("desk", "受理台", "reception")

    e.add_door("d-east", "east", "hall", "both")
    e.add_door("d-west", "west", "hall", "both")
    e.add_door("d-side", "hall", "room", "both")
    e.add_door("d-desk", "hall", "desk", "both")

    e.add_screen("sc-east", "east")
    e.add_screen("sc-west", "west")
    e.add_screen("sc-hall", "hall")
    e.add_screen("sc-room", "room")
    e.add_screen("sc-desk", "desk")

    # old tenancy first, then the poetry society takes the room
    e.register_activity("lecture", "讲座", ["room"], allow_seen=True, host="讲座主持人")
    e.register_activity("poetry", "诗会", ["room"], allow_seen=True, host="诗社主持人")

    # field crew reports the passages actually open
    e.door_field("d-east", True)
    e.door_field("d-side", True)

    # --- baseline confirmed signage (per the brief) ------------------------
    # East: "first into the corridor, via d-east"
    _display(e, "sc-east", "poetry", "d-east")
    # Corridor: "from the side door d-side into the lecture room"
    _display(e, "sc-hall", "poetry", "d-side")

    # The room has been handed to the poetry society, so east may open for poetry.
    opening = e.open_direct("poetry", "sc-east")
    return eng, opening


def _display(eng, sid, target, door):
    """Backend sends a copy and the device confirms the display."""
    v = eng.send_copy(sid, target, door)["sent_version"]
    eng.device_state(sid, "received", v)
    eng.device_state(sid, "buffered", v)
    eng.device_state(sid, "displayed", v)
    return v
