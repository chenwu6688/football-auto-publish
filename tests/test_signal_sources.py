import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import signal_sources as ss


class TestSignalSources(unittest.TestCase):
    """计划 11.2 L2：社媒/搜索信号接入，且绝不假装有信号。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_load_external_missing_returns_empty(self):
        self.assertEqual(ss.load_external(self.tmp / "nope.json"), {})

    def test_load_external_reads_signals(self):
        p = self.tmp / "ext.json"
        p.write_text(json.dumps({"signals": {"曼城": {"社媒讨论量": 12}}},
                                ensure_ascii=False), encoding="utf-8")
        self.assertEqual(ss.load_external(p), {"曼城": {"社媒讨论量": 12}})

    def test_build_signals_only_hits(self):
        ents = ["曼城", "不存在的队"]
        hot = [{"word": "曼城绝杀利物浦", "num": 34567, "rank": 1}]
        per_term = {"曼城": ["曼城vs利物浦", "曼城 转会", "天气"], "不存在的队": ["无关词"]}
        sig = ss.build_signals(ents, hot=hot, per_term=per_term)
        # 只含命中实体
        self.assertIn("曼城", sig)
        self.assertNotIn("不存在的队", sig)
        self.assertEqual(sig["曼城"]["社媒讨论量"], 34567)
        # 命中 2 个含实体的联想词 → 搜索指数 20
        self.assertEqual(sig["曼城"]["搜索指数"], 20)

    def test_build_signals_no_hit_gives_nothing(self):
        sig = ss.build_signals(["冷门队"], hot=[{"word": "别的", "num": 1, "rank": 5}],
                               per_term={"冷门队": ["无关"]})
        self.assertEqual(sig, {})   # 不写 0 冒充

    def test_merge_takes_max_per_field(self):
        a = {"曼城": {"同题报道条数": 3, "社媒讨论量": 5}}
        b = {"曼城": {"社媒讨论量": 9, "搜索指数": 40}}
        merged = ss.merge_with_forwarded(a, b)
        self.assertEqual(merged["曼城"]["同题报道条数"], 3)
        self.assertEqual(merged["曼城"]["社媒讨论量"], 9)
        self.assertEqual(merged["曼城"]["搜索指数"], 40)

    def test_write_external_roundtrip(self):
        p = ss.write_external({"曼城": {"搜索指数": 30}}, entities=["曼城"],
                              path=self.tmp / "e.json")
        self.assertEqual(ss.load_external(p), {"曼城": {"搜索指数": 30}})

    def test_fetch_baidu_suggest_parses_nonstandard_json(self):
        # 百度联想返回非标准 JSON（键无引号），必须能正确抽取 s 数组
        class _R:
            status_code = 200
            encoding = "gbk"
            apparent_encoding = "GB2312"
            text = 'cb({q:"曼城",p:false,s:["曼城","曼城赛程","曼城转会"]});'

            def raise_for_status(self):
                pass

        with mock.patch.object(ss.requests, "get", return_value=_R()):
            self.assertEqual(ss.fetch_baidu_suggest("曼城"),
                             ["曼城", "曼城赛程", "曼城转会"])


class TestRankerIntegration(unittest.TestCase):
    """缺失信号被 ranker 标记，命中信号进入 _signals。"""

    def test_ranker_marks_missing_when_no_social(self):
        import ranker
        cands = [{"card": {"id": "x", "主体": "曼城", "动作": "", "发生时间": "2026-10-10",
                           "可用角度": []}}]
        out = ranker.l2_enrich(cands, {"曼城": {"同题报道条数": 3}})
        missing = out[0]["_signal_missing"]
        self.assertIn("社媒讨论量", missing)
        self.assertIn("搜索指数", missing)
        self.assertEqual(out[0]["_signals"]["同题报道条数"], 3)


if __name__ == "__main__":
    unittest.main()
