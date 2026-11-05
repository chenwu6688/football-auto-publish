#!/usr/bin/env python3
"""队名「赛果动词」清洗回归测试（计划 7.3 / 第十章）。

## 真实事故（2026-11-05 15:13 熔断）

早间批次触发第十章自动熔断，提示：

    同批次一致性校验打回 5 次，疑事实层失真：
    [朗斯主场1-9被科莫血洗，法布雷加斯赛后一句话比比分还扎心] 缺少客队名: 血洗

三层叠加：

1. 采集层按「比分前=主队／比分后=客队」切标题，「朗斯主场1-9被科莫血洗」
   → ``away='科莫血洗'``（动词粘连在队名尾部）；
2. 校验层用黑名单 ``_TEAM_DIRTY_TOKENS`` 判「像不像队名」，表里有
   「胜/负/绝杀/逆转/首发/阵容…」但**没有「血洗」** → 判为合法队名 → 硬要求
   正文出现「血洗」。正文当然不会把动词当队名写，于是必然打回；
3. 熔断层按「打回次数」计，同一篇重试 + 降级重生成 5 次就把阈值堆满，
   一篇稿子把「这一篇不过」放大成「全批停发」。

本组测试锁三件事：**能救则救**（科莫血洗→科莫，仍正常校验）、
**救不回来才跳过**（血洗→空串，跳过校验并留痕）、**不放松**
（干净队名但正文真没提，仍然拦截）。
"""

import tempfile
import unittest
from pathlib import Path

import orchestrator as o
import team_names as tn


def _mk_fixture(home, away, hg, ag, goals=None):
    return {"home_team": home, "away_team": away, "home_score": hg,
            "away_score": ag, "goals": goals or []}


class TestCleanTeamName(unittest.TestCase):
    """清洗函数本身：表驱动，正反都测。"""

    #: 脏串 → 期望的干净值
    DIRTY = {
        # 2026-11-05 熔断现场
        "科莫血洗": "科莫",
        "血洗科莫": "科莫",
        "被科莫血洗": "科莫",
        # 同类赛果动词（黑名单历史上逐个漏过的那些）
        "狂胜朗斯": "朗斯",
        "惨败科莫": "科莫",
        "大胜曼城": "曼城",
        "横扫利物浦": "利物浦",
        "吊打阿森纳": "阿森纳",
        "绝杀拜仁": "拜仁",
        "逆转切尔西": "切尔西",
        "淘汰出局": "",
        # 只有动词本身 → 不是队名
        "血洗": "",
        "逆转": "",
        "淘汰": "",
    }

    #: 真队名一律不许被改（清洗过头的典型受害者）
    KEEP = ("曼联", "拜仁慕尼黑", "巴黎圣日耳曼", "国际米兰", "AC米兰",
            "神户胜利船", "慕尼黑1860", "全北现代", "Manchester City FC",
            "大田市民", "莱比锡", "皇家马德里")

    def test_dirty_strings_cleaned(self):
        for raw, want in self.DIRTY.items():
            with self.subTest(raw=raw):
                self.assertEqual(tn.clean_team_name(raw), want)

    def test_real_names_untouched(self):
        for name in self.KEEP:
            with self.subTest(name=name):
                self.assertEqual(tn.clean_team_name(name), name,
                                 "真队名被清洗改写了，这就是过修")

    def test_result_only_is_not_team(self):
        self.assertTrue(tn.is_result_only("血洗"))
        self.assertFalse(tn.is_result_only("科莫血洗"))
        self.assertFalse(tn.is_result_only("科莫"))
        self.assertFalse(tn.looks_like_team("血洗"))

    def test_glued_name_still_looks_like_team(self):
        # 「科莫血洗」判为「像队名」是对的：它清洗后就是真队名科莫。
        # 判定「是不是队名」与「用哪个串去比」是两件事。
        self.assertTrue(tn.looks_like_team("科莫血洗"))
        self.assertEqual(o._clean_team("科莫血洗"), "科莫")


