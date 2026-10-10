import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ranker


class TestRankerLLMWiring(unittest.TestCase):
    """计划 11.2：L3 模型打分真实启用，且仅重排不删题、失败自动降级。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "ranker_cache.json"
        self._orig = ranker.CACHE_PATH
        ranker.CACHE_PATH = self.tmp

    def tearDown(self):
        ranker.CACHE_PATH = self._orig

    def _topics(self):
        return [
            {"title": "A", "content_type": "热点球评", "keywords_cn": ["曼城"],
             "column_name": "热点球评", "angle": "防守崩了"},
            {"title": "B", "content_type": "人物故事", "keywords_cn": ["皇马"],
             "column_name": "人物故事", "angle": "老将逆袭"},
        ]

    def test_llm_mode_reorders_without_dropping(self):
        import orchestrator as o

        def fake_call(messages):
            # 让 topic-1（B）总分高于 topic-0（A）
            return {"scores": [
                {"id": "topic-1", "冲突度": 5, "人物知名度": 5, "时效性": 5,
                 "话题延展性": 5, "粉丝画像匹配度": 5},
                {"id": "topic-0", "冲突度": 1, "人物知名度": 1, "时效性": 1,
                 "话题延展性": 1, "粉丝画像匹配度": 1},
            ]}, "test-model"

        topics = self._topics()
        with mock.patch.object(o, "_ranker_llm_call", side_effect=fake_call), \
             mock.patch.object(ranker, "awareness_of", return_value="A"):
            res = o._rank_topics(topics, {"news_articles": []}, "2026-10-10")
        self.assertEqual(res["mode"], "llm")
        self.assertEqual(len(topics), 2)             # 不删题
        self.assertEqual(topics[0]["title"], "B")    # 模型分高的排前

    def test_llm_failure_degrades_to_rule(self):
        import orchestrator as o

        def boom(messages):
            raise RuntimeError("额度耗尽")

        topics = self._topics()
        with mock.patch.object(o, "_ranker_llm_call", side_effect=boom), \
             mock.patch.object(ranker, "awareness_of", return_value="A"):
            res = o._rank_topics(topics, {"news_articles": []}, "2026-10-10")
        self.assertEqual(res["mode"], "rule")        # 降级规则排序
        self.assertEqual(len(topics), 2)             # 仍不丢题

    def test_llm_adapter_delegates_to_call_llm_json(self):
        import orchestrator as o
        with mock.patch.object(o, "call_llm_json", return_value=({"scores": []}, "m")) as m:
            out, model = o._ranker_llm_call([{"role": "user", "content": "x"}])
        self.assertEqual(model, "m")
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
