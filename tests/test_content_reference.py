"""内容参照层单测（计划 13.3）。"""

import tempfile
import unittest
from pathlib import Path

import content_reference as cr


class TestConfig(unittest.TestCase):
    """配置加载：参照项 / 排除项 / 五家媒体。"""

    def test_load_default_config(self):
        cr.reset_cache()
        refs = cr.reference_items()
        excs = cr.exclusion_items()
        media = cr.media_items()
        self.assertGreaterEqual(len(refs), 4, "参照项应覆盖四种方法论")
        self.assertGreaterEqual(len(excs), 4, "排除项应含四类")
        self.assertEqual(len(media), 5, "五家头部媒体")

    def test_reference_keys(self):
        keys = {r["key"] for r in cr.reference_items()}
        for k in ("信源标注规范", "标题信息结构", "话题时效判断", "栏目化与内容分层"):
            self.assertIn(k, keys)

    def test_exclusion_keys(self):
        keys = {e["key"] for e in cr.exclusion_items()}
        for k in ("中下游球队转会细节", "欧战赛前前瞻", "二线联赛赛事稿", "三千字深度长文"):
            self.assertIn(k, keys)

    def test_bad_path_returns_empty(self):
        cfg = cr.load_config("/nonexistent/xxx.yaml")
        self.assertEqual(cfg["参照项"], [])
        self.assertEqual(cfg["排除项"], [])


class TestGuidance(unittest.TestCase):
    def test_reference_guidance_mentions_methods(self):
        g = cr.reference_guidance()
        self.assertIn("信源标注规范", g)
        self.assertIn("懂球帝", g)
        self.assertIn("同质化", g)   # 风险提示

    def test_exclusion_guidance_lists_all(self):
        g = cr.exclusion_guidance()
        for k in ("欧战赛前前瞻", "二线联赛赛事稿", "三千字深度长文", "中下游球队转会细节"):
            self.assertIn(k, g)

    def test_guidance_combined(self):
        self.assertIn("可参照", cr.guidance())
        self.assertIn("严禁照搬", cr.guidance())


class TestCheckTopic(unittest.TestCase):
    """排除项判定：命中正确 + 不误杀。"""

    def test_preview_excluded(self):
        self.assertTrue(cr.is_excluded("皇马vs曼城前瞻：安帅的最后一搏？"))
        self.assertTrue(cr.is_excluded({"title": "赛前分析：拜仁能否终结连败"}))
        self.assertTrue(cr.is_excluded({"label": "🔮 赛前前瞻: 曼城 vs 阿森纳"}))

    def test_downstream_transfer_excluded(self):
        self.assertTrue(cr.is_excluded("保级队锋线引援：300万欧签下旧将"))
        self.assertTrue(cr.is_excluded("升班马转会细节曝光：中场全换血"))

    def test_downstream_only_no_transfer_passes(self):
        # 仅「升班马/保级队」无转会词 → 强队对决，不误杀
        self.assertFalse(cr.is_excluded("升班马教曼联做人！0-2开门黑"))
        self.assertFalse(cr.is_excluded("英超烧钱40亿欧！升班马比西甲三强还能花？"))

    def test_second_tier_league_needs_match_word(self):
        self.assertTrue(cr.is_excluded("英冠战报：利兹联绝杀登顶"))
        self.assertFalse(cr.is_excluded("英冠联赛的商业版图（转会视角）"))

    def test_long_article_by_keyword_and_length(self):
        self.assertTrue(cr.is_excluded("深度长文：解析曼城十年王朝"))
        self.assertTrue(cr.is_excluded({"title": "曼城十年史", "target_chars": 3200}))
        self.assertFalse(cr.is_excluded({"title": "曼城十年史", "target_chars": 1200}))

    def test_normal_topic_passes(self):
        for t in ("罗马诺：皇马4000万欧报价遭拒，中场补强为何卡壳？",
                  "姆巴佩伤停三周，巴黎锋线谁来扛？",
                  "拜仁回顾：三冠王的那个赛季"):
            self.assertFalse(cr.is_excluded(t), t)

    def test_content_type_not_matched(self):
        # content_type 含「转会」不应与「升班马」误撞（纪律 3）
        self.assertFalse(cr.is_excluded({"title": "曼联0-2升班马开门黑",
                                         "content_type": "转会资讯"}))

    def test_violation_shape(self):
        v = cr.check_topic("欧冠赛事前瞻：巴黎客战")
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["key"], "欧战赛前前瞻")
        self.assertIn("前瞻", v[0]["matched"])


class TestFilter(unittest.TestCase):
    def test_filter_topics(self):
        topics = [{"title": "皇马×曼城前瞻：谁能笑到最后？"},
                  {"title": "罗马诺：皇马锁定了新中场，4500万欧报价已递交"},
                  {"title": "英冠战报：利兹联登顶"}]
        kept, dropped = cr.filter_topics(topics)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 2)
        self.assertIn("_ref_violations", dropped[0][0])


class TestSelftest(unittest.TestCase):
    def test_selftest_passes(self):
        self.assertEqual(cr.selftest(), [])
        self.assertIn("通过", cr.render_selftest())


class TestCustomConfig(unittest.TestCase):
    def test_custom_config_path(self):
        yaml_text = (
            "参照项:\n  - key: 信源标注规范\n    desc: x\n"
            "排除项:\n  - key: 欧战赛前前瞻\n    reason: y\n    match_any: [前瞻]\n"
            "五家媒体: []\n风险提示: []\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cr.yaml"
            p.write_text(yaml_text, encoding="utf-8")
            cr.reset_cache()
            self.assertTrue(cr.is_excluded({"title": "赛前前瞻：X vs Y"}, path=p))
            self.assertFalse(cr.is_excluded({"title": "普通转会标题"}, path=p))
            cr.reset_cache()


if __name__ == "__main__":
    unittest.main()
