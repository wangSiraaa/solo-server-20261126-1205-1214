"""JSON 文件持久化与线程安全存储。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


class Store:
    def __init__(self, path: Optional[str | os.PathLike[str]] = None):
        self.path = Path(path or os.environ.get("WHITEBOARD_DATA", "/workspace/data/whiteboard.json"))
        self._lock = threading.RLock()
        self.state: Dict[str, Any] = {
            "version": 1,
            "teacher_sessions": {},
            "sessions": {},
            "boards": {},
            "merges": {},
        }
        self.load()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                raw = self.path.read_text(encoding="utf-8")
                if raw.strip():
                    loaded = json.loads(raw)
                    for key, value in self.state.items():
                        loaded.setdefault(key, value if not isinstance(value, dict) else {})
                    self.state = loaded

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.state, f, ensure_ascii=False, indent=2, sort_keys=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, self.path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)

    # 常用集合访问器，集中处理 KeyError。
    def get(self, collection: str, resource_id: str) -> Dict[str, Any]:
        try:
            return self.state[collection][resource_id]
        except KeyError as exc:
            raise KeyError(f"{collection[:-1] if collection.endswith('s') else collection} not found: {resource_id}") from exc

    def put(self, collection: str, resource_id: str, value: Dict[str, Any]) -> None:
        self.state.setdefault(collection, {})[resource_id] = value
