"""球队层映射增量维护单测（计划 13.1）。"""

import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_team_map as btm  # noqa: E402
from fixture_library import resolve_team  # noqa: E402


class TestCurated(unittest.TestCase):
    def test_curated_parsed(self):
        c = btm.load_curated()
        self.assertEqual(c["al hilal"]["name_zh"], "利雅得新月")
        self.assertEqual(c["al hilal"]["awareness"], "A")
        self.assertEqual(c["fc basel 1893"]["name_zh"], "巴塞尔")
        self.assertIn("FC Basel", c["fc basel 1893"]["aliases"])

    def test_no_english_leak_as_zh(self):
        # curated 表里每条都必须有非空中文译名（不机器音译、不以英文冒充）
        for k, v in btm.load_curated().items():
            self.assertTrue(v["name_zh"], k)
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in v["name_zh"]), k)


class TestBuildClubs(unittest.TestCase):
    def test_curated_registered(self):
        reg, pend = btm.build_clubs(["Al Hilal", "Urawa Red Diamonds"], {})
        zh = {v["name_zh"] for v in reg.values()}
        self.assertIn("利雅得新月", zh)
        self.assertIn("浦和红钻", zh)
        self.assertTrue(all(v["category"] == "club" for v in reg.values()))

    def test_uncurated_goes_pending(self):
        reg, pend = btm.build_clubs(["Some Unknown United FC"], {})
        self.assertNotIn("club_some_unknown_united_fc", reg)
        self.assertIn("club_some_unknown_united_fc", pend)
        # 绝不把英文名当中文名登记
        self.assertFalse(any(v["name_zh"] == "Some Unknown United FC" for v in reg.values()))

    def test_duplicate_zh_skipped(self):
        # 已有「皇家社会」时，另一英文写法的同一支队不得重复登记
        existing = {"club_real_sociedad_de_futbol": {
            "internal_id": "club_real_sociedad_de_futbol", "name_en": "Real Sociedad de Fútbol",
            "name_zh": "皇家社会", "category": "club", "awareness": "B", "aliases": []}}
        reg, pend = btm.build_clubs([], existing)
        self.assertNotIn("皇家社会", [v["name_zh"] for v in reg.values()])

    def test_existing_preserved_fd_id(self):
        # 已在映射表中的球队（此处模拟 Celtic 已登记），增量维护不得丢掉 fd_id/赛事
        existing = {"club_celtic": {
            "internal_id": "club_celtic", "name_en": "Celtic", "name_zh": "凯尔特人",
            "category": "club", "awareness": "B", "fd_id": 100, "aliases": [],
            "competitions": ["欧冠"]}}
        reg, _ = btm.build_clubs([], existing)
        self.assertEqual(reg["club_celtic"]["fd_id"], 100)
        self.assertEqual(reg["club_celtic"]["competitions"], ["欧冠"])


class TestResolveTeamIntercontinental(unittest.TestCase):
    def _emap(self):
        data = json.loads((Path(btm.PROJECT_ROOT) / "config" / "entity_map.json").read_text(encoding="utf-8"))
        by_en = {}
        for e in data["entities"].values():
            by_en[e["name_en"].lower()] = e
            for a in e.get("aliases", []):
                by_en.setdefault(a.lower(), e)
        return {"raw": data, "by_en": by_en}

    def test_afc_club_resolves(self):
        em = self._emap()
        r = resolve_team("Al Hilal", em, "亚冠")
        self.assertIsNotNone(r)
        self.assertEqual(r["name_zh"], "利雅得新月")

    def test_europa_club_resolves(self):
        em = self._emap()
        self.assertEqual(resolve_team("Celtic", em, "欧联")["name_zh"], "凯尔特人")

    def test_csl_english_name_resolves(self):
        # API-Football 返回中超英文队名时也能解析（此前会退化成英文名直出）
        em = self._emap()
        r = resolve_team("Shanghai Port", em, "中超")
        self.assertEqual(r["name_zh"], "上海海港")

    def test_csl_chinese_fallback(self):
        em = self._emap()
        r = resolve_team("某未知中超队", em, "中超")
        self.assertEqual(r["name_zh"], "某未知中超队")
        self.assertEqual(r["awareness"], "B")

    def test_unregistered_english_dropped(self):
        em = self._emap()
        self.assertIsNone(resolve_team("No Such Club FC", em, "亚冠"))


if __name__ == "__main__":
    unittest.main()
