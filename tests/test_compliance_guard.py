import tempfile
import unittest
from pathlib import Path

import compliance_guard as cg


class TestComplianceGuard(unittest.TestCase):
    """计划第十章 · 合规红线：不实违规出现一次即熔断。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "state.json"

    def test_initial_not_suspended(self):
        self.assertFalse(cg.is_suspended(self.tmp))
        st = cg.status(self.tmp)
        self.assertFalse(st["suspended"])
        self.assertEqual(st["false_content_count"], 0)

    def test_violation_suspends_immediately(self):
        cg.record_violation(cg.FALSE_CONTENT, "平台判定标题不实", date="2026-10-10",
                            path=self.tmp)
        self.assertTrue(cg.is_suspended(self.tmp))
        st = cg.status(self.tmp)
        self.assertEqual(st["false_content_count"], 1)
        self.assertIn("不实", st["reason"])

    def test_clear_resumes(self):
        cg.record_violation(cg.FALSE_CONTENT, "x", path=self.tmp)
        self.assertTrue(cg.is_suspended(self.tmp))
        cg.clear(operator="陈少", note="已定位为源文错误", path=self.tmp)
        self.assertFalse(cg.is_suspended(self.tmp))
        # 违规历史保留（合规指标仍需统计）
        self.assertEqual(cg.status(self.tmp)["false_content_count"], 1)

    def test_auto_suspend_threshold(self):
        # 未达阈值：不熔断
        self.assertFalse(cg.auto_suspend_if_systemic(1, path=self.tmp))
        self.assertFalse(cg.is_suspended(self.tmp))
        # 达阈值：自动熔断一次
        self.assertTrue(cg.auto_suspend_if_systemic(
            cg.AUTO_SUSPEND_REJECT_THRESHOLD, detail="打回过多", path=self.tmp))
        self.assertTrue(cg.is_suspended(self.tmp))
        # 已熔断时不重复登记
        self.assertFalse(cg.auto_suspend_if_systemic(99, path=self.tmp))

    def test_status_counts_only_false_content(self):
        cg.auto_suspend_if_systemic(cg.AUTO_SUSPEND_REJECT_THRESHOLD, path=self.tmp)
        st = cg.status(self.tmp)
        # 自动熔断属于「疑似系统性失真」，不计入不实违规次数
        self.assertEqual(st["false_content_count"], 0)
        self.assertTrue(st["suspended"])


class TestOrchestratorHook(unittest.TestCase):
    """自动熔断钩子接入 orchestrator 一致性打回路径。"""

    def test_hook_counts_and_resets(self):
        import orchestrator as o
        import compliance_guard as cg

        # 用临时状态文件，避免污染真实 data/
        self_tmp = Path(tempfile.mkdtemp()) / "s.json"
        orig = cg.STATE_PATH
        cg.STATE_PATH = self_tmp
        try:
            o._CONSISTENCY_REJECTS["count"] = 0
            o._CONSISTENCY_REJECTS["suspended"] = False
            # 打回 5 次触发自动熔断
            for i in range(cg.AUTO_SUSPEND_REJECT_THRESHOLD):
                o._note_consistency_reject(f"卡外比分 #{i}")
            self.assertTrue(cg.is_suspended(self_tmp))
        finally:
            cg.STATE_PATH = orig
            o._CONSISTENCY_REJECTS["count"] = 0
            o._CONSISTENCY_REJECTS["suspended"] = False


if __name__ == "__main__":
    unittest.main()
