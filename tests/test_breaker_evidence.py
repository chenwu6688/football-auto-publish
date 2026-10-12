#!/usr/bin/env python3
"""熔断证据与打回可追溯单测（计划 7.3 / 第十章）。

2026-10-12 的自动熔断事后**无法定位触发批次**：熔断 history 只留最后一条提示，
一致性台账只存 issues 摘要、不存选题。本组测试锁住事后复盘所需的两样东西：

  1. 熔断 history 的 ``evidence`` 必须保留最近若干次打回（含选题）；
  2. 一致性台账的打回事件必须带选题标题（可追溯率可统计）。
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

import compliance_guard as cg
import consistency_stats as cs
import orchestrator as o


class TestConsistencyTraceability(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self._s, self._l = cs.STATS_PATH, cs.LABELS_PATH
        cs.STATS_PATH = Path(self._d.name) / "stats.json"
        cs.LABELS_PATH = Path(self._d.name) / "labels.json"

    def tearDown(self):
        cs.STATS_PATH, cs.LABELS_PATH = self._s, self._l
        self._d.cleanup()

    def test_title_recorded_only_on_reject(self):
        rej = cs.record(blocking=True, passed=False, issues=["缺少客队名: 拜仁慕尼黑"],
                        title="多特1-2拜仁", subject="多特、拜仁")
        ok = cs.record(blocking=True, passed=True, issues=[], title="不该出现")
        self.assertEqual(rej["title"], "多特1-2拜仁")
        self.assertNotIn("title", ok, "通过项不占空间，不记标题")

    def test_traceability_rate(self):
        cs.record(blocking=True, passed=False, issues=["x"], title="A")
        cs.record(blocking=True, passed=False, issues=["y"], title="B")
        cs.record(blocking=True, passed=False, issues=["z"])  # 缺标题（历史事件）
        t = cs.traceability(30)
        self.assertEqual(t["rejects"], 3)
        self.assertEqual(t["with_title"], 2)
        self.assertAlmostEqual(t["rate"], 2 / 3, places=4)

    def test_traceability_no_records_is_none(self):
        t = cs.traceability(30)
        self.assertEqual(t["rejects"], 0)
        self.assertIsNone(t["rate"], "无记录必须是 None，不能冒充 0")

    def test_rejects_newest_first(self):
        cs.record(blocking=True, passed=False, issues=["1"], title="早")
        cs.record(blocking=True, passed=False, issues=["2"], title="晚")
        self.assertEqual([e["title"] for e in cs.rejects(30)], ["晚", "早"])


class TestBreakerEvidence(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self._p = Path(self._d.name) / "compliance.json"
        self._s, self._l = cs.STATS_PATH, cs.LABELS_PATH
        cs.STATS_PATH = Path(self._d.name) / "stats.json"
        cs.LABELS_PATH = Path(self._d.name) / "labels.json"
        self._prev_state = cg.STATE_PATH
        cg.STATE_PATH = self._p
        self._prev_rejects = dict(o._CONSISTENCY_REJECTS)
        o._CONSISTENCY_REJECTS.update({"count": 0, "suspended": False, "evidence": []})

    def tearDown(self):
        cg.STATE_PATH = self._prev_state
        cs.STATS_PATH, cs.LABELS_PATH = self._s, self._l
        o._CONSISTENCY_REJECTS.update(self._prev_rejects)
        self._d.cleanup()

    def test_no_suspend_below_threshold(self):
        for _ in range(cg.AUTO_SUSPEND_REJECT_THRESHOLD - 1):
            o._note_consistency_reject("[选题] 缺少客队名: 拜仁慕尼黑")
        self.assertFalse(cg.is_suspended())

    def test_suspend_at_threshold_with_evidence(self):
        n = cg.AUTO_SUSPEND_REJECT_THRESHOLD
        for i in range(n):
            o._note_consistency_reject(f"[选题{i}] 缺少客队名: 拜仁慕尼黑")
        self.assertTrue(cg.is_suspended())
        rec = cg.violations("疑似系统性失真")[0]
        lines = [l for l in rec["evidence"].splitlines() if l.strip()]
        self.assertEqual(len(lines), n, "证据须保留全部触发打回")
        self.assertTrue(all("选题" in l for l in lines))
        self.assertTrue(any("拜仁慕尼黑" in l for l in lines))

    def test_evidence_capped(self):
        # 把上限压到阈值以下，验证只保留「最近 cap 条」
        prev = o._CONSISTENCY_EVIDENCE_MAX
        o._CONSISTENCY_EVIDENCE_MAX = 3
        try:
            for i in range(cg.AUTO_SUSPEND_REJECT_THRESHOLD):
                o._note_consistency_reject(f"[选题{i}] 问题{i}")
        finally:
            o._CONSISTENCY_EVIDENCE_MAX = prev
        rec = cg.violations("疑似系统性失真")[0]
        lines = [l for l in rec["evidence"].splitlines() if l.strip()]
        self.assertEqual(len(lines), 3, "证据须按上限截断，只留最近几条")
        self.assertTrue(all("选题4" in l or "选题3" in l or "选题2" in l for l in lines),
                        "截断须保留最近的打回")

    def test_auto_suspend_writes_evidence_directly(self):
        cg.auto_suspend_if_systemic(cg.AUTO_SUSPEND_REJECT_THRESHOLD,
                                    detail="同批次打回超阈值", evidence="证据A\n证据B",
                                    path=self._p)
        rec = cg.violations("疑似系统性失真", path=self._p)[0]
        self.assertEqual(rec["evidence"], "证据A\n证据B")


class TestVerifyScriptTraceability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "verify_publish_gate",
            str(Path(__file__).resolve().parent.parent / "scripts" / "verify_publish_gate.py"))
        cls.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.script)

    def test_traceability_check_is_advisory(self):
        res = self.script.check_reject_traceability()
        self.assertTrue(res.get("advisory"))
        self.assertTrue(res.get("ok"))

    def test_alias_probe_does_not_pollute_real_ledger(self):
        """探测本身会用 check_consistency_plan，必须隔离台账路径。"""
        before = cs.summary()
        self.script.check_team_alias_awareness()
        after = cs.summary()
        self.assertEqual(before, after, "别名探测不得写入真实一致性台账")
        self.assertEqual(cs.STATS_PATH.name, "consistency_stats.json",
                         "探测结束必须恢复真实台账路径")


if __name__ == "__main__":
    unittest.main()
