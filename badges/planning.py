"""Schedule planning: choose badge designs for guests.

A *schedule* is a list of :class:`SessionSpec`.  Each session lists the guests
who appear in it (they may all share the screen).  Sessions listed together in
``simulcasts`` share one picture (two rooms joined into one layout), so all of
their guests are mutually co-present.

A :class:`Plan` assigns every distinct guest one *style* (badge design) such
that:

* the style is allowed for that guest (its badge pool);
* styles already signed for the season are honoured (pinned);
* for every pair of guests that can be co-present, the two styles are
  distinguishable in **every** supported viewing context.

The search is a complete backtracking list-colouring so that whenever a feasible
arrangement exists it is found; it never falls back to "stop everybody".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .model import Theme, distinguishable, explain_indistinguishable


@dataclass
class SessionSpec:
    id: str
    label: str = ""
    guests: Tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"id": self.id, "label": self.label, "guests": list(self.guests)}

    @staticmethod
    def from_json(d: dict) -> "SessionSpec":
        return SessionSpec(d["id"], d.get("label", ""), tuple(d.get("guests", ())))


@dataclass
class PlanResult:
    ok: bool
    assignment: Dict[str, str] = field(default_factory=dict)
    reason: str = ""
    groups: List[List[str]] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "assignment": dict(self.assignment),
            "reason": self.reason,
            "groups": [list(g) for g in self.groups],
        }


# ---------------------------------------------------------------------------
# Co-appearance graph
# ---------------------------------------------------------------------------


def co_appearance_groups(
    sessions: Sequence[SessionSpec], simulcasts: Optional[Sequence[Sequence[str]]] = None
) -> List[List[str]]:
    """Union-find guests into sets that can share a picture.

    Every session is a clique.  Each entry of ``simulcasts`` joins the sessions
    it names into one bigger clique.
    """
    by_id = {s.id: s for s in sessions}
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Start each guest in its own set.
    for s in sessions:
        for g in s.guests:
            find(g)

    # Join all guests inside a session.
    for s in sessions:
        gs = list(s.guests)
        for g in gs[1:]:
            union(gs[0], g)

    # Join sessions participating in the same simulcast.
    for group in simulcasts or []:
        present = [sid for sid in group if sid in by_id]
        if not present:
            continue
        pivot_guests = by_id[present[0]].guests
        for sid in present[1:]:
            for g in by_id[sid].guests:
                if pivot_guests:
                    union(pivot_guests[0], g)
                elif by_id[sid].guests:
                    union(g, by_id[sid].guests[0])

    sets: Dict[str, List[str]] = {}
    for g in list(parent):
        sets.setdefault(find(g), []).append(g)
    return [sorted(v) for v in sets.values()]


def _adjacency(groups: Sequence[Sequence[str]]) -> Dict[str, Set[str]]:
    adj: Dict[str, Set[str]] = {}
    for group in groups:
        for i, a in enumerate(group):
            adj.setdefault(a, set())
            for b in group:
                if a != b:
                    adj[a].add(b)
    return adj


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------


def plan_schedule(
    theme: Theme,
    sessions: Sequence[SessionSpec],
    allowances: Optional[Dict[str, Iterable[str]]] = None,
    pinned: Optional[Dict[str, str]] = None,
    simulcasts: Optional[Sequence[Sequence[str]]] = None,
) -> PlanResult:
    """Find a valid style assignment for the whole schedule.

    ``allowances`` restricts each guest to a pool of styles (default: every
    style in the theme).  ``pinned`` holds already-signed styles that must be
    honoured.  Returns ``ok=False`` with an explanation when no arrangement can
    satisfy all co-present pairs.
    """
    allowances = allowances or {}
    pinned = pinned or {}
    groups = co_appearance_groups(sessions, simulcasts)
    adj = _adjacency(groups)
    guests = sorted(adj)
    all_styles = list(theme.styles)

    # Validate pinned styles exist and are allowed.
    for g, s in pinned.items():
        if s not in theme.styles:
            return PlanResult(False, reason=f"已签牌 {g} -> {s}，但主题里没有该样式", groups=groups)
        pool = set(allowances.get(g, all_styles))
        if s not in pool:
            return PlanResult(
                False,
                reason=f"{g} 已签 {s}，但它不在允许牌池 {sorted(pool)} 内",
                groups=groups,
            )

    def pool_for(g: str) -> List[str]:
        if g in pinned:
            return [pinned[g]]
        pool = list(allowances.get(g, all_styles))
        # Deterministic order: keep theme order but only allowed.
        order = {name: i for i, name in enumerate(all_styles)}
        pool = [s for s in all_styles if s in set(pool)]
        return pool

    # Most-constrained-first ordering.
    order = sorted(guests, key=lambda g: (len(pool_for(g)), g))

    assignment: Dict[str, str] = {}
    conflict_notes: List[str] = []

    def compatible(g: str, style: str) -> Optional[str]:
        """Return an explanation if ``style`` clashes with an assigned neighbour."""
        for nb in adj.get(g, ()):  # neighbours sorted for determinism
            if nb in assignment:
                other = assignment[nb]
                if not distinguishable(theme, style, other):
                    return explain_indistinguishable(theme, style, other)
        return None

    # Seed pinned assignment first (they are in the variable order anyway).
    for g in guests:
        if g in pinned:
            assignment[g] = pinned[g]

    # Two pinned guests may already collide with each other.
    seeded = sorted(assignment)
    for i, a in enumerate(seeded):
        for b in seeded[i + 1 :]:
            if not distinguishable(theme, assignment[a], assignment[b]):
                return PlanResult(
                    False,
                    reason=explain_indistinguishable(theme, assignment[a], assignment[b]),
                    groups=groups,
                )

    def backtrack(i: int) -> bool:
        if i == len(order):
            return True
        g = order[i]
        if g in assignment:
            return backtrack(i + 1)
        for style in pool_for(g):
            why = compatible(g, style)
            if why is not None:
                if len(conflict_notes) < 8:
                    conflict_notes.append(f"{g}={style}: {why}")
                continue
            assignment[g] = style
            if backtrack(i + 1):
                return True
            del assignment[g]
        return False

    if backtrack(0):
        return PlanResult(True, dict(assignment), "", groups)

    # Build a helpful failure reason: focus on the tightest guest.
    details = []
    for g in guests:
        pool = pool_for(g)
        nbrs = sorted(n for n in adj.get(g, ()) if n in pinned)
        if nbrs:
            details.append(f"{g} 的牌池 {pool}，与已签邻居 {nbrs} 需两两可区分")
    reason = "无可行排布："
    if details:
        reason += "; ".join(details)
    else:
        reason += "各牌池无法让所有可能同屏的来宾两两可区分"
    if conflict_notes:
        reason += "。示例冲突: " + conflict_notes[0]
    return PlanResult(False, reason=reason, groups=groups)


def solve_complete(
    theme: Theme,
    guests: Sequence[str],
    pools: Dict[str, Iterable[str]],
    pinned: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, str]]:
    """Colour a *complete* graph: every guest must differ from every other.

    Used for the present set of a single live picture (session or merged
    screen).  Returns an assignment or ``None``.
    """
    pinned = pinned or {}
    all_styles = list(theme.styles)
    order = sorted(guests, key=lambda g: (1 if g in pinned else 0, len(list(pools.get(g, all_styles)))))

    assignment: Dict[str, str] = {}
    for g in guests:
        if g in pinned:
            assignment[g] = pinned[g]

    # Validate the pinned seed: two pinned guests may collide with each other.
    seeded = sorted(assignment)
    for i, a in enumerate(seeded):
        for b in seeded[i + 1 :]:
            if not distinguishable(theme, assignment[a], assignment[b]):
                return None

    def pool_for(g: str) -> List[str]:
        if g in pinned:
            return [pinned[g]]
        allowed = set(pools.get(g, all_styles))
        return [s for s in all_styles if s in allowed]

    def ok(g: str, style: str) -> bool:
        for nb, other in assignment.items():
            if nb == g:
                continue
            if not distinguishable(theme, style, other):
                return False
        return True

    def bt(i: int) -> bool:
        if i == len(order):
            return True
        g = order[i]
        if g in assignment:
            return bt(i + 1)
        for style in pool_for(g):
            if ok(g, style):
                assignment[g] = style
                if bt(i + 1):
                    return True
                del assignment[g]
        return False

    if bt(0):
        return dict(assignment)
    return None


def plan_sessions_for_schedule(
    theme: Theme, sessions, allowances=None, pinned=None, simulcasts=None
) -> Dict[str, Dict[str, str]]:
    """Convenience: map session id -> {guest: style} for a feasible plan."""
    res = plan_schedule(theme, sessions, allowances, pinned, simulcasts)
    if not res.ok:
        raise ValueError(res.reason)
    return {s.id: {g: res.assignment[g] for g in s.guests} for s in sessions}
