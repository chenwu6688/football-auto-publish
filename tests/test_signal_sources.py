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
        self.no_redfox = self.tmp / "no_redfox.json"   # 隔离 RedFox 真实信号表

    def test_load_external_missing_returns_empty(self):
        self.assertEqual(ss.load_external(self.tmp / "nope.json", self.no_redfox), {})

    def test_load_external_reads_signals(self):
        p = self.tmp / "ext.json"
        p.write_text(json.dumps({"signals": {"曼城": {"社媒讨论量": 12}}},
                                ensure_ascii=False), encoding="utf-8")
        self.assertEqual(ss.load_external(p, self.no_redfox), {"曼城": {"社媒讨论量": 12}})

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
        self.assertEqual(ss.load_external(p, self.no_redfox), {"曼城": {"搜索指数": 30}})

    # ---- RedFox 预热信号（Phase 1 只读试点） ----
    def test_load_redfox_missing_returns_empty(self):
        self.assertEqual(ss.load_redfox(self.no_redfox), {})

    def test_load_redfox_reads_social_only(self):
        p = self.tmp / "rf.json"
        p.write_text(json.dumps({"signals": {"曼城": {"社媒讨论量": 1154911, "作品数": 22}},
                                 "hot_words": []}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(ss.load_redfox(p), {"曼城": {"社媒讨论量": 1154911}})

    def test_load_redfox_skips_non_numeric(self):
        p = self.tmp / "rf2.json"
        p.write_text(json.dumps({"signals": {"曼城": {"社媒讨论量": None},
                                            "皇马": "坏数据"}}, ensure_ascii=False),
                     encoding="utf-8")
        self.assertEqual(ss.load_redfox(p), {})

    def test_load_external_merges_redfox_takes_max(self):
        ext = self.tmp / "ext.json"
        ext.write_text(json.dumps({"signals": {"曼城": {"社媒讨论量": 12, "搜索指数": 30}}},
                                  ensure_ascii=False), encoding="utf-8")
        rf = self.tmp / "rf.json"
        rf.write_text(json.dumps({"signals": {"曼城": {"社媒讨论量": 1154911},
                                             "梅西": {"社媒讨论量": 88}}},
                                 ensure_ascii=False), encoding="utf-8")
        out = ss.load_external(ext, rf)
        self.assertEqual(out["曼城"]["社媒讨论量"], 1154911)   # 取较大值
        self.assertEqual(out["曼城"]["搜索指数"], 30)          # 原表字段保留
        self.assertEqual(out["梅西"]["社媒讨论量"], 88)        # 新实体并入

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


class TestFetchHotUnion(unittest.TestCase):
    """fetch_hot 改为百度榜 + 微博榜并集（百度优先、微博补充覆盖）。

    全部用 mock 打掉真实网络请求，绝不发真实请求、绝不写 data/ 下任何文件。
    """

    def test_union_baidu_first_dedup_by_word(self):
        baidu = [
            {"word": "曼城", "num": 100, "rank": 1},
            {"word": "皇马", "num": 90, "rank": 2},
        ]
        weibo = [
            {"word": "皇马", "num": 50, "rank": 1},    # 重合词（百度已有）
            {"word": "梅西", "num": 80, "rank": 2},    # 微博独有
        ]
        with mock.patch.object(ss, "fetch_baidu_hot", return_value=baidu), \
             mock.patch.object(ss, "fetch_weibo_hot", return_value=weibo):
            out = ss.fetch_hot()

        # 并集：百度词在前，重合词只出现一次
        self.assertEqual([d["word"] for d in out], ["曼城", "皇马", "梅西"])
        # 重合词「皇马」保留百度侧的 num 原值 90（微博侧为 50，未被覆盖/归一化）
        self.assertEqual(out[1]["word"], "皇马")
        self.assertEqual(out[1]["num"], 90)
        # 微博独有的词必须出现在结果里（本次修复核心价值）
        self.assertIn("梅西", [d["word"] for d in out])
        # rank 连续 1..n
        self.assertEqual([d["rank"] for d in out], [1, 2, 3])

    def test_weibo_only_word_present(self):
        baidu: list[dict] = []
        weibo = [
            {"word": "姆巴佩", "num": 70, "rank": 1},
            {"word": "C罗", "num": 60, "rank": 2},
        ]
        with mock.patch.object(ss, "fetch_baidu_hot", return_value=baidu), \
             mock.patch.object(ss, "fetch_weibo_hot", return_value=weibo):
            out = ss.fetch_hot()

        # 百度空、微博有 → 返回微博列表（rank 已重排）
        self.assertEqual([d["word"] for d in out], ["姆巴佩", "C罗"])
        self.assertEqual([d["rank"] for d in out], [1, 2])
        self.assertEqual(out[0]["num"], 70)
        self.assertIn("姆巴佩", [d["word"] for d in out])

    def test_both_empty(self):
        with mock.patch.object(ss, "fetch_baidu_hot", return_value=[]), \
             mock.patch.object(ss, "fetch_weibo_hot", return_value=[]):
            out = ss.fetch_hot()
        self.assertEqual(out, [])

    def test_weibo_exception_falls_to_empty(self):
        # 微博接口异常时 fetch_weibo_hot 自身返回 []，并集只含百度
        baidu = [{"word": "利物浦", "num": 40, "rank": 1}]
        weibo: list[dict] = []
        with mock.patch.object(ss, "fetch_baidu_hot", return_value=baidu), \
             mock.patch.object(ss, "fetch_weibo_hot", return_value=weibo):
            out = ss.fetch_hot()
        self.assertEqual([d["word"] for d in out], ["利物浦"])
        self.assertEqual(out[0]["rank"], 1)


class _FakeResp:
    """最小 requests.Response 替身：只提供本模块用到的接口。"""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ss.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class TestWeiboHotRequest(unittest.TestCase):
    """微博热搜接口的请求契约：必须带 Referer，否则被防盗链挡成 403。"""

    # 真实响应形如 {"ok":1,"data":{"realtime":[{"word_scheme":"#…#","note":"…","num":…}]}}
    # 注意：条目**没有** word 字段，纯词在 note 里。
    PAYLOAD = {"ok": 1, "data": {"realtime": [
        {"word_scheme": "#曼城绝杀利物浦#", "note": "曼城绝杀利物浦", "num": 1181819},
        {"word_scheme": "#国足新名单#", "note": "国足新名单", "num": 851789},
    ]}}

    def test_sends_referer_header(self):
        captured = {}

        def _fake_get(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            return _FakeResp(self.PAYLOAD)

        with mock.patch.object(ss.requests, "get", _fake_get):
            out = ss.fetch_weibo_hot()
        self.assertEqual(captured["headers"].get("Referer"), "https://weibo.com/")
        self.assertIn("User-Agent", captured["headers"])
        self.assertEqual(len(out), 2)

    def test_parses_note_when_word_absent(self):
        with mock.patch.object(ss.requests, "get",
                               lambda *a, **k: _FakeResp(self.PAYLOAD)):
            out = ss.fetch_weibo_hot()
        self.assertEqual([d["word"] for d in out], ["曼城绝杀利物浦", "国足新名单"])
        self.assertEqual([d["rank"] for d in out], [1, 2])
        self.assertEqual(out[0]["num"], 1181819)

    def test_403_returns_empty(self):
        with mock.patch.object(ss.requests, "get",
                               lambda *a, **k: _FakeResp({}, status=403)):
            self.assertEqual(ss.fetch_weibo_hot(), [])


if __name__ == "__main__":
    unittest.main()
