import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "export_static_data", str(Path(__file__).resolve().parent.parent / "scripts" / "export_static_data.py"))


def _load_mod():
    mod = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(mod)
    return mod


class TestExportEnrich(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mod = _load_mod()
        self.mod.OUTPUT_DIR = self.tmp
        self.mod.PERF_LOG_PATH = self.tmp / "performance_log.json"
        d = self.tmp / "2026-10-10"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(json.dumps({"articles": [
            {"index": 1, "title": "曼城主场能否啃下铁桶阵？",
             "content_type": "热点球评", "column_name": "战术榜单",
             "batch_name": "晨读", "tags": [], "keywords": []},
        ]}, ensure_ascii=False), encoding="utf-8")
        (d / "article-1-x.md").write_text(
            '---\ntitle: "曼城主场能否啃下铁桶阵？"\ndate: 2026-10-10\n'
            'tags: []\nkeywords: []\narticle_index: 1\nbatch_name: 晨读\n'
            'originality_note: "摘要"\n---\n\n# 标题\n\n正文\n', encoding="utf-8")
        (self.tmp / "performance_log.json").write_text(json.dumps({"articles": {
            "2026-10-10/article-1": {"date": "2026-10-10", "index": 1,
                                     "reads": 5200, "new_followers": 37, "retention_rate": 0.5},
        }}, ensure_ascii=False), encoding="utf-8")

    def test_enrich_adds_plan_116_fields(self):
        meta = self.mod.get_metadata("2026-10-10")
        arts = self.mod._enrich_articles(self.mod.scan_articles("2026-10-10"), meta, "2026-10-10")
        a = arts[0]
        self.assertEqual(a["content_type"], "热点球评")
        self.assertEqual(a["column_name"], "战术榜单")
        self.assertEqual(a["reads"], 5200)
        self.assertEqual(a["new_followers"], 37)

    def test_fallback_from_metadata_includes_fields(self):
        meta = self.mod.get_metadata("2026-10-10")
        fb = self.mod._articles_from_metadata(meta, "2026-10-10")
        self.assertEqual(fb[0]["content_type"], "热点球评")
        self.assertEqual(fb[0]["column_name"], "战术榜单")
        self.assertEqual(fb[0]["new_followers"], 37)

    def test_missing_perf_defaults_zero(self):
        (self.tmp / "performance_log.json").unlink()
        meta = self.mod.get_metadata("2026-10-10")
        arts = self.mod._enrich_articles(self.mod.scan_articles("2026-10-10"), meta, "2026-10-10")
        self.assertEqual(arts[0]["reads"], 0)
        self.assertEqual(arts[0]["new_followers"], 0)


if __name__ == "__main__":
    unittest.main()
