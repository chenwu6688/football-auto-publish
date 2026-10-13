#!/usr/bin/env python3
"""转粉通道单测（R11 内容诊断 2026-10-13「建议5」）。

覆盖：
  - 占位符 -1 / -1.00 / 空 / -- → None（待接入）；真实 0 保留
  - 表头最长匹配（粉丝总数 不被「粉丝数」吃掉）
  - 只有粉丝总数时按相邻已登记日差分；缺失日不插值
  - 合并写：None 不覆盖真实值、已有日期不重复登记
  - 看板口径：文章级优先 / 账号级兜底 / 两者皆无 → None（绝不 0）
  - 生成侧：缺列（如头条导出无「涨粉数」）不得填 0
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import growth_import as gi          # noqa: E402
import toutiao_import as tti        # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import build_metrics_dashboard as bmd  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_placeholder_is_none(self):
        for v in (-1, "-1", "-1.00", "-1.0", "", " ", "--", "—", "N/A", "abc", None, "无"):
            with self.subTest(v=v):
                self.assertIsNone(gi.normalize_count(v))

    def test_real_zero_kept(self):
        self.assertEqual(gi.normalize_count(0), 0)
        self.assertEqual(gi.normalize_count("0"), 0)
        self.assertEqual(gi.normalize_count("1,234"), 1234)

    def test_header_longest_match(self):
        self.assertEqual(gi.map_header("粉丝总数")[0], "total_followers")
        self.assertEqual(gi.map_header("累计粉丝数")[0], "total_followers")
        self.assertEqual(gi.map_header("粉丝涨粉数")[0], "new_followers")
        self.assertEqual(gi.map_header("涨粉数")[0], "new_followers")

    def test_ignored_columns_shadow(self):
        self.assertEqual(gi.map_header("粉丝展现量"), (None, True))
        self.assertEqual(gi.map_header("取关数"), (None, True))

    def test_normalize_date(self):
        self.assertEqual(gi.normalize_date("2026-10-03"), "2026-10-03")
        self.assertEqual(gi.normalize_date("2026/10/3"), "2026-10-03")
        self.assertEqual(gi.normalize_date("2026年10月3日"), "2026-10-03")
        self.assertEqual(gi.normalize_date("20261003"), "2026-10-03")
        self.assertIsNone(gi.normalize_date("昨天"))


class TestParseAndDerive(unittest.TestCase):
    ROWS = [["统计日期", "粉丝总数", "涨粉数"],
            ["2026-10-01", 10000, 12],
            ["2026-10-02", 10025, None],
            ["2026-10-03", 10060, 35],
            ["2026-10-04", -1, -1]]

    def test_parse_skips_all_missing_rows(self):
        recs = gi.parse_rows(self.ROWS)
        self.assertEqual([r["date"] for r in recs], ["2026-10-01", "2026-10-02", "2026-10-03"])
        # 解析阶段不猜涨粉
        self.assertIsNone(recs[1]["new_followers"])

    def test_derive_only_for_totals(self):
        days = {r["date"]: {"total_followers": r["total_followers"],
                            "new_followers": r["new_followers"]} for r in gi.parse_rows(self.ROWS)}
        filled = gi.derive_deltas(days)
        self.assertEqual(filled, ["2026-10-02"])
        self.assertEqual(days["2026-10-02"]["new_followers"], 25)
        self.assertEqual(days["2026-10-02"]["derived"], "total_delta")
        self.assertEqual(days["2026-10-03"]["new_followers"], 35)   # 真实值不被覆盖

    def test_no_backfill_across_gap(self):
        # 10-01 与 10-05 之间有缺口：只按相邻已登记日差分，不插值中间的日期
        days = {"2026-10-01": {"total_followers": 100, "new_followers": None},
                "2026-10-05": {"total_followers": 140, "new_followers": None}}
        gi.derive_deltas(days)
        self.assertEqual(days["2026-10-05"]["new_followers"], 40)
        self.assertEqual(sorted(days.keys()), ["2026-10-01", "2026-10-05"])

    def test_merge_does_not_overwrite_with_none(self):
        data = {"days": {"2026-10-01": {"total_followers": 100, "new_followers": 12}}}
        gi.merge(data, [{"date": "2026-10-01", "total_followers": None, "new_followers": None}])
        self.assertEqual(data["days"]["2026-10-01"]["new_followers"], 12)

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "fans_daily.json"
            data = gi.merge(gi.load(p), [{"date": "2026-10-01", "total_followers": 100,
                                          "new_followers": 7}], source="t.csv")
            gi.save(data, p)
            back = gi.load(p)
            self.assertEqual(back["days"]["2026-10-01"]["new_followers"], 7)
            self.assertEqual(gi.daily_new_followers(p), {"2026-10-01": 7})

    def test_daily_skips_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "f.json"
            gi.save({"days": {"2026-10-01": {"total_followers": 100, "new_followers": None},
                              "2026-10-02": {"total_followers": 110, "new_followers": 0}}}, p)
            self.assertEqual(gi.daily_new_followers(p), {"2026-10-02": 0})

    def test_selftest_all_pass(self):
        bad = [n for n, ok in gi.selftest() if not ok]
        self.assertEqual(bad, [], f"冒烟未通过：{bad}")


class TestToutiaoMissingColumn(unittest.TestCase):
    """头条图文导出没有「涨粉数」列——必须留 None，绝不填 0。"""

    def test_missing_followers_column_is_none(self):
        rec = {}
        cells = {"reads": "100", "likes": "5", "impressions": "1000"}   # 无 new_followers 列
        tti._fill_metrics(rec, lambda f: cells.get(f, ""))
        self.assertEqual(rec["reads"], 100)
        self.assertIsNone(rec["new_followers"], "缺「涨粉数」列应留 None（待接入），不得填 0")
        self.assertIsNone(rec["follower_reads"])

    def test_real_zero_kept(self):
        rec = {}
        tti._fill_metrics(rec, lambda f: {"new_followers": "0"}.get(f, ""))
        self.assertEqual(rec["new_followers"], 0)

    def test_negative_placeholder_is_none(self):
        rec = {}
        tti._fill_metrics(rec, lambda f: {"new_followers": "-1"}.get(f, ""))
        self.assertIsNone(rec["new_followers"])


class TestDashboardGrowth(unittest.TestCase):
    """看板「日均涨粉」口径：文章级 → 账号级 → None（绝不 0）。"""

    def _collect_with(self, tmp: Path, *, account_days=None):
        growth_path = Path(tmp) / "fans_daily.json"
        if account_days:
            gi.save({"days": {d: {"total_followers": 1000 * i, "new_followers": v}
                              for i, (d, v) in enumerate(account_days.items(), 1)}}, growth_path)
        orig_out, orig_growth = bmd.OUTPUT_DIR, gi.GROWTH_PATH
        bmd.OUTPUT_DIR = Path(tmp) / "output"
        gi.GROWTH_PATH = growth_path
        try:
            (bmd.OUTPUT_DIR / "2026-10-10").mkdir(parents=True, exist_ok=True)
            (bmd.OUTPUT_DIR / "2026-10-10" / "metadata.json").write_text(
                '{"total_articles": 2, "batches_completed": [], "articles": [], "topics": []}',
                encoding="utf-8")
            return bmd.collect(30, now=bmd.datetime(2026, 10, 13, 12, 0, tzinfo=bmd.CST))
        finally:
            bmd.OUTPUT_DIR, gi.GROWTH_PATH = orig_out, orig_growth

    def test_no_data_is_none_not_zero(self):
        with tempfile.TemporaryDirectory() as td:
            g = self._collect_with(Path(td))["growth"]
            self.assertIsNone(g["日均涨粉"])
            self.assertIsNone(g["涨粉数据来源"])

    def test_account_channel_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            g = self._collect_with(Path(td),
                                   account_days={"2026-10-09": 10, "2026-10-10": 20})["growth"]
            self.assertEqual(g["日均涨粉"], 15.0)          # (10+20)/2
            self.assertEqual(g["涨粉数据来源"], "粉丝分析通道")
            self.assertEqual(g["涨粉样本天数"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
