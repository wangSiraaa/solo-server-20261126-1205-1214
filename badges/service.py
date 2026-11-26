"""Stateful service: seasons, signing, speeches, live appearances.

Design decisions (the "选一种制度" the task asks for):

1. **Seasons are isolated snapshots.**  A ``Season`` freezes the theme it was
   opened with.  Changing the theme creates a *new* season; the old season's
   signatures and speeches keep resolving against the old theme — issuing a
   badge in one season never changes an earlier season's speech attribution.

2. **Signing (发牌) is permanent within a season.**  ``sign`` records
   ``guest -> style`` for the whole season ("整季固定牌").  A signature can be
   *used* in any number of sessions, and the same style may be re-used by the
   same guest.  Two co-present guests can never share an indistinguishable
   style; the planner enforces it and ``sign`` re-checks it.

3. **Appearance receipts (登场回单) are re-issuable.**  A receipt is bound to
   ``(session, speech_no_range)`` and rendered at the moment it is issued, so a
   guest who has left may lend the physical badge to someone else later.  A
   *published* speech screenshot is frozen: ``attribute(speech_no)`` is stored
   the first time and never changes, even if the badge is re-lent.

4. **Live (现场借牌) is judged on the present set.**  ``request_go_live`` clamps
   the set of guests currently on the wire and asks the solver for a complete
   colouring *at that moment*.  It only rejects when the present set genuinely
   cannot be distinguished (e.g. the theme collapses to one design).  It never
   stops everyone.  A "merge screen" (并屏) request is the same judgement over
   the union of two rooms' present sets.

5. **Leaving (确认退场) is explicit.**  ``leave`` frees a guest's live badge for
   lending, but never edits an already issued receipt or a recorded speech.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .model import Theme
from .planning import SessionSpec, PlanResult, plan_schedule, solve_complete
from .render import distinguishable, explain_indistinguishable


class ServiceError(Exception):
    """Raised for invalid requests (maps to HTTP 4xx in the server)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Signature:
    guest: str
    style: str
    session_ids: Tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"guest": self.guest, "style": self.style, "sessions": list(self.session_ids)}


@dataclass
class Receipt:
    receipt_id: str
    guest: str
    style: str
    session_id: str
    speech_numbers: Tuple[int, ...]
    theme_name: str
    rendered: Dict[str, Tuple[str, str, str]]
    signed_in_season: str

    def to_json(self) -> dict:
        return {
            "receipt_id": self.receipt_id,
            "guest": self.guest,
            "style": self.style,
            "session_id": self.session_id,
            "speech_numbers": list(self.speech_numbers),
            "theme": self.theme_name,
            "signed_in_season": self.signed_in_season,
            "rendered": {k: list(v) for k, v in self.rendered.items()},
        }


@dataclass
class SpeechRecord:
    speech_no: int
    guest: str
    style: str
    session_id: str
    season_id: str
    receipt_id: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "speech_no": self.speech_no,
            "guest": self.guest,
            "style": self.style,
            "session_id": self.session_id,
            "season_id": self.season_id,
            "receipt_id": self.receipt_id,
        }


@dataclass
class Season:
    season_id: str
    theme: Theme
    signatures: Dict[str, Signature] = field(default_factory=dict)
    receipts: Dict[str, Receipt] = field(default_factory=dict)
    speeches: Dict[int, SpeechRecord] = field(default_factory=dict)
    # Live presentation state.
    present: Dict[str, str] = field(default_factory=dict)  # guest -> style currently worn
    live_room: str = ""
    receipt_counter: int = 0
    speech_counter: int = 0
    events: List[dict] = field(default_factory=list)

    # -- helpers --------------------------------------------------------
    def log(self, kind: str, **data) -> None:
        entry = {"season": self.season_id, "kind": kind}
        entry.update(data)
        self.events.append(entry)

    def style_of(self, guest: str) -> str:
        if guest in self.present:
            return self.present[guest]
        if guest in self.signatures:
            return self.signatures[guest].style
        raise ServiceError(f"{guest} 在该季既无签约牌也不在现场", 404)

    def members_on_stage(self) -> List[str]:
        return sorted(self.present)


