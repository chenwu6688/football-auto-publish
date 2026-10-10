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


if __name__ == "__main__":
    unittest.main()
