import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import api_football as af


class TestApiFootball(unittest.TestCase):
    """计划 13.2：洲际赛程补齐（亚冠/欧联/欧协联/中超），联赛 ID 动态解析。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_cache = af.LEAGUE_CACHE
        self._orig_dir = af.FIXTURE_DIR
        af.FIXTURE_DIR = self.tmp
        af.LEAGUE_CACHE = self.tmp / "af_leagues.json"
        os.environ["API_FOOTBALL_KEY"] = "test-key"

    def tearDown(self):
        af.LEAGUE_CACHE = self._orig_cache
        af.FIXTURE_DIR = self._orig_dir
        os.environ.pop("API_FOOTBALL_KEY", None)

    class _R:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    def test_resolve_league_ids_dynamic(self):
        def fake_get(path, params, timeout=20):
            return self._R({"response": [
                {"league": {"id": 3, "name": "UEFA Europa League", "type": "League"},
                 "seasons": [{"year": 2025, "current": False},
                             {"year": 2026, "current": True}]}
            ]})

        with mock.patch.object(af, "_get", side_effect=fake_get):
            ids = af.resolve_league_ids(["欧联"])
        self.assertEqual(ids["欧联"]["id"], 3)
        self.assertEqual(ids["欧联"]["season"], 2026)
        # 缓存落盘，二次调用不再联网
        self.assertTrue(af.LEAGUE_CACHE.exists())
        self.assertEqual(af.resolve_league_ids(["欧联"])["欧联"]["id"], 3)

    def test_no_key_returns_empty(self):
        os.environ.pop("API_FOOTBALL_KEY", None)
        os.environ.pop("RAPIDAPI_KEY", None)
        self.assertEqual(af.fetch_fixtures(7), [])

    def test_to_fd_maps_structure(self):
        fx = {"fixture": {"id": 111, "date": "2026-11-01T12:00:00+00:00",
                          "status": {"short": "NS"}},
              "teams": {"home": {"name": "Shanghai Port"}, "away": {"name": "Beijing Guoan"}},
              "league": {"id": 169}}
        m = af._to_fd(fx, "中超")
        self.assertEqual(m["competition"]["name"], "中超")
        self.assertEqual(m["homeTeam"]["name"], "Shanghai Port")
        self.assertEqual(m["source"], "api-football")
        self.assertTrue(m["utcDate"].startswith("2026-11-01"))

    def test_fetch_league_fixtures_uses_cache(self):
        # 预铺联赛缓存 + 当日赛程缓存 → 全程不联网
        af.LEAGUE_CACHE.write_text(json.dumps({"中超": {"id": 169, "season": 2026}}),
                                   encoding="utf-8")
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        (self.tmp / f"af_中超_{today}.json").write_text(
            json.dumps([{"id": 1, "competition": {"id": "af-169", "name": "中超"}}],
                       ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(af, "_get", side_effect=AssertionError("不应联网")):
            out = af.fetch_league_fixtures("中超", 7)
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
