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


class TestBreakerBlocksCurrentBatch(unittest.TestCase):
    """R18 修复验收：熔断须拦住当前批次（2026-10-13 生产事故）。

    事故谱系：一致性打回超阈值的熔断在生成阶段才触发，但当晚批次照常生成
    并发布（CI 只在批次开头检查一次），故障直到次日批次开头才停下。
    修复三层，逐层验收：
      1. orchestrator._breaker_active() —— 生成侧发现熔断即中止（宁停勿错，省额度）；
      2. batch.yml「合规熔断复核（生成后）」—— 发布前复核，熔断则本批 exit 1；
      3. publisher.publish_all() —— 发布边界硬闸，熔断即 sys.exit(1)。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "state.json"

    def test_publisher_refuses_when_suspended(self):
        """发布边界硬闸：熔断中 publish_all 直接退出（先于登录态检查）。"""
        import compliance_guard as cg
        import publisher as pub
        cg.record_violation(cg.FALSE_CONTENT, "单测熔断", date="2026-10-14", path=self.tmp)
        orig = cg.STATE_PATH
        cg.STATE_PATH = self.tmp
        try:
            with self.assertRaises(SystemExit) as ctx:
                pub.publish_all("2026-10-14", headless=True)
            self.assertEqual(ctx.exception.code, 1)
        finally:
            cg.STATE_PATH = orig

    def test_publisher_passes_gate_when_clear(self):
        """未熔断时熔断闸不得误伤：越过闸门与登录态检查，进入发布流程。"""
        import compliance_guard as cg
        import publisher as pub

        class _Reached(Exception):
            pass

        with tempfile.TemporaryDirectory() as td:
            auth = Path(td) / "auth.json"
            auth.write_text("{}", encoding="utf-8")
            orig_auth, orig_pw, orig_load = pub.AUTH_FILE, pub.sync_playwright, pub.load_articles
            orig_state = cg.STATE_PATH
            pub.AUTH_FILE = auth
            pub.sync_playwright = lambda: (_ for _ in ()).throw(_Reached())
            pub.load_articles = lambda *a, **k: []
            cg.STATE_PATH = self.tmp  # 空状态文件 = 未熔断
            try:
                with self.assertRaises(_Reached):
                    pub.publish_all("2026-10-14", headless=True)
            finally:
                pub.AUTH_FILE, pub.sync_playwright = orig_auth, orig_pw
                pub.load_articles = orig_load
                cg.STATE_PATH = orig_state

    def test_orchestrator_breaker_active(self):
        """生成侧：进程内触发或状态文件已熔断，_breaker_active() 均为真。"""
        import orchestrator as o
        import compliance_guard as cg
        orig_state = cg.STATE_PATH
        cg.STATE_PATH = self.tmp
        o._CONSISTENCY_REJECTS["count"] = 0
        o._CONSISTENCY_REJECTS["suspended"] = False
        try:
            self.assertFalse(o._breaker_active())
            # 进程内触发（生成阶段一致性打回超阈值）
            o._CONSISTENCY_REJECTS["suspended"] = True
            self.assertTrue(o._breaker_active())
            o._CONSISTENCY_REJECTS["suspended"] = False
            # 状态文件已熔断（跨进程 / CI 复核场景）
            cg.record_violation(cg.FALSE_CONTENT, "x", path=self.tmp)
            self.assertTrue(o._breaker_active())
        finally:
            cg.STATE_PATH = orig_state
            o._CONSISTENCY_REJECTS["count"] = 0
            o._CONSISTENCY_REJECTS["suspended"] = False

    def test_batch_yml_recheck_gates_publish(self):
        """CI 层：生成后复核必须先于发布，且熔断中止的批次不标记完成。"""
        import yaml
        wf_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "batch.yml"
        wf = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        steps = wf["jobs"]["publish"]["steps"]
        names = [s.get("name", "") for s in steps]
        recheck = next((s for s in steps if s.get("id") == "compliance_recheck"), None)
        self.assertIsNotNone(recheck, "batch.yml 缺少「合规熔断复核（生成后）」步骤")
        i_re = steps.index(recheck)
        i_pub = names.index("发布到头条号")
        i_wb = names.index("回写批次状态（幂等保护）")
        self.assertLess(i_re, i_pub, "复核步骤必须位于「发布到头条号」之前")
        self.assertIn("exit 1", recheck["run"], "复核步骤熔断时必须 exit 1（任务失败→失败通知）")
        self.assertIn("compliance_recheck", str(steps[i_pub]["if"]),
                      "「发布到头条号」未按复核结果跳过")
        self.assertIn("compliance_recheck", str(steps[i_wb]["if"]),
                      "熔断中止的批次不得标记完成（否则解除后补发被幂等保护跳过）")


if __name__ == "__main__":
    unittest.main()
