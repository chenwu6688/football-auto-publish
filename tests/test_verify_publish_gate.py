"""发布闸门（时效台账 / 一致性台账 / 验收脚本）单测（计划 7.2 / 7.3）。"""

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import consistency_stats as cs
import freshness_ledger as fl

CST_FMT = "%Y-%m-%d %H:%M"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent.parent / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFreshnessLedger(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self._orig = fl.LEDGER_PATH
        fl.LEDGER_PATH = Path(self.d.name) / "fl.json"

    def tearDown(self):
        fl.LEDGER_PATH = self._orig
        self.d.cleanup()

    def test_note_and_summary(self):
        now = datetime(2026, 10, 12, 9, 0)
        fl.note(label="新闻流", checked=10, dropped_ages=[80, 100], unknown=1,
                kept_ages=[2, 5], max_hours=72, date=now)
        s = fl.summary(14, today=now)
        self.assertEqual(s["runs"], 1)
        self.assertEqual(s["checked"], 10)
        self.assertEqual(s["dropped"], 2)
        self.assertEqual(s["unknown"], 1)
        self.assertEqual(s["over_limit_runs"], 0)

    def test_over_limit_detected(self):
        now = datetime(2026, 10, 12, 9, 0)
        # 闸门本不该保留超限项；构造一条以验证台账能识别违规
        fl.note(checked=1, dropped_ages=[], unknown=0, kept_ages=[99], max_hours=72, date=now)
        self.assertEqual(len(fl.violations(14, today=now)), 1)

    def test_prune_old(self):
        old = datetime(2026, 8, 1, 9, 0)
        fl.note(checked=1, dropped_ages=[100], date=old)
        fl.note(checked=1, dropped_ages=[], kept_ages=[1], date=datetime(2026, 10, 12, 9, 0))
        self.assertEqual(len(fl.load()), 1, "超过 KEEP_DAYS 的记录应被裁剪")

    def test_missing_days(self):
        now = datetime(2026, 10, 12, 9, 0)
        fl.note(checked=1, kept_ages=[1], date=now)
        s = fl.summary(3, today=now)
        self.assertEqual(s["days_covered"], 1)
        self.assertEqual(len(s["missing_days"]), 2)


class TestConsistencyStats(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self._s, self._l = cs.STATS_PATH, cs.LABELS_PATH
        cs.STATS_PATH = Path(self.d.name) / "cs.json"
        cs.LABELS_PATH = Path(self.d.name) / "cl.json"

    def tearDown(self):
        cs.STATS_PATH, cs.LABELS_PATH = self._s, self._l
        self.d.cleanup()

    def test_reject_rate(self):
        cs.record(blocking=True, passed=True, issues=[])
        cs.record(blocking=True, passed=False, issues=["卡外比分"])
        cs.record(blocking=False, passed=True, issues=["warn"])
        s = cs.summary()
        self.assertEqual(s["blocking_checks"], 2)
        self.assertEqual(s["rejects"], 1)
        self.assertEqual(s["warn_only"], 1)
        self.assertAlmostEqual(s["reject_rate"], 0.5)

    def test_no_records(self):
        s = cs.summary()
        self.assertFalse(s["has_records"])
        self.assertIsNone(s["reject_rate"])
        self.assertEqual(s["reject_rate_display"], "未接入")

    def test_false_positive_rate_unlabeled(self):
        self.assertIsNone(cs.false_positive_rate(), "无标注应返回 None，绝不用 0 冒充")

    def test_false_positive_rate_labeled(self):
        cs.record_labels(labeled_rejects=20, false_positive=1)
        self.assertAlmostEqual(cs.false_positive_rate(), 0.05)
        cs.record_labels(labeled_rejects=20, false_positive=0)
        self.assertAlmostEqual(cs.false_positive_rate(), 1 / 40)
        self.assertEqual(cs.labels()["labeled_rejects"], 40)

    def test_prune(self):
        old = datetime(2026, 6, 1, 9, 0)
        cs.record(blocking=True, passed=False, issues=["x"], date=old)
        cs.record(blocking=True, passed=True, issues=[])
        self.assertEqual(len(cs.events()), 1)


class TestVerifyPublishGate(unittest.TestCase):
    """验收脚本自身的聚合逻辑（隔离数据后应可跑通）。"""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self._s, self._l = cs.STATS_PATH, cs.LABELS_PATH
        self._f = fl.LEDGER_PATH
        cs.STATS_PATH = Path(self.d.name) / "cs.json"
        cs.LABELS_PATH = Path(self.d.name) / "cl.json"
        fl.LEDGER_PATH = Path(self.d.name) / "fl.json"
        self.vg = _load("verify_publish_gate", "scripts/verify_publish_gate.py")

    def tearDown(self):
        cs.STATS_PATH, cs.LABELS_PATH = self._s, self._l
        fl.LEDGER_PATH = self._f
        self.d.cleanup()

    def test_report_shape_and_ok(self):
        rep = self.vg.verify()
        self.assertIn("ok", rep)
        self.assertIn("checks", rep)
        items = [c["item"] for c in rep["checks"]]
        self.assertTrue(any("零硬违规" in i for i in items))
        self.assertTrue(any("零旧闻新发" in i.replace("连续14天无", "") for i in items)
                        or any("旧闻新发" in i for i in items))

    def test_empty_env_passes(self):
        # 隔离环境（无产物/无台账）应全部跳过并通过
        self.assertTrue(self.vg.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
