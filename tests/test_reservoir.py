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


if __name__ == "__main__":
    unittest.main()
