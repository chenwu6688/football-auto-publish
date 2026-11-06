"""出口合规审核（P0-1，2026-11-06）。

## 它守的是什么

合规闸门原先只装在 `_rewrite_with_retry` 一条通道上，其余 6 个 `generate_*`
（一级确定性供给 4 个 + 紧急球评 + 前瞻）一个都没有 ——「一半稿件从生成到发布，
合规判定为零」，全压到发布前那一层；而那一层命中就是**停发整批**。

本文件守四件事，缺一条这套改动就等于没做：

1. 判据**真的在判**：干净稿不误杀、违规稿必须被打红（否则闸门是恒真的摆设）。
2. **未判定 ≠ 通过**：出口拿不到证据的条目必须如实标 `not_judged` 并写原因，
   不许因为它「看起来没问题」就记 pass。
3. **证据落盘契约**：成功也要写 `_compliance`（逐条 status + 原因）。
   「成功就不写字段」正是 11-06 认定审核空转的根因。
4. **丢该篇、不停整批**：命中只剔除这一篇；`drop_violations` 端到端验证
   md 被移出发布集合、metadata 留证、另一篇不受影响。
"""

import ast
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import verify_compliance as vc  # noqa: E402

ORCH = ROOT / "orchestrator.py"

CLEAN_BODY = (
    "那不勒斯客场 2-1 逆转，中场休息后换了两处站位。\n\n"
    "第二段说中场的变化：边路压上，后腰回撤补位。\n\n"
    "第三段讲教练的调整，换人之后节奏明显快了。\n\n"
    "第四段收尾，这球你怎么看？\n\n"
    "> 本文由 AI 生成\n"
)

CLEAN = {"title": "那不勒斯客场 2-1 逆转，中场换位见效",
         "content": CLEAN_BODY, "images": ["a.jpg"]}


def _rules():
    return vc.load_rules()


def _verdict(res, rid):
    return next((v for v in res["verdicts"] if v["id"] == rid), None)


class TestAuditArticle(unittest.TestCase):
    """出口单篇审核的判定行为。"""

    def test_clean_article_is_not_dropped(self):
        res = vc.audit_article(dict(CLEAN), "2026-11-06", body=CLEAN_BODY)
        self.assertFalse(res["drop"], f"干净稿被误杀：{res['blocked_by']}")
        self.assertEqual(res["risk_score"], 0)

    def test_every_rule_gets_a_verdict(self):
        """逐条都要有结论——漏一条就是「静默跳过」。"""
        res = vc.audit_article(dict(CLEAN), "2026-11-06", body=CLEAN_BODY)
        rules = [r for r in _rules()["rules"] if str(r.get("tier")) != "n/a"]
        self.assertEqual(len(res["verdicts"]), len(rules),
                         "有规则没被出口审核覆盖到")
        self.assertEqual(res["total"], len(rules))
        for v in res["verdicts"]:
            self.assertIn(v["status"], (vc.PASS, vc.FAIL, vc.UNJ, vc.NA))
            self.assertTrue(v["detail"].strip(), f"{v['id']} 没有写原因")

    def test_inducement_drops_the_article(self):
        bad = dict(CLEAN, content=CLEAN_BODY + "\n关注我，每天带你复盘！\n")
        res = vc.audit_article(bad, "2026-11-06", body=bad["content"])
        self.assertTrue(res["drop"])
        self.assertIn("TT-07", res["blocked_by"])
        self.assertGreaterEqual(res["risk_score"], 20)

    def test_sensational_title_drops_the_article(self):
        bad = dict(CLEAN, title="震惊！那不勒斯更衣室彻底炸了")
        res = vc.audit_article(bad, "2026-11-06", body=CLEAN_BODY)
        self.assertTrue(res["drop"])
        self.assertIn("TT-04", res["blocked_by"])

    def test_political_boundary_drops_the_article(self):
        bad = dict(CLEAN, content=CLEAN_BODY + "\n外交部就此事表态。\n")
        res = vc.audit_article(bad, "2026-11-06", body=bad["content"])
        self.assertTrue(res["drop"])
        self.assertIn("TT-13", res["blocked_by"])

    def test_gambling_drops_the_article(self):
        bad = dict(CLEAN, content=CLEAN_BODY + "\n这场盘口开出让球盘，滚球可以买球。\n")
        res = vc.audit_article(bad, "2026-11-06", body=bad["content"])
        self.assertTrue(res["drop"])
        self.assertIn("TT-01", res["blocked_by"])


