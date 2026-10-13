"""择优模型分层调用（计划 11.2 / 12.4）单元测试。

覆盖：L1 预筛四类剔除与条数截断、L2 信号增强与缺失标记、L3 规则五维打分、
L4 缓存 24h 去重与 token 超限降级、模型打分（注入式）。
"""
import sys
import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ranker
from utils import CST

NOW = datetime(2026, 10, 10, 20, 0, tzinfo=CST)


def card(cid, subj, action="转会", occurred="2026-10-10", angles=None, life=None):
    c = {"id": cid, "主体": subj, "动作": action, "数值": None, "发生时间": occurred,
         "来源列表": [{"来源名": "zhibo8", "地址": "http://z"}], "可信度": "单源",
         "可用角度": angles or ["角度1", "角度2", "角度3"], "生命周期": life or "进行中"}
    return c


class TestL1Prefilter(unittest.TestCase):
    def setUp(self):
        ranker._awareness_cache = {"曼城": "A", "哈兰德": "A", "科隆": "C"}

    def test_drops_expired_and_stale_and_cold(self):
        cands = [{"card": card("c1", "曼城"), "板块": "战术榜单"},
                 {"card": card("c2", "曼城", life="已过期"), "板块": "战术榜单"},
                 {"card": card("c3", "曼城", occurred="2026-09-01"), "板块": "战术榜单"},
                 {"card": card("c4", "科隆"), "板块": "战术榜单"}]
        kept, dropped = ranker.l1_prefilter(cands, {"now": NOW, "max_age_hours": 72})
        self.assertEqual([c["card"]["id"] for c in kept], ["c1"])
        self.assertEqual(len(dropped), 3)

    def test_drops_section_full(self):
        cands = [{"card": card("c1", "曼城"), "板块": "中国足球"}]
        kept, dropped = ranker.l1_prefilter(
            cands, {"now": NOW, "section_remaining": {"中国足球": 0}})
        self.assertEqual(kept, [])
        self.assertIn("板块配比已满", dropped[0][1])

    def test_truncates_to_target(self):
        cands = [{"card": card(f"c{i}", "曼城"), "板块": "战术榜单"} for i in range(30)]
        kept, dropped = ranker.l1_prefilter(cands, {"now": NOW}, target_max=20)
        self.assertEqual(len(kept), 20)
        self.assertEqual(len(dropped), 10)


class TestL2Enrich(unittest.TestCase):
    def test_attaches_signals_and_marks_missing(self):
        cands = [{"card": card("c1", "曼城")}]
        ranker.l2_enrich(cands, {"曼城": {"同题报道条数": 5}})
        sig = cands[0]["_signals"]
        self.assertEqual(sig["同题报道条数"], 5)
        self.assertIn("社媒讨论量", cands[0]["_signal_missing"])

    def test_source_weight_fallback(self):
        cands = [{"card": card("c1", "曼城")}]
        ranker.l2_enrich(cands)
        self.assertEqual(cands[0]["_signals"]["来源媒体权重"], ranker._SOURCE_WEIGHT["zhibo8"])


class TestRuleScore(unittest.TestCase):
    def setUp(self):
        ranker._awareness_cache = {"曼城": "A", "科隆": "C"}

    def test_dims_in_range(self):
        sc = ranker.rule_score(card("c1", "曼城"), None, NOW)
        self.assertEqual(set(sc.keys()), set(ranker.DIMENSIONS))
        for v in sc.values():
            self.assertTrue(1 <= v <= 5)

    def test_famous_scores_higher_than_obscure(self):
        a = ranker.rule_total(card("a", "曼城"), None, NOW)
        b = ranker.rule_total(card("b", "科隆"), None, NOW)
        self.assertGreater(a, b)


