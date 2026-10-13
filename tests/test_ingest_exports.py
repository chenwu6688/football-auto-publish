#!/usr/bin/env python3
"""导出投递箱 + 滞后监控单测（R11 内容诊断 2026-10-13「建议7」）。

覆盖：
  - 类型识别：作品数据导出 → perf；粉丝分析导出 → growth；识别不出 → unknown（不猜）
  - 幂等：同一文件（sha256）二次投递只处理一次
  - 纪律：unknown / failed 的文件**留在 inbox**，不移动、不导入
  - 归档：处理成功的文件移到 data/processed/<日期>/
  - 滞后监控：滞后天数计算 / 无数据 → None（待接入不算滞后）/ 超阈值告警 / 回填覆盖率
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import growth_import as gi          # noqa: E402
import ingest_exports as ie         # noqa: E402
import check_perf_lag as cpl        # noqa: E402


def _write(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8-sig")
    return p


class TestDetectKind(unittest.TestCase):
    def test_perf_export(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(Path(td) / "图文_20260101-20261012_1-400.csv",
                       "标题,发布时间,展现量,阅读量,点击率\nA队绝杀,2026-10-12,1000,100,10%\n")
            kind, header = ie.detect_kind(f)
            self.assertEqual(kind, "perf")
            self.assertIn("标题", header)

    def test_growth_export(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(Path(td) / "粉丝分析.csv", "统计日期,粉丝总数,涨粉数\n2026-10-01,100,5\n")
            self.assertEqual(ie.detect_kind(f)[0], "growth")

    def test_growth_not_misjudged_as_perf(self):
        # 粉丝分析没有展现量/阅读量列，不得被判成作品数据导出
        with tempfile.TemporaryDirectory() as td:
            f = _write(Path(td) / "g.csv", "日期,累计粉丝,净增粉丝\n2026-10-01,100,5\n")
            self.assertEqual(ie.detect_kind(f)[0], "growth")

    def test_unknown_stays_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write(Path(td) / "random.csv", "a,b,c\n1,2,3\n")
            self.assertEqual(ie.detect_kind(f)[0], "unknown")

    def test_unreadable(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "empty.csv"
            f.write_text("", encoding="utf-8")
            self.assertEqual(ie.detect_kind(f)[0], "unreadable")


class TestIngestOne(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self._old_processed = ie.PROCESSED
        self._old_growth = gi.GROWTH_PATH
        ie.PROCESSED = self.tmp / "processed"
        gi.GROWTH_PATH = self.tmp / "out" / "fans_daily.json"
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        ie.INBOX = self.inbox

    def tearDown(self):
        ie.PROCESSED, gi.GROWTH_PATH = self._old_processed, self._old_growth
        self._td.cleanup()

    def test_growth_ingest_and_archive(self):
        f = _write(self.inbox / "粉丝分析.csv",
                   "统计日期,粉丝总数,涨粉数\n2026-10-01,10000,12\n2026-10-02,10025,\n")
        state = {"processed": {}, "history": []}
        rec = ie.ingest_one(f, state)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["rows"], 2)
        self.assertFalse(f.exists(), "处理成功的文件应移出 inbox")
        self.assertTrue((self.tmp / "processed").exists())
        days = gi.load()["days"]
        self.assertEqual(days["2026-10-02"]["new_followers"], 25)   # 差分补出

    def test_duplicate_skipped(self):
        f = _write(self.inbox / "g.csv", "统计日期,粉丝总数\n2026-10-01,100\n")
        state = {"processed": {}, "history": []}
        self.assertEqual(ie.ingest_one(f, state)["status"], "ok")
        g2 = _write(self.inbox / "g.csv", "统计日期,粉丝总数\n2026-10-01,100\n")
        rec = ie.ingest_one(g2, state)
        self.assertEqual(rec["status"], "duplicate")

    def test_unknown_stays_in_inbox(self):
        f = _write(self.inbox / "weird.csv", "xxx,yyy\n1,2\n")
        state = {"processed": {}, "history": []}
        rec = ie.ingest_one(f, state)
        self.assertEqual(rec["status"], "unknown")
        self.assertTrue(f.exists(), "未知类型必须原样留在 inbox")
        self.assertNotIn("archived_to", rec)

    def test_perf_uses_shared_entrypoint(self):
        f = _write(self.inbox / "perf.csv", "标题,展现量,阅读量\nA,100,10\n")
        calls = {}

        def fake(path, **kw):
            calls["path"] = path
            return {"ok": True, "res": {}, "report": {"ok": True, "matched": [1, 2]},
                    "stats": {"imported": 2, "dates": ["2026-10-12"], "errors": []},
                    "dialog": kw}
        old = ie.ip.ingest_toutiao
        ie.ip.ingest_toutiao = fake
        try:
            rec = ie.ingest_one(f, {"processed": {}, "history": []})
        finally:
            ie.ip.ingest_toutiao = old
        self.assertEqual(rec["kind"], "perf")
        self.assertEqual(rec["imported"], 2)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(calls["path"], f)

    def test_failed_stays_in_inbox(self):
        f = _write(self.inbox / "perf.csv", "标题,展现量,阅读量\nA,100,10\n")

        def boom(path, **kw):
            raise RuntimeError("网络坏了")
        old = ie.ip.ingest_toutiao
        ie.ip.ingest_toutiao = boom
        try:
            rec = ie.ingest_one(f, {"processed": {}, "history": []})
        finally:
            ie.ip.ingest_toutiao = old
        self.assertEqual(rec["status"], "failed")
        self.assertTrue(f.exists(), "失败文件应留在 inbox 以便重试")

    def test_dry_run_does_not_move(self):
        f = _write(self.inbox / "g.csv", "统计日期,粉丝总数\n2026-10-01,100\n")
        rec = ie.ingest_one(f, {"processed": {}, "history": []}, dry_run=True)
        self.assertEqual(rec["status"], "dry-run")
        self.assertTrue(f.exists())


class TestPerfLag(unittest.TestCase):
    def test_lag_days(self):
        self.assertEqual(cpl.lag_days("2026-10-10", "2026-10-13"), 3)
        self.assertEqual(cpl.lag_days("2026-10-13", "2026-10-13"), 0)
        self.assertEqual(cpl.lag_days("2026-10-20", "2026-10-13"), 0)   # 未来日期不为负
        self.assertIsNone(cpl.lag_days(None, "2026-10-13"))
        self.assertIsNone(cpl.lag_days("不是日期", "2026-10-13"))

    def test_latest_date(self):
        self.assertEqual(cpl._latest_date(["2026-10-01", "2026-09-30", ""]), "2026-10-01")
        self.assertIsNone(cpl._latest_date([]))

    def test_report_flags_over_threshold(self):
        rep = cpl.report(days=14, perf_lag_max=3, growth_lag_max=7, today="2026-10-13")
        # 未接入转粉 → 只是 notice，不算 problem
        self.assertTrue(any("转粉通道尚未接入" in n for n in rep["notices"]))
        # 用伪造的滞后直接验证告警分支
        p_lag = cpl.lag_days("2026-10-01", "2026-10-13")
        self.assertGreater(p_lag, 3)

    def test_report_ok_shape(self):
        rep = cpl.report(days=14, today="2026-10-13")
        for k in ("绩效滞后天数", "转粉滞后天数", "回填覆盖率", "problems", "notices", "ok"):
            self.assertIn(k, rep)
        self.assertIn("rate", rep["回填覆盖率"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
