"""The decision engine: given a snapshot of the world, decide whether a
chosen set of pieces can actually be opened, and if so, *how*.

Two independent scarcities must be satisfied at once:

* **Translator slots** -- each piece needs ``duration`` consecutive, currently
  unbooked shifts on one eligible translator.  Slots are globally exclusive.
* **Deductible points** -- each piece needs ``points_required`` points that can
  actually be charged.  Only active pledges whose accepted-piece list covers
  the piece, whose conditional requirements are all satisfied, and that stay
  inside the supporter's *per-issue* ceiling can be charged.

Wish amounts (``wish_points``) are display-only and never fund anything.
"""

from __future__ import annotations

from collections import deque
from itertools import combinations

from .model import (
    FundingLine,
    Plan,
    PledgeView,
    SlotAssignment,
    World,
)

INF = 10**9


# --------------------------------------------------------------------------
# Slot matching: pack each piece into a contiguous run of one translator's
# free shifts.
# --------------------------------------------------------------------------
def _contiguous_starts(ords: list[int], duration: int) -> list[int]:
    if duration > len(ords):
        return []
    starts = []
    for i in range(len(ords) - duration + 1):
        window = ords[i : i + duration]
        if window[-1] - window[0] == duration - 1:
            starts.append(window[0])
    return starts


def assign_slots(world: World, target: list[str]) -> dict[str, SlotAssignment] | None:
    """Exact backtracking placement of pieces onto translator shift runs."""
    # Most constrained first: longer pieces and fewer eligible translators.
    pieces = sorted(
        target,
        key=lambda p: (
            -world.piece(p).duration,
            len(world.piece(p).eligible),
            p,
        ),
    )

    # Mutable copy of availability: translator -> set of free ordinals.
    free = {t: set(o for o, _ in slots) for t, slots in world.free_slots.items()}
    # ord -> label for receipt output
    label = {
        (t, o): lbl for t, slots in world.free_slots.items() for o, lbl in slots
    }

    result: dict[str, SlotAssignment] = {}

    def place(idx: int) -> bool:
        if idx == len(pieces):
            return True
        pid = pieces[idx]
        pv = world.piece(pid)
        d = pv.duration
        for t in pv.eligible:
            if t not in free:
                continue
            ords = sorted(free[t])
            for start in _contiguous_starts(ords, d):
                run = list(range(start, start + d))
                for o in run:
                    free[t].discard(o)
                result[pid] = SlotAssignment(
                    piece_id=pid,
                    translator_id=t,
                    slots=[label[(t, o)] for o in run],
                )
                if place(idx + 1):
                    return True
                del result[pid]
                for o in run:
                    free[t].add(o)
        return False

    if place(0):
        return result
    return None


# --------------------------------------------------------------------------
# Deductible-points: max-flow from supporters through pledges into pieces.
# --------------------------------------------------------------------------
class _Dinic:
    def __init__(self, n: int):
        self.n = n
        self.g: list[list[list[int]]] = [[] for _ in range(n)]  # [to, cap, rev]

    def add(self, u: int, v: int, cap: int) -> int:
        self.g[u].append([v, cap, len(self.g[v])])
        self.g[v].append([u, 0, len(self.g[u]) - 1])
        return len(self.g[u]) - 1

    def max_flow(self, s: int, t: int) -> int:
        total = 0
        while True:
            level = [-1] * self.n
            level[s] = 0
            q = deque([s])
            while q:
                u = q.popleft()
                for v, cap, _ in self.g[u]:
                    if cap > 0 and level[v] < 0:
                        level[v] = level[u] + 1
                        q.append(v)
            if level[t] < 0:
                return total
            it = [0] * self.n

            def dfs(u: int, pushed: int) -> int:
                if u == t:
                    return pushed
                while it[u] < len(self.g[u]):
                    e = self.g[u][it[u]]
                    v, cap, rev = e
                    if cap > 0 and level[v] == level[u] + 1:
                        got = dfs(v, min(pushed, cap))
                        if got:
                            e[1] -= got
                            self.g[v][rev][1] += got
                            return got
                    it[u] += 1
                return 0

            while True:
                pushed = dfs(s, INF)
                if not pushed:
                    break
                total += pushed


