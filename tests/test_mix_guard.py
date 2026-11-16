"""选题类型配比守卫（2026-11-16 改动点 3）。

锁四件事：
  1. 目标值只从 `config/ratio_config.yaml` 读，代码里没有第二份；
  2. 占比按**台账日级聚合**算（metadata 是覆盖写，一天只留最后一批）；
  3. 样本不足 / 配置读不到 → `ok=None`（未判定），不是通过；
  4. 超配只留证，不返回「阻断发布」这种语义（它没有 return 任何发布指令）。
"""

import json
import tempfile
import unittest
from pathlib import Path

import mix_guard


class TestTargets(unittest.TestCase):
    def test_targets_from_ratio_config(self):
        t = mix_guard.load_targets()
        self.assertIsInstance(t, dict)
        self.assertEqual(set(t), {"转会官宣", "中国足球", "冲突舆情", "未分类", "战报比分"})
        self.assertEqual(sum(t.values()), 100)
        self.assertEqual(t["战报比分"], 10)

    def test_missing_config_is_none(self):
        self.assertIsNone(mix_guard.load_targets(Path(tempfile.mkdtemp()) / "nope.yaml"))


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = mix_guard.AUDIT_DIR
        mix_guard.AUDIT_DIR = self.tmp
        self.day = "2026-11-16"

    def tearDown(self):
        mix_guard.AUDIT_DIR = self._orig

    def _ledger(self, titles, day=None):
        d = day or self.day
        (self.tmp / f"publish_ledger_{d}.json").write_text(json.dumps(
            {"date": d, "records": [{"title": t, "status": "published"} for t in titles]},
            ensure_ascii=False), encoding="utf-8")

    def test_overshoot_detected(self):
        self._ledger([f"曼联4-{i}-0维拉，多古补时救主" for i in range(20)])
        rep = mix_guard.snapshot(date_str=self.day, window_days=1)
        self.assertFalse(rep["ok"])
        self.assertIn("战报比分", [o["type"] for o in rep["overshoot"]])
        self.assertEqual(rep["table"]["战报比分"]["count"], 20)

    def test_no_overshoot_when_scoreline_absent(self):
        self._ledger([f"皇马官宣签下第{i}名新援，五年合同锁死" for i in range(20)])
        rep = mix_guard.snapshot(date_str=self.day, window_days=1)
        self.assertNotIn("战报比分", [o["type"] for o in rep["overshoot"]])

    def test_small_sample_is_undetermined_not_pass(self):
        self._ledger(["曼联4-4维拉，多古补时救主"])
        rep = mix_guard.snapshot(date_str=self.day, window_days=1)
        self.assertIsNone(rep["ok"])
        self.assertIn("样本", rep["reason"])

    def test_missing_targets_is_undetermined(self):
        self._ledger([f"曼联4-{i}-0维拉" for i in range(20)])
        orig = mix_guard.load_targets
        mix_guard.load_targets = lambda *a, **k: None
        try:
            rep = mix_guard.snapshot(date_str=self.day, window_days=1)
        finally:
            mix_guard.load_targets = orig
        self.assertIsNone(rep["ok"])
        self.assertTrue(rep["degraded"])

    def test_pending_batch_is_added_and_predictions_excluded(self):
        self._ledger([f"皇马官宣签下第{i}名新援" for i in range(19)])
        arts = [{"title": "罗马诺：曼联官宣签下前锋", "topic_category": "转会官宣"},
                {"title": "明日预测", "topic_category": "转会官宣", "_is_prediction": True}]
        rep = mix_guard.snapshot(arts, date_str=self.day, window_days=1)
        self.assertEqual(rep["n"], 20)          # 19 台账 + 1 本批；预测稿不计
        self.assertEqual(rep["batch_n"], 1)

    def test_same_day_three_batches_are_counted(self):
        """metadata 是覆盖写，一天只留最后一批；台账必须把三批都在分母里。"""
        self._ledger(["罗马诺：官宣第一签"] * 7, day="2026-11-15")
        self._ledger(["罗马诺：官宣第二签"] * 7, day="2026-11-16")
        rep = mix_guard.snapshot(date_str=self.day, window_days=2)
        self.assertEqual(rep["n"], 14)

    def test_duplicate_latest_file_is_ignored(self):
        """`publish_ledger_latest.json` 是单批文件，混进来会让分母虚高。"""
        self._ledger([f"曼联4-{i}-0维拉" for i in range(20)])
        (self.tmp / "publish_ledger_latest.json").write_text(json.dumps(
            {"records": [{"title": "曼联4-9-0维拉"}] * 20}), encoding="utf-8")
        rep = mix_guard.snapshot(date_str=self.day, window_days=1)
        self.assertEqual(rep["n"], 20)


if __name__ == "__main__":
    unittest.main()
