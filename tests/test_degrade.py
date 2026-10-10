import unittest
from unittest import mock

import degrade


class TestDegradePolicy(unittest.TestCase):
    def test_policy_covers_plan_six_paths(self):
        for stage in ("赛程源", "事实层", "标题评分", "评分连续不过", "一致性校验", "调度层"):
            self.assertIn(stage, degrade.DEGRADE_POLICY)
            self.assertFalse(degrade.DEGRADE_POLICY[stage]["阻塞发布"])

    def test_note_drain_peek(self):
        degrade.drain()  # 清空
        degrade.note("事实层", "编译失败")
        self.assertEqual(len(degrade.peek()), 1)
        ev = degrade.drain()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["环节"], "事实层")
        self.assertEqual(degrade.peek(), [])

    def test_low_risk_section(self):
        self.assertIn(degrade.pick_low_risk_section(), degrade.LOW_RISK_SECTIONS)


class TestRewriteDowngrade(unittest.TestCase):
    """计划九：一致性校验打回 → 降级为低风险板块后重生成一次，仍不过丢弃。"""

    def test_consistency_fail_downgrades_then_drops(self):
        import orchestrator as o

        calls = {"n": 0}

        def fake_rewrite(topic, match_context, index, temperature=0.5,
                         retry_hint="", date_str="", source=None):
            calls["n"] += 1
            return {"title": "曼城主场能否啃下铁桶阵？",
                    "content": "正文" * 200, "content_type": topic.get("content_type"),
                    "ai_perspective": "后防才是问题。"}

        src = {"fixture": {"home_team": "曼城", "away_team": "利物浦",
                           "home_score": 2, "away_score": 1, "goals": [],
                           "source": "zhibo8", "article_text": "x" * 300,
                           "source_images": []}}
        topic = {"title": "t", "content_type": "热点球评"}

        degrade.drain()
        with mock.patch.object(o, "rewrite_article", side_effect=fake_rewrite), \
             mock.patch.object(o, "check_rewrite_fidelity", return_value=(True, [])), \
             mock.patch.object(o, "validate_article_vs_match_data", return_value=(False, ["卡外比分 9-0"])):
            art, err = o._rewrite_with_retry(topic, {"all_fixtures": [], "fixtures_by_league": {}},
                                             1, src, 0, "2026-10-10")
        self.assertEqual(art, {})
        self.assertIn("事实验证失败", err)
        # 选题被降级为低风险板块
        self.assertTrue(topic.get("_degraded"))
        self.assertIn(topic.get("content_type"), degrade.LOW_RISK_SECTIONS)
        # 记录了两条降级事件（先降级重生成、再丢弃）
        events = degrade.drain()
        stages = [e["环节"] for e in events]
        self.assertEqual(stages.count("一致性校验"), 2)


if __name__ == "__main__":
    unittest.main()
