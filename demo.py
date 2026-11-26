#!/usr/bin/env python3
"""Walk the spec's narrative end-to-end and print the traces.

Run:  python3 demo.py
"""

import json
import os
import tempfile

from lampshop.db import Database
from lampshop.service import DomainService

T = dict(
    root="2026-01-01T00:00:00Z",
    ab_start="2026-01-05T09:00:00Z", ab_end="2026-01-05T13:00:00Z",
    b_prod="2026-01-06T00:00:00Z",
    bc_start="2026-01-10T09:00:00Z", bc_end="2026-01-10T13:00:00Z",
    c_qual="2026-01-11T00:00:00Z",
    sign="2026-01-12T10:00:00Z",
    shift_s="2026-01-12T08:00:00Z", shift_e="2026-01-12T18:00:00Z",
)


def head(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def show(label, obj):
    print(f"\n[{label}]")
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def main():
    tmp = tempfile.mkdtemp()
    svc = DomainService(Database(os.path.join(tmp, "demo.db")))

    head("0. Setup: people, craft, posts, and root mentor 甲")
    for pid, name in [("A", "甲"), ("B", "乙"), ("C", "丙")]:
        svc.create_person(name, pid)
    svc.create_craft("shade", "灯罩", min_minutes=120)
    svc.add_craft_step("shade", "cut", "裁片", 1)
    svc.add_craft_step("shade", "sew", "缝制", 2)
    svc.create_product_type("lamp", "灯罩成品", ["shade"])
    svc.create_station("bench1", "一号台")
    svc.create_post("signer", "签收岗", sign_authority=True)
    svc.create_post("apprentice", "学徒岗", sign_authority=False)
    svc.seed_root("A", ["shade"], T["root"])
    print("甲 is the initial mentor (root production + mentoring authority).")

    head("1. Registration book is filled before arrival; completion creates evidence")
    ev_ab = svc.register_evidence(
        "shade", T["ab_start"], T["ab_end"], ["A"], ["B"], ["cut", "sew"],
        station_id="bench1", registered_at="2026-01-02T08:00:00Z", evidence_id="ev-ab")
    print(f"before completion: status={ev_ab['status']}, is_evidence={ev_ab['is_evidence']}")
    svc.complete_evidence("ev-ab", T["ab_end"])
    print(f"after  completion: is_evidence={svc.get_evidence('ev-ab')['is_evidence']}")

    head("2. 乙 earns production standing, then mentoring authority")
    svc.grant_qualification("B", "shade", "production", T["b_prod"])
    svc.grant_qualification("B", "shade", "mentoring", T["b_prod"], enforce_eligibility=False)
    print("乙 can now teach.")

    head("3. 乙 teaches 丙; 丙 becomes eligible")
    svc.register_evidence(
        "shade", T["bc_start"], T["bc_end"], ["B"], ["C"], ["cut", "sew"],
        station_id="bench1", registered_at="2026-01-09T00:00:00Z", evidence_id="ev-bc")
    svc.complete_evidence("ev-bc", T["bc_end"])
    show("丙 eligibility", svc.eligibility("C", "shade", T["c_qual"]))
    svc.grant_qualification("C", "shade", "production", T["c_qual"])

    head("4. 丙 signs the first independent piece while on the signing post")
    svc.create_shift("C", "signer", T["shift_s"], T["shift_e"])
    svc.create_product("lamp", "piece-001")
    res = svc.sign_product("piece-001", "C", "req-001", at_ts=T["sign"])
    show("signed basis (snapshot)", res["basis"])

    head("5. Shopkeeper traces from product number to the basis actually used")
    show("product_basis(piece-001)", svc.product_basis("piece-001"))

    head("6. Resend the same request after a 'restart' -> no second final signer")
    fresh = DomainService(Database(os.path.join(tmp, "demo.db")))
    replay = fresh.sign_product("piece-001", "C", "req-001", at_ts=T["sign"])
    print(f"replayed={replay['replayed']}, same signature_id="
          f"{replay['signature_id'] == res['signature_id']}")

    head("7. Retract 乙->丙 evidence; old piece keeps its basis, new piece cannot reuse it")
    svc.retract_evidence("ev-bc", "recorded against the wrong crew", at_ts="2026-01-20T00:00:00Z")
    print("old piece still signed:",
          bool(svc.product_basis("piece-001")["signed"]))
    show("old piece current evidence status",
         svc.product_basis("piece-001")["current_status_of_cited_evidence"])
    svc.create_shift("C", "signer", "2026-01-21T08:00:00Z", "2026-01-21T18:00:00Z")
    svc.create_product("lamp", "piece-002")
    try:
        svc.sign_product("piece-002", "C", "req-002", at_ts="2026-01-21T10:00:00Z")
        print("UNEXPECTED: new piece signed")
    except Exception as exc:
        print(f"new piece rejected as expected: {getattr(exc, 'code', exc)}")

    head("8. The retraction is an event, never an erasure")
    show("audit trail for ev-bc", svc.audit_trail(entity_id="ev-bc"))

    print("\nDemo complete. Database was temporary.\n")


if __name__ == "__main__":
    main()
