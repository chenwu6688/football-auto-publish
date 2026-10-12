"""传闻分级措辞与传闻标题硬校验单测（计划 4.1 / 3.2 / 13.3）。"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import rumor_wording as rw


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent.parent / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestNormalizeTier(unittest.TestCase):
    def test_chinese_and_english_aliases(self):
        self.assertEqual(rw.normalize_tier("传闻"), rw.CONF_RUMOR)
        self.assertEqual(rw.normalize_tier("rumor"), rw.CONF_RUMOR)
        self.assertEqual(rw.normalize_tier("low"), rw.CONF_RUMOR)
        self.assertEqual(rw.normalize_tier("单源"), rw.CONF_SINGLE)
        self.assertEqual(rw.normalize_tier("medium"), rw.CONF_SINGLE)
        self.assertEqual(rw.normalize_tier("已确认"), rw.CONF_CONFIRMED)
        self.assertEqual(rw.normalize_tier("confirmed"), rw.CONF_CONFIRMED)

    def test_unknown_returns_none(self):
        self.assertIsNone(rw.normalize_tier(None))
        self.assertIsNone(rw.normalize_tier(""))
        self.assertIsNone(rw.normalize_tier("大概吧"))
        self.assertIsNone(rw.normalize_tier("C"))

    def test_tiers_same_as_fact_card(self):
        import fact_card as fc
        self.assertEqual(set(rw.TIERS), {fc.CONF_CONFIRMED, fc.CONF_SINGLE, fc.CONF_RUMOR})


class TestTierOf(unittest.TestCase):
    def test_direct_keys(self):
        self.assertEqual(rw.tier_of({"可信度": "传闻"}), rw.CONF_RUMOR)
        self.assertEqual(rw.tier_of({"fact_tier": "single"}), rw.CONF_SINGLE)
        self.assertEqual(rw.tier_of("已确认"), rw.CONF_CONFIRMED)

    def test_nested_cards_take_weakest(self):
        obj = {"可信度": "已确认", "cards": [{"可信度": "传闻"}, {"可信度": "单源"}]}
        self.assertEqual(rw.tier_of(obj), rw.CONF_RUMOR)      # 取严不取宽

    def test_nested_facts_list(self):
        self.assertEqual(rw.tier_of({"facts": [{"tier": "rumor"}]}), rw.CONF_RUMOR)

    def test_unknown(self):
        self.assertIsNone(rw.tier_of({}))
        self.assertIsNone(rw.tier_of({"allowed_teams": ["曼城"]}))
        self.assertIsNone(rw.tier_of(None))

    def test_weakest_helper(self):
        self.assertEqual(rw.weakest(["已确认", "单源"]), rw.CONF_SINGLE)
        self.assertEqual(rw.weakest([]), None)


class TestTitleViolations(unittest.TestCase):
    """计划 4.1：传闻类标题禁用肯定语气。"""

    def test_rumor_title_with_assertive_banned(self):
        for t in ("曼城官宣签下某某", "某某确定加盟皇马", "交易敲定", "某某正式加盟曼城", "尘埃落定"):
            v = rw.title_violations(rw.CONF_RUMOR, t)
            self.assertTrue(v, f"{t} 应被判违规")

    def test_rumor_title_without_hedge_banned(self):
        v = rw.title_violations(rw.CONF_RUMOR, "某某加盟曼城")
        self.assertTrue(any("缺少模糊" in x for x in v))

    def test_rumor_title_with_hedge_passes(self):
        for t in ("某某或将加盟曼城", "据悉某某接近转会", "曝某某被曼城询价", "传闻某某转会"):
            self.assertEqual(rw.title_violations(rw.CONF_RUMOR, t), [], f"{t} 不应判违规")

    def test_confirmed_title_declarative_ok(self):
        self.assertEqual(rw.title_violations(rw.CONF_CONFIRMED, "某某正式加盟曼城"), [])

    def test_single_title_prefers_source(self):
        self.assertEqual(rw.title_violations(rw.CONF_SINGLE, "罗马诺：某某接近加盟曼城"), [])
        self.assertTrue(rw.title_violations(rw.CONF_SINGLE, "某某加盟曼城"))

    def test_unknown_tier_no_judgement(self):
        self.assertEqual(rw.title_violations(None, "曼城官宣签下某某"), [])


class TestBodyViolations(unittest.TestCase):
    def test_rumor_body_needs_wording(self):
        self.assertTrue(rw.body_violations(rw.CONF_RUMOR, "双方已经谈妥。"))
        self.assertEqual(rw.body_violations(rw.CONF_RUMOR, "有消息称双方仍在谈。"), [])
        self.assertEqual(rw.body_violations(rw.CONF_RUMOR, "据悉双方已接触。"), [])

    def test_single_body_needs_source(self):
        self.assertTrue(rw.body_violations(rw.CONF_SINGLE, "两边谈好了。"))
        self.assertEqual(rw.body_violations(rw.CONF_SINGLE, "据米兰体育报报道，两边仍在谈。"), [])

    def test_confirmed_body_free(self):
        self.assertEqual(rw.body_violations(rw.CONF_CONFIRMED, "俱乐部已官宣。"), [])


class TestCheck(unittest.TestCase):
    def test_rumor_blocks_title_and_body(self):
        res = rw.check(rw.CONF_RUMOR, "曼城官宣签下某某", "已经谈妥了")
        self.assertFalse(res["ok"])
        self.assertTrue(res["judged"])
        self.assertEqual(len(res["hard"]), 3)          # 断言词 + 缺模糊 + 正文缺措辞
        self.assertIn("肯定语气", res["hint"])

    def test_single_tier_is_warn_by_default(self):
        res = rw.check(rw.CONF_SINGLE, "某某加盟曼城", "两边谈好了")
        self.assertTrue(res["ok"])                     # 默认仅传闻级为硬违规
        self.assertTrue(res["warn"])

    def test_blocking_tiers_can_extend(self):
        res = rw.check(rw.CONF_SINGLE, "某某加盟曼城", "两边谈好了",
                       blocking_tiers=(rw.CONF_SINGLE, rw.CONF_RUMOR))
        self.assertFalse(res["ok"])

    def test_unknown_tier_not_judged(self):
        res = rw.check(None, "曼城官宣签下某某", "已官宣")
        self.assertTrue(res["ok"])
        self.assertFalse(res["judged"])

    def test_check_topic_uses_tier_of(self):
        topic = {"可信度": "传闻", "cards": [{"可信度": "单源"}]}
        res = rw.check_topic(topic, "曼城官宣签下某某", "")
        self.assertFalse(res["ok"])
        self.assertEqual(res["tier"], rw.CONF_RUMOR)

    def test_selftest_all_pass(self):
        self.assertEqual(rw.selftest(), [])
        self.assertIn("通过", rw.render_selftest())


class TestOrchestratorIntegration(unittest.TestCase):
    """计划 4.1 落到标题评分器与一致性校验（orchestrator）。

    注意：``check_consistency_plan`` 会写一致性台账（计划 7.3），故必须把台账路径
    隔离到临时文件，否则跑测试会污染真实的 ``data/consistency_stats.json``。
    """

    @classmethod
    def setUpClass(cls):
        import orchestrator
        cls.o = orchestrator

    def setUp(self):
        import consistency_stats as cs
        self._d = tempfile.TemporaryDirectory()
        self._s, self._l = cs.STATS_PATH, cs.LABELS_PATH
        cs.STATS_PATH = Path(self._d.name) / "stats.json"
        cs.LABELS_PATH = Path(self._d.name) / "labels.json"

    def tearDown(self):
        import consistency_stats as cs
        cs.STATS_PATH, cs.LABELS_PATH = self._s, self._l
        self._d.cleanup()

    def test_rumor_assertive_title_scores_zero(self):
        sc, passed, reasons = self.o.score_title_plan(
            {}, "曼城官宣签下哈兰德，转会费创纪录", "转会资讯", fact_tier="传闻")
        self.assertEqual(sc, 0)
        self.assertFalse(passed)
        self.assertTrue(any("肯定语气" in r for r in reasons))

    def test_rumor_hedged_title_scores_normally(self):
        sc, passed, _ = self.o.score_title_plan(
            {}, "哈兰德或将转会曼城，据悉报价已递交，谁能笑到最后？", "转会资讯",
            fact_tier="传闻")
        self.assertGreater(sc, 0)
        self.assertTrue(passed)

    def test_tier_auto_derived_from_topic(self):
        sc, passed, _ = self.o.score_title_plan(
            {"可信度": "传闻"}, "曼城官宣签下哈兰德", "转会资讯")
        self.assertEqual(sc, 0)
        self.assertFalse(passed)

    def test_no_tier_no_rumor_penalty(self):
        sc, _, _ = self.o.score_title_plan({}, "曼城官宣签下哈兰德", "转会资讯")
        self.assertGreater(sc, 0)

    def test_confirmed_tier_declarative_allowed(self):
        sc, _, reasons = self.o.score_title_plan(
            {}, "曼城正式官宣签下哈兰德，五年合同锁死核心", "转会资讯", fact_tier="已确认")
        self.assertGreater(sc, 0)
        self.assertFalse(any("肯定语气" in r for r in reasons))

    def test_consistency_plan_enforces_rumor_wording(self):
        art = {"title": "曼城官宣签下某某", "content": "已经谈妥了，静待官宣。"}
        ok, issues = self.o.check_consistency_plan(art, {"可信度": "传闻"}, blocking=True)
        self.assertFalse(ok)
        self.assertTrue(any("肯定语气" in i for i in issues))

    def test_consistency_plan_no_tier_is_noop(self):
        art = {"title": "曼城官宣签下某某", "content": "已经谈妥了。"}
        ok, issues = self.o.check_consistency_plan(art, {}, blocking=True)
        self.assertTrue(ok)
        self.assertEqual(issues, [])

    def test_consistency_plan_hedged_rumor_passes(self):
        art = {"title": "某某或将加盟曼城", "content": "据悉，双方仍在谈判，一切以官宣为准。"}
        ok, issues = self.o.check_consistency_plan(art, {"可信度": "传闻"}, blocking=True)
        self.assertTrue(ok)
        self.assertEqual(issues, [])


class TestVerifyScript(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "verify_rumor_wording",
            str(Path(__file__).resolve().parent.parent / "scripts" / "verify_rumor_wording.py"))
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)

    def test_rule_and_tier_checks_are_hard_and_pass(self):
        for fn in (self.script.check_rules, self.script.check_tier_source):
            c = fn()
            self.assertFalse(c.get("advisory"))
            self.assertTrue(c["ok"])

    def test_article_scan_skips_without_output(self):
        orig = self.script.OUTPUT_DIR
        self.script.OUTPUT_DIR = Path("/nonexistent/output")
        try:
            c = self.script.check_article_titles()
        finally:
            self.script.OUTPUT_DIR = orig
        self.assertTrue(c["skipped"])
        self.assertTrue(c["ok"])

    def test_article_scan_flags_rumor_assertive_title(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "2026-10-12").mkdir(parents=True, exist_ok=True)
        (tmp / "2026-10-12" / "metadata.json").write_text(json.dumps({
            "date": "2026-10-12",
            "articles": [
                {"title": "曼城官宣签下某某，据悉转会费惊人"},   # 传闻标记 + 断言词 → 违规
                {"title": "哈兰德或将转会，谁能笑到最后？"},     # 仅传闻标记 → 合规
            ]}, ensure_ascii=False), encoding="utf-8")
        orig = self.script.OUTPUT_DIR
        self.script.OUTPUT_DIR = tmp
        try:
            c = self.script.check_article_titles()
        finally:
            self.script.OUTPUT_DIR = orig
        self.assertFalse(c["ok"])
        self.assertEqual(len(c["violations"]), 1)
        self.assertEqual(c["checked"], 2)

    def test_vocab_check_is_advisory(self):
        c = self.script.check_vocab()
        self.assertTrue(c.get("advisory"))
        self.assertTrue(c["ok"])


if __name__ == "__main__":
    unittest.main()
