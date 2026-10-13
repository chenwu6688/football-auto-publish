"""配比调参四道防呆（计划 8.1 / 11.5）单元测试。

覆盖：样本阈值冻结、死区、单次幅度上限、冷静期、探索配额托底、数据回收 join。
调参轴 = 选题类型（R11 内容诊断，2026-10-13 起由「四板块」改为「选题类型」）。
"""
import sys
import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import ratio_tuner as rt

TODAY = datetime(2026, 10, 10)


def base_cfg():
    return {
        "生效日期": "2026-09-01",
        "选题类型配比": {"转会官宣": 20, "冲突舆情": 20, "中国足球": 20,
                     "战报比分": 20, "未分类": 20},
        "调整纪律": {"滚动窗口天数": 14, "最小样本": 30, "单次调整上限": 10,
                     "冷静期天数": 14, "死区阈值": 0.20, "探索配额": [0.10, 0.15]},
        "history": [],
    }


def obs_for(category, n, reads, day="2026-10-08"):
    return [{"date": day, "index": i, "category": category, "reads": reads, "retention": 0.3}
            for i in range(n)]


class TestDiscipline(unittest.TestCase):
    def test_frozen_when_sample_small(self):
        obs = []
        for sec in rt.PLAN_CATEGORIES:
            obs += obs_for(sec, 5, 100)
        sug = rt.compute_suggestion(base_cfg(), obs, TODAY)
        self.assertFalse(sug["是否自动执行"])
        self.assertEqual(set(sug["冻结类型"]), set(rt.PLAN_CATEGORIES))
        self.assertEqual(sug["建议配比"], base_cfg()["选题类型配比"])

    def test_dead_zone_no_change(self):
        obs = []
        for sec in rt.PLAN_CATEGORIES:
            obs += obs_for(sec, 40, 100)   # 各类型篇均一致 → 差异 0
        sug = rt.compute_suggestion(base_cfg(), obs, TODAY)
        self.assertFalse(sug["是否自动执行"])
        self.assertEqual(sug["建议配比"], base_cfg()["选题类型配比"])

    def test_step_capped_at_10(self):
        cats = rt.PLAN_CATEGORIES
        obs = obs_for(cats[0], 40, 1000)   # 转会官宣远高于其他类型
        for sec in cats[1:]:
            obs += obs_for(sec, 40, 100)
        sug = rt.compute_suggestion(base_cfg(), obs, TODAY)
        self.assertTrue(sug["是否自动执行"])
        # 20% + 10 = 30%（单次上限 10 个百分点）
        self.assertEqual(sug["建议配比"][cats[0]], 30)
        self.assertEqual(sum(sug["建议配比"].values()), 100)

    def test_cooldown_skips_category(self):
        cfg = base_cfg()
        cats = rt.PLAN_CATEGORIES
        sec, other = cats[0], cats[1]
        cur = dict(cfg["选题类型配比"])
        after = dict(cur)
        after[sec] = cur[sec] + 5
        after[other] = cur[other] - 5
        cfg["history"] = [{"生效日期": "2026-10-05",   # 5 天内调整过该类型
                           "变更前配比": cur, "变更后配比": after,
                           "变更原因": "测试"}]
        cfg["选题类型配比"] = after
        obs = obs_for(sec, 40, 1000)
        for s in cats[1:]:
            obs += obs_for(s, 40, 100)
        sug = rt.compute_suggestion(cfg, obs, TODAY)
        self.assertIn("冷静期", sug["类别指标"][sec]["决策"])
        self.assertEqual(sug["建议配比"][sec], after[sec])   # 未被调整

    def test_explore_quota_floor(self):
        cfg = base_cfg()
        cats = rt.PLAN_CATEGORIES
        worst = cats[0]
        cfg["选题类型配比"] = {worst: 10, cats[1]: 40, cats[2]: 20, cats[3]: 20, cats[4]: 10}
        obs = obs_for(worst, 40, 100)     # 最差 → 想继续下调
        obs += obs_for(cats[1], 40, 1000)
        obs += obs_for(cats[2], 40, 200)
        obs += obs_for(cats[3], 40, 200)
        obs += obs_for(cats[4], 40, 200)
        sug = rt.compute_suggestion(cfg, obs, TODAY)
        self.assertGreaterEqual(sug["建议配比"][worst], 10)   # 被 10% 托底
        self.assertEqual(sum(sug["建议配比"].values()), 100)


