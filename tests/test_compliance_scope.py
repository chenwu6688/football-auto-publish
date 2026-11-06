#!/usr/bin/env python3
"""审核范围（scope）与头条规则表单测（2026-11-06 立）。

这份测试守的是**审核体系的结构**，不是内容质量：
  - 规则表 ↔ 判据 ↔ 发布门禁三者必须对得上；
  - 发布前门禁只许放合规项（否则生产线抖动就停发，与扣分无关）；
  - 合规项数有天花板（「审核复杂度」唯一可量化的代理指标）；
  - 判据读不到证据时必须判「未判定」，不许拿标题/摘要冒充；
  - 方向错误的旧规则（强制索要关注）不得回归。

为什么单独一份：2026-11-06 的精简结论是「审核复杂度来自四类判据混用同一套语义」。
这类结构性问题不会让任何单测变红，只会让下一个接手的人重新踩一遍，
所以必须有测试盯着结构本身。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import yaml  # noqa: E402

RULES_PATH = PROJECT_ROOT / "config" / "toutiao_rules.yaml"
TIERS_PATH = PROJECT_ROOT / "config" / "audit_tiers.yaml"

VALID_TIERS = {"block", "rewrite", "warn", "n/a"}
VALID_SCOPES = {"compliance", "quality", "growth", "ops", "dev", "meta"}


def _rules() -> dict:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")) or {}


def _tiers() -> dict:
    return yaml.safe_load(TIERS_PATH.read_text(encoding="utf-8")) or {}


class TestRuleTable(unittest.TestCase):
    def test_has_rules(self):
        rules = _rules().get("rules") or []
        self.assertGreaterEqual(len(rules), 10, "规则表条目太少了，可能被误删")

    def test_required_fields(self):
        for r in _rules()["rules"]:
            for f in ("id", "name", "credit", "tier", "judge"):
                self.assertIn(f, r, f"{r.get('id')} 缺字段 {f}")
            self.assertIn(r["tier"], VALID_TIERS, f"{r['id']} tier 非法")

    def test_scored_rules_are_not_warn_only(self):
        """有分值的规则不能只告警 —— 扣分的项必须能拦住。"""
        for r in _rules()["rules"]:
            if int(r.get("credit") or 0) >= 10:
                self.assertNotEqual(r["tier"], "warn",
                                    f"{r['id']} 标了 {r['credit']} 分却只 warn")

    def test_covers_the_deduction_items_we_can_hit(self):
        """足球图文稿会踩的那几条，一条都不能漏登记。"""
        ids = {r["id"] for r in _rules()["rules"]}
        need = {
            "TT-01": "违法违规（含博彩/著作权）",
            "TT-02": "谣言或不实内容",
            "TT-03": "旧闻新发/时效",
            "TT-04": "标题党",
            "TT-05": "关键信息与事实不符",
            "TT-06": "抄袭侵权（洗稿）",
            "TT-07": "诱导交互",
            "TT-08": "违规涨粉",
            "TT-09": "图文不符",
            "TT-10": "格式低质",
            "TT-11": "失实表述/AI 未标识",
        }
        self.assertEqual(need.keys() - ids, set(), "以下扣分项没有登记")

    def test_not_applicable_is_explicit(self):
        """不适用的条款要明写 n/a，而不是悄悄不登记。"""
        na = [r for r in _rules()["rules"] if r["tier"] == "n/a"]
        self.assertTrue(na, "必须显式登记不适用项（例如首发激励未开通）")
        for r in na:
            self.assertTrue(str(r.get("why_keep") or "").strip(),
                            f"{r['id']} 标了 n/a 却没写为什么")


class TestScopeDiscipline(unittest.TestCase):
    def test_every_item_has_valid_scope(self):
        for name, it in _tiers()["items"].items():
            self.assertIn(it.get("scope"), VALID_SCOPES, f"{name} scope 缺失或非法")

    def test_prepublish_only_compliance(self):
        """发布前门禁只许放合规项 —— 本次精简的核心不变量。"""
        bad = []
        for name, it in _tiers()["items"].items():
            if "pre-publish" in (it.get("tiers") or []) and it.get("scope") != "compliance":
                bad.append(f"{name}({it.get('scope')})")
        self.assertEqual(bad, [], f"非合规项混进了发布前门禁：{bad}")

    def test_prepublish_items_declare_rules(self):
        for name, it in _tiers()["items"].items():
            if "pre-publish" in (it.get("tiers") or []):
                self.assertTrue(it.get("rule_ids"),
                                f"{name} 进发布门禁却没挂 rule_ids")

    def test_compliance_count_ceiling(self):
        from verify_rule_coverage import MAX_COMPLIANCE_ITEMS
        comp = [n for n, it in _tiers()["items"].items() if it.get("scope") == "compliance"]
        self.assertLessEqual(len(comp), MAX_COMPLIANCE_ITEMS,
                             f"合规项又变多了（{len(comp)} > {MAX_COMPLIANCE_ITEMS}）：{comp}")

    def test_every_rule_owned(self):
        owned = set()
        for it in _tiers()["items"].values():
            if it.get("scope") == "compliance":
                owned.update(it.get("rule_ids") or [])
        orphan = [r["id"] for r in _rules()["rules"]
                  if r["tier"] != "n/a" and r["id"] not in owned]
        self.assertEqual(orphan, [], f"以下规则没有任何验收项引用：{orphan}")


class TestJudgesBehave(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import verify_compliance as vc
        cls.vc = vc
        cls.rules = {r["id"]: r for r in vc.load_rules()["rules"]}

    def test_selftest_all_pass(self):
        bad = [n for n, ok in self.vc.selftest() if not ok]
        self.assertEqual(bad, [], f"合规负向自检未通过：{bad}")

    def test_no_body_means_not_judged(self):
        """读不到正文一律「未判定」，绝不拿标题冒充（11-06 noon 0 篇的根因）。"""
        item = {"date": "2026-11-06", "dir": PROJECT_ROOT / "no_such_dir",
                "art": {"title": "正常标题"}, "_body": None}
        for judge in (self.vc.j_inducement, self.vc.j_structure, self.vc.j_ai_annotation):
            self.assertEqual(judge(item, self.rules["TT-07"])["status"],
                             self.vc.UNJ)

    def test_title_only_fail_is_not_compliance_fail(self):
        """三要素不足是质量分，不许被当成标题党红线（本次自查踩过这个坑）。"""
        item = {"date": "2026-11-06", "dir": PROJECT_ROOT,
                "art": {"title": "领跑，那不勒斯这一关，队长憋的劲该往哪使"},
                "_body": "正文。\n\n第二段。\n\n第三段。\n\n第四段。"}
        self.assertEqual(self.vc.j_title_gate(item, self.rules["TT-04"])["status"],
                         self.vc.PASS)

    def test_report_never_green_when_nothing_judged(self):
        """一条都没判出来时，报告不能标「无违规」。"""
        rep = {"window_days": 1, "since": "2026-11-06", "articles": 0,
               "rules": [{"id": "TT-01", "name": "x", "credit": 70, "tier": "block",
                          "status": self.vc.UNJ, "detail": "", "hits": []}],
               "blocking": [], "risk_score": 0}
        text = self.vc.render(rep)
        self.assertIn("不能", text)
        self.assertIn("⚠️", text)


class TestInducementIsHardGate(unittest.TestCase):
    def test_gate_v2_blocks_inducement(self):
        import gate_v2
        self.assertIn("inducement", gate_v2.HARD_GATES)
        self.assertTrue(gate_v2.blocking("inducement"))

    def test_old_bait_patterns_removed(self):
        """「下注/押注/扣1/站队」此前是**达标模式**，等于奖励诱导话术。"""
        cfg = yaml.safe_load(
            (PROJECT_ROOT / "config" / "interaction_bait.yaml").read_text(encoding="utf-8"))
        joined = " ".join(cfg.get("互动引导") or [])
        for bad in ("下注", "押注", "扣一", "站队", "投票"):
            self.assertNotIn(bad, joined, f"「{bad}」不该再是互动引导的达标模式")
        self.assertFalse(cfg.get("关注引导必需"), "关注引导不该是必须项")


class TestCoverageVerifier(unittest.TestCase):
    def test_verify_passes(self):
        import verify_rule_coverage as vrc
        rep = vrc.verify()
        self.assertTrue(rep["ok"], f"规则覆盖校验未通过：{rep['hard_failed']}")

    def test_selftest_all_pass(self):
        import verify_rule_coverage as vrc
        bad = [n for n, ok in vrc.selftest() if not ok]
        self.assertEqual(bad, [], f"覆盖校验负向自检未通过：{bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
