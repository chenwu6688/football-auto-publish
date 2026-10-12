#!/usr/bin/env python3
"""单批篇数纪律测试（策略手册 第五章 / 完善计划 12.1）。

覆盖：常态与加发上限裁定、白名单、周四自动加发、周内额度、晚间不加发、
日合计受计划 6-8 约束、台账周内计数与留痕完整性、写入失败降级。

台账一律写到临时目录，绝不污染 data/uplift_ledger.json。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import batch_quota as bq  # noqa: E402
from constants import BATCH_CONFIG, BATCH_QUOTA  # noqa: E402

THURSDAY = 3
MONDAY = 0


class LedgerFixture(unittest.TestCase):
    """把台账路径指向临时文件，保证测试不写真实台账。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = bq.LEDGER_PATH
        bq.LEDGER_PATH = Path(self._tmp.name) / "uplift_ledger.json"

    def tearDown(self):
        bq.LEDGER_PATH = self._orig
        self._tmp.cleanup()


class TestPolicy(LedgerFixture):
    def test_normal_max_is_two(self):
        self.assertEqual(bq.normal_max(), 2)

    def test_uplift_caps_match_manual(self):
        self.assertEqual(bq.uplift_max("morning"), 3)
        self.assertEqual(bq.uplift_max("noon"), 3)
        self.assertEqual(bq.uplift_max("evening"), 2)

    def test_unknown_batch_never_uplifts(self):
        self.assertEqual(bq.uplift_max("unknown"), bq.normal_max())

    def test_daily_bounds_are_plan_values(self):
        self.assertEqual(bq.daily_bounds(), (6, 8))

    def test_reason_whitelist(self):
        self.assertEqual(set(bq.allowed_reasons()), {"thursday_boost", "s_level_event"})
        self.assertEqual(bq.weekly_limit(), 2)

    def test_reason_matching_is_exact(self):
        self.assertTrue(bq.is_uplift_reason("thursday_boost"))
        self.assertFalse(bq.is_uplift_reason("thursday"))
        self.assertFalse(bq.is_uplift_reason(""))
        self.assertFalse(bq.is_uplift_reason(None))

    def test_auto_reason_only_thursday(self):
        self.assertEqual(bq.auto_reason(THURSDAY), "thursday_boost")
        self.assertIsNone(bq.auto_reason(MONDAY))
        self.assertIsNone(bq.auto_reason(None))


class TestDecide(LedgerFixture):
    def test_normal_day_capped_at_two(self):
        for batch in ("morning", "noon", "evening"):
            d = bq.decide(batch, weekday=MONDAY)
            self.assertEqual(d["max_articles"], 2, batch)
            self.assertFalse(d["uplifted"], batch)

    def test_thursday_auto_uplift_for_morning_and_noon(self):
        for batch in ("morning", "noon"):
            d = bq.decide(batch, weekday=THURSDAY)
            self.assertEqual(d["max_articles"], 3, batch)
            self.assertTrue(d["uplifted"], batch)
            self.assertEqual(d["reason"], "thursday_boost")

    def test_evening_never_uplifts_on_thursday(self):
        d = bq.decide("evening", weekday=THURSDAY)
        self.assertEqual(d["max_articles"], 2)
        self.assertFalse(d["uplifted"])

    def test_explicit_s_level_event_uplifts(self):
        d = bq.decide("noon", weekday=MONDAY, uplift_reason="s_level_event")
        self.assertEqual(d["max_articles"], 3)
        self.assertTrue(d["uplifted"])
        self.assertEqual(d["reason"], "s_level_event")

    def test_off_whitelist_reason_is_rejected(self):
        d = bq.decide("morning", weekday=MONDAY, uplift_reason="想多发几篇")
        self.assertEqual(d["max_articles"], 2)
        self.assertFalse(d["uplifted"])
        self.assertIn("白名单", d["detail"])

    def test_weekly_exhausted_blocks_uplift(self):
        d = bq.decide("morning", weekday=THURSDAY, weekly_uplifted=bq.weekly_limit())
        self.assertEqual(d["max_articles"], 2)
        self.assertFalse(d["uplifted"])
        self.assertIn("已用满", d["detail"])

    def test_unknown_weekday_does_not_auto_uplift(self):
        d = bq.decide("morning", weekday=None)
        self.assertEqual(d["max_articles"], 2)

    def test_daily_ceiling_within_plan_range(self):
        total = sum(bq.decide(b, weekday=THURSDAY)["max_articles"]
                    for b in ("morning", "noon", "evening"))
        lo, hi = bq.daily_bounds()
        self.assertEqual(total, 8)
        self.assertLessEqual(total, hi)
        self.assertGreaterEqual(total, lo)

    def test_weekly_used_increments_only_when_uplifted(self):
        self.assertEqual(bq.decide("morning", weekday=THURSDAY, weekly_uplifted=0)["weekly_used"], 1)
        self.assertEqual(bq.decide("morning", weekday=MONDAY, weekly_uplifted=0)["weekly_used"], 0)

    def test_decision_carries_source_and_caps(self):
        d = bq.decide("morning", weekday=MONDAY)
        self.assertIn("策略手册", d["source"])
        self.assertEqual(d["normal_max"], 2)
        self.assertEqual(d["uplift_max"], 3)


