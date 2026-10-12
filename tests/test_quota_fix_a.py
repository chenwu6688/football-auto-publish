"""P0 修法 A（R14）：确定性供给占用批次配额 + 预测类剔除发布口径。

覆盖四个可回归点：
  1. `_deterministic_used_today` 每日预算计数（读今日 metadata 批次级 supply_layers）；
  2. `_generate_articles_from_topics` 的 index_offset（确定性文章先占位，
     主稿必须从 offset 之后编号，否则元组索引/配图错位）；
  3. 看板 collect 按 is_prediction（成稿级）+ _is_prediction 标题对拍（历史兜底）剔除；
  4. 配额验收 check_actual_batches 同样双口径剔除（对照实验：不剔除即判超配）。
"""

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import orchestrator as orch
import scripts.build_metrics_dashboard as m
import scripts.verify_batch_quota as vbq

CST = ZoneInfo("Asia/Shanghai")


class TestDeterministicUsedToday(unittest.TestCase):
    """每日预算计数：一级·确定性今日已用量（成稿级累计优先）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = orch.OUTPUT_DIR
        orch.OUTPUT_DIR = self.tmp

    def tearDown(self):
        orch.OUTPUT_DIR = self._orig

    def _write_meta(self, date_str, articles=None, supply_layers=None):
        d = self.tmp / date_str
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(
            json.dumps({"date": date_str, "articles": articles or [],
                        "supply_layers": supply_layers},
                       ensure_ascii=False), encoding="utf-8")

    def test_missing_metadata_returns_zero(self):
        self.assertEqual(orch._deterministic_used_today("2026-10-12"), 0)

    def test_counts_article_level_layers(self):
        # 成稿级 supply_layer 计数（CI 跨批合并后 articles 即当日累计）
        self._write_meta("2026-10-12", articles=[
            {"title": "A", "supply_layer": "一级·确定性"},
            {"title": "B", "supply_layer": "二级·突发"},
            {"title": "C", "supply_layer": "一级·确定性·节点"},
            {"title": "D", "supply_layer": "三级·软性"},
        ])
        self.assertEqual(orch._deterministic_used_today("2026-10-12"), 2)

    def test_ci_merge_scenario_article_level_wins(self):
        # CI 回写合并后：articles 为当日累计（2 篇确定性），
        # 而批次级 supply_layers 只留最后一批（1 篇）——必须以成稿级为准，
        # 否则每批都只看到 1、预算永远封不顶（每日 3 篇 = 50% 配比）。
        self._write_meta("2026-10-12",
                         articles=[{"title": "A", "supply_layer": "一级·确定性"},
                                   {"title": "B", "supply_layer": "一级·确定性"},
                                   {"title": "C", "supply_layer": "二级·突发"}],
                         supply_layers={"counts": {"一级·确定性": 1}})
        self.assertEqual(orch._deterministic_used_today("2026-10-12"), 2)

    def test_batch_level_fallback_for_legacy_data(self):
        # 改造前数据无成稿级 supply_layer → 回落批次级汇总
        self._write_meta("2026-10-12",
                         articles=[{"title": "A", "supply_layer": None}],
                         supply_layers={"counts": {"一级·确定性": 1}})
        self.assertEqual(orch._deterministic_used_today("2026-10-12"), 1)

    def test_null_layers_returns_zero(self):
        self._write_meta("2026-10-12", articles=[], supply_layers=None)
        self.assertEqual(orch._deterministic_used_today("2026-10-12"), 0)

    def test_corrupt_metadata_returns_zero(self):
        d = self.tmp / "2026-10-12"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text("{ broken json", encoding="utf-8")
        self.assertEqual(orch._deterministic_used_today("2026-10-12"), 0)

    def test_daily_budget_constant_is_two(self):
        # 日配额 6~8 × 计划 12.1 的 ~30% ≈ 2 篇/日
        self.assertEqual(orch.DETERMINISTIC_DAILY_BUDGET, 2)


class TestArticleIndexOffset(unittest.TestCase):
    """确定性文章先占位后，主稿生成必须按 offset 编号（索引/配图不错位）。"""

    def test_offset_shifts_tuple_index_and_images_map(self):
        stats = {"generated": 0, "valid": 0, "failed": 0, "issues": []}
        articles, images_map = [], {}
        topics = [{"title": f"主稿{i}", "content_type": "热点球评"} for i in range(2)]

        def _fake_gen(topic, match_data, idx, **kw):
            return {"title": topic["title"], "content": "x" * 300}, None

        with mock.patch.object(orch, "_has_source_for_topic", return_value=True), \
             mock.patch.object(orch, "_find_source_article", return_value=None), \
             mock.patch.object(orch, "search_images", return_value=[]), \
             mock.patch.object(orch, "generate_article_with_retry", side_effect=_fake_gen), \
             mock.patch.object(orch, "warn_if_low_info_increment", return_value=4):
            orch._generate_articles_from_topics(
                topics, 2, {}, images_map, stats, articles, index_offset=1)

        # 确定性文章占了位置 0，主稿必须落在 1、2（否则与确定性文章索引相撞）
        self.assertEqual([i for i, _ in articles], [1, 2])
        self.assertEqual(sorted(images_map.keys()), [1, 2])
        self.assertEqual(stats["valid"], 2)

    def test_zero_offset_keeps_legacy_numbering(self):
        stats = {"generated": 0, "valid": 0, "failed": 0, "issues": []}
        articles, images_map = [], {}
        topics = [{"title": "唯一主稿", "content_type": "热点球评"}]

        def _fake_gen(topic, match_data, idx, **kw):
            return {"title": topic["title"], "content": "x" * 300}, None

        with mock.patch.object(orch, "_has_source_for_topic", return_value=True), \
             mock.patch.object(orch, "_find_source_article", return_value=None), \
             mock.patch.object(orch, "search_images", return_value=[]), \
             mock.patch.object(orch, "generate_article_with_retry", side_effect=_fake_gen), \
             mock.patch.object(orch, "warn_if_low_info_increment", return_value=4):
            orch._generate_articles_from_topics(topics, 1, {}, images_map, stats, articles)

        self.assertEqual([i for i, _ in articles], [0])


class TestDashboardPredictionExclusion(unittest.TestCase):
    """看板发布口径剔除预测类（R13 决策④）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = m.OUTPUT_DIR
        m.OUTPUT_DIR = self.tmp
        self.now = datetime(2026, 10, 12, 12, 0, tzinfo=CST)
        ds = self.now.strftime("%Y-%m-%d")
        d = self.tmp / ds
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(json.dumps({
            "date": ds,
            "batches_completed": ["morning", "noon", "evening"],
            "articles": [
                {"index": 1, "title": "正式稿A", "column_name": "热点球评",
                 "sources_used": [{"url": "u"}]},
                # 成稿级 is_prediction 标（R13 决策④起新落 metadata）
                {"index": 2, "title": "预测稿B", "column_name": "热点球评",
                 "is_prediction": True},
                # 历史兜底：无成稿级标，只有 topics 级 _is_prediction，按标题对拍
                {"index": 3, "title": "预测稿C", "column_name": "人物故事"},
            ],
            "topics": [{"title": "预测稿C", "_is_prediction": True}],
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        m.OUTPUT_DIR = self._orig

    def test_predictions_excluded_from_publish_counts(self):
        data = m.collect(1, now=self.now)
        s = data["supply"]
        self.assertEqual(s["总篇数"], 1)            # 仅正式稿A
        self.assertEqual(s["预测类剔除"], 2)         # B（成稿级标）+ C（topics 对拍）
        self.assertEqual(s["板块分布"], {"热点球评": 1})
        self.assertEqual(s["溯源覆盖率"], 100.0)     # 1/1，预测稿不进分母

    def test_report_shows_exclusion_note(self):
        data = m.collect(1, now=self.now)
        comp = {"suspended": False, "false_content_count": 0}
        rep = m.build_report(data, comp, 1, now=self.now)
        md = m.render_markdown(rep)
        self.assertIn("已剔除预测类", md)
        self.assertIn("2 篇", md)


class TestQuotaExcludesPredictions(unittest.TestCase):
    """配额验收按 is_prediction 剔除预测稿（成稿级标 + topics 对拍双口径）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = vbq.OUTPUT_DIR
        vbq.OUTPUT_DIR = self.tmp
        self.ds = datetime.now(CST).strftime("%Y-%m-%d")

    def tearDown(self):
        vbq.OUTPUT_DIR = self._orig

    def _write_day(self, articles, topics=None):
        d = self.tmp / self.ds
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(json.dumps({
            "date": self.ds, "articles": articles, "topics": topics or [],
        }, ensure_ascii=False), encoding="utf-8")

    def test_flagged_prediction_not_counted_against_cap(self):
        # 晚间 2 篇正式 + 1 篇带标预测稿：剔除后 2 篇 = 运行时上限，不违规
        self._write_day([
            {"index": 1, "title": "正式1", "batch_name": "晚间"},
            {"index": 2, "title": "正式2", "batch_name": "晚间"},
            {"index": 3, "title": "预测X", "batch_name": "晚间", "is_prediction": True},
        ])
        res = vbq.check_actual_batches(days=1)
        self.assertEqual(res["batch_violations"], [])
        self.assertEqual(res["manual_violations"], [])

    def test_unflagged_prediction_would_violate(self):
        # 对照实验：同样的 3 篇若不带预测标 → 晚间 3 篇 > 上限 2，必须判违规
        # （证明上面的「不违规」确实是剔除逻辑的功劳，而非数据本身不超配）
        self._write_day([
            {"index": 1, "title": "正式1", "batch_name": "晚间"},
            {"index": 2, "title": "正式2", "batch_name": "晚间"},
            {"index": 3, "title": "预测X", "batch_name": "晚间"},
        ])
        res = vbq.check_actual_batches(days=1)
        self.assertTrue(any("晚间 3 篇" in v for v in res["batch_violations"]))

    def test_topics_level_prediction_fallback(self):
        # 历史数据：无成稿级标，仅 topics 级 _is_prediction，按标题对拍剔除
        self._write_day(
            [
                {"index": 1, "title": "正式1", "batch_name": "晚间"},
                {"index": 2, "title": "正式2", "batch_name": "晚间"},
                {"index": 3, "title": "预测Y", "batch_name": "晚间"},
            ],
            topics=[{"title": "预测Y", "_is_prediction": True}],
        )
        res = vbq.check_actual_batches(days=1)
        self.assertEqual(res["batch_violations"], [])


class TestPublisherSkipsPredictions(unittest.TestCase):
    """发布器显式跳过预测稿（R13 决策④）：frontmatter is_prediction 拦截。"""

    def setUp(self):
        import importlib
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["OUTPUT_DIR"] = str(self.tmp)
        import publisher
        importlib.reload(publisher)          # 重绑 OUTPUT_BASE 到临时目录
        self.publisher = publisher
        d = self.tmp / "2026-10-12"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(
            json.dumps({"date": "2026-10-12", "articles": []},
                       ensure_ascii=False), encoding="utf-8")
        (d / "article-1-a.md").write_text(
            '---\ntitle: "正式稿A"\ndate: 2026-10-12\nbatch_name: 晚间\n'
            "is_prediction: false\n---\n\n# 正式稿A\n\n正文。",
            encoding="utf-8")
        (d / "article-2-b.md").write_text(
            '---\ntitle: "预测稿B"\ndate: 2026-10-12\nbatch_name: \n'
            "is_prediction: true\n---\n\n# 预测稿B\n\n正文。",
            encoding="utf-8")

    def tearDown(self):
        os.environ.pop("OUTPUT_DIR", None)
        import importlib
        import publisher
        importlib.reload(publisher)          # 恢复默认 OUTPUT_BASE

    def test_prediction_skipped_with_batch_filter(self):
        arts = self.publisher.load_articles("2026-10-12", batch_filter="evening")
        self.assertEqual([a["title"] for a in arts], ["正式稿A"])

    def test_prediction_skipped_without_batch_filter(self):
        # 全量发布（无批次过滤）时同样必须拦截——此前降级路径会漏发
        arts = self.publisher.load_articles("2026-10-12")
        self.assertEqual([a["title"] for a in arts], ["正式稿A"])

    def test_legacy_articles_without_flag_still_load(self):
        # 改造前文章无 is_prediction 字段 → 不受影响（向后兼容）
        d = self.tmp / "2026-10-11"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(
            json.dumps({"date": "2026-10-11", "articles": []},
                       ensure_ascii=False), encoding="utf-8")
        (d / "article-1-old.md").write_text(
            '---\ntitle: "老文章"\ndate: 2026-10-11\nbatch_name: 晨读\n---\n\n# 老文章\n\n正文。',
            encoding="utf-8")
        arts = self.publisher.load_articles("2026-10-11")
        self.assertEqual([a["title"] for a in arts], ["老文章"])


class TestFrontmatterPredictionFlag(unittest.TestCase):
    """成稿落盘时把 is_prediction 写进 frontmatter（供发布器拦截）。"""

    def test_prediction_flag_written_to_frontmatter(self):
        from file_writer import FileWriter
        with tempfile.TemporaryDirectory() as tmp:
            fw = FileWriter(base_dir=tmp)
            res = fw.save_article("2026-10-12", 1, {
                "title": "预测稿", "content": "正文", "_is_prediction": True})
            text = Path(res["article_path"]).read_text(encoding="utf-8")
            self.assertIn("is_prediction: true", text)
            res2 = fw.save_article("2026-10-12", 2, {"title": "正式稿", "content": "正文"})
            text2 = Path(res2["article_path"]).read_text(encoding="utf-8")
            self.assertIn("is_prediction: false", text2)


class TestSupplyMixExcludesPredictions(unittest.TestCase):
    """供给配比验收同样剔除预测类（与看板/配额验收三处同口径）。"""

    def setUp(self):
        import scripts.verify_supply_mix as vsm
        self.vsm = vsm
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = vsm.OUTPUT_DIR
        vsm.OUTPUT_DIR = self.tmp

    def tearDown(self):
        self.vsm.OUTPUT_DIR = self._orig

    def test_load_day_excludes_predictions_dual_channel(self):
        d = self.tmp / "2026-10-12"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(json.dumps({
            "date": "2026-10-12",
            "articles": [
                {"title": "正式1", "supply_layer": "二级·突发"},
                {"title": "预测X", "supply_layer": "二级·突发", "is_prediction": True},
                {"title": "预测Y", "supply_layer": "二级·突发"},
            ],
            "topics": [{"title": "预测Y", "_is_prediction": True}],
        }, ensure_ascii=False), encoding="utf-8")
        arts = self.vsm._load_day("2026-10-12")
        self.assertEqual([a["title"] for a in arts], ["正式1"])


if __name__ == "__main__":
    unittest.main()
