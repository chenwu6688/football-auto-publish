#!/usr/bin/env python3
"""周更一键闭环单测（R11 建议5+7 收口）。

覆盖「用户只交文件，剩下全自动」这条链路的正确性与**副作用边界**：
  - collect_from：同内容幂等跳过、同名不同内容加哈希后缀不覆盖
  - run(dry_run)：绝不写库 / 不写产物
  - run 端到端：投递粉丝分析 CSV → data/growth 落盘 + 汇总报告生成 + ok=True
  - 识别不出的文件：留箱 + ok=False（不猜、不吞）
  - 待接入语义：转粉无数据时显示「待接入」，绝不显示 0
  - 台账隔离：所有路径指向 tempfile，不污染真实 data/
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import check_perf_lag as cpl          # noqa: E402
import growth_import as gi            # noqa: E402
import ingest_exports as ie           # noqa: E402
import weekly_review as wr            # noqa: E402


GROWTH_CSV = """统计日期,粉丝总数,涨粉数
2026-10-08,10000,12
2026-10-09,10025,
2026-10-10,10060,35
"""


class TempEnv:
    """把周更链路的所有路径指到 tempfile（台账隔离，绝不碰真实 data/）。"""

    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.inbox = self.root / "inbox"
        self.processed = self.root / "processed"
        self.state = self.root / "ingest_state.json"
        self.growth = self.root / "growth" / "fans_daily.json"
        self.output = self.root / "output"
        self.audit = self.root / "audit"
        self.perf_log = self.output / "performance_log.json"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(parents=True, exist_ok=True)
        self.perf_log.write_text('{"articles": {}}', encoding="utf-8")

    def __enter__(self):
        self._saved = dict(
            ie_inbox=ie.INBOX, ie_processed=ie.PROCESSED, ie_state=ie.STATE_PATH,
            gi_growth=gi.GROWTH_PATH, cpl_perf=cpl.PERF_LOG, cpl_out=cpl.OUTPUT_DIR,
            wr_inbox=wr.INBOX, wr_audit=wr.AUDIT_DIR,
        )
        ie.INBOX, ie.PROCESSED, ie.STATE_PATH = self.inbox, self.processed, self.state
        gi.GROWTH_PATH = self.growth
        cpl.PERF_LOG, cpl.OUTPUT_DIR = self.perf_log, self.output
        wr.INBOX, wr.AUDIT_DIR = self.inbox, self.audit
        return self

    def __exit__(self, *exc):
        s = self._saved
        ie.INBOX, ie.PROCESSED, ie.STATE_PATH = s["ie_inbox"], s["ie_processed"], s["ie_state"]
        gi.GROWTH_PATH = s["gi_growth"]
        cpl.PERF_LOG, cpl.OUTPUT_DIR = s["cpl_perf"], s["cpl_out"]
        wr.INBOX, wr.AUDIT_DIR = s["wr_inbox"], s["wr_audit"]
        self._td.cleanup()
        return False

    def put(self, name: str, text: str) -> Path:
        p = self.inbox / name
        p.write_text(text, encoding="utf-8")
        return p


class TestCollectFrom(unittest.TestCase):
    def test_collect_copies_new_files(self):
        with TempEnv() as env, tempfile.TemporaryDirectory() as sd:
            src = Path(sd) / "粉丝分析.csv"
            src.write_text(GROWTH_CSV, encoding="utf-8")
            got = wr.collect_from(src)
            self.assertEqual(got, ["粉丝分析.csv"])
            self.assertTrue((env.inbox / "粉丝分析.csv").exists())

    def test_collect_is_idempotent_for_same_content(self):
        with TempEnv() as env, tempfile.TemporaryDirectory() as sd:
            src = Path(sd) / "a.csv"
            src.write_text(GROWTH_CSV, encoding="utf-8")
            wr.collect_from(src)
            self.assertEqual(wr.collect_from(src), [], "同一份文件第二次应收空（幂等）")
            self.assertEqual(len(list(env.inbox.glob("a*"))), 1, "不应产生副本")

    def test_same_name_different_content_gets_hash_suffix(self):
        with TempEnv() as env, tempfile.TemporaryDirectory() as sd:
            src = Path(sd) / "a.csv"
            src.write_text(GROWTH_CSV, encoding="utf-8")
            wr.collect_from(src)
            src.write_text(GROWTH_CSV.replace("10000", "20000"), encoding="utf-8")
            got = wr.collect_from(src)
            self.assertEqual(len(got), 1)
            self.assertNotEqual(got[0], "a.csv", "同名不同内容必须换名，绝不覆盖")
            self.assertEqual(len(list(env.inbox.glob("a*"))), 2)

    def test_missing_source_is_noop(self):
        with TempEnv():
            self.assertEqual(wr.collect_from("/nonexistent/path/xyz"), [])


class TestRunDryRun(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with TempEnv() as env:
            env.put("粉丝分析.csv", GROWTH_CSV)
            before = sorted(p.name for p in env.inbox.iterdir())
            out = wr.run(dry_run=True)
            self.assertEqual(sorted(p.name for p in env.inbox.iterdir()), before,
                             "dry-run 不得移动/删除 inbox 文件")
            self.assertFalse(env.state.exists(), "dry-run 不写幂等台账")
            self.assertFalse(env.growth.exists(), "dry-run 不写转粉数据")
            self.assertFalse(env.audit.exists(), "dry-run 不写汇总报告")
            self.assertNotIn("report", out)


class TestRunEndToEnd(unittest.TestCase):
    def test_growth_file_lands_and_report_written(self):
        with TempEnv() as env:
            env.put("粉丝分析.csv", GROWTH_CSV)
            out = wr.run(src=None, write_dashboard=False, tuner=False)
            self.assertTrue(out["ok"], out.get("failures"))
            # ① 数据真的落盘
            days = gi.load(env.growth)["days"]
            self.assertEqual(sorted(days), ["2026-10-08", "2026-10-09", "2026-10-10"])
            self.assertEqual(days["2026-10-09"]["new_followers"], 25, "差分应补出 10-09 的 25")
            # ② 文件归档、inbox 清空
            self.assertFalse((env.inbox / "粉丝分析.csv").exists())
            self.assertTrue(list(env.processed.rglob("粉丝分析.csv")))
            # ③ 汇总报告（仓库内记相对路径、仓库外记绝对路径）
            rep = Path(out["report"])
            rep = rep if rep.is_absolute() else PROJECT_ROOT / rep
            self.assertTrue(rep.exists())
            self.assertIn("周更一键闭环报告", rep.read_text(encoding="utf-8"))

    def test_unknown_file_stays_and_marks_not_ok(self):
        with TempEnv() as env:
            env.put("乱七八糟.csv", "甲,乙,丙\n1,2,3\n")
            out = wr.run(write_dashboard=False, tuner=False)
            self.assertFalse(out["ok"], "识别不出的文件必须让 ok=False（要人看一眼）")
            self.assertTrue((env.inbox / "乱七八糟.csv").exists(), "未知文件必须留在 inbox")
            self.assertIn("unknown", out["inbox"]["counts"])

    def test_pending_growth_shows_pending_not_zero(self):
        with TempEnv() as env:
            out = wr.run(write_dashboard=False, tuner=False)
            md = wr.render_md(out)
            self.assertIn("（待接入）", md)
            self.assertIn("转粉通道尚未接入", md)
            g = (out.get("dashboard") or {}).get("growth")
            if g is not None:
                self.assertIsNone(g["日均涨粉"], "无数据必须是 None，不是 0")


class TestSafeGuards(unittest.TestCase):
    def test_step_failure_does_not_abort_others(self):
        with TempEnv() as env:
            orig = ie.run
            ie.run = lambda **k: (_ for _ in ()).throw(RuntimeError("模拟崩溃"))
            try:
                out = wr.run(write_dashboard=False, tuner=False)
            finally:
                ie.run = orig
            self.assertFalse(out["ok"])
            self.assertTrue(any("消费投递箱 失败" in f for f in out["failures"]))
            self.assertIn("lag", out, "前一步崩溃不得阻断滞后监控")

    def test_inbox_clean_semantics(self):
        self.assertTrue(wr._inbox_clean(None))
        self.assertTrue(wr._inbox_clean({"counts": {"ok": 2}}))
        self.assertFalse(wr._inbox_clean({"counts": {"unknown": 1}}))
        self.assertFalse(wr._inbox_clean({"counts": {"failed": 1}}))

    def test_tuner_is_read_only(self):
        """调参只出建议：绝不改 config/ratio_config.yaml。"""
        cfg_path = PROJECT_ROOT / "config" / "ratio_config.yaml"
        before = cfg_path.read_text(encoding="utf-8")
        with TempEnv():
            out = wr.run(write_dashboard=False)
        self.assertEqual(cfg_path.read_text(encoding="utf-8"), before,
                         "周更闭环不得自动改配比配置")
        self.assertIn("tuner", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
