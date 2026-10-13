#!/usr/bin/env python3
"""选题分类与类别优先级单测（计划 5.1 / R11 内容诊断）。

覆盖：
  - 五类分类正例（转会官宣 / 冲突舆情 / 中国足球 / 战报比分 / 未分类）
  - 形态优先于主题（比分 + 中国足球词 → 战报比分）
  - content_type == "转会资讯" 直达路径
  - 边界：空 dict / 无 title / 非 dict 不抛异常
  - prioritize：tier 序 / 组内 score 降序 / 战报比分 cap 裁剪 / dropped 带原因 /
    输入不被 mutate / 空列表 / cap 参数覆盖 / 无 _category 裸 dict
  - 降级：load_config→None 时仍可分类、可排序（_FALLBACK，不阻塞）
  - 配置完整性与 selftest 全过
  - 轻量集成：合成选题列表跑 classify + prioritize 组合，验证 _category 落位与排序
    （select_topics 管线级重集成由 scripts/verify_topic_category.py 硬项 D 覆盖）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import topic_category as tc  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_transfer_positive(self):
        for t in ("罗马诺：皇马官宣签下姆巴佩，五年合同锁死核心",
                  "曼城转会费创纪录，赖斯续约谈判进入最后阶段"):
            with self.subTest(t=t):
                self.assertEqual(tc.classify({"title": t}), tc.CAT_TRANSFER, t)

    def test_transfer_content_type_path(self):
        # 标题不含任何转会词，仅靠 content_type=="转会资讯" 直达
        self.assertEqual(tc.classify({"title": "今晚有大事发生", "content_type": "转会资讯"}),
                         tc.CAT_TRANSFER)

    def test_conflict_positive(self):
        for t in ("VAR争议判罚引爆全场，两队球迷赛后互喷",
                  "更衣室内讧彻底曝光，主力前锋炮轰主帅战术"):
            with self.subTest(t=t):
                self.assertEqual(tc.classify({"title": t}), tc.CAT_CONFLICT, t)

    def test_china_positive(self):
        for t in ("国足世预赛26人名单出炉，三名新人首次入选",
                  "中超争冠进入白热化，申花海港同分进入决胜时刻"):
            with self.subTest(t=t):
                self.assertEqual(tc.classify({"title": t}), tc.CAT_CHINA, t)

    def test_scoreline_positive(self):
        for t in ("亚泰主场1-5惨败，无锡吴钩狂轰5球",
                  "荷兰2-2日本：傲慢的代价"):
            with self.subTest(t=t):
                self.assertEqual(tc.classify({"title": t}), tc.CAT_SCORELINE, t)

    def test_form_beats_theme(self):
        # 比分 + 中国足球词：形态优先 → 战报比分（不因「国安/中超」改判中国足球）
        self.assertEqual(tc.classify({"title": "国安3-1大胜鲁能，中超争冠主动权易手"}),
                         tc.CAT_SCORELINE)

    def test_angle_and_keywords_fields(self):
        self.assertEqual(
            tc.classify({"title": "一笔改变格局的交易", "angle": "官宣后的更衣室反应"}),
            tc.CAT_TRANSFER)
        self.assertEqual(
            tc.classify({"title": "本周看点合集", "keywords_cn": ["中超", "争冠"]}),
            tc.CAT_CHINA)

    def test_unclassified(self):
        for t in ({"title": "今晚的月亮真圆，适合静静看球"},
                  {"title": "梅西与C罗的商业版图之争，谁更会做生意"}):
            with self.subTest(t=t):
                self.assertEqual(tc.classify(t), tc.CAT_UNKNOWN, t)

    def test_edge_no_raise(self):
        for t in ({}, {"title": ""}, {"title": None}, {"angle": "官宣"}, "not a dict", None):
            with self.subTest(t=t):
                cat = tc.classify(t)
                self.assertIn(cat, (tc.CAT_TRANSFER, tc.CAT_CONFLICT, tc.CAT_CHINA,
                                    tc.CAT_SCORELINE, tc.CAT_UNKNOWN))

    def test_detail_fields(self):
        d = tc.classify_detail({"title": "亚泰主场1-5惨败"})
        self.assertEqual(d["category"], tc.CAT_SCORELINE)
        self.assertTrue(d["signals"])
        self.assertFalse(d["degraded"])


class TestPrioritize(unittest.TestCase):
    def test_tier_order(self):
        tps = [
            {"title": "中超争冠白热化，申花海港同分火并", "score": 80},
            {"title": "VAR争议判罚引爆全场，球迷互喷", "score": 90},
            {"title": "罗马诺：皇马官宣签下姆巴佩", "score": 70},
        ]
        kept, dropped = tc.prioritize(tps)
        self.assertEqual([k["title"] for k in kept],
                         [tps[2]["title"], tps[1]["title"], tps[0]["title"]])
        self.assertEqual(dropped, [])

    def test_score_desc_within_tier(self):
        tps = [{"title": "罗马诺：官宣第一签", "score": 60},
               {"title": "曼城转会费创纪录", "score": 90}]
        kept, _ = tc.prioritize(tps)
        self.assertEqual([k["title"] for k in kept], [tps[1]["title"], tps[0]["title"]])

    def test_scoreline_cap_drops_lowest(self):
        tps = [{"title": "亚泰主场1-5惨败", "score": 90},
               {"title": "荷兰2-2日本：傲慢的代价", "score": 80},
               {"title": "曼联点球6-5利兹联", "score": 70}]
        kept, dropped = tc.prioritize(tps)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["title"], tps[0]["title"])
        self.assertEqual(len(dropped), 2)
        for d in dropped:
            self.assertEqual(d["category"], tc.CAT_SCORELINE)
            self.assertTrue(d.get("reason"))

    def test_all_scoreline_cap_keeps_one(self):
        tps = [{"title": "荷兰2-2日本：傲慢的代价", "score": 80},
               {"title": "曼联点球6-5利兹联", "score": 70}]
        kept, dropped = tc.prioritize(tps)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 1)

    def test_empty(self):
        self.assertEqual(tc.prioritize([]), ([], []))

    def test_cap_override(self):
        tps = [{"title": "亚泰主场1-5惨败", "score": 90},
               {"title": "荷兰2-2日本：傲慢的代价", "score": 80}]
        kept, dropped = tc.prioritize(tps, cap=0)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 2)

    def test_input_not_mutated(self):
        tps = [{"title": "亚泰主场1-5惨败", "score": 90},
               {"title": "罗马诺：皇马官宣签下姆巴佩", "score": 70},
               {"title": "荷兰2-2日本：傲慢的代价", "score": 80}]
        before = [t["title"] for t in tps]
        tc.prioritize(tps)
        self.assertEqual([t["title"] for t in tps], before)

    def test_bare_dict_without_category(self):
        # 无 _category 的裸 dict 也能排（内部现判）
        tps = [{"title": "亚泰主场1-5惨败", "score": 90},
               {"title": "罗马诺：皇马官宣签下姆巴佩", "score": 60}]
        kept, _ = tc.prioritize(tps)
        self.assertEqual(kept[0]["title"], tps[1]["title"])

    def test_non_scoreline_never_dropped(self):
        tps = [{"title": "罗马诺：皇马官宣签下姆巴佩", "score": 60},
               {"title": "VAR争议判罚引爆全场，球迷互喷", "score": 50},
               {"title": "国足世预赛26人名单出炉", "score": 40}]
        kept, dropped = tc.prioritize(tps, cap=0)
        self.assertEqual(len(kept), 3)
        self.assertEqual(dropped, [])


class TestDegraded(unittest.TestCase):
    def tearDown(self):
        tc.load_config = _ORIG_LOAD_CONFIG

    def test_degraded_classify_and_prioritize(self):
        tc.load_config = lambda *a, **k: None
        # 分类仍走 _FALLBACK，返回合法类别
        self.assertEqual(tc.classify({"title": "亚泰主场1-5惨败"}), tc.CAT_SCORELINE)
        self.assertEqual(tc.classify({"title": "官宣：五年合同锁死"}), tc.CAT_TRANSFER)
        d = tc.classify_detail({"title": "亚泰主场1-5惨败"})
        self.assertTrue(d["degraded"])
        # 排序仍工作（cap 走 _FALLBACK 默认 1）
        kept, dropped = tc.prioritize([
            {"title": "亚泰主场1-5惨败", "score": 90},
            {"title": "荷兰2-2日本：傲慢的代价", "score": 80},
            {"title": "罗马诺：皇马官宣签下姆巴佩", "score": 60}])
        self.assertEqual([k["title"] for k in kept][0], "罗马诺：皇马官宣签下姆巴佩")
        self.assertEqual(len(kept), 2)
        self.assertEqual(len(dropped), 1)


_ORIG_LOAD_CONFIG = tc.load_config


class TestConfig(unittest.TestCase):
    def test_config_loads(self):
        cfg = tc.load_config()
        self.assertIsNotNone(cfg, "config/topic_category.yaml 应可解析")
        for key in ("版本", "生效日期", "口径出处", "类别优先级", "单批软上限", "监控",
                    "比分模式", "赛果模式", "转会关键词", "冲突关键词", "中国足球关键词"):
            self.assertIn(key, cfg, f"缺字段 {key}")

    def test_priority_has_five(self):
        cfg = tc.load_config()
        pr = cfg["类别优先级"]
        for c in (tc.CAT_TRANSFER, tc.CAT_CONFLICT, tc.CAT_CHINA, tc.CAT_UNKNOWN, tc.CAT_SCORELINE):
            self.assertIn(c, pr)

    def test_monitor_thresholds_in_range(self):
        mon = tc.load_config()["监控"]
        for k in ("战报比分占比阈值", "未分类占比阈值"):
            v = float(mon[k])
            self.assertTrue(0.0 < v < 1.0, f"{k}={v} 越界")

    def test_patterns_compile(self):
        import re
        for key in ("比分模式", "赛果模式"):
            for pat in (tc.load_config()[key] or []):
                re.compile(str(pat))

    def test_selftest_all_pass(self):
        bad = [n for n, ok in tc.selftest() if not ok]
        self.assertEqual(bad, [], f"冒烟未通过：{bad}")


class TestIntegrationLight(unittest.TestCase):
    """轻量集成：模拟 select_topics 的打标 +  prioritize 组合，验证 _category 落位与排序。
    select_topics 管线级回归（含真实 metadata topics）由 scripts/verify_topic_category.py
    硬项 D 覆盖，此处不重复接线。"""

    def test_tag_then_prioritize_pipeline(self):
        raw = [
            {"title": "亚泰主场1-5惨败，无锡吴钩狂轰5球", "score": 95, "content_type": "热点球评"},
            {"title": "罗马诺：皇马官宣签下姆巴佩，五年合同锁死核心", "score": 88, "content_type": "转会资讯"},
            {"title": "更衣室内讧彻底曝光，主力前锋炮轰主帅战术", "score": 82, "content_type": "八卦趣事"},
            {"title": "国安3-1大胜鲁能，中超争冠主动权易手", "score": 90, "content_type": "热点球评"},
        ]
        # 1) 打标（与 orchestrator.select_topics 插入段同式）
        for t in raw:
            t["_category"] = tc.classify(t)
        self.assertEqual([t["_category"] for t in raw],
                         [tc.CAT_SCORELINE, tc.CAT_TRANSFER, tc.CAT_CONFLICT, tc.CAT_SCORELINE])
        # 2) 优先级 + 软上限
        kept, dropped = tc.prioritize(raw)
        self.assertEqual([t["_category"] for t in kept],
                         [tc.CAT_TRANSFER, tc.CAT_CONFLICT, tc.CAT_SCORELINE])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["topic"]["title"], raw[3]["title"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
