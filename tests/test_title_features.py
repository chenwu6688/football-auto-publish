# -*- coding: utf-8 -*-
"""标题特征六维与对标汇总的单测（2026-11-01 头部账号对标）。

所有用例的标题与阅读量都来自当次真实抓取，不是编的。
改词表必须同步改这里 —— 否则口径漂移又没人发现。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import title_features as tf


class TestAnchor(unittest.TestCase):
    """过程锚点：只认「读者不知道结局」的过程，不认结果陈述。"""

    def test_let_three_chase_four(self):
        """让3追4 —— 头部 4.2 万播放爆款的核心结构。"""
        f = tf.extract("让3追4！拜仁史诗级大逆转，30分钟连轰4球，凯恩替补完成绝杀！")
        self.assertTrue(f.has_anchor)
        self.assertIn("让球逆转", f.anchor_kinds)
        self.assertIn("追平/逆转", f.anchor_kinds)

    def test_minute_marker(self):
        """第94分钟 + 读秒绝杀 = 两个锚点。"""
        f = tf.extract("第94分钟丢球，阿森纳遭读秒绝杀！领先只剩3分，曼城看到希望！")
        self.assertTrue(f.has_anchor)
        self.assertGreaterEqual(f.anchor_count, 2)

    def test_rounds_without_win(self):
        f = tf.extract("主场0-3大败，利物浦彻底崩盘！近7轮输了6场，球迷第80分钟退场")
        self.assertIn("N轮不胜/连败", f.anchor_kinds)

    def test_result_statement_is_not_anchor(self):
        """「终结英格兰25年奖项荒」是结果陈述，只有 4 阅读，不许算锚点。

        这条是自检逼出来的：旧词表把「终结…荒」当锚点，导致一个 4 阅读的
        哑弹被标成"有锚点"，会让覆盖率指标整体虚高。
        """
        f = tf.extract("2026金球奖揭晓：凯恩凭73球夺魁，终结英格兰25年奖项荒")
        self.assertFalse(f.has_anchor, f"不应命中锚点，实际命中 {f.anchor_kinds}")

    def test_bookish_has_no_anchor(self):
        f = tf.extract("红黑军团降服蓝黑军团：莫雷拉闪耀圣西罗，国米八战德比难求一胜")
        self.assertFalse(f.has_anchor)
        self.assertTrue(f.is_bookish)

    def test_our_own_titles_have_no_anchor(self):
        """我方 76 篇标题的真实样本 —— 全是空问句，一个锚点都没有。"""
        for t in (
            "当年那个追风少年曲波，如今在基层默默带娃，他到底图个啥？",
            "马拉多纳的上帝之手，英格兰到底冤不冤呢",
            "米兰德比谁先崩？国米22分压着米兰，两位主帅的底牌竟然差在这？",
        ):
            with self.subTest(t=t):
                self.assertFalse(tf.extract(t).has_anchor)


class TestBucket(unittest.TestCase):
    """档位划分必须与 2026-11-01 报告里的表一一对应。"""

    def test_buckets_match_report(self):
        cases = {
            "让3追4！拜仁史诗级大逆转，30分钟连轰4球，凯恩替补完成绝杀！": "锚点+感叹",
            "红黑军团降服蓝黑军团：莫雷拉闪耀圣西罗，国米八战德比难求一胜": "书面语",
            "德国杯：拜仁6-1大胜马格德堡，多点开花挺进1/8决赛": "数字无感叹无情绪",
            "马拉多纳的上帝之手，英格兰到底冤不冤呢": "三者都没有",
        }
        for title, want in cases.items():
            with self.subTest(title=title[:20]):
                self.assertEqual(tf.bucket(title), want)

    def test_bucket_order_covers_all(self):
        """BUCKET_ORDER 必须能装下 bucket() 的所有返回值，否则报表会漏行。"""
        samples = [
            "让3追4！拜仁史诗级大逆转！", "第94分钟丢球，阿森纳遭绝杀",
            "主场0-3大败！彻底崩盘", "3-1赢了，进了4个球",
            "太惨了！", "6-1大胜马格德堡", "红黑军团降服蓝黑军团", "他就是个普通人",
        ]
        for s in samples:
            with self.subTest(s=s):
                self.assertIn(tf.bucket(s), tf.BUCKET_ORDER)

    def test_empty_title_safe(self):
        f = tf.extract("")
        self.assertEqual(f.han_len, 0)
        self.assertFalse(f.has_anchor)
        self.assertEqual(tf.bucket(""), "三者都没有")
        # None 也要能扛住（台账里有脏数据）
        self.assertEqual(tf.extract(None).han_len, 0)


class TestSummarize(unittest.TestCase):
    """汇总函数：样本不足必须标 thin，不许拿 2 条样本当结论。"""

    def _rows(self):
        return [
            {"title": "让3追4！拜仁史诗级大逆转，30分钟连轰4球！", "views": 18000},
            {"title": "第94分钟丢球，阿森纳遭读秒绝杀！领先只剩3分！", "views": 10000},
            {"title": "红黑军团降服蓝黑军团：莫雷拉闪耀圣西罗", "views": 8},
            {"title": "德国杯：拜仁6-1大胜马格德堡，多点开花挺进", "views": 11},
            {"title": "他就是个普通人", "views": 76},
            {"title": "他说了两句话", "views": 74},
        ]

    def test_basic_shape(self):
        r = tf.summarize(self._rows())
        self.assertEqual(r["n"], 6)
        self.assertIsNotNone(r["overall"]["median"])
        self.assertIsNotNone(r["anchor_coverage"])
        self.assertIsNotNone(r["question_rate"])

    def test_anchor_group_higher(self):
        """锚点组的中位必须高于无锚点组 —— 这是整个改进计划的前提。"""
        r = tf.summarize(self._rows())
        hit = r["features"]["has_anchor"]
        self.assertEqual(hit["n"], 2)
        self.assertGreater(hit["median"], hit["miss_median"])

    def test_thin_flag(self):
        r = tf.summarize(self._rows())
        self.assertTrue(r["features"]["has_anchor"]["thin"], "n=2 必须标 thin")

    def test_question_rate(self):
        rows = [{"title": "这事到底图个啥？", "views": 1},
                {"title": "他真的行吗？", "views": 2},
                {"title": "6-1大胜马格德堡", "views": 3}]
        r = tf.summarize(rows)
        self.assertAlmostEqual(r["question_rate"], 66.7, places=1)

    def test_missing_views_not_counted_as_zero(self):
        """没有阅读数的篇目不许当 0 计入分母 —— 否则中位数被压低（口径造假）。"""
        rows = [{"title": "让3追4！大逆转绝杀！"}, {"title": "第94分钟丢球，遭读秒绝杀", "views": 100}]
        r = tf.summarize(rows)
        self.assertEqual(r["n"], 2)              # 标题都算
        self.assertEqual(r["overall"]["n"], 1)   # 但只有 1 条有阅读数

    def test_empty_returns_zero_shape(self):
        r = tf.summarize([])
        self.assertEqual(r["n"], 0)
        self.assertEqual(r["buckets"], {})
        self.assertIsNone(r["anchor_coverage"])


class TestSelftest(unittest.TestCase):
    def test_module_selftest_all_pass(self):
        bad = [(n, ok) for n, ok in tf.selftest() if not ok]
        self.assertEqual(bad, [], f"模块自检失败：{bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
