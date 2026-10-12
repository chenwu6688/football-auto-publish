"""事实层与量表验收单测（计划 6.1 / 6.2 / 12.2 / 12.4）。"""

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vfl = _load("verify_fact_layer", "scripts/verify_fact_layer.py")
import fact_card as fc  # noqa: E402

CST = timezone(timedelta(hours=8))


class TestFactLayer(unittest.TestCase):
    def test_fields(self):
        c = vfl.check_fields()
        self.assertTrue(c["ok"], c["detail"])
        for f in vfl.REQUIRED_FIELDS:
            self.assertIn(f, c["fields"])

    def test_confidence(self):
        self.assertTrue(vfl.check_confidence()["ok"])

    def test_lifecycle(self):
        c = vfl.check_lifecycle()
        self.assertTrue(c["ok"], c["detail"])
        self.assertEqual(len(c["states"]), 3)

    def test_dimensions(self):
        c = vfl.check_dimensions()
        self.assertTrue(c["ok"], c["detail"])
        self.assertEqual(set(c["dimensions"]), vfl.PLAN_DIMENSIONS)

    def test_topic_structure_advisory(self):
        c = vfl.check_topic_structure()
        self.assertTrue(c["advisory"])

    def test_verify_overall(self):
        rep = vfl.verify()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["hard_failed"], [])

    def test_render_md(self):
        md = vfl.render_md(vfl.verify())
        self.assertIn("事实层与量表验收", md)
        self.assertIn("五维", md)


class TestLifecycleBoundary(unittest.TestCase):
    """直接验证 compute_lifecycle 的边界语义，独立于验收脚本。"""

    def setUp(self):
        self.now = datetime(2026, 10, 12, 9, 0, tzinfo=CST)

    def _lc(self, days):
        t = (self.now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        return fc.compute_lifecycle({"发生时间": t}, self.now)

    def test_ongoing(self):
        self.assertEqual(self._lc(2), fc.LC_ONGOING)

    def test_settled(self):
        self.assertEqual(self._lc(8), fc.LC_SETTLED)
        self.assertEqual(self._lc(25), fc.LC_SETTLED)

    def test_expired(self):
        self.assertEqual(self._lc(40), fc.LC_EXPIRED)

    def test_unparseable_is_ongoing(self):
        self.assertEqual(fc.compute_lifecycle({"发生时间": ""}, self.now), fc.LC_ONGOING)


if __name__ == "__main__":
    unittest.main()
