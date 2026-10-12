#!/usr/bin/env python3
"""标题反模式闸门单测（计划 3.3 / 第十章）。

覆盖：
  - 三条件命中判定（比分 / 赛果开场）
  - 条件二豁免（具体人物：人工表 + entity_map 球员）
  - 条件三豁免（悬念/评价/叙事钩子、疑问号）
  - 边界：比分不在起首、空标题、配置不可用降级放行
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


class TestConfig(unittest.TestCase):
    def test_config_loads(self):
        cfg = ta.load_config()
        self.assertIsNotNone(cfg, "config/title_antipattern.yaml 应可解析")
        for key in ("版本", "规则名", "开场窗口", "比分模式", "赛果模式", "人物名", "钩子模式", "判罚"):
            self.assertIn(key, cfg, f"缺字段 {key}")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
