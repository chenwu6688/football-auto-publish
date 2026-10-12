"""绩效记录主键唯一性单测（计划第十章数据接入的前置修复）。

背景：绩效记录原主键是 `{date}/article-{index}`，但 index 是「每批从 1 开始」的
批内序号，同一天多批次必然重复（真实数据里 2026-10-12 的 index 2/3 各出现两次），
用它做键会把不同文章的数据写到一起。现改为 slug 优先、旧键只读兜底。

纪律：全部在临时目录内进行，**不读也不写真实 output/**。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import import_performance as ip   # noqa: E402
import performance_logger as pl    # noqa: E402


class TestPerfKey(unittest.TestCase):
    def test_slug_preferred(self):
        self.assertEqual(pl.perf_key("2026-10-12", index=2, slug="abc"), "2026-10-12/abc")

    def test_legacy_fallback(self):
        self.assertEqual(pl.perf_key("2026-10-12", index=2), "2026-10-12/article-2")
        self.assertEqual(pl.perf_key("2026-10-12", index=2, slug=None),
                         "2026-10-12/article-2")

    def test_resolve_prefers_slug(self):
        log = {"articles": {
            "2026-10-12/abc": {"reads": 1},
            "2026-10-12/article-2": {"reads": 99},
        }}
        self.assertEqual(pl.resolve_performance(log, "2026-10-12", index=2, slug="abc"),
                         {"reads": 1})

    def test_resolve_falls_back_to_index(self):
        log = {"articles": {"2026-10-12/article-2": {"reads": 99}}}
        self.assertEqual(pl.resolve_performance(log, "2026-10-12", index=2), {"reads": 99})
        self.assertEqual(pl.resolve_performance(log, "2026-10-12", index=7), {})

    def test_index_occurrences(self):
        meta = {"articles": [{"index": 2}, {"index": 2}, {"index": 5}]}
        self.assertEqual(pl.index_occurrences(meta), {2: 2, 5: 1})


class TestLogAndSync(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._p1 = mock.patch.object(pl, "OUTPUT_DIR", self.tmp)
        self._p2 = mock.patch.object(pl, "PERF_LOG_PATH", self.tmp / "performance_log.json")
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def _log(self):
        return json.loads((self.tmp / "performance_log.json").read_text(encoding="utf-8"))["articles"]

    def test_same_index_different_slug_do_not_collide(self):
        pl.log_article_performance("2026-10-12", 2, reads=5120, slug="a-abc")
        pl.log_article_performance("2026-10-12", 2, reads=3044, slug="b-def")
        arts = self._log()
        self.assertEqual(len(arts), 2)
        self.assertEqual(arts["2026-10-12/a-abc"]["reads"], 5120)
        self.assertEqual(arts["2026-10-12/b-def"]["reads"], 3044)

    def test_legacy_key_still_written_without_slug(self):
        pl.log_article_performance("2026-10-12", 7, reads=1)
        self.assertIn("2026-10-12/article-7", self._log())

    def test_record_carries_slug(self):
        pl.log_article_performance("2026-10-12", 2, reads=5, slug="a-abc")
        self.assertEqual(self._log()["2026-10-12/a-abc"]["slug"], "a-abc")

    def _write_meta(self, articles):
        d = self.tmp / "2026-10-12"
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(
            json.dumps({"date": "2026-10-12", "articles": articles}, ensure_ascii=False),
            encoding="utf-8")

    def _read_meta(self):
        return json.loads((self.tmp / "2026-10-12" / "metadata.json").read_text(encoding="utf-8"))

    def test_sync_by_slug_and_legacy_unique_index(self):
        self._write_meta([
            {"index": 2, "slug": "a-abc", "title": "甲"},
            {"index": 2, "slug": "b-def", "title": "乙"},
            {"index": 9, "title": "无 slug 但当天唯一"},
        ])
        (self.tmp / "performance_log.json").write_text(json.dumps({"articles": {
            "2026-10-12/a-abc": {"reads": 5120},
            "2026-10-12/b-def": {"reads": 3044},
            "2026-10-12/article-9": {"reads": 77},
        }}), encoding="utf-8")

        pl.update_metadata_performance("2026-10-12")
        arts = self._read_meta()["articles"]
        self.assertEqual(arts[0]["performance"]["reads"], 5120)
        self.assertEqual(arts[1]["performance"]["reads"], 3044)
        self.assertEqual(arts[2]["performance"]["reads"], 77)

    def test_sync_skips_ambiguous_index_instead_of_writing_wrong_article(self):
        self._write_meta([{"index": 5, "title": "甲"}, {"index": 5, "title": "乙"}])
        (self.tmp / "performance_log.json").write_text(json.dumps({"articles": {
            "2026-10-12/article-5": {"reads": 55},
        }}), encoding="utf-8")

        pl.update_metadata_performance("2026-10-12")
        for a in self._read_meta()["articles"]:
            self.assertNotIn("performance", a,
                             "index 当天不唯一时必须跳过，不能串号写到两篇上")

    def test_static_key_helpers_agree(self):
        self.assertEqual(pl.perf_key("2026-10-12", index=1, slug="s"),
                         f"2026-10-12/s")


class TestValidateRowSlug(unittest.TestCase):
    def test_slug_only_accepted(self):
        rec, err = ip.validate_row({"date": "2026-10-12", "slug": "a-abc", "reads": "100"}, 2)
        self.assertIsNone(err)
        self.assertEqual(rec["slug"], "a-abc")
        self.assertEqual(rec["reads"], 100)

    def test_neither_index_nor_slug_rejected(self):
        rec, err = ip.validate_row({"date": "2026-10-12", "reads": "100"}, 2)
        self.assertIsNone(rec)
        self.assertIn("index", err)

    def test_units_cleaned_in_canonical_path(self):
        rec, err = ip.validate_row(
            {"date": "2026-10-12", "index": "1", "reads": "1.8万",
             "impressions": "5,120", "retention_rate": "41.2%"}, 2)
        self.assertIsNone(err)
        self.assertEqual(rec["reads"], 18000)
        self.assertEqual(rec["impressions"], 5120)
        self.assertAlmostEqual(rec["retention_rate"], 0.412)


if __name__ == "__main__":
    unittest.main()
