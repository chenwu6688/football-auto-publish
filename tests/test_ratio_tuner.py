"""配比调参四道防呆（计划 8.1 / 11.5）单元测试。

覆盖：样本阈值冻结、死区、单次幅度上限、冷静期、探索配额托底、数据回收 join。
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
        "板块配比": {"转会动态": 25, "人物故事": 25, "中国足球": 20, "战术榜单": 30},
        "调整纪律": {"滚动窗口天数": 14, "最小样本": 30, "单次调整上限": 10,
                     "冷静期天数": 14, "死区阈值": 0.20, "探索配额": [0.10, 0.15]},
        "history": [],
    }


def obs_for(section, n, reads, day="2026-10-08"):
    return [{"date": day, "index": i, "section": section, "reads": reads, "retention": 0.3}
            for i in range(n)]


class TestDiscipline(unittest.TestCase):
    def test_frozen_when_sample_small(self):
        obs = []
        for sec in rt.PLAN_SECTIONS:
            obs += obs_for(sec, 5, 100)
        sug = rt.compute_suggestion(base_cfg(), obs, TODAY)
        self.assertFalse(sug["是否自动执行"])
        self.assertEqual(set(sug["冻结板块"]), set(rt.PLAN_SECTIONS))
        self.assertEqual(sug["建议配比"], base_cfg()["板块配比"])

    def test_dead_zone_no_change(self):
        obs = []
        for sec in rt.PLAN_SECTIONS:
            obs += obs_for(sec, 40, 100)   # 各板块篇均一致 → 差异 0
        sug = rt.compute_suggestion(base_cfg(), obs, TODAY)
        self.assertFalse(sug["是否自动执行"])
        self.assertEqual(sug["建议配比"], base_cfg()["板块配比"])

    def test_step_capped_at_10(self):
        obs = []
        # 转会动态远高于其他板块
        obs += obs_for("转会动态", 40, 1000)
        obs += obs_for("人物故事", 40, 100)
        obs += obs_for("中国足球", 40, 100)
        obs += obs_for("战术榜单", 40, 100)
        sug = rt.compute_suggestion(base_cfg(), obs, TODAY)
        self.assertTrue(sug["是否自动执行"])
        # 25% + 10 = 35%
        self.assertEqual(sug["建议配比"]["转会动态"], 35)
        self.assertEqual(sum(sug["建议配比"].values()), 100)

    def test_cooldown_skips_section(self):
        cfg = base_cfg()
        cfg["history"] = [{"生效日期": "2026-10-05",   # 5 天内调整过转会动态
                           "变更前配比": {"转会动态": 25, "人物故事": 25, "中国足球": 20, "战术榜单": 30},
                           "变更后配比": {"转会动态": 30, "人物故事": 25, "中国足球": 20, "战术榜单": 25},
                           "变更原因": "测试"}]
        cfg["板块配比"] = {"转会动态": 30, "人物故事": 25, "中国足球": 20, "战术榜单": 25}
        obs = []
        obs += obs_for("转会动态", 40, 1000)
        obs += obs_for("人物故事", 40, 100)
        obs += obs_for("中国足球", 40, 100)
        obs += obs_for("战术榜单", 40, 100)
        sug = rt.compute_suggestion(cfg, obs, TODAY)
        self.assertIn("冷静期", sug["板块指标"]["转会动态"]["决策"])
        self.assertEqual(sug["建议配比"]["转会动态"], 30)   # 未被调整

    def test_explore_quota_floor(self):
        cfg = base_cfg()
        cfg["板块配比"] = {"转会动态": 10, "人物故事": 40, "中国足球": 20, "战术榜单": 30}
        obs = []
        obs += obs_for("转会动态", 40, 100)     # 最差 → 想继续下调
        obs += obs_for("人物故事", 40, 1000)
        obs += obs_for("中国足球", 40, 200)
        obs += obs_for("战术榜单", 40, 200)
        sug = rt.compute_suggestion(cfg, obs, TODAY)
        self.assertGreaterEqual(sug["建议配比"]["转会动态"], 10)   # 被 10% 托底
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
        self.assertEqual(obs[0]["section"], "转会动态")   # 转会资讯 → 转会动态
        self.assertEqual(obs[0]["reads"], 5000)


class TestSectionMap(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(rt.resolve_section("转会资讯", None), "转会动态")
        self.assertEqual(rt.resolve_section("排行榜", "数据盘点"), "战术榜单")
        self.assertEqual(rt.resolve_section("未知类型", None), "其他")


if __name__ == "__main__":
    unittest.main()
