"""成稿→发布漏斗口径与台账恒等式单测（2026-11-16 改动点 6）。

背景：台账分母一直是「尝试数」（published + failed），合规剔除的稿子、以及
成稿了却连台账都没进的稿子都不在分母里。11-02~11-15 实测：旧口径 98.4%、
成稿口径 82.4% —— 差的 17 个点里大部分不是「发失败」，而是取证缺口。
一刀切把两个数合成一个，要么冤枉发布器，要么把缺口藏起来。

本文件钉死四件事：
  A. ``draft_snapshot`` 读不到就 ``available=False``，**不写 0**（缺数据 ≠ 0 篇）；
  B. ``published_with_title_gate_issue`` 是「已发布带瑕疵」，不是丢稿，单独一栏；
  C. 日级 ledger 才算 ``lost_upstream``；单批次文件算了就是拿两把尺子相减；
  D. 两条恒等式真的会红 —— 未知 status、成稿少于记录，都必须被抓住，
     且「一次都没判过」必须是未判定（None），不能读成通过。
"""

import importlib.util
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

import ledger_funnel as lf  # noqa: E402
import publisher  # noqa: E402

CST = timezone(timedelta(hours=8))


def _today() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _load_verify():
    spec = importlib.util.spec_from_file_location(
        "verify_publish_success", PROJECT_ROOT / "scripts" / "verify_publish_success.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDraftSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "output"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, day, meta):
        d = self.out / day
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def test_counts_drafts_dropped_and_gate_separately(self):
        self._write("2026-11-16", {
            "articles": [{"title": "甲"}, {"title": "乙", "title_gate_blocked": "结构违规"}],
            "compliance_dropped": [{"index": 1, "title": "丙"}],
        })
        s = lf.draft_snapshot("2026-11-16", self.out)
        self.assertTrue(s["available"])
        self.assertEqual(s["drafts"], 2)
        self.assertEqual(s["dropped_compliance"], 1)
        self.assertEqual(s["draft_total"], 3)
        # 关键：闸门留证的篇数是「已发布带瑕疵」，不能被并进「剔除」
        self.assertEqual(s["title_gate_blocked_published"], 1)

    def test_missing_metadata_is_unavailable_not_zero(self):
        s = lf.draft_snapshot("2026-01-01", self.out)
        self.assertFalse(s["available"])
        self.assertIsNone(s["drafts"])          # 不写 0
        self.assertTrue(s["reason"])

    def test_broken_json_is_unavailable_not_zero(self):
        d = self.out / "2026-11-16"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text("{坏 json", encoding="utf-8")
        s = lf.draft_snapshot("2026-11-16", self.out)
        self.assertFalse(s["available"])
        self.assertIsNone(s["drafts"])

    def test_scope_is_day_cumulative(self):
        """metadata 是同日跨批次累加的，scope 必须显式标出来，否则会被当本批数用。"""
        self._write("2026-11-16", {"articles": []})
        self.assertEqual(lf.draft_snapshot("2026-11-16", self.out)["scope"], "day_cumulative")


class TestIdentity(unittest.TestCase):
    def _counts(self, **kw):
        c = {"published": 0, "failed": 0, "skipped_prediction": 0,
             "skipped_already_published": 0}
        c.update(kw)
        return c

    def test_partition_ok(self):
        r = lf.identity(self._counts(published=2, failed=1), 3)
        self.assertTrue(r["records_identity_ok"])
        self.assertEqual(r["unaccounted_status"], 0)

    def test_unknown_status_breaks_partition(self):
        """负向：出现没登记的新状态码 —— 恒等式必须红，别去调阈值。"""
        r = lf.identity(self._counts(published=2), 3)
        self.assertFalse(r["records_identity_ok"])
        self.assertEqual(r["unaccounted_status"], 1)

    def test_lost_upstream_can_be_negative_and_is_not_clamped(self):
        """台账比成稿还多是证据，抹平成 0 就是掩盖。"""
        r = lf.identity(self._counts(published=5), 5, drafts=3)
        self.assertEqual(r["lost_upstream"], -2)
        self.assertFalse(r["cover_ok"])

    def test_cover_ok_none_without_drafts(self):
        r = lf.identity(self._counts(published=2), 2)
        self.assertIsNone(r["cover_ok"])        # 未判定，不判通过


class TestPublisherLedgerFunnel(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._audit, self._out = publisher.AUDIT_DIR, publisher.OUTPUT_BASE
        publisher.AUDIT_DIR = self.tmp / "audit"
        publisher.OUTPUT_BASE = self.tmp / "output"
        d = publisher.OUTPUT_BASE / "2026-11-16"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(json.dumps({
            "articles": [{"title": "甲", "batch_name": "晨读"},
                         {"title": "乙", "batch_name": "晨读"},
                         {"title": "丙", "batch_name": "午间"}],
            "compliance_dropped": [{"index": 1, "title": "丁", "blocked_by": ["TT-03"]}],
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        publisher.AUDIT_DIR, publisher.OUTPUT_BASE = self._audit, self._out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recs(self):
        return [{"status": "published", "title": "甲", "batch_name": "晨读", "platform_id": "1"},
                {"status": "failed", "title": "丙", "batch_name": "午间", "reason": "超时"}]

    def test_day_aggregate_carries_draft_side(self):
        p = publisher.write_publish_ledger("2026-11-16", self._recs(), batch_filter="morning")
        c = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(c["counts"]["total"], 2)
        self.assertEqual(c["counts"]["drafts"], 3)
        self.assertEqual(c["counts"]["dropped_compliance"], 1)
        # 成稿 3 篇、台账 2 条 → 1 篇「成稿在、台账没有」
        self.assertEqual(c["counts"]["lost_upstream"], 1)
        self.assertTrue(c["identity"]["records_identity_ok"])
        self.assertTrue(c["identity"]["cover_ok"])

    def test_batch_file_does_not_fake_lost_upstream(self):
        """单批次文件拿本批 records 减当天 drafts 会造出假缺口，所以本层必须留空。"""
        publisher.write_publish_ledger("2026-11-16", self._recs(), batch_filter="morning")
        b = json.loads((publisher.AUDIT_DIR / "publish_ledger_2026-11-16_morning.json")
                       .read_text(encoding="utf-8"))
        self.assertIsNone(b["counts"]["lost_upstream"])
        self.assertIsNone(b["identity"]["cover_ok"])
        self.assertEqual(b["identity"]["scope"], "batch")

    def test_total_semantics_unchanged(self):
        """counts.total 仍是「台账记录条数」——改口径不是把 total 变成成稿数。"""
        p = publisher.write_publish_ledger("2026-11-16", self._recs())
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(d["counts"]["total"], len(d["records"]))

    def test_no_metadata_leaves_drafts_null(self):
        p = publisher.write_publish_ledger("2026-12-31", self._recs())
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertIsNone(d["counts"]["drafts"])
        self.assertFalse(d["identity"]["available"])
        self.assertIsNone(d["identity"]["cover_ok"])


class TestVerifyChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.v = _load_verify()
        self._orig = self.v.AUDIT_DIR
        self.v.AUDIT_DIR = self.tmp
        self.day = _today()

    def tearDown(self):
        self.v.AUDIT_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger(self, counts, records):
        (self.tmp / f"publish_ledger_{self.day}.json").write_text(json.dumps(
            {"date": self.day, "counts": counts, "records": records},
            ensure_ascii=False), encoding="utf-8")

    def test_identity_passes_with_two_sides(self):
        self._ledger({"total": 2, "published": 1, "failed": 1, "drafts": 3},
                     [{"status": "published", "title": "甲"},
                      {"status": "failed", "title": "乙"}])
        r = self.v.check_ledger_identity(1)
        self.assertTrue(r["ok"])
        self.assertEqual(r["cover_judged"], 1)

    def test_unknown_status_fails(self):
        self._ledger({"total": 2, "published": 1, "failed": 0, "drafts": 2},
                     [{"status": "published", "title": "甲"},
                      {"status": "skipped_quota", "title": "乙"}])
        r = self.v.check_ledger_identity(1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["partition_bad"], 1)

    def test_cover_bad_fails(self):
        self._ledger({"total": 3, "published": 3, "failed": 0, "drafts": 2},
                     [{"status": "published", "title": x} for x in "甲乙丙"])
        r = self.v.check_ledger_identity(1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["cover_bad"], 1)

    def test_no_drafts_is_undetermined_not_pass(self):
        """老台账没有成稿侧字段 → I2 未判定 → 整体 None，绝不能读成通过。"""
        self._ledger({"total": 1, "published": 1, "failed": 0},
                     [{"status": "published", "title": "甲"}])
        r = self.v.check_ledger_identity(1)
        self.assertIsNone(r["ok"])
        self.assertEqual(r["cover_judged"], 0)

    def test_draft_rate_reports_both_sides(self):
        self._ledger({"total": 1, "published": 1, "failed": 0, "drafts": 2,
                      "dropped_compliance": 1, "skipped_prediction": 0,
                      "lost_upstream": 1, "published_with_title_gate_issue": 0,
                      "skipped_already_published": 0},
                     [{"status": "published", "title": "甲"}])
        # 应发 3 篇 < MIN_SAMPLE(5) → 未判定（不许拿 1 天定生死）
        self.assertIsNone(self.v.check_draft_rate(1)["ok"])
        self._ledger({"total": 10, "published": 8, "failed": 0, "drafts": 20,
                      "dropped_compliance": 2, "skipped_prediction": 1,
                      "lost_upstream": 10, "published_with_title_gate_issue": 0,
                      "skipped_already_published": 0},
                     [{"status": "published", "title": f"篇{i}"} for i in range(10)])
        r = self.v.check_draft_rate(1)
        self.assertTrue(r["ok"])
        # 分母 = 20 + 2 - 1 = 21，实发 8
        self.assertEqual(r["draft_rate"], round(100.0 * 8 / 21, 1))

    def test_draft_rate_undetermined_without_field(self):
        self._ledger({"total": 1, "published": 1, "failed": 0},
                     [{"status": "published", "title": "甲"}])
        r = self.v.check_draft_rate(1)
        self.assertIsNone(r["ok"])
        self.assertTrue(r["pending"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