class TestValidationWithGluedNames(unittest.TestCase):
    """校验层：脏串不许变成「要求正文出现动词」的硬约束。"""

    BODY = ("朗斯在主场1-9被科莫血洗，法布雷加斯赛后只提了"
            "4-0赢曼联的旧账。")

    def test_pure_verb_away_skipped_not_blocked(self):
        """复现事故现场：away_team='血洗' 不再导致「缺少客队名: 血洗」。"""
        fx = _mk_fixture("朗斯", "血洗", 1, 9)
        art = {"title": "朗斯主场1-9被科莫血洗", "content": self.BODY}
        passed, issues = o.validate_article_vs_match_data(fx, art)
        self.assertTrue(passed, f"纯动词脏队名应跳过校验而非拦截，却报: {issues}")
        self.assertFalse(any("血洗" in i for i in issues))

    def test_glued_name_rescued_and_still_checked(self):
        """粘连队名救回「科莫」后继续校验：正文提到则过，没提仍拦。"""
        fx = _mk_fixture("朗斯", "科莫血洗", 1, 9)
        ok = {"title": "朗斯主场1-9被科莫血洗", "content": self.BODY}
        self.assertTrue(o.validate_article_vs_match_data(fx, ok)[0])

        # 同一份脏数据，正文却没提科莫 → 必须拦（证明没把校验整体跳过）
        bad = {"title": "朗斯主场1-9惨败",
               "content": "朗斯在主场1-9输球，法布雷加斯赛后只提了4-0赢曼联的旧账。"}
        passed, issues = o.validate_article_vs_match_data(fx, bad)
        self.assertFalse(passed, "救回队名后仍必须真的比对正文，不能因脏串就放行")
        self.assertTrue(any("缺少客队名" in i for i in issues))

    def test_clean_name_missing_still_blocked(self):
        """净化不能变成放松：干净队名 + 正文真没提 → 照常拦截。"""
        fx = _mk_fixture("朗斯", "科莫", 1, 9)
        art = {"title": "朗斯主场1-9惨败",
               "content": "朗斯在主场1-9输球，法布雷加斯赛后只提了4-0赢曼联的旧账。"}
        passed, issues = o.validate_article_vs_match_data(fx, art)
        self.assertFalse(passed)
        self.assertIn("缺少客队名: 科莫", issues)

    def test_home_side_dirty_also_handled(self):
        fx = _mk_fixture("狂胜科莫", "朗斯", 9, 1)
        art = {"title": "科莫9-1朗斯", "content": "科莫在主场9-1狂胜朗斯。"}
        self.assertTrue(o.validate_article_vs_match_data(fx, art)[0],
                        "主队名的赛果动词同样要剥掉")


class TestMatchReferenceCleans(unittest.TestCase):
    """多源校准返回的也必须是干净队名，别把脏串往下游传。"""

    def test_reference_returns_clean_names(self):
        fx = _mk_fixture("朗斯", "科莫血洗", 1, 9)
        ctx = {"all_fixtures": [fx]}
        ref = o._build_match_reference(fx, ctx)
        self.assertEqual(ref["away_team"], "科莫")
        self.assertEqual(ref["home_team"], "朗斯")


class TestRejectCountingByArticle(unittest.TestCase):
    """熔断阈值按「篇」计，不按「次」计（2026-11-05 放大机制）。"""

    TITLE = "[朗斯主场1-9被科莫血洗，法布雷加斯赛后一句话比比分还扎心]"

    def setUp(self):
        import compliance_guard as cg
        self._cg = cg
        self._d = tempfile.TemporaryDirectory()
        self._prev = cg.STATE_PATH
        cg.STATE_PATH = Path(self._d.name) / "compliance.json"
        self._prev_rejects = dict(o._CONSISTENCY_REJECTS)
        o._CONSISTENCY_REJECTS.update(
            {"count": 0, "subjects": [], "suspended": False, "evidence": []})

    def tearDown(self):
        self._cg.STATE_PATH = self._prev
        o._CONSISTENCY_REJECTS.update(self._prev_rejects)
        self._d.cleanup()

    def test_same_article_repeated_does_not_trip_breaker(self):
        """一篇稿子被同一条错规则拦住 5 次 ≠ 系统性失真。"""
        for _ in range(5):
            o._note_consistency_reject(f"{self.TITLE} 缺少客队名: 血洗")
        self.assertFalse(self._cg.is_suspended(),
                         "同一篇重复打回不该熔断全批——这正是 11-05 的放大机制")

    def test_distinct_articles_still_trip_breaker(self):
        for i in range(self._cg.AUTO_SUSPEND_REJECT_THRESHOLD):
            o._note_consistency_reject(f"[选题{i}] 缺少客队名: 某某队")
        self.assertTrue(self._cg.is_suspended(), "多篇同时失真仍然要熔断（没放松）")

    def test_detail_reports_both_counts(self):
        self._cg.AUTO_SUSPEND_REJECT_THRESHOLD = 2
        try:
            o._note_consistency_reject("[选题A] 缺少客队名: 甲")
            o._note_consistency_reject("[选题A] 缺少客队名: 甲")
            o._note_consistency_reject("[选题B] 缺少客队名: 乙")
        finally:
            self._cg.AUTO_SUSPEND_REJECT_THRESHOLD = 5
        detail = self._cg.violations("疑似系统性失真")[0]["detail"]
        self.assertIn("3 次", detail)
        self.assertIn("2 篇", detail, "要能一眼看出「3 次打回其实只涉及 2 篇」")


class TestScraperCleansAtSource(unittest.TestCase):
    """采集层在入口就剥动词，别把脏串上传给事实层。"""

    TITLE = "朗斯主场1-9被科莫血洗，法布雷加斯赛后一句话比比分还扎心"

    def test_link_text_parsed_clean(self):
        from media_scraper import SportsScraper
        r = SportsScraper()._parse_match_from_link_text(
            self.TITLE, "http://x/match1.htm", "2026-11-05")
        self.assertIsNotNone(r)
        self.assertEqual(r["home_team"], "朗斯")
        self.assertEqual(r["away_team"], "科莫",
                         "「科莫血洗」必须剥成「科莫」，否则下游只能跳过校验")


if __name__ == "__main__":
    unittest.main()
