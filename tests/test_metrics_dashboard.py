import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import scripts.build_metrics_dashboard as m


class TestMetricsDashboard(unittest.TestCase):
    """计划第十章 · 验收指标看板计算。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = m.OUTPUT_DIR
        m.OUTPUT_DIR = self.tmp
        # 一天，3 篇文章，2 篇有溯源，performance 覆盖 2 篇
        d = self.tmp / "2026-10-10"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(json.dumps({
            "date": "2026-10-10",
            "batches_completed": ["morning", "noon", "evening"],
            "articles": [
                {"index": 1, "title": "A", "column_name": "热点球评",
                 "sources_used": [{"url": "u"}], "content_type": "热点球评"},
                {"index": 2, "title": "B", "column_name": "人物故事",
                 "source_post": "src", "content_type": "人物故事"},
                {"index": 3, "title": "C", "column_name": "热点球评",
                 "content_type": "热点球评"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (self.tmp / "performance_log.json").write_text(json.dumps({
            "articles": {
                "2026-10-10/article-1": {"reads": 100, "retention_rate": 0.4, "new_followers": 5},
                "2026-10-10/article-2": {"reads": 300, "retention_rate": 0.6, "new_followers": 3},
            }
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        m.OUTPUT_DIR = self._orig

    def test_collect_growth_and_supply(self):
        now = datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        data = m.collect(1, now=now)
        g, s = data["growth"], data["supply"]
        self.assertEqual(g["篇均阅读"], 200.0)         # (100+300)/2
        self.assertEqual(g["平均完成率"], 50.0)        # (0.4+0.6)/2*100
        self.assertEqual(g["日均涨粉"], 8.0)           # 5+3
        self.assertEqual(s["总篇数"], 3)
        self.assertEqual(s["批次完成率"], 100.0)
        self.assertAlmostEqual(s["溯源覆盖率"], 66.7, places=1)  # 2/3
        self.assertEqual(s["板块分布"]["热点球评"], 2)

    def test_report_marks_missing_growth_honestly(self):
        now = datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        # 无 performance 数据时应为 None（不冒充 0）
        data = m.collect(1, now=now)
        data["growth"]["整体点击率"] = None
        comp = {"suspended": False, "false_content_count": 0}
        rep = m.build_report(data, comp, 1, now=now)
        rows = dict((r[0], r[1]) for r in rep["growth_rows"])
        self.assertIn("待接入", rows["整体点击率"])
        self.assertEqual(rows["不实违规次数"], 0)
        md = m.render_markdown(rep)
        self.assertIn("合规红线", md)

    def test_hot_signals_block_missing_file_is_none(self):
        # 无 RedFox 信号表 → 全 None（看板显示「待接入」，绝不造假 0）
        orig = m.SIGNALS_DIR
        m.SIGNALS_DIR = self.tmp / "no_signals"
        try:
            hot = m.hot_signals_block()
        finally:
            m.SIGNALS_DIR = orig
        self.assertIsNone(hot["命中词数"])
        self.assertIsNone(hot["生成时间"])
        rep = m.build_report({"growth": {"篇均阅读": None, "平均完成率": None,
                                         "整体点击率": None, "粉丝阅读占比": None,
                                         "日均涨粉": None, "_data_points": 0},
                              "supply": {"覆盖天数": 0, "总篇数": 0, "日均篇数": 0,
                                         "批次完成率": 0, "溯源覆盖率": 0,
                                         "板块分布": {}, "预测类剔除": 0},
                              "hot": hot},
                             {"suspended": False, "false_content_count": 0}, 1)
        self.assertIn("暂无信号表", m.render_markdown(rep))
        self.assertIn("暂无信号表", m.render_html(rep))


if __name__ == "__main__":
    unittest.main()
