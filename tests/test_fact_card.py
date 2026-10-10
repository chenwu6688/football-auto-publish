"""事实卡结构化落库（计划 6.1 / 12.2）单元测试。

覆盖：三级可信度、生命周期三态、6.1 字段完整性、按 id 合并且来源合并可升级可信度。
"""
import sys
import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fact_card as fc
from utils import CST

NOW = datetime(2026, 10, 10, 20, 0, tzinfo=CST)


class TestConfidence(unittest.TestCase):
    def test_three_levels(self):
        self.assertEqual(fc.derive_confidence([]), fc.CONF_RUMOR)
        self.assertEqual(fc.derive_confidence([{"来源名": "zhibo8"}]), fc.CONF_SINGLE)
        self.assertEqual(
            fc.derive_confidence([{"来源名": "zhibo8"}, {"来源名": "dongqiudi"}]),
            fc.CONF_CONFIRMED)

    def test_same_source_not_counted_twice(self):
        self.assertEqual(
            fc.derive_confidence([{"来源名": "zhibo8"}, {"来源名": "zhibo8"}]),
            fc.CONF_SINGLE)


class TestLifecycle(unittest.TestCase):
    def _card(self, occurred):
        return fc.build_fact_card("曼城", "比赛结果", "3-1", occurred,
                                  [{"来源名": "zhibo8"}], now=NOW)

    def test_ongoing(self):
        self.assertEqual(self._card("2026-10-09").get("生命周期"), fc.LC_ONGOING)

    def test_settled(self):
        self.assertEqual(self._card("2026-09-25").get("生命周期"), fc.LC_SETTLED)

    def test_expired(self):
        self.assertEqual(self._card("2026-08-01").get("生命周期"), fc.LC_EXPIRED)

    def test_future_is_ongoing(self):
        self.assertEqual(self._card("2026-10-20").get("生命周期"), fc.LC_ONGOING)


class TestSchema(unittest.TestCase):
    def test_plan_6_1_fields_present(self):
        card = fc.build_fact_card("曼城", "转会", "1.5亿欧元", "2026-10-10",
                                  [{"来源名": "zhibo8", "地址": "http://z"}], now=NOW)
        for k in ("id", "主体", "动作", "数值", "发生时间", "来源列表", "可信度", "可用角度"):
            self.assertIn(k, card, f"缺字段 {k}")
        self.assertTrue(card["id"])
        self.assertTrue(card["可用角度"])


class TestUpsert(unittest.TestCase):
    def test_dedupe_and_upgrade_confidence(self):
        store = {"cards": []}
        c1 = fc.build_fact_card("哈兰德", "转会", "1.5亿", "2026-10-10",
                                [{"来源名": "zhibo8"}], now=NOW)
        store, added, updated = fc.upsert_cards(store, [c1], NOW)
        self.assertEqual((added, updated), (1, 0))
        self.assertEqual(store["cards"][0]["可信度"], fc.CONF_SINGLE)
        # 第二条独立来源 → 合并后升为「已确认」
        c2 = fc.build_fact_card("哈兰德", "转会", "1.5亿", "2026-10-10",
                                [{"来源名": "dongqiudi"}], now=NOW)
        self.assertEqual(c1["id"], c2["id"])
        store, added, updated = fc.upsert_cards(store, [c2], NOW)
        self.assertEqual((added, updated), (0, 1))
        self.assertEqual(len(store["cards"]), 1)
        self.assertEqual(store["cards"][0]["可信度"], fc.CONF_CONFIRMED)


class TestPersist(unittest.TestCase):
    def test_compile_and_persist_writes_file(self):
        md = {"all_fixtures": [
            {"home_team": "曼城", "away_team": "利物浦", "home_score": 3, "away_score": 1,
             "source": "zhibo8", "source_url": "http://z", "utc_date": "2026-10-10",
             "data_confidence": "high"}],
            "news_articles": [
            {"title": "曼城有意引进新前锋", "source": "dongqiudi", "url": "http://d",
             "published_at": "2026-10-10 09:00"}]}
        p = tempfile.mktemp(suffix=".json")
        store, stats = fc.compile_and_persist(md, date_str="2026-10-10", store_path=p, now=NOW)
        self.assertTrue(os.path.exists(p))
        self.assertGreaterEqual(stats["total"], 2)
        on_disk = json.loads(Path(p).read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk["cards"]), stats["total"])


if __name__ == "__main__":
    unittest.main()
