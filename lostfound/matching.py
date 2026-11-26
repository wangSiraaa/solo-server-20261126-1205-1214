from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, Iterable

Constraint = dict[str, Any]
State = dict[str, Any]


def normalize_value_constraint(value: Any) -> Constraint:
    """Normalize one owner memory.

    Accepted forms:
      "star"                       -> certain singleton
      ["star", "moon"]             -> uncertain one-of-two
      "unseen", None, "*"          -> not seen / no constraint
      {"certain": "star"}
      {"one_of": ["star", "moon"]}
      {"unseen": true}
    """
    if value is None or value == "unseen" or value == "*":
        return {"kind": "unseen", "allowed": None}

    if isinstance(value, str):
        return {"kind": "certain", "allowed": [value]}

    if isinstance(value, (list, tuple)):
        values = [str(v) for v in value]
        if not values:
            return {"kind": "unseen", "allowed": None}
        kind = "certain" if len(set(values)) == 1 else "uncertain"
        return {"kind": kind, "allowed": values}

    if isinstance(value, dict):
        if value.get("unseen") or value.get("kind") == "unseen" or not value.get("allowed", True):
            return {"kind": "unseen", "allowed": None}
        if "certain" in value:
            return {"kind": "certain", "allowed": [str(value["certain"])]}
        if "one_of" in value:
            values = [str(v) for v in value["one_of"]]
            kind = "certain" if len(set(values)) == 1 else "uncertain"
            return {"kind": kind, "allowed": values}
        if "allowed" in value:
            raw_allowed = value["allowed"]
            if not raw_allowed:
                return {"kind": "unseen", "allowed": None}
            values = [str(v) for v in raw_allowed]
            kind = value.get("kind") or ("certain" if len(values) == 1 else "uncertain")
            return {"kind": kind, "allowed": values}

    raise ValueError(f"unsupported feature constraint: {value!r}")


def normalize_claim_statement(
    appearance: dict[str, Any] | None,
    memory: dict[str, Any] | None,
) -> dict[str, Constraint]:
    constraints: dict[str, Constraint] = {}
    for key, value in (appearance or {}).items():
        # Public appearance is remembered as an exact value unless caller
        # explicitly supplies the same constraint object syntax.
        if isinstance(value, (dict, list)) or value is None:
            constraints[f"appearance.{key}"] = normalize_value_constraint(value)
        else:
            constraints[f"appearance.{key}"] = {
                "kind": "certain",
                "allowed": [str(value)],
            }
    for key, value in (memory or {}).items():
        constraints[str(key)] = normalize_value_constraint(value)
    return constraints


def item_trait(state: State, item_no: str, key: str) -> tuple[Any, bool]:
    """Return (value, usable).  A sealed/missing value is not usable evidence."""
    if key.startswith("appearance."):
        field = key.split(".", 1)[1]
        item = state["items_by_no"][item_no]
        if field not in item.get("appearance", {}):
            return None, False
        return item["appearance"][field], True

    feature = state.get("features", {}).get(item_no, {}).get(key)
    if feature is None:
        # An unregistered dimension cannot be used to exclude a claim.
        return None, False
    if not int(feature.get("revealed", 0)):
        return feature.get("actual_value"), False
    return feature.get("actual_value"), True


def compatible(state: State, claim: dict[str, Any], item_no: str) -> bool:
    for key, constraint in claim.get("constraints", {}).items():
        allowed = constraint.get("allowed")
        if not allowed:
            continue
        actual, usable = item_trait(state, item_no, key)
        if not usable:
            continue
        if str(actual) not in {str(v) for v in allowed}:
            return False
    return True


def candidate_graph(
    state: State,
    claim_nos: Iterable[str] | None = None,
    item_nos: Iterable[str] | None = None,
) -> dict[str, list[str]]:
    claims = state["claims_by_no"]
    items = state["items_by_no"]
    c_nos = list(claim_nos) if claim_nos is not None else list(claims)
    i_nos = list(item_nos) if item_nos is not None else list(items)
    graph: dict[str, list[str]] = {}
    for c_no in c_nos:
        claim = claims[c_no]
        graph[c_no] = [i_no for i_no in i_nos if compatible(state, claim, i_no)]
    return graph


