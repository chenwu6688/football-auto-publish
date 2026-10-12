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


if __name__ == "__main__":
    unittest.main()
