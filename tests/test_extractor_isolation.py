import unittest

import extractor as ex


SRC = (
    "北京时间10月9日，英超第8轮，曼城主场3-1战胜利物浦。"
    "第12分钟，哈兰德接德布劳内直塞破门；第34分钟，萨拉赫点球扳平。"
    "第58分钟，哈兰德头球梅开二度。此役过后，曼城8轮积19分升至榜首。"
    "瓜迪奥拉赛后表示球队下半场强度是关键。"
)


class TestExtractorIsolation(unittest.TestCase):
    def test_regex_facts_are_structured_only(self):
        facts = ex.extract_facts(SRC, {"source": "zhibo8"}, call_llm=None)
        self.assertTrue(facts)
        # 每个事实只承载结构化字段
        for f in facts:
            self.assertEqual(set(f.keys()), {"主体", "动作", "数值", "时间", "来源"})
            self.assertEqual(f["来源"], "zhibo8")
        # 至少抓到比分/数字类事实
        joined = " ".join(f["数值"] for f in facts)
        self.assertIn("3-1" if "3-1" in SRC else "", joined + " ")

    def test_no_field_carries_original_sentence(self):
        facts = ex.extract_facts(SRC, {"source": "zhibo8"}, call_llm=None)
        for f in facts:
            for v in f.values():
                # 任何字段都不得是「整句原文」（≥12 字逐字重合）
                ok, _hit = ex.assert_no_verbatim(str(v), [SRC])
                # 单个主体名（如「哈兰德」）很短，允许；只要不是长句即可
                self.assertTrue(len(str(v)) < 12 or ok,
                                f"字段疑似原文句子: {v!r}")

    def test_facts_block_has_no_verbatim_overlap(self):
        facts = ex.extract_facts(SRC, {"source": "zhibo8"}, call_llm=None)
        block = ex.build_facts_block(facts, sources=[SRC])
        ok, hit = ex.assert_no_verbatim(block, [SRC], max_run=12)
        self.assertTrue(ok, f"事实块与源文逐字重合: {hit!r}")
        self.assertIn("结构化事实", block)

    def test_assert_no_verbatim_detects_copy(self):
        block = "- 第12分钟，哈兰德接德布劳内直塞破门"  # 与源文逐字重合
        ok, hit = ex.assert_no_verbatim(block, [SRC])
        self.assertFalse(ok)
        self.assertGreaterEqual(len(hit), 12)

    def test_facts_from_fixture(self):
        facts = ex.facts_from_fixture({
            "home_team": "曼城", "away_team": "利物浦",
            "home_score": 3, "away_score": 1, "league": "英超",
            "utc_date": "2026-10-10T19:00:00Z", "source": "zhibo8"})
        self.assertTrue(any(f["动作"] == "对阵" and f["数值"] == "3-1" for f in facts))

    def test_build_source_facts_block_end_to_end(self):
        src = {"article_text": SRC,
               "fixture": {"source": "zhibo8", "home_team": "", "away_team": "",
                           "article_text": SRC}}
        block, facts = ex.build_source_facts_block(src)
        self.assertTrue(facts)
        ok, hit = ex.assert_no_verbatim(block, [SRC])
        self.assertTrue(ok, f"生成器可见素材与原文重合: {hit!r}")

    def test_empty_source(self):
        self.assertEqual(ex.extract_facts("", None), [])
        self.assertIn("未产出", ex.build_facts_block([], sources=[]))


if __name__ == "__main__":
    unittest.main()
