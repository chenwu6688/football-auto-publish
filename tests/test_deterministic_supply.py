"""一级·确定性节点供给单测（计划 12.1）。"""

import json
import tempfile
import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import deterministic_supply as ds

CST = timezone(timedelta(hours=8))

CAL = {
    "season": "test",
    "nodes": [
        {"id": "n_close", "type": "转会窗截止日", "label": "冬窗关闭",
         "date": "2027-02-02", "lead_days": 7, "column": "转会动态", "awareness": "A",
         "template": "冬窗只剩{days}天，谁最着急？"},
        {"id": "n_final", "type": "赛季节点", "label": "欧冠决赛",
         "date": "2027-05-29", "lead_days": 7, "column": "战术榜单", "awareness": "A",
         "template": "欧冠决赛还有{days}天"},
        {"id": "n_far", "type": "赛季节点", "label": "很远的节点",
         "date": "2030-01-01", "lead_days": 7, "column": "战术榜单", "awareness": "C",
         "template": "还有{days}天"},
    ],
}


class TestBuildNodeTopics(unittest.TestCase):
    def test_window_filter(self):
        # 2027-02-01 距冬窗关闭 1 天 → 命中；欧冠决赛 5-29 太远 → 不命中
        topics = ds.build_node_topics(date(2027, 2, 1), horizon_days=7, calendar=CAL)
        ids = [t["node_id"] for t in topics]
        self.assertIn("n_close", ids)
        self.assertNotIn("n_final", ids)
        self.assertNotIn("n_far", ids)

    def test_horizon_boundary(self):
        # 预生成窗口 = node_date ∈ [today, today+horizon]；lead 只决定可发布窗口
        # 2027-01-26 距冬窗关闭(02-02) 7 天 → 命中（horizon=7 边界）
        t0 = ds.build_node_topics(date(2027, 1, 26), horizon_days=7, calendar=CAL)
        self.assertIn("n_close", [t["node_id"] for t in t0])
        # 2027-01-25 距 02-02 为 8 天 → 超出预生成窗口
        t1 = ds.build_node_topics(date(2027, 1, 25), horizon_days=7, calendar=CAL)
        self.assertNotIn("n_close", [t["node_id"] for t in t1])

    def test_template_and_fields(self):
        topics = ds.build_node_topics(date(2027, 1, 30), horizon_days=7, calendar=CAL)
        t = next(x for x in topics if x["node_id"] == "n_close")
        self.assertEqual(t["days_to"], 3)
        self.assertIn("3", t["conflict"])
        self.assertEqual(t["source_layer"], "确定性")
        self.assertEqual(t["板块"], "转会动态")
        self.assertEqual(t["publish_by"], "2027-02-02")
        self.assertEqual(t["publish_from"], "2027-01-26")

    def test_node_date_passed_excluded(self):
        topics = ds.build_node_topics(date(2027, 2, 3), horizon_days=7, calendar=CAL)
        self.assertNotIn("n_close", [t["node_id"] for t in topics])


class TestStoreLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._p1 = mock.patch.object(ds, "STORE_DIR", self.tmp)
        self._p2 = mock.patch.object(ds, "STORE_PATH", self.tmp / "items.json")
        self._p1.start(); self._p2.start()

    def tearDown(self):
        self._p1.stop(); self._p2.stop()

    def _topic(self, node_id="n_close", pb="2027-02-02", pf="2027-01-26"):
        return {"topic_id": f"node-{node_id}-{pb}", "node_id": node_id,
                "node_type": "转会窗截止日", "node_date": pb,
                "publish_from": pf, "publish_by": pb, "板块": "转会动态"}

    def test_stock_available_fill(self):
        rid = ds.stock({"title": "冬窗倒计时", "content": "x" * 300}, self._topic())
        avail = ds.available(date_str="2027-02-01")
        self.assertEqual(len(avail), 1)
        picked = ds.fill_gap(1, date_str="2027-02-01")
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["id"], rid)
        # 取用后不再可用
        self.assertEqual(ds.available(date_str="2027-02-01"), [])

    def test_available_outside_window(self):
        ds.stock({"title": "冬窗倒计时", "content": "x" * 300}, self._topic())
        # 早于 publish_from
        self.assertEqual(ds.available(date_str="2027-01-01"), [])
        # 晚于 publish_by
        self.assertEqual(ds.available(date_str="2027-02-05"), [])

    def test_expire_marks_past(self):
        ds.stock({"title": "冬窗倒计时", "content": "x" * 300}, self._topic())
        n = ds.expire(now=datetime(2027, 2, 10, tzinfo=CST))
        self.assertEqual(n, 1)
        self.assertEqual(ds.available(date_str="2027-02-01"), [])

    def test_pending_excludes_stocked(self):
        cal = CAL
        before = ds.pending_nodes(date(2027, 2, 1), horizon_days=7, calendar=cal)
        self.assertIn("n_close", [t["node_id"] for t in before])
        ds.stock({"title": "x", "content": "y" * 300}, self._topic())
        after = ds.pending_nodes(date(2027, 2, 1), horizon_days=7, calendar=cal)
        self.assertNotIn("n_close", [t["node_id"] for t in after])


class TestCalendarIO(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        cal = ds.load_calendar(Path(tempfile.mkdtemp()) / "nope.yaml")
        self.assertEqual(cal["nodes"], [])

    def test_real_calendar_loads(self):
        cal = ds.load_calendar()
        self.assertTrue(len(cal.get("nodes", [])) >= 3)


if __name__ == "__main__":
    unittest.main()
