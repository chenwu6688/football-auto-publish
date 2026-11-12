"""命中「话术类 20 分档」时先删句再决定丢不丢（2026-11-12）。

## 为什么

11-11 晚间：两篇稿子结尾各带一句「…关注…」（生成它们的蓄水池提示词当时还在
强制要求写账号引导），出口合规按 TT-07/TT-08 各判 20 分 → **整篇丢弃** →
本批 0 篇 → `batches_completed` 不写 evening → 次日心跳报「缺失批次」。

扣 20 分与交 0 篇，代价差一个数量级。正确处置是**删掉违规那一句再发**。

## 边界（改之前先看清楚）

- 只有 `judge == "inducement"` 且 `credit <= 20` 的规则允许删句修复；
- 事实类（不实信息 / 侵权 / 赌博 70 分档）**删句等于抹掉错误证据**，必须整篇丢；
- 删完必须**重判**，仍不干净就整篇丢（删句是补救，不是豁免）；
- 删了哪几句要落进 metadata（`compliance_sentence_removed`），否则「这篇收尾
  为什么变短了」永远查不到。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import interaction_bait  # noqa: E402
from scripts import verify_compliance as vc  # noqa: E402

CLEAN_BODY = (
    "那不勒斯客场 2-1 逆转，中场休息后换了两处站位。\n\n"
    "第二段说中场的变化：边路压上，后腰回撤补位。\n\n"
    "第三段讲教练的调整，换人之后节奏明显快了。\n\n"
    "第四段收尾，这球你怎么看？\n\n"
    # AI 标识必须在（TT-11 缺标识算失实表述 10 分）：少了它，本用例会因为
    # 另一条规则命中而判成「不可删句修复」，测的就不是删句这条链了。
    "> 本文由 AI 生成\n"
)


def _art(body, title="那不勒斯客场 2-1 逆转，中场换位见效"):
    return {"title": title, "content": body, "images": ["a.jpg"]}


class TestRepairableGate(unittest.TestCase):
    """哪一档允许删句修复：判据从规则表读，不在调用侧另列白名单。"""

    def test_inducement_rules_are_repairable(self):
        self.assertTrue(vc.repairable_by_sentence(["TT-07"]))
        self.assertTrue(vc.repairable_by_sentence(["TT-07", "TT-08"]))

    def test_fact_rules_are_not_repairable(self):
        """不实信息 / 赌博是 70 分档：删句等于抹掉证据。"""
        self.assertFalse(vc.repairable_by_sentence(["TT-01"]))

    def test_mixed_hits_are_not_repairable(self):
        """只要有一条事实类命中，整篇丢——不能靠删一句话混过去。"""
        self.assertFalse(vc.repairable_by_sentence(["TT-07", "TT-01"]))

    def test_unknown_rule_is_not_repairable(self):
        self.assertFalse(vc.repairable_by_sentence(["TT-99"]))

    def test_empty_is_not_repairable(self):
        self.assertFalse(vc.repairable_by_sentence([]))


class TestStripInducement(unittest.TestCase):
    def test_removes_only_the_soliciting_sentence(self):
        body = CLEAN_BODY + "关注老六，这类老故事我还存着一柜子。\n"
        new, removed = interaction_bait.strip_inducement(body)
        self.assertEqual(len(removed), 1)
        self.assertIn("关注老六", removed[0])
        self.assertNotIn("关注老六", new)
        self.assertIn("那不勒斯客场 2-1 逆转", new, "正文主体被误删")

    def test_no_hit_returns_original(self):
        new, removed = interaction_bait.strip_inducement(CLEAN_BODY)
        self.assertEqual(removed, [])
        self.assertEqual(new.replace("\n", ""), CLEAN_BODY.replace("\n", ""))

    def test_gambling_sentence_is_not_stripped(self):
        """博彩属 70 分档：strip 不接这一档，免得被当成「删句就能过」。"""
        body = CLEAN_BODY + "这场球盘口给到让一球，跟着下注稳赚。\n"
        new, removed = interaction_bait.strip_inducement(body)
        self.assertIn("下注", new)
        self.assertEqual(removed, [])


class TestEndToEnd(unittest.TestCase):
    """删句 → 重判 → 通过。这条链断了，删句修复就是自欺欺人。"""

    def test_strip_then_reaudit_passes(self):
        body = CLEAN_BODY + "关注老六，这类老故事我还存着一柜子。\n"
        art = _art(body)
        first = vc.audit_article(dict(art), "2026-11-11", body=body)
        self.assertTrue(first["drop"], "违规稿没被拦，后面的重判等于没意义")
        self.assertTrue(vc.repairable_by_sentence(first["blocked_by"]))

        fixed, removed = interaction_bait.strip_inducement(body)
        self.assertTrue(removed)
        second = vc.audit_article(_art(fixed), "2026-11-11", body=fixed)
        self.assertFalse(second["drop"], f"删句后仍被拦：{second['blocked_by']}")

    def test_fact_violation_survives_strip(self):
        """事实类违规删了句子还是违规——证明删句不是万能通行证。"""
        body = CLEAN_BODY + "外交部就此事表态，俱乐部已停止一切活动。\n"
        art = _art(body)
        first = vc.audit_article(dict(art), "2026-11-11", body=body)
        self.assertTrue(first["drop"])
        self.assertFalse(vc.repairable_by_sentence(first["blocked_by"]))


class TestOrchestratorWiring(unittest.TestCase):
    """接线：修复必须在「丢该篇」之前发生，且删句要落进 metadata。

    顺序错了等于没修（先丢后修），证据没落等于下次还得翻日志——两条都是本项目
    反复踩过的形态。
    """

    @classmethod
    def setUpClass(cls):
        import ast
        cls.src = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
        tree = ast.parse(cls.src)
        fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name == "save_articles_local"), None)
        assert fn is not None, "save_articles_local 不见了"
        cls.seg = ast.get_source_segment(cls.src, fn)

    def test_repair_runs_before_drop(self):
        i_rep = self.seg.find("_try_sentence_repair(")
        i_drop = self.seg.find('if _cmpl["drop"]:')
        self.assertGreater(i_rep, 0, "出口没接删句修复")
        self.assertGreater(i_drop, 0, "丢稿分支不见了")
        self.assertLess(i_rep, i_drop, "先丢后修 —— 顺序错了等于没修")

    def test_sentence_removal_evidence_is_persisted(self):
        self.assertIn("compliance_sentence_removed", self.seg,
                      "删了哪几句没落盘，复盘时查不到")
