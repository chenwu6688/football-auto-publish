#!/usr/bin/env python3
"""CI 元数据合并的回归测试（2026-11-05「台账自相矛盾」事故）。

## 事故形态

`output/2026-11-05/metadata.json` 里同时躺着这些字段：

    articles:              4 篇（今日累计）
    image_floor.articles_below: 0        ← 只反映最后一批那 1 篇

合并动作发生在 **CI 工作流的两段内联 python** 里，它们只合并 `articles` /
`batches_completed`，`image_floor` 直接沿用本批的值。于是一份文件里「今日累计」
和「本批统计」两个口径并存，验收读 `articles_below` 会得出「今天全达标」的结论，
而实际有 3 篇各只有 1 张图。

本组测试锁住：合并后 `articles_below` 必须按**合并后的文章列表**重算，且只统计
「有配图台账」的稿子（改造前的老产物没有台账，不参与判定——未判定 ≠ 通过）。
"""

import json
import tempfile
import unittest
from pathlib import Path

import image_ledger

ROOT = Path(__file__).resolve().parent.parent


def _load_merge_module():
    """scripts/ 不是包，按路径导入。"""
    import importlib.util
    p = ROOT / "scripts" / "merge_metadata.py"
    spec = importlib.util.spec_from_file_location("merge_metadata", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mm = _load_merge_module()


def _old_article(title, n_img):
    """改造前的老产物：没有 image_cards / image_anchors 两栏。"""
    return {"title": title, "content_type": "热点球评",
            "images": [f"a{i}.jpg" for i in range(n_img)]}


def _new_article(title, n_img, ct="热点球评"):
    """改造后的产物：固定写台账两栏（哪怕是空列表）。"""
    return {"title": title, "content_type": ct,
            "images": [f"n{i}.jpg" for i in range(n_img)],
            "image_cards": [], "image_anchors": []}


class TestImageLedgerJudge(unittest.TestCase):
    def test_missing_key_means_legacy(self):
        self.assertFalse(image_ledger.has_image_ledger(_old_article("旧", 1)))

    def test_empty_list_still_counts_as_tracked(self):
        # 新版即使一张原创信息图都没出，也会写空列表 —— 这是「有新台账」不是「没台账」
        a = {"title": "新", "images": ["x.jpg"], "image_cards": [], "image_anchors": []}
        self.assertTrue(image_ledger.has_image_ledger(a))

    def test_count_below_floor_skips_legacy(self):
        arts = [_old_article("旧1", 1), _old_article("旧2", 1),
                _new_article("新达标", 3), _new_article("新不足", 1)]
        n, titles = image_ledger.count_below_floor(arts, 3)
        self.assertEqual(n, 1, "只有「有台账且不足 3 张」的才算，老产物不参与")
        self.assertEqual(titles, ["新不足"])


class TestMergeMetadata(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.dir = Path(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def _write(self, name, obj):
        p = self.dir / name
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return p

    def test_merges_articles_and_recomputes_below(self):
        """复现事故现场：老 3 篇各 1 张 + 新 1 篇 3 张 → below 必须是 0（老的不计）。"""
        dst = self._write("dst.json", {
            "date": "2026-11-05", "batches_completed": ["noon"],
            "image_floor": {"since": "2026-11-05", "min_per_article": 3,
                            "articles_below": 0},
            "articles": [_old_article("旧1", 1), _old_article("旧2", 1),
                         _old_article("旧3", 1)]})
        src = self._write("src.json", {
            "date": "2026-11-05", "batches_completed": ["morning"],
            "image_floor": {"since": "2026-11-05", "min_per_article": 3,
                            "articles_below": 0},
            "articles": [_new_article("新1", 3)]})
        meta = mm.merge(src, dst)
        self.assertEqual(len(meta["articles"]), 4, "文章要跨批累计")
        self.assertEqual(meta["batches_completed"], ["morning", "noon"])
        self.assertEqual(meta["image_floor"]["articles_below"], 0)
        self.assertNotIn("below_titles", meta["image_floor"])

    def test_new_article_below_floor_is_counted(self):
        dst = self._write("dst.json", {
            "date": "2026-11-05", "batches_completed": ["noon"],
            "image_floor": {"since": "2026-11-05", "min_per_article": 3,
                            "articles_below": 0},
            "articles": [_old_article("旧1", 1)]})
        src = self._write("src.json", {
            "date": "2026-11-05", "batches_completed": ["morning"],
            "image_floor": {"since": "2026-11-05", "min_per_article": 3,
                            "articles_below": 0},
            "articles": [_new_article("新达标", 3), _new_article("新不足", 1)]})
        meta = mm.merge(src, dst)
        self.assertEqual(meta["image_floor"]["articles_below"], 1)
        self.assertEqual(meta["image_floor"]["below_titles"], ["新不足"])

    def test_dedup_same_title_and_type(self):
        a = _new_article("同一篇", 3)
        dst = self._write("dst.json", {
            "batches_completed": ["noon"],
            "image_floor": {"since": "2026-11-05", "min_per_article": 3},
            "articles": [a]})
        src = self._write("src.json", {
            "batches_completed": ["morning"], "articles": [dict(a)]})
        meta = mm.merge(src, dst)
        self.assertEqual(len(meta["articles"]), 1, "重复回写不该把同一篇算两遍")

    def test_no_image_floor_stays_absent(self):
        """改造前的批次没有 image_floor：不许凭空造一个（否则「未判定」会变「通过」）。"""
        dst = self._write("dst.json", {"batches_completed": ["noon"],
                                       "articles": [_old_article("旧1", 1)]})
        src = self._write("src.json", {"batches_completed": ["morning"],
                                       "articles": [_old_article("旧2", 1)]})
        meta = mm.merge(src, dst)
        self.assertNotIn("image_floor", meta)

    def test_floor_falls_back_from_dst_when_src_missing(self):
        dst = self._write("dst.json", {
            "batches_completed": ["noon"],
            "image_floor": {"since": "2026-11-05", "min_per_article": 3},
            "articles": [_new_article("新达标", 3)]})
        src = self._write("src.json", {"batches_completed": ["morning"],
                                       "articles": [_new_article("新不足", 1)]})
        meta = mm.merge(src, dst)
        self.assertEqual(meta["image_floor"]["since"], "2026-11-05")
        self.assertEqual(meta["image_floor"]["articles_below"], 1)


class TestWorkflowUsesScript(unittest.TestCase):
    """CI 里两处回写必须调同一个脚本，不许再各写一份内联合并。"""

    def test_batch_workflow_calls_merge_script(self):
        src = (ROOT / ".github" / "workflows" / "batch.yml").read_text(encoding="utf-8")
        self.assertEqual(src.count("scripts/merge_metadata.py"), 2,
                         "batch.yml 两处回写都要走统一脚本")
        self.assertNotIn('python3 -c "\n          import json\n          from pathlib import Path',
                         src, "内联合并脚本必须清掉（口径两处各写各的就会对不上）")


if __name__ == "__main__":
    unittest.main()
