"""重放 CLI：python -m badminton.replay <data_dir> <game_id>

从本地 JSONL 事件日志重放整局，打印事件流与最终状态。
重启后重放同一日志，状态逐位一致，不会凭空多打一拍。
"""

import json
import sys

from . import projection
from .service import Service
from .store import Store


def main(argv):
    if len(argv) != 2:
        print("用法: python -m badminton.replay <data_dir> <game_id>")
        return 2
    data_dir, game_id = argv
    store = Store(data_dir)
    events = store.load_events(game_id)
    if events is None:
        print(f"找不到本局日志: {game_id}")
        return 1

    print(f"=== 局 {game_id} 事件流（共 {len(events)} 条）===")
    for event in events:
        line = f"#{event['event_id']:>3} {event['type']}"
        if event.get("idempotency_key"):
            line += f"  [idem={event['idempotency_key']}]"
        print(line, json.dumps(event["data"], ensure_ascii=False))

    state = projection.replay(events)
    print("\n=== 重放得到的最终状态 ===")
    print(json.dumps(Service.public_state(state), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
