# -*- coding: utf-8 -*-
"""标题锚点验收脚本的三态判定（2026-11-03 复盘）。

要钉死的缺陷：旧实现在「样本不足」时返回 ``ok=True`` + ``advisory``，而 ``verify()`` 把
advisory 项整个排除 → **4 篇判通过、5 篇判失败**。同一份真相，判定方向随样本量翻转，
门禁就成了噪音。本文件用合成目录测三条：

1. 样本不足 → ``ok is None``（未判定），既不是 True 也不是 False；
2. 判定对样本量单调：4 篇与 5 篇的方向不能相反；
3. 累计样本够了却从未达标 → 直接 FAIL，堵住「永远样本不足」的自我豁免。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))

ANCHOR_TITLE = "第94分钟丢球，阿森纳遭读秒绝杀！领先只剩3分"
PLAIN_TITLE = "利物浦主场哑火吞零蛋，阿森纳这次把悬念提前掐灭"


def _load_verifier():
    path = ROOT / "scripts" / "verify_title_anchor.py"
    spec = importlib.util.spec_from_file_location("verify_title_anchor_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_day(root: Path, day: str, titles) -> None:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    meta = {"articles": [{"title": t, "is_prediction": False} for t in titles]}
    (d / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


class TestCoverageTriState(unittest.TestCase):
    def setUp(self):
        self.va = _load_verifier()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.today = datetime.now(CST).date()
        # 生效日钉在「昨天」，保证测试不随真实日期漂移
        self.va.TITLE_SINCE = (self.today - timedelta(days=1)).strftime("%Y-%m-%d")
        self.va.OUTPUT_DIR = self.root
        self.yday = (self.today - timedelta(days=1)).strftime("%Y-%m-%d")
        self.tsday = self.today.strftime("%Y-%m-%d")

    def tearDown(self):
        self.tmp.cleanup()

    def test_insufficient_sample_is_pending_not_pass(self):
        """4 篇 < MIN_SAMPLE：必须是 None（未判定），绝不能是 True（通过）。"""
        _write_day(self.root, self.tsday, [PLAIN_TITLE] * 4)
        r = self.va.check_anchor_coverage(7)
        self.assertIsNone(r["ok"], "样本不足时 ok 必须为 None；返回 True 会把「没样本」伪装成「达标」")
        self.assertTrue(r.get("pending"))

    def test_monotonic_across_sample_threshold(self):
        """4 篇与 5 篇的方向不能相反 —— 这正是旧实现的核心缺陷。"""
        _write_day(self.root, self.tsday, [PLAIN_TITLE] * 4)
        r4 = self.va.check_anchor_coverage(7)
        _write_day(self.root, self.tsday, [PLAIN_TITLE] * 5)
        r5 = self.va.check_anchor_coverage(7)
        self.assertIsNone(r4["ok"])
        self.assertIs(r5["ok"], False)
        self.assertNotEqual(r4["ok"], r5["ok"],
                            "4 篇通过、5 篇失败是不单调：同一份真相不该因样本量改判")

    def test_five_samples_with_anchors_passes(self):
        """5 篇里 3 篇有锚点 = 60% ≥ 40%，必须真判通过（不是 pending 蒙过去的）。"""
        _write_day(self.root, self.tsday,
                   [ANCHOR_TITLE, ANCHOR_TITLE, ANCHOR_TITLE, PLAIN_TITLE, PLAIN_TITLE])
        r = self.va.check_anchor_coverage(7)
        self.assertIs(r["ok"], True)
        self.assertAlmostEqual(r["value"], 60.0, places=1)

    def test_thin_sample_is_flagged(self):
        """n<ROBUST_SAMPLE 仍判，但必须标 thin，避免拿 5 篇当定论。"""
        _write_day(self.root, self.tsday, [ANCHOR_TITLE] * 3 + [PLAIN_TITLE] * 2)
        r = self.va.check_anchor_coverage(7)
        self.assertTrue(r.get("thin"))

    def test_cumulative_never_achieved_fails(self):
        """窗口内只有 4 篇（不足），但生效日累计 30 篇 0 达标 → 必须 FAIL。

        这条是防「永远样本不足 = 永久豁免」：否则只要每天成稿少于 MIN_SAMPLE，
        这条硬项就永远不会红。
        """
        _write_day(self.root, self.yday, [PLAIN_TITLE] * 26)
        _write_day(self.root, self.tsday, [PLAIN_TITLE] * 4)
        r = self.va.check_anchor_coverage(1)          # 窗口只含今天 → 4 篇，不足
        self.assertIs(r["ok"], False,
                      "累计样本已达 CUM_MIN_SAMPLE 且从未达标，不许再以「样本不足」搁置")


class TestVerdict(unittest.TestCase):
    """verify() 的结论聚合：pending 不能被算成 pass，failed 必须压过一切。"""

    def setUp(self):
        self.va = _load_verifier()

    def _rep(self, statuses):
        checks = [{"item": f"c{i}", "ok": s, "detail": []} for i, s in enumerate(statuses)]
        failed = [c["item"] for c in checks if c["ok"] is False]
        pending = [c["item"] for c in checks if c["ok"] is None]
        status = "fail" if failed else ("pending" if pending else "pass")
        return {"generated_at": "x", "window_days": 7, "since": "x",
                "ok": not failed, "status": status, "checks": checks,
                "hard_failed": failed, "pending": pending}

    def test_pending_is_not_rendered_as_pass(self):
        md = self.va.render_md(self._rep([True, None]))
        self.assertIn("未判定", md)
        self.assertNotIn("通过 ✅", md)

    def test_fail_beats_pending(self):
        rep = self._rep([None, False])
        self.assertEqual(rep["status"], "fail")
        self.assertIn("未通过", self.va.render_md(rep))

    def test_all_true_is_pass(self):
        rep = self._rep([True, True])
        self.assertEqual(rep["status"], "pass")
        self.assertIn("通过 ✅", self.va.render_md(rep))


if __name__ == "__main__":
    unittest.main(verbosity=2)
