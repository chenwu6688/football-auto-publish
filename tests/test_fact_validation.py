"""事实校验的单元测试：覆盖脏数据（客胜曼联 / 内利上演）与多渠道校准。

回归防护：
- 充值前批次因 '缺少客队名: 客胜曼联'、'缺少球员: 内利上演' 把 1/3 文章拦下；
  修复后这两个脏数据应被多渠道校准/容错，不再误拦；
- 真实缺失（干净队名/球员确实未在改写文出现）仍必须拦截。
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import (
    _looks_like_team,
    _clean_player_name,
    _build_match_reference,
    check_rewrite_fidelity,
    validate_article_vs_match_data,
)


def _mk_fixture(home, away, hg, ag, goals=None):
    return {
        "home_team": home,
        "away_team": away,
        "home_score": hg,
        "away_score": ag,
        "goals": goals or [],
    }


class TestTeamNameSanity(unittest.TestCase):
    def test_clean_team(self):
        self.assertTrue(_looks_like_team("曼联"))
        self.assertTrue(_looks_like_team("曼城"))
        self.assertTrue(_looks_like_team("Manchester City FC"))

    def test_dirty_team_rejected(self):
        # 含结果/红牌描述词 -> 不是纯队名
        self.assertFalse(_looks_like_team("客胜曼联"))
        self.assertFalse(_looks_like_team("十人曼城"))
        self.assertFalse(_looks_like_team("逆转巴萨"))
        self.assertFalse(_looks_like_team(""))
        self.assertFalse(_looks_like_team("A"))


class TestPlayerNameClean(unittest.TestCase):
    def test_strip_verb_suffix(self):
        self.assertEqual(_clean_player_name("内利上演")[0], "内利")
        self.assertEqual(_clean_player_name("马丁内利")[0], "马丁内利")

    def test_dirty_flag(self):
        cleaned, dirty = _clean_player_name("内利上演")
        self.assertTrue(dirty)  # 原始含动作词，可信度低
        _, dirty2 = _clean_player_name("内马尔")
        self.assertFalse(dirty2)  # 干净名


class TestMultiSourceReference(unittest.TestCase):
    def test_recover_from_sibling(self):
        dirty = _mk_fixture("十人曼城", "客胜曼联", 1, 0, [{"scorer_name": "内利上演"}])
        sibling = _mk_fixture("曼城", "曼联", 1, 0, [{"scorer_name": "内马尔"}])
        ctx = {"all_fixtures": [dirty, sibling]}
        ref = _build_match_reference(dirty, ctx)
        self.assertEqual(ref["home_team"], "曼城")
        self.assertEqual(ref["away_team"], "曼联")
        self.assertIn("内马尔", ref["scorers"])


class TestRewriteFidelity(unittest.TestCase):
    def test_dirty_scorer_tolerated(self):
        # 脏球员名 '内利上演' + 兄弟源干净名 '内马尔'，改写文含内马尔 -> 通过
        dirty = _mk_fixture("十人曼城", "客胜曼联", 1, 0, [{"scorer_name": "内利上演"}])
        sibling = _mk_fixture("曼城", "曼联", 1, 0, [{"scorer_name": "内马尔"}])
        ctx = {"all_fixtures": [dirty, sibling]}
        article = {"content": "十人应战的曼城硬啃下曼联，内马尔那脚太关键了。", "title": "曼城曼联"}
        passed, issues = check_rewrite_fidelity(dirty, article, ctx)
        self.assertTrue(passed, f"应放行，却报: {issues}")

    def test_dirty_scorer_without_sibling_tolerated(self):
        # 无兄弟源时，脏球员名也只放宽不拦截（仅警告）
        dirty = _mk_fixture("十人曼城", "客胜曼联", 1, 0, [{"scorer_name": "内利上演"}])
        article = {"content": "曼城拿下曼联，比赛很精彩。", "title": "曼城曼联"}
        passed, issues = check_rewrite_fidelity(dirty, article, {"all_fixtures": [dirty]})
        self.assertTrue(passed, f"无兄弟源也应放宽，却报: {issues}")

    def test_confident_scorer_missing_still_fails(self):
        # 干净可信球员 '武磊' 源文章里有、但改写文确实未出现 -> 必须拦截
        fx = _mk_fixture("西班牙人", "巴萨", 0, 2, [{"scorer_name": "武磊"}])
        fx["article_text"] = "武磊首发登场，但巴萨客场2-0取胜西班牙人。"
        article = {"content": "巴萨客场轻松取胜。", "title": "巴萨胜"}
        passed, issues = check_rewrite_fidelity(fx, article, {"all_fixtures": [fx]})
        self.assertFalse(passed)
        self.assertTrue(any("武磊" in i for i in issues))


class TestMatchDataValidation(unittest.TestCase):
    def test_dirty_away_team_recovered(self):
        dirty = _mk_fixture("十人曼城", "客胜曼联", 1, 0, [])
        sibling = _mk_fixture("曼城", "曼联", 1, 0, [])
        ctx = {"all_fixtures": [dirty, sibling]}
        article = {"content": "曼城 1-0 曼联，十人作战仍拿下。", "title": "曼城曼联"}
        passed, issues = validate_article_vs_match_data(dirty, article, ctx)
        self.assertTrue(passed, f"应放行，却报: {issues}")

    def test_clean_team_missing_still_fails(self):
        # 干净队名 '曼联' 确实未在改写文出现 -> 必须拦截
        fx = _mk_fixture("曼城", "曼联", 1, 0, [])
        article = {"content": "曼城主场取胜。", "title": "曼城胜"}
        passed, issues = validate_article_vs_match_data(fx, article, {"all_fixtures": [fx]})
        self.assertFalse(passed)
        self.assertTrue(any("缺少客队名" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
