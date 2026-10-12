"""主调度巡检单测（计划 3.3）。

重点锁定一个**已实际踩过的假阳性坑**：晚间目标 2026-10-12 由 17:30 调到 18:00，
若用新目标去判读 9 月数据，「17:40 = 17:30 + 10 分钟生成」会被误判成发散，
从而误报「主调度疑似失效」。故 targets_for 必须按日期取当时生效的目标。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import check_scheduler_health as csh  # noqa: E402


class TestTargetsFor(unittest.TestCase):
    def test_after_move_uses_1800(self):
        self.assertIn("18:00", csh.targets_for("2026-10-12"))
        self.assertNotIn("17:30", csh.targets_for("2026-10-12"))

    def test_before_move_uses_1730(self):
        t = csh.targets_for("2026-09-15")
        self.assertIn("17:30", t)
        self.assertNotIn("18:00", t)


class TestClassify(unittest.TestCase):
    def _m(self, hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    def test_september_evening_1740_is_on_target_not_offgrid(self):
        """9 月晚间目标 17:30，17:40 是准点（目标+10分钟生成耗时）。"""
        cls = csh.classify(self._m("17:40"), csh.targets_for("2026-09-15"))
        self.assertEqual(cls, "on_target")

    def test_october_evening_1805_is_on_target(self):
        cls = csh.classify(self._m("18:05"), csh.targets_for("2026-10-12"))
        self.assertEqual(cls, "on_target")

    def test_fallback_window_detected(self):
        cls = csh.classify(self._m("18:35"), csh.targets_for("2026-10-12"))
        self.assertEqual(cls, "fallback")

    def test_dead_zone_detected(self):
        cls = csh.classify(self._m("03:20"), csh.targets_for("2026-10-12"))
        self.assertEqual(cls, "dead_zone")

    def test_morning_noon_targets(self):
        self.assertEqual(csh.classify(self._m("08:07"), csh.targets_for("2026-10-12")),
                         "on_target")
        self.assertEqual(csh.classify(self._m("12:11"), csh.targets_for("2026-10-12")),
                         "on_target")


if __name__ == "__main__":
    unittest.main()
