"""调度时段解析（scripts/resolve_check_slot.py）单测。

事故背景（2026-10-23 用户收到的假警）：
  「2026-10-23 今日应完成批次（morning）全部缺失」——其实 10-23 的晨读批次
  07:00 才开始，而这条告警是 10-22 22:17 的终检被 GitHub 拖到 10-23 02:33
  才跑出来的。旧实现用「运行日/运行小时」判断目标日期与午检/终检，延迟跨日
  或延迟过 15 点就必然误报。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import resolve_check_slot as rcs  # noqa: E402

FINAL_CRON = "17 14 * * *"    # 22:17 CST 终检
MIDDAY_CRON = "37 4 * * *"    # 12:37 CST 午检


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


class TestParseCron(unittest.TestCase):
    def test_parses_hour_minute(self):
        self.assertEqual(rcs.parse_cron(FINAL_CRON), (14, 17))
        self.assertEqual(rcs.parse_cron(MIDDAY_CRON), (4, 37))

    def test_rejects_non_simple_expressions(self):
        for bad in ("", "   ", "*/5 * * * *", "abc def", "99 99 * * *"):
            self.assertIsNone(rcs.parse_cron(bad), bad)


class TestResolveSlot(unittest.TestCase):
    def test_final_slot_on_time(self):
        self.assertEqual(rcs.resolve_slot(FINAL_CRON, utc("2026-10-22T14:20:00Z")),
                         ("2026-10-22", "final"))

    def test_final_slot_delayed_past_midnight_keeps_previous_day(self):
        """10-23 02:33 CST（= 10-22 18:33Z）：这条跑的是 10-22 的终检。"""
        self.assertEqual(rcs.resolve_slot(FINAL_CRON, utc("2026-10-22T18:33:00Z")),
                         ("2026-10-22", "final"))

    def test_midday_slot_on_time(self):
        self.assertEqual(rcs.resolve_slot(MIDDAY_CRON, utc("2026-10-22T04:40:00Z")),
                         ("2026-10-22", "midday"))

    def test_midday_delayed_past_15_cst_still_midday(self):
        """10-21/10-22 实测：12:37 的午检拖到 17:3x 才跑，不能当成终检。

        按运行小时判断会 expect=evening → 而 evening 批次此时还没开始 → 假警。
        """
        self.assertEqual(rcs.resolve_slot(MIDDAY_CRON, utc("2026-10-22T09:33:00Z")),
                         ("2026-10-22", "midday"))

    def test_slot_exactly_now_counts_as_today(self):
        self.assertEqual(rcs.resolve_slot(FINAL_CRON, utc("2026-10-22T14:17:00Z")),
                         ("2026-10-22", "final"))

    def test_slot_spans_month_boundary(self):
        """11-01 08:10 CST 跑的是 10-31 22:17 的终检。"""
        self.assertEqual(rcs.resolve_slot(FINAL_CRON, utc("2026-11-01T00:10:00Z")),
                         ("2026-10-31", "final"))

    def test_other_cron_time_is_followed(self):
        """cron 改了也跟着走（collector 06:05Z = 14:05 CST，属午检段）。"""
        self.assertEqual(rcs.resolve_slot("5 6 * * *", utc("2026-10-22T06:10:00Z")),
                         ("2026-10-22", "midday"))

    def test_dispatch_at_night_targets_previous_day(self):
        """手工 dispatch 在 02:33 CST：只可能想看前一天的终检。"""
        self.assertEqual(rcs.resolve_slot("", utc("2026-10-22T18:33:00Z")),
                         ("2026-10-22", "final"))

    def test_dispatch_daytime_uses_today(self):
        self.assertEqual(rcs.resolve_slot("", utc("2026-10-23T01:00:00Z")),
                         ("2026-10-23", "midday"))
        self.assertEqual(rcs.resolve_slot("", utc("2026-10-23T12:00:00Z")),
                         ("2026-10-23", "final"))

    def test_unknown_cron_falls_back_to_now(self):
        self.assertEqual(rcs.resolve_slot("*/5 * * * *", utc("2026-10-23T01:00:00Z")),
                         ("2026-10-23", "midday"))

    def test_naive_datetime_treated_as_utc(self):
        self.assertEqual(rcs.resolve_slot(FINAL_CRON, datetime(2026, 10, 22, 18, 33)),
                         ("2026-10-22", "final"))


class TestCli(unittest.TestCase):
    def test_prints_date_and_period(self):
        out = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "resolve_check_slot.py"),
             "--schedule", FINAL_CRON, "--now", "2026-10-22T18:33:00Z"],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "2026-10-22 final")


if __name__ == "__main__":
    unittest.main()
