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

VALID_TIERS = {"block", "rewrite", "warn", "pass", "n/a"}
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
        """不适用的条款必须显式登记，而不是悄悄不登记。

        2026-11-06 之前这里断言「至少有一条 n/a」。TT-12 从 n/a 改成 pass 之后，
        规则表里不再有 n/a —— 这正是「不适用」改由 `out_of_scope` 承载的时机：
        **同一条信息只写一处**，否则表里写 n/a、注释里再列一遍，迟早只改一处。
        """
        for r in _rules()["rules"]:
            if r["tier"] == "n/a":
                self.assertTrue(str(r.get("why_keep") or "").strip(),
                                f"{r['id']} 标了 n/a 却没写为什么")
        out = _rules().get("out_of_scope") or []
        self.assertTrue(out, "不适用条款必须集中登记在 out_of_scope")
        for it in out:
            self.assertTrue(it.get("scope") and it.get("why"),
                            f"out_of_scope 条目缺 scope/why：{it}")
            self.assertTrue(it.get("items"), f"{it.get('scope')} 没列出具体项")

    def test_no_na_dumping_ground(self):
        """n/a 只能用于「业务形态不产生这类内容」，不能用来关掉麻烦的规则。"""
        na = [r["id"] for r in _rules()["rules"] if r["tier"] == "n/a"]
        self.assertEqual(na, [],
                         "当前 14 条规则都应处于 block/rewrite/warn/pass 之一；"
                         "真有不适用的，请挪到 out_of_scope 并写清理由")


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


class TestFirstPublishScope(unittest.TestCase):
    """TT-12：单平台是**配置事实**，不是代码常量。"""

    def test_platforms_declared_and_single(self):
        scope = _rules().get("publish_scope") or {}
        self.assertEqual(scope.get("platforms"), ["toutiao"],
                         "目前只发头条一个平台；改多平台必须同步 TT-12 的 tier")

    def test_tt12_is_pass_not_na(self):
        r = {x["id"]: x for x in _rules()["rules"]}["TT-12"]
        self.assertEqual(r["tier"], "pass",
                         "n/a 意味着「这事跟我们无关」，但非首发扣分只是条件未触发")

    def test_originality_half_is_delegated(self):
        """原创半边刻意不重复绑 plagiarism，避免同一违规被算两遍分。"""
        r = {x["id"]: x for x in _rules()["rules"]}["TT-12"]
        self.assertEqual(r.get("depends_on"), ["TT-06"])
        self.assertNotIn("plagiarism", str(r.get("judge")))

    def test_judge_follows_config_not_memory(self):
        import verify_compliance as vc
        item = {"date": "2026-11-06", "dir": PROJECT_ROOT, "art": {"title": "x"},
                "_body": "正文"}
        got = {}
        for plats in (["toutiao"], ["toutiao", "wechat"], [], ["wechat"]):
            got[tuple(plats)] = vc.j_first_publish(
                item, {"_publish_scope": {"platforms": plats}})["status"]
        self.assertEqual(got[("toutiao",)], "pass")
        # 平台一变，答案必须跟着变 —— 这就是「不写死」的意思
        self.assertEqual(got[("toutiao", "wechat")], "not_judged")
        self.assertEqual(got[()], "not_judged")
        self.assertEqual(got[("wechat",)], "not_judged")

    def test_missing_scope_is_not_pass(self):
        """配置缺失时不许默认当首发（默认值必须是「未判定」）。"""
        import verify_compliance as vc
        r = vc.j_first_publish({"date": "2026-11-06", "dir": PROJECT_ROOT,
                                "art": {"title": "x"}, "_body": "正文"}, {})
        self.assertEqual(r["status"], "not_judged")


class TestCoverageVerifier(unittest.TestCase):
    def test_verify_passes(self):
        import verify_rule_coverage as vrc
        rep = vrc.verify()
        self.assertTrue(rep["ok"], f"规则覆盖校验未通过：{rep['hard_failed']}")

    def test_selftest_all_pass(self):
        import verify_rule_coverage as vrc
        bad = [n for n, ok in vrc.selftest() if not ok]
        self.assertEqual(bad, [], f"覆盖校验负向自检未通过：{bad}")


class TestPromptAlignment(unittest.TestCase):
    """H 项：判定侧判违规的话术，不许出现在喂给模型的文本里。

    2026-11-06 晚整批 0 篇的直接原因就是这一项缺失：判定侧白天改成 20 分档违规，
    提示词仍在强制要求，模型照写、写完被自己的闸门拦掉。前三方（规则表 / 判据 /
    发布门禁）都有检查，唯独生成提示词没有 —— 这是「同一规矩写两处」的第四方。
    """

    def test_real_prompts_are_clean(self):
        import verify_rule_coverage as vrc
        rep = vrc.check_prompt_alignment()
        self.assertTrue(rep["ok"], "提示词里还有诱导话术：\n" + "\n".join(rep["detail"]))

    def test_prompts_are_actually_found(self):
        """扫不到文件就报绿，是这类检查最典型的假绿。"""
        import verify_rule_coverage as vrc
        texts = vrc.prompt_texts()
        self.assertGreaterEqual(len(texts), 2, f"提示词文件没扫到：{list(texts)}")
        self.assertTrue(any("rewrite_article" in k for k in texts), texts.keys())

    def test_catches_inducement(self):
        import verify_rule_coverage as vrc
        for bad in ("支持XX的扣1，反对的扣2", "点个关注，明天继续",
                    "评论区下注，我先来"):
            rep = vrc.check_prompt_alignment({"x.txt": bad})
            self.assertFalse(rep["ok"], f"没抓到：{bad}")

    def test_no_exemption_for_negative_examples(self):
        """「不要写点关注」这种负例照样报红。

        不做豁免是刻意的：写进提示词的负例本身会被模型当模板抄。
        第一版修复就是这么写的，扫描器报红反而逼出了更好的说法。
        """
        import verify_rule_coverage as vrc
        rep = vrc.check_prompt_alignment({"x.txt": "不要写「点个关注」这类话术"})
        self.assertFalse(rep["ok"], "负例被豁免了 —— 那等于默许提示词里留可抄的样例")

    def test_blacklist_comes_from_config(self):
        """黑名单只能有一处：从 interaction_bait.yaml 读，不在检查侧再列一遍。"""
        import inspect
        import verify_rule_coverage as vrc
        src = inspect.getsource(vrc._inducement_patterns)
        self.assertIn("interaction_bait.yaml", src)
        self.assertNotIn("扣1", src, "检查侧又硬编码了一份词表")

    def test_batch_runs_it_before_generating(self):
        """必须在**生成前**跑，否则只是事后确认「已经烧掉两分钟」。"""
        wf = (Path(__file__).resolve().parent.parent
              / ".github" / "workflows" / "batch.yml").read_text(encoding="utf-8")
        idx_check = wf.find("提示词合规自检")
        idx_gen = wf.find("name: 生成文章")
        self.assertGreater(idx_check, 0, "batch.yml 没有提示词自检步骤")
        self.assertGreater(idx_gen, 0)
        self.assertLess(idx_check, idx_gen, "提示词自检被放到了生成之后，等于没拦")


if __name__ == "__main__":
    unittest.main(verbosity=2)
