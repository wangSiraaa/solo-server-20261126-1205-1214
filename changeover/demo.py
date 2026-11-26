"""Interactive / scripted demo of the changeover backend.

Run::

    python3 -m changeover.demo baseline
    python3 -m changeover.demo retract
    python3 -m changeover.demo stale
    python3 -m changeover.demo whole-hall
    python3 -m changeover.demo successive
    python3 -m changeover.demo restart

Each subcommand is a self-contained story from the brief, printing the actual
server responses so a duty operator can watch the promises keep or fail.
"""

import json
import os
import sys
import tempfile

from .engine import build_engine
from .fixtures import build_baseline
from .fixtures_successive import build_successive


def hr(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def show(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def confirm(e, sid, v):
    for k in ("received", "buffered", "displayed"):
        e.device_state(sid, k, v)


def _tmp():
    return tempfile.mktemp(suffix=".jsonl")


# ---------------------------------------------------------------------------
def demo_baseline():
    hr("BASELINE: east -> corridor -> side door -> lecture room; room is poetry's")
    p = _tmp()
    e, op = build_baseline(p)
    print("\npresentation for east entrance (first door + activity):")
    show(e.screen_view("sc-east"))
    print("\nhall sign really showing (侧门 d-side 去讲室):")
    show(e.screen_view("sc-hall"))
    print("\nthe certified opening that follows from real display + open doors:")
    show(op)
    print("\nvisitor trial walk east -> poetry:")
    show(e.walk("sc-east", "poetry"))
    e.store.close()
    os.unlink(p)


def demo_retract():
    hr("RETRACT: field receipt says the corridor door is STILL partitioned")
    p = _tmp()
    e, op = build_baseline(p)
    print("before:", op["number"], op["status"])
    e.door_field("d-side", False)
    print("\nfield receipt: d-side still partitioned -> opening must be withdrawn:")
    show(e.opening_view(op["number"]))
    print("\nattempt to certify east again:")
    try:
        e.open_direct("poetry", "sc-east")
    except Exception as ex:
        print("refused:", ex.code, "-", ex.message)
    e.store.close()
    os.unlink(p)


def demo_stale():
    hr("STALE: doors restored, but hall's real display points back at the east door")
    p = _tmp()
    e, op = build_baseline(p)
    e.door_field("d-side", False)
    print("opening after partition:", e.opening_view(op["number"])["status"])
    e.door_field("d-side", True)
    v = e.send_copy("sc-hall", "poetry", "d-east")["sent_version"]
    confirm(e, "sc-hall", v)
    print("\nhall real display now:", e.screen_view("sc-hall")["display_door"])
    print("visitor walk east -> poetry:", e.walk("sc-east", "poetry"))
    print("\ncannot certify open off a loop:")
    try:
        e.open_direct("poetry", "sc-east")
    except Exception as ex:
        print("refused:", ex.code, "-", ex.message)
    e.store.close()
    os.unlink(p)


def demo_whole_hall():
    hr("WHOLE-HALL: mask first, partition真到位, stage signage, then open doors")
    p = _tmp()
    e, _ = build_baseline(p)
    e.register_activity("symposium", "研讨", ["room"], True, "研讨主持")
    c = e.start_changeover(
        "whole_hall", "poetry", "symposium", ["sc-east"],
        doors_to_open=["d-side"], doors_to_partition=["d-side"],
    )["changeover"]
    print("\nplan:")
    show(e.plan(c))
    e.door_field("d-side", False)
    e.changeover_field(c, "d-side")
    for s in e.conflict_screens(e.state.changeovers[c]):
        e.set_mask(s, True)
    print("\nmasked conflicts, partition field-confirmed. stage new signage:")
    plan = e._route_plan("east", "symposium", extra_open=["d-side"])
    for sid, did in plan:
        v = e.send_copy(sid, "symposium", did)["sent_version"]
        confirm(e, sid, v)
        e.set_mask(sid, False)
    print("blockers before doors open:")
    show(e.status(c)["entrances"]["sc-east"]["blockers"])
    e.door_field("d-side", True)
    print("blockers after doors open:")
    show(e.status(c)["entrances"]["sc-east"]["blockers"])
    print("\ncertify + host receipt:")
    show(e.attempt_open(c))
    show(e.receipts_view())
    e.store.close()
    os.unlink(p)


def demo_successive():
    hr("SUCCESSIVE: hand each entrance over; never drop the last old lifeline")
    p = _tmp()
    e = build_successive(p)
    c = e.start_changeover("successive", "lecture", "poetry", ["sc-east", "sc-west"])["changeover"]
    print("\nhand EAST over first (west keeps the old route):")
    r = e.switch_entrance(c, "sc-east")
    for cp in r["copies"]:
        confirm(e, cp["screen"], cp["version"])
    show(r)
    print("east -> poetry walkable:", e.walk("sc-east", "poetry")["ok"])
    print("west -> lecture still walkable:", e.walk("sc-west", "lecture")["ok"])
    print("\ntry to switch WEST before the host ends the old activity:")
    try:
        e.switch_entrance(c, "sc-west")
    except Exception as ex:
        show({"refused": ex.code, "message": ex.message, "details": ex.details})
    print("\nhost registers old activity ended, now switch WEST:")
    e.end_activity("lecture")
    r = e.switch_entrance(c, "sc-west")
    for cp in r["copies"]:
        confirm(e, cp["screen"], cp["version"])
    print("west -> poetry walkable:", e.walk("sc-west", "poetry")["ok"])
    print("\noffline inspection of actual remaining guidance:")
    e.set_online("sc-hall", False)
    show(e.left_guidance(entrance_screen="sc-west", target="poetry"))
    e.store.close()
    os.unlink(p)


def demo_restart():
    hr("RESTART: a late stale 'displayed' must not change the reproduced route")
    p = _tmp()
    e, op = build_baseline(p)
    n = op["number"]
    print("opening", n, "route:", [(h["screen"], h["door"]) for h in op["route"]])
    v2 = e.send_copy("sc-hall", "poetry", "d-side")["sent_version"]
    confirm(e, "sc-hall", v2)
    print("\ndeliver a LATE confirmation of the older version v%d:" % (v2 - 1))
    show(e.device_state("sc-hall", "displayed", v2 - 1)["notes"])
    e.store.close()
    print("\nrestart and query opening number again:")
    e2 = build_engine(p)
    show(e2.opening_view(n))
    print("\nand opening numbers survive:", [o["number"] for o in e2.openings_view()])
    e2.store.close()
    os.unlink(p)


COMMANDS = {
    "baseline": demo_baseline,
    "retract": demo_retract,
    "stale": demo_stale,
    "whole-hall": demo_whole_hall,
    "successive": demo_successive,
    "restart": demo_restart,
}


def main(argv):
    cmds = argv[1:] or list(COMMANDS)
    for name in cmds:
        if name not in COMMANDS:
            print("unknown demo %r; choose from %s" % (name, ", ".join(COMMANDS)))
            return 2
        COMMANDS[name]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
