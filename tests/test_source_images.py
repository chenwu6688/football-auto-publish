"""新闻源正文图复用（source_images）与「规则-阈值相斥」守卫的单测。

三段：
  1. `source_images` 取值口径（四个历史字段名 + fixture 嵌套 + 脏 URL 过滤）；
  2. `orchestrator._img_topic` 的两步取图（直接取 / 按队名二元组关联取）；
  3. 两条防漂移硬项：六处 `search_images` 必须由 `_img_topic` 供料、
     配比目标必须与每批结构等价（改一处不同步另一处必红）。

负向断言是重点：只测「正常时通过」等于没测。
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import batch_mix  # noqa: E402
import content_mix  # noqa: E402
import source_images  # noqa: E402
import verify_batch_mix as vbm  # noqa: E402
import verify_cover_coverage as vcc  # noqa: E402


class TestImagesOf(unittest.TestCase):
    def test_fixture_direct(self):
        fx = {"source_images": [{"url": "https://a.example/1.jpg"}]}
        self.assertEqual(len(source_images.images_of(fx)), 1)

    def test_fixture_nested(self):
        """真实那一层是 source["fixture"]["source_images"] —— 首版就漏在这里。"""
        got = source_images.images_of({"fixture": {"source_images": ["https://a.example/2.jpg"]}})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["url"], "https://a.example/2.jpg")

    def test_media_scraper_images_field(self):
        self.assertEqual(len(source_images.images_of(
            {"images": [{"url": "https://a.example/3.jpg"}]})), 1)

    def test_facts_nested(self):
        self.assertEqual(len(source_images.images_of(
            {"facts": {"source_images": ["https://a.example/4.jpg"]}})), 1)

    def test_dirty_urls_dropped(self):
        dirty = ["/img/a.jpg", "data:image/png;base64,xx", "ftp://x/y.jpg", ""]
        self.assertEqual(source_images.images_of({"source_images": dirty}), [])

    def test_placeholder_dropped(self):
        self.assertEqual(source_images.images_of(
            {"source_images": ["https://x.example/loading.gif"]}), [])

    def test_dedupe_and_limit(self):
        got = source_images.images_of({"source_images": [
            f"https://e.example/{i}.jpg" for i in range(6)] +
            ["https://e.example/0.jpg"]})
        self.assertEqual(len(got), source_images.MAX_IMAGES)

    def test_non_dict_is_safe(self):
        self.assertEqual(source_images.images_of(None), [])
        self.assertEqual(source_images.images_of("x"), [])


class TestIndexAndLookup(unittest.TestCase):
    MD = {"all_fixtures": [
        {"home_team": "皇马", "away_team": "巴萨",
         "source_images": [{"url": "https://a.example/m.jpg"}], "source_url": "u1"},
        {"home_team": "拜仁", "away_team": "多特", "source_images": []},
    ]}
    FORMS = staticmethod(lambda n: frozenset({n}))

    def test_index_only_keeps_fixtures_with_images(self):
        idx = source_images.build_index(self.MD)
        self.assertEqual(len(idx), 1)
        self.assertEqual(idx[0]["home"], "皇马")

    def test_index_cached_on_match_data(self):
        md = {"all_fixtures": list(self.MD["all_fixtures"])}
        source_images.get_index(md)
        self.assertIn("_source_img_index", md)

    def test_lookup_requires_both_teams(self):
        idx = source_images.build_index(self.MD)
        self.assertEqual(len(source_images.lookup(idx, "皇马3-1巴萨", self.FORMS)), 1)
        # 只提到一支 → 不配（配错图是事实错误，无图有数据卡兜底）
        self.assertEqual(source_images.lookup(idx, "皇马又赢了", self.FORMS), [])

    def test_lookup_empty_inputs(self):
        self.assertEqual(source_images.lookup([], "皇马打巴萨", self.FORMS), [])
        self.assertEqual(source_images.lookup(
            source_images.build_index(self.MD), "", self.FORMS), [])

    def test_same_city_alias_overlap_skipped(self):
        idx = [{"home": "上海海港", "away": "上海申花",
                "images": [{"url": "https://a.example/s.jpg"}]}]
        forms = lambda n: frozenset({n, "上海"})   # noqa: E731
        self.assertEqual(source_images.lookup(idx, "上海海港对阵上海申花", forms), [])


class TestImgTopic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import orchestrator
        cls.o = orchestrator

    def test_direct_field(self):
        t = self.o._img_topic({"title": "x"}, {"source_images": ["https://a.example/1.jpg"]})
        self.assertEqual(len(t["source_images"]), 1)

    def test_nested_fixture_field(self):
        t = self.o._img_topic({"title": "x"},
                              {"fixture": {"source_images": ["https://a.example/2.jpg"]}})
        self.assertEqual(len(t["source_images"]), 1)

    def test_lookup_by_teams_when_candidate_has_no_images(self):
        md = {"all_fixtures": [{"home_team": "皇家马德里", "away_team": "巴塞罗那",
                                "source_images": ["https://a.example/3.jpg"]}]}
        t = self.o._img_topic({"title": "皇马3-1巴萨，第70分钟换人后连丢2球",
                               "keywords_cn": ["皇马", "巴萨"]}, {}, match_data=md)
        self.assertEqual(len(t["source_images"]), 1)

    def test_no_image_when_only_one_team(self):
        md = {"all_fixtures": [{"home_team": "皇家马德里", "away_team": "巴塞罗那",
                                "source_images": ["https://a.example/4.jpg"]}]}
        t = self.o._img_topic({"title": "曼联又输了", "keywords_cn": ["曼联"]},
                              {}, match_data=md)
        self.assertNotIn("source_images", t)

    def test_no_image_without_match_data(self):
        t = self.o._img_topic({"title": "皇马3-1巴萨"}, {})
        self.assertNotIn("source_images", t)


class TestWiringGuards(unittest.TestCase):
    def test_all_search_images_fed_by_img_topic(self):
        """8/8 处 search_images 必须由 _img_topic 供料（逐条路径都拿得到正文图）。"""
        r = vcc.check_img_topic_wiring()
        self.assertTrue(r["ok"], r["detail"])

    def test_wiring_guard_catches_bypass(self):
        """负向：写一条绕过 _img_topic 的调用，硬项必须 FAIL。"""
        src = (
            "def ok_one():\n"
            "    t = _img_topic({'title': 'x'})\n"
            "    return search_images(t, count=3)\n"
            "\n"
            "def bad_one():\n"
            "    return search_images({'title': 'y'}, count=3)\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "orch.py"
            p.write_text(src, encoding="utf-8")
            orig = vcc.ORCHESTRATOR
            vcc.ORCHESTRATOR = p
            try:
                r = vcc.check_img_topic_wiring()
            finally:
                vcc.ORCHESTRATOR = orig
        self.assertFalse(r["ok"])
        self.assertTrue(any("1/2" in d for d in r["detail"]), r["detail"])

    def test_target_matches_structure(self):
        """配比目标必须等于结构能产出的份额（容差内）。"""
        r = vbm._check_structure_matches_target()
        self.assertTrue(r["ok"], r["detail"])

    def test_target_guard_catches_drift(self):
        """负向：把排行榜目标改到 45%，结构只产 16.7% → 必须红。"""
        orig = dict(content_mix.TARGET_PCT)
        try:
            content_mix.TARGET_PCT["排行榜"] = 45.0
            r = vbm._check_structure_matches_target()
            self.assertFalse(r["ok"])
            self.assertTrue(any("排行榜" in i for i in r["issues"]), r["issues"])
        finally:
            content_mix.TARGET_PCT.clear()
            content_mix.TARGET_PCT.update(orig)

    def test_data_slot_not_monopolized(self):
        """一天 3 个 data 槽不能全给数据对比，否则排行榜目标不可达。"""
        day, seen = {}, []
        for _ in range(3):
            pool = [{"content_type": ct, "title": f"{ct}{i}"}
                    for ct in ("热点球评", "数据对比", "排行榜") for i in range(5)]
            picked, _rep = batch_mix.assign(pool, 2, day_counts=day)
            ct = content_mix.normalize(picked[-1]["content_type"])
            seen.append(ct)
            day[ct] = day.get(ct, 0) + 1
        self.assertEqual(set(seen), {"数据对比", "排行榜"}, seen)


if __name__ == "__main__":
    unittest.main()
