"""JSON persistence with atomic replacement.

The service is intentionally dependency-free.  A JSON document is enough for a
single counter, while atomic writes mean a process restart sees either the old
committed state or the new committed state, never a half-written file.
"""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from typing import Any, Dict


def default_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "counters": {},
        "people": {},
        "pieces": {},
        "intakes": [],
        "clasps": {},
        "chains": {},
        "orders": {},
        "quotes": {},
        "visits": {},
        "consents": {},
        "clasp_openings": {},
        "removals": {},
        "temp_removals": [],
        "chain_returns": {},
        "rentals": {},
        "events": [],
    }


class JsonStore:
    def __init__(self, path: str):
        self.path = path
        self.state = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path or self.path == ":memory:":
            return default_state()
        if not os.path.exists(self.path):
            return default_state()
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        base = default_state()
        base.update(data)
        return base

    def save(self) -> None:
        if not self.path or self.path == ":memory:":
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".beadworks-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def snapshot(self) -> Dict[str, Any]:
        return deepcopy(self.state)