def _hopcroft_karp(adj: list[list[int]], right_size: int) -> list[int]:
    """Return match_l[u] = v or -1."""
    n = len(adj)
    match_l = [-1] * n
    match_r = [-1] * right_size
    dist = [0] * n

    from collections import deque

    def bfs() -> bool:
        q: deque[int] = deque()
        for u in range(n):
            if match_l[u] == -1:
                dist[u] = 0
                q.append(u)
            else:
                dist[u] = -1
        found = False
        while q:
            u = q.popleft()
            for v in adj[u]:
                pu = match_r[v]
                if pu == -1:
                    found = True
                elif dist[pu] == -1:
                    dist[pu] = dist[u] + 1
                    q.append(pu)
        return found

    def dfs(u: int) -> bool:
        for v in adj[u]:
            pu = match_r[v]
            if pu == -1 or (dist[pu] == dist[u] + 1 and dfs(pu)):
                match_l[u] = v
                match_r[v] = u
                return True
        dist[u] = -1
        return False

    while bfs():
        for u in range(n):
            if match_l[u] == -1:
                dfs(u)
    return match_l


def _kosaraju_components(adj: list[list[int]]) -> list[int]:
    n = len(adj)
    reverse: list[list[int]] = [[] for _ in range(n)]
    for u, vs in enumerate(adj):
        for v in vs:
            reverse[v].append(u)

    seen = [False] * n
    order: list[int] = []
    for start in range(n):
        if seen[start]:
            continue
        stack: list[tuple[int, int]] = [(start, 0)]
        seen[start] = True
        while stack:
            node, idx = stack[-1]
            if idx < len(adj[node]):
                nxt = adj[node][idx]
                stack[-1] = (node, idx + 1)
                if not seen[nxt]:
                    seen[nxt] = True
                    stack.append((nxt, 0))
            else:
                order.append(node)
                stack.pop()

    component = [-1] * n
    cid = 0
    for start in reversed(order):
        if component[start] != -1:
            continue
        component[start] = cid
        stack = [start]
        while stack:
            u = stack.pop()
            for v in reverse[u]:
                if component[v] == -1:
                    component[v] = cid
                    stack.append(v)
        cid += 1
    return component


def perfect_matching_edges(
    claim_nos: list[str],
    item_nos: list[str],
    graph: dict[str, list[str]],
) -> set[tuple[str, str]] | None:
    """Edges that occur in at least one perfect one-to-one matching.

    Returns None when no perfect matching exists.
    """
    if len(claim_nos) != len(item_nos):
        return None

    c_index = {c: i for i, c in enumerate(claim_nos)}
    i_index = {it: j for j, it in enumerate(item_nos)}
    adj = [
        [i_index[i] for i in graph.get(c, []) if i in i_index]
        for c in claim_nos
    ]
    match_l = _hopcroft_karp(adj, len(item_nos))
    if any(v == -1 for v in match_l):
        return None

    n = len(claim_nos)
    m = len(item_nos)
    directed: list[list[int]] = [[] for _ in range(n + m)]
    allowed: set[tuple[str, str]] = set()

    for u, vs in enumerate(adj):
        c_no = claim_nos[u]
        matched_v = match_l[u]
        for v in vs:
            i_no = item_nos[v]
            if v == matched_v:
                # Matched edge is always part of this perfect matching.
                allowed.add((c_no, i_no))
                directed[n + v].append(u)
            else:
                directed[u].append(n + v)

    comp = _kosaraju_components(directed)
    for u, vs in enumerate(adj):
        c_no = claim_nos[u]
        for v in vs:
            i_no = item_nos[v]
            if match_l[u] == v or comp[u] == comp[n + v]:
                allowed.add((c_no, i_no))
    return allowed


