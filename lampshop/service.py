"""Core domain service for the lampshade workshop.

Operating regimes
-----------------
The spec asks to *choose one* regime, decide how converging lineages are
handled, and make every finished piece traceable to the basis actually used.

Chosen default: **per-order endorsement (随单背书制)**.
    Every signature re-walks the real student->mentor lineage down to a
    still-valid root.  If an old practicum is retracted, the chain that used it
    can no longer anchor *new* signatures; pieces already signed keep the
    snapshot of the basis they used.  A missing (no-longer-qualified) teacher
    may be replaced only by another mentor who is actually recorded on the same
    practicum and is still qualified -- never by "some certificate holder in
    the shop".

Alternative: **periodic certification (按期发证制)**.
    At period opening the roster's valid experience is re-checked and
    time-limited certificates are issued.  Signatures check the certificate and
    the on-duty post, not the whole lineage.  If old evidence turns out wrong,
    certificates already issued inside their validity are handled by an
    explicit review policy (``honor_through_period`` or
    ``revoke_on_retraction``); the next period re-verifies the lineage and will
    not renew a certificate whose basis has been retracted.

Lineage convergence
-------------------
When several valid lineages reach a person, *all* are recorded in the basis
snapshot.  The ``primary`` lineage is chosen deterministically:
fewest hops, then earliest completion timestamp of the root-most evidence,
then smallest evidence id.  This is documented in every basis payload as
``lineage_policy`` so the shopkeeper can reproduce the choice.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, Iterable, List, Optional

from .db import Database
from .errors import Conflict, NotFound, ValidationError
from . import timeutil as tu

LINEAGE_POLICY = (
    "primary = fewest hops, then earliest completion of root-most evidence, "
    "then smallest evidence id; all valid lineages retained"
)

CAP_PRODUCTION = "production"
CAP_MENTORING = "mentoring"

REGIME_ENDORSEMENT = "endorsement"
REGIME_PERIODIC = "periodic"


def _rows(cur) -> List[Dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _one(cur) -> Optional[Dict[str, Any]]:
    r = cur.fetchone()
    return dict(r) if r else None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _uniq(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


class DomainService:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ audit
    def _audit(self, conn, action: str, entity_type: str, entity_id: str, details: dict):
        conn.execute(
            "INSERT INTO audit_log(at_ts, action, entity_type, entity_id, details_json) "
            "VALUES(?,?,?,?,?)",
            (tu.now_str(), action, entity_type, entity_id, json.dumps(details, sort_keys=True)),
        )

    # ------------------------------------------------------------------ setup
    def create_person(self, name: str, person_id: str = None, role: str = "person") -> dict:
        pid = person_id or _new_id("person")
        if not name:
            raise ValidationError("name is required")
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
                raise Conflict("DUPLICATE", f"person exists: {pid}")
            conn.execute("INSERT INTO people(id, name, role) VALUES(?,?,?)", (pid, name, role))
            self._audit(conn, "create", "person", pid, {"name": name, "role": role})
        return {"id": pid, "name": name, "role": role}

    def get_person(self, pid: str) -> dict:
        with self.db.read() as conn:
            row = _one(conn.execute("SELECT * FROM people WHERE id=?", (pid,)))
        if not row:
            raise NotFound("person", pid)
        return row

    def list_people(self) -> List[dict]:
        with self.db.read() as conn:
            return _rows(conn.execute("SELECT * FROM people ORDER BY id"))

    def create_craft(self, craft_id: str, name: str, min_minutes: float, required: bool = True) -> dict:
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM crafts WHERE id=?", (craft_id,)).fetchone():
                raise Conflict("DUPLICATE", f"craft exists: {craft_id}")
            conn.execute(
                "INSERT INTO crafts(id, name, min_minutes, required) VALUES(?,?,?,?)",
                (craft_id, name, float(min_minutes), 1 if required else 0),
            )
            self._audit(conn, "create", "craft", craft_id, {"min_minutes": min_minutes})
        return {"id": craft_id, "name": name, "min_minutes": float(min_minutes), "required": required}

    def add_craft_step(self, craft_id: str, step_id: str, name: str, seq: int = 0, required: bool = True) -> dict:
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM crafts WHERE id=?", (craft_id,)).fetchone():
                raise NotFound("craft", craft_id)
            if conn.execute("SELECT 1 FROM craft_steps WHERE id=?", (step_id,)).fetchone():
                raise Conflict("DUPLICATE", f"step exists: {step_id}")
            conn.execute(
                "INSERT INTO craft_steps(id, craft_id, name, seq, required) VALUES(?,?,?,?,?)",
                (step_id, craft_id, name, seq, 1 if required else 0),
            )
            self._audit(conn, "create", "craft_step", step_id, {"craft_id": craft_id})
        return {"id": step_id, "craft_id": craft_id, "name": name, "seq": seq, "required": required}

    def get_craft(self, craft_id: str) -> dict:
        with self.db.read() as conn:
            craft = _one(conn.execute("SELECT * FROM crafts WHERE id=?", (craft_id,)))
            if not craft:
                raise NotFound("craft", craft_id)
            craft["steps"] = _rows(
                conn.execute("SELECT * FROM craft_steps WHERE craft_id=? ORDER BY seq, id", (craft_id,))
            )
        return craft

    def create_product_type(self, pt_id: str, name: str, craft_ids: List[str]) -> dict:
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM product_types WHERE id=?", (pt_id,)).fetchone():
                raise Conflict("DUPLICATE", f"product_type exists: {pt_id}")
            for cid in craft_ids:
                if not conn.execute("SELECT 1 FROM crafts WHERE id=?", (cid,)).fetchone():
                    raise NotFound("craft", cid)
            conn.execute("INSERT INTO product_types(id, name) VALUES(?,?)", (pt_id, name))
            for cid in craft_ids:
                conn.execute(
                    "INSERT INTO product_type_crafts(product_type_id, craft_id) VALUES(?,?)", (pt_id, cid)
                )
            self._audit(conn, "create", "product_type", pt_id, {"crafts": craft_ids})
        return {"id": pt_id, "name": name, "crafts": _uniq(craft_ids)}

    def create_station(self, station_id: str, name: str) -> dict:
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM stations WHERE id=?", (station_id,)).fetchone():
                raise Conflict("DUPLICATE", f"station exists: {station_id}")
            conn.execute("INSERT INTO stations(id, name) VALUES(?,?)", (station_id, name))
            self._audit(conn, "create", "station", station_id, {"name": name})
        return {"id": station_id, "name": name}

    def create_post(self, post_id: str, name: str, sign_authority: bool) -> dict:
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone():
                raise Conflict("DUPLICATE", f"post exists: {post_id}")
            conn.execute(
                "INSERT INTO posts(id, name, sign_authority) VALUES(?,?,?)",
                (post_id, name, 1 if sign_authority else 0),
            )
            self._audit(conn, "create", "post", post_id, {"sign_authority": sign_authority})
        return {"id": post_id, "name": name, "sign_authority": sign_authority}

    def create_shift(self, person_id: str, post_id: str, start_ts: str, end_ts: str, shift_id: str = None) -> dict:
        sid = shift_id or _new_id("shift")
        start = tu.norm_dt(start_ts)
        end = tu.norm_dt(end_ts)
        if tu.parse_dt(end) <= tu.parse_dt(start):
            raise ValidationError("shift end must be after start")
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise NotFound("person", person_id)
            post = _one(conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)))
            if not post:
                raise NotFound("post", post_id)
            conn.execute(
                "INSERT INTO shifts(id, person_id, post_id, start_ts, end_ts) VALUES(?,?,?,?,?)",
                (sid, person_id, post_id, start, end),
            )
            self._audit(conn, "create", "shift", sid, {"person_id": person_id, "post_id": post_id})
        return {"id": sid, "person_id": person_id, "post_id": post_id,
                "start_ts": start, "end_ts": end, "sign_authority": bool(post["sign_authority"])}

    def set_setting(self, key: str, value: str) -> dict:
        self.db.set_setting(key, value)
        return {"key": key, "value": value}

    def get_regime(self) -> str:
        return self.db.get_setting("regime", REGIME_ENDORSEMENT)

    @staticmethod
    def _regime_in_conn(conn) -> str:
        row = conn.execute("SELECT value FROM settings WHERE key='regime'").fetchone()
        return row["value"] if row else REGIME_ENDORSEMENT

    def set_regime(self, regime: str) -> dict:
        if regime not in (REGIME_ENDORSEMENT, REGIME_PERIODIC):
            raise ValidationError(f"unknown regime: {regime}")
        return self.set_setting("regime", regime)

    def seed_root(self, person_id: str, craft_ids: List[str], at_ts: str) -> dict:
        """Grant an initial mentor standing production+mentoring authority."""
        at = tu.norm_dt(at_ts)
        created = []
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise NotFound("person", person_id)
            for cid in craft_ids:
                if not conn.execute("SELECT 1 FROM crafts WHERE id=?", (cid,)).fetchone():
                    raise NotFound("craft", cid)
                for cap in (CAP_PRODUCTION, CAP_MENTORING):
                    qid = _new_id("qual")
                    conn.execute(
                        "INSERT INTO qualifications(id, person_id, craft_id, capacity, regime, "
                        "valid_from, valid_until, status, source_kind, source_ref, created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (qid, person_id, cid, cap, "grant", at, None, "active", "root", None, tu.now_str()),
                    )
                    created.append({"id": qid, "craft_id": cid, "capacity": cap})
            self._audit(conn, "seed_root", "person", person_id, {"crafts": craft_ids, "at": at})
        return {"person_id": person_id, "qualifications": created}

    # --------------------------------------------------------------- evidence
    def register_evidence(self, craft_id: str, start_ts: str, end_ts: str,
                          mentors: List[str], students: List[str], steps: List[str],
                          station_id: str = None, kind: str = "apprenticeship",
                          registered_at: str = None, evidence_id: str = None,
                          note: str = None, mentor_steps: Dict[str, List[str]] = None) -> dict:
        """Fill the registration book -- before arrival.  Creates *no* evidence yet."""
        eid = evidence_id or _new_id("ev")
        start = tu.norm_dt(start_ts)
        end = tu.norm_dt(end_ts)
        if tu.parse_dt(end) <= tu.parse_dt(start):
            raise ValidationError("evidence end must be after start")
        if not mentors:
            raise ValidationError("at least one mentor is required")
        if not students:
            raise ValidationError("at least one student is required")
        if kind not in ("apprenticeship", "independent_guidance"):
            raise ValidationError(f"unknown evidence kind: {kind}")
        reg_at = tu.norm_dt(registered_at) if registered_at else tu.now_str()
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM evidence WHERE id=?", (eid,)).fetchone():
                raise Conflict("DUPLICATE", f"evidence exists: {eid}")
            if not conn.execute("SELECT 1 FROM crafts WHERE id=?", (craft_id,)).fetchone():
                raise NotFound("craft", craft_id)
            for p in list(mentors) + list(students):
                if not conn.execute("SELECT 1 FROM people WHERE id=?", (p,)).fetchone():
                    raise NotFound("person", p)
            if station_id and not conn.execute("SELECT 1 FROM stations WHERE id=?", (station_id,)).fetchone():
                raise NotFound("station", station_id)
            for s in steps:
                row = _one(conn.execute("SELECT * FROM craft_steps WHERE id=?", (s,)))
                if not row:
                    raise NotFound("craft_step", s)
                if row["craft_id"] != craft_id:
                    raise ValidationError(f"step {s} does not belong to craft {craft_id}")
            ms_map: Dict[str, List[str]] = {}
            if mentor_steps:
                for person, msteps in mentor_steps.items():
                    if person not in mentors:
                        raise ValidationError(f"mentor_steps references non-mentor {person}")
                    for s in msteps:
                        sc = _one(conn.execute("SELECT * FROM craft_steps WHERE id=?", (s,)))
                        if not sc or sc["craft_id"] != craft_id:
                            raise ValidationError(f"step {s} does not belong to craft {craft_id}")
                    ms_map[person] = _uniq(msteps)
            conn.execute(
                "INSERT INTO evidence(id, kind, craft_id, station_id, start_ts, end_ts, status, "
                "registered_at, note) VALUES(?,?,?,?,?,?,?,?,?)",
                (eid, kind, craft_id, station_id, start, end, "registered", reg_at, note),
            )
            for m in _uniq(mentors):
                conn.execute("INSERT INTO evidence_mentors(evidence_id, person_id) VALUES(?,?)", (eid, m))
            for s in _uniq(students):
                conn.execute("INSERT INTO evidence_students(evidence_id, person_id) VALUES(?,?)", (eid, s))
            for s in _uniq(steps):
                conn.execute("INSERT INTO evidence_steps(evidence_id, step_id) VALUES(?,?)", (eid, s))
            for person, msteps in ms_map.items():
                for s in msteps:
                    conn.execute(
                        "INSERT INTO evidence_mentor_steps(evidence_id, person_id, step_id) "
                        "VALUES(?,?,?)", (eid, person, s))
            self._audit(conn, "register", "evidence", eid, {"craft_id": craft_id, "status": "registered"})
        return self.get_evidence(eid)

    def complete_evidence(self, evidence_id: str, completed_at: str = None) -> dict:
        at = tu.norm_dt(completed_at) if completed_at else tu.now_str()
        with self.db.transaction() as conn:
            ev = _one(conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)))
            if not ev:
                raise NotFound("evidence", evidence_id)
            if ev["status"] == "completed":
                return self._ev_payload(conn, evidence_id, at)
            if ev["status"] == "retracted":
                raise Conflict("RETRACTED", "cannot complete a retracted evidence record")
            if tu.parse_dt(at) < tu.parse_dt(ev["end_ts"]):
                raise ValidationError("completed_at cannot precede the practicum end time")
            conn.execute(
                "UPDATE evidence SET status='completed', completed_at=? WHERE id=?", (at, evidence_id)
            )
            self._audit(conn, "complete", "evidence", evidence_id, {"completed_at": at})
            return self._ev_payload(conn, evidence_id, at)

    def verify_evidence(self, evidence_id: str, verified_at: str = None) -> dict:
        """Second confirmation, needed for independent_guidance before it is usable."""
        at = tu.norm_dt(verified_at) if verified_at else tu.now_str()
        with self.db.transaction() as conn:
            ev = _one(conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)))
            if not ev:
                raise NotFound("evidence", evidence_id)
            if ev["status"] != "completed":
                raise Conflict("NOT_COMPLETED", "only completed evidence can be verified")
            if ev["verified_at"] is None:
                conn.execute("UPDATE evidence SET verified_at=? WHERE id=?", (at, evidence_id))
                self._audit(conn, "verify", "evidence", evidence_id, {"verified_at": at})
            return self._ev_payload(conn, evidence_id, at)

    def retract_evidence(self, evidence_id: str, reason: str, at_ts: str = None) -> dict:
        at = tu.norm_dt(at_ts) if at_ts else tu.now_str()
        with self.db.transaction() as conn:
            ev = _one(conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)))
            if not ev:
                raise NotFound("evidence", evidence_id)
            if ev["status"] == "retracted":
                return self._ev_payload(conn, evidence_id, at)
            # Keep the original completion event; record retraction as a new
            # fact.  We never delete -- a correction must not look like the
            # piece was never signed.
            conn.execute(
                "UPDATE evidence SET status='retracted', retracted_at=?, retraction_reason=? WHERE id=?",
                (at, reason, evidence_id),
            )
            self._audit(conn, "retract", "evidence", evidence_id,
                        {"at": at, "reason": reason,
                         "prior_completed_at": ev["completed_at"]})
            return self._ev_payload(conn, evidence_id, at)

    def get_evidence(self, evidence_id: str, at_ts: str = None) -> dict:
        at = tu.norm_dt(at_ts) if at_ts else tu.now_str()
        with self.db.read() as conn:
            if not conn.execute("SELECT 1 FROM evidence WHERE id=?", (evidence_id,)).fetchone():
                raise NotFound("evidence", evidence_id)
            return self._ev_payload(conn, evidence_id, at)

    def list_evidence(self, person_id: str = None, craft_id: str = None, at_ts: str = None) -> List[dict]:
        at = tu.norm_dt(at_ts) if at_ts else tu.now_str()
        clauses = []
        params: list = []
        if craft_id:
            clauses.append("e.craft_id=?")
            params.append(craft_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.db.read() as conn:
            if person_id:
                sql = ("SELECT DISTINCT e.id FROM evidence e "
                       "LEFT JOIN evidence_students s ON s.evidence_id=e.id "
                       "LEFT JOIN evidence_mentors m ON m.evidence_id=e.id "
                       f"WHERE (s.person_id=? OR m.person_id=?){(' AND ' + ' AND '.join(clauses)) if clauses else ''} "
                       "ORDER BY e.start_ts")
                ids = [r["id"] for r in conn.execute(sql, [person_id, person_id] + params)]
            else:
                ids = [r["id"] for r in conn.execute(
                    f"SELECT e.id FROM evidence e{where} ORDER BY e.start_ts", params)]
            return [self._ev_payload(conn, i, at) for i in ids]

    def _ev_payload(self, conn, evidence_id: str, at: str) -> dict:
        ev = _one(conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)))
        if not ev:
            raise NotFound("evidence", evidence_id)
        mentors = [r["person_id"] for r in conn.execute(
            "SELECT person_id FROM evidence_mentors WHERE evidence_id=? ORDER BY person_id", (evidence_id,))]
        students = [r["person_id"] for r in conn.execute(
            "SELECT person_id FROM evidence_students WHERE evidence_id=? ORDER BY person_id", (evidence_id,))]
        steps = [r["step_id"] for r in conn.execute(
            "SELECT step_id FROM evidence_steps WHERE evidence_id=?", (evidence_id,))]
        ev["mentors"] = mentors
        ev["students"] = students
        ev["steps"] = steps
        ev["mentor_steps"] = self._mentor_steps(conn, evidence_id)
        ev["minutes"] = tu.duration_minutes(ev["start_ts"], ev["end_ts"])
        ev["supervision"] = self._supervision_report(conn, ev)
        usable, why = self._usable_info(conn, ev, at)
        ev["usable_at"] = usable
        ev["usable_reasons"] = why
        ev["is_evidence"] = ev["status"] == "completed"
        return ev

    # ------------------------------------------------------- temporal checks
    @staticmethod
    def _qual_valid_at(q: dict, at: str) -> bool:
        if tu.parse_dt(q["valid_from"]) > tu.parse_dt(at):
            return False
        if q["valid_until"] and tu.parse_dt(at) > tu.parse_dt(q["valid_until"]):
            return False
        if q["revoked_at"] and tu.parse_dt(at) >= tu.parse_dt(q["revoked_at"]):
            return False
        return True

    @staticmethod
    def _qual_covers_window(q: dict, start: str, end: str) -> bool:
        if tu.parse_dt(q["valid_from"]) > tu.parse_dt(start):
            return False
        if q["valid_until"] and tu.parse_dt(end) > tu.parse_dt(q["valid_until"]):
            return False
        if q["revoked_at"] and tu.parse_dt(end) > tu.parse_dt(q["revoked_at"]):
            return False
        return True

    # Preference order when several records could apply: explicit root
    # authority first, then back-office grants, then earned practicum records;
    # within a class, the most recently effective wins; ties broken by id so the
    # choice is fully deterministic and reproducible.
    _SOURCE_RANK = {"root": 0, "grant": 1, "practicum": 2}

    def _quals_for(self, conn, person_id: str, craft_id: str, capacity: str) -> List[dict]:
        rows = _rows(conn.execute(
            "SELECT * FROM qualifications WHERE person_id=? AND craft_id=? AND capacity=?",
            (person_id, craft_id, capacity),
        ))
        rows.sort(key=lambda q: (
            self._SOURCE_RANK.get(q["source_kind"], 9),
            "".join(chr(0x10FFFF - ord(c)) for c in q["valid_from"]),  # desc by valid_from
            q["id"],
        ))
        return rows

    def _mentoring_valid_at(self, conn, person_id: str, craft_id: str, at: str):
        for q in self._quals_for(conn, person_id, craft_id, CAP_MENTORING):
            if self._qual_valid_at(q, at):
                return q
        return None

    def _production_valid_at(self, conn, person_id: str, craft_id: str, at: str):
        for q in self._quals_for(conn, person_id, craft_id, CAP_PRODUCTION):
            if self._qual_valid_at(q, at):
                return q
        return None

    def _mentoring_covers(self, conn, person_id: str, craft_id: str, start: str, end: str):
        for q in self._quals_for(conn, person_id, craft_id, CAP_MENTORING):
            if self._qual_covers_window(q, start, end):
                return q
        return None

    def _supervision_report(self, conn, ev: dict) -> dict:
        """Who was actually qualified to supervise this practicum while it ran."""
        covered = []
        for m in ev["mentors"] if "mentors" in ev else [r["person_id"] for r in conn.execute(
                "SELECT person_id FROM evidence_mentors WHERE evidence_id=?", (ev["id"],))]:
            q = self._mentoring_covers(conn, m, ev["craft_id"], ev["start_ts"], ev["end_ts"])
            if q:
                covered.append({"mentor_id": m, "qualification_id": q["id"]})
        return {"validly_supervised": len(covered) > 0, "qualified_mentors": covered}

    def _usable_info(self, conn, ev: dict, at: str):
        reasons = []
        if ev["status"] != "completed":
            reasons.append("not_completed")
        if ev["completed_at"] and tu.parse_dt(ev["completed_at"]) > tu.parse_dt(at):
            reasons.append("completed_in_future")
        if ev["retracted_at"] and tu.parse_dt(ev["retracted_at"]) <= tu.parse_dt(at):
            reasons.append("retracted")
        if ev["kind"] == "independent_guidance":
            if not ev["verified_at"]:
                reasons.append("not_verified")
            elif tu.parse_dt(ev["verified_at"]) > tu.parse_dt(at):
                reasons.append("verified_in_future")
        sup = self._supervision_report(conn, ev)
        if not sup["validly_supervised"]:
            reasons.append("not_validly_supervised")
        return (len(reasons) == 0, reasons)

    def _evidence_usable_at(self, conn, ev: dict, at: str) -> bool:
        usable, _ = self._usable_info(conn, ev, at)
        return usable

    # ----------------------------------------------------------- disputed time
    @staticmethod
    def _union_minutes(intervals: List[tuple]) -> float:
        if not intervals:
            return 0.0
        pts = sorted(intervals)
        total = 0.0
        cur_s, cur_e = pts[0]
        for s, e in pts[1:]:
            if s <= cur_e:
                if e > cur_e:
                    cur_e = e
            else:
                total += (tu.parse_dt(cur_e) - tu.parse_dt(cur_s)).total_seconds() / 60.0
                cur_s, cur_e = s, e
        total += (tu.parse_dt(cur_e) - tu.parse_dt(cur_s)).total_seconds() / 60.0
        return total

    def disputed_minutes(self, person_id: str, at_ts: str = None) -> dict:
        """Overlaps where one person is at two stations at the same time.

        The same person cannot count both sides fully: accrual uses the union of
        intervals, and this report exposes the contested minutes per pair.
        """
        at = tu.norm_dt(at_ts) if at_ts else tu.now_str()
        with self.db.read() as conn:
            rows = _rows(conn.execute(
                "SELECT DISTINCT e.* FROM evidence e "
                "LEFT JOIN evidence_students s ON s.evidence_id=e.id "
                "LEFT JOIN evidence_mentors m ON m.evidence_id=e.id "
                "WHERE s.person_id=? OR m.person_id=? ORDER BY e.start_ts",
                (person_id, person_id),
            ))
            items = []
            for ev in rows:
                items.append({
                    "evidence_id": ev["id"],
                    "station_id": ev["station_id"],
                    "start_ts": ev["start_ts"],
                    "end_ts": ev["end_ts"],
                    "craft_id": ev["craft_id"],
                })
            pairs = []
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    a, b = items[i], items[j]
                    if a["station_id"] == b["station_id"]:
                        continue
                    ov = tu.overlap_minutes(a["start_ts"], a["end_ts"], b["start_ts"], b["end_ts"])
                    if ov > 0:
                        pairs.append({
                            "a": a, "b": b,
                            "overlap_minutes": round(ov, 3),
                        })
            # Total contested time = measure of the minutes during which the
            # person is booked at two *different* stations at once.  Computed with
            # a sweep line so that three or more simultaneous records are not
            # double-counted (each minute counts once).
            total = self._multi_station_minutes(items)
        return {
            "person_id": person_id,
            "at": at,
            "pairs": pairs,
            "total_disputed_minutes": round(total, 3),
            "note": ("overlap between different stations; disputed total counts each "
                     "contested minute once; accrual de-duplicates via interval union"),
        }

    @staticmethod
    def _multi_station_minutes(items: List[dict]) -> float:
        events = []
        for it in items:
            if it["station_id"] is None:
                continue
            events.append((tu.parse_dt(it["start_ts"]), 1, it["station_id"]))
            events.append((tu.parse_dt(it["end_ts"]), -1, it["station_id"]))
        if not events:
            return 0.0
        events.sort(key=lambda e: (e[0], e[1]))
        active: Dict[str, int] = {}
        total = 0.0
        prev_t = events[0][0]
        i = 0
        while i < len(events):
            t = events[i][0]
            if len([s for s, c in active.items() if c > 0]) >= 2:
                total += (t - prev_t).total_seconds() / 60.0
            while i < len(events) and events[i][0] == t:
                _, delta, station = events[i]
                active[station] = active.get(station, 0) + delta
                if active[station] <= 0:
                    active.pop(station, None)
                i += 1
            prev_t = t
        return total

    # ---------------------------------------------------------------- accrual
    def _student_evidence(self, conn, person_id: str, craft_id: str) -> List[dict]:
        return _rows(conn.execute(
            "SELECT e.* FROM evidence e JOIN evidence_students s ON s.evidence_id=e.id "
            "WHERE s.person_id=? AND e.craft_id=? ORDER BY e.start_ts",
            (person_id, craft_id),
        ))

    def eligibility(self, person_id: str, craft_id: str, at_ts: str = None) -> dict:
        at = tu.norm_dt(at_ts) if at_ts else tu.now_str()
        with self.db.read() as conn:
            craft = _one(conn.execute("SELECT * FROM crafts WHERE id=?", (craft_id,)))
            if not craft:
                raise NotFound("craft", craft_id)
            required_steps = [r["id"] for r in conn.execute(
                "SELECT id FROM craft_steps WHERE craft_id=? AND required=1", (craft_id,))]
            evs = self._student_evidence(conn, person_id, craft_id)
            usable = []
            intervals = []
            covered_steps = set()
            for ev in evs:
                if self._evidence_usable_at(conn, ev, at):
                    usable.append(ev["id"])
                    intervals.append((ev["start_ts"], ev["end_ts"]))
                    for r in conn.execute(
                            "SELECT step_id FROM evidence_steps WHERE evidence_id=?", (ev["id"],)):
                        covered_steps.add(r["step_id"])
            minutes = self._union_minutes(intervals)
            missing = [s for s in required_steps if s not in covered_steps]
            eligible = (minutes >= float(craft["min_minutes"])) and (len(missing) == 0)
            return {
                "person_id": person_id,
                "craft_id": craft_id,
                "at": at,
                "accrued_minutes": round(minutes, 3),
                "min_minutes": craft["min_minutes"],
                "required_steps": required_steps,
                "missing_steps": missing,
                "usable_evidence": usable,
                "eligible_for_production": eligible,
            }

    # ---------------------------------------------------------- lineage engine
    def _mentor_steps(self, conn, evidence_id: str) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for r in conn.execute(
                "SELECT person_id, step_id FROM evidence_mentor_steps WHERE evidence_id=? "
                "ORDER BY person_id, step_id", (evidence_id,)):
            out.setdefault(r["person_id"], []).append(r["step_id"])
        return out

    def _guided_relevant_step(self, conn, evidence_id: str, person_id: str) -> bool:
        """Did ``person_id`` actually guide a step on this practicum?

        If no per-mentor step attribution was recorded we fall back to the
        co-mentor being listed on the practicum at all (so older records still
        work); when attribution *is* present it must name this person.
        """
        attrs = self._mentor_steps(conn, evidence_id)
        if not attrs:
            return True
        return bool(attrs.get(person_id))

    def _edge_options(self, conn, student: str, craft_id: str, at: str) -> List[dict]:
        """All usable evidence edges from ``student`` to a still-valid mentor."""
        evs = self._student_evidence(conn, student, craft_id)
        edges = []
        for ev in evs:
            if not self._evidence_usable_at(conn, ev, at):
                continue
            mentors = [r["person_id"] for r in conn.execute(
                "SELECT person_id FROM evidence_mentors WHERE evidence_id=? ORDER BY person_id", (ev["id"],))]
            acting = []
            for m in mentors:
                was = self._mentoring_covers(conn, m, craft_id, ev["start_ts"], ev["end_ts"])
                still = self._mentoring_valid_at(conn, m, craft_id, at)
                if not (was and still):
                    continue
                # Substitution requires the stand-in to have actually guided a
                # relevant step on this very practicum -- never just "a
                # qualified person somewhere in the shop".
                guided = self._guided_relevant_step(conn, ev["id"], m)
                if not guided:
                    continue
                acting.append({"mentor_id": m, "was_qualified_qual": was["id"],
                               "still_qualified_qual": still["id"]})
            if not acting:
                continue
            # someone qualified *then* but not *now* on the same practicum:
            # another still-qualified co-guide may stand in for them.
            not_now = [m for m in mentors
                       if self._mentoring_covers(conn, m, craft_id, ev["start_ts"], ev["end_ts"])
                       and not self._mentoring_valid_at(conn, m, craft_id, at)]
            for a in acting:
                substituted = bool(not_now) and (a["mentor_id"] not in not_now)
                edges.append({
                    "student": student,
                    "mentor": a["mentor_id"],
                    "evidence_id": ev["id"],
                    "kind": ev["kind"],
                    "completed_at": ev["completed_at"],
                    "verified_at": ev["verified_at"],
                    "mentor_qualification_id": a["still_qualified_qual"],
                    "mentor_on_evidence": mentors,
                    "guided_relevant_step": True,
                    "substituted": substituted,
                    "substituted_for": not_now if substituted else [],
                })
        return edges

    def _is_root(self, conn, person_id: str, craft_id: str, at: str) -> bool:
        for q in self._quals_for(conn, person_id, craft_id, CAP_MENTORING):
            if q["source_kind"] == "root" and self._qual_valid_at(q, at):
                return True
        return False

    def lineage_chains(self, conn, person_id: str, craft_id: str, at: str,
                       seen=None, depth: int = 0) -> List[List[dict]]:
        """Enumerate full student->...->root chains valid at ``at``."""
        seen = seen or set()
        if depth > 25:
            return []
        if self._is_root(conn, person_id, craft_id, at):
            return [[]]
        if person_id in seen:
            return []
        seen = seen | {person_id}
        chains: List[List[dict]] = []
        for edge in self._edge_options(conn, person_id, craft_id, at):
            tails = self.lineage_chains(conn, edge["mentor"], craft_id, at, seen, depth + 1)
            for tail in tails:
                chains.append([edge] + tail)
        return chains

    @staticmethod
    def _pick_primary(chains: List[List[dict]]):
        if not chains:
            return None

        def key(chain):
            last = chain[-1]
            return (len(chain), last.get("completed_at") or "", last.get("evidence_id") or "")

        return min(chains, key=key)

    # ------------------------------------------------------- qualifications
    def grant_qualification(self, person_id: str, craft_id: str, capacity: str,
                            valid_from: str, valid_until: str = None,
                            source_kind: str = None, source_ref: str = None,
                            regime: str = "grant", enforce_eligibility: bool = True) -> dict:
        vf = self._norm_boundary(valid_from, end=False)
        vu = self._norm_boundary(valid_until, end=True) if valid_until else None
        if capacity not in (CAP_PRODUCTION, CAP_MENTORING):
            raise ValidationError(f"unknown capacity: {capacity}")
        # A qualification earned by satisfying the eligibility rule is backed by
        # a practicum lineage; a grant that skips the rule is handed-down
        # authority.  This distinction drives whether endorsement re-walks the
        # lineage or accepts the authority directly.
        if source_kind is None:
            source_kind = "practicum" if enforce_eligibility else "grant"
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise NotFound("person", person_id)
            if not conn.execute("SELECT 1 FROM crafts WHERE id=?", (craft_id,)).fetchone():
                raise NotFound("craft", craft_id)
            if capacity == CAP_PRODUCTION and enforce_eligibility:
                elig = self.eligibility(person_id, craft_id, at_ts=vf)
                if not elig["eligible_for_production"]:
                    raise Conflict("NOT_ELIGIBLE", "insufficient valid evidence for production qualification",
                                   elig)
            qid = _new_id("qual")
            conn.execute(
                "INSERT INTO qualifications(id, person_id, craft_id, capacity, regime, valid_from, "
                "valid_until, status, source_kind, source_ref, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (qid, person_id, craft_id, capacity, regime, vf, vu, "active",
                 source_kind, source_ref, tu.now_str()),
            )
            self._audit(conn, "grant", "qualification", qid,
                        {"person_id": person_id, "craft_id": craft_id, "capacity": capacity,
                         "valid_from": vf, "valid_until": vu})
            q = _one(conn.execute("SELECT * FROM qualifications WHERE id=?", (qid,)))
        return q

    def revoke_qualification(self, qual_id: str, reason: str, at_ts: str = None) -> dict:
        at = tu.norm_dt(at_ts) if at_ts else tu.now_str()
        with self.db.transaction() as conn:
            q = _one(conn.execute("SELECT * FROM qualifications WHERE id=?", (qual_id,)))
            if not q:
                raise NotFound("qualification", qual_id)
            conn.execute(
                "UPDATE qualifications SET status='revoked', revoked_at=?, revoke_reason=? WHERE id=?",
                (at, reason, qual_id),
            )
            self._audit(conn, "revoke", "qualification", qual_id, {"at": at, "reason": reason})
            return _one(conn.execute("SELECT * FROM qualifications WHERE id=?", (qual_id,)))

    def list_qualifications(self, person_id: str = None, craft_id: str = None,
                            capacity: str = None, at_ts: str = None) -> List[dict]:
        clause = []
        params: list = []
        if person_id:
            clause.append("person_id=?"); params.append(person_id)
        if craft_id:
            clause.append("craft_id=?"); params.append(craft_id)
        if capacity:
            clause.append("capacity=?"); params.append(capacity)
        where = (" WHERE " + " AND ".join(clause)) if clause else ""
        with self.db.read() as conn:
            rows = _rows(conn.execute(f"SELECT * FROM qualifications{where} ORDER BY created_at", params))
            if at_ts:
                at = tu.norm_dt(at_ts)
                for r in rows:
                    r["valid_at_query"] = self._qual_valid_at(r, at)
            return rows

    def person_lineage(self, person_id: str, craft_id: str, at_ts: str = None) -> dict:
        at = tu.norm_dt(at_ts) if at_ts else tu.now_str()
        with self.db.read() as conn:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise NotFound("person", person_id)
            chains = self.lineage_chains(conn, person_id, craft_id, at)
            prod = self._production_valid_at(conn, person_id, craft_id, at)
            ment = self._mentoring_valid_at(conn, person_id, craft_id, at)
            return {
                "person_id": person_id,
                "craft_id": craft_id,
                "at": at,
                "is_root": self._is_root(conn, person_id, craft_id, at),
                "production_qualification": prod,
                "mentoring_qualification": ment,
                "valid_lineage_count": len(chains),
                "primary": self._pick_primary(chains),
                "chains": chains,
                "lineage_policy": LINEAGE_POLICY,
            }

    # --------------------------------------------------------------- products
    def create_product(self, product_type_id: str, product_id: str = None,
                       customer_session: str = None, created_at: str = None) -> dict:
        pid = product_id or _new_id("product")
        created = tu.norm_dt(created_at) if created_at else tu.now_str()
        with self.db.transaction() as conn:
            pt = _one(conn.execute("SELECT * FROM product_types WHERE id=?", (product_type_id,)))
            if not pt:
                raise NotFound("product_type", product_type_id)
            if conn.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
                raise Conflict("DUPLICATE", f"product exists: {pid}")
            conn.execute(
                "INSERT INTO products(id, product_type_id, status, created_at, customer_session) "
                "VALUES(?,?,?,?,?)",
                (pid, product_type_id, "pending", created, customer_session),
            )
            self._audit(conn, "create", "product", pid, {"product_type_id": product_type_id})
        return self.get_product(pid)

    def get_product(self, product_id: str) -> dict:
        with self.db.read() as conn:
            p = _one(conn.execute("SELECT * FROM products WHERE id=?", (product_id,)))
            if not p:
                raise NotFound("product", product_id)
            crafts = [r["craft_id"] for r in conn.execute(
                "SELECT craft_id FROM product_type_crafts WHERE product_type_id=? ORDER BY craft_id",
                (p["product_type_id"],))]
            p["required_crafts"] = crafts
            sig = _one(conn.execute("SELECT * FROM signatures WHERE product_id=?", (product_id,)))
            p["signature"] = self._sig_payload(sig) if sig else None
        return p

    def _sig_payload(self, sig: dict) -> dict:
        if not sig:
            return None
        out = dict(sig)
        out["basis"] = json.loads(sig["basis_json"])
        return out

    def product_basis(self, product_id: str) -> dict:
        with self.db.read() as conn:
            p = _one(conn.execute("SELECT * FROM products WHERE id=?", (product_id,)))
            if not p:
                raise NotFound("product", product_id)
            sig = _one(conn.execute("SELECT * FROM signatures WHERE product_id=?", (product_id,)))
            if not sig:
                return {"product_id": product_id, "status": p["status"], "signed": False,
                        "basis": None,
                        "note": "not yet signed; no final signer exists"}
            return {
                "product_id": product_id,
                "status": p["status"],
                "signed": True,
                "signature": self._sig_payload(sig),
                "current_status_of_cited_evidence": self._current_evidence_status(conn, sig),
            }

    def _current_evidence_status(self, conn, sig: dict) -> List[dict]:
        """Show, for the snapshot's cited evidence, how it stands *now*.

        The snapshot itself is immutable; this is a convenience view so the
        shopkeeper can see that an old piece was signed on evidence later
        retracted, without the piece being retroactively erased.
        """
        basis = json.loads(sig["basis_json"])
        ids = set()

        def walk(obj):
            if isinstance(obj, dict):
                if "evidence_id" in obj and obj["evidence_id"]:
                    ids.add(obj["evidence_id"])
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(basis)
        out = []
        for eid in sorted(ids):
            ev = _one(conn.execute("SELECT * FROM evidence WHERE id=?", (eid,)))
            if ev:
                out.append({"evidence_id": eid, "status": ev["status"],
                            "completed_at": ev["completed_at"],
                            "retracted_at": ev["retracted_at"],
                            "verified_at": ev["verified_at"]})
        return out

    # ---------------------------------------------------------------- signing
    def sign_product(self, product_id: str, signer_id: str, request_id: str,
                     at_ts: str = None, customer_session: str = None) -> dict:
        if not request_id:
            raise ValidationError("request_id is required for idempotent signing")
        at = tu.norm_dt(at_ts) if at_ts else tu.now_str()
        with self.db.transaction() as conn:
            # 1. Idempotency: replaying a stored request returns its result.
            prev = _one(conn.execute(
                "SELECT * FROM signature_requests WHERE request_id=?", (request_id,)))
            if prev:
                stored = json.loads(prev["result_json"])
                if stored.get("product_id") != product_id:
                    raise Conflict("REQUEST_REUSED",
                                   f"request_id {request_id} already used for product {stored.get('product_id')}")
                stored["replayed"] = True
                return stored

            prod = _one(conn.execute("SELECT * FROM products WHERE id=?", (product_id,)))
            if not prod:
                raise NotFound("product", product_id)

            # 2. A piece has exactly one final signer.  Even a *fresh* request
            #    against an already-signed piece must not create a second one.
            existing = _one(conn.execute("SELECT * FROM signatures WHERE product_id=?", (product_id,)))
            if existing:
                result = self._sig_result(product_id, existing, duplicate=True)
                self._store_request(conn, request_id, product_id, result)
                return result

            regime = self._regime_in_conn(conn)
            basis = self._evaluate_signing(conn, prod, signer_id, at, regime)
            if not basis["ok"]:
                # Failures are not cached, so a caller may retry once the cause
                # is fixed; what must never happen is a second final signer.
                raise Conflict(basis["error"], basis["message"], basis)

            sig_id = _new_id("sig")
            try:
                conn.execute(
                    "INSERT INTO signatures(id, product_id, signer_id, at_ts, regime, basis_json, "
                    "request_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (sig_id, product_id, signer_id, at, regime,
                     json.dumps(basis["basis"], sort_keys=True), request_id, tu.now_str()),
                )
            except sqlite3.IntegrityError:
                # Another writer won the race (e.g. a second process).  The
                # UNIQUE(product_id) constraint guarantees a single final signer;
                # return that one rather than surfacing a spurious error.
                existing = _one(conn.execute(
                    "SELECT * FROM signatures WHERE product_id=?", (product_id,)))
                if not existing:
                    raise
                result = self._sig_result(product_id, existing, duplicate=True)
                self._store_request(conn, request_id, product_id, result)
                return result
            conn.execute(
                "UPDATE products SET status='signed', signed_at=?, signer_id=?, signature_id=? "
                "WHERE id=? AND status='pending'",
                (at, signer_id, sig_id, product_id),
            )
            conn.execute(
                "UPDATE products SET customer_session=COALESCE(?, customer_session) WHERE id=?",
                (customer_session, product_id),
            )
            self._audit(conn, "sign", "product", product_id,
                        {"signer_id": signer_id, "regime": regime, "signature_id": sig_id,
                         "request_id": request_id})
            sig = _one(conn.execute("SELECT * FROM signatures WHERE id=?", (sig_id,)))
            result = self._sig_result(product_id, sig, duplicate=False)
            self._store_request(conn, request_id, product_id, result)
            return result

    def _store_request(self, conn, request_id: str, product_id: str, result: dict):
        conn.execute(
            "INSERT OR IGNORE INTO signature_requests(request_id, product_id, ok, result_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (request_id, product_id, 1 if result.get("signed") else 0,
             json.dumps(result, sort_keys=True), tu.now_str()),
        )

    def _sig_result(self, product_id: str, sig: dict, duplicate: bool) -> dict:
        return {
            "ok": True,
            "signed": True,
            "product_id": product_id,
            "signer_id": sig["signer_id"],
            "signature_id": sig["id"],
            "at_ts": sig["at_ts"],
            "regime": sig["regime"],
            "basis": json.loads(sig["basis_json"]),
            "duplicate_request": duplicate,
            "replayed": False,
        }

    def _evaluate_signing(self, conn, prod: dict, signer_id: str, at: str, regime: str) -> dict:
        if not conn.execute("SELECT 1 FROM people WHERE id=?", (signer_id,)).fetchone():
            return {"ok": False, "error": "SIGNER_NOT_FOUND",
                    "message": f"signer not found: {signer_id}"}
        # On-duty post with sign authority at the actual time/date.
        post = self._on_duty_post(conn, signer_id, at)
        if not post:
            return {"ok": False, "error": "POST_NOT_AUTHORIZED",
                    "message": "signer is not on duty at a post with sign authority at this time",
                    "details": {"at": at},
                    "qualification_still_valid": self._quals_snapshot(conn, signer_id, prod, at)}
        if not post["sign_authority"]:
            return {"ok": False, "error": "POST_NOT_AUTHORIZED",
                    "message": "signer's current post has no sign authority",
                    "details": {"post_id": post["post_id"], "at": at},
                    "qualification_still_valid": self._quals_snapshot(conn, signer_id, prod, at)}

        crafts = [r["craft_id"] for r in conn.execute(
            "SELECT craft_id FROM product_type_crafts WHERE product_type_id=? ORDER BY craft_id",
            (prod["product_type_id"],))]
        if not crafts:
            return {"ok": False, "error": "NO_REQUIRED_CRAFTS",
                    "message": "product type declares no crafts"}

        if regime == REGIME_PERIODIC:
            return self._evaluate_periodic(conn, prod, signer_id, at, crafts, post)
        return self._evaluate_endorsement(conn, prod, signer_id, at, crafts, post)

    def _quals_snapshot(self, conn, signer_id: str, prod: dict, at: str) -> List[dict]:
        crafts = [r["craft_id"] for r in conn.execute(
            "SELECT craft_id FROM product_type_crafts WHERE product_type_id=? ORDER BY craft_id",
            (prod["product_type_id"],))]
        out = []
        for cid in crafts:
            q = self._production_valid_at(conn, signer_id, cid, at)
            out.append({"craft_id": cid, "hold_valid_production_qualification": bool(q),
                        "qualification": q})
        return out

    def _on_duty_post(self, conn, person_id: str, at: str) -> Optional[dict]:
        rows = _rows(conn.execute(
            "SELECT sh.*, p.sign_authority, p.name AS post_name FROM shifts sh "
            "JOIN posts p ON p.id = sh.post_id WHERE sh.person_id=? ORDER BY sh.start_ts",
            (person_id,),
        ))
        for sh in rows:
            if tu.parse_dt(sh["start_ts"]) <= tu.parse_dt(at) <= tu.parse_dt(sh["end_ts"]):
                return {"shift_id": sh["id"], "post_id": sh["post_id"],
                        "post_name": sh["post_name"], "sign_authority": bool(sh["sign_authority"]),
                        "shift_start": sh["start_ts"], "shift_end": sh["end_ts"]}
        return None

    def _evaluate_endorsement(self, conn, prod, signer_id, at, crafts, post) -> dict:
        craft_bases = {}
        for cid in crafts:
            q = self._production_valid_at(conn, signer_id, cid, at)
            if not q:
                return {"ok": False, "error": "NO_PRODUCTION_QUALIFICATION",
                        "message": f"signer holds no valid production qualification for craft {cid} at this time",
                        "details": {"craft_id": cid, "at": at}}
            if q["source_kind"] in ("root", "grant"):
                # An explicit origin of authority: root standing, or a manual
                # back-office grant that intentionally skipped the eligibility
                # check.  There is no upstream apprenticeship to walk; these are
                # audited administrative acts, accepted directly.
                craft_bases[cid] = {
                    "qualification_id": q["id"], "source_kind": q["source_kind"],
                    "basis_origin": "granted_authority", "chains": [], "primary": [],
                }
                continue
            chains = self.lineage_chains(conn, signer_id, cid, at)
            if not chains:
                return {"ok": False, "error": "NO_VALID_BASIS",
                        "message": (f"no still-valid lineage for craft {cid}; the basis was "
                                    f"retracted, unverified, or invalidly supervised"),
                        "details": {"craft_id": cid, "at": at},
                        "qualification_still_valid": bool(q)}
            primary = self._pick_primary(chains)
            craft_bases[cid] = {
                "qualification_id": q["id"], "source_kind": q["source_kind"],
                "basis_origin": "lineage", "chains": chains, "primary": primary,
                "valid_chain_count": len(chains),
            }
        basis = {
            "regime": REGIME_ENDORSEMENT,
            "at": at,
            "signer_id": signer_id,
            "product_id": prod["id"],
            "product_type_id": prod["product_type_id"],
            "post": post,
            "crafts": craft_bases,
            "lineage_policy": LINEAGE_POLICY,
            "snapshot": "immutable; reflects the basis actually used at signing time",
        }
        return {"ok": True, "basis": basis}

    def _evaluate_periodic(self, conn, prod, signer_id, at, crafts, post) -> dict:
        day = tu.date_of(at)
        craft_bases = {}
        for cid in crafts:
            cert = self._valid_certificate(conn, signer_id, cid, day)
            if not cert:
                return {"ok": False, "error": "NO_VALID_CERTIFICATE",
                        "message": f"signer holds no valid certificate for craft {cid} on {day}",
                        "details": {"craft_id": cid, "date": day},
                        "qualification_still_valid": self._quals_snapshot(conn, signer_id, prod, at)}
            craft_bases[cid] = {
                "certificate_id": cert["id"], "period_id": cert["period_id"],
                "valid_from": cert["valid_from"], "valid_until": cert["valid_until"],
                "issuance_basis": json.loads(cert["basis_json"]),
            }
        basis = {
            "regime": REGIME_PERIODIC,
            "at": at,
            "date": day,
            "signer_id": signer_id,
            "product_id": prod["id"],
            "product_type_id": prod["product_type_id"],
            "post": post,
            "crafts": craft_bases,
            "note": "signed against an issued period certificate per the review policy in force",
        }
        return {"ok": True, "basis": basis}

    # ---------------------------------------------------------------- periods
    def create_period(self, start_date: str, end_date: str,
                      review_policy: str = "honor_through_period", period_id: str = None) -> dict:
        if review_policy not in ("honor_through_period", "revoke_on_retraction"):
            raise ValidationError(f"unknown review policy: {review_policy}")
        sd = tu.norm_date(start_date)
        ed = tu.norm_date(end_date)
        if ed < sd:
            raise ValidationError("period end_date must be on/after start_date")
        pid = period_id or _new_id("period")
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM periods WHERE id=?", (pid,)).fetchone():
                raise Conflict("DUPLICATE", f"period exists: {pid}")
            conn.execute(
                "INSERT INTO periods(id, start_date, end_date, review_policy, status) VALUES(?,?,?,?,?)",
                (pid, sd, ed, review_policy, "open"),
            )
            self._audit(conn, "create", "period", pid, {"start": sd, "end": ed, "policy": review_policy})
        return self.get_period(pid)

    def get_period(self, period_id: str) -> dict:
        with self.db.read() as conn:
            p = _one(conn.execute("SELECT * FROM periods WHERE id=?", (period_id,)))
            if not p:
                raise NotFound("period", period_id)
            p["certificates"] = _rows(conn.execute(
                "SELECT * FROM certificates WHERE period_id=? ORDER BY person_id, craft_id", (period_id,)))
        return p

    def open_period(self, period_id: str, roster: List[str], craft_ids: List[str] = None,
                    issued_at: str = None) -> dict:
        """Verify valid experience + certified mentors, then issue time-limited certs."""
        at = tu.norm_dt(issued_at) if issued_at else tu.now_str()
        with self.db.transaction() as conn:
            period = _one(conn.execute("SELECT * FROM periods WHERE id=?", (period_id,)))
            if not period:
                raise NotFound("period", period_id)
            if craft_ids is None:
                craft_ids = [r["id"] for r in conn.execute("SELECT id FROM crafts WHERE required=1")]
            issued, skipped = [], []
            for person_id in roster:
                if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                    raise NotFound("person", person_id)
                for cid in craft_ids:
                    ok, why, basis = self._issuance_check(conn, person_id, cid, at)
                    if not ok:
                        skipped.append({"person_id": person_id, "craft_id": cid, "reason": why})
                        continue
                    cid_cert = _new_id("cert")
                    conn.execute(
                        "INSERT INTO certificates(id, period_id, person_id, craft_id, capacity, "
                        "valid_from, valid_until, basis_json, status, created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (cid_cert, period_id, person_id, cid, CAP_PRODUCTION,
                         period["start_date"], period["end_date"],
                         json.dumps(basis, sort_keys=True), "active", tu.now_str()),
                    )
                    issued.append({"certificate_id": cid_cert, "person_id": person_id, "craft_id": cid})
            conn.execute("UPDATE periods SET opened_at=?, status='open' WHERE id=?", (at, period_id))
            self._audit(conn, "open", "period", period_id,
                        {"roster": roster, "issued": len(issued), "skipped": len(skipped)})
            return {"period_id": period_id, "issued": issued, "skipped": skipped}

    def _issuance_check(self, conn, person_id: str, craft_id: str, at: str):
        """Eligibility AND a still-certified mentor AND a full lineage to a root.

        "Re-verify the lineage along the way": a person is only certified at
        period opening if every cited practicum is still usable *and* they can
        still be walked down to a root through mentors who are certified now.
        """
        elig = self.eligibility(person_id, craft_id, at)
        is_root = self._is_root(conn, person_id, craft_id, at)
        if not elig["eligible_for_production"] and not is_root:
            return False, "insufficient_valid_experience", elig
        # A root mentor is the origin of authority: there is no upstream
        # apprenticeship to cite, so root standing itself certifies them.
        if is_root:
            basis = {
                "regime": REGIME_PERIODIC,
                "person_id": person_id,
                "craft_id": craft_id,
                "issued_at": at,
                "basis_origin": "root_authority",
                "cited_evidence": [],
                "is_root": True,
                "verified_lineage_policy": LINEAGE_POLICY,
            }
            return True, None, basis
        chains = self.lineage_chains(conn, person_id, craft_id, at)
        if not chains:
            return False, "lineage_broken_to_root", {"craft_id": craft_id, "at": at}
        cited = []
        for eid in elig["usable_evidence"]:
            sup_now = None
            for r in conn.execute("SELECT person_id FROM evidence_mentors WHERE evidence_id=?", (eid,)):
                q = self._mentoring_valid_at(conn, r["person_id"], craft_id, at)
                if q:
                    sup_now = (r["person_id"], q["id"])
                    break
            if not sup_now:
                return False, "no_certified_mentor_now", {"evidence_id": eid}
            cited.append({"evidence_id": eid, "mentor_id": sup_now[0], "mentor_qualification_id": sup_now[1]})
        basis = {
            "regime": REGIME_PERIODIC,
            "person_id": person_id,
            "craft_id": craft_id,
            "issued_at": at,
            "accrued_minutes": elig["accrued_minutes"],
            "cited_evidence": cited,
            "is_root": is_root,
            "valid_lineage_count": len(chains),
            "primary_lineage": self._pick_primary(chains) if chains and chains[0] else [],
            "verified_lineage_policy": LINEAGE_POLICY,
        }
        return True, None, basis

    def review_period(self, period_id: str, at_ts: str = None) -> dict:
        """Apply the period's explicit review policy to already-issued certs."""
        at = tu.norm_dt(at_ts) if at_ts else tu.now_str()
        with self.db.transaction() as conn:
            period = _one(conn.execute("SELECT * FROM periods WHERE id=?", (period_id,)))
            if not period:
                raise NotFound("period", period_id)
            policy = period["review_policy"]
            affected = []
            certs = _rows(conn.execute(
                "SELECT * FROM certificates WHERE period_id=? AND status='active'", (period_id,)))
            for cert in certs:
                basis = json.loads(cert["basis_json"])
                broken = []
                for c in basis.get("cited_evidence", []):
                    ev = _one(conn.execute("SELECT * FROM evidence WHERE id=?", (c["evidence_id"],)))
                    if ev and ev["status"] == "retracted":
                        broken.append(c["evidence_id"])
                if not broken:
                    continue
                if policy == "revoke_on_retraction":
                    conn.execute(
                        "UPDATE certificates SET status='revoked', review_decision=? WHERE id=?",
                        (f"revoked at {at}: retracted evidence {broken}", cert["id"]),
                    )
                    affected.append({"certificate_id": cert["id"], "decision": "revoked",
                                     "retracted_evidence": broken})
                else:  # honor_through_period
                    conn.execute(
                        "UPDATE certificates SET review_decision=? WHERE id=?",
                        (f"honored through period despite retracted evidence {broken} (policy)", cert["id"]),
                    )
                    affected.append({"certificate_id": cert["id"], "decision": "honored_through_period",
                                     "retracted_evidence": broken})
            self._audit(conn, "review", "period", period_id, {"policy": policy, "affected": affected})
            return {"period_id": period_id, "policy": policy, "affected": affected,
                    "note": "signatures already made are never erased; only certificate status changes"}

    def _valid_certificate(self, conn, person_id: str, craft_id: str, day: str) -> Optional[dict]:
        rows = _rows(conn.execute(
            "SELECT * FROM certificates WHERE person_id=? AND craft_id=? AND status='active' "
            "ORDER BY valid_from", (person_id, craft_id)))
        for c in rows:
            if c["valid_from"] <= day <= c["valid_until"]:
                return c
        return None

    def list_certificates(self, person_id: str = None, craft_id: str = None,
                          on_date: str = None) -> List[dict]:
        clause, params = [], []
        if person_id:
            clause.append("person_id=?"); params.append(person_id)
        if craft_id:
            clause.append("craft_id=?"); params.append(craft_id)
        where = (" WHERE " + " AND ".join(clause)) if clause else ""
        with self.db.read() as conn:
            rows = _rows(conn.execute(f"SELECT * FROM certificates{where} ORDER BY created_at", params))
            for r in rows:
                r["basis"] = json.loads(r["basis_json"])
                if on_date:
                    r["valid_on_date"] = (r["status"] == "active" and
                                          r["valid_from"] <= tu.norm_date(on_date) <= r["valid_until"])
            return rows

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _norm_boundary(value: str, end: bool) -> str:
        if value is None:
            raise ValidationError("boundary required")
        s = value.strip()
        if len(s) == 10:  # date only
            return tu.norm_dt(s + ("T23:59:59Z" if end else "T00:00:00Z"))
        return tu.norm_dt(s)

    def health(self) -> dict:
        with self.db.read() as conn:
            counts = {}
            for t in ("people", "crafts", "evidence", "qualifications", "products",
                      "signatures", "periods", "certificates"):
                counts[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        return {"ok": True, "regime": self.get_regime(), "counts": counts}

    def reset(self) -> dict:
        with self.db.transaction() as conn:
            for t in ("signature_requests", "signatures", "products", "certificates", "periods",
                      "qualifications", "evidence_steps", "evidence_students", "evidence_mentors",
                      "evidence", "shifts", "product_type_crafts", "product_types",
                      "craft_steps", "crafts", "stations", "posts", "people", "audit_log", "settings"):
                conn.execute(f"DELETE FROM {t}")
        return {"ok": True, "reset": True}

    def audit_trail(self, entity_id: str = None, limit: int = 200) -> List[dict]:
        with self.db.read() as conn:
            if entity_id:
                rows = _rows(conn.execute(
                    "SELECT * FROM audit_log WHERE entity_id=? ORDER BY id DESC LIMIT ?",
                    (entity_id, limit)))
            else:
                rows = _rows(conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)))
            for r in rows:
                r["details"] = json.loads(r["details_json"])
            return rows
