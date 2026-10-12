"""数据源与信源清单验收单测（计划 13.2 / 13.4）。"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vsr = _load("verify_source_roster", "scripts/verify_source_roster.py")


class TestSourceRoster(unittest.TestCase):
    def test_four_sources(self):
        c = vsr.check_four_sources()
        self.assertTrue(c["ok"], c["detail"])
        self.assertTrue(all(c["evidence"].values()), c["evidence"])

    def test_watchlist(self):
        c = vsr.check_watchlist()
        self.assertTrue(c["ok"], c["detail"])
        self.assertEqual(len(c["present"]), len(vsr.PLAN_SOURCES))

    def test_signal_only(self):
        self.assertTrue(vsr.check_signal_only()["ok"], vsr.check_signal_only()["detail"])

    def test_no_vendors(self):
        c = vsr.check_no_vendors()
        self.assertTrue(c["advisory"])
        self.assertTrue(c["ok"], c["hits"])

    def test_media_boundary_advisory(self):
        c = vsr.check_media_boundary()
        self.assertTrue(c["advisory"])
        # 生产代码中不得把无授权媒体作为「生成内容源」——仅允许出现在素材/映射/参照层
        for f in c["hits"].get("懂球帝", []):
            self.assertNotIn("extractor", f)

    def test_verify_overall(self):
        rep = vsr.verify()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["hard_failed"], [])

    def test_render_md(self):
        md = vsr.render_md(vsr.verify())
        self.assertIn("四源分工", md)
        self.assertIn("信源", md)


if __name__ == "__main__":
    unittest.main()
