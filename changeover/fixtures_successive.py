"""A second layout for the successive (per-entrance) handover demo.

Six zones, six doors.  The point of this layout is that the *west* entrance has
its **own direct** door to the old activity's room, so west can keep a complete
walkable route to the old activity while the *east* entrance is handed over on
its own.

Zones:
  east  东门      entrance   sc-east
  west  西门      entrance   sc-west
  hall  走廊      corridor   sc-hall
  old   旧讲室    room       sc-old     <- old activity 讲座
  new   新讲室    room       sc-new     <- new activity 诗会
  desk  受理台    reception  sc-desk

Doors:
  d-east    east <-> hall
  d-west    west <-> hall
  d-e-old   east <-> old      (east's independent old route)
  d-w-old   west <-> old      (west's independent old route  <-- the lifeline)
  d-h-old   hall <-> old
  d-h-new   hall <-> new
"""

from .engine import build_engine


def build_successive(path=":memory:"):
    e = build_engine(path)

    e.add_zone("east", "东门", "entrance")
    e.add_zone("west", "西门", "entrance")
    e.add_zone("hall", "走廊", "corridor")
    e.add_zone("old", "旧讲室", "room")
    e.add_zone("new", "新讲室", "room")
    e.add_zone("desk", "受理台", "reception")

    e.add_door("d-east", "east", "hall", "both")
    e.add_door("d-west", "west", "hall", "both")
    e.add_door("d-e-old", "east", "old", "both")
    e.add_door("d-w-old", "west", "old", "both")
    e.add_door("d-h-old", "hall", "old", "both")
    e.add_door("d-h-new", "hall", "new", "both")

    e.add_screen("sc-east", "east")
    e.add_screen("sc-west", "west")
    e.add_screen("sc-hall", "hall")
    e.add_screen("sc-old", "old")
    e.add_screen("sc-new", "new")
    e.add_screen("sc-desk", "desk")

    e.register_activity("lecture", "讲座", ["old"], allow_seen=True, host="讲座主持人")
    e.register_activity("poetry", "诗会", ["new"], allow_seen=True, host="诗社主持人")

    for d in ("d-east", "d-west", "d-e-old", "d-w-old", "d-h-old", "d-h-new"):
        e.door_field(d, True)

    # Baseline: everyone still walks to the OLD activity.
    _display(e, "sc-east", "lecture", "d-e-old")
    _display(e, "sc-west", "lecture", "d-w-old")
    _display(e, "sc-hall", "lecture", "d-h-old")
    # Certify the baseline old-activity openings from both entrances.
    e.open_direct("lecture", "sc-east")
    e.open_direct("lecture", "sc-west")
    return e


def display(e, sid, target, door):
    v = e.send_copy(sid, target, door)["sent_version"]
    e.device_state(sid, "received", v)
    e.device_state(sid, "buffered", v)
    e.device_state(sid, "displayed", v)
    return v


_display = display
