#!/usr/bin/env python3
"""标题正向规则单测（计划 3.3 / R11 内容诊断结论落地）。

覆盖：
  - 各计分规则（悬念词加分 / 最优长度带边缘 / 赛事名词及其三种豁免 / 联赛名永不减分 /
    情绪词减分 / 崩盘豁免 / 组合算术 / 空标题）
  - 配置完整性（赛事名词不含联赛名，回归守护）
  - 降级放行（load_config → None）
  - selftest 全通过
  - 生成侧接入：orchestrator.score_title_plan / score_fixture_title 的排序不变量
  - 发布闸门分桶：verify_publish_gate._score_titles 把 R11 理由的 0 分归入 r11 桶
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import title_positive as tp  # noqa: E402


class TestRules(unittest.TestCase):
    def test_suspense_bonus(self):
        # 22 字（最优带内）含「没想到」→ 12 + 10 = 22
        self.assertEqual(tp.evaluate("没想到" + "某" * 19)["bonus"], 22)

    def test_optimal_band_edges(self):
        # 21-30 最优带 → +10（无悬念/事件/情绪的中性标题）
        self.assertEqual(tp.evaluate("这场较量" + "某" * 21)["bonus"], 10)
        # 18-20 字 → 无最优带加分
        self.assertEqual(tp.evaluate("这场较量" + "某" * 15)["bonus"], 0)
        # 31+ 字 → 无最优带加分
        self.assertEqual(tp.evaluate("这场较量" + "某" * 29)["bonus"], 0)

    def test_event_noun_only_penalty(self):
        # 赛事名词 + 无具体性 → -12（长度落在 15，跳出最优带避免叠加）
        self.assertEqual(tp.evaluate("世界杯" + "某" * 12)["bonus"], -12)

    def test_event_noun_exempt_person(self):
        r = tp.evaluate("世界杯梅西" + "某" * 8)
        self.assertEqual(r["bonus"], 0)
        self.assertTrue(all(not x.startswith("R11数据:标题含赛事名词") for x in r["reasons"]))

    def test_event_noun_exempt_known_teams(self):
        r = tp.evaluate("世界杯皇马" + "某" * 8, known_teams={"皇马"})
        self.assertEqual(r["bonus"], 0)

    def test_event_noun_exempt_has_specific(self):
        r = tp.evaluate("世界杯" + "某" * 12, has_specific=True)
        self.assertEqual(r["bonus"], 0)

    def test_league_name_never_penalized(self):
        # 中超是联赛名，不在赛事名词表 → 永不减分
        self.assertEqual(tp.evaluate("中超争冠白热化" + "某" * 6)["bonus"], 0)

    def test_emotion_penalty(self):
        self.assertEqual(tp.evaluate("防线炸了")["bonus"], -8)

    def test_bengpan_exemption(self):
        # 下半场崩盘 → 崩盘豁免，不触发「崩」情绪减分
        self.assertEqual(tp.evaluate("下半场崩盘")["bonus"], 0)

    def test_beng_without_bengpan(self):
        self.assertEqual(tp.evaluate("巴萨崩了")["bonus"], -8)

    def test_combo_arithmetic(self):
        # 没想到(悬念+12) + 最优带(21-30,+10) + 决赛(事件-12) + 崩(情绪-8) = +2
        combo = "没想到这届决赛竟成绝唱谁能想到结局如此崩了"
        self.assertTrue(21 <= len(combo) <= 30, f"组合标题应在最优带内，实为 {len(combo)} 字")
        self.assertEqual(tp.evaluate(combo)["bonus"], 2)

    def test_empty_title(self):
        r = tp.evaluate("")
        self.assertEqual(r["bonus"], 0)
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["detail"], ["标题为空，不判定"])
        self.assertFalse(r["degraded"])


class TestConfig(unittest.TestCase):
    def test_config_loads(self):
        cfg = tp.load_config()
        self.assertIsNotNone(cfg, "config/title_positive.yaml 应可解析")
        for key in ("版本", "生效日期", "口径出处", "悬念词", "最优长度带",
                    "赛事名词", "情绪词", "加分", "减分"):
            self.assertIn(key, cfg, f"缺字段 {key}")

    def test_scores_are_ints(self):
        cfg = tp.load_config()
        self.assertIsInstance(cfg["加分"]["悬念"], int)
        self.assertIsInstance(cfg["加分"]["最优带"], int)
        self.assertIsInstance(cfg["减分"]["赛事名词"], int)
        self.assertIsInstance(cfg["减分"]["情绪词"], int)

    def test_event_nouns_exclude_leagues(self):
        cfg = tp.load_config()
        forbidden = {"中超", "英超", "西甲", "意甲", "德甲", "法甲"}
        for league in forbidden:
            self.assertNotIn(league, cfg["赛事名词"],
                             f"赛事名词表不应含联赛名 {league}（回归守护）")


class TestDegraded(unittest.TestCase):
    def test_degraded_releases(self):
        orig = tp.load_config
        tp.load_config = lambda *a, **k: None
        try:
            r = tp.evaluate("世界杯来了")
            self.assertEqual(r["bonus"], 0)
            self.assertTrue(r["degraded"])
        finally:
            tp.load_config = orig


class TestSelftest(unittest.TestCase):
    def test_selftest_all_pass(self):
        bad = [n for n, ok in tp.selftest() if not ok]
        self.assertEqual(bad, [], f"冒烟未通过：{bad}")


class TestOrchestratorIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import orchestrator
        cls.o = orchestrator

    def test_event_noun_lower_than_person_version(self):
        # 同一核心，含具体人名的版本应严格高于仅赛事名词的版本
        base_event = "世界杯" + "某" * 14            # 赛事名词无具体性 → 被减分
        base_person = "世界杯梅西" + "某" * 12       # 含具体人名 → 豁免
        s_ev, _, _ = self.o.score_title_plan({}, base_event, "热点球评")
        s_pe, _, _ = self.o.score_title_plan({}, base_person, "热点球评")
        self.assertLess(s_ev, s_pe, "含赛事名词无具体性应低于含具体人名版本")

    def test_suspense_optimal_higher_than_plain(self):
        plain = "这场较量" + "某" * 21               # 仅命中最优带 +10
        suspense = "没想到" + "某" * 23              # 悬念 + 最优带 = +22
        s_p, _, _ = self.o.score_title_plan({}, plain, "热点球评")
        s_s, _, _ = self.o.score_title_plan({}, suspense, "热点球评")
        self.assertLess(s_p, s_s, "悬念+最优带版本应高于纯中性版本")

    def test_template_prefix_still_zero(self):
        # 模板前缀规则必须仍把分数归零，R11 块不可「复活」它
        t = "老六精准预测：" + "没想到" + "某" * 20
        sc, ok, reasons = self.o.score_title_plan({}, t, "热点球评")
        self.assertEqual(sc, 0)
        self.assertFalse(ok)

    def test_fixture_title_no_event_penalty_with_teams(self):
        topic = {"home_zh": "皇马", "away_zh": "巴萨"}
        # 含主客队名 + 赛事名词 → 具体性豁免，不得有赛事名词减分理由
        title = "皇马对巴萨的世界杯焦点战谁能笑到最后呢"
        sc, reasons = self.o.score_fixture_title(topic, title)
        self.assertFalse(any(r.startswith("R11数据:标题含赛事名词") for r in reasons))

    def test_fixture_title_suspense_bonus_applied(self):
        topic = {"home_zh": "皇马", "away_zh": "巴萨"}
        plain = "皇马对巴萨的这场比赛结果将会如何演变呢"
        suspense = "没想到皇马巴萨这场对决竟出现如此戏剧性转折呢"
        s_p, _ = self.o.score_fixture_title(topic, plain)
        s_s, _ = self.o.score_fixture_title(topic, suspense)
        self.assertLess(s_p, s_s, "含悬念词的赛程标题应高于中性版本")


class TestPublishGateSeparation(unittest.TestCase):
    def _load_vg(self):
        spec = importlib.util.spec_from_file_location(
            "verify_publish_gate",
            PROJECT_ROOT / "scripts" / "verify_publish_gate.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_r11_zero_goes_to_r11_bucket(self):
        import orchestrator
        vg = self._load_vg()
        orig = orchestrator.score_title_plan
        # score_title_plan 的基础分 ≥22，R11 减分（-12/-8）自然不会把标题压到 0；
        # 此处直接验证分桶路由逻辑：若某标题 score==0 且理由含「R11数据:」前缀，
        # _score_titles 必须把它归入 r11 桶，而非原有硬违规 zero 桶或赛果播报式 anti 桶。
        def fake(topic, title, content_type, batch_mode=None, *, fact_tier=None):
            return 0, False, [
                "R11数据:标题含赛事名词「世界杯」且无具体人物/球队，"
                "回测 CTR 0.47% 垫底，请把具体的人/队提到最前"]
        orchestrator.score_title_plan = fake
        try:
            arts = [({"date": "2026-10-13"},
                     {"title": "世界杯来了", "content_type": "热点球评"})]
            total, passed, zero, fails, anti, r11 = vg._score_titles(arts)
        finally:
            orchestrator.score_title_plan = orig
        self.assertEqual(total, 1)
        self.assertEqual(len(r11), 1, "R11 理由的 0 分应落入 r11 桶")
        self.assertEqual(len(zero), 0, "不应误判为原有硬违规 zero 桶")
        self.assertEqual(len(anti), 0, "不应误判为赛果播报式 anti 桶")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAdoptionMonitor(unittest.TestCase):
    """R11 采用率监控：低采用率告警、达标不告警、空输入安静返回。"""

    def _run(self, topics):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            tp.warn_adoption(topics)
        return buf.getvalue()

    def test_low_adoption_warns(self):
        # 全 plain 标题：无悬念词、长度不足 → 两条告警都应出现
        topics = [{"title": "曼城昨晚比赛结果"}, {"title": "皇马发出邀请"}]
        out = self._run(topics)
        self.assertIn("悬念词采用率偏低", out)
        self.assertIn("21-30 字采用率偏低", out)

    def test_high_adoption_no_warn(self):
        # 两条都须 ≥21 字且含悬念词（覆盖「悬念词 + 最优带」双达标）
        topics = [{"title": "没想到曼城竟然能在决赛完成惊天大反转太震撼"},   # 22 字，悬念
                  {"title": "原来皇马这笔转会的真相竟然是这样子实在太离谱"}]   # 22 字，悬念
        out = self._run(topics)
        self.assertIn("采用达标", out)
        self.assertNotIn("偏低", out)

    def test_empty_and_blank_quiet(self):
        self.assertEqual(self._run([]), "")
        self.assertEqual(self._run([{"title": "  "}, {"no_title": 1}]), "")

    def test_band_helper_uses_config(self):
        cfg = tp.load_config() or {}
        self.assertTrue(tp._in_band("二" * 25, cfg))
        self.assertFalse(tp._in_band("二" * 10, cfg))
        self.assertFalse(tp._in_band("", cfg))
