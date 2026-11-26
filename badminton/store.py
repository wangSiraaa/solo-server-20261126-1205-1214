"""本地持久化：每局一个 JSONL 事件日志，追加写 + fsync。

重启后只需重放事件日志即可恢复；同一张边裁记录（幂等键）重放不会
凭空多打一拍，因为投影里保留了已处理幂等键。
"""

import json
import os
import threading

from . import projection


class IdempotencyError(Exception):
    def __init__(self, previous_event_id):
        super().__init__("重复请求，已处理过该幂等键")
        self.previous_event_id = previous_event_id


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._locks = {}
        self._master_lock = threading.Lock()

    def _path(self, game_id: str) -> str:
        return os.path.join(self.data_dir, f"{game_id}.jsonl")

    def _lock_for(self, game_id: str) -> threading.Lock:
        with self._master_lock:
            lock = self._locks.get(game_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[game_id] = lock
            return lock

    def exists(self, game_id: str) -> bool:
        return os.path.exists(self._path(game_id))

    def load_events(self, game_id: str):
        path = self._path(game_id)
        if not os.path.exists(path):
            return None
        events = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def load_state(self, game_id: str):
        events = self.load_events(game_id)
        if events is None:
            return None
        return projection.replay(events)

    def find_idempotent(self, game_id: str, idempotency_key: str):
        """幂等预检：在任何业务校验之前查同一幂等键的首次事件。"""
        if not idempotency_key:
            return None
        for event in self.load_events(game_id) or []:
            if event.get("idempotency_key") == idempotency_key:
                return event
        return None

    def _write_batch(self, game_id: str, new_events: list, idempotency_key):
        lock = self._lock_for(game_id)
        with lock:
            events = self.load_events(game_id) or []
            if idempotency_key is not None:
                for old in events:
                    if old.get("idempotency_key") == idempotency_key:
                        raise IdempotencyError(old["event_id"])
            next_id = len(events) + 1
            stamped = []
            for idx, event in enumerate(new_events):
                event = dict(event)
                event["event_id"] = next_id + idx
                if idempotency_key is not None:
                    event["idempotency_key"] = idempotency_key
                stamped.append(event)
            events.extend(stamped)
            path = self._path(game_id)
            with open(path, "a", encoding="utf-8") as fh:
                for event in stamped:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return stamped

    def append(self, game_id: str, event: dict, idempotency_key: str | None = None):
        """串行化追加单个事件；幂等键重复时拒绝并回传首次事件。"""
        return self._write_batch(game_id, [event], idempotency_key)[0]

    def append_batch(
        self, game_id: str, events: list, idempotency_key: str | None = None
    ):
        """同一把锁内原子追加多条事件，共用一个幂等键。"""
        return self._write_batch(game_id, events, idempotency_key)
