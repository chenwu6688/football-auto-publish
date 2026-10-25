#!/usr/bin/env python3
"""失败告警「必须带原因」回归测试（2026-10-25 事故）。

事故：一天连收三条 WxPusher 告警，正文只有「noon 批次发布失败 / 日期 / 链接」，
没有原因。手机上无法判断是合规熔断、登录过期还是发布失败，只能回电脑翻运行记录。

本组测试锁住：
  1. 告警正文必须含「原因」一行，且原因永不为空（缺失时给明确占位，不冒充无事）；
  2. 原因由合规状态拼出时，必须带上「何时触发 + 具体判定」，可供直接定位；
  3. 原因文件读写闭环；文件缺失不抛异常。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import compliance_guard  # noqa: E402
import notify_failure  # noqa: E402
import write_failure_reason as wfr  # noqa: E402


class TestBuildContent(unittest.TestCase):
    def test_reason_is_in_body(self):
        body = notify_failure.build_content(
            "noon", "2026-10-25", "合规熔断（10-24 18:04 触发）：比分不一致",
            "o/r", "123")
        self.assertIn("原因: 合规熔断", body)
        self.assertIn("日期: 2026-10-25", body)
        self.assertIn("actions/runs/123", body)

    def test_empty_reason_still_explicit(self):
        body = notify_failure.build_content("noon", "2026-10-25", "", "o/r", "1")
        self.assertIn("原因: 详见运行日志", body)
        self.assertNotIn("原因: \n", body)


class TestReasonFile(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.path = Path(self._d.name) / "wb_fail_reason.txt"

    def tearDown(self):
        self._d.cleanup()

    def test_write_read_roundtrip(self):
        wfr.write("合规熔断（未解除）", self.path)
        self.assertEqual(wfr.read(self.path), "合规熔断（未解除）")

    def test_read_missing_file_gives_placeholder(self):
        self.assertIn("未捕获", wfr.read(self.path))

    def test_write_empty_falls_back(self):
        self.assertEqual(wfr.write("", self.path), "未知原因（详见运行日志）")


class TestComplianceReason(unittest.TestCase):
    """从真实合规状态拼原因：必须含触发时间与判定原文。"""

    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self._p = Path(self._d.name) / "state.json"
        self._p.write_text(json.dumps({
            "suspended": True,
            "reason": "疑似系统性失真：比分不一致: 比赛数据 蓉城 0-1 英博，但文中未出现该比分",
            "since": "2026-10-24T18:04:38+08:00",
            "history": [],
        }, ensure_ascii=False), encoding="utf-8")
        self._orig = compliance_guard.STATE_PATH
        compliance_guard.STATE_PATH = self._p

    def tearDown(self):
        compliance_guard.STATE_PATH = self._orig
        self._d.cleanup()

    def test_reason_contains_since_and_detail(self):
        r = wfr.compliance_reason("合规熔断（未解除）")
        self.assertIn("2026-10-24 18:04", r)
        self.assertIn("比分不一致", r)
        self.assertIn("未解除", r)


if __name__ == "__main__":
    unittest.main()
