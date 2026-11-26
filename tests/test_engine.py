"""领域规则单元测试，覆盖碰撞、遮挡、窗口、幂等等关键不变量。"""

import os
import shutil
import tempfile
import unittest

from danmaku.engine import Engine
from danmaku.model import (
    ORDER_BROKEN,
    ORDER_EXPIRED,
    ORDER_LOCKED,
    ORDER_SETTLED,
    build_trajectory,
    cell_key,
    trajectory_is_complete,
)
from danmaku.player import Player
from danmaku.store import Store, replay_dir


class TempShow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="danmaku-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, mode="presale", width=3, tracks=1):
        d = os.path.join(self.tmp, f"show-{mode}")
        return Engine(Store.create(d, "s", "screen", width, tracks, mode))


class TestTrajectory(TempShow):
    def test_width1_speed1_three_positions(self):
        # 题目基准：连续三拍依次占第 3、2、1 列。
        traj = build_trajectory("o", width=1, speed=1, screen_w=3,
                                track=1, start=1)
        cols = [(s.beat, s.right_col) for s in traj.visible_steps]
        self.assertEqual(cols, [(1, 3), (2, 2), (3, 1)])
        self.assertTrue(trajectory_is_complete(traj, 3))

    def test_wide_bar_covers_each_column(self):
        # 宽 2、速 1：每个可见列都至少经过一次，且完整展示要求 W 个可见拍。
        traj = build_trajectory("o", width=2, speed=1, screen_w=3,
                                track=1, start=1)
        seen = set()
        for s in traj.visible_steps:
            seen.update(range(s.left_col, s.right_col + 1))
        self.assertEqual(seen, {1, 2, 3})
        self.assertEqual(len(traj.visible_steps), 3)
        self.assertTrue(trajectory_is_complete(traj, 3))

    def test_speed_skipping_column_is_incomplete(self):
        # 速 2 在 3 列屏上跳过中间列，不构成完整展示。
        traj = build_trajectory("o", width=1, speed=2, screen_w=3,
                                track=1, start=1)
        self.assertFalse(trajectory_is_complete(traj, 3))


class TestPresale(TempShow):
    def test_lock_blocks_later_order(self):
        eng = self.make()
        r1 = eng.place_presale_order("O1", "A", 1, 1, 1, 1)
        self.assertTrue(r1["accepted"])
        r2 = eng.place_presale_order("O2", "B", 1, 1, 1, 1)
        self.assertFalse(r2["accepted"])
        self.assertEqual(eng.s.orders["O2"].status, ORDER_BROKEN)

    def test_occlusion_breaks_and_restore_repairs(self):
        eng = self.make()
        eng.add_occlusion("X", 2, 2, 1, 2, 2)
        r = eng.place_presale_order("O1", "A", 1, 1, 1, 1)
        self.assertFalse(r["accepted"])
        affected = eng.remove_occlusion("X")
        row = next(x for x in affected["affected_orders"] if x["order_id"] == "O1")
        self.assertFalse(row["broken"])
        self.assertEqual(eng.s.orders["O1"].status, ORDER_LOCKED)

    def test_locked_trajectory_survives_unrelated_new_occlusion(self):
        eng = self.make(tracks=2)
        eng.place_presale_order("O1", "A", 1, 1, 1, 1, allowed_tracks=[1])
        # 字幕只遮轨 2，轨 1 的票不受影响。
        affected = eng.add_occlusion("X", 1, 3, 2, 1, 3)
        self.assertTrue(all(x["order_id"] != "O1"
                            for x in affected["affected_orders"]))
        self.assertEqual(eng.s.orders["O1"].status, ORDER_LOCKED)

    def test_started_instruction_is_immovable(self):
        eng = self.make(tracks=2)
        eng.place_presale_order("O1", "A", 1, 1, 1, 1, allowed_tracks=[1])
        eng.dispatch_presale("O1")
        p = Player(eng)
        p.claim()
        p.advance(1)  # 开播回条，轨迹在途
        # 加字幕遮轨 1，在途指令不能挪；轨 2 无票可救，O1 保持 PLAYING。
        eng.add_occlusion("X", 2, 2, 1, 1, 3)
        self.assertEqual(eng.s.orders["O1"].status, "PLAYING")