class TestUnjudgedIsNotPass(unittest.TestCase):
    """拿不到证据的条目必须是未判定，且原因可读。"""

    def test_missing_evidence_is_not_judged_not_pass(self):
        """一级确定性稿没有 _gate / _similarity —— 这两条不许判 pass。"""
        art = dict(CLEAN)          # 刻意不带 _gate / _similarity / publish_by
        res = vc.audit_article(art, "2026-11-06", body=CLEAN_BODY)
        for rid in ("TT-02", "TT-05", "TT-06"):
            v = _verdict(res, rid)
            self.assertIsNotNone(v, f"{rid} 没出现在出口结论里")
            self.assertEqual(v["status"], vc.UNJ,
                             f"{rid} 缺证据却判了 {v['status']}：{v['detail']}")
            self.assertTrue(v["detail"].strip())

    def test_batch_level_rule_is_registered_not_skipped(self):
        """批量级判据在出口判不了，必须登记为未判定并写明原因。"""
        res = vc.audit_article(dict(CLEAN), "2026-11-06", body=CLEAN_BODY)
        v = _verdict(res, "TT-14")
        self.assertIsNotNone(v, "TT-14 被静默跳过了")
        self.assertEqual(v["status"], vc.UNJ)
        self.assertIn("批量级", v["detail"])

    def test_no_body_does_not_become_pass(self):
        """正文给不出时不许拿标题冒充——正文类判据一律未判定。"""
        art = {"title": "那不勒斯 2-1 逆转"}
        res = vc.audit_article(art, "2026-11-06", body=None)
        for rid in ("TT-07", "TT-10", "TT-11"):
            v = _verdict(res, rid)
            self.assertEqual(v["status"], vc.UNJ, f"{rid} 无正文却判了 {v['status']}")
        self.assertFalse(res["drop"], "无证据时不许凭猜丢稿")

    def test_judged_count_is_exposed(self):
        """`judged/total` 是「这次到底审了几条」的唯一可核数字。"""
        res = vc.audit_article(dict(CLEAN), "2026-11-06", body=CLEAN_BODY)
        self.assertLess(res["judged"], res["total"],
                        "全都能判了？先把证据契约铺齐再说")
        self.assertGreater(res["judged"], 0)


class TestSameImplementationAsPublishGate(unittest.TestCase):
    """出口与发布前必须用**同一份判据**，不许各写一套。"""

    def test_audit_article_reuses_judges(self):
        src = Path(vc.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "audit_article"), None)
        self.assertIsNotNone(fn, "audit_article 不见了")
        seg = ast.get_source_segment(src, fn)
        self.assertIn("_SINGLE.get(name)", seg, "出口没有走共享的判据表")
        self.assertIn("_judge_names(rule)", seg, "出口没有按规则表取判据")
        self.assertIn("_DROP_TIERS", seg, "出口没有按档位决定丢不丢")

    def test_rule_table_is_the_only_source(self):
        """规则清单不在代码里再列一遍（判据 id 由规则表给）。"""
        src = Path(vc.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        hardcoded = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                for elt in node.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str) \
                            and elt.value.startswith("TT-"):
                        hardcoded.append(elt.value)
        self.assertEqual(hardcoded, [],
                         f"代码里硬编码了规则 id 清单：{hardcoded}——规则表才是唯一权威")


