"""Arena controller: action validation + persistence + replay.

This sits between the HTTP layer and the pure model/scoring.  It owns a set of
sessions, a persistent store directory (so a process can be restarted and
reload the exact log), and the server-side hint resolution (the server -- not
the team -- decides which letter a hint reveals, and which crossing entries it
teaches).
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import Any, Dict, List, Optional

from . import events as E
from .model import Session, XwordError, digest, normalise_scenario
from .scoring import Scorer, score_diff, score_events, score_revision


def _locked(fn):
    """Serialise a mutating Arena method under the arena's re-entrant lock.

    Fair first-arrival ordering, at-most-once action ids and atomic
    hint-request+hint pairs all rely on a stable critical section even though
    the HTTP server is multi-threaded.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class Arena:
    def __init__(self, store_dir: Optional[str] = None) -> None:
        self.store_dir = store_dir
        self.sessions: Dict[str, Session] = {}
        # serialise mutations: the HTTP layer is threaded, and the fair
        # first-arrival ordering depends on a stable append order
        self._lock = threading.RLock()
        if store_dir:
            os.makedirs(store_dir, exist_ok=True)
            self._load_all()

    # -- persistence -------------------------------------------------------
    def _path(self, sid: str) -> str:
        return os.path.join(self.store_dir, f"{sid}.json")

    def _save(self, sid: str) -> None:
        if not self.store_dir:
            return
        import json
        with open(self._path(sid), "w", encoding="utf-8") as f:
            json.dump(self.sessions[sid].to_dict(), f, ensure_ascii=False,
                      sort_keys=True, indent=2)

    def _load_all(self) -> None:
        import json
        for name in sorted(os.listdir(self.store_dir)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(self.store_dir, name), encoding="utf-8") as f:
                data = json.load(f)
            sid = name[:-5]
            self.sessions[sid] = Session.from_dict(data)

    # -- session lifecycle -------------------------------------------------
    def create_scenario(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        s = normalise_scenario(spec)
        return s

    @_locked
    def create_session(self, scenario: Dict[str, Any],
                       team_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        sess = Session(scenario)
        sid = uuid.uuid4().hex[:12]
        self.sessions[sid] = sess
        teams = list(team_ids or [])
        if teams:
            self._append(sid, E.ev_start(teams, sess.scenario["mode"]))
        self._save(sid)
        return {"session_id": sid, "scenario": sess.scenario,
                "mode": sess.scenario["mode"], "revision": "r0"}

    def get(self, sid: str) -> Session:
        if sid not in self.sessions:
            raise XwordError(f"unknown session {sid}")
        return self.sessions[sid]

    # -- event append helper ----------------------------------------------
    def _append(self, sid: str, ev: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            sess = self.get(sid)
            before = Scorer(sess, sess.log).teams
            seq = sess.append(ev)
            after = Scorer(sess, sess.log).teams
            self._save(sid)
            delta = {}
            for tm in after:
                before_score = before[tm].score if tm in before else 0
                d = after[tm].score - before_score
                if d:
                    delta[tm] = d
            return {"seq": seq,
                    "outcome": {"type": ev.get("type"), "seq": seq,
                                "score_delta": delta}}

    # -- team actions ------------------------------------------------------
    @_locked
    def add_team(self, sid: str, team: str) -> Dict[str, Any]:
        sess = self.get(sid)
        # start events can be appended only before any play; emulate by a
        # lightweight team-registration event folded as start with one id
        return self._append(sid, E.ev_start([team], sess.scenario["mode"]))

    @_locked
    def draft(self, sid: str, team: str, entry_id: str,
              letters: Dict[str, str]) -> Dict[str, Any]:
        sess = self.get(sid)
        _check_entry(sess, entry_id)
        _check_team(sess, team)
        clean = {str(k): str(v).upper() for k, v in letters.items()}
        return self._append(sid, E.ev_draft(team, entry_id, clean))

    @_locked
    def request_hint(self, sid: str, team: str, entry_id: str,
                     cell: List[int], action_id: Optional[str] = None) -> Dict[str, Any]:
        sess = self.get(sid)
        entry = _check_entry(sess, entry_id)
        _check_team(sess, team)
        cell_t = (int(cell[0]), int(cell[1]))
        if cell_t not in entry.cells:
            raise XwordError("cell is not part of that entry")
        aid = action_id or uuid.uuid4().hex
        # server decides the letter and which entries the cell teaches
        letter = entry.letter_at(cell_t)
        teaches = [eid for eid, e in sess.entries().items() if cell_t in e.cells]
        # log the request (intent) and the served hint
        self._append(sid, E.ev_hint_request(team, entry_id, list(cell_t)))
        res = self._append(sid, E.ev_hint(team, entry_id, list(cell_t),
                                          letter, aid))
        res["hint"] = {"entry": entry_id, "cell": list(cell_t),
                       "letter": letter, "teaches": teaches}
        return res

    @_locked
    def handin(self, sid: str, team: str, entry_id: str, word: str,
               action_id: Optional[str] = None) -> Dict[str, Any]:
        sess = self.get(sid)
        _check_entry(sess, entry_id)
        _check_team(sess, team)
        aid = action_id or uuid.uuid4().hex
        return self._append(sid, E.ev_handin(team, entry_id, word, aid))

    @_locked
    def seal(self, sid: str, team: str, entry_id: str, word: str,
             action_id: Optional[str] = None) -> Dict[str, Any]:
        sess = self.get(sid)
        _check_entry(sess, entry_id)
        _check_team(sess, team)
        aid = action_id or uuid.uuid4().hex
        return self._append(sid, E.ev_seal(team, entry_id, word, aid))

    @_locked
    def reveal(self, sid: str, team: Optional[str] = None) -> Dict[str, Any]:
        sess = self.get(sid)
        targets = [team] if team else list(
            {e.get("team") for e in sess.log if e.get("team")})
        results = []
        for tm in targets:
            results.append(self._append(sid, E.ev_reveal(tm)))
        if not team:
            self._append(sid, E.ev_new_revision("revealed"))
        return {"revealed": targets, "results": results}

    # -- judge -------------------------------------------------------------
    @_locked
    def judge_revise(self, sid: str, entry_id: str, accepted: List[str],
                     note: str = "", new_revision: Optional[str] = None
                     ) -> Dict[str, Any]:
        sess = self.get(sid)
        _check_entry(sess, entry_id)
        res = self._append(sid, E.ev_judge_revise(entry_id, accepted, note))
        rev = None
        if new_revision:
            rev = self.make_revision(sid, new_revision)
        return {"outcome": res, "revision": rev}

    @_locked
    def revise_score_table(self, sid: str, cell_points: Dict[str, int],
                           note: str = "",
                           new_revision: Optional[str] = None) -> Dict[str, Any]:
        """Change a cell's reward (e.g. drop 山 to zero) *as a revision*.

        The change is an event, so it is folded like any other and old scores
        recompute against the fact set visible at that revision.  The judge can
        diff the two revisions to see exactly which cells/totals moved.
        """
        sess = self.get(sid)
        known = set(sess.scenario["cell_points"].keys())
        for k in cell_points:
            if str(k) not in known:
                raise XwordError(f"unknown cell {k}; known: {sorted(known)}")
        res = self._append(sid, E.ev_score_table_revise(cell_points, note))
        rev = self.make_revision(sid, new_revision) if new_revision else None
        return {"outcome": res, "revision": rev}

    def make_revision(self, sid: str, label: str) -> str:
        sess = self.get(sid)
        label = sess.make_revision(label)
        self._append(sid, E.ev_new_revision(label))
        self._save(sid)
        return label

    # -- replay / restart --------------------------------------------------
    @_locked
    def replay_handin(self, sid: str, event: Dict[str, Any]) -> Dict[str, Any]:
        """Replay an externally-supplied submission verbatim (with its original
        seq and action_id).  Because action_ids are remembered, this never
        adds points twice.  The original event is preserved in the log."""
        sess = self.get(sid)
        etype = event.get("type")
        if etype not in ("handin", "seal_handin"):
            raise XwordError("replay supports handin / seal_handin only")
        replay = dict(event)
        replay.setdefault("action_id", "replay-" + uuid.uuid4().hex)
        # fold whole log with this appended, then report the *delta* it caused
        before = Scorer(sess, sess.log)
        trial_events = sess.log + [dict(replay)]
        after = Scorer(sess, trial_events)
        delta = {t: after.teams[t].score - before.teams[t].score
                 for t in after.teams}
        sess.append(replay)
        self._save(sid)
        return {"replayed": replay, "delta": delta,
                "added_points": sum(delta.values())}

    @_locked
    def restart(self, sid: str) -> Dict[str, Any]:
        """Simulate a process restart: drop in-memory state, reload from disk,
        and re-fold.  Returns the refolded totals so a client can check them."""
        if not self.store_dir:
            raise XwordError("no store configured; restart is a no-op")
        self.sessions.pop(sid, None)
        self._load_all()
        sess = self.get(sid)
        board = score_events(sess, sess.log)
        return {"restarted": True, "session_id": sid,
                "log_len": len(sess.log), "totals": board["totals"]}

    # -- views -------------------------------------------------------------
    def scoreboard(self, sid: str, revision: Optional[str] = None,
                   viewer: Optional[str] = None) -> Dict[str, Any]:
        sess = self.get(sid)
        return score_revision(sess, revision, viewer=viewer)

    def diff(self, sid: str, old: str, new: str) -> Dict[str, Any]:
        return score_diff(self.get(sid), old, new)

    def log(self, sid: str, revision: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.get(sid).events_upto(revision)

    def session_info(self, sid: str) -> Dict[str, Any]:
        sess = self.get(sid)
        return {"session_id": sid, "scenario": sess.scenario,
                "revisions": dict(sess.cuts),
                "current_revision": sess.current_revision,
                "log_len": len(sess.log)}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _check_entry(sess: Session, entry_id: str):
    ents = sess.entries()
    if entry_id not in ents:
        raise XwordError(f"unknown entry {entry_id}")
    return ents[entry_id]


def _check_team(sess: Session, team: str) -> None:
    known = {e.get("team") for e in sess.log if e.get("team")}
    starts = [e for e in sess.log if e.get("type") == "start"]
    registered = {t for s in starts for t in s.get("team_ids", [])}
    if known or registered:
        if team not in known and team not in registered:
            raise XwordError(f"unknown team {team}")
