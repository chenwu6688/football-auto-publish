#!/usr/bin/env python3
r"""比分「口径归一」判定回归测试（计划 7.3 · 误杀率<5%）。

背景（真实事故）：2026-10-24 晚间批次自动熔断，原因是同一篇改写连续 5 次被判
「比分不一致: 比赛数据 蓉城 0-1 英博，但文中未出现该比分」，次日中午批次因此
直接失败（合规熔断中，发布已暂停）。

根因不是数据错，也不是文章真的没写比分，而是**比分口径只认一种写法**：
三个判定点各写各的正则，且只认 ASCII 的「-」「:」
  - check_rewrite_fidelity        `(\d+)[:-](\d+)`
  - validate_article_vs_match_data `(\d+)\s*[-–:]\s*(\d+)`
  - check_consistency_plan        `(\d{1,2})[-:：](\d{1,2})`
中文正文写「0比1」「0：1」「0—1」「０－１」全部落空 → 误判成「未出现该比分」。

本组测试锁住三件事：
  1. 等价写法必须放行（比/：/—/全角/主客颠倒）；
  2. 真不一致必须拦截（0-1 的数据写成 2-0 不许放）；
  3. 日期/时间/轮次不得被当成比分（既不能误放行，也不能误判成卡外比分）。
"""

import tempfile
import unittest
from pathlib import Path

import orchestrator as o


def _mk_fixture(home, away, hg, ag, goals=None):
    return {"home_team": home, "away_team": away, "home_score": hg, "away_score": ag,
            "goals": goals or []}


class _LedgerIsolated(unittest.TestCase):
    """check_consistency_plan 会写一致性台账，测试必须隔离路径。"""

    def setUp(self):
        import consistency_stats as cs
        self._d = tempfile.TemporaryDirectory()
        self._s, self._l = cs.STATS_PATH, cs.LABELS_PATH
        cs.STATS_PATH = Path(self._d.name) / "stats.json"
        cs.LABELS_PATH = Path(self._d.name) / "labels.json"

    def tearDown(self):
        import consistency_stats as cs
        cs.STATS_PATH, cs.LABELS_PATH = self._s, self._l
        self._d.cleanup()


class TestExtractScores(unittest.TestCase):
    """抽取器本身：等价写法归一，日期/时间不算比分。"""

    def test_ascii_and_fullwidth(self):
        self.assertEqual(o._extract_scores("蓉城0-1英博"), [(0, 1)])
        self.assertEqual(o._extract_scores("蓉城０－１英博"), [(0, 1)])

    def test_chinese_bi_and_colons(self):
        self.assertEqual(o._extract_scores("蓉城0比1英博"), [(0, 1)])
        self.assertEqual(o._extract_scores("蓉城0：1英博"), [(0, 1)])
        self.assertEqual(o._extract_scores("蓉城0—1英博"), [(0, 1)])

    def test_spaces_around_score(self):
        self.assertEqual(o._extract_scores("比分 0 比 1"), [(0, 1)])

    def test_date_time_round_are_not_scores(self):
        self.assertEqual(o._extract_scores("2026-10-24 15:30 第27轮开球"), [])
        self.assertEqual(o._extract_scores("比赛在2026-10-1结束"), [])
        self.assertEqual(o._extract_scores("26-10-24 的记录"), [])

    def test_multi_goal_still_parsed(self):
        self.assertEqual(o._extract_scores("武汉三镇3-5输球降级"), [(3, 5)])

    def test_dedup_keeps_order(self):
        self.assertEqual(o._extract_scores("0-1，又是0比1"), [(0, 1)])


class TestScoreCaliberInFactCheck(unittest.TestCase):
    """validate_article_vs_match_data：等价写法放行，真不一致拦截。"""

    def _check(self, content, hg=0, ag=1):
        fx = _mk_fixture("蓉城", "英博", hg, ag)
        return o.validate_article_vs_match_data(fx, {"title": "蓉城输球", "content": content})

    def test_equivalent_forms_pass(self):
        for c in ("蓉城主场0-1不敌英博。", "蓉城0比1负于英博。", "蓉城0：1负于英博。",
                  "蓉城0—1英博。", "成都蓉城０－１大连英博。", "英博1-0拿下蓉城。"):
            ok, issues = self._check(c)
            self.assertTrue(ok, f"应放行但被拦：{c} → {issues}")

    def test_real_mismatch_blocked(self):
        for c in ("蓉城主场0比2不敌英博。", "蓉城2-0英博。", "蓉城大胜英博。"):
            ok, issues = self._check(c)
            self.assertFalse(ok, f"应拦截却放行：{c}")
            self.assertTrue(any("比分不一致" in i for i in issues))

    def test_missing_score_blocked_but_date_not_counted(self):
        # 只有日期/时间、没有比分 → 仍须拦截（日期不能被当成比分放行）
        ok, issues = self._check("2026-10-24 15:30 蓉城对英博，赛前前瞻。")
        self.assertFalse(ok)
        self.assertTrue(any("比分不一致" in i for i in issues))

    def test_scoreless_forward_look_not_blocked_when_no_result(self):
        # 无赛果数据（前瞻）不强制比分
        fx = _mk_fixture("蓉城", "英博", None, None)
        ok, _ = o.validate_article_vs_match_data(
            fx, {"title": "前瞻", "content": "蓉城主场迎战英博，赛前氛围热烈。"})
        self.assertTrue(ok)


class TestScoreCaliberInRewriteFidelity(unittest.TestCase):
    """check_rewrite_fidelity：只在「写了比分但不对」时拦。"""

    def test_equivalent_form_passes(self):
        src = _mk_fixture("蓉城", "英博", 0, 1)
        ok, issues = o.check_rewrite_fidelity(src, {"title": "t", "content": "蓉城0比1英博。"})
        self.assertTrue(ok, issues)

    def test_wrong_score_blocked(self):
        src = _mk_fixture("蓉城", "英博", 0, 1)
        ok, issues = o.check_rewrite_fidelity(src, {"title": "t", "content": "蓉城0比3英博。"})
        self.assertFalse(ok)
        self.assertTrue(any("比分不一致" in i for i in issues))

    def test_no_score_not_blocked_here(self):
        # 本层只管「不矛盾」，是否必须写比分由事实层校验决定
        src = _mk_fixture("蓉城", "英博", 0, 1)
        ok, issues = o.check_rewrite_fidelity(
            src, {"title": "t", "content": "蓉城输给英博，领先优势缩水。"})
        self.assertTrue(ok, issues)


class TestScoreCaliberInConsistencyPlan(_LedgerIsolated):
    """check_consistency_plan：卡外比分判定用同一口径。"""

    def test_equivalent_form_is_not_stray(self):
        ok, issues = o.check_consistency_plan(
            {"title": "蓉城0比1英博", "content": "蓉城0比1不敌英博。"},
            {"allowed_scores": ["0-1"], "allowed_teams": ["蓉城", "英博"]},
            blocking=True)
        self.assertFalse(any("卡外" in i for i in issues), issues)

    def test_real_stray_score_still_caught(self):
        ok, issues = o.check_consistency_plan(
            {"title": "蓉城3比2英博", "content": "蓉城3比2绝杀英博。"},
            {"allowed_scores": ["0-1"], "allowed_teams": ["蓉城", "英博"]},
            blocking=True)
        self.assertTrue(any("卡外" in i for i in issues), issues)


if __name__ == "__main__":
    unittest.main()
