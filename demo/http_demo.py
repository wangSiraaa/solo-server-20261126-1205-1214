"""题目验收场景的完整 HTTP 演示脚本（对运行中的本地服务发请求）。

用法：
  python -m photolab.cli serve --db /tmp/lab.db --port 8091 &
  python demo/http_demo.py http://127.0.0.1:8091
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8091"


def call(method: str, path: str, payload=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def minimal_payload(batch="B1", card=(("阶二", 2), ("阶三", 3)),
                    start=None, budget=2, order_id="O1", paper="P1"):
    start = start or {"0,0": 2, "0,1": 1}
    return {
        "batch_id": batch, "rows": 1, "cols": 2,
        "card": [list(pair) for pair in card],
        "sheet_budget": 5, "shot_budget": budget,
        "papers": [{"id": paper, "start": start, "beam_per_cell": {}}],
        "order": {
            "id": order_id, "customer": "订户", "reuse_count": 0,
            "requirements": [
                {"paper_id": paper, "cell": "0,0", "role": "face", "level": "阶二"},
                {"paper_id": paper, "cell": "0,1", "role": "background", "level": "阶三"},
            ],
        },
    }


def main() -> None:
    print("=" * 70)
    print("场景 1：最小生意。脸累计二、背景累计一，目标脸阶二/背景阶三，预算两拍")
    print("=" * 70)
    code, _ = call("POST", "/batches", minimal_payload())
    print(f"开单 -> {code}")
    code, prep = call("POST", "/batches/B1/prepare")
    print(f"备料 -> {code}，可行={prep['feasible']}，需遮片 {prep['plan']['masks']}，"
          f"总拍数 {prep['plan']['shot_total']}")
    for shot in prep["plan"]["papers"][0]["shots"]:
        print(f"  一拍：遮片 {shot['mask']} 翻到 {shot['offset']}，透光 {shot['exposures']}")
    call("POST", "/batches/B1/open")
    code, s1 = call("POST", "/papers/P1/shots")
    print(f"第 1 拍回条 -> {code}，累计 {s1['accumulated']}，完成={s1['completed']}")
    code, s2 = call("POST", "/papers/P1/shots")
    print(f"第 2 拍回条 -> {code}，累计 {s2['accumulated']}，完成={s2['completed']}，成品 {s2['product']}")
    code, late = call("POST", "/papers/P1/shots")
    print(f"迟到的第 3 拍回条 -> {code} {late['error']}")
    code, sample = call("POST", "/papers/P1/samples", {"sample_id": "S1", "note": "接新样"})
    print(f"接样 -> 实测色阶 {sample['observed']}")
    code, delivery = call("POST", "/orders/O1/deliver")
    print(f"取件 -> {delivery['status']}，成品 {[p['product'] for p in delivery['products']]}")

    print()
    print("=" * 70)
    print("场景 2：只换样卡，背景目标阶从光量四才开始，仍只剩两拍 -> 不能交")
    print("=" * 70)
    call("POST", "/batches", minimal_payload(
        "B2", card=(("阶二", 2), ("阶三", 4)), order_id="O2", paper="P2"))
    code, prep2 = call("POST", "/batches/B2/prepare")
    print(f"备料 -> HTTP {code}，可行={prep2['feasible']}，原因：{prep2['reason']}")

    print()
    print("=" * 70)
    print("场景 3：恢复原样卡，仅把脸改成已超过目标阶（脸累计三）-> 不能交")
    print("=" * 70)
    call("POST", "/batches", minimal_payload(
        "B3", start={"0,0": 3, "0,1": 1}, order_id="O3", paper="P3"))
    code, prep3 = call("POST", "/batches/B3/prepare")
    print(f"备料 -> HTTP {code}，可行={prep3['feasible']}，原因：{prep3['reason']}")

    print()
    print("=" * 70)
    print("场景 4：另一张起点不同的纸（背景零起步），预算三拍 -> 算出只照背景三拍")
    print("=" * 70)
    call("POST", "/batches", minimal_payload(
        "B4", start={"0,0": 2, "0,1": 0}, budget=3, order_id="O4", paper="P4"))
    code, prep4 = call("POST", "/batches/B4/prepare")
    print(f"备料 -> 可行={prep4['feasible']}，总拍数 {prep4['plan']['shot_total']}")
    for shot in prep4["plan"]["papers"][0]["shots"]:
        print(f"  一拍：遮片 {shot['mask']} 翻到 {shot['offset']}，透光 {shot['exposures']}")
    call("POST", "/batches/B4/open")
    for i in range(3):
        call("POST", "/papers/P4/shots")
    code, status = call("GET", "/batches/B4/status")
    print(f"三拍后累计 -> {status['papers'][0]['accumulated']}")


if __name__ == "__main__":
    main()
