"""审核机制元验收的单测（scripts/verify_audit_health.py，2026-11-04）。

这里的每个用例都在回答同一个问题：**这条判定真的会红吗**。

背景：11-04 发现 `verify_publish_gate` 复算标题时不传 `recent_titles`，
「疑问收尾 14 天 ≤2 篇」这条规则从来没触发过，它每次都在判「合规 ✅」——
一个恒真的判定顶着绿灯把真违规掩掉了。所以元验收本身必须有负向用例，
否则它只是在给「审核机制」发安慰奖。
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


BASE_CFG = {
    "version": 2,
    "meta": {"baseline_negative_debt": 1, "debt_must_not_grow": True,
             "required_daily": ["publish_success"]},
    "items": {
        "publish_success": {"tiers": ["daily"], "kind": "hard", "why": "x", "negative": None},
        "content_mix": {"tiers": ["pre-publish", "daily"], "kind": "hard", "why": "x",
                        "negative": None},
        "cover_coverage": {"tiers": ["daily"], "kind": "hard", "why": "x",
                           "negative": "放松阈值必须变通过"},
    },
}


class AuditHealthTest(unittest.TestCase):
    def setUp(self):
        self.ah = _load("verify_audit_health", "scripts/verify_audit_health.py")
        self.d = tempfile.TemporaryDirectory()
        root = Path(self.d.name)
        self.scr = root / "scripts"
        self.wf = root / ".github" / "workflows"
        self.scr.mkdir(parents=True)
        self.wf.mkdir(parents=True)
        self._orig = (self.ah.SCRIPTS_DIR, self.ah.WORKFLOWS_DIR)
        self.ah.SCRIPTS_DIR = self.scr
        self.ah.WORKFLOWS_DIR = self.wf

    def tearDown(self):
        self.ah.SCRIPTS_DIR, self.ah.WORKFLOWS_DIR = self._orig
        self.d.cleanup()

    # ── 工具 ──────────────────────────────────────────────
    def _script(self, name: str, *, artifact: bool = True):
        body = "import json\n" if artifact else ""
        if artifact:
            body += ('AUDIT_DIR = "data/audit"\n'
                     'json.dump({}, open("x.json", "w"))\n')
        (self.scr / f"verify_{name}.py").write_text(body, encoding="utf-8")

    def _workflow(self, text: str, name: str = "w.yml"):
        (self.wf / name).write_text(text, encoding="utf-8")

    def _wired_all(self, *, soft: bool = False):
        lines = ["jobs:", "  verify:", "    steps:"]
        for n in ("publish_success", "content_mix", "cover_coverage"):
            tail = ' \\\n            || echo "::warning::x"' if soft else ""
            lines.append(f"      - run: python3 scripts/verify_{n}.py --quiet{tail}")
        self._workflow("\n".join(lines) + "\n")

    # ── A. 清单完整性 ─────────────────────────────────────
    def test_roster_passes_when_aligned(self):
        for n in BASE_CFG["items"]:
            self._script(n)
        self._wired_all()
        self.assertTrue(self.ah.check_roster(BASE_CFG)["ok"])

    def test_roster_fails_on_unregistered_script(self):
        """有人加了验收却没说什么时候跑 —— 必须红。"""
        for n in BASE_CFG["items"]:
            self._script(n)
        self._script("brand_new_check")
        rep = self.ah.check_roster(BASE_CFG)
        self.assertFalse(rep["ok"])
        self.assertIn("brand_new_check", rep["detail"][1])

    def test_roster_fails_on_ghost_item(self):
        """清单描述了不存在的脚本 —— 说明清单在自我安慰。"""
        for n in BASE_CFG["items"]:
            self._script(n)
        cfg = dict(BASE_CFG)
        cfg["items"] = dict(BASE_CFG["items"])
        cfg["items"]["ghost_check"] = {"tiers": ["weekly"], "kind": "hard", "why": "x"}
        rep = self.ah.check_roster(cfg)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("ghost_check" in d for d in rep["detail"]))

    def test_roster_fails_on_bad_tier_and_kind(self):
        for n in BASE_CFG["items"]:
            self._script(n)
        cfg = dict(BASE_CFG)
        cfg["items"] = dict(BASE_CFG["items"])
        cfg["items"]["cover_coverage"] = {"tiers": ["every-day"], "kind": "硬",
                                          "why": "x"}
        rep = self.ah.check_roster(cfg)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("tiers 非法" in d for d in rep["detail"]))
        self.assertTrue(any("kind 非法" in d for d in rep["detail"]))

    # ── B. 负向债务 ───────────────────────────────────────
    def test_debt_passes_within_baseline(self):
        # 3 项里 2 项无负向，baseline=1 → 应 FAIL；改 baseline=2 才通过
        cfg = dict(BASE_CFG)
        self.assertFalse(self.ah.check_negative_debt(cfg)["ok"])
        cfg = {**BASE_CFG, "meta": {**BASE_CFG["meta"], "baseline_negative_debt": 2}}
        self.assertTrue(self.ah.check_negative_debt(cfg)["ok"])

    def test_debt_fails_when_grows(self):
        """新增一项却没有负向用例 —— 它只是「看起来在验收」。"""
        cfg = {**BASE_CFG, "meta": {**BASE_CFG["meta"], "baseline_negative_debt": 2}}
        cfg["items"] = dict(BASE_CFG["items"])
        cfg["items"]["new_check"] = {"tiers": ["weekly"], "kind": "hard",
                                     "why": "x", "negative": None}
        rep = self.ah.check_negative_debt(cfg)
        self.assertFalse(rep["ok"])
        self.assertGreater(rep["value"], rep["limit"])

    def test_debt_fails_without_baseline(self):
        cfg = {"items": BASE_CFG["items"], "meta": {}}
        self.assertFalse(self.ah.check_negative_debt(cfg)["ok"])

    # ── C. 硬项接线 ───────────────────────────────────────
    def test_hard_wired_passes(self):
        for n in BASE_CFG["items"]:
            self._script(n)
        self._wired_all()
        self.assertTrue(self.ah.check_hard_wired(BASE_CFG)["ok"])

    def test_hard_wired_fails_when_never_called(self):
        """写了却从没被 CI 跑过 = 「有验收」的假象。"""
        for n in BASE_CFG["items"]:
            self._script(n)
        self._workflow("jobs:\n  v:\n    steps:\n      - run: python3 scripts/verify_publish_success.py\n")
        rep = self.ah.check_hard_wired(BASE_CFG)
        self.assertFalse(rep["ok"])
        self.assertEqual(sorted(rep["not_called"]), ["content_mix", "cover_coverage"])

    def test_hard_wired_fails_when_prepublish_softened(self):
        """声明为发布前硬项却带 `|| echo`：FAIL 了稿子照样发。"""
        for n in BASE_CFG["items"]:
            self._script(n)
        self._workflow(
            "jobs:\n  v:\n    steps:\n"
            "      - run: python3 scripts/verify_publish_success.py --quiet\n"
            "      - run: python3 scripts/verify_content_mix.py --quiet \\\n"
            '          || echo "::warning::x"\n'
            "      - run: python3 scripts/verify_cover_coverage.py --quiet\n")
        rep = self.ah.check_hard_wired(BASE_CFG)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["softened"], ["content_mix"])

    def test_hard_wired_ignores_commented_lines(self):
        """注释里写一句 verify_xxx.py 不算调用（10-25 僵尸测试清理的坑）。"""
        for n in BASE_CFG["items"]:
            self._script(n)
        self._wired_all()
        self._workflow(
            "jobs:\n  v:\n    steps:\n"
            "      # 这里曾经调用过 python3 scripts/verify_content_mix.py\n"
            "      - run: echo ok\n", name="b.yml")
        self.assertTrue(self.ah.check_hard_wired(BASE_CFG)["ok"])

    # ── D. 每日最小集 ─────────────────────────────────────
    def test_required_daily_fails_when_moved_out(self):
        cfg = {**BASE_CFG, "meta": {**BASE_CFG["meta"],
                                    "required_daily": ["publish_success", "content_mix"]}}
        cfg["items"] = dict(BASE_CFG["items"])
        cfg["items"]["content_mix"] = {"tiers": ["pre-publish"], "kind": "hard",
                                       "why": "x", "negative": None}
        rep = self.ah.check_required_daily(cfg)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["not_daily"], ["content_mix"])

    def test_required_daily_fails_when_missing(self):
        cfg = {**BASE_CFG, "meta": {**BASE_CFG["meta"],
                                    "required_daily": ["never_exists"]}}
        rep = self.ah.check_required_daily(cfg)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["missing"], ["never_exists"])

    # ── F. 元层负向自检 ───────────────────────────────────
    def test_meta_negative_passes_on_real_config(self):
        self.assertTrue(self.ah.check_meta_negative(BASE_CFG)["ok"])

    def test_meta_negative_fails_when_baseline_absurd(self):
        """baseline 大到永远够用 ⇒ 债务判定恒真 ⇒ 元层负向自检必须红。"""
        cfg = {**BASE_CFG, "meta": {**BASE_CFG["meta"], "baseline_negative_debt": 999}}
        self.assertFalse(self.ah.check_meta_negative(cfg)["ok"])

    def test_meta_negative_fails_without_baseline(self):
        self.assertFalse(self.ah.check_meta_negative({"items": {}, "meta": {}})["ok"])

    # ── 真实清单 ──────────────────────────────────────────
    def test_real_config_passes(self):
        """仓库里真实的 config/audit_tiers.yaml 必须通过（这条会随仓库漂移）。"""
        self.ah.SCRIPTS_DIR, self.ah.WORKFLOWS_DIR = self._orig
        cfg = self.ah.load_config()
        for c in (self.ah.check_roster(cfg), self.ah.check_negative_debt(cfg),
                  self.ah.check_hard_wired(cfg), self.ah.check_required_daily(cfg),
                  self.ah.check_meta_negative(cfg)):
            self.assertTrue(c["ok"], f"{c['item']} 应通过：{c['detail']}")


if __name__ == "__main__":
    unittest.main()
