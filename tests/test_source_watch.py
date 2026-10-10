import unittest

import ranker
import source_watch


class TestSourceWatch(unittest.TestCase):
    def test_watchlist_loaded(self):
        data = source_watch.load_watchlist()
        self.assertGreaterEqual(len(data.get("sources", [])), 10)

    def test_match_sources(self):
        self.assertIn("罗马诺", source_watch.match_sources("罗马诺：曼城接近签下维尔茨"))
        self.assertIn("图片报", source_watch.match_sources("图片报：拜仁有意某前锋"))
        self.assertEqual(source_watch.match_sources("一场普通的联赛战报"), [])

    def test_source_weight(self):
        self.assertEqual(source_watch.source_weight(["罗马诺"]), 1.0)
        self.assertGreater(source_watch.source_weight(["天空体育"]), 0)

    def test_signals_from_articles(self):
        arts = [
            {"title": "罗马诺：曼城谈妥维尔茨", "summary": "转会接近完成"},
            {"title": "曼城又赢了", "summary": "英超第8轮"},
            {"title": "皇马签下中卫", "summary": "西甲"},
        ]
        sig = source_watch.signals_from_articles(arts, entities={"曼城", "皇马"})
        self.assertEqual(sig["曼城"]["同题报道条数"], 2)
        self.assertGreater(sig["曼城"]["来源媒体权重"], 0)
        self.assertEqual(sig["皇马"]["同题报道条数"], 1)
        # 社媒/搜索不在本模块产出：未接入即「缺失」（不写 0 冒充），由 signal_sources 补齐
        self.assertNotIn("社媒讨论量", sig["曼城"])
        self.assertNotIn("搜索指数", sig["曼城"])

    def test_signals_from_match_data(self):
        md = {"news_articles": [{"title": "曼城 news"}],
              "transfer_news": [{"title": "曼城 转会"}]}
        sig = source_watch.signals_from_match_data(md, entities={"曼城"})
        self.assertEqual(sig["曼城"]["同题报道条数"], 2)


class TestRankerSignalLookup(unittest.TestCase):
    def test_l2_lookup_by_keyword(self):
        topics = [{"title": "曼城主场能否啃下铁桶阵？", "keywords_cn": ["曼城", "利物浦"]}]
        signals = {"曼城": {"同题报道条数": 3, "来源媒体权重": 0.9}}
        ranker.l2_enrich(topics, signals)
        self.assertEqual(topics[0]["_signals"]["同题报道条数"], 3)
        self.assertEqual(topics[0]["_signals"]["来源媒体权重"], 0.9)

    def test_l2_lookup_by_title_entity(self):
        topics = [{"title": "哈兰德又要爆发？曼城锋线隐忧", "keywords_cn": []}]
        signals = {"哈兰德": {"同题报道条数": 2, "来源媒体权重": 0.8}}
        ranker.l2_enrich(topics, signals)
        self.assertEqual(topics[0]["_signals"]["同题报道条数"], 2)

    def test_l2_missing_marked(self):
        topics = [{"title": "某队某场", "keywords_cn": []}]
        ranker.l2_enrich(topics, {})
        self.assertIn("同题报道条数", topics[0].get("_signal_missing", []))


if __name__ == "__main__":
    unittest.main()
