"""增长指标批量回填通道单测（计划第十章）。"""

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import import_performance as ip  # noqa: E402
import performance_logger as pl  # noqa: E402


class TestValidateRow(unittest.TestCase):
    def test_valid_row_normalized(self):
        rec, err = ip.validate_row(
            {"date": "2026-10-10", "index": "2", "reads": "5000", "comments": "32",
             "likes": "120", "shares": "8", "retention_rate": "0.41",
             "new_followers": "12", "impressions": "180000", "follower_reads": "300"}, 2)
        self.assertIsNone(err)
        self.assertEqual(rec["date"], "2026-10-10")
        self.assertEqual(rec["index"], 2)
        self.assertAlmostEqual(rec["retention_rate"], 0.41)
        self.assertEqual(rec["impressions"], 180000)

    def test_retention_percent_converted(self):
        rec, err = ip.validate_row({"date": "2026-10-10", "index": "1", "retention_rate": "41"}, 2)
        self.assertIsNone(err)
        self.assertAlmostEqual(rec["retention_rate"], 0.41)

    def test_missing_optional_defaults_zero(self):
        rec, err = ip.validate_row({"date": "2026-10-10", "index": "1"}, 2)
        self.assertIsNone(err)
        self.assertEqual(rec["reads"], 0)
        self.assertEqual(rec["impressions"], 0)

    def test_bad_date(self):
        rec, err = ip.validate_row({"date": "2026/10/10", "index": "1"}, 3)
        self.assertIsNone(rec)
        self.assertIn("date", err)

    def test_bad_index(self):
        rec, err = ip.validate_row({"date": "2026-10-10", "index": "0"}, 4)
        self.assertIsNone(rec)
        self.assertIn("index", err)

    def test_negative_rejected(self):
        rec, err = ip.validate_row({"date": "2026-10-10", "index": "1", "reads": "-5"}, 5)
        self.assertIsNone(rec)
        self.assertIn("reads", err)

    def test_retention_out_of_range(self):
        rec, err = ip.validate_row({"date": "2026-10-10", "index": "1", "retention_rate": "150"}, 6)
        self.assertIsNone(rec)
        self.assertIn("retention_rate", err)


class TestLoadRows(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_csv_load(self):
        p = self.tmp / "a.csv"
        p.write_text("date,index,reads\n2026-10-10,1,100\n", encoding="utf-8")
        rows = ip.load_rows(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reads"], "100")

    def test_json_list_load(self):
        p = self.tmp / "a.json"
        p.write_text(json.dumps([{"date": "2026-10-10", "index": 1, "reads": 100}]), encoding="utf-8")
        self.assertEqual(len(ip.load_rows(p)), 1)

    def test_json_wrapped_load(self):
        p = self.tmp / "b.json"
        p.write_text(json.dumps({"articles": [{"date": "2026-10-10", "index": 2}]}), encoding="utf-8")
        self.assertEqual(ip.load_rows(p)[0]["index"], 2)


class TestImportRows(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._p1 = mock.patch.object(pl, "OUTPUT_DIR", self.tmp)
        self._p2 = mock.patch.object(pl, "PERF_LOG_PATH", self.tmp / "performance_log.json")
        self._p1.start(); self._p2.start()

    def tearDown(self):
        self._p1.stop(); self._p2.stop()

    def test_dry_run_writes_nothing(self):
        rows = [{"date": "2026-10-10", "index": "1", "reads": "100", "__lineno": 2}]
        res = ip.import_rows(rows, dry_run=True)
        self.assertEqual(res["imported"], 1)
        self.assertFalse((self.tmp / "performance_log.json").exists())

    def test_import_writes_log(self):
        rows = [
            {"date": "2026-10-10", "index": "1", "reads": "100", "impressions": "1000",
             "follower_reads": "10", "retention_rate": "0.5", "__lineno": 2},
            {"date": "2026-10-10", "index": "2", "reads": "200", "__lineno": 3},
        ]
        res = ip.import_rows(rows, dry_run=False, sync=False)
        self.assertEqual(res["imported"], 2)
        log = json.loads((self.tmp / "performance_log.json").read_text(encoding="utf-8"))
        self.assertEqual(log["articles"]["2026-10-10/article-1"]["reads"], 100)
        self.assertEqual(log["articles"]["2026-10-10/article-1"]["impressions"], 1000)

    def test_errors_reported_but_valid_imported(self):
        rows = [
            {"date": "bad", "index": "1", "__lineno": 2},
            {"date": "2026-10-10", "index": "3", "reads": "50", "__lineno": 3},
        ]
        res = ip.import_rows(rows, dry_run=False, sync=False)
        self.assertEqual(res["imported"], 1)
        self.assertEqual(len(res["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
