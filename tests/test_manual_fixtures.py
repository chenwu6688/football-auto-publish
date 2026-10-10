import importlib.util
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "fixture_library", str(Path(__file__).resolve().parent.parent / "scripts" / "fixture_library.py"))


def _load():
    mod = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(mod)
    return mod


class TestManualFixtures(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_load_future_and_skip_past(self):
        yml = """
fixtures:
  - date: "2030-06-01"
    competition: 世预赛
    home: 中国
    away: 巴林
    time: "19:00"
  - date: "2000-01-01"
    competition: 世预赛
    home: 中国
    away: 韩国
"""
        p = Path(tempfile.mkdtemp()) / "m.yaml"
        p.write_text(yml, encoding="utf-8")
        out = self.mod.load_manual_fixtures(p)
        # 过去日期被跳过
        self.assertEqual(len(out), 1)
        m = out[0]
        self.assertEqual(m["source"], "manual")
        self.assertEqual(m["homeTeam"]["name"], "中国")
        self.assertEqual(m["awayTeam"]["name"], "巴林")
        self.assertTrue(m["utcDate"].startswith("2030-06-01T19:00"))
        self.assertEqual(m["competition"]["name"], "世预赛")

    def test_empty_or_missing_file(self):
        self.assertEqual(self.mod.load_manual_fixtures(Path("/nonexistent/x.yaml")), [])
        p = Path(tempfile.mkdtemp()) / "empty.yaml"
        p.write_text("fixtures: []\n", encoding="utf-8")
        self.assertEqual(self.mod.load_manual_fixtures(p), [])

    def test_skip_incomplete_entries(self):
        yml = """
fixtures:
  - date: "2030-06-01"
    home: 中国
  - home: 中国
    away: 日本
"""
        p = Path(tempfile.mkdtemp()) / "m.yaml"
        p.write_text(yml, encoding="utf-8")
        self.assertEqual(self.mod.load_manual_fixtures(p), [])

    def test_chinese_name_resolves_for_manual(self):
        emap = {"by_en": {}}
        r = self.mod.resolve_team("中国", emap, "世预赛")
        self.assertIsNotNone(r)
        self.assertEqual(r["name_zh"], "中国")


if __name__ == "__main__":
    unittest.main()
