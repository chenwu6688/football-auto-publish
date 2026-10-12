"""六个差异化方向单测（计划 13.5）。"""

import unittest

import differentiation as df


class TestConfig(unittest.TestCase):
    def test_six_directions(self):
        keys = [d["key"] for d in df.directions()]
        self.assertEqual(keys, df.EXPECTED)

    def test_fields_complete(self):
        self.assertEqual(df.selftest(), [])
        for d in df.directions():
            for f in ("依据", "落地要点", "files", "marker"):
                self.assertTrue(d.get(f), f"{d['key']} 缺 {f}")

    def test_bad_path(self):
        df.reset_cache()
        self.assertEqual(df.load_config("/nope/x.yaml")["方向"], [])
        df.reset_cache()


class TestCoverage(unittest.TestCase):
    def test_all_covered(self):
        self.assertEqual(df.missing(), [])
        cov = {c["key"]: c for c in df.coverage()}
        for k in df.EXPECTED:
            self.assertTrue(cov[k]["ok"], k)
            self.assertTrue(cov[k]["hit"])

    def test_coverage_shape(self):
        c = df.coverage()[0]
        self.assertIn("marker", c)
        self.assertIn("files", c)


class TestSelftestRender(unittest.TestCase):
    def test_render(self):
        self.assertIn("通过", df.render_selftest())


if __name__ == "__main__":
    unittest.main()
