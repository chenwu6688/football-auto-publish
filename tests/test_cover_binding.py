import unittest
from unittest import mock

import cover_binding as cb


class TestCoverBinding(unittest.TestCase):
    def test_article_entities_detected(self):
        arts = cb.article_entities({"title": "曼城主场能否啃下铁桶阵？",
                                    "content": "利物浦的萨拉赫状态火热。"})
        self.assertIn("曼城", arts)
        self.assertTrue(any("利物浦" == e or e.startswith("利物浦") for e in arts))

    def test_generic_query_detection(self):
        for q in ("football", "soccer", "stadium", "football match stadium", "football match"):
            self.assertTrue(cb.is_generic_query(q), q)
        self.assertFalse(cb.is_generic_query("哈兰德 football"))

    def test_build_bound_query_prefers_entities(self):
        q = cb.build_bound_query(["哈兰德", "曼城"], ["曼城主场能否"])
        self.assertIn("哈兰德", q)
        self.assertIn("曼城", q)

    def test_build_bound_query_empty_when_no_binding(self):
        # 只有通用词 → 不构成绑定，返回空
        self.assertEqual(cb.build_bound_query([], ["football", "stadium"]), "")

    def test_wikipedia_image_always_bound(self):
        self.assertTrue(cb.image_is_bound({"source": "wikipedia", "alt": ""}, []))

    def test_generic_stock_image_rejected(self):
        img = {"source": "unsplash", "alt": "football match stadium sports"}
        self.assertFalse(cb.image_is_bound(img, ["曼城"]))
        filtered = cb.filter_bound_images([img], ["曼城"])
        self.assertEqual(filtered, [])

    def test_entity_tagged_image_kept(self):
        img = {"source": "unsplash", "alt": "曼城 football"}
        self.assertTrue(cb.image_is_bound(img, ["曼城"]))
        self.assertEqual(len(cb.filter_bound_images([img], ["曼城"])), 1)


class TestSearchImagesBinding(unittest.TestCase):
    def _import(self):
        import data_collector as dc
        return dc

    def test_no_entities_no_title_terms_returns_empty(self):
        dc = self._import()
        with mock.patch.object(dc, "extract_search_entities", return_value=([], [], "")):
            with mock.patch.object(dc, "search_wikipedia", return_value=[]):
                with mock.patch.object(dc, "search_footyrenders", return_value=[]):
                    with mock.patch.object(dc, "UNSPLASH_KEY", "k"):
                        with mock.patch.object(dc.requests, "get") as g:
                            g.return_value.status_code = 200
                            g.return_value.json.return_value = {"results": []}
                            out = dc.search_images({"title": "？", "keywords": []}, count=5)
        self.assertEqual(out, [])

    def test_entity_topic_uses_bound_query(self):
        """图库 query 必须绑定正文实体，但**必须是英文**。

        2026-11-04 拿到 Pexels Key 后实测：图库是英文标签索引，
        query="皇家马德里" 返回酱油饺子、query="欧冠" 返回欧元纸币。
        所以旧契约「query 里要出现中文实体曼城」本身就是 bug 的成因，
        新契约是「query 必须是该实体的英文名，且任何情况下不得出现中文」。
        """
        dc = self._import()
        captured = []

        def fake_get(url, params=None, **kw):
            captured.append(params or {})
            m = mock.Mock()
            m.status_code = 200
            m.json.return_value = {"results": [
                {"urls": {"regular": "u1"}, "description": ""}]}
            return m

        with mock.patch.object(dc, "extract_search_entities", return_value=([], [], "")):
            with mock.patch.object(dc, "search_wikipedia", return_value=[]):
                with mock.patch.object(dc, "search_footyrenders", return_value=[]):
                    with mock.patch.object(dc, "UNSPLASH_KEY", "k"):
                        with mock.patch.object(dc.requests, "get", side_effect=fake_get):
                            out = dc.search_images({"title": "曼城主场能否啃下铁桶阵？",
                                                    "keywords": []}, count=3)
        qs = [str(p.get("query", "")) for p in captured]
        self.assertTrue(any("Manchester City" in q for q in qs), qs)
        # 负向：任何一条 query 都不许出现中文字符
        self.assertFalse([q for q in qs if any("\u4e00" <= c <= "\u9fff" for c in q)], qs)
        # 结果因 query 绑定而保留
        self.assertTrue(all(img.get("source") != "unsplash" or not cb.is_generic_query(img.get("query", ""))
                            for img in out))

    def test_generic_query_image_filtered(self):
        self.assertEqual(
            cb.filter_bound_images([{"source": "unsplash", "query": "football stadium", "alt": ""}], ["曼城"]),
            [])


if __name__ == "__main__":
    unittest.main()
