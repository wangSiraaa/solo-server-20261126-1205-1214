"""Append-only JSONL event log with synchronous durability.

Every mutation is one or more events written as single lines and flushed with
``fsync`` before the request returns.  Rebuilding state is a pure replay, so a
process restart reproduces exactly what visitors could walk at any past instant
(the opening snapshots are events too).
"""

import json
import os
import threading
import time

from .state import State, StateError


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.state = State()
        self._seq = 0
        self._fh = None
        self._load()

    # ---- loading -----------------------------------------------------------
    def _load(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        if not os.path.exists(self.path):
            open(self.path, "a").close()
        good_bytes = 0
        with open(self.path, "rb") as fh:
            for raw in fh:
                if not raw.endswith(b"\n"):
                    # torn tail from a crash; drop it on the floor
                    break
                try:
                    ev = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    break
                try:
                    self.state.apply(ev)
                except StateError:
                    # A corrupted-but-well-formed line should not brick the
                    # service; stop replay at the last consistent point.
                    break
                self._seq = max(self._seq, int(ev.get("seq", self._seq)))
                good_bytes += len(raw)
        # Truncate any torn/corrupt tail so future appends stay parseable.
        size = os.path.getsize(self.path)
        if good_bytes != size:
            with open(self.path, "r+b") as fh:
                fh.truncate(good_bytes)
        self._fh = open(self.path, "a", encoding="utf-8")

    # ---- writing -----------------------------------------------------------
    def make_events(self, specs):
        now = time.time()
        out = []
        for spec in specs:
            self._seq += 1
            out.append(
                {
                    "seq": self._seq,
                    "ts": now,
                    "type": spec["type"],
                    "data": spec.get("data", {}),
                }
            )
        return out

    def append(self, events):
        with self.lock:
            for ev in events:
                self._fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self):
        with self.lock:
            if self._fh:
                self._fh.close()
                self._fh = None
