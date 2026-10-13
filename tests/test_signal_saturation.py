"""信号饱和度体检（scripts/verify_signal_saturation.py）单测 + merge 零填充回归。"""
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ranker                      # noqa: E402
import signal_sources as ss        # noqa: E402
import verify_signal_saturation as vss  # noqa: E402


def _table(n, field, value):
    return {f"e{i}": {field: value} for i in range(n)}


class TestDistribution(unittest.TestCase):
    def setUp(self):
        vss._install_isolated_env()

    def test_saturated_detected(self):
        d = vss._distribution(_table(12, "搜索指数", 100))["搜索指数"]
        self.assertEqual(d["n"], 12)
        self.assertEqual(d["mode"], 100)
        self.assertAlmostEqual(d["ratio"], 1.0)
        self.assertEqual(d["distinct"], 1)

    def test_varied_not_saturated(self):
        t = {f"e{i}": {"社媒讨论量": i * 1000} for i in range(12)}
        d = vss._distribution(t)["社媒讨论量"]
        self.assertEqual(d["n"], 12)
        self.assertLess(d["ratio"], vss.SATURATION_RATIO)   # 每种取值只出现一次
        self.assertEqual(d["distinct"], 12)

    def test_zero_coverage(self):
        d = vss._distribution(_table(5, "搜索指数", 100))["同题报道条数"]
        self.assertEqual(d["n"], 0)
        self.assertEqual(d["mode"], None)
        self.assertEqual(d["ratio"], 0.0)

    def test_missing_field_isolated(self):
        """只看「有该字段」的实体，不被无该字段的实体拉低分母。"""
        t = {f"e{i}": {"搜索指数": 100} for i in range(11)}
        t["eX"] = {"社媒讨论量": 5}                          # 无搜索指数，不入分母
        d = vss._distribution(t)["搜索指数"]
        self.assertEqual(d["n"], 11)
        self.assertAlmostEqual(d["ratio"], 1.0)


class TestHardSaturatedInert(unittest.TestCase):
    def setUp(self):
        vss._install_isolated_env()

    def test_no_saturated_passes(self):
        rep = vss.check_saturated_fields_inert(_table(12, "社媒讨论量", 1_000), [])
        self.assertTrue(rep["ok"])

    def test_saturated_but_inert_passes(self):
        """饱和字段若不参与打分（如已移出的搜索指数），硬项通过。"""
        rep = vss.check_saturated_fields_inert(_table(12, "搜索指数", 100), ["搜索指数"])
        self.assertTrue(rep["ok"], rep["detail"])

    def test_saturated_and_active_fails(self):
        """核心能力：饱和字段一旦被接回加分，硬项必须失败。

        社媒讨论量是活跃加分字段（_SOCIAL_FAME_BANDS）；造一批全取饱和值
        （20 万）的实体，去掉该字段后 rule_score 必然变化 → FAIL。
        """
        rep = vss.check_saturated_fields_inert(_table(12, "社媒讨论量", 200_000),
                                               ["社媒讨论量"])
        self.assertFalse(rep["ok"], rep["detail"])
        self.assertIn("饱和字段不得参与加分", " ".join(rep["detail"]))


class TestMergeNoZeroPad(unittest.TestCase):
    """merge_with_forwarded 不得用 0 冒充缺失（回归：2026-10-13 修正）。"""

    def test_new_entity_gets_no_default_record(self):
        out = ss.merge_with_forwarded({}, {"曼城": {"社媒讨论量": 1154911}})
        self.assertEqual(out["曼城"], {"社媒讨论量": 1154911})   # 无 0 填充字段

    def test_existing_entity_keeps_only_real_fields(self):
        out = ss.merge_with_forwarded({"梅西": {"社媒讨论量": 1000}},
                                      {"梅西": {"社媒讨论量": 500}})
        self.assertEqual(out["梅西"], {"社媒讨论量": 1000})      # 取较大值，不额外造键

    def test_missing_marker_survives_merge(self):
        """端到端：合并后交给 ranker，未命中字段必须仍被标 _signal_missing。"""
        vss._install_isolated_env()
        merged = ss.merge_with_forwarded({}, {"曼城": {"社媒讨论量": 200_000}})
        card = {"id": "m", "主体": "曼城", "动作": "转会",
                "来源列表": [{"来源名": "zhibo8", "地址": "http://z"}]}
        cand = {"card": card}
        ranker.l2_enrich([cand], merged)
        self.assertIn("搜索指数", cand["_signal_missing"])       # 未被 0 冒充
        self.assertNotIn("社媒讨论量", cand["_signal_missing"])  # 真实命中


class TestVerifyShape(unittest.TestCase):
    def setUp(self):
        vss._install_isolated_env()

    def test_report_shape(self):
        with mock.patch.object(vss.signal_sources, "load_external",
                               return_value=_table(12, "搜索指数", 100)):
            rep = vss.verify()
        self.assertIn("saturated", rep)
        self.assertIn("distribution", rep)
        self.assertEqual(rep["saturated"], ["搜索指数"])
        self.assertIsInstance(rep["ok"], bool)

    def test_empty_table_ok(self):
        with mock.patch.object(vss.signal_sources, "load_external", return_value={}):
            rep = vss.verify()
        self.assertTrue(rep["ok"])                               # 空表不阻断
        self.assertEqual(rep["saturated"], [])


if __name__ == "__main__":
    unittest.main()
