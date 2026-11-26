"""Business logic for changeovers, openings and visitor walkability.

Two changeover agreements are implemented:

* ``whole_hall``  -- mask every conflicting screen, physically partition, then
  stage the full new signage set before any door opens.
* ``successive`` -- hand each independent entrance over one at a time and never
  kill the last walking route to the old activity until the host has registered
  that the old activity ended.

The invariant that both share: an "opening" (a certified visitor route) is a
snapshot that is only ever created from *confirmed* state and is retracted the
moment the confirmed state stops supporting it.
"""

from collections import deque

from .errors import ApiError
from .state import MAX_DOORS, MAX_ZONES, RECEPTION, StateError

SCREEN_EVENT_STATES = {"received", "buffered", "displayed"}


class Engine:
    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------ utils
    @property
    def state(self):
        return self.store.state

    def _commit(self, specs, reevaluate=True):
        specs = [s for s in specs if s]
        if not specs:
            if reevaluate:
                self._reevaluate_openings()
            return []
        events = self.store.make_events(specs)
        self.store.append(events)
        notes = []
        try:
            for ev in events:
                note = self.state.apply(ev)
                if note:
                    notes.append({"seq": ev["seq"], "note": note})
        except StateError as exc:
            raise ApiError("event rejected: %s" % exc, status=409, code="state_rejected")
        if reevaluate:
            self._reevaluate_openings()
        return notes

    def _require_zone(self, zid):
        if zid not in self.state.zones:
            raise ApiError("unknown zone %r" % zid, status=404, code="unknown_zone")

    def _require_door(self, did):
        if did not in self.state.doors:
            raise ApiError("unknown door %r" % did, status=404, code="unknown_door")
        return self.state.doors[did]

    def _require_screen(self, sid):
        if sid not in self.state.screens:
            raise ApiError("unknown screen %r" % sid, status=404, code="unknown_screen")
        return self.state.screens[sid]

    def _require_activity(self, aid):
        if aid not in self.state.activities:
            raise ApiError("unknown activity %r" % aid, status=404, code="unknown_activity")
        return self.state.activities[aid]

    def _require_changeover(self, cid):
        if cid not in self.state.changeovers:
            raise ApiError("unknown changeover %r" % cid, status=404, code="unknown_changeover")
        return self.state.changeovers[cid]

    # -------------------------------------------------------------- topology
    def add_zone(self, zid, name, kind):
        if zid in self.state.zones:
            raise ApiError("duplicate zone %r" % zid, status=409, code="duplicate_zone")
        if len(self.state.zones) >= MAX_ZONES:
            raise ApiError("at most %d zones" % MAX_ZONES, status=409, code="too_many_zones")
        if kind not in ("entrance", "corridor", "room", "reception"):
            raise ApiError("bad zone kind %r" % kind, code="bad_zone_kind")
        self._commit([{"type": "zone.added", "data": {"id": zid, "name": name, "kind": kind}}])
        return {"zone": zid}

    def add_door(self, did, a, b, direction="both", open_doors=False):
        if did in self.state.doors:
            raise ApiError("duplicate door %r" % did, status=409, code="duplicate_door")
        if len(self.state.doors) >= MAX_DOORS:
            raise ApiError("at most %d doors" % MAX_DOORS, status=409, code="too_many_doors")
        self._require_zone(a)
        self._require_zone(b)
        if a == b:
            raise ApiError("door endpoints must differ", code="bad_door")
        if direction not in ("both", "a_to_b", "b_to_a"):
            raise ApiError("bad direction", code="bad_direction")
        self._commit(
            [
                {
                    "type": "door.added",
                    "data": {"id": did, "a": a, "b": b, "direction": direction, "open": open_doors},
                }
            ],
            reevaluate=False,
        )
        return {"door": did}

    def add_screen(self, sid, zone):
        if sid in self.state.screens:
            raise ApiError("duplicate screen %r" % sid, status=409, code="duplicate_screen")
        self._require_zone(zone)
        if self.state.zone_screen().get(zone) is not None:
            raise ApiError("zone %r already has a screen" % zone, status=409, code="duplicate_screen_zone")
        self._commit([{"type": "screen.added", "data": {"id": sid, "zone": zone}}], reevaluate=False)
        return {"screen": sid}

    def register_activity(self, aid, name, zones, allow_seen, host=""):
        if aid in self.state.activities:
            raise ApiError("duplicate activity %r" % aid, status=409, code="duplicate_activity")
        for z in zones:
            self._require_zone(z)
        self._commit(
            [
                {
                    "type": "activity.registered",
                    "data": {
                        "id": aid,
                        "name": name,
                        "zones": list(zones),
                        "allow_seen": bool(allow_seen),
                        "host": host,
                    },
                }
            ],
            reevaluate=False,
        )
        return {"activity": aid}

    def end_activity(self, aid):
        self._require_activity(aid)
        self._commit([{"type": "activity.ended", "data": {"id": aid}}])
        return {"activity": aid, "status": "ended"}

    # --------------------------------------------------------------- devices
    def door_field(self, did, open_doors, changeover=None):
        self._require_door(did)
        specs = [{"type": "door.field", "data": {"id": did, "open": bool(open_doors)}}]
        if changeover:
            self._require_changeover(changeover)
            specs.append(
                {"type": "changeover.field_confirmed", "data": {"id": changeover, "door": did}}
            )
        self._commit(specs)
        return {"door": did, "open": bool(open_doors)}

    def changeover_field(self, cid, door):
        """Site crew reports a partition / passage physically in place."""
        self._require_changeover(cid)
        self._require_door(door)
        self._commit([{"type": "changeover.field_confirmed", "data": {"id": cid, "door": door}}])
        co = self.state.changeovers[cid]
        return {"changeover": cid, "confirmed_doors": sorted(co.field_confirmed_doors)}

    def send_copy(self, sid, target, door, version=None):
        """Backend hands a new caption to a screen.  This is *not* a display."""
        scr = self._require_screen(sid)
        if door is not None:
            self._require_door(door)
        if target != RECEPTION:
            self._require_activity(target)
        v = version if version is not None else scr.sent_version + 1
        if v <= scr.sent_version:
            raise ApiError("copy version must exceed current (%d)" % scr.sent_version, status=409, code="bad_version")
        notes = self._commit(
            [{"type": "screen.copy_sent", "data": {"id": sid, "version": v, "target": target, "door": door}}]
        )
        return {"screen": sid, "sent_version": v, "copies": self._screen_public(scr), "notes": notes}

    def device_state(self, sid, kind, version=None):
        """Visitor-facing screen confirms receive / buffer / display."""
        scr = self._require_screen(sid)
        if kind not in SCREEN_EVENT_STATES:
            raise ApiError("bad device action", code="bad_action")
        if not scr.online:
            raise ApiError("screen %r is offline" % sid, status=409, code="screen_offline")
        if kind == "displayed" and version is not None:
            # The display confirmation is the point of no return: guard the
            # "keep one complete route to the old activity" promise here too.
            v = int(version)
            if v in scr.copies:
                target, door = scr.copies[v]
                self._guard_last_lifeline(sid, target, door)
        data = {"id": sid}
        if version is not None:
            data["version"] = int(version)
        notes = self._commit([{"type": "screen." + kind, "data": data}])
        return {"screen": sid, "action": kind, "state": self._screen_public(scr), "notes": notes}

    def _guard_last_lifeline(self, sid, target, door):
        """Refuse a display change that would erase the last route to an old
        activity while that activity has not been formally ended."""
        prospective = {sid: {"target": target, "door": door}}
        for co in self.state.changeovers.values():
            if co.mode != "successive":
                continue
            old = self.state.activities.get(co.from_activity)
            if old is None or old.status == "ended":
                continue
            if sid not in co.entrance_screens and sid not in co.signage_screens:
                continue
            before = [e.id for e in self.state.entrance_screens() if self.state.walk(e.id, co.from_activity)["ok"]]
            after = [
                e.id
                for e in self.state.entrance_screens()
                if self.state.walk(e.id, co.from_activity, overrides=prospective)["ok"]
            ]
            if before and not after:
                raise ApiError(
                    "displaying this copy on %s would erase the last complete route to %s, "
                    "which the host has not ended yet" % (sid, co.from_activity),
                    status=409,
                    code="would_drop_old_lifeline",
                    details={"changeover": co.id, "old_activity": co.from_activity, "lifelines": before},
                )

    def set_mask(self, sid, masked):
        scr = self._require_screen(sid)
        if not scr.online:
            raise ApiError("screen %r is offline" % sid, status=409, code="screen_offline")
        self._commit([{"type": "screen.masked", "data": {"id": sid, "masked": bool(masked)}}])
        return {"screen": sid, "masked": bool(masked)}

    def set_online(self, sid, online):
        self._require_screen(sid)
        self._commit([{"type": "screen.online", "data": {"id": sid, "online": bool(online)}}], reevaluate=False)
        return {"screen": sid, "online": bool(online)}

    # ------------------------------------------------------------- views
    def _screen_public(self, scr):
        return {
            "id": scr.id,
            "zone": scr.zone,
            "online": scr.online,
            "masked": scr.masked,
            "sent_version": scr.sent_version,
            "received_version": scr.received_version,
            "buffered_version": scr.buffered_version,
            "display_version": scr.display_version,
            "display_target": scr.display_target,
            "display_door": scr.display_door,
            "showing": scr.showing,
        }

    def screen_view(self, sid):
        return self._screen_public(self._require_screen(sid))

    def screens_view(self):
        return [self._screen_public(s) for s in self.state.screens.values()]

    def topology(self):
        return {
            "zones": [
                {"id": z.id, "name": z.name, "kind": z.kind} for z in self.state.zones.values()
            ],
            "doors": [
                {
                    "id": d.id,
                    "a": d.a,
                    "b": d.b,
                    "direction": d.direction,
                    "open": d.open,
                }
                for d in self.state.doors.values()
            ],
            "screens": self.screens_view(),
            "activities": [
                {
                    "id": a.id,
                    "name": a.name,
                    "zones": sorted(a.zones),
                    "allow_seen": a.allow_seen,
                    "host": a.host,
                    "status": a.status,
                }
                for a in self.state.activities.values()
            ],
        }

    # ------------------------------------------------------------- walking
    def walk(self, entrance_screen, target, overrides=None):
        res = self.state.walk(entrance_screen, target, overrides=overrides)
        # attach human readable per-hop screen labels
        for hop in res["route"]:
            scr = self.state.screens[hop["screen"]]
            hop["zone"] = scr.zone
            hop["zone_name"] = self.state.zones[scr.zone].name
        return res

    def left_guidance(self, entrance_screen=None, target=None):
        """What a visitor would *actually* follow right now (works offline).

        Never relies on copies that were merely sent.
        """
        result = {"screens": self.screens_view()}
        if entrance_screen is None and target is None:
            return result
        if entrance_screen is None:
            # every entrance, for the requested target
            target = target
            per = {}
            for s in self.state.entrance_screens():
                per[s.id] = self.walk(s.id, target)
            result["target"] = target
            result["entrances"] = per
            return result
        if target is None:
            target = self.state.screens[entrance_screen].display_target
            if target is None:
                raise ApiError("screen shows nothing; supply target", code="no_target")
        result["walk"] = self.walk(entrance_screen, target)
        return result

    # ---------------------------------------------------------- changeovers
    def start_changeover(self, mode, from_activity, to_activity, entrance_screens,
                         signage_screens=None, doors_to_open=None, doors_to_partition=None, cid=None):
        if mode not in ("whole_hall", "successive"):
            raise ApiError("mode must be whole_hall or successive", code="bad_mode")
        self._require_activity(from_activity)
        self._require_activity(to_activity)
        if not entrance_screens:
            raise ApiError("entrance_screens required", code="missing_entrances")
        for sid in entrance_screens:
            scr = self._require_screen(sid)
            if self.state.zones[scr.zone].kind != "entrance":
                raise ApiError("screen %r is not in an entrance zone" % sid, code="not_entrance")
        signage_screens = signage_screens or self._wayfinding_screens()
        for sid in signage_screens:
            self._require_screen(sid)
        doors_to_open = doors_to_open or []
        doors_to_partition = doors_to_partition or []
        for did in list(doors_to_open) + list(doors_to_partition):
            self._require_door(did)
        cid = cid or ("co-%d" % (len(self.state.changeovers) + 1))
        if cid in self.state.changeovers:
            raise ApiError("duplicate changeover", status=409, code="duplicate_changeover")
        self._commit(
            [
                {
                    "type": "changeover.started",
                    "data": {
                        "id": cid,
                        "mode": mode,
                        "from_activity": from_activity,
                        "to_activity": to_activity,
                        "entrance_screens": list(entrance_screens),
                        "signage_screens": list(signage_screens),
                        "doors_to_open": list(doors_to_open),
                        "doors_to_partition": list(doors_to_partition),
                    },
                },
                {
                    "type": "changeover.phase",
                    "data": {"id": cid, "phase": "started"},
                },
            ],
            reevaluate=False,
        )
        # remember partition list (kept in memory only metadata; recorded as phase data)
        co = self.state.changeovers[cid]
        return {"changeover": cid}

    def _wayfinding_screens(self):
        return [
            s.id
            for s in self.state.screens.values()
            if self.state.zones[s.zone].kind in ("entrance", "corridor")
        ]

    def _screen_shows(self, scr, activity):
        return scr.showing and scr.display_target == activity

    def conflict_screens(self, co):
        """Screens actually showing the old activity that must be masked."""
        out = []
        for scr in self.state.screens.values():
            if self._screen_shows(scr, co.from_activity):
                out.append(scr.id)
        return out

    def plan(self, cid):
        co = self._require_changeover(cid)
        conflicts = self.conflict_screens(co)
        partitions = list(co.doors_to_partition)
        steps = []
        if co.mode == "whole_hall":
            steps.append(
                {
                    "action": "partition",
                    "doors": partitions,
                    "why": "close the listed partitions before touching signage",
                }
            )
            steps.append(
                {
                    "action": "mask_conflicts",
                    "screens": conflicts,
                    "why": "uniformly mask every screen still showing the old activity",
                }
            )
            steps.append(
                {
                    "action": "stage_signage",
                    "screens": co.signage_screens,
                    "target": co.to_activity,
                    "why": "prepare the whole signage set and wait for real display confirmations",
                }
            )
            steps.append(
                {
                    "action": "open_doors",
                    "doors": co.doors_to_open,
                    "why": "only now may the passage doors be reported open",
                }
            )
            steps.append(
                {
                    "action": "open",
                    "entrances": co.entrance_screens,
                    "why": "certify each entrance and issue the host a visitor-may-enter receipt",
                }
            )
        else:
            steps.append(
                {
                    "action": "switch_entrances",
                    "entrances": co.entrance_screens,
                    "why": "switch entrances one at a time, never dropping the last old route",
                }
            )
        return {"changeover": cid, "mode": co.mode, "conflict_screens": conflicts, "steps": steps}

    def _whole_hall_gate(self, co, entrance_screen):
        blockers = []
        # 0. any declared partitions must be physically confirmed in place
        missing_partitions = [
            did for did in co.doors_to_partition if did not in co.field_confirmed_doors
        ]
        if missing_partitions:
            blockers.append({"gate": "partitions_confirmed", "doors": missing_partitions})
        # 1. every screen still showing old guidance must be really masked
        unmasked = [sid for sid in self.conflict_screens(co) if not self.state.screens[sid].masked]
        if unmasked:
            blockers.append({"gate": "conflicts_masked", "screens": unmasked})
        # 2. the whole signage set *on the new route* must be confirmed showing it
        plan = self._route_plan(self.state.screens[entrance_screen].zone, co.to_activity,
                                extra_open=co.doors_to_open)
        if plan is None:
            blockers.append({"gate": "route_plan", "reason": "no signed path to the new activity"})
            route = {"ok": False, "reason": "no_route_plan", "route": []}
        else:
            not_ready = []
            for sid, did in plan:
                scr = self.state.screens[sid]
                if not (scr.showing and scr.display_target == co.to_activity and scr.display_door == did):
                    not_ready.append(
                        {
                            "screen": sid,
                            "want_door": did,
                            "showing": scr.showing,
                            "display_target": scr.display_target,
                            "note": "copy sent is not enough; needs a confirmed display",
                        }
                    )
            if not_ready:
                blockers.append({"gate": "signage_ready", "screens": not_ready})
            route = self.walk(entrance_screen, co.to_activity)
        # 3. doors needed by the new route must be field-confirmed open
        closed = [did for did in co.doors_to_open if not self.state.doors[did].open]
        if closed:
            blockers.append({"gate": "doors_open", "doors": closed})
        # 4. route must actually be walkable from this entrance
        if plan is not None and not route["ok"]:
            blockers.append({"gate": "route_walkable", "reason": route["reason"]})
        return blockers, route

    def _route_plan(self, from_zone, activity_id, extra_open=None):
        """BFS over doors toward an activity's zones.

        ``extra_open`` lists door ids that count as traversable even if the field
        crew has not yet reported them open (used to plan/stage signage before the
        doors actually open).  Returns an ordered list of ``(screen_id, door_id)``
        hops, or ``None`` if no signed path exists.
        """
        extra_open = set(extra_open or [])
        act = self.state.activities.get(activity_id)
        if act is None:
            return None
        goals = set(act.zones)
        if from_zone in goals:
            return []
        screen_of = self.state.zone_screen()
        seen = {from_zone}
        q = deque([(from_zone, [])])
        while q:
            zone, hops = q.popleft()
            for door in self.state.doors.values():
                if not (door.open or door.id in extra_open) or zone not in (door.a, door.b):
                    continue
                nxt = door.other(zone)
                if nxt in seen:
                    continue
                scr = screen_of.get(zone)
                if scr is None:
                    # cannot guide onward from a zone with no sign; not a valid path
                    continue
                new_hops = hops + [(scr.id, door.id)]
                if nxt in goals:
                    return new_hops
                seen.add(nxt)
                q.append((nxt, new_hops))
        return None

    def _old_lifeline(self, co, except_entrance):
        """Pick a still-walkable route to the old activity from some other entrance."""
        for s in self.state.entrance_screens():
            if s.id == except_entrance:
                continue
            res = self.state.walk(s.id, co.from_activity)
            if res["ok"]:
                return {
                    "entrance": s.id,
                    "screens": [h["screen"] for h in res["route"]],
                    "hops": res["route"],
                }
        return None

    def _last_lifeline_for(self, co, entrance_screen):
        others = [
            s.id
            for s in self.state.entrance_screens()
            if s.id != entrance_screen and self.state.walk(s.id, co.from_activity)["ok"]
        ]
        return not others

    def switch_entrance(self, cid, entrance_screen, door=None):
        """Hand one entrance over to the new activity as far as the old route allows.

        Only the screens *not* on the protected old lifeline are re-captioned
        now; screens the lifeline still needs are held back.  The last entrance
        still guiding visitors to the old activity may not switch until the host
        has formally ended it.
        """
        co = self._require_changeover(cid)
        if co.mode != "successive":
            raise ApiError("switch-entrance is for successive changeovers", code="wrong_mode")
        if entrance_screen not in co.entrance_screens:
            raise ApiError("not an entrance of this changeover", code="not_in_changeover")
        scr = self._require_screen(entrance_screen)
        old_ended = self.state.activities[co.from_activity].status == "ended"
        last = self._last_lifeline_for(co, entrance_screen)
        if last and not old_ended:
            raise ApiError(
                "refusing to switch the last entrance still guiding visitors to the old "
                "activity before the host has ended it",
                status=409,
                code="last_old_lifeline",
                details={"changeover": cid, "old_activity": co.from_activity},
            )

        plan = self._route_plan(scr.zone, co.to_activity)
        if plan is None:
            raise ApiError(
                "no open, signed path from %s to %s yet" % (scr.zone, co.to_activity),
                status=409,
                code="no_route",
            )

        lifeline = None if old_ended else self._old_lifeline(co, entrance_screen)
        protected = set(lifeline["screens"]) if lifeline else set()

        blocked = [(sid, did) for sid, did in plan if sid in protected]
        if blocked:
            raise ApiError(
                "cannot switch %s yet: its complete new route still needs screens that "
                "carry the protected old route" % entrance_screen,
                status=409,
                code="blocked_by_lifeline",
                details={
                    "blocked_screens": [s for s, _ in blocked],
                    "lifeline": lifeline,
                },
            )

        copies = []
        for sid, did in plan:
            cur = self.state.screens[sid]
            if cur.showing and cur.display_target == co.to_activity and cur.display_door == did:
                copies.append({"screen": sid, "door": did, "version": cur.display_version, "already": True})
                continue
            r = self.send_copy(sid, co.to_activity, did)
            copies.append({"screen": sid, "door": did, "version": r["sent_version"]})
        self._commit([{"type": "changeover.phase", "data": {"id": cid, "phase": "switching"}}], reevaluate=False)

        return {
            "changeover": cid,
            "entrance": entrance_screen,
            "copies": copies,
            "protected_lifeline": lifeline,
            "note": "copies sent; each screen only switches once its device confirms the display",
        }

    def _first_door_toward(self, from_zone, activity_id):
        """BFS over currently-open doors to a target zone; return first door id."""
        act = self.state.activities.get(activity_id)
        if act is None:
            return None
        goals = set(act.zones)
        seen = {from_zone}
        q = deque([(from_zone, None)])
        while q:
            zone, first = q.popleft()
            for door in self.state.doors.values():
                if not door.open:
                    continue
                if zone not in (door.a, door.b):
                    continue
                nxt = door.other(zone)
                if nxt in seen:
                    continue
                f = first or door.id
                if nxt in goals:
                    return f
                seen.add(nxt)
                q.append((nxt, f))
        return None

    def attempt_open(self, cid, entrances=None):
        co = self._require_changeover(cid)
        entrances = entrances or co.entrance_screens
        results = []
        for sid in entrances:
            if co.mode == "whole_hall":
                blockers, route = self._whole_hall_gate(co, sid)
                if blockers:
                    results.append({"entrance_screen": sid, "opened": False, "blockers": blockers})
                    continue
            else:
                route = self.walk(sid, co.to_activity)
                if not route["ok"]:
                    results.append(
                        {"entrance_screen": sid, "opened": False, "blockers": [{"gate": "route_walkable", "reason": route["reason"]}]}
                    )
                    continue
            opened = self._certify_open(co.to_activity, sid, changeover=cid, route=route)
            results.append({"entrance_screen": sid, "opened": True, "opening": opened})
        self._commit([{"type": "changeover.phase", "data": {"id": cid, "phase": "opened"}}], reevaluate=False)
        return {"changeover": cid, "results": results}

    # -------------------------------------------------------------- openings
    def open_direct(self, activity, entrance_screen):
        """Certify an entrance for an activity without the full changeover gates.

        Still refuses reception as an activity and requires a real walkable route.
        """
        scr = self._require_screen(entrance_screen)
        if self.state.zones[scr.zone].kind != "entrance":
            raise ApiError("screen is not in an entrance zone", code="not_entrance")
        self._require_activity(activity)
        route = self.walk(entrance_screen, activity)
        if not route["ok"]:
            raise ApiError(
                "visitors cannot actually walk from %s to %s yet" % (entrance_screen, activity),
                status=409,
                code="not_walkable",
                details=route,
            )
        return self._certify_open(activity, entrance_screen, changeover=None, route=route)

    def _certify_open(self, activity, entrance_screen, changeover, route):
        number = self.state.next_opening
        specs = [
            {
                "type": "opening.created",
                "data": {
                    "number": number,
                    "changeover": changeover,
                    "activity": activity,
                    "entrance_screen": entrance_screen,
                    "route": route["route"],
                    "final_zone": route["final_zone"],
                },
            }
        ]
        if changeover:
            specs.append(
                {"type": "changeover.opened", "data": {"id": changeover, "opening_number": number}}
            )
        specs.append(
            {
                "type": "receipt.issued",
                "data": {
                    "opening_number": number,
                    "activity": activity,
                    "entrance_screen": entrance_screen,
                    "host": self.state.activities[activity].host,
                    "message": "访客可进 / visitors may enter",
                    "route": route["route"],
                },
            }
        )
        self._commit(specs, reevaluate=False)
        self._reevaluate_openings()
        return self.opening_view(number)

    def _reevaluate_openings(self):
        """Retract every active opening whose real walkable route has vanished."""
        retract = []
        for n, op in list(self.state.openings.items()):
            if op.status != "active":
                continue
            res = self.state.walk(op.entrance_screen, op.activity)
            if not res["ok"]:
                retract.append(
                    {
                        "type": "opening.retracted",
                        "data": {"number": n, "reason": res["reason"]},
                    }
                )
        if retract:
            self._commit(retract, reevaluate=False)

    def opening_view(self, number):
        op = self.state.openings.get(number)
        if op is None:
            raise ApiError("unknown opening %r" % number, status=404, code="unknown_opening")
        # live re-walk to show what is actually walkable *now*
        live = self.state.walk(op.entrance_screen, op.activity) if op.status == "active" else None
        return {
            "number": op.number,
            "changeover": op.changeover,
            "activity": op.activity,
            "entrance_screen": op.entrance_screen,
            "status": op.status,
            "route": op.route,
            "final_zone": op.final_zone,
            "retracted_reason": op.retracted_reason,
            "retracted_ts": op.retracted_ts,
            "snapshot_reproducible": bool(op.route),
            "live_walkable": None if live is None else live["ok"],
        }

    def openings_view(self, status=None):
        out = []
        for n in sorted(self.state.openings):
            v = self.opening_view(n)
            if status and v["status"] != status:
                continue
            out.append(v)
        return out

    def receipts_view(self):
        return list(self.state.receipts)

    # ----------------------------------------------------------- status deets
    def status(self, cid):
        co = self._require_changeover(cid)
        out = {
            "changeover": cid,
            "mode": co.mode,
            "phase": co.phase,
            "from_activity": co.from_activity,
            "to_activity": co.to_activity,
            "conflict_screens": self.conflict_screens(co),
            "old_ended": self.state.activities[co.from_activity].status == "ended",
            "entrances": {},
        }
        for sid in co.entrance_screens:
            if co.mode == "successive":
                old_ended = self.state.activities[co.from_activity].status == "ended"
                last = self._last_lifeline_for(co, sid)
                out["entrances"][sid] = {
                    "can_switch": (not last) or old_ended,
                    "last_lifeline": last,
                    "old_ended": old_ended,
                    "kept_old_routes": [
                        s.id
                        for s in self.state.entrance_screens()
                        if s.id != sid and self.state.walk(s.id, co.from_activity)["ok"]
                    ],
                }
            else:
                blockers, route = self._whole_hall_gate(co, sid)
                out["entrances"][sid] = {
                    "can_open": not blockers,
                    "blockers": blockers,
                    "route": route["route"],
                }
            cur = self.state.screens[sid]
            out["entrances"][sid]["display"] = {
                "showing": cur.showing,
                "target": cur.display_target,
                "door": cur.display_door,
            }
        # still-possible actions when a screen lags
        if co.mode == "whole_hall":
            pending = [
                s.id
                for s in self.state.screens.values()
                if s.sent_version > s.display_version
            ]
            out["pending_confirmations"] = pending
            out["still_possible"] = [
                "mask other conflict screens",
                "field-confirm partitions",
                "send copies to lagging screens",
                "confirm other devices",
            ]
            if pending:
                out["blocked"] = ["cannot issue visitor-may-enter until lagging screens confirm"]
        return out
