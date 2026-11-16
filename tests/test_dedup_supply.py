"""补位层去重（2026-11-16 改动点 1）。

背景：`dedup.apply` 只在候选表上去重，候选被丢掉后 `fill_gap` 会从库存取货、
把同一场补回来（11-10 晨读：`llm 2 → dedup 1 → batch_mix 2`）。
本文件锁住三件事：

  1. `dedup.exclusion_ctx` 只整理上下文，判据仍走 `dedup.judge`（不另写一套）；
  2. 补位命中去重时**不标记 used**（不悄悄消耗库存），且允许不足额；
  3. 一次 `fill_gap` 里已取走的条目会被记住，第 2 条同场取不出来。
"""

import tempfile
import unittest
from pathlib import Path

import dedup
import deterministic_supply as ds


class TestExclusionCtx(unittest.TestCase):
    def test_extra_titles_are_judged_as_written(self):
        """本批内存稿必须能挡住补位——补位发生在落盘之前，读不到 metadata。"""
        ctx = dedup.exclusion_ctx(None, extra_titles=["曼联4-4维拉，多古补时救主"])
        verdict, reason = dedup.judge(
            {"title": "曼联4-4维拉，多古补时救主", "keywords": ["曼联", "维拉"]}, ctx)
        self.assertEqual(verdict, "drop")
        self.assertIn(reason, ("fixture", "title", "event"))

    def test_unrelated_title_passes(self):
        ctx = dedup.exclusion_ctx(None, extra_titles=["曼联4-4维拉，多古补时救主"])
        verdict, _ = dedup.judge(
            {"title": "国米2-1逆转米兰，劳塔罗双响", "keywords": ["国米", "米兰"]}, ctx)
        self.assertEqual(verdict, "ok")

    def test_no_date_no_extra_returns_empty_ctx(self):
        """读不到历史时必须放行，不能把库存全判成重复（计划九）。"""
        ctx = dedup.exclusion_ctx(None)
        self.assertEqual(ctx["titles_today"], set())
        self.assertEqual(ctx["fixture_pairs"], set())
        self.assertEqual(dedup.judge({"title": "任意标题", "keywords": []}, ctx)[0], "ok")

    def test_remember_accumulates_within_batch(self):
        """同一批逐条取货时，先取的条目要能挡住后一条同场。"""
        ctx = dedup.exclusion_ctx(None)
        dedup.remember(ctx, "曼联4-4维拉，多古补时救主")
        self.assertEqual(
            dedup.judge({"title": "曼联4-4维拉，8轮不胜创队史",
                         "keywords": ["曼联", "维拉"]}, ctx)[0], "drop")


class TestFillGapExclusion(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir, self._orig_path = ds.STORE_DIR, ds.STORE_PATH
        ds.STORE_DIR = self.tmp
        ds.STORE_PATH = self.tmp / "items.json"
        self.day = "2026-11-16"

    def tearDown(self):
        ds.STORE_DIR, ds.STORE_PATH = self._orig_dir, self._orig_path

    def _stock(self, title, keywords=None):
        items = ds._load_store().get("items", [])
        items.append({"id": f"it{len(items)}", "status": "stocked", "title": title,
                      "keywords": keywords or [], "keywords_cn": keywords or [],
                      "section": "转会动态", "content_type": "转会资讯",
                      "content": "x" * 300,
                      "publish_from": "2026-11-10", "publish_by": "2026-11-30"})
        ds._save_store({"items": items})

    def test_duplicate_is_skipped_and_kept_stocked(self):
        self._stock("曼联4-4维拉，多古补时救主", ["曼联", "维拉"])
        self._stock("国米2-1逆转米兰，劳塔罗双响", ["国米", "米兰"])
        ctx = dedup.exclusion_ctx(None, extra_titles=["曼联4-4维拉，多古补时救主"])
        stats = {"补位拒绝重复": 0}
        picked = ds.fill_gap(2, date_str=self.day, exclude=ctx, stats=stats)
        self.assertEqual([p["title"] for p in picked],
                         ["国米2-1逆转米兰，劳塔罗双响"])
        self.assertEqual(stats["补位拒绝重复"], 1)
        # 被排除的条目**不标记 used**：留在库里等 publish_by 自然过期
        items = ds._load_store()["items"]
        dup = [i for i in items if i["title"].startswith("曼联")]
        self.assertEqual(dup[0]["status"], "stocked")
        self.assertFalse(dup[0].get("used_at"))

    def test_short_fill_is_allowed(self):
        """宁可少发一篇，也不补一条重复的。"""
        self._stock("曼联4-4维拉，多古补时救主", ["曼联", "维拉"])
        ctx = dedup.exclusion_ctx(None, extra_titles=["曼联4-4维拉，多古补时救主"])
        self.assertEqual(ds.fill_gap(2, date_str=self.day, exclude=ctx), [])

    def test_callable_exclude(self):
        self._stock("A 稿", ["甲队", "乙队"])
        self._stock("B 稿", ["丙队", "丁队"])
        picked = ds.fill_gap(2, date_str=self.day,
                             exclude=lambda it: (it["title"] != "A 稿", "callable 拒绝"))
        self.assertEqual([p["title"] for p in picked], ["B 稿"])

    def test_exclude_exception_is_not_a_pass(self):
        """判据自己炸了不能当通过——否则「闸门崩了」会表现成「重复照发」。"""
        self._stock("A 稿", ["甲队", "乙队"])

        def boom(item):
            raise RuntimeError("判据炸了")

        stats = {"补位拒绝重复": 0}
        self.assertEqual(ds.fill_gap(1, date_str=self.day, exclude=boom, stats=stats), [])
        self.assertEqual(stats["补位拒绝重复"], 1)

    def test_second_pick_in_same_call_sees_the_first(self):
        """一次取 2 条时，第 2 条必须知道第 1 条已被本批占用。"""
        self._stock("曼联4-4维拉，多古补时救主", ["曼联", "维拉"])
        self._stock("曼联4-4维拉，8轮不胜创队史", ["曼联", "维拉"])
        stats = {"补位拒绝重复": 0}
        picked = ds.fill_gap(2, date_str=self.day,
                             exclude=dedup.exclusion_ctx(None), stats=stats)
        self.assertEqual(len(picked), 1)
        self.assertEqual(stats["补位拒绝重复"], 1)


if __name__ == "__main__":
    unittest.main()