def evaluate(state: State) -> dict[str, Any]:
    """Compute signability.

    Closed/complete registrations may use one-to-one elimination through
    perfect matchings.  Ordinary open registrations use direct feature
    uniqueness over every registered item; reserved/out items remain in that
    evidence set so elimination after another claim signs cannot create a
    false unique match.
    """
    round_row = state["round"]
    all_claim_nos = [c["claim_no"] for c in state["claims"]]
    all_item_nos = [i["item_no"] for i in state["items"]]
    available_item_nos = {
        no for no, item in state["items_by_no"].items()
        if item.get("status") == "in_stock"
    }

    # Evidence graph uses all registered physical items, including items now
    # reserved/out.  This is deliberate for open-world uniqueness.
    evidence_graph = candidate_graph(state, all_claim_nos, all_item_nos)

    complete = bool(int(round_row.get("complete_pairing", 0))) and not bool(
        int(round_row.get("allow_unfound", 1))
    )

    results: list[dict[str, Any]] = []
    summary = defaultdict(int)

    if complete:
        # For the binding closed allocation, existing vouchers are fixed and
        # removed; the remaining sets must still form a perfect matching.
        active_claims = [
            c_no for c_no in all_claim_nos
            if state["claims_by_no"][c_no].get("status") == "filed"
        ]
        active_items = [
            i_no for i_no in all_item_nos
            if state["items_by_no"][i_no].get("status") == "in_stock"
        ]
        active_graph = candidate_graph(state, active_claims, active_items)
        allowed = perfect_matching_edges(active_claims, active_items, active_graph)
        consistent = allowed is not None

        possible_by_claim: dict[str, list[str]] = defaultdict(list)
        if allowed is not None:
            for c_no, i_no in allowed:
                possible_by_claim[c_no].append(i_no)

        for c_no in all_claim_nos:
            claim = state["claims_by_no"][c_no]
            candidates = evidence_graph.get(c_no, [])
            if claim.get("status") != "filed":
                result = _nonfiled_result(claim, candidates)
            elif not consistent:
                result = {
                    "claim_no": c_no,
                    "claimant": claim.get("claimant", ""),
                    "status": claim.get("status"),
                    "candidate_item_nos": candidates,
                    "possible_item_nos": [],
                    "forced_item_no": None,
                    "signable": False,
                    "reason_code": "complete_manifest_inconsistent",
                    "reason": "完整一对一登记无法形成完美配对，登记需核对，不能签出。",
                    "next_action": "verify_manifest",
                }
            else:
                possible = sorted(possible_by_claim.get(c_no, []))
                forced = possible[0] if len(possible) == 1 else None
                signable = forced is not None and forced in available_item_nos
                if forced and forced not in available_item_nos:
                    code = "forced_item_unavailable"
                    reason = "完整配对指向的实物已被占用或出库。"
                    action = "verify_stock"
                elif forced:
                    code = "forced_complete_pairing"
                    reason = "在完整一对一登记的所有配对中均指向同一件。"
                    action = "sign_voucher"
                else:
                    code = "ambiguous_complete_pairing"
                    reason = "完整登记仍存在多种一对一配对，需检查或复访。"
                    action = _window_next_action(round_row)
                result = {
                    "claim_no": c_no,
                    "claimant": claim.get("claimant", ""),
                    "status": claim.get("status"),
                    "candidate_item_nos": candidates,
                    "possible_item_nos": possible,
                    "forced_item_no": forced if signable else None,
                    "global_forced_item_no": forced,
                    "signable": signable,
                    "reason_code": code,
                    "reason": reason,
                    "next_action": action,
                }
            results.append(result)
            summary[result["reason_code"]] += 1
            if result["signable"]:
                summary["signable"] += 1

        item_results = _item_results(state)
        return {
            "mode": "closed_complete",
            "complete_pairing": True,
            "manifest_consistent": consistent,
            "claims": results,
            "items": item_results,
            "summary": dict(summary),
        }

    # Open registration: direct uniqueness only.  No perfect-match elimination.
    unique_claimants: dict[str, list[str]] = defaultdict(list)
    for c_no in all_claim_nos:
        claim = state["claims_by_no"][c_no]
        if claim.get("status") != "filed":
            continue
        candidates = evidence_graph.get(c_no, [])
        if len(candidates) == 1:
            unique_claimants[candidates[0]].append(c_no)

    for c_no in all_claim_nos:
        claim = state["claims_by_no"][c_no]
        candidates = evidence_graph.get(c_no, [])
        if claim.get("status") != "filed":
            result = _nonfiled_result(claim, candidates)
        elif len(candidates) == 0:
            result = {
                "claim_no": c_no,
                "claimant": claim.get("claimant", ""),
                "status": claim.get("status"),
                "candidate_item_nos": [],
                "possible_item_nos": [],
                "forced_item_no": None,
                "signable": False,
                "reason_code": "no_candidate",
                "reason": "现有登记实物中没有相容件；可由登记员显式记为无失物虚报。",
                "next_action": "mark_no_match_or_wait_for_item",
            }
        elif len(candidates) > 1:
            assigned_note = ""
            if any(
                state["items_by_no"][i].get("status") != "in_stock"
                for i in candidates
            ):
                assigned_note = "即使其中另一件已签给别人，开放登记也不允许用排除法替本领。"
            result = {
                "claim_no": c_no,
                "claimant": claim.get("claimant", ""),
                "status": claim.get("status"),
                "candidate_item_nos": candidates,
                "possible_item_nos": candidates,
                "forced_item_no": None,
                "signable": False,
                "reason_code": "ambiguous_evidence",
                "reason": "本人特征仍相容多件，有歧义，告知待核。" + assigned_note,
                "next_action": _window_next_action(round_row),
            }
        else:
            item_no = candidates[0]
            competitors = [
                c for c in unique_claimants.get(item_no, []) if c != c_no
            ]
            item_status = state["items_by_no"][item_no].get("status")
            if competitors:
                result = {
                    "claim_no": c_no,
                    "claimant": claim.get("claimant", ""),
                    "status": claim.get("status"),
                    "candidate_item_nos": candidates,
                    "possible_item_nos": candidates,
                    "forced_item_no": None,
                    "signable": False,
                    "reason_code": "conflicting_unique_claims",
                    "reason": "另一份申领也唯一指向同一件，需核对或检查。",
                    "next_action": _window_next_action(round_row),
                }
            elif item_status != "in_stock":
                result = {
                    "claim_no": c_no,
                    "claimant": claim.get("claimant", ""),
                    "status": claim.get("status"),
                    "candidate_item_nos": candidates,
                    "possible_item_nos": candidates,
                    "forced_item_no": None,
                    "signable": False,
                    "reason_code": "unique_match_unavailable",
                    "reason": "唯一相容件已预留或出库，需核对凭条/库房。",
                    "next_action": "verify_stock",
                }
            else:
                result = {
                    "claim_no": c_no,
                    "claimant": claim.get("claimant", ""),
                    "status": claim.get("status"),
                    "candidate_item_nos": candidates,
                    "possible_item_nos": candidates,
                    "forced_item_no": item_no,
                    "signable": True,
                    "reason_code": "unique_feature_match",
                    "reason": "在全部登记实物中只有这一件与本人确知特征相容。",
                    "next_action": "sign_voucher",
                }
        results.append(result)
        summary[result["reason_code"]] += 1
        if result["signable"]:
            summary["signable"] += 1

    return {
        "mode": "open_evidence",
        "complete_pairing": False,
        "allow_unfound": bool(int(round_row.get("allow_unfound", 1))),
        "manifest_consistent": True,
        "claims": results,
        "items": _item_results(state),
        "summary": dict(summary),
    }


