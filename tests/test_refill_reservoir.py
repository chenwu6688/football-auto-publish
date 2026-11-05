#!/usr/bin/env python3
"""蓄水池备货闸门（scripts/refill_reservoir.py）的单测。

2026-11-05 事故：noon 批次转正的两篇软性稿标题是「…还能找回吗？」「…谁才是你最强？」，
发布时间落在 verify_publish_gate 生效日（当天）之后，直接把这个硬项刷红。往上追是三层：

1. 备货 prompt 明写「疑问收尾」——等于在生产端批量制造空问标题；
2. 备货时调 `score_title_plan` **不传 recent_titles**，靠「14 天疑问收尾≤2 篇」判的
   规则永远不触发（分数看着及格，规则其实没跑）；
3. 转正时完全不过闸门，10 月的存量直接发出去。

三层现在都堵住了，这个文件守 1 和 2（3 在 tests/test_cover_card.py 与
tests/test_reservoir.py 里守）。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import refill_reservoir as rr  # noqa: E402


class TestPromptNoQuestionEnding(unittest.TestCase):
    def test_template_does_not_ask_for_question_ending(self):
        t = rr._TMPL
        self.assertNotIn("疑问收尾\"", t, "prompt 又在要求疑问收尾了")
        self.assertNotIn("26-30字,疑问收尾", t)
        self.assertIn("不要以疑问句收尾", t)
        self.assertIn("过程锚点", t)


class TestTitleGate(unittest.TestCase):
    def setUp(self):
        self._saved = rr._RECENT_CACHE
        rr._RECENT_CACHE = ["近 14 天已经发过的标题"]

    def tearDown(self):
        rr._RECENT_CACHE = self._saved

    def test_recent_titles_are_passed_to_scorer(self):
        """不传 recent_titles = 疑问收尾限额永不触发 = 假绿。"""
        import orchestrator
        seen = {}

        def fake(topic, title, ct, **kw):
            seen.update(kw)
            return 55, False, ["疑问收尾超额"]

        saved = orchestrator.score_title_plan
        orchestrator.score_title_plan = fake
        try:
            obj = {"title": "他到底图个啥？"}
            ok, hint = rr._title_gate(obj, "人物故事")
        finally:
            orchestrator.score_title_plan = saved
        self.assertIn("recent_titles", seen, "评分调用没带历史标题")
        self.assertEqual(seen["recent_titles"], rr._RECENT_CACHE)
        self.assertFalse(ok)
        self.assertIn("疑问句", hint)
        self.assertFalse(obj["_title_passed"])

    def test_pass_sets_flag(self):
        import orchestrator
        saved = orchestrator.score_title_plan
        orchestrator.score_title_plan = lambda *a, **k: (88, True, [])
        try:
            obj = {"title": "第94分钟绝杀，皇马3-1拿下德比"}
            ok, hint = rr._title_gate(obj, "热点球评")
        finally:
            orchestrator.score_title_plan = saved
        self.assertTrue(ok)
        self.assertEqual(hint, "")
        self.assertEqual(obj["_title_score"], 88)

    def test_scorer_crash_is_not_a_pass(self):
        import orchestrator
        saved = orchestrator.score_title_plan

        def boom(*a, **k):
            raise RuntimeError("炸了")

        orchestrator.score_title_plan = boom
        try:
            ok, hint = rr._title_gate({"title": "x"}, "人物故事")
        finally:
            orchestrator.score_title_plan = saved
        self.assertFalse(ok)
        self.assertIn("不合格", hint)


class TestDoNotStockBadTitle(unittest.TestCase):
    def test_main_skips_stocking_when_title_not_passed(self):
        import reservoir
        saved = {
            "_make_one": rr._make_one,
            "available": reservoir.available,
            "expire": reservoir.expire,
            "stock": reservoir.stock,
            "stats": reservoir.stats,
        }
        stocked = []
        rr._make_one = lambda angle, idx: {"title": "空问标题？", "content": "x" * 300,
                                           "_title_passed": False, "_title_score": 0}
        reservoir.available = lambda *a, **k: []
        reservoir.expire = lambda *a, **k: 0
        reservoir.stock = lambda art, **k: stocked.append(art) or "rsv-x"
        reservoir.stats = lambda *a, **k: {"available": 0}
        argv = sys.argv
        sys.argv = ["refill_reservoir.py", "--max-gen", "1"]
        try:
            rr.main()
        finally:
            sys.argv = argv
            rr._make_one = saved["_make_one"]
            reservoir.available = saved["available"]
            reservoir.expire = saved["expire"]
            reservoir.stock = saved["stock"]
            reservoir.stats = saved["stats"]
        self.assertEqual(stocked, [], "标题不过闸门却入库了 → 转正后必然违规发布")


if __name__ == "__main__":
    unittest.main()
