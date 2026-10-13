#!/usr/bin/env python3
"""标题反模式闸门单测（计划 3.3 / 第十章）。

覆盖：
  - 三条件命中判定（比分 / 赛果开场）
  - 条件二豁免（具体人物：人工表 + entity_map 球员）
  - 条件三豁免（悬念/评价/叙事钩子、疑问号）
  - 边界：比分不在起首、空标题、配置不可用降级放行
  - 规则二「纯赛事名词式」（2026-10-13 R11 建议4）：赛事名词起首命中 /
    人物·球队·钩子豁免 / 不在起首放行
  - 生成侧接入：orchestrator.score_title_plan 判 0 分打回
  - 配置完整性
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import title_antipattern as ta  # noqa: E402


class TestAntipatternRules(unittest.TestCase):
    def test_scoreline_at_start_hits(self):
        for t in ("3-1暴揍中亚狼", "5-1荷兰拿下瑞典", "亚泰主场1-5惨败"):
            with self.subTest(t=t):
                self.assertTrue(ta.check(t)["hit"], t)

    def test_result_word_at_start_hits(self):
        for t in ("大胜之后球队直接放假", "血洗对手后防线彻底暴露"):
            with self.subTest(t=t):
                self.assertTrue(ta.check(t)["hit"], t)

    def test_person_exempts(self):
        # 人工表人物
        self.assertFalse(ta.check("梅西绝杀皇马2-1")["hit"])
        self.assertFalse(ta.check("张玉宁62球追平队史第一！国安残阵逼平领头羊")["hit"])
        # entity_map 球员
        self.assertFalse(ta.check("曼联点球6-5利兹联，B费丢点")["hit"])

    def test_hook_exempts(self):
        for t in ("4-3！切尔西这场球看得我心脏受不了",
                  "1-0的争议，比丢球更难咽",
                  "0-5！国足又输巴勒斯坦，这剧本我们看了二十多年",
                  "克罗地亚0-7英格兰、国足0-5，谁的假期更堵心"):
            with self.subTest(t=t):
                self.assertFalse(ta.check(t)["hit"], t)

    def test_scoreline_not_at_start_exempts(self):
        self.assertFalse(ta.check("西班牙4-1克罗地亚，最动人的却是第81分钟那一幕")["hit"])
        self.assertFalse(ta.check("这一夜，皇马2-1国米")["hit"])

    def test_empty_title(self):
        r = ta.check("")
        self.assertTrue(r["ok"])
        self.assertFalse(r["hit"])

    def test_degraded_releases(self):
        orig = ta.load_config
        ta.load_config = lambda *a, **k: None
        try:
            r = ta.check("3-1暴揍中亚狼")
            self.assertTrue(r["ok"])
            self.assertFalse(r["hit"])
            self.assertTrue(r["degraded"])
        finally:
            ta.load_config = orig

    def test_penalty_fields(self):
        r = ta.check("3-1暴揍中亚狼")
        self.assertEqual(r["penalty_score"], 0)
        self.assertTrue(r["hint"])
        self.assertEqual(r["signal"]["kind"], "比分")


class TestRule2TournamentNouns(unittest.TestCase):
    """规则二：纯赛事名词式（R11 2026-10-13 建议4「加分升级为打回」）。"""

    def test_tournament_noun_at_start_hits(self):
        for t in ("世界杯夺冠赔率榜出炉，豪门集体看衰",
                  "欧冠抽签结果出炉，强强对话提前上演",
                  "小组赛第二轮，出线名额争夺进入白热化",
                  "决赛前的准备，两队都在憋大招"):
            with self.subTest(t=t):
                r = ta.check(t)
                self.assertTrue(r["hit"], t)
                self.assertEqual(r["signal"]["kind"], "赛事名词", t)
                self.assertEqual(r["penalty_score"], 0)

    def test_both_rules_are_hard_blocks(self):
        # 两条规则任一命中都是 0 分硬违规，rule 字段标明实际命中的规则
        for t, rule in (("3-1暴揍中亚狼", "赛果播报式"),
                        ("世界杯夺冠赔率榜出炉，豪门集体看衰", "纯赛事名词式")):
            with self.subTest(t=t):
                r = ta.check(t)
                self.assertEqual(r["rule"], rule)
                self.assertFalse(r["ok"])

    def test_person_exempts_rule2(self):
        for t in ("世界杯决赛，梅西圆梦", "世界杯决赛，姆巴佩上演帽子戏法"):
            with self.subTest(t=t):
                self.assertFalse(ta.check(t)["hit"], t)

    def test_team_exempts_rule2(self):
        # 皇马来自 entity_map club 译名
        self.assertFalse(ta.check("欧冠决赛，皇马第15次捧杯")["hit"])

    def test_hook_exempts_rule2(self):
        self.assertFalse(ta.check("世界杯小组赛，为什么总是这几支球队出局")["hit"])

    def test_not_at_start_releases_rule2(self):
        for t in ("三支球队的世界杯出线形势分析", "他等了八年，才等到这届世界杯"):
            with self.subTest(t=t):
                self.assertFalse(ta.check(t)["hit"], t)

    def test_league_names_not_in_nouns(self):
        # 联赛名刻意不入表：中国足球联赛是有效本地类别（与 title_positive 同口径）
        nouns = ta.tournament_names()
        for lg in ("中超", "英超", "西甲", "意甲", "德甲", "法甲"):
            self.assertNotIn(lg, nouns, lg)

    def test_team_names_include_entity_map_club(self):
        self.assertIn("皇马", ta.team_names())
        self.assertGreater(len(ta.team_names()), 100)


class TestConfig(unittest.TestCase):
    def test_config_loads(self):
        cfg = ta.load_config()
        self.assertIsNotNone(cfg, "config/title_antipattern.yaml 应可解析")
        for key in ("版本", "规则名", "开场窗口", "比分模式", "赛果模式", "人物名", "钩子模式", "判罚"):
            self.assertIn(key, cfg, f"缺字段 {key}")

    def test_config_rule2_present(self):
        cfg = ta.load_config()
        r2 = cfg.get("规则二")
        self.assertIsInstance(r2, dict, "缺规则二（纯赛事名词式）配置块")
        for key in ("规则名", "赛事名词", "判罚"):
            self.assertIn(key, r2, f"规则二缺字段 {key}")
        self.assertEqual(int(r2["判罚"]["分数"]), 0)
        self.assertIn("打回", str(r2["判罚"]["动作"]))
        self.assertTrue(str(r2["判罚"].get("提示", "")).strip())

    def test_config_penalty_is_hard(self):
        cfg = ta.load_config()
        self.assertEqual(int(cfg["判罚"]["分数"]), 0)
        self.assertIn("打回", str(cfg["判罚"]["动作"]))

    def test_person_names_merged(self):
        names = ta.person_names()
        self.assertIn("梅西", names)       # 人工表
        self.assertGreater(len(names), 100)  # 含 entity_map 球员

    def test_selftest_all_pass(self):
        bad = [n for n, ok in ta.selftest() if not ok]
        self.assertEqual(bad, [], f"冒烟未通过：{bad}")


class TestOrchestratorIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import orchestrator
        cls.o = orchestrator

    def test_hard_violation_zero_score(self):
        for t in ("3-1暴揍中亚狼", "亚泰主场1-5惨败"):
            with self.subTest(t=t):
                sc, ok, reasons = self.o.score_title_plan({}, t, "热点球评")
                self.assertEqual(sc, 0)
                self.assertFalse(ok)
                self.assertTrue(any("开场" in r or "赛果播报" in r for r in reasons))

    def test_person_title_not_blocked_by_antipattern(self):
        sc, _ok, reasons = self.o.score_title_plan(
            {}, "1-5惨败后，费耶诺德后卫说了句大实话，巴萨球迷听了都点头", "热点球评")
        self.assertFalse(any("比分开场" in r for r in reasons))

    def test_tournament_noun_zero_score(self):
        # 规则二（纯赛事名词式）同样必须在生成侧 0 分打回
        for t in ("世界杯夺冠赔率榜出炉，豪门集体看衰",
                  "小组赛第二轮，出线名额争夺进入白热化"):
            with self.subTest(t=t):
                sc, ok, reasons = self.o.score_title_plan({}, t, "热点球评")
                self.assertEqual(sc, 0)
                self.assertFalse(ok)
                self.assertTrue(any("赛事名词" in r or "开场" in r for r in reasons), t)

    def test_tournament_noun_negative_not_blocked(self):
        sc, _ok, reasons = self.o.score_title_plan(
            {}, "世界杯踢一半官宣转会，利物浦是在帮球员还是坑球员？", "热点球评")
        self.assertFalse(any("赛事名词" in r for r in reasons))


if __name__ == "__main__":
    unittest.main(verbosity=2)
