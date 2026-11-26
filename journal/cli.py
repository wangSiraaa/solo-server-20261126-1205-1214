"""Local command-line entry point for the translation-journal desk.

Examples
--------
    python -m journal.cli --db desk.db create-issue island --mode rolling
    python -m journal.cli --db desk.db add-translator tr1 --slots 周二,周三
    ...
    python -m journal.cli --db desk.db query island      # survives restart

Every command prints JSON, so a reader (or another script) can verify the
deductions and the opening receipt directly.
"""

from __future__ import annotations

import argparse
import json
import sys

from .service import JournalError, JournalService, NeedsEditorDecision


def _split(v: str | None) -> list[str]:
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="journal", description=__doc__)
    p.add_argument("--db", default="desk.db", help="SQLite path (default desk.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ci = sub.add_parser("create-issue")
    ci.add_argument("issue_id")
    ci.add_argument("--title", default="")
    ci.add_argument("--mode", default="joint", choices=["joint", "rolling"])

    at = sub.add_parser("add-translator")
    at.add_argument("translator_id")
    at.add_argument("--name", default="")
    at.add_argument("--slots", default="", help="comma list of shift labels")

    ap = sub.add_parser("add-piece")
    ap.add_argument("issue_id")
    ap.add_argument("piece_id")
    ap.add_argument("--title", default="")
    ap.add_argument("--points", type=int, required=True)
    ap.add_argument("--duration", type=int, default=1)
    ap.add_argument("--eligible", required=True, help="comma list translator ids")
    ap.add_argument("--wish", type=int, default=0, help="advertised wish (not money)")

    pl = sub.add_parser("pledge")
    pl.add_argument("issue_id")
    pl.add_argument("pledge_id")
    pl.add_argument("supporter")
    pl.add_argument("--points", type=int, required=True)
    pl.add_argument("--accepts", default="", help="comma list; empty means any")
    pl.add_argument("--requires", default="", help="comma list of prerequisite pieces")
    pl.add_argument("--cap", type=int, default=None, help="per-issue ceiling for supporter")

    wd = sub.add_parser("withdraw")
    wd.add_argument("issue_id")
    wd.add_argument("pledge_id")

    sub.add_parser("candidates").add_argument("issue_id")

    oj = sub.add_parser("open-joint")
    oj.add_argument("issue_id")
    oj.add_argument("--request-id", required=True)
    oj.add_argument("--selection", default=None,
                    help="candidate_id or comma piece list")

    op = sub.add_parser("open-piece")
    op.add_argument("issue_id")
    op.add_argument("--request-id", required=True)
    op.add_argument("--piece", default=None)

    q = sub.add_parser("query")
    q.add_argument("issue_id")

    r = sub.add_parser("receipt")
    r.add_argument("receipt_id")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    svc = JournalService(args.db)
    try:
        if args.cmd == "create-issue":
            out = svc.create_issue(args.issue_id, args.title, args.mode)
        elif args.cmd == "add-translator":
            out = svc.add_translator(args.translator_id, args.name, _split(args.slots))
        elif args.cmd == "add-piece":
            out = svc.add_piece(
                args.issue_id, args.piece_id, args.title, args.points,
                args.duration, _split(args.eligible), wish_points=args.wish,
            )
        elif args.cmd == "pledge":
            out = svc.pledge(
                args.issue_id, args.pledge_id, args.supporter, args.points,
                accepts=_split(args.accepts), requires=_split(args.requires),
                supporter_cap=args.cap,
            )
        elif args.cmd == "withdraw":
            out = svc.withdraw_pledge(args.issue_id, args.pledge_id)
        elif args.cmd == "candidates":
            out = svc.list_candidates(args.issue_id)
        elif args.cmd == "open-joint":
            sel = None
            if args.selection:
                sel = _split(args.selection) if "," in args.selection else args.selection
            out = svc.open_joint(args.issue_id, args.request_id, sel)
        elif args.cmd == "open-piece":
            out = svc.open_piece(args.issue_id, args.request_id, args.piece)
        elif args.cmd == "query":
            out = svc.query_issue(args.issue_id)
        elif args.cmd == "receipt":
            out = svc.get_receipt(args.receipt_id)
        else:  # pragma: no cover
            raise JournalError(f"unknown command {args.cmd}")
    except NeedsEditorDecision as exc:
        print(json.dumps({"error": "needs_editor_decision",
                          "issue_id": exc.issue_id,
                          "candidates": exc.candidates}, ensure_ascii=False, indent=2))
        return 2
    except JournalError as exc:
        print(json.dumps({"error": "journal_error", "message": str(exc),
                          "issue_id": getattr(exc, "issue_id", None)},
                         ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
