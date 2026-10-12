#!/usr/bin/env python3
"""发布时段纪律单测（计划 3.3 / 第十章）。

覆盖：时刻解析与 UTC 换算、四处调度口径一致（constants / ratio_config /
wrangler / worker / batch.yml）、死区护栏存在且不与时段冲突。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


vg = _load("verify_publish_schedule", "scripts/verify_publish_schedule.py")


class TestParsing(unittest.TestCase):
    def test_hm(self):
        self.assertEqual(vg._hm("08:00"), (8, 0))
        self.assertEqual(vg._hm("18:00"), (18, 0))
        self.assertEqual(vg._hm(" 17:30 "), (17, 30))

    def test_hm_invalid(self):
        with self.assertRaises(ValueError):
            vg._hm("8点")

    def test_to_utc(self):
        self.assertEqual(vg._to_utc_hm(8, 0), (0, 0))
        self.assertEqual(vg._to_utc_hm(12, 0), (4, 0))
        self.assertEqual(vg._to_utc_hm(18, 0), (10, 0))
        self.assertEqual(vg._to_utc_hm(4, 30), (20, 30))  # 跨日


class TestScheduleConsistency(unittest.TestCase):
    def test_windows_shape(self):
        w = vg.windows()
        self.assertIsNotNone(w)
        for cn in ("晨读", "午间", "晚间"):
            self.assertIn(cn, w)
            self.assertTrue(w[cn].get("目标"))
        self.assertEqual(w["晚间"]["目标"], "18:00")
        self.assertEqual(w["死区"], ["00:00", "06:00"])

    def test_constants_match(self):
        self.assertTrue(vg.check_targets_match_constants()["ok"])

    def test_wrangler_crons(self):
        r = vg.check_wrangler()
        self.assertTrue(r["ok"], r["detail"])
        self.assertIn("10:00", r["crons_utc"])

    def test_worker_map(self):
        r = vg.check_worker()
        self.assertTrue(r["ok"], r["detail"])
        self.assertEqual(vg._worker_map().get(10), "evening")

    def test_backstop_crons(self):
        r = vg.check_backstop()
        self.assertTrue(r["ok"], r["detail"])
        self.assertIn((10, 30), vg._batch_yml_crons())
        self.assertIn((10, 50), vg._batch_yml_crons())

    def test_deadzone_guard(self):
        r = vg.check_deadzone_guard()
        self.assertTrue(r["ok"], r["detail"])

    def test_no_overlap(self):
        self.assertTrue(vg.check_no_overlap()["ok"])

    def test_verify_overall_ok(self):
        rep = vg.verify()
        self.assertTrue(rep["ok"], rep.get("hard_failed"))


class TestDeadzoneLogic(unittest.TestCase):
    def test_deadzone_boundary(self):
        # 00:00-05:59 属死区，06:00 起放行
        d0, d1 = vg._hm("00:00"), vg._hm("06:00")
        start, end = d0[0] * 60 + d0[1], d1[0] * 60 + d1[1]
        self.assertTrue(start <= 0 * 60 < end)
        self.assertTrue(start <= 5 * 60 + 59 < end)
        self.assertFalse(start <= 6 * 60 < end)

    def test_targets_outside_deadzone(self):
        w = vg.windows()
        d0, d1 = vg._hm(w["死区"][0]), vg._hm(w["死区"][1])
        start, end = d0[0] * 60 + d0[1], d1[0] * 60 + d1[1]
        for cn in ("晨读", "午间", "晚间"):
            t = vg._hm(w[cn]["目标"])
            self.assertFalse(start <= t[0] * 60 + t[1] < end, f"{cn} 目标落在死区")


if __name__ == "__main__":
    unittest.main(verbosity=2)
