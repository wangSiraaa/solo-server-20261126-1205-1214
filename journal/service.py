"""Public service API for the translation-journal crowdfunding desk.

Everything the editorial desk does -- registering an issue, taking and
withdrawing pledges, deciding what to open, and letting readers check a
receipt -- goes through :class:`JournalService`.  All state lives in SQLite, so
the same calls after a restart see the same data, and a repeated cutoff/open
request with the same ``request_id`` returns the stored receipt instead of
deducting again.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .db import Database
from .engine import (
    feasible_candidates,
    rolling_feasible,
    solve_plan,
)
from .model import PieceView, PledgeView, World


class JournalError(Exception):
    """A caller-side error (bad input, unknown id, illegal state)."""


class NeedsEditorDecision(Exception):
    """More than one batch is feasible; the editor must pick one.

    Carries the feasible candidates so the caller can choose and record the
    choice in the issue's receipt.
    """

    def __init__(self, issue_id: str, candidates: list[dict]):
        self.issue_id = issue_id
        self.candidates = candidates
        super().__init__(
            f"issue {issue_id}: {len(candidates)} feasible batches; "
            "choose one and pass its candidate_id"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JournalService:
    def __init__(self, path=":memory:"):
        self.db = Database(path)
        # Keep an open handle for the in-memory case used by tests; file DBs
        # are opened per transaction.

    # ------------------------------------------------------------------ setup
    def create_issue(self, issue_id: str, title: str = "", mode: str = "joint") -> dict:
        if mode not in ("joint", "rolling"):
            raise JournalError(f"mode must be 'joint' or 'rolling', got {mode!r}")
        with self.db.transaction() as c:
            if _issue_exists(c, issue_id):
                raise JournalError(f"issue {issue_id!r} already exists")
            c.execute(
                "INSERT INTO issues (id, title, mode, status, created_at) "
                "VALUES (?,?,?,?,?)",
                (issue_id, title, mode, "open", _now()),
            )
        return {"issue_id": issue_id, "mode": mode, "status": "open"}

    def add_translator(self, translator_id: str, name: str = "",
                       slots: list[str] | None = None) -> dict:
        slots = slots or []
        with self.db.transaction() as c:
            row = c.execute(
                "SELECT id FROM translators WHERE id=?", (translator_id,)
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO translators (id, name) VALUES (?,?)",
                    (translator_id, name or translator_id),
                )
            for i, label in enumerate(slots):
                c.execute(
                    "INSERT OR IGNORE INTO translator_slots "
                    "(translator_id, ord, label) VALUES (?,?,?)",
                    (translator_id, i, label),
                )
        return {"translator_id": translator_id, "slots": slots}

    def add_piece(self, issue_id: str, piece_id: str, title: str,
                  points_required: int, duration: int,
                  eligible_translators: list[str],
                  wish_points: int = 0) -> dict:
        if points_required < 0:
            raise JournalError("points_required must be >= 0")
        if duration < 1:
            raise JournalError("duration must be >= 1")
        with self.db.transaction() as c:
            _require_issue(c, issue_id)
            if c.execute(
                "SELECT 1 FROM pieces WHERE issue_id=? AND piece_id=?",
                (issue_id, piece_id),
            ).fetchone():
                raise JournalError(f"piece {piece_id!r} already exists")
            c.execute(
                "INSERT INTO pieces (issue_id, piece_id, title, points_required,"
                " duration, wish_points) VALUES (?,?,?,?,?,?)",
                (issue_id, piece_id, title, points_required, duration, wish_points),
            )
            for t in eligible_translators:
                c.execute(
                    "INSERT OR IGNORE INTO piece_translators "
                    "(issue_id, piece_id, translator_id) VALUES (?,?,?)",
                    (issue_id, piece_id, t),
                )
        return {"issue_id": issue_id, "piece_id": piece_id}

    # ---------------------------------------------------------------- pledges
    def pledge(self, issue_id: str, pledge_id: str, supporter: str, points: int,
               accepts: list[str] | None = None,
               requires: list[str] | None = None,
               supporter_cap: int | None = None) -> dict:
        """Record a pledge.

        ``accepts`` empty means "any piece in this issue".  ``requires`` lists
        pieces that must also be opened for this money to become usable.
        ``supporter_cap`` (optional) sets this supporter's *per-issue* ceiling;
        it is a ceiling over all of the supporter's pledges together, never a
        per-piece allowance.
        """
        if points < 0:
            raise JournalError("points must be >= 0")
        with self.db.transaction() as c:
            _require_issue(c, issue_id)
            if c.execute(
                "SELECT 1 FROM pledges WHERE issue_id=? AND pledge_id=?",
                (issue_id, pledge_id),
            ).fetchone():
                raise JournalError(f"pledge {pledge_id!r} already exists")
            c.execute(
                "INSERT INTO pledges (issue_id, pledge_id, supporter, points,"
                " active, created_at) VALUES (?,?,?,?,1,?)",
                (issue_id, pledge_id, supporter, points, _now()),
            )
            for p in accepts or []:
                c.execute(
                    "INSERT INTO pledge_accepts (issue_id, pledge_id, piece_id)"
                    " VALUES (?,?,?)",
                    (issue_id, pledge_id, p),
                )
            for p in requires or []:
                c.execute(
                    "INSERT INTO pledge_requires (issue_id, pledge_id, piece_id)"
                    " VALUES (?,?,?)",
                    (issue_id, pledge_id, p),
                )
            if supporter_cap is not None:
                if supporter_cap < 0:
                    raise JournalError("supporter_cap must be >= 0")
                c.execute(
                    "INSERT INTO supporter_caps (issue_id, supporter, cap)"
                    " VALUES (?,?,?) ON CONFLICT(issue_id, supporter)"
                    " DO UPDATE SET cap=excluded.cap",
                    (issue_id, supporter, supporter_cap),
                )
        return {"issue_id": issue_id, "pledge_id": pledge_id, "supporter": supporter}

    def withdraw_pledge(self, issue_id: str, pledge_id: str,
                        request_id: str | None = None) -> dict:
        with self.db.transaction() as c:
            row = c.execute(
                "SELECT active FROM pledges WHERE issue_id=? AND pledge_id=?",
                (issue_id, pledge_id),
            ).fetchone()
            if row is None:
                raise JournalError(f"unknown pledge {pledge_id!r}")
            if not row["active"]:
                return {"issue_id": issue_id, "pledge_id": pledge_id,
                        "active": False}
            # Only a pledge with no deductions yet can be fully withdrawn.
            spent = _pledge_deducted(c, issue_id, pledge_id)
            if spent > 0:
                raise JournalError(
                    f"pledge {pledge_id!r} already paid {spent} point(s); "
                    "cannot withdraw"
                )
            c.execute(
                "UPDATE pledges SET active=0 WHERE issue_id=? AND pledge_id=?",
                (issue_id, pledge_id),
            )
        return {"issue_id": issue_id, "pledge_id": pledge_id, "active": False}

    # --------------------------------------------------------------- decision
    def list_candidates(self, issue_id: str) -> list[dict]:
        """Read-only survey of what could be opened.  Never reserves slots."""
        world = self._world(issue_id)
        if world.mode == "rolling":
            out = []
            for pid in sorted(world.pieces):
                if pid in world.opened:
                    continue
                plan = rolling_feasible(world, pid)
                if plan is not None:
                    out.append(plan.to_public())
            return out
        return [p.to_public() for p in feasible_candidates(world)]

    # ----------------------------------------------------------------- opening
    def open_joint(self, issue_id: str, request_id: str,
                   selection: str | list[str] | None = None) -> dict:
        """One atomic cutoff for a joint issue.

        Pieces, their conditional pledges and the translators' slots are
        realised in the *same* transaction.  If ``selection`` is omitted and
        exactly one maximal batch is feasible it is chosen automatically;
        otherwise the editor must pass the ``candidate_id`` (or a piece list).
        """
        cached = self._replayed(request_id, issue_id, "open_joint")
        if cached is not None:
            return cached

        with self.db.transaction() as c:
            # Re-check inside the serialised write transaction so a racing
            # duplicate returns the stored receipt instead of double-charging.
            cached = self._replayed_on(c, request_id, issue_id, "open_joint")
            if cached is not None:
                return cached
            world = self._world_in(c, issue_id)
            if world.mode != "joint":
                raise JournalError(f"issue {issue_id!r} is not a joint issue")
            candidates = feasible_candidates(world)
            if not candidates:
                raise JournalError("no feasible batch can be opened right now")

            chosen = self._select(candidates, selection, issue_id,
                                  need_decision=True, world=world)
            satisfied = frozenset(chosen) | world.opened
            plan = solve_plan(world, list(chosen), satisfied)
            if plan is None:  # defensive: selection must be re-validated
                raise JournalError("selected batch is not feasible")

            receipt = self._commit_plan(c, issue_id, request_id, plan, "open_joint")
        return receipt

    def open_piece(self, issue_id: str, request_id: str,
                   piece_id: str | None = None) -> dict:
        """Rolling opening: realise exactly one piece *now*.

        Only unconditional money, or money whose prerequisite pieces are
        already opened, may be used.  The grabbed slot and the points are
        locked immediately, so later pieces cannot borrow an un-opened piece
        as surety.
        """
        cached = self._replayed(request_id, issue_id, "open_piece")
        if cached is not None:
            return cached

        with self.db.transaction() as c:
            cached = self._replayed_on(c, request_id, issue_id, "open_piece")
            if cached is not None:
                return cached
            world = self._world_in(c, issue_id)
            if world.mode != "rolling":
                raise JournalError(f"issue {issue_id!r} is not a rolling issue")

            if piece_id is None:
                ids = [p for p in sorted(world.pieces) if p not in world.opened
                       and rolling_feasible(world, p) is not None]
                if not ids:
                    raise JournalError("no piece can be opened right now")
                piece_id = ids[0]

            if piece_id not in world.pieces:
                raise JournalError(f"unknown piece {piece_id!r}")
            if piece_id in world.opened:
                raise JournalError(f"piece {piece_id!r} already opened")
            plan = rolling_feasible(world, piece_id)
            if plan is None:
                raise JournalError(
                    f"piece {piece_id!r} cannot be opened right now "
                    "(slot taken or no usable unconditional funding)"
                )

            receipt = self._commit_plan(c, issue_id, request_id, plan, "open_piece")
        return receipt

    # ------------------------------------------------------------------ reads
    def get_receipt(self, receipt_id: str) -> dict:
        return self._load_receipt(receipt_id)

    def query_issue(self, issue_id: str) -> dict:
        """Reader query: what is open, who backs it, who translates it."""
        c = self.db.connect()
        row = c.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        if row is None:
            raise JournalError(f"unknown issue {issue_id!r}")
        pieces = self._pieces(c, issue_id)
        opened = self._opened(c, issue_id)
        assignments = {}
        for pid in opened:
            r = c.execute(
                "SELECT receipt_id, translator_id, slots FROM assignments "
                "WHERE issue_id=? AND piece_id=? ORDER BY rowid DESC LIMIT 1",
                (issue_id, pid),
            ).fetchone()
            if r:
                assignments[pid] = {
                    "translator_id": r["translator_id"],
                    "slots": json.loads(r["slots"]),
                    "receipt_id": r["receipt_id"],
                }
        # Funding trace per piece from every receipt's deductions.
        funding: dict[str, list[dict]] = {pid: [] for pid in pieces}
        for d in c.execute(
            "SELECT pledge_id, supporter, piece_id, points FROM deductions "
            "WHERE issue_id=? ORDER BY rowid", (issue_id,)
        ):
            funding.setdefault(d["piece_id"], []).append(
                {"pledge_id": d["pledge_id"], "supporter": d["supporter"],
                 "points": d["points"]}
            )
        pledges = self._pledges(c, issue_id, pieces)
        return {
            "issue_id": issue_id,
            "title": row["title"],
            "mode": row["mode"],
            "status": row["status"],
            "pieces": {
                pid: {
                    "title": pv.title,
                    "points_required": pv.points_required,
                    "duration": pv.duration,
                    "eligible": list(pv.eligible),
                    "wish_points": pv.wish_points,   # display only, never money
                    "opened": pid in opened,
                    "translator": assignments.get(pid),
                    "funding": funding.get(pid, []),
                }
                for pid, pv in pieces.items()
            },
            "pledges": [
                {
                    "pledge_id": p.pledge_id,
                    "supporter": p.supporter,
                    "points": p.points,
                    "remaining": p.remaining,
                    "accepts": list(p.accepts),
                    "requires": list(p.requires),
                    "active": p.active,
                }
                for p in pledges
            ],
            "supporter_caps": self._supporter_caps(c, issue_id),
            "receipts": [
                r["receipt_id"]
                for r in c.execute(
                    "SELECT receipt_id FROM receipts WHERE issue_id=?"
                    " ORDER BY rowid", (issue_id,)
                )
            ],
        }

    # --------------------------------------------------------------- internals
    def _world(self, issue_id: str) -> World:
        return self._world_in(self.db.connect(), issue_id)

    def _world_in(self, c, issue_id: str) -> World:
        _require_issue(c, issue_id)
        row = c.execute("SELECT mode FROM issues WHERE id=?", (issue_id,)).fetchone()
        pieces = self._pieces(c, issue_id)
        opened = frozenset(self._opened(c, issue_id))

        free: dict[str, list[tuple[int, str]]] = {}
        for ts in c.execute(
            "SELECT translator_id, ord, label FROM translator_slots "
            "ORDER BY translator_id, ord"
        ):
            free.setdefault(ts["translator_id"], []).append(
                (ts["ord"], ts["label"])
            )
        # Remove globally booked shifts.
        booked = {
            (b["translator_id"], b["label"])
            for b in c.execute("SELECT translator_id, label FROM slot_bookings")
        }
        for t in free:
            free[t] = [(o, lbl) for (o, lbl) in free[t] if (t, lbl) not in booked]

        pledges = tuple(self._pledges(c, issue_id, pieces))
        caps = self._supporter_caps(c, issue_id)
        deducted_sup: dict[str, int] = {}
        for d in c.execute(
            "SELECT supporter, SUM(points) AS s FROM deductions "
            "WHERE issue_id=? GROUP BY supporter", (issue_id,)
        ):
            deducted_sup[d["supporter"]] = d["s"]
        # A supporter with no explicit per-issue ceiling is capped by the
        # combined size of their own pledges (never a per-piece allowance).
        supporters = {p.supporter for p in pledges}
        effective_caps = {}
        for s in supporters:
            if s in caps:
                effective_caps[s] = caps[s]
            else:
                effective_caps[s] = sum(
                    p.points for p in pledges if p.supporter == s
                )
        supporter_remaining = {
            s: effective_caps[s] - deducted_sup.get(s, 0) for s in supporters
        }
        return World(
            issue_id=issue_id,
            mode=row["mode"],
            pieces=pieces,
            opened=opened,
            free_slots=free,
            pledges=pledges,
            supporter_remaining=supporter_remaining,
            supporter_caps=effective_caps,
        )

    def _pieces(self, c, issue_id: str) -> dict[str, PieceView]:
        out = {}
        for r in c.execute(
            "SELECT * FROM pieces WHERE issue_id=?", (issue_id,)
        ):
            elig = tuple(
                e["translator_id"]
                for e in c.execute(
                    "SELECT translator_id FROM piece_translators "
                    "WHERE issue_id=? AND piece_id=? ORDER BY translator_id",
                    (issue_id, r["piece_id"]),
                )
            )
            out[r["piece_id"]] = PieceView(
                piece_id=r["piece_id"],
                title=r["title"],
                points_required=r["points_required"],
                duration=r["duration"],
                wish_points=r["wish_points"],
                eligible=elig,
            )
        return out

    def _pledges(self, c, issue_id: str, pieces) -> list[PledgeView]:
        out = []
        for r in c.execute(
            "SELECT * FROM pledges WHERE issue_id=? ORDER BY rowid", (issue_id,)
        ):
            accepts = tuple(
                a["piece_id"]
                for a in c.execute(
                    "SELECT piece_id FROM pledge_accepts WHERE issue_id=?"
                    " AND pledge_id=? ORDER BY piece_id",
                    (issue_id, r["pledge_id"]),
                )
            )
            requires = tuple(
                a["piece_id"]
                for a in c.execute(
                    "SELECT piece_id FROM pledge_requires WHERE issue_id=?"
                    " AND pledge_id=? ORDER BY piece_id",
                    (issue_id, r["pledge_id"]),
                )
            )
            spent = _pledge_deducted(c, issue_id, r["pledge_id"])
            out.append(
                PledgeView(
                    pledge_id=r["pledge_id"],
                    supporter=r["supporter"],
                    points=r["points"],
                    remaining=r["points"] - spent,
                    accepts=accepts,
                    requires=requires,
                    active=bool(r["active"]),
                )
            )
        return out

    def _opened(self, c, issue_id: str) -> list[str]:
        return [
            r["piece_id"]
            for r in c.execute(
                "SELECT piece_id FROM opened_pieces WHERE issue_id=?", (issue_id,)
            )
        ]

    def _supporter_caps(self, c, issue_id: str) -> dict[str, int]:
        return {
            r["supporter"]: r["cap"]
            for r in c.execute(
                "SELECT supporter, cap FROM supporter_caps WHERE issue_id=?",
                (issue_id,),
            )
        }

    def _select(self, candidates, selection, issue_id, need_decision: bool,
                world=None) -> tuple[str, ...]:
        """Resolve the editor's choice to a tuple of piece ids.

        ``selection`` may be a ``candidate_id`` (must be one of the offered
        maximal batches) or an explicit piece list, which is accepted if it can
        itself be opened as a smaller feasible subset.
        """
        if selection is None:
            if len(candidates) == 1:
                return tuple(candidates[0].pieces)
            if need_decision:
                raise NeedsEditorDecision(
                    issue_id,
                    [c.to_public() for c in candidates],
                )
            raise JournalError("no candidate to open")
        if isinstance(selection, str):
            for c in candidates:
                if c.candidate_id() == selection:
                    return tuple(c.pieces)
            raise JournalError(f"no feasible batch with candidate_id {selection!r}")
        if isinstance(selection, list):
            wanted = tuple(sorted(selection))
            if world is not None and wanted and solve_plan(
                world, list(wanted), frozenset(wanted) | world.opened
            ) is not None:
                return wanted
            for c in candidates:
                if tuple(c.pieces) == wanted:
                    return wanted
            raise JournalError(f"piece list {selection!r} is not a feasible batch")
        raise JournalError("selection must be a candidate_id or a piece list")

    def _commit_plan(self, c, issue_id: str, request_id: str, plan, kind: str) -> dict:
        receipt_id = "rcpt_" + uuid.uuid4().hex[:16]

        for pid, asg in plan.assignments.items():
            for slot in asg.slots:
                # Global exclusivity guard, enforced by the DB unique key.
                if c.execute(
                    "SELECT 1 FROM slot_bookings WHERE translator_id=? AND label=?",
                    (asg.translator_id, slot),
                ).fetchone():
                    raise JournalError(
                        f"slot {asg.translator_id}/{slot} already booked"
                    )
                c.execute(
                    "INSERT INTO slot_bookings "
                    "(translator_id, label, issue_id, piece_id, receipt_id)"
                    " VALUES (?,?,?,?,?)",
                    (asg.translator_id, slot, issue_id, pid, receipt_id),
                )
            c.execute(
                "INSERT INTO assignments "
                "(receipt_id, issue_id, piece_id, translator_id, slots)"
                " VALUES (?,?,?,?,?)",
                (receipt_id, issue_id, pid, asg.translator_id,
                 json.dumps(asg.slots)),
            )

        for pid in plan.pieces:
            for line in plan.funding[pid]:
                c.execute(
                    "INSERT INTO deductions "
                    "(receipt_id, issue_id, pledge_id, supporter, piece_id, points)"
                    " VALUES (?,?,?,?,?,?)",
                    (receipt_id, issue_id, line.pledge_id, line.supporter,
                     pid, line.points),
                )
            c.execute(
                "INSERT INTO opened_pieces (issue_id, piece_id, receipt_id)"
                " VALUES (?,?,?)",
                (issue_id, pid, receipt_id),
            )

        payload = {
            "receipt_id": receipt_id,
            "issue_id": issue_id,
            "kind": kind,
            "request_id": request_id,
            "created_at": _now(),
            "candidate_id": plan.candidate_id(),
            "pieces": plan.to_public()["pieces"],
            "supporter_deducted": dict(plan.supporter_deducted),
            "pledge_deducted": dict(plan.pledge_deducted),
        }
        c.execute(
            "INSERT INTO receipts (receipt_id, issue_id, request_id, kind,"
            " payload, created_at) VALUES (?,?,?,?,?,?)",
            (receipt_id, issue_id, request_id, kind,
             json.dumps(payload), payload["created_at"]),
        )
        c.execute(
            "INSERT INTO request_log (request_id, issue_id, action, receipt_id)"
            " VALUES (?,?,?,?)",
            (request_id, issue_id, kind, receipt_id),
        )
        c.execute(
            "UPDATE issues SET status='opened' WHERE id=? AND mode='joint'",
            (issue_id,),
        )
        return payload

    def _replayed(self, request_id: str, issue_id: str, action: str) -> dict | None:
        return self._replayed_on(self.db.connect(), request_id, issue_id, action)

    def _replayed_on(self, c, request_id: str, issue_id: str, action: str) -> dict | None:
        row = c.execute(
            "SELECT issue_id, action, receipt_id FROM request_log WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["issue_id"] != issue_id or row["action"] != action:
            raise JournalError(
                f"request_id {request_id!r} was already used for "
                f"{row['action']} on issue {row['issue_id']!r}"
            )
        return self._load_receipt(row["receipt_id"])

    def _load_receipt(self, receipt_id: str) -> dict:
        row = self.db.connect().execute(
            "SELECT payload FROM receipts WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        if row is None:
            raise JournalError(f"unknown receipt {receipt_id!r}")
        return json.loads(row["payload"])

    # small helper used only when raising NeedsEditorDecision
    _current_issue = None

def _issue_exists(c, issue_id: str) -> bool:
    return c.execute("SELECT 1 FROM issues WHERE id=?", (issue_id,)).fetchone() is not None


def _require_issue(c, issue_id: str) -> None:
    if not _issue_exists(c, issue_id):
        raise JournalError(f"unknown issue {issue_id!r}")


def _pledge_deducted(c, issue_id: str, pledge_id: str) -> int:
    row = c.execute(
        "SELECT COALESCE(SUM(points),0) AS s FROM deductions "
        "WHERE issue_id=? AND pledge_id=?",
        (issue_id, pledge_id),
    ).fetchone()
    return int(row["s"])
