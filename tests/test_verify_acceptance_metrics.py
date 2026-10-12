"""验收指标与合规熔断验收单测（计划第十章）。"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vam = _load("verify_acceptance_metrics", "scripts/verify_acceptance_metrics.py")
import compliance_guard as cg  # noqa: E402


class TestAcceptanceMetrics(unittest.TestCase):
    def test_compliance_redline(self):
        c = vam.check_compliance_redline()
        self.assertTrue(c["ok"], c["detail"])

    def test_targets(self):
        c = vam.check_targets()
        self.assertTrue(c["ok"], c["detail"])
        self.assertEqual(set(c["targets"].keys()), set(vam.PLAN_TARGETS.keys()))

    def test_no_fake_zero(self):
        self.assertTrue(vam.check_no_fake_zero()["ok"])

    def test_red_line_semantics(self):
        self.assertTrue(vam.check_red_line_semantics()["ok"])

    def test_verify_overall(self):
        rep = vam.verify()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["hard_failed"], [])

    def test_does_not_pollute_state(self):
        """校验过程必须使用隔离状态文件，不得污染真实 compliance_state.json。"""
        before = cg.STATE_PATH
        vam.verify()
        self.assertEqual(cg.STATE_PATH, before)

    def test_render_md(self):
        md = vam.render_md(vam.verify())
        self.assertIn("合规红线", md)
        self.assertIn("第十章", md)


class TestComplianceGuardUnit(unittest.TestCase):
    """直接锚定熔断语义：出现一次不实违规即暂停，解除后恢复。"""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.p = Path(self.d.name) / "s.json"

    def tearDown(self):
        self.d.cleanup()

    def test_record_suspends(self):
        self.assertFalse(cg.is_suspended(self.p))
        cg.record_violation(cg.FALSE_CONTENT, "t", path=self.p)
        self.assertTrue(cg.is_suspended(self.p))
        self.assertEqual(cg.status(self.p)["false_content_count"], 1)

    def test_clear_resumes(self):
        cg.record_violation(cg.FALSE_CONTENT, "t", path=self.p)
        cg.clear("tester", path=self.p)
        self.assertFalse(cg.is_suspended(self.p))

    def test_systemic_threshold(self):
        self.assertFalse(cg.auto_suspend_if_systemic(cg.AUTO_SUSPEND_REJECT_THRESHOLD - 1, path=self.p))
        self.assertTrue(cg.auto_suspend_if_systemic(cg.AUTO_SUSPEND_REJECT_THRESHOLD, path=self.p))


if __name__ == "__main__":
    unittest.main()