class TestPool(TempShow):
    def test_dispatch_only_after_real_progress(self):
        eng = self.make(mode="pool")
        eng.place_pool_order("O1", "A", 1, 1, 1, 1, 1)
        # 没有任何进度前，指令不得下发。
        self.assertEqual(
            [x for x in eng.s.outbox if not x.claimed], [])
        r = eng.progress(0)
        self.assertEqual(len(r["dispatched"]), 1)

    def test_track_switch_resolves_contention(self):
        eng = self.make(mode="pool", tracks=2)
        eng.place_pool_order("O1", "A", 2, 1, 1, 2, 1)
        eng.place_pool_order("O2", "B", 2, 1, 1, 2, 1)
        r = eng.progress(0)
        tracks = {x["order_id"]: x["track"] for x in r["dispatched"]}
        self.assertEqual(tracks, {"O1": 1, "O2": 2})

    def test_stop_keeps_debt_and_redelivers(self):
        eng = self.make(mode="pool", tracks=1)
        eng.place_pool_order("O1", "A", 1, 1, 1, 12, quantity=2)
        p = Player(eng)
        r = eng.progress(2)        # beat 2 派出，入口拍 3
        instr = r["dispatched"][0]["instr_id"]
        p.advance(3)               # 第 3 拍开播
        eng.stop_playback(instr, 4)
        self.assertEqual(eng.s.orders["O1"].quantity, 2)
        r = eng.progress(5)        # 重新派出，入口拍 6
        self.assertEqual(r["dispatched"][0]["start"], 6)
        p.advance(8)               # 6/7/8 滚完第一次
        self.assertEqual(eng.s.orders["O1"].quantity, 1)
        eng.progress(8)            # 同一拍续派第二次，入口拍 9
        p.advance(11)              # 9/10/11 滚完
        self.assertEqual(eng.s.orders["O1"].status, ORDER_SETTLED)

    def test_expired_window(self):
        eng = self.make(mode="pool")
        eng.place_pool_order("O1", "A", 1, 1, 1, 1, 1)
        eng.progress(0)  # 唯一三拍被占（同轨无位）——此处先派成功
        eng.place_pool_order("O2", "B", 1, 1, 1, 1, 1)
        r = eng.progress(1)
        self.assertEqual(eng.s.orders["O2"].status, ORDER_EXPIRED)
        self.assertTrue(any(x["order_id"] == "O2" for x in r["expired"]))


class TestReceiptsAndReplay(TempShow):
    def test_duplicate_receipt_idempotent_across_restart(self):
        d = os.path.join(self.tmp, "show-rc")
        eng = Engine(Store.create(d, "s", "screen", 3, 1, "pool"))
        eng.place_pool_order("O1", "A", 1, 1, 1, 3, 1)
        p = Player(eng)
        p.advance(0)
        p.advance(1)
        instr_id = next(iter(eng.s.instructions))
        receipt = eng.s.instructions[instr_id].receipt_id

        reopened = Engine(Store.open(d))
        # 跨重启重传同一张回条：两次都识别为重复，不产生副作用。
        first = reopened.receipt(receipt, instr_id, 1)
        second = reopened.receipt(receipt, instr_id, 1)
        self.assertTrue(first["duplicate"])
        self.assertTrue(second["duplicate"])
        # 重传不产生第二条指令、不重复结算。
        self.assertEqual(
            sum(1 for i in reopened.s.instructions.values()
                if i.status == "STARTED"), 1)

    def test_replay_matches_disk_state(self):
        d = os.path.join(self.tmp, "show-rp")
        eng = Engine(Store.create(d, "s", "screen", 3, 2, "pool"))
        eng.place_pool_order("O1", "A", 2, 1, 1, 4, 2)
        p = Player(eng)
        p.advance(0)
        p.advance(3)
        eng.place_pool_order("O2", "B", 1, 1, 4, 9, 1)
        p.advance(6)
        p.advance(10)
        replayed, _events, disk = replay_dir(d)
        self.assertEqual(replayed.to_dict(), disk.to_dict())

    def test_progress_cannot_go_backwards(self):
        eng = self.make(mode="pool")
        eng.progress(3)
        with self.assertRaises(ValueError):
            eng.progress(2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
