#!/usr/bin/env python3
"""队名「别名感知」判定回归测试（计划 7.3）。

背景（真实事故）：2026-10-12 自动发布被第十章熔断，原因是同批次一致性校验连续
打回 5 次，末次提示「缺少客队名: 拜仁慕尼黑」。根因是事实校验按**字面包含**判定队名
——事实层规范名是「拜仁」（entity_map name_zh），而来源/正文用全称「拜仁慕尼黑」，
于是同义写法被当成「缺队名」。本组测试同时锁住两个误杀源：

  1. 同队不同写法（拜仁 / 拜仁慕尼黑、巴黎 / 巴黎圣日耳曼）；
  2. 子串串味（正文「国际米兰」中的「米兰」被当成另一队）。

并确保**不放松**：真缺队名、真出现卡外球队仍然拦截。
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

import orchestrator as o


class _LedgerIsolated(unittest.TestCase):
    """check_consistency_plan 会写一致性台账（计划 7.3），测试须隔离路径。"""

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


def _mk_fixture(home, away, hg, ag, goals=None):
    return {"home_team": home, "away_team": away, "home_score": hg, "away_score": ag,
            "goals": goals or []}


class TestTeamForms(unittest.TestCase):
    def test_aliases_share_one_entity(self):
        forms = o._team_forms("拜仁慕尼黑")
        self.assertIn("拜仁", forms)
        self.assertIn("拜仁慕尼黑", forms)

    def test_short_name_expands_to_full(self):
        self.assertIn("拜仁慕尼黑", o._team_forms("拜仁"))

    def test_milan_alias_belongs_to_acmilan_only(self):
        # entity_map：AC米兰别名含「米兰」，国际米兰不含
        self.assertIn("米兰", o._team_forms("AC米兰"))
        self.assertNotIn("米兰", o._team_forms("国际米兰"))

    def test_same_team_and_different_team(self):
        self.assertTrue(o._same_team("拜仁", "拜仁慕尼黑"))
        self.assertTrue(o._same_team("巴黎", "巴黎圣日耳曼"))
        self.assertFalse(o._same_team("国际米兰", "AC米兰"))
        self.assertFalse(o._same_team("拜仁", "多特"))

    def test_unknown_team_degrades_to_itself(self):
        self.assertEqual(o._team_forms("某不存在的球队"), frozenset({"某不存在的球队"}))
        self.assertEqual(o._team_forms(""), frozenset())
        self.assertEqual(o._team_forms(None), frozenset())


class TestMatchDataValidationAliasAware(unittest.TestCase):
    """validate_article_vs_match_data（事实校验）."""

    def test_full_name_fixture_short_name_article_passes(self):
        # 事故复现用例：修复前报「缺少客队名: 拜仁慕尼黑」
        fx = _mk_fixture("多特", "拜仁慕尼黑", 1, 2)
        art = {"title": "拜仁客场2-1拿下多特",
               "content": "拜仁在客场以2-1击败多特，全取三分。"}
        passed, issues = o.validate_article_vs_match_data(fx, art)
        self.assertTrue(passed, f"同义写法不应误杀，却报: {issues}")

    def test_short_name_fixture_full_name_article_passes(self):
        fx = _mk_fixture("多特", "拜仁", 1, 2)
        art = {"title": "拜仁慕尼黑客场取胜", "content": "拜仁慕尼黑2-1多特。"}
        passed, issues = o.validate_article_vs_match_data(fx, art)
        self.assertTrue(passed, f"反向写法也不应误杀，却报: {issues}")

    def test_title_only_mention_counts(self):
        fx = _mk_fixture("巴黎圣日耳曼", "马赛", 2, 0)
        art = {"title": "巴黎2-0马赛", "content": "主队2-0取胜。"}
        passed, issues = o.validate_article_vs_match_data(fx, art)
        self.assertTrue(passed, f"标题命中即算提及，却报: {issues}")

    def test_genuinely_missing_away_team_still_blocked(self):
        # 不放松：正文完全没提客队（任何写法都没写）
        fx = _mk_fixture("曼城", "曼联", 1, 0)
        art = {"title": "曼城主场取胜", "content": "曼城主场取胜。"}
        passed, issues = o.validate_article_vs_match_data(fx, art)
        self.assertFalse(passed)
        self.assertTrue(any("缺少客队名" in i for i in issues), issues)

    def test_other_club_does_not_satisfy_requirement(self):
        fx = _mk_fixture("多特", "拜仁慕尼黑", 1, 2)
        art = {"title": "多特1-2落败", "content": "多特1-2不敌勒沃库森。"}
        passed, issues = o.validate_article_vs_match_data(fx, art)
        self.assertFalse(passed)
        self.assertTrue(any("缺少客队名: 拜仁慕尼黑" in i for i in issues), issues)


class TestConsistencyPlanAliasAware(_LedgerIsolated):
    """check_consistency_plan（事实卡实体校验）."""

    def test_allowed_team_hit_via_alias(self):
        # 正文只提到事实卡内的球队（无卡外知名球队），别名写法必须算命中
        art = {"title": "拜仁慕尼黑客场取胜", "content": "拜仁慕尼黑2-1拿下对手，拜仁全场压制。"}
        ok, issues = o.check_consistency_plan(art, {"allowed_teams": ["拜仁"]}, blocking=True)
        self.assertTrue(ok, f"别名命中不应报未提及，却报: {issues}")

    def test_substring_collision_not_flagged(self):
        # 反向误报：正文「国际米兰」里的「米兰」不应被当成卡外球队
        art = {"title": "国际米兰主场取胜", "content": "国际米兰1-0拿下，国米继续领跑。"}
        ok, issues = o.check_consistency_plan(art, {"allowed_teams": ["国米"]}, blocking=True)
        self.assertTrue(ok, f"子串串味不应误杀，却报: {issues}")

    def test_card_external_team_still_blocked(self):
        art = {"title": "米兰2-1尤文", "content": "AC米兰2-1击败尤文图斯。"}
        ok, issues = o.check_consistency_plan(art, {"allowed_teams": ["AC米兰"]}, blocking=True)
        self.assertFalse(ok)
        self.assertTrue(any("卡外" in i for i in issues), issues)

    def test_no_team_mention_blocked(self):
        art = {"title": "某队取胜", "content": "本场比赛由主队拿下。"}
        ok, issues = o.check_consistency_plan(art, {"allowed_teams": ["曼城"]}, blocking=True)
        self.assertFalse(ok)
        self.assertTrue(any("未提及任何事实卡内球队" in i for i in issues), issues)


class TestLongestMatchExtraction(unittest.TestCase):
    def test_longest_match_wins(self):
        got = o._teams_in_content("国际米兰1-0拿下", o._KNOWN_TEAMS)
        self.assertIn("国米", got)
        self.assertNotIn("米兰", got)

    def test_standalone_milan_is_acmilan(self):
        got = o._teams_in_content("米兰2-1尤文", o._KNOWN_TEAMS)
        self.assertIn("米兰", got)
        self.assertNotIn("国米", got)

    def test_team_in_content_alias_variants(self):
        self.assertTrue(o._team_in_content("拜仁", "拜仁慕尼黑客场取胜"))
        self.assertTrue(o._team_in_content("拜仁慕尼黑", "拜仁客场取胜"))
        self.assertFalse(o._team_in_content("拜仁", "多特客场取胜"))
        self.assertFalse(o._team_in_content("拜仁", ""))


class TestVerifyScriptHasAliasCheck(_LedgerIsolated):
    """verify_publish_gate 必须把别名感知做成可回归校验（且自带台账隔离）。"""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "verify_publish_gate",
            str(Path(__file__).resolve().parent.parent / "scripts" / "verify_publish_gate.py"))
        cls.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.script)

    def test_alias_check_is_hard_and_passes(self):
        res = self.script.check_team_alias_awareness()
        self.assertFalse(res.get("advisory"), "别名感知是硬项")
        self.assertTrue(res.get("ok"), res.get("detail"))


if __name__ == "__main__":
    unittest.main()