class TestCollectObservations(unittest.TestCase):
    def test_join_performance_and_metadata(self):
        tmp = Path(tempfile.mkdtemp())
        d = "2026-10-08"
        (tmp / d).mkdir(parents=True)
        (tmp / "performance_log.json").write_text(json.dumps({
            "articles": {f"{d}/article-1": {"date": d, "index": 1, "reads": 5000,
                                            "retention_rate": 0.4}}}), encoding="utf-8")
        (tmp / d / "metadata.json").write_text(json.dumps({
            "articles": [{"index": 1, "content_type": "转会资讯", "column_name": "转会雷达"}]}),
            encoding="utf-8")
        old = rt.OUTPUT_DIR
        rt.OUTPUT_DIR = tmp
        try:
            obs = rt.collect_observations(14, TODAY)
        finally:
            rt.OUTPUT_DIR = old
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["category"], "转会官宣")   # 转会资讯 → 转会类
        self.assertEqual(obs[0]["reads"], 5000)


class TestMetadataFallback(unittest.TestCase):
    """无本地台账时回落 metadata.articles[].performance（CI 干净检出场景）。"""

    def _mk(self, articles):
        tmp = Path(tempfile.mkdtemp())
        d = "2026-10-08"
        (tmp / d).mkdir(parents=True)
        (tmp / d / "metadata.json").write_text(
            json.dumps({"articles": articles}, ensure_ascii=False), encoding="utf-8")
        return tmp

    def test_fallback_when_no_ledger(self):
        tmp = self._mk([{"index": 1, "slug": "a-1", "content_type": "转会资讯",
                         "column_name": "转会雷达",
                         "performance": {"reads": 1200, "retention_rate": 0.35}}])
        old = rt.OUTPUT_DIR
        rt.OUTPUT_DIR = tmp
        try:
            obs = rt.collect_observations(14, TODAY)
        finally:
            rt.OUTPUT_DIR = old
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["reads"], 1200)
        self.assertEqual(obs[0]["category"], "转会官宣")

    def test_ledger_wins_and_no_double_count(self):
        tmp = self._mk([{"index": 1, "slug": "a-1", "content_type": "转会资讯",
                         "column_name": "转会雷达",
                         "performance": {"reads": 1200, "retention_rate": 0.35}}])
        (tmp / "performance_log.json").write_text(json.dumps({"articles": {
            "2026-10-08/a-1": {"date": "2026-10-08", "index": 1, "slug": "a-1",
                               "reads": 5000, "retention_rate": 0.4}}}), encoding="utf-8")
        old = rt.OUTPUT_DIR
        rt.OUTPUT_DIR = tmp
        try:
            obs = rt.collect_observations(14, TODAY)
        finally:
            rt.OUTPUT_DIR = old
        self.assertEqual(len(obs), 1, "台账与 metadata 不应重复计数")
        self.assertEqual(obs[0]["reads"], 5000, "同一篇以台账为准")

    def test_metric_without_reads_skipped(self):
        tmp = self._mk([{"index": 2, "slug": "b-2", "content_type": "排行榜",
                         "column_name": "数据盘点",
                         "performance": {"impressions": 900}}])
        old = rt.OUTPUT_DIR
        rt.OUTPUT_DIR = tmp
        try:
            obs = rt.collect_observations(14, TODAY)
        finally:
            rt.OUTPUT_DIR = old
        self.assertEqual(obs, [], "缺 reads 的记录不算一条观测（不冒充 0）")


class TestCategoryMap(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(rt.resolve_category(content_type="转会资讯"), "转会官宣")
        self.assertEqual(rt.resolve_category(topic_category="战报比分"), "战报比分")
        self.assertEqual(rt.resolve_category(title="皇马官宣签下姆巴佩"), "转会官宣")
        self.assertEqual(rt.resolve_category(title="国安3-1大胜海港"), "战报比分")
        self.assertEqual(rt.resolve_category(title="随便一条没有特征的标题"), "未分类")
        # topic_category 缺失且现场判定失败时，栏目兜底仍归转会类
        self.assertEqual(rt.resolve_category(column_name="转会雷达"), "转会官宣")


if __name__ == "__main__":
    unittest.main()