class TestSignalBoost(unittest.TestCase):
    """计划（2026-10-13）：社媒信号真正进入规则五维，且不破坏无信号基线。"""

    def setUp(self):
        # 内马尔=B（基线 3，有空间验证加分）；曼城=A（基线 5，验证上限不破）
        ranker._awareness_cache = {"曼城": "A", "内马尔": "B", "科隆": "C"}

    def test_social_boosts_fame_when_high(self):
        c = card("x", "内马尔")
        base = ranker._dim_fame(c, {"_signals": {}})
        boosted = ranker._dim_fame(c, {"_signals": {"社媒讨论量": 200_000}})
        self.assertEqual(base, 3)                  # B 级基线
        self.assertEqual(boosted, 4)               # 命中 20万档 +1 → 4（单档，步长 +1）

    def test_boost_is_one_step_even_for_low_baseline(self):
        """低知名度主体（C 级基线 1）命中阈值也只 +1 → 2，不被抬到 B 级球星的 3。"""
        c = card("x", "科隆")
        self.assertEqual(ranker._dim_fame(c, {"_signals": {"社媒讨论量": 1154911}}), 2)

    def test_low_social_equals_baseline(self):
        c = card("x", "内马尔")
        self.assertEqual(ranker._dim_fame(c, {"_signals": {"社媒讨论量": 1000}}), 3)
        # 缺字段（无 _signals）也等于基线
        self.assertEqual(ranker._dim_fame(c, None), 3)

    def test_search_index_does_not_boost_extensibility(self):
        """搜索指数实测 97% 恒为 100（百度联想饱和），无区分度 → 明确不参与加分。

        锁死回归：防止后续有人「顺手」把它接回维度打分。
        """
        c = card("x", "曼城", angles=["角度1"])    # 角度 1 个 → 基线 3
        self.assertEqual(ranker._dim_extensibility(c, {"_signals": {"搜索指数": 100}}), 3)
        self.assertEqual(ranker._dim_extensibility(c, {"_signals": {"搜索指数": 90}}), 3)

    def test_fame_clamp_holds_upper_bound(self):
        c = card("x", "曼城")                       # A 级基线 5
        self.assertEqual(ranker._dim_fame(c, {"_signals": {"社媒讨论量": 200_000}}), 5)

    def test_no_signal_baseline_unchanged(self):
        """无 _signals（或全 0）时五维分与改动前逐维一致（手算基线写死）。"""
        c = card("m", "曼城")                       # 默认角度 3 个；发生时间=NOW 当天
        baseline = {"冲突度": 3, "人物知名度": 5, "时效性": 5,
                    "话题延展性": 5, "粉丝画像匹配度": 3}
        self.assertEqual(ranker.rule_score(c, None, NOW), baseline)
        self.assertEqual(
            ranker.rule_score(c, {"_signals": {"社媒讨论量": 0, "搜索指数": 0,
                                              "同题报道条数": 0}}, NOW), baseline)

    def test_rank_prefers_strong_signal_end_to_end(self):
        """两个除信号外完全相同的候选：强信号者 rule_total 更高且排在前。"""
        cands = [{"card": card("c1", "内马尔", angles=["角度1"]), "板块": "战术榜单"},
                 {"card": card("c2", "内马尔", angles=["角度1"]), "板块": "战术榜单"}]
        signals = {"c1": {"社媒讨论量": 200_000, "搜索指数": 90, "同题报道条数": 0}}
        c1 = {"card": card("c1", "内马尔", angles=["角度1"])}
        c1["_signals"] = signals["c1"]
        c2 = {"card": card("c2", "内马尔", angles=["角度1"]), "_signals": {}}
        self.assertGreater(ranker.rule_total(c1["card"], c1, NOW),
                           ranker.rule_total(c2["card"], c2, NOW))

        res = ranker.rank_candidates(cands, now=NOW, use_llm=False, signals=signals)
        self.assertEqual(res["mode"], "rule")
        self.assertEqual(res["ranking"][0]["id"], "c1")


class TestRankFlow(unittest.TestCase):
    def setUp(self):
        ranker._awareness_cache = {"曼城": "A", "哈兰德": "A", "科隆": "C"}
        self._orig = ranker.CACHE_PATH
        ranker.CACHE_PATH = __import__("pathlib").Path(tempfile.mktemp(suffix=".json"))

    def tearDown(self):
        ranker.CACHE_PATH = self._orig

    def test_rule_mode_without_llm(self):
        cands = [{"card": card("c1", "曼城"), "板块": "战术榜单"},
                 {"card": card("c2", "哈兰德"), "板块": "人物故事"}]
        res = ranker.rank_candidates(cands, now=NOW, use_llm=False)
        self.assertEqual(res["mode"], "rule")
        self.assertEqual(len(res["ranking"]), 2)
        self.assertGreaterEqual(res["ranking"][0]["总分"], res["ranking"][1]["总分"])

    def test_llm_mode_and_cache(self):
        calls = {"n": 0}

        def fake_llm(messages):
            calls["n"] += 1
            # 给"曼城"高分
            return {"scores": [
                {"id": "c1", "冲突度": 5, "人物知名度": 5, "时效性": 5,
                 "话题延展性": 5, "粉丝画像匹配度": 5},
                {"id": "c2", "冲突度": 1, "人物知名度": 1, "时效性": 1,
                 "话题延展性": 1, "粉丝画像匹配度": 1}]}, "fake-model"

        cands = [{"card": card("c1", "曼城"), "板块": "战术榜单"},
                 {"card": card("c2", "科隆"), "板块": "战术榜单"}]
        # 科隆 C 级会在 L1 被剔除，改用 A 级实体
        cands[1] = {"card": card("c2", "哈兰德"), "板块": "人物故事"}
        res = ranker.rank_candidates(cands, now=NOW, use_llm=True, call_llm=fake_llm)
        self.assertEqual(res["mode"], "llm")
        self.assertEqual(res["ranking"][0]["id"], "c1")
        self.assertEqual(res["ranking"][0]["总分"], 25)
        self.assertEqual(calls["n"], 1)

        # 第二次：24h 内命中缓存，不再调用模型
        res2 = ranker.rank_candidates(cands, now=NOW, use_llm=True, call_llm=fake_llm)
        self.assertEqual(res2["mode"], "llm")
        self.assertEqual(calls["n"], 1)

    def test_token_budget_degrades_to_rule(self):
        cache = {"date": NOW.strftime("%Y-%m-%d"), "judged": {}, "tokens_used": 10**9}
        ranker._save_cache(cache)
        cands = [{"card": card("c1", "曼城"), "板块": "战术榜单"}]
        res = ranker.rank_candidates(cands, now=NOW, use_llm=True,
                                     call_llm=lambda m: ({"scores": []}, "x"))
        self.assertEqual(res["mode"], "rule")
        self.assertTrue(any("token 超限" in n for n in res["notes"]))


if __name__ == "__main__":
    unittest.main()