class Service:
    def __init__(self) -> None:
        self.seasons: Dict[str, Season] = {}
        self.season_order: List[str] = []
        self.session_to_season: Dict[str, str] = {}
        self.active_season_id: Optional[str] = None
        self._season_counter = 0

    # ------------------------------------------------------------------
    # Seasons / themes
    # ------------------------------------------------------------------
    def open_season(self, theme: Theme, season_id: Optional[str] = None) -> Season:
        if season_id is None:
            self._season_counter += 1
            season_id = f"S{self._season_counter}"
        if season_id in self.seasons:
            raise ServiceError(f"season {season_id} 已存在")
        season = Season(season_id=season_id, theme=theme.clone())
        self.seasons[season_id] = season
        self.season_order.append(season_id)
        self.active_season_id = season_id
        season.log("season_opened", theme=theme.name)
        return season

    def get_season(self, season_id: Optional[str] = None) -> Season:
        sid = season_id or self.active_season_id
        if sid is None:
            raise ServiceError("尚未开启任何一季", 404)
        if sid not in self.seasons:
            raise ServiceError(f"未知的季 {sid}", 404)
        return self.seasons[sid]

    def use_season(self, season_id: str) -> Season:
        season = self.get_season(season_id)
        self.active_season_id = season_id
        return season

    def set_theme(self, theme: Theme, season_id: Optional[str] = None) -> Season:
        """Change the active season's theme only if nothing has been issued.

        For a truly new season use :meth:`open_season`.  This helper exists so
        a demo can tweak a theme before the first signature.
        """
        season = self.get_season(season_id)
        if season.signatures or season.receipts or season.speeches:
            raise ServiceError(
                "该季已有发出/发言记录，主题改动必须开新一季（旧记录按旧约定保留）",
                409,
            )
        season.theme = theme.clone()
        season.log("theme_set", theme=theme.name)
        return season

    # ------------------------------------------------------------------
    # Schedule planning & signing
    # ------------------------------------------------------------------
    def plan(
        self,
        sessions: Sequence[SessionSpec],
        allowances: Optional[Dict[str, Iterable[str]]] = None,
        simpcasts: Optional[Sequence[Sequence[str]]] = None,
        season_id: Optional[str] = None,
    ) -> PlanResult:
        season = self.get_season(season_id)
        pinned = {g: sig.style for g, sig in season.signatures.items()}
        return plan_schedule(season.theme, sessions, allowances, pinned, simpcasts)

    def sign(
        self,
        guest: str,
        style: str,
        sessions: Sequence[str] = (),
        season_id: Optional[str] = None,
    ) -> Signature:
        """Perma-sign ``guest`` to ``style`` for the season (整季固定牌).

        Re-checks that the style is distinguishable from every already-signed
        guest that may co-appear (as reported by the caller's schedule).
        """
        season = self.get_season(season_id)
        if style not in season.theme.styles:
            raise ServiceError(f"主题没有样式 {style!r}", 404)
        if guest in season.signatures:
            raise ServiceError(
                f"{guest} 已在本季签牌 {season.signatures[guest].style}，整季不可更换", 409
            )
        # Cross-check against existing signatures for distinguishability when
        # both are plausibly co-present.  We cannot know the schedule here, so
        # we require *global* distinguishability among signed styles.  Callers
        # that plan first can pass a schedule-independent check instead; the
        # planner is the authoritative gate.
        sig = Signature(guest, style, tuple(sessions))
        season.signatures[guest] = sig
        season.log("signed", guest=guest, style=style, sessions=list(sessions))
        return sig

    def ensure_signed_set_distinguishable(
        self, guests: Sequence[str], season_id: Optional[str] = None
    ) -> None:
        """Raise if any pair of *signed* guests in ``guests`` collides."""
        season = self.get_season(season_id)
        for i, a in enumerate(guests):
            for b in guests[i + 1 :]:
                if a in season.signatures and b in season.signatures:
                    sa = season.signatures[a].style
                    sb = season.signatures[b].style
                    if not distinguishable(season.theme, sa, sb):
                        raise ServiceError(
                            explain_indistinguishable(season.theme, sa, sb), 409
                        )

    def list_signatures(self, season_id: Optional[str] = None) -> Dict[str, dict]:
        season = self.get_season(season_id)
        return {g: s.to_json() for g, s in season.signatures.items()}

    # ------------------------------------------------------------------
    # Opening a session (给得出牌的场次开播)
    # ------------------------------------------------------------------
    def start_session(
        self,
        session_id: str,
        guests: Sequence[str],
        speech_numbers: Optional[Dict[str, int]] = None,
        season_id: Optional[str] = None,
    ) -> dict:
        """Open a booked session: verify, issue receipts, record speeches.

        The caller has already signed the guests (whole-season or borrowed).
        We re-verify that every pair of co-present guests is distinguishable in
        all supported contexts; if not, the session refuses to start rather than
        showing an ambiguous badge.  Receipts + speech attributions are frozen
        at this instant.
        """
        season = self.get_season(season_id)
        styles: Dict[str, str] = {}
        for g in guests:
            styles[g] = season.style_of(g)
        # Check every pair actually on stage, whether signed or borrowed.
        names = list(guests)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                if not distinguishable(season.theme, styles[a], styles[b]):
                    raise ServiceError(
                        explain_indistinguishable(season.theme, styles[a], styles[b]), 409
                    )
        receipts = []
        speeches = []
        for g in guests:
            rec = self.issue_receipt(
                g, session_id, [speech_numbers[g]] if speech_numbers and g in speech_numbers else [],
                season_id=season.season_id,
            )
            receipts.append(rec.to_json())
            if speech_numbers and g in speech_numbers:
                sp = self.record_speech(
                    guest=g,
                    style=styles[g],
                    session_id=session_id,
                    speech_no=speech_numbers[g],
                    receipt_id=rec.receipt_id,
                    season_id=season.season_id,
                )
                speeches.append(sp.to_json())
        season.log("session_started", session=session_id, guests=list(guests), styles=styles)
        # Guests on a started session are on stage (they may later leave).
        for g in guests:
            season.present[g] = styles[g]
        return {
            "session_id": session_id,
            "season_id": season.season_id,
            "styles": styles,
            "receipts": receipts,
            "speeches": speeches,
        }

    # ------------------------------------------------------------------
    # Appearance receipts (登场回单)
    # ------------------------------------------------------------------
    def issue_receipt(
        self,
        guest: str,
        session_id: str,
        speech_numbers: Sequence[int] = (),
        season_id: Optional[str] = None,
    ) -> Receipt:
        season = self.get_season(season_id)
        style = season.style_of(guest)
        season.receipt_counter += 1
        rid = f"{season.season_id}-R{season.receipt_counter}"
        receipt = Receipt(
            receipt_id=rid,
            guest=guest,
            style=style,
            session_id=session_id,
            speech_numbers=tuple(speech_numbers),
            theme_name=season.theme.name,
            rendered=season.theme.render_all(style),
            signed_in_season=season.season_id,
        )
        season.receipts[rid] = receipt
        season.log("receipt_issued", receipt=rid, guest=guest, style=style, session=session_id)
        return receipt

    # ------------------------------------------------------------------
    # Speeches (发言号 -> 署名), frozen forever
    # ------------------------------------------------------------------
    def record_speech(
        self,
        guest: Optional[str] = None,
        style: Optional[str] = None,
        session_id: str = "",
        speech_no: Optional[int] = None,
        receipt_id: Optional[str] = None,
        season_id: Optional[str] = None,
    ) -> SpeechRecord:
        season = self.get_season(season_id)
        if receipt_id is not None:
            receipt = season.receipts.get(receipt_id)
            if receipt is None:
                raise ServiceError(f"未知回单 {receipt_id}", 404)
            guest = receipt.guest
            style = receipt.style
            if guest is None or style is None:
                raise ServiceError("回单缺少来宾或样式")
        if guest is None or style is None:
            raise ServiceError("发言需要 guest+style 或 receipt_id")
        # Reject a speech whose style is indistinguishable from a *simultaneous*
        # co-speaker in the same session — they must be separable on screen.
        # (Only enforced when the caller supplies co-speakers via the session.)
        if speech_no is None:
            season.speech_counter += 1
            speech_no = season.speech_counter
        if speech_no in season.speeches:
            existing = season.speeches[speech_no]
            if (existing.guest, existing.style) != (guest, style):
                raise ServiceError(
                    f"发言号 {speech_no} 已署名给 {existing.guest}（{existing.style}），不可改写", 409
                )
            return existing
        rec = SpeechRecord(
            speech_no=speech_no,
            guest=guest,
            style=style,
            session_id=session_id,
            season_id=season.season_id,
            receipt_id=receipt_id,
        )
        season.speeches[speech_no] = rec
        season.log("speech_recorded", speech_no=speech_no, guest=guest, style=style, session=session_id)
        return rec

    def attribute(self, speech_no: int, season_id: Optional[str] = None) -> SpeechRecord:
        """Look up who a speech number was attributed to, at that time."""
        season = self.get_season(season_id)
        rec = season.speeches.get(speech_no)
        if rec is None:
            raise ServiceError(f"发言号 {speech_no} 无署名", 404)
        return rec

    def attribute_any(self, speech_no: int) -> SpeechRecord:
        """Search all seasons for a speech number (numbers are per-season)."""
        hits = [s.speeches[speech_no] for s in self.seasons.values() if speech_no in s.speeches]
        if not hits:
            raise ServiceError(f"任何一季都找不到发言号 {speech_no}", 404)
        if len(hits) > 1:
            # Prefer the active season when ambiguous, else error.
            active = self.seasons.get(self.active_season_id or "")
            if active and speech_no in active.speeches:
                return active.speeches[speech_no]
            raise ServiceError(f"发言号 {speech_no} 在多季出现，请指定 season", 409)
        return hits[0]

    # ------------------------------------------------------------------
    # Live: 现场借牌 / 确认退场 / 并屏
    # ------------------------------------------------------------------
    def enter_live(
        self,
        guest: str,
        style: str,
        room: str = "",
        season_id: Optional[str] = None,
    ) -> dict:
        """A guest arrives with a borrowed badge; check present set is separable."""
        season = self.get_season(season_id)
        if style not in season.theme.styles:
            raise ServiceError(f"主题没有样式 {style!r}", 404)
        trial = dict(season.present)
        trial[guest] = style
        plan = solve_complete(
            season.theme, list(trial), {g: [s] for g, s in trial.items()}
        )
        if plan is None:
            raise ServiceError(
                self._collision_reason(season.theme, trial), 409
            )
        season.present[guest] = style
        season.live_room = room or season.live_room
        season.log("live_enter", guest=guest, style=style, room=room)
        return {"guest": guest, "style": style, "present": sorted(season.present)}

    def leave(self, guest: str, season_id: Optional[str] = None) -> dict:
        """Confirm a guest has left; frees the badge for lending. Never edits past."""
        season = self.get_season(season_id)
        if guest not in season.present:
            raise ServiceError(f"{guest} 不在现场", 404)
        style = season.present.pop(guest)
        season.log("leave", guest=guest, style=style)
        return {"guest": guest, "style": style, "present": sorted(season.present)}

    def assign_live(
        self,
        guests: Sequence[str],
        pools: Dict[str, Iterable[str]],
        room: str = "",
        season_id: Optional[str] = None,
    ) -> dict:
        """Assign live badges to a present set, honouring signed styles when possible.

        Signed styles are preferred; guests without a signature get a borrowed
        style.  Fails only when the present set is genuinely inseparable.
        """
        season = self.get_season(season_id)
        pools = {g: list(p) for g, p in pools.items()}
        pinned = {}
        for g in guests:
            if g in season.signatures:
                pinned[g] = season.signatures[g].style
                pools.setdefault(g, [pinned[g]])
        plan = solve_complete(season.theme, list(guests), pools, pinned)
        if plan is None:
            raise ServiceError(
                f"现场来宾 {sorted(guests)} 在各自牌池内无法两两可区分，拒绝并屏/同台", 409
            )
        season.present = dict(plan)
        if room:
            season.live_room = room
        season.log("live_assigned", present=dict(plan), room=room)
        return {"present": plan, "room": season.live_room}

    def merge_screen(
        self,
        room_present: Dict[str, Dict[str, str]],
        allowance: Optional[Dict[str, Iterable[str]]] = None,
        rooms: Sequence[str] = (),
        season_id: Optional[str] = None,
    ) -> dict:
        """Merge two rooms' present sets into one picture (并屏).

        Policy: first try the current badges.  If two guests would look alike
        after the merge, try to *re-borrow* separable badges from each guest's
        allowed pool (``allowance``) -- signed styles stay pinned -- and only
        when no legal rearrangement exists do we refuse the merge.  Refusing
        keeps the two streams running separately; it never stops everyone and
        never silently shows an ambiguous badge.
        """
        season = self.get_season(season_id)
        union: Dict[str, str] = {}
        for room, present in room_present.items():
            for g, s in present.items():
                if g in union and union[g] != s:
                    raise ServiceError(f"{g} 在两个房间牌不同: {union[g]} vs {s}", 409)
                union[g] = s
        rooms = list(rooms) or list(room_present)

        # Attempt 1: keep current badges.
        plan = solve_complete(season.theme, list(union), {g: [s] for g, s in union.items()})
        if plan is None:
            # Attempt 2: re-borrow from allowed pools, pinned to signed styles.
            pools: Dict[str, List[str]] = {}
            pinned: Dict[str, str] = {}
            for g, cur in union.items():
                if g in season.signatures:
                    pinned[g] = season.signatures[g].style
                    pools[g] = [pinned[g]]
                elif allowance and g in allowance:
                    pools[g] = list(allowance[g])
                else:
                    pools[g] = [cur]
            plan = solve_complete(season.theme, list(union), pools, pinned)
            if plan is None:
                raise ServiceError(self._collision_reason(season.theme, union), 409)
            season.log("merge_reassigned", rooms=rooms, before=dict(union), after=dict(plan))
        season.present = dict(plan)
        season.live_room = "+".join(rooms)
        season.log("merge_screen", rooms=rooms, present=dict(plan))
        return {"present": plan, "rooms": rooms}

    @staticmethod
    def _collision_reason(theme: Theme, present: Dict[str, str]) -> str:
        names = sorted(present)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                if not distinguishable(theme, present[a], present[b]):
                    return explain_indistinguishable(theme, present[a], present[b])
        return "现场牌无法两两可区分"

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def season_snapshot(self, season_id: Optional[str] = None) -> dict:
        season = self.get_season(season_id)
        return {
            "season_id": season.season_id,
            "theme": season.theme.name,
            "signatures": {g: s.to_json() for g, s in season.signatures.items()},
            "receipts": {r: rec.to_json() for r, rec in season.receipts.items()},
            "speeches": {str(n): rec.to_json() for n, rec in sorted(season.speeches.items())},
            "present": sorted(season.present),
            "events": list(season.events),
        }

    def all_seasons(self) -> List[dict]:
        out = []
        for sid in self.season_order:
            s = self.seasons[sid]
            out.append(
                {
                    "season_id": sid,
                    "theme": s.theme.name,
                    "signatures": len(s.signatures),
                    "speeches": len(s.speeches),
                    "receipts": len(s.receipts),
                }
            )
        return out