def solve_funding(
    world: World,
    target: list[str],
    satisfied: frozenset[str],
) -> tuple[dict[str, list[FundingLine]], dict[str, int], dict[str, int]] | None:
    """Return (funding_by_piece, supporter_deducted, pledge_deducted) or None.

    ``target`` pieces each need their ``points_required`` fulfilled.
    ``satisfied`` is the set of pieces regarded as opened for condition checks.
    """
    supporter_cap_remaining: dict[str, int] = dict(world.supporter_remaining)

    supporters = sorted({p.supporter for p in world.pledges})
    pledge_ids = sorted({p.pledge_id for p in world.pledges})
    piece_ids = sorted(target)

    n_src = 0
    n_sink = 1 + len(supporters) + len(pledge_ids) + len(piece_ids)
    sup_node = {s: 1 + i for i, s in enumerate(supporters)}
    pl_node = {pid: 1 + len(supporters) + i for i, pid in enumerate(pledge_ids)}
    pc_node = {
        pid: 1 + len(supporters) + len(pledge_ids) + i
        for i, pid in enumerate(piece_ids)
    }
    total_nodes = n_sink + 1
    din = _Dinic(total_nodes)

    supp_edge: dict[str, int] = {}
    pl_edge: dict[str, int] = {}
    sup_pl_edge: dict[tuple[str, str], int] = {}
    pl_pc_edge: dict[tuple[str, str], int] = {}

    for s in supporters:
        cap = max(0, supporter_cap_remaining.get(s, 0))
        supp_edge[s] = din.add(n_src, sup_node[s], cap)

    pledge_by_id = {p.pledge_id: p for p in world.pledges}
    for pid in pledge_ids:
        pv = pledge_by_id[pid]
        if not pv.active:
            continue
        cap = max(0, pv.remaining)
        sup_pl_edge[(pv.supporter, pid)] = din.add(
            sup_node[pv.supporter], pl_node[pid], cap
        )
        pl_edge[pid] = cap
        if not set(pv.requires).issubset(satisfied):
            continue
        for pce in piece_ids:
            # An empty accept list means "any piece in this issue".
            if not pv.accepts or pce in pv.accepts:
                pl_pc_edge[(pid, pce)] = din.add(
                    pl_node[pid], pc_node[pce], INF
                )

    for pce in piece_ids:
        din.add(pc_node[pce], n_sink, world.piece(pce).points_required)

    required = sum(world.piece(p).points_required for p in piece_ids)
    if din.max_flow(n_src, n_sink) != required:
        return None

    # Read flows back off the residual graph.
    funding: dict[str, list[FundingLine]] = {p: [] for p in piece_ids}
    supporter_deducted: dict[str, int] = {}
    pledge_deducted: dict[str, int] = {}
    for (pid, pce), eidx in pl_pc_edge.items():
        edge = din.g[pl_node[pid]][eidx]
        used = INF - edge[1]
        if used <= 0:
            continue
        pv = pledge_by_id[pid]
        funding[pce].append(
            FundingLine(supporter=pv.supporter, pledge_id=pid, points=used)
        )
        pledge_deducted[pid] = pledge_deducted.get(pid, 0) + used
        supporter_deducted[pv.supporter] = (
            supporter_deducted.get(pv.supporter, 0) + used
        )
    return funding, supporter_deducted, pledge_deducted


# --------------------------------------------------------------------------
# Whole-plan feasibility.
# --------------------------------------------------------------------------
def solve_plan(
    world: World,
    target: list[str],
    satisfied: frozenset[str],
) -> Plan | None:
    """Combine slot matching + funding; return a realised Plan or None."""
    if not target:
        return None
    slots = assign_slots(world, target)
    if slots is None:
        return None
    fund = solve_funding(world, target, satisfied)
    if fund is None:
        return None
    funding, supporter_deducted, pledge_deducted = fund
    return Plan(
        pieces=sorted(target),
        assignments=slots,
        funding=funding,
        supporter_deducted=supporter_deducted,
        pledge_deducted=pledge_deducted,
    )


def feasible_candidates(world: World, max_pieces: int = 16) -> list[Plan]:
    """Enumerate inclusion-maximal feasible batches for a joint cutoff.

    For each still-unopened piece-set, the whole batch is tested at once so
    that mutually conditional pledges ("only if the island piece also opens")
    are satisfied *inside* the same founding action.
    """
    openable = sorted(p for p in world.pieces if p not in world.opened)
    if not openable:
        return []

    if len(openable) > max_pieces:
        # Fall back to greedy: try all pieces, drop those that break feasibility.
        batch = list(openable)
        while batch and solve_plan(world, batch, frozenset(batch)) is None:
            # remove the piece with the fewest accepting pledges, deterministically
            def scarcity(pce: str) -> tuple[int, str]:
                accepts = sum(
                    1
                    for pl in world.pledges
                    if pl.active and pl.remaining > 0 and pce in pl.accepts
                )
                return (accepts, pce)

            batch.remove(min(batch, key=scarcity))
        plan = solve_plan(world, batch, frozenset(batch)) if batch else None
        return [plan] if plan else []

    feasible: list[Plan] = []
    for size in range(1, len(openable) + 1):
        for combo in combinations(openable, size):
            plan = solve_plan(world, list(combo), frozenset(combo))
            if plan is not None:
                feasible.append(plan)

    # Keep inclusion-maximal plans only.
    maximal: list[Plan] = []
    for plan in feasible:
        pset = set(plan.pieces)
        if any(
            pset < set(other.pieces) for other in feasible
        ):
            continue
        maximal.append(plan)
    # Deterministic, larger first.
    maximal.sort(key=lambda pl: (-len(pl.pieces), pl.candidate_id()))
    return maximal


def rolling_feasible(world: World, piece_id: str) -> Plan | None:
    """Can this single piece be opened *now* in rolling mode?

    Only unconditional money or money whose prerequisites are *already* opened
    may be used; a not-yet-opened piece can never serve as surety.
    """
    plan = solve_plan(world, [piece_id], frozenset(world.opened))
    return plan
