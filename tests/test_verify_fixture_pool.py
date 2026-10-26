"""赛程选题池验收校验单测（计划 7.1）。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import verify_fixture_pool as vfp  # noqa: E402


def _t(comp, day, sec, angle="强强对话", conflict="A与B对撞"):
    return {"competition": comp, "kickoff_cst": f"{day} 20:00", "板块": sec,
            "angle_type": angle, "conflict": conflict, "topic_id": f"{comp}-{day}"}


class TestCoverage(unittest.TestCase):
    def test_all_five_covered(self):
        topics = [_t("英超", "2026-10-12", "战术榜单"), _t("欧冠", "2026-10-12", "战术榜单"),
                  _t("欧联", "2026-10-13", "战术榜单"), _t("中超", "2026-10-13", "中国足球"),
                  _t("世预赛", "2026-10-14", "中国足球")]
        c = vfp.check_coverage(topics)
        self.assertTrue(c["ok"])
        self.assertEqual(c["missing"], [])

    def test_missing_group_fails(self):
        topics = [_t("英超", "2026-10-12", "战术榜单")]
        c = vfp.check_coverage(topics)
        self.assertFalse(c["ok"])
        self.assertIn("欧联", c["missing"])
        self.assertIn("国足", c["missing"])


class TestPerDay(unittest.TestCase):
    def test_thin_day_detected(self):
        topics = [_t("英超", "2026-10-12", "战术榜单")] * 6 + [_t("西甲", "2026-10-13", "战术榜单")] * 2
        c = vfp.check_per_day(topics, min_per_day=6)
        self.assertFalse(c["ok"])
        self.assertIn("2026-10-13", c["thin_days"])

    def test_all_days_pass(self):
        topics = [_t("英超", "2026-10-12", "战术榜单")] * 6 + [_t("西甲", "2026-10-13", "战术榜单")] * 7
        c = vfp.check_per_day(topics, min_per_day=6)
        self.assertTrue(c["ok"])


class TestNoPreview(unittest.TestCase):
    def test_preview_marker_fails(self):
        topics = [_t("英超", "2026-10-12", "战术榜单", angle="赛事前瞻", conflict="赛前预测首发")]
        c = vfp.check_no_preview(topics)
        self.assertFalse(c["ok"])
        self.assertEqual(len(c["hits"]), 1)

    def test_no_preview_passes(self):
        topics = [_t("英超", "2026-10-12", "战术榜单", angle="强强对话")]
        self.assertTrue(vfp.check_no_preview(topics)["ok"])


class TestRatio(unittest.TestCase):
    def test_advisory_not_blocking_hard(self):
        # 配比项为告警项：即使严重偏离也不得让整体验收 FAIL（其余硬项全通过时）
        topics = ([_t("英超", "2026-10-12", "战术榜单")] * 3 +
                  [_t("欧冠", "2026-10-12", "战术榜单")] * 3 +
                  [_t("欧联", "2026-10-13", "战术榜单")] * 3 +
                  [_t("中超", "2026-10-13", "中国足球")] * 3 +
                  [_t("世预赛", "2026-10-14", "中国足球")] * 6)
        rep = vfp.verify({"topics": topics, "generated_at": "x"})
        self.assertTrue(rep["ok"])
        ratio = next(c for c in rep["checks"] if c.get("advisory"))
        self.assertFalse(ratio["ok"])          # 告警确实触发
        self.assertEqual(rep["hard_failed"], [])

    def test_ratio_deviation_computed(self):
        topics = [_t("英超", "2026-10-12", "战术榜单")] * 10
        c = vfp.check_ratio(topics)
        self.assertAlmostEqual(c["deviations"]["战术榜单"], 70.0, places=1)


class TestOverall(unittest.TestCase):
    def test_hard_failure_reflected(self):
        topics = [_t("英超", "2026-10-12", "战术榜单")] * 6   # 缺 4 类
        rep = vfp.verify({"topics": topics})
        self.assertFalse(rep["ok"])
        self.assertIn("五类赛程覆盖", rep["hard_failed"])

    def test_render_contains_verdict(self):
        topics = [_t("英超", "2026-10-12", "战术榜单")]
        md = vfp.render_md(vfp.verify({"topics": topics}))
        self.assertIn("未通过", md)
        self.assertIn("告警", md)


class TestAngleRules(unittest.TestCase):
    """R11 角度口径（建议3「换角度不换内容」）：赛程角度模板必须是「人 + 悬念」。"""

    def test_angle_rules_are_person_or_suspense(self):
        c = vfp.check_angle_rules()
        self.assertTrue(c["ok"], c.get("detail"))
        self.assertGreaterEqual(c["n_rules"], 5)

    def test_angle_ratio_advisory_when_old_wording(self):
        old = [_t("英超", "2026-10-12", "战术榜单",
                  conflict="{lower}的针对性打法，是{higher}这场最大的变数")]
        c = vfp.check_angle_person_suspense(old)
        self.assertTrue(c["advisory"])          # 只告警，不阻断
        self.assertFalse(c["ok"])
        self.assertEqual(c["hit"], 0)

    def test_angle_ratio_full_with_new_templates(self):
        """**每一条**模板都必须含人角色词或悬念词（P0-3 后每型多条，逐条校验）。

        模板从单条改成每型 3 条后，只校验「第一条」会漏掉新加的两条；
        这里把全部模板都格式化成 conflict，占比必须仍是 100%。
        """
        import fixture_library as fl
        topics = []
        for r in fl.ANGLE_RULES:
            templates = r[3] if isinstance(r[3], (list, tuple)) else [r[3]]
            for tmpl in templates:
                topics.append(_t("英超", "2026-10-12", "战术榜单",
                                 conflict=tmpl.format(home="A", away="B",
                                                      lower="B", higher="A")))
        c = vfp.check_angle_person_suspense(topics)
        self.assertTrue(c["ok"], f"这些模板缺人/悬念词：{c.get('detail')}")
        self.assertEqual(c["ratio"], 1.0)

    def test_every_angle_type_has_multiple_templates(self):
        """每型 ≥3 条模板——单一模板正是「19/26 篇同一句式」的结构性病根。"""
        import fixture_library as fl
        thin = [r[1] for r in fl.ANGLE_RULES
                if not isinstance(r[3], (list, tuple)) or len(r[3]) < 3]
        self.assertEqual(thin, [], f"这些分型模板不足 3 条：{thin}")


if __name__ == "__main__":
    unittest.main()
