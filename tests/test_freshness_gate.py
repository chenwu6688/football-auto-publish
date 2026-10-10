"""新闻流 72 小时时效闸门（计划 4.1 / 7.2）单元测试。

回归防护：
- 扣分项「发布已过时效内容 -10」的唯一硬性对冲手段就是这道闸门；
- 有据可查的过期素材必须被剔除（URL 日期 / 显式字段 / 文本内嵌时间三条路径）；
- 无法判定时间的条目保守保留（宁可少拦，不可把流水线掐死），但需计入 unknown。
"""
import sys
import os
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import parse_news_time, filter_fresh_news, NEWS_MAX_AGE_HOURS, CST


NOW = datetime(2026, 10, 10, 20, 0, tzinfo=CST)


class TestParseNewsTime(unittest.TestCase):
    def test_url_date(self):
        it = {"url": "https://news.zhibo8.com/zuqiu/2026-10-09/abc.htm"}
        dt = parse_news_time(it, NOW)
        self.assertEqual(dt.strftime("%Y-%m-%d"), "2026-10-09")

    def test_explicit_public_time(self):
        it = {"publicTime": "2026-10-10 09:15:00"}
        dt = parse_news_time(it, NOW)
        self.assertEqual(dt.strftime("%Y-%m-%d %H:%M"), "2026-10-10 09:15")

    def test_embedded_mmdd(self):
        it = {"text": "足球某队：xxx五洲世界杯10-10 09:15"}
        dt = parse_news_time(it, NOW)
        self.assertEqual(dt.strftime("%Y-%m-%d %H:%M"), "2026-10-10 09:15")

    def test_relative_hours(self):
        it = {"title": "3小时前"}
        dt = parse_news_time(it, NOW)
        self.assertEqual((NOW - dt).total_seconds(), 3 * 3600)

    def test_unknown(self):
        self.assertIsNone(parse_news_time({"title": "没有任何时间"}, NOW))


class TestFilterFreshNews(unittest.TestCase):
    def test_drops_stale_keeps_fresh(self):
        items = [
            {"url": "https://news.zhibo8.com/zuqiu/2026-10-10/new.htm"},   # 今天
            {"publicTime": "2026-10-08 12:00:00"},                          # ~56h，保留
            {"publicTime": "2026-09-20 08:00:00"},                          # ~490h，剔除
            {"text": "足球：xxx09-30 11:15"},                               # 10 天前，剔除
            {"title": "无时间"},                                            # unknown，保留
        ]
        kept, dropped, unknown = filter_fresh_news(items, NEWS_MAX_AGE_HOURS, NOW, label="t")
        self.assertEqual(len(dropped), 2)
        self.assertEqual(unknown, 1)
        self.assertEqual(len(kept), 3)

    def test_marks_freshness(self):
        items = [{"publicTime": "2026-10-10 09:00:00"}]
        kept, _, _ = filter_fresh_news(items, NEWS_MAX_AGE_HOURS, NOW, label="t")
        self.assertEqual(kept[0]["_freshness"], "fresh")
        self.assertLess(kept[0]["_age_hours"], 72)

    def test_boundary_72h(self):
        # 恰在 72h 边界内（71h）保留
        within = {"publicTime": (NOW - timedelta(hours=71)).strftime("%Y-%m-%d %H:%M:%S")}
        kept, dropped, _ = filter_fresh_news([within], NEWS_MAX_AGE_HOURS, NOW, label="t")
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 0)


if __name__ == "__main__":
    unittest.main()
