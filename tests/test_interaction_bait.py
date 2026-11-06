#!/usr/bin/env python3
"""互动引导闸门单测（R11 内容诊断 2026-10-13「建议5」· 2026-11-06 合规修正）。

覆盖：
  - 判定：只要求「给读者留一个可回复的入口」；**关注引导不再是必须项**
  - 语境纪律：「值得关注」这类事实性用词不得被误判成钩子
  - 诱导话术黑名单：索要关注 / 互关互粉 / 扣1 站队 / 博彩术语 → 必须拦
  - 兜底注入：只补互动引导，**不再注入关注话术**
  - 降级：配置不可用 → 放行且标记 degraded
  - 生成侧接入：orchestrator.check_interaction_bait / check_inducement 走同一判定
  - 配置完整性

2026-11-06 为什么改：旧口径要求「关注引导 + 互动引导」缺一不可，最后由保存侧
注入「关注老六，这波后续我替你盯着」。而信用分扣分表明列
「诱导交互 20 / 违规涨粉 20 / 引导互粉 20」——等于为了让稿子涨粉，
系统在批量生产平台扣分的话术。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import interaction_bait as ib  # noqa: E402

#: 符合新口径的「好稿」：抛出可讨论的问题，不索要关注
GOOD = "这球换你当裁判，吹不吹？评论区告诉我。"


class TestCheck(unittest.TestCase):
    def test_full_pass(self):
        r = ib.check(GOOD)
        self.assertTrue(r["ok"])
        self.assertEqual(r["missing"], [])

    def test_follow_no_longer_required(self):
        """2026-11-06：没有关注引导也达标（索要关注是扣分项，不是加分项）。"""
        r = ib.check(GOOD)
        self.assertFalse(r["follow_required"])
        self.assertTrue(r["ok"])

    def test_missing_comment(self):
        r = ib.check("这场球老六全程没缓过来。")
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], ["互动引导"])

    def test_hint_present_when_fail(self):
        r = ib.check("干巴巴的赛报。")
        self.assertTrue(r["hint"])
        self.assertEqual(r["penalty_score"], 0)

    def test_factual_wording_not_bait(self):
        # 「值得关注」是事实性用词，不是粉丝沉淀钩子，更不是诱导
        r = ib.check("这笔引援值得关注，事态还在发酵。")
        self.assertIn("互动引导", r["missing"])
        self.assertTrue(ib.check_inducement("这笔引援值得关注，事态还在发酵。")["ok"])

    def test_field_supplies_comment_evidence(self):
        r = ib.check("就这样。", bait_field="你觉得谁会先换帅？")
        self.assertTrue(r["ok"])
        self.assertIn("field:interaction_bait", r["hits"]["comment"])

    def test_short_field_not_counted(self):
        r = ib.check("就这样。", bait_field="哦")
        self.assertFalse(r["ok"])

    def test_empty_content(self):
        r = ib.check("")
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], ["正文"])

    def test_degraded_releases(self):
        orig = ib.load_config
        ib.load_config = lambda *a, **k: None
        try:
            r = ib.check("什么都没有。")
            self.assertTrue(r["ok"])
            self.assertTrue(r["degraded"])
        finally:
            ib.load_config = orig


class TestInducement(unittest.TestCase):
    """诱导话术黑名单（对应信用分 20 分档三条 + 70 分档赌博）。"""

    def test_follow_begging_blocked(self):
        for txt in ("关注老六，这波后续我替你盯着。",
                    "点个关注，明天赛后第一时间来复盘。",
                    "关注不迷路。"):
            r = ib.check_inducement(txt)
            self.assertFalse(r["ok"], txt)
            self.assertIn("索要关注", {h["group"] for h in r["hits"]})

    def test_mutual_follow_blocked(self):
        r = ib.check_inducement("关注我，我必回关。")
        self.assertFalse(r["ok"])
        self.assertIn("互关互粉", {h["group"] for h in r["hits"]})

    def test_interaction_begging_blocked(self):
        for txt in ("同意的扣1。", "不服来辩。", "投票看结果。"):
            self.assertFalse(ib.check_inducement(txt)["ok"], txt)

    def test_gambling_blocked(self):
        for txt in ("这单我押注主队。", "打开盘口看看水位。", "滚球追一手。"):
            r = ib.check_inducement(txt)
            self.assertFalse(r["ok"], txt)
            self.assertIn("博彩", {h["group"] for h in r["hits"]})

    def test_clean_text_passes(self):
        self.assertTrue(ib.check_inducement(GOOD)["ok"])
        self.assertTrue(ib.check_inducement("那不勒斯客场 2-1 拿下，中场换了三个人。")["ok"])

    def test_empty_content_not_flagged_here(self):
        """空正文归 TT-10 格式低质，这里不重复报（否则一条问题被两处计罚）。"""
        self.assertTrue(ib.check_inducement("")["ok"])

    def test_degraded_releases(self):
        self.assertFalse(ib.check_inducement("关注我。")["ok"])
        orig = ib.load_config
        ib.load_config = lambda *a, **k: None
        try:
            d = ib.check_inducement("关注我。")
            self.assertTrue(d["ok"])
            self.assertTrue(d["degraded"])
        finally:
            ib.load_config = orig


class TestInject(unittest.TestCase):
    def test_inject_adds_comment_only(self):
        art = {"content": "正文，没有引导。", "interaction_type": "站队式"}
        out = ib.inject(art)
        self.assertTrue(out["changed"])
        self.assertEqual(out["injected"], ["互动引导"])
        self.assertTrue(ib.check(art["content"])["ok"])
        self.assertEqual(art["interaction_bait_source"], "template_fallback")

    def test_inject_never_adds_follow(self):
        """关注话术自 2026-11-06 起不再自动注入（那是诱导交互/违规涨粉）。"""
        art = {"content": "正文。", "interaction_type": "共鸣式"}
        ib.inject(art)
        self.assertNotIn("关注", art["content"])

    def test_inject_skips_when_ok(self):
        art = {"content": GOOD, "interaction_type": "站队式"}
        self.assertFalse(ib.inject(art)["changed"])
        self.assertEqual(art["content"], GOOD)

    def test_inject_does_not_fabricate_facts(self):
        art = {"content": "正文。", "interaction_type": "站队式"}
        ib.inject(art)
        self.assertNotIn("http", art["content"])

    def test_selftest_all_pass(self):
        bad = [n for n, ok in ib.selftest() if not ok]
        self.assertEqual(bad, [], f"冒烟未通过：{bad}")


class TestConfig(unittest.TestCase):
    def test_config_loads(self):
        cfg = ib.load_config()
        self.assertIsNotNone(cfg, "config/interaction_bait.yaml 应可解析")
        for key in ("版本", "关注引导", "互动引导", "诱导话术", "判罚", "兜底模板"):
            self.assertIn(key, cfg, f"缺字段 {key}")

    def test_follow_not_required_in_config(self):
        cfg = ib.load_config()
        self.assertIs(cfg.get("关注引导必需"), False,
                      "索要关注是扣分项；配置必须显式标 false，否则会退回旧口径")

    def test_gambling_words_not_in_bait_patterns(self):
        """「下注/押注/扣1」此前写在**达标模式**里，等于奖励诱导话术。"""
        cfg = ib.load_config()
        joined = " ".join(cfg.get("互动引导") or [])
        for bad in ("下注", "押注", "扣", "站队", "投票"):
            self.assertNotIn(bad, joined, f"「{bad}」不该再是互动引导的达标模式")

    def test_penalty_is_hard(self):
        cfg = ib.load_config()
        self.assertEqual(int(cfg["判罚"]["分数"]), 0)
        self.assertIn("打回", str(cfg["判罚"]["动作"]))

    def test_fallback_templates(self):
        follow, comment = ib.fallback_texts("投票式")
        self.assertTrue(comment)
        self.assertTrue(ib.check(comment)["ok"])
        self.assertIn("停用", follow)

    def test_unknown_type_uses_default(self):
        _follow, comment = ib.fallback_texts("不存在的类型")
        self.assertTrue(ib.check(comment)["ok"])


class TestOrchestratorIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import orchestrator
        cls.o = orchestrator

    def test_wrapper_matches_module(self):
        r = self.o.check_interaction_bait({"content": "干巴巴的赛报。", "interaction_bait": ""})
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], ["互动引导"])

    def test_wrapper_ok_for_full(self):
        self.assertTrue(self.o.check_interaction_bait({"content": GOOD})["ok"])

    def test_inducement_wrapper_blocks(self):
        r = self.o.check_inducement({"content": "关注老六，后续我替你盯着。"})
        self.assertFalse(r["ok"])

    def test_save_side_accepts_field_bait(self):
        """interaction_bait 字段是「互动设计已完成」的证据，不该再被重复注入。

        2026-11-06 前：字段只补互动引导 → 还要补关注引导 → 注入「关注老六」。
        现在：字段已补足互动引导，且关注引导不再是必需项 → 无需注入。
        """
        art = {"content": "正文主体。", "interaction_bait": "你觉得这波谁最亏？",
               "interaction_type": "共鸣式"}
        out = ib.inject(art)
        self.assertEqual(out["injected"], [])
        self.assertFalse(out["changed"])
        self.assertTrue(ib.check(art["content"], bait_field=art["interaction_bait"])["ok"])

    def test_retry_hint_mentions_missing(self):
        hint = self.o._bait_retry_hint(self.o.check_interaction_bait({"content": "干巴巴的赛报。"}))
        self.assertIn("互动引导", hint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
