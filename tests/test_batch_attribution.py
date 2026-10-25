#!/usr/bin/env python3
"""批次归属兜底回归（2026-10-25 预测稿事故）。

事故：晚间批次生成 3 篇，metadata 里第 3 篇 batch_name 为空、is_prediction=false。
发布器 `--batch=evening` 按 frontmatter 过滤只发 2 篇——第 3 篇（明日赛前预测）
既没被显式标记为预测稿，也没批次归属，只是「恰好」被过滤掉。一旦该批过滤 0 命中，
publisher 会降级发布全部文章，预测稿就会当真稿发到头条号。

本组测试锁住：
  1. 漏写 batch_name 的成稿必须被补齐（否则永远发不出去）；
  2. 已有归属不被覆盖（别的批次/预生成内容不得被改写）；
  3. 补归属不改变 is_prediction —— 预测稿仍由 publisher 显式跳过。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import orchestrator  # noqa: E402


class TestEnsureBatchAttribution(unittest.TestCase):
    def test_fills_missing_batch_name(self):
        arts = [{"title": "A"}, {"title": "B", "_batch_name": "晚间"}]
        filled = orchestrator.ensure_batch_attribution(arts, "晚间", "18:00")
        self.assertEqual(filled, 1)
        self.assertEqual(arts[0]["_batch_name"], "晚间")
        self.assertEqual(arts[0]["_batch_time"], "18:00")

    def test_keeps_existing_attribution(self):
        arts = [{"title": "A", "_batch_name": "晨读", "_batch_time": "08:00"}]
        filled = orchestrator.ensure_batch_attribution(arts, "晚间", "18:00")
        self.assertEqual(filled, 0)
        self.assertEqual(arts[0]["_batch_name"], "晨读")
        self.assertEqual(arts[0]["_batch_time"], "08:00")

    def test_prediction_flag_untouched(self):
        arts = [{"title": "明日预测", "_is_prediction": True}]
        orchestrator.ensure_batch_attribution(arts, "晚间", "18:00")
        self.assertTrue(arts[0]["_is_prediction"], "补归属不得清掉预测标记")
        self.assertEqual(arts[0]["_batch_name"], "晚间")

    def test_empty_batch_name_is_noop(self):
        arts = [{"title": "A"}]
        self.assertEqual(orchestrator.ensure_batch_attribution(arts, "", ""), 0)
        self.assertNotIn("_batch_name", arts[0])

    def test_tolerates_non_dict_items(self):
        self.assertEqual(orchestrator.ensure_batch_attribution([None, "x"], "晚间"), 0)


if __name__ == "__main__":
    unittest.main()
