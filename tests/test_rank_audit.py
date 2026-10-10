import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "weekly_rank_audit",
    str(Path(__file__).resolve().parent.parent / "scripts" / "weekly_rank_audit.py"))


def _load():
    mod = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(mod)
    return mod


class TestRankAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mod = _load()
        self.mod.OUTPUT_DIR = self.tmp / "output"
        self.mod.CACHE_PATH = self.tmp / "ranker_cache.json"
        self.mod.AUDIT_DIR = self.tmp / "rank_audit"
        self.mod.CALIB_PATH = self.mod.AUDIT_DIR / "calibration.json"

    def _write_metadata(self, date_str, titles):
        d = self.mod.OUTPUT_DIR / date_str
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(json.dumps({"topics": [
            {"title": t, "content_type": "热点球评", "keywords_cn": ["曼城"],
             "_column_name": "战术榜单"} for t in titles
        ]}, ensure_ascii=False), encoding="utf-8")

    def test_collect_from_metadata_fallback(self):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        self._write_metadata(today, ["曼城主场能否啃下铁桶阵？", "利物浦中场失控谁之过？"])
        recs, src = self.mod.collect_records(7)
        self.assertEqual(src, "metadata")
        self.assertEqual(len(recs), 2)
        # 五维齐全
        ok, missing = self.mod.coverage_ok(recs)
        self.assertTrue(ok, missing)

    def test_collect_from_cache(self):
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.mod.CACHE_PATH.write_text(json.dumps({"judged": {
            "card-1": {"scores": {d: 3 for d in self.mod.DIMENSIONS}, "at": now, "total": 15}
        }}, ensure_ascii=False), encoding="utf-8")
        recs, src = self.mod.collect_records(7)
        self.assertEqual(src, "ranker_cache")
        self.assertEqual(recs[0]["来源"], "model")

    def test_coverage_detects_missing_dimension(self):
        recs = [{"维度": {"冲突度": 3, "人物知名度": 4}, "总分": 7}]
        ok, missing = self.mod.coverage_ok(recs)
        self.assertFalse(ok)
        self.assertIn("时效性", missing)

    def test_worksheet_and_persist(self):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        self._write_metadata(today, [f"选题{i}" for i in range(5)])
        recs, src = self.mod.collect_records(7)
        sample = self.mod._sample(recs, 3)
        means = self.mod._dim_means(sample)
        ws = self.mod.build_worksheet(sample, 7, src, means, {}, True, [])
        self.assertIn("待人工复核清单", ws)
        self.assertIn("量表覆盖五维：✅", ws)
        ws_path, calib = self.mod.persist(ws, sample, means, {}, True, [], src, 7)
        self.assertTrue(ws_path.exists())
        saved = json.loads(calib.read_text(encoding="utf-8"))
        self.assertEqual(saved["history"][-1]["窗口天数"], 7)
        self.assertEqual(saved["history"][-1]["样本数"], len(sample))

    def test_drift_vs_prev(self):
        drift = self.mod._drift({"冲突度": 4.0, "时效性": 3.0},
                                {"维度均值": {"冲突度": 3.0, "时效性": 3.5}})
        self.assertEqual(drift["冲突度"], 1.0)
        self.assertEqual(drift["时效性"], -0.5)


if __name__ == "__main__":
    unittest.main()
