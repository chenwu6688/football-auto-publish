import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import reservoir


class TestReservoir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = reservoir.RESERVOIR_DIR
        self._orig_items = reservoir.ITEMS_PATH
        reservoir.RESERVOIR_DIR = self.tmp / "reservoir"
        reservoir.ITEMS_PATH = reservoir.RESERVOIR_DIR / "items.json"
        self.now = datetime(2026, 10, 10, 12, 0)

    def tearDown(self):
        reservoir.RESERVOIR_DIR = self._orig_dir
        reservoir.ITEMS_PATH = self._orig_items

    def test_stock_and_available(self):
        reservoir.stock({"title": "老将的最后一舞？", "content": "x" * 300,
                         "content_type": "人物故事"}, section="人物故事", now=self.now)
        self.assertEqual(len(reservoir.available(self.now)), 1)

    def test_fill_gap_fifo_and_used(self):
        for i in range(3):
            reservoir.stock({"title": f"t{i}", "content": "x" * 300}, now=self.now)
        picked = reservoir.fill_gap(2, today=self.now)
        self.assertEqual([p["title"] for p in picked], ["t0", "t1"])
        self.assertEqual(len(reservoir.available(self.now)), 1)
        # 再取只剩 1 条
        self.assertEqual(len(reservoir.fill_gap(5, today=self.now)), 1)
        self.assertEqual(len(reservoir.available(self.now)), 0)

    def test_expire(self):
        reservoir.stock({"title": "old", "content": "x" * 300}, ttl_days=1, now=self.now)
        later = self.now + timedelta(days=3)
        self.assertEqual(reservoir.expire(later), 1)
        self.assertEqual(reservoir.available(later), [])
        # 过期条目不会被 fill_gap 取用
        self.assertEqual(reservoir.fill_gap(1, today=later), [])

    def test_stats(self):
        reservoir.stock({"title": "a", "content": "x" * 300}, now=self.now)
        s = reservoir.stats(self.now)
        self.assertEqual(s["total"], 1)
        self.assertEqual(s["available"], 1)
        self.assertEqual(s["by_status"].get("stocked"), 1)

    def test_accept_gate_rejects_before_marking_used(self):
        """2026-11-05：蓄水池旧条目标题不过闸门 → 留池标 rejected，绝不发出去。"""
        reservoir.stock({"title": "老将还能踢几年？", "content": "x" * 300,
                         "content_type": "人物故事"}, now=self.now)
        reservoir.stock({"title": "皇马3-1巴萨，第94分钟绝杀", "content": "x" * 300,
                         "content_type": "热点球评"}, now=self.now)

        def accept(item):
            bad = item["article"]["title"].endswith("？")
            return (not bad), "标题以疑问收尾"

        picked = reservoir.fill_gap(2, today=self.now, accept=accept)
        self.assertEqual([p["title"] for p in picked], ["皇马3-1巴萨，第94分钟绝杀"])
        items = reservoir._load()["items"]
        rejected = [i for i in items if i.get("status") == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reject_reason"], "标题以疑问收尾")
        self.assertFalse(rejected[0].get("used_at"))
        # 被拒的条目不占配额、也不会被下一批重复重试
        self.assertEqual(reservoir.available(self.now), [])

    def test_accept_gate_exception_is_not_a_pass(self):
        reservoir.stock({"title": "x", "content": "x" * 300}, now=self.now)

        def boom(item):
            raise RuntimeError("闸门炸了")

        self.assertEqual(reservoir.fill_gap(1, today=self.now, accept=boom), [])
        self.assertEqual(reservoir._load()["items"][0]["status"], "rejected")

    def test_accept_gate_fills_remaining_slots_from_later_items(self):
        """拒了第一条要继续往后取，不能因为一条不合格就少补一篇。"""
        reservoir.stock({"title": "坏标题？", "content": "x" * 300}, now=self.now)
        reservoir.stock({"title": "好标题", "content": "x" * 300}, now=self.now)
        picked = reservoir.fill_gap(2, today=self.now,
                                    accept=lambda it: (not it["article"]["title"].endswith("？"),
                                                       "疑问收尾"))
        self.assertEqual([p["title"] for p in picked], ["好标题"])


if __name__ == "__main__":
    unittest.main()
