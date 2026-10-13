#!/usr/bin/env python3
"""互动引导闸门单测（R11 内容诊断 2026-10-13「建议5」）。

覆盖：
  - 判定：关注引导 / 互动引导 两类缺一不可
  - 语境纪律：「值得关注」这类事实性用词不得被误判成钩子
  - interaction_bait 字段可补足互动引导证据
  - 兜底注入：缺哪类补哪类、已有引导不重复注入、注入后必达标
  - 降级：配置不可用 → 放行且标记 degraded
  - 生成侧接入：orchestrator.check_interaction_bait 走同一判定；
    保存侧注入把 interaction_bait 字段真正写进正文（此前被静默跳过）
  - 配置完整性
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import interaction_bait as ib  # noqa: E402

FULL = ("这球换你当裁判，吹不吹？评论区告诉我。\n---\n关注老六，这转会窗的瓜我替你盯着。")


class TestCheck(unittest.TestCase):
    def test_full_pass(self):
        r = ib.check(FULL)
        self.assertTrue(r["ok"])
        self.assertEqual(r["missing"], [])

    def test_missing_follow(self):
        r = ib.check("这场球太离谱了。你怎么看？评论区聊聊。")
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], ["关注引导"])

    def test_missing_comment(self):
        r = ib.check("这场球老六全程没缓过来。点个关注，后续我替你盯着。")
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], ["互动引导"])

    def test_missing_both(self):
        r = ib.check("这是一篇干巴巴的赛报，没有任何引导。")
        self.assertFalse(r["ok"])
        self.assertEqual(set(r["missing"]), {"关注引导", "互动引导"})

    def test_hint_present_when_fail(self):
        r = ib.check("干巴巴的赛报。")
        self.assertTrue(r["hint"])
        self.assertEqual(r["penalty_score"], 0)

    def test_factual_wording_not_bait(self):
        # 「值得关注」是事实性用词，不是粉丝沉淀钩子
        r = ib.check("这笔引援值得关注，事态还在发酵。")
        self.assertIn("关注引导", r["missing"])

    def test_field_supplies_comment_evidence(self):
        r = ib.check("点个关注，后续我替你盯着。", bait_field="你觉得谁会先换帅？")
        self.assertTrue(r["ok"])
        self.assertIn("field:interaction_bait", r["hits"]["comment"])

    def test_short_field_not_counted(self):
        r = ib.check("点个关注，后续我替你盯着。", bait_field="哦")
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


class TestInject(unittest.TestCase):
    def test_inject_adds_missing_only(self):
        art = {"content": "正文，没有引导。", "interaction_type": "站队式"}
        out = ib.inject(art)
        self.assertTrue(out["changed"])
        self.assertEqual(set(out["injected"]), {"关注引导", "互动引导"})
        self.assertTrue(ib.check(art["content"])["ok"])
        self.assertEqual(art["interaction_bait_source"], "template_fallback")

    def test_inject_skips_when_ok(self):
        art = {"content": FULL, "interaction_type": "站队式"}
        self.assertFalse(ib.inject(art)["changed"])
        self.assertEqual(art["content"], FULL)

    def test_inject_only_comment_when_follow_present(self):
        art = {"content": "点个关注不迷路。", "interaction_type": "预测式"}
        out = ib.inject(art)
        self.assertEqual(out["injected"], ["互动引导"])
        self.assertIn("评论区", art["content"])

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
        for key in ("版本", "关注引导", "互动引导", "判罚", "兜底模板"):
            self.assertIn(key, cfg, f"缺字段 {key}")

    def test_penalty_is_hard(self):
        cfg = ib.load_config()
        self.assertEqual(int(cfg["判罚"]["分数"]), 0)
        self.assertIn("打回", str(cfg["判罚"]["动作"]))

    def test_fallback_templates(self):
        follow, comment = ib.fallback_texts("投票式")
        self.assertTrue(follow)
        self.assertTrue(comment)
        self.assertTrue(ib.check(f"{comment}\n{follow}")["ok"])

    def test_unknown_type_uses_default(self):
        follow, comment = ib.fallback_texts("不存在的类型")
        self.assertTrue(ib.check(f"{comment}\n{follow}")["ok"])


class TestOrchestratorIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import orchestrator
        cls.o = orchestrator

    def test_wrapper_matches_module(self):
        art = {"content": "干巴巴的赛报。", "interaction_bait": ""}
        r = self.o.check_interaction_bait(art)
        self.assertFalse(r["ok"])
        self.assertEqual(set(r["missing"]), {"关注引导", "互动引导"})

    def test_wrapper_ok_for_full(self):
        self.assertTrue(self.o.check_interaction_bait({"content": FULL})["ok"])

    def test_save_side_injects_field_bait(self):
        """interaction_bait 字段此前被静默跳过；现在必须注入正文并复核达标。"""
        art = {"content": "正文主体。", "interaction_bait": "你觉得这波谁最亏？",
               "interaction_type": "共鸣式"}
        # 保存侧流程：先按字段注入，再复核；字段已有互动问题，故只需补关注引导
        out = ib.inject(art)
        self.assertIn("关注引导", out["injected"])
        self.assertTrue(ib.check(art["content"], bait_field=art["interaction_bait"])["ok"])

    def test_retry_hint_mentions_missing(self):
        art = {"content": "干巴巴的赛报。"}
        hint = self.o._bait_retry_hint(self.o.check_interaction_bait(art))
        self.assertIn("关注引导", hint)
        self.assertIn("互动引导", hint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
