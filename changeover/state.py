"""In-memory floor state, rebuilt deterministically from an append-only event log.

The state is the single source of truth for "what the visitor could actually see
and walk right now".  Everything a visitor-facing screen shows is derived from
*confirmed* display events -- a copy that the backend merely sent is never enough
for a screen to count as showing something.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

RECEPTION = "__reception__"
MAX_ZONES = 6
MAX_DOORS = 6


class StateError(Exception):
    pass


@dataclass
class Zone:
    id: str
    name: str
    kind: str  # entrance | corridor | room | reception


@dataclass
class Door:
    id: str
    a: str
    b: str
    direction: str  # both | a_to_b | b_to_a
    open: bool = False

    def other(self, zone: str) -> str:
        if zone == self.a:
            return self.b
        if zone == self.b:
            return self.a
        raise StateError("door %s does not touch zone %s" % (self.id, zone))

    def can_traverse(self, from_zone: str) -> bool:
        if not self.open:
            return False
        if from_zone not in (self.a, self.b):
            return False
        if self.direction == "both":
            return True
        if self.direction == "a_to_b":
            return from_zone == self.a
        if self.direction == "b_to_a":
            return from_zone == self.b
        return False


@dataclass
class Screen:
    id: str
    zone: str
    # What the backend has sent (desired) and how far it got.
    sent_version: int = 0
    # version -> (target, door) for every copy ever sent
    copies: Dict[int, tuple] = field(default_factory=dict)
    received_version: int = 0
    buffered_version: int = 0
    # What the glass is *actually* showing, i.e. the last confirmed display.
    display_version: int = 0
    display_target: Optional[str] = None
    display_door: Optional[str] = None
    masked: bool = False
    online: bool = True

    @property
    def showing(self) -> bool:
        return (not self.masked) and self.display_version > 0 and self.display_target is not None


@dataclass
class Activity:
    id: str
    name: str
    zones: Set[str]
    allow_seen: bool
    host: str
    status: str = "active"  # active | ended


@dataclass
class Changeover:
    id: str
    mode: str  # whole_hall | successive
    from_activity: str
    to_activity: str
    entrance_screens: List[str]
    signage_screens: List[str]
    doors_to_open: List[str] = field(default_factory=list)
    doors_to_partition: List[str] = field(default_factory=list)
    field_confirmed_doors: Set[str] = field(default_factory=set)
    phase: str = "started"
    opening_number: Optional[int] = None
    started_ts: float = 0.0


@dataclass
class Opening:
    number: int
    changeover: Optional[str]
    activity: str
    entrance_screen: str
    route: List[dict]
    final_zone: Optional[str]
    created_ts: float
    status: str = "active"  # active | retracted
    retracted_reason: Optional[str] = None
    retracted_ts: Optional[float] = None


@dataclass
class State:
    zones: Dict[str, Zone] = field(default_factory=dict)
    doors: Dict[str, Door] = field(default_factory=dict)
    screens: Dict[str, Screen] = field(default_factory=dict)
    activities: Dict[str, Activity] = field(default_factory=dict)
    changeovers: Dict[str, Changeover] = field(default_factory=dict)
    openings: Dict[int, Opening] = field(default_factory=dict)
    receipts: List[dict] = field(default_factory=list)
    next_opening: int = 1
    applied: int = 0

    # ---- lookups -----------------------------------------------------------
    def zone_screen(self) -> Dict[str, Screen]:
        out: Dict[str, Screen] = {}
        for s in self.screens.values():
            out.setdefault(s.zone, s)
        return out

    def entrance_screens(self) -> List[Screen]:
        return [
            s
            for s in self.screens.values()
            if s.zone in self.zones and self.zones[s.zone].kind == "entrance"
        ]

    # ---- walking -----------------------------------------------------------
    def walk(self, entrance_screen: str, target: str, overrides: Optional[dict] = None):
        """Follow the *actually displayed* guidance from one screen to a target.

        ``overrides`` lets a caller ask "what if this screen showed X" without
        mutating state; used to test whether a change would break a path.

        Returns a dict: {ok, reason, route, final_zone}.
        """
        overrides = overrides or {}
        screen = self.screens.get(entrance_screen)
        if screen is None:
            return {"ok": False, "reason": "unknown_screen", "route": [], "final_zone": None}

        if target == RECEPTION:
            activity = None
            target_zones: Set[str] = {z for z, zz in self.zones.items() if zz.kind == "reception"}
            if not target_zones:
                return {"ok": False, "reason": "no_reception_zone", "route": [], "final_zone": None}
        else:
            activity = self.activities.get(target)
            if activity is None:
                return {"ok": False, "reason": "unknown_activity", "route": [], "final_zone": None}
            target_zones = activity.zones
            if not target_zones:
                return {"ok": False, "reason": "activity_has_no_zone", "route": [], "final_zone": None}

        def eff(sid):
            s = self.screens[sid]
            ov = overrides.get(sid)
            if ov is None:
                return s.masked, s.display_version > 0, s.display_target, s.display_door
            masked = ov.get("masked", s.masked)
            target_ = ov.get("target", s.display_target)
            door = ov.get("door", s.display_door)
            return masked, target_ is not None, target_, door

        route: List[dict] = []
        visited_screens: Set[str] = set()
        visited_zones: Set[str] = set()
        cur = screen
        zone = screen.zone
        visited_zones.add(zone)

        # Already inside the target?
        if zone in target_zones and cur.id not in overrides:
            return {"ok": True, "reason": "already_there", "route": [], "final_zone": zone}

        while True:
            if cur.id in visited_screens:
                return {"ok": False, "reason": "cycle_without_exit", "route": route, "final_zone": None}
            visited_screens.add(cur.id)

            masked, shown, dtarget, ddoor = eff(cur.id)
            if masked or not shown:
                return {"ok": False, "reason": "screen_hidden:%s" % cur.id, "route": route, "final_zone": None}
            if dtarget != target:
                return {"ok": False, "reason": "target_mismatch:%s" % cur.id, "route": route, "final_zone": None}

            z = self.zones.get(cur.zone)
            if z is None:
                return {"ok": False, "reason": "screen_in_unknown_zone:%s" % cur.id, "route": route, "final_zone": None}
            if activity is not None and not activity.allow_seen and z.kind == "corridor":
                return {"ok": False, "reason": "privacy_leak:%s" % cur.id, "route": route, "final_zone": None}

            door = self.doors.get(ddoor) if ddoor else None
            if door is None:
                return {"ok": False, "reason": "no_door:%s" % cur.id, "route": route, "final_zone": None}
            if not door.can_traverse(cur.zone):
                return {"ok": False, "reason": "door_blocked:%s" % door.id, "route": route, "final_zone": None}

            nxt = door.other(cur.zone)
            route.append(
                {
                    "screen": cur.id,
                    "activity": dtarget,
                    "door": door.id,
                    "from_zone": cur.zone,
                    "to_zone": nxt,
                }
            )
            if nxt in target_zones:
                return {"ok": True, "reason": "arrived", "route": route, "final_zone": nxt}
            if nxt in visited_zones:
                return {"ok": False, "reason": "cycle_without_exit", "route": route, "final_zone": None}
            visited_zones.add(nxt)

            nxt_screen = self.zone_screen().get(nxt)
            if nxt_screen is None:
                return {"ok": False, "reason": "dead_end_no_screen:%s" % nxt, "route": route, "final_zone": None}
            cur = nxt_screen

    def count_paths(self, target: str, overrides: Optional[dict] = None) -> List[str]:
        """Entrance screens from which ``target`` is currently walkable."""
        out = []
        for s in self.entrance_screens():
            if self.walk(s.id, target, overrides=overrides)["ok"]:
                out.append(s.id)
        return out

    # ---- event application -------------------------------------------------
    def apply(self, ev: dict) -> Optional[dict]:
        """Apply one event.  Returns a note dict if the event was ignored."""
        t = ev["type"]
        d = ev.get("data", {})
        self.applied += 1

        if t == "zone.added":
            if d["id"] in self.zones:
                raise StateError("duplicate zone")
            if len(self.zones) >= MAX_ZONES:
                raise StateError("at most %d zones" % MAX_ZONES)
            self.zones[d["id"]] = Zone(d["id"], d.get("name", d["id"]), d.get("kind", "room"))

        elif t == "door.added":
            if d["id"] in self.doors:
                raise StateError("duplicate door")
            if len(self.doors) >= MAX_DOORS:
                raise StateError("at most %d doors" % MAX_DOORS)
            if d["a"] not in self.zones or d["b"] not in self.zones:
                raise StateError("door endpoints must be known zones")
            self.doors[d["id"]] = Door(
                d["id"], d["a"], d["b"], d.get("direction", "both"), bool(d.get("open", False))
            )

        elif t == "screen.added":
            if d["id"] in self.screens:
                raise StateError("duplicate screen")
            if d["zone"] not in self.zones:
                raise StateError("screen zone must be known")
            if self.zone_screen().get(d["zone"]) is not None:
                raise StateError("one screen per zone")
            self.screens[d["id"]] = Screen(d["id"], d["zone"])

        elif t == "activity.registered":
            self.activities[d["id"]] = Activity(
                d["id"],
                d.get("name", d["id"]),
                set(d.get("zones", [])),
                bool(d.get("allow_seen", True)),
                d.get("host", ""),
                "active",
            )

        elif t == "activity.ended":
            if d["id"] in self.activities:
                self.activities[d["id"]].status = "ended"

        elif t == "door.field":
            if d["id"] not in self.doors:
                raise StateError("unknown door")
            self.doors[d["id"]].open = bool(d["open"])

        elif t == "screen.copy_sent":
            scr = self.screens.get(d["id"])
            if scr is None:
                raise StateError("unknown screen")
            v = int(d["version"])
            if v <= scr.sent_version:
                return {"ignored": "copy_version_not_increasing", "version": v}
            scr.sent_version = v
            scr.copies[v] = (d["target"], d["door"])

        elif t == "screen.received":
            scr = self.screens.get(d["id"])
            if scr is None:
                raise StateError("unknown screen")
            v = int(d["version"])
            if v > scr.received_version:
                scr.received_version = v

        elif t == "screen.buffered":
            scr = self.screens.get(d["id"])
            if scr is None:
                raise StateError("unknown screen")
            v = int(d["version"])
            if v > scr.buffered_version:
                scr.buffered_version = v

        elif t == "screen.displayed":
            scr = self.screens.get(d["id"])
            if scr is None:
                raise StateError("unknown screen")
            v = int(d["version"])
            # The screen can only display a copy it actually received & buffered.
            if v > scr.buffered_version:
                return {"ignored": "displayed_before_buffered", "version": v}
            if v not in scr.copies:
                return {"ignored": "unknown_copy_version", "version": v}
            if v < scr.display_version:
                # A late, older "I am showing v1" after v2 is already on the glass.
                return {"ignored": "stale_display", "version": v}
            if v == scr.display_version:
                return {"ignored": "duplicate_display", "version": v}
            target, door = scr.copies[v]
            scr.display_version = v
            scr.display_target = target
            scr.display_door = door

        elif t == "screen.masked":
            scr = self.screens.get(d["id"])
            if scr is None:
                raise StateError("unknown screen")
            scr.masked = bool(d["masked"])

        elif t == "screen.online":
            scr = self.screens.get(d["id"])
            if scr is None:
                raise StateError("unknown screen")
            scr.online = bool(d["online"])

        elif t == "changeover.started":
            self.changeovers[d["id"]] = Changeover(
                d["id"],
                d["mode"],
                d["from_activity"],
                d["to_activity"],
                list(d.get("entrance_screens", [])),
                list(d.get("signage_screens", [])),
                list(d.get("doors_to_open", [])),
                list(d.get("doors_to_partition", [])),
                started_ts=ev.get("ts", 0.0),
            )

        elif t == "changeover.phase":
            co = self.changeovers.get(d["id"])
            if co:
                co.phase = d["phase"]

        elif t == "changeover.field_confirmed":
            co = self.changeovers.get(d["id"])
            if co:
                co.field_confirmed_doors.add(d["door"])

        elif t == "changeover.opened":
            co = self.changeovers.get(d["id"])
            if co:
                co.opening_number = d["opening_number"]
                co.phase = "opened"

        elif t == "opening.created":
            n = int(d["number"])
            self.openings[n] = Opening(
                n,
                d.get("changeover"),
                d["activity"],
                d["entrance_screen"],
                list(d.get("route", [])),
                d.get("final_zone"),
                ev.get("ts", 0.0),
            )
            self.next_opening = max(self.next_opening, n + 1)

        elif t == "opening.retracted":
            n = int(d["number"])
            if n in self.openings and self.openings[n].status == "active":
                self.openings[n].status = "retracted"
                self.openings[n].retracted_reason = d.get("reason")
                self.openings[n].retracted_ts = ev.get("ts")

        elif t == "receipt.issued":
            self.receipts.append(d)

        else:
            raise StateError("unknown event type %s" % t)

        return None