class TestDropViolations(unittest.TestCase):
    """发布前逐篇剔除：丢该篇，不停整批。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._o_root, self._o_audit = vc.ROOT, vc.AUDIT_DIR
        vc.ROOT = self.tmp
        vc.AUDIT_DIR = self.tmp / "audit"
        self.day = "2026-11-06"
        self.dir = self.tmp / "output" / self.day
        self.dir.mkdir(parents=True)

    def tearDown(self):
        vc.ROOT, vc.AUDIT_DIR = self._o_root, self._o_audit
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, bad_first=True):
        bad_body = CLEAN_BODY + "\n关注我，带你复盘每场球！\n"
        good_body = CLEAN_BODY
        (self.dir / "article-1-x.md").write_text(
            bad_body if bad_first else good_body, encoding="utf-8")
        (self.dir / "article-2-y.md").write_text(
            good_body if bad_first else bad_body, encoding="utf-8")
        meta = {"articles": [
            {"index": 1, "title": "第一篇", "path": str(self.dir / "article-1-x.md")},
            {"index": 2, "title": "第二篇", "path": str(self.dir / "article-2-y.md")},
        ]}
        (self.dir / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return meta

    def test_only_the_bad_article_is_removed(self):
        self._seed()
        out = vc.drop_violations(self.day)
        self.assertEqual(len(out["dropped"]), 1, f"剔除篇数不对：{out}")
        self.assertEqual(out["kept"], 1)
        self.assertIn("TT-07", out["dropped"][0]["blocked_by"])
        self.assertFalse((self.dir / "article-1-x.md").exists(),
                         "违规篇的 md 还留在发布集合里")
        self.assertTrue((self.dir / "article-2-y.md").exists(),
                        "合规篇被误移走了 —— 这就是「整批停」")
        self.assertTrue(list((self.dir / "dropped").glob("article-1-x.md")),
                        "剔除的稿子必须留档，不能直接删")

    def test_metadata_keeps_the_reason(self):
        self._seed()
        vc.drop_violations(self.day)
        meta = json.loads((self.dir / "metadata.json").read_text(encoding="utf-8"))
        dropped = [a for a in meta["articles"] if a.get("dropped_by_compliance")]
        self.assertEqual(len(dropped), 1)
        art = dropped[0]
        self.assertIn("TT-07", art["dropped_by_compliance"]["blocked_by"])
        self.assertTrue(art["dropped_by_compliance"]["verdicts"])
        self.assertTrue(art["_compliance"]["verdicts"], "逐条判定结果没落盘")

    def test_idempotent(self):
        """重复跑：不再重复剔除，也**不许覆盖**第一次留下的证据。"""
        self._seed()
        first = vc.drop_violations(self.day)
        second = vc.drop_violations(self.day)
        self.assertEqual(len(first["dropped"]), 1)
        self.assertEqual(second["dropped"], [], "第二次又剔了一遍")
        self.assertEqual(second["kept"], 1)
        meta = json.loads((self.dir / "metadata.json").read_text(encoding="utf-8"))
        art = next(a for a in meta["articles"] if a.get("dropped_by_compliance"))
        self.assertIn("TT-07", art["dropped_by_compliance"]["blocked_by"],
                      "第二次跑把「为什么被剔除」的证据冲掉了")

    def test_no_metadata_is_reported_not_crashed(self):
        out = vc.drop_violations("2026-01-01")
        self.assertEqual(out["dropped"], [])
        self.assertIn("note", out)


class TestOrchestratorWiring(unittest.TestCase):
    """接线：出口审核必须真的在 save_articles_local 里，且处置是 continue。"""

    @classmethod
    def setUpClass(cls):
        cls.src = ORCH.read_text(encoding="utf-8")
        tree = ast.parse(cls.src)
        cls.fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                       and n.name == "save_articles_local"), None)

    def test_calls_shared_audit(self):
        self.assertIsNotNone(self.fn, "save_articles_local 不见了")
        seg = ast.get_source_segment(self.src, self.fn)
        self.assertIn("compliance_gate.audit_article", seg,
                      "出口没调合规入口——6 条生成通道就又裸奔了")
        self.assertIn("body=art.get(\"content\")", seg,
                      "出口审核必须拿最终正文（含注入后的互动话术）去判")

    def test_drop_is_continue_not_abort(self):
        seg = ast.get_source_segment(self.src, self.fn)
        idx = seg.find('if _cmpl["drop"]')
        self.assertGreater(idx, 0, "没有 drop 分支")
        branch = seg[idx:idx + 1600]
        self.assertIn("continue", branch, "命中违规后没有 continue —— 会变成停整批")
        self.assertNotIn("raise", branch)
        self.assertNotIn("sys.exit", branch)

    def test_compliance_evidence_saved_even_on_success(self):
        seg = ast.get_source_segment(self.src, self.fn)
        self.assertIn('"_compliance"', seg, "成功时没写 _compliance（证据契约）")
        self.assertNotIn('if not _cmpl["drop"]:\n            art["_compliance"]',
                         "证据只在失败时才写 —— 正是「成功就不写字段」的老毛病")

    def test_exception_does_not_block_publishing(self):
        seg = ast.get_source_segment(self.src, self.fn)
        start = seg.find("compliance_gate.audit_article")
        self.assertGreater(start, 0)
        idx = seg.find("except Exception as _exc", start)
        self.assertGreater(idx, start, "出口审核没有兜住异常")
        self.assertIn('"drop": False', seg[idx:idx + 400],
                      "审核自己炸了不该阻断发布，但必须 loud + 留痕")

    def test_all_dropped_is_announced(self):
        self.assertIn("compliance_dropped", self.src)
        self.assertIn("这是「全丢」，不是「正常空批」", self.src,
                      "0 篇和空批必须区分——否则没人知道该补发")


class TestWorkflowDisposal(unittest.TestCase):
    """CI 侧的处置必须与出口一致：逐篇剔除，不是停整批。"""

    def test_batch_uses_drop_violations(self):
        wf = (ROOT / ".github" / "workflows" / "batch.yml").read_text(encoding="utf-8")
        self.assertIn("--drop-violations", wf,
                      "发布前那层还是「命中即停整批」")
        self.assertIn("--fail-on-block-only", wf)

    def test_image_relevance_only_hard_items_block(self):
        wf = (ROOT / ".github" / "workflows" / "batch.yml").read_text(encoding="utf-8")
        self.assertIn("--fail-on-hard-only", wf,
                      "产物相关性还在阻断发布 —— 一篇图不对会拖垮整批")
        self.assertNotIn("verify_image_relevance.py --quiet || rc=$?", wf,
                         "又退回「一刀切全硬」了")

    def test_daily_layer_keeps_strict(self):
        """每日层不许软化：未判定必须暴露。"""
        wf = (ROOT / ".github" / "workflows" / "daily-result.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/verify_compliance.py --days 1", wf)
        self.assertNotIn("verify_compliance.py --days 1 --quiet || true", wf)


if __name__ == "__main__":
    unittest.main()
