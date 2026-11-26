"""Reproducible end-to-end walkthrough of the required scenarios.

Run:  python3 scripts/demo.py [db_path]
Prints the reader-facing facts at each step.  Each stage is a fresh set of
service calls on the same SQLite file; stage 3 re-opens the file to prove a
cold restart sees the same data.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from journal.service import JournalError, JournalService, NeedsEditorDecision  # noqa: E402


def show(title: str, obj) -> None:
    import json
    print(f"\n### {title}")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)

    # ---- Stage 1: open the one unconditional piece --------------------------
    s = JournalService(path)
    s.create_issue("island", "海岛篇", "rolling")
    s.add_translator("t1", "唯一译者", ["周二"])          # Tuesday is the ONLY shift
    s.add_piece("island", "island", "海岛", 6, 1, ["t1"], wish_points=40)
    print("Stage 1: wish=40 but no pledge ->", s.list_candidates("island"))
    s.pledge("island", "p1", "alice", 6, accepts=["island"])
    receipt = s.open_piece("island", "req-open-island", "island")
    show("Stage 1: open island (receipt)", receipt)
    show("Stage 1: reader query", s.query_issue("island")["pieces"]["island"])

    # replay the same request
    show("Stage 1: replay same request (no double charge)",
         s.open_piece("island", "req-open-island", "island")["pledge_deducted"])
    s.db.close()

    # ---- Stage 2: conditional version, other piece has no translator --------
    s = JournalService(path)
    s.create_issue("cond", "条件篇", "rolling")
    s.add_piece("cond", "island", "海岛", 6, 1, ["t1"])
    s.add_piece("cond", "other", "另一篇", 6, 1, [])       # nobody can translate
    s.pledge("cond", "p2", "alice", 6, accepts=["island"], requires=["other"])
    print("\nStage 2: conditional + untranslatable other -> candidates:",
          s.list_candidates("cond"))
    try:
        s.open_piece("cond", "req-cond", "island")
    except JournalError as exc:
        print("Stage 2: cannot open ->", exc)

    # ---- Stage 3: unconditional again, but Tuesday is already taken ---------
    s.create_issue("restored", "已占档期", "rolling")
    s.add_piece("restored", "island2", "海岛二", 6, 1, ["t1"])
    s.pledge("restored", "p3", "alice", 6, accepts=["island2"])
    print("\nStage 3: unconditional but t1/周二 booked by stage 1 -> candidates:",
          s.list_candidates("restored"))
    try:
        s.open_piece("restored", "req-restored", "island2")
    except JournalError as exc:
        print("Stage 3: cannot open ->", exc)

    # ---- Stage 4: mutual-condition joint cutoff -----------------------------
    # Fresh translators: t1's only shift was consumed in stage 1.
    s.create_issue("joint", "联合成刊", "joint")
    s.add_translator("t5", "T5", ["周一"])
    s.add_translator("t6", "T6", ["周四"])
    s.add_piece("joint", "bridge", "桥梁", 6, 1, ["t5"])
    s.add_piece("joint", "island", "海岛", 6, 1, ["t6"])
    s.pledge("joint", "pa", "alice", 6, accepts=["bridge"], requires=["island"])
    s.pledge("joint", "pb", "bob", 6, accepts=["island"], requires=["bridge"])
    print("\nStage 4: mutual wait -> candidates:",
          [c["candidate_id"] for c in s.list_candidates("joint")])
    show("Stage 4: one joint cutoff realises both", s.open_joint("joint", "req-joint"))
    s.db.close()

    # ---- Stage 5: cold restart, reader checks by issue ---------------------
    s = JournalService(path)
    q = s.query_issue("joint")
    show("Stage 5: cold restart, reader sees who took which piece",
         {pid: {"opened": p["opened"], "translator": p["translator"],
                "funding": p["funding"]}
          for pid, p in q["pieces"].items()})
    show("Stage 5: replay the joint cutoff (no double charge)",
         s.open_joint("joint", "req-joint")["pledge_deducted"])

    # ---- Stage 6: three pieces, two translators -> editor must pick --------
    s.create_issue("pick", "需取舍", "joint")
    s.add_translator("t3", "T3", ["周四", "周五"])
    s.add_translator("t7", "T7", ["周一", "周三"])
    for pid in ("a", "b", "c"):
        s.add_piece("pick", pid, pid.upper(), 5, 1, ["t3", "t7"])
    s.pledge("pick", "pp", "carol", 10, accepts=["a", "b", "c"])
    try:
        s.open_joint("pick", "req-pick")
    except NeedsEditorDecision as exc:
        show("Stage 6: several feasible batches -> editor chooses",
             [c["candidate_id"] for c in exc.candidates])
    show("Stage 6: editor records choice a|c",
         s.open_joint("pick", "req-pick", selection="a|c"))

    # ---- Stage 7: per-issue ceiling is not per-piece -----------------------
    s.create_issue("cap", "五点上限", "joint")
    s.add_translator("t8", "T8", ["周一", "周二"])
    s.add_piece("cap", "x", "X", 5, 1, ["t8"])
    s.add_piece("cap", "y", "Y", 5, 1, ["t8"])
    s.pledge("cap", "pc", "dave", 5, accepts=["x", "y"], supporter_cap=5)
    print("\nStage 7: cap=5 total (not 5 per piece) ->",
          [c["candidate_id"] for c in s.list_candidates("cap")])
    s.db.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else tempfile.mktemp(suffix=".db")
    main(db)