def _nonfiled_result(claim: dict[str, Any], candidates: list[str]) -> dict[str, Any]:
    status = claim.get("status")
    if status == "approved":
        action = "outbound_with_voucher"
        reason = "已签领物凭条，等待任一窗口出库。"
    elif status == "out":
        action = "collected"
        reason = "已经完成出库交接。"
    elif status == "no_match":
        action = "closed_no_match"
        reason = "已显式登记为无对应失物的申领。"
    else:
        action = "closed"
        reason = f"申领状态为 {status}。"
    return {
        "claim_no": claim["claim_no"],
        "claimant": claim.get("claimant", ""),
        "status": status,
        "candidate_item_nos": candidates,
        "possible_item_nos": [],
        "forced_item_no": None,
        "signable": False,
        "reason_code": f"claim_{status}",
        "reason": reason,
        "next_action": action,
    }


def _window_next_action(round_row: dict[str, Any]) -> str:
    if round_row.get("window_mode") == "sealed":
        return "sealed_statement_then_custodian_inspection"
    return "revisit_two_choice_answer"


def _item_results(state: State) -> list[dict[str, Any]]:
    out = []
    for item_no, item in state["items_by_no"].items():
        features = state.get("features", {}).get(item_no, {})
        out.append(
            {
                "item_no": item_no,
                "label": item.get("label", ""),
                "status": item.get("status"),
                "revealed_features": sorted(
                    k for k, f in features.items() if int(f.get("revealed", 0))
                ),
                "unrevealed_features": sorted(
                    k for k, f in features.items() if not int(f.get("revealed", 0))
                ),
            }
        )
    return out


def with_feature_overrides(state: State, overrides: dict[tuple[str, str], str]) -> State:
    """Deep-copy state and make hypothetical feature values visible."""
    simulated = copy.deepcopy(state)
    for (item_no, key), value in overrides.items():
        feature = simulated["features"][item_no][key]
        feature["revealed"] = 1
        feature["actual_value"] = value
    return simulated