class TestLedger(LedgerFixture):
    def test_record_and_read_back(self):
        bq.record("2026-10-15", "morning", "thursday_boost", articles=3)
        recs = bq.records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["batch"], "morning")
        self.assertEqual(recs[0]["articles"], 3)

    def test_weekly_count_isolated_by_week(self):
        bq.record("2026-10-15", "morning", "thursday_boost")   # 同周（周四）
        bq.record("2026-10-15", "noon", "thursday_boost")      # 同周
        bq.record("2026-10-08", "morning", "thursday_boost")   # 上一周
        self.assertEqual(bq.weekly_uplifted("2026-10-15"), 2)
        self.assertEqual(bq.weekly_uplifted("2026-10-08"), 1)

    def test_weekly_count_on_missing_ledger(self):
        self.assertEqual(bq.weekly_uplifted("2026-10-15"), 0)

    def test_summarize_flags_incomplete_records(self):
        path = bq.LEDGER_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"records": [
            {"date": "2026-10-15", "batch": "", "reason": "thursday_boost"},
        ]}, ensure_ascii=False), encoding="utf-8")
        s = bq.summarize(date_str="2026-10-15")
        self.assertEqual(len(s["incomplete"]), 1)
        self.assertTrue(s["within_limit"])

    def test_summarize_respects_weekly_limit(self):
        for i in range(3):
            bq.record("2026-10-15", "morning", "thursday_boost")
        s = bq.summarize(date_str="2026-10-15")
        self.assertEqual(s["this_week"], 3)
        self.assertFalse(s["within_limit"])

    def test_record_failure_does_not_raise(self):
        bq.LEDGER_PATH = Path("/proc/definitely/not/writable/uplift.json")
        entry = bq.record("2026-10-15", "morning", "thursday_boost", articles=3)
        self.assertEqual(entry["batch"], "morning")


class TestRuntimeConfig(LedgerFixture):
    def test_batch_config_caps_are_two(self):
        for batch in ("morning", "noon", "evening"):
            self.assertEqual(BATCH_CONFIG[batch]["max_articles"], 2, batch)

    def test_batch_config_matches_quota_policy(self):
        self.assertEqual(BATCH_CONFIG["morning"]["max_articles"], BATCH_QUOTA["normal_max"])
        self.assertEqual(BATCH_CONFIG["noon"]["max_articles"], BATCH_QUOTA["normal_max"])
        self.assertEqual(BATCH_CONFIG["evening"]["max_articles"], BATCH_QUOTA["normal_max"])

    def test_quota_declares_plan_source(self):
        self.assertIn("12.1", BATCH_QUOTA["source"])
        self.assertIn("策略手册", BATCH_QUOTA["source"])

    def test_selftest_all_pass(self):
        bad = [name for name, ok in bq.selftest() if not ok]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
