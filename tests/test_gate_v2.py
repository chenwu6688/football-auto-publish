"""闸门分级 v2 的口径测试（2026-11-05）。

守的是这次改造的核心主张：**按语义分级，不按判据名分级**。

- 名字缺失（正文用「他们」「主队」指代）→ 只记分，绝不阻断；
- 数值冲突（卡外比分、比分方向反、进球数造假）→ 仍是硬闸门；
- 空事实卡 → `judged=False`，不拦也不当通过；
- `GATE_MODE=strict` → 一键回到旧的 7 道与门口径。
"""

import json
import os
import unittest
from unittest import mock

import gate_v2


FX = {"home_team": "曼城", "away_team": "利物浦",
      "home_score": 2, "away_score": 1, "goals": []}
ART = {"title": "曼城2-1利物浦", "content": "曼城在主场2-1拿下利物浦，中场控制住了节奏。"}


class TestModeSwitch(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("GATE_MODE", None)

    def test_default_is_v2(self):
        os.environ.pop("GATE_MODE", None)
        self.assertEqual(gate_v2.mode(), "v2")

    def test_strict_rollback(self):
        os.environ["GATE_MODE"] = "strict"
        self.assertEqual(gate_v2.mode(), "strict")
        self.assertTrue(gate_v2.blocking("fidelity_name"))
        self.assertTrue(gate_v2.blocking("anti_copy"))

    def test_unknown_value_falls_back_to_v2(self):
        # 拼错不许悄悄变成 strict（那会让「没生效」看起来像「没放行」）
        os.environ["GATE_MODE"] = "V2 "
        self.assertEqual(gate_v2.mode(), "v2")

    def test_v2_only_hard_gates_block(self):
        os.environ.pop("GATE_MODE", None)
        self.assertTrue(gate_v2.blocking("fact_conflict"))
        self.assertTrue(gate_v2.blocking("compliance"))
        for name in ("fidelity_name", "anti_copy", "title_score",
                     "interaction", "word_count", "coverage"):
            self.assertFalse(gate_v2.blocking(name), name)
        self.assertEqual(gate_v2.tier_of("fact_conflict"), "hard")
        self.assertEqual(gate_v2.tier_of("title_score"), "score")


class TestFactGateV2(unittest.TestCase):
    def setUp(self):
        os.environ.pop("GATE_MODE", None)

    def test_missing_name_does_not_block(self):
        """10-12 / 10-13 两次熔断的形态：正文没写全称，旧判据 FAIL。"""
        g = gate_v2.fact_gate(ART, FX, None,
                              legacy=(False, ["缺少客队名: 利物浦"]))
        self.assertFalse(g["block"])
        self.assertEqual(g["record"]["legacy_issues"], ["缺少客队名: 利物浦"])
        self.assertEqual(g["record"]["legacy_conflicts"], [])

    def test_out_of_card_score_still_blocks(self):
        """「卡外比分 9-0」是臆造数值，不能跟着名字缺失一起放过。"""
        g = gate_v2.fact_gate(ART, FX, None,
                              legacy=(False, ["卡外比分 9-0"]))
        self.assertTrue(g["block"])
        self.assertIn("9-0", g["hint"])

    def test_real_contradiction_blocks_without_legacy(self):
        """旧判据放过了，新判据也要拦：把比分方向写反。"""
        art = {"title": "利物浦2-1曼城", "content": "利物浦在客场2-1赢了曼城。"}
        g = gate_v2.fact_gate(art, FX, None, legacy=(True, []))
        self.assertTrue(g["block"])
        self.assertTrue(g["record"]["contradictions"])

    def test_empty_fixture_is_unjudged_not_passed(self):
        """空事实卡：不拦，但也绝不能记成「通过」。"""
        g = gate_v2.fact_gate({"title": "t", "content": "正文"},
                              {"home_team": "", "away_team": ""}, None,
                              legacy=(True, []))
        self.assertFalse(g["block"])
        self.assertIs(g["record"]["judged"], False)

    def test_strict_mode_restores_legacy_block(self):
        os.environ["GATE_MODE"] = "strict"
        g = gate_v2.fact_gate(ART, FX, None,
                              legacy=(False, ["缺少客队名: 利物浦"]))
        self.assertTrue(g["block"])

    def test_assert_module_error_does_not_block(self):
        """判据自己炸了只记 error，不许阻断发布（埋点/闸门不是同一件事）。"""
        with mock.patch.object(gate_v2, "_fact_assert",
                               side_effect=RuntimeError("boom")):
            g = gate_v2.fact_gate(ART, FX, None, legacy=(True, []))
        self.assertFalse(g["block"])
        self.assertIn("boom", g["record"]["error"])


class TestConflictQuota(unittest.TestCase):
    """新判据首日保险丝：同批矛盾选题满额后改记分，不再停批。"""

    def setUp(self):
        os.environ.pop("GATE_MODE", None)
        gate_v2.reset_batch()

    def tearDown(self):
        gate_v2.reset_batch()

    def test_quota_stops_blocking_after_threshold(self):
        bad = {"title": "利物浦2-1曼城", "content": "利物浦在客场2-1赢了曼城。"}
        blocked = [gate_v2.fact_gate(bad, FX, None, subject=f"选题{i}",
                                     legacy=(True, []))["block"]
                   for i in range(gate_v2.MAX_CONFLICT_SUBJECTS)]
        self.assertTrue(all(blocked))
        after = gate_v2.fact_gate(bad, FX, None, subject="选题X", legacy=(True, []))
        self.assertFalse(after["block"])
        self.assertIn("fail_open", after["record"])

    def test_same_subject_counted_once(self):
        bad = {"title": "利物浦2-1曼城", "content": "利物浦在客场2-1赢了曼城。"}
        for _ in range(10):   # 同一篇重试 10 次也只算 1 篇
            gate_v2.fact_gate(bad, FX, None, subject="同一选题", legacy=(True, []))
        self.assertEqual(gate_v2.conflict_subjects(), ["同一选题"])

    def test_strict_mode_ignores_quota(self):
        os.environ["GATE_MODE"] = "strict"
        bad = {"title": "利物浦2-1曼城", "content": "利物浦在客场2-1赢了曼城。"}
        for i in range(gate_v2.MAX_CONFLICT_SUBJECTS + 2):
            g = gate_v2.fact_gate(bad, FX, None, subject=f"选题{i}", legacy=(True, []))
            self.assertTrue(g["block"], f"strict 第 {i} 篇不该被配额放过")


class TestRecordAndSummary(unittest.TestCase):
    def setUp(self):
        os.environ.pop("GATE_MODE", None)

    def test_attach_writes_metadata_only(self):
        art = dict(ART)
        gate_v2.attach(art, {"judged": True, "contradictions": []})
        self.assertIn("_gate", art)
        # 不进正文
        self.assertNotIn("_gate", art["content"])

    def test_summary_counts(self):
        arts = [
            {"_gate": {"fact": {"judged": True, "contradictions": [],
                                "legacy_issues": ["缺少客队名: 利物浦"]}}},
            {"_gate": {"fact": {"judged": True, "contradictions": ["比分方向反"],
                                "legacy_issues": []}}},
            {"_gate": {"fact": {"judged": False, "contradictions": [],
                                "legacy_issues": []}}},
            {"title": "没有台账的稿子"},
        ]
        s = gate_v2.summary(arts)
        self.assertEqual(s["articles"], 4)
        self.assertEqual(s["fact_blocked"], 1)
        self.assertEqual(s["legacy_would_block"], 1)
        self.assertEqual(s["unjudged"], 1)   # 没有 _gate 的不算「未判定」
        self.assertEqual(s["mode"], "v2")
        self.assertIsInstance(json.dumps(s), str)


if __name__ == "__main__":
    unittest.main()
