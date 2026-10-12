"""自动调参纪律与四道防呆验收单测（计划 8.1 / 8.2 / 11.5）。"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vtd = _load("verify_tuning_discipline", "scripts/verify_tuning_discipline.py")
import ratio_tuner as rt  # noqa: E402


class TestChecks(unittest.TestCase):
    def test_config_values_ok(self):
        c = vtd.check_config()
        self.assertTrue(c["ok"], c["detail"])
        self.assertEqual(len(c["rows"]), 6)

    def test_exec_points_present(self):
        self.assertTrue(vtd.check_exec()["ok"])

    def test_four_guards_pass(self):
        c = vtd.check_four_guards()
        self.assertTrue(c["ok"], [x for x in c["cases"] if not x["ok"]])
        self.assertEqual(len(c["cases"]), 4)

    def test_layered_metrics_advisory(self):
        c = vtd.check_layered_metrics()
        self.assertTrue(c["advisory"])
        self.assertEqual(len(c["layers"]), 3)

    def test_verify_overall(self):
        rep = vtd.verify()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["hard_failed"], [])

    def test_config_detects_wrong_value(self):
        orig = rt.load_config
        try:
            rt.load_config = lambda *a, **k: {"调整纪律": {"最小样本": 5}}
            c = vtd.check_config()
            self.assertFalse(c["ok"])
        finally:
            rt.load_config = orig

    def test_render_md(self):
        md = vtd.render_md(vtd.verify())
        self.assertIn("四道防呆", md)
        self.assertIn("通过", md)


class TestSmoke(unittest.TestCase):
    def test_freeze(self):
        self.assertTrue(vtd._smoke_freeze()["ok"])

    def test_deadzone(self):
        self.assertTrue(vtd._smoke_deadzone()["ok"])

    def test_amplitude_and_quota(self):
        self.assertTrue(vtd._smoke_amplitude()["ok"])

    def test_cooldown(self):
        self.assertTrue(vtd._smoke_cooldown()["ok"])


if __name__ == "__main__":
    unittest.main()
