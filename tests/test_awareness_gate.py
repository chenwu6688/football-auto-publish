"""受众认知度分级硬门槛单测（计划 13.1）。"""

import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import awareness_gate as ag

CST = timezone(timedelta(hours=8))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent.parent / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestLevels(unittest.TestCase):
    """A/B/C 解析口径：未登记 != C（计划 13.1）。"""

    def setUp(self):
        self.emap = {"皇马": "A", "勒沃库森": "B", "科莫": "C"}

    def test_level_of(self):
        self.assertEqual(ag.level_of("皇马", self.emap), "A")
        self.assertEqual(ag.level_of("勒沃库森", self.emap), "B")
        self.assertEqual(ag.level_of("科莫", self.emap), "C")
        self.assertEqual(ag.level_of("查无此队", self.emap), ag.UNREGISTERED)
        self.assertEqual(ag.level_of("", self.emap), ag.UNREGISTERED)

    def test_classify_rejects_illegal(self):
        self.assertEqual(ag.classify("a"), "A")          # 大小写规整
        self.assertEqual(ag.classify(" C "), "C")
        self.assertEqual(ag.classify("D"), ag.UNREGISTERED)
        self.assertEqual(ag.classify(None), ag.UNREGISTERED)

    def test_registration_and_confidence(self):
        self.assertTrue(ag.is_registered("皇马", self.emap))
        self.assertFalse(ag.is_registered("查无此队", self.emap))
        self.assertEqual(ag.confidence_of("皇马", self.emap), "high")
        self.assertEqual(ag.confidence_of("勒沃库森", self.emap), "medium")
        self.assertEqual(ag.confidence_of("科莫", self.emap), "low")
        self.assertEqual(ag.confidence_of("查无此队", self.emap), "")

    def test_title_generation_blocked_for_c(self):
        # 计划 13.1：C 级 confidence=low，不进标题生成，仅作背景数据
        self.assertTrue(ag.can_generate_title("皇马", self.emap))
        self.assertTrue(ag.can_generate_title("勒沃库森", self.emap))
        self.assertFalse(ag.can_generate_title("科莫", self.emap))
        self.assertFalse(ag.can_generate_title("查无此队", self.emap))

    def test_parse_entity_map_from_file(self):
        d = json.loads((Path(__file__).resolve().parent.parent
                        / "config" / "entity_map.json").read_text(encoding="utf-8"))
        m = ag.parse_entity_map()
        expect = {v["name_zh"] for v in d["entities"].values() if v.get("name_zh")}
        self.assertEqual(set(m.keys()), expect)          # 以 name_zh 为键（重名合并）
        self.assertGreater(len(m), 500)
        self.assertTrue(all(v in ag.LEVELS for v in m.values()))


class TestDecideAgainstPlanTable(unittest.TestCase):
    """逐条对照计划 13.1 分级表（表 25）与表 19（L1 预筛）。"""

    def test_a_enters_news_flow(self):
        for sec in ("转会动态", "人物故事", "中国足球", "战术榜单"):
            d = ag.decide_level("A", sec, channel="news")
            self.assertEqual(d["action"], "pass")
            self.assertTrue(d["ok"])
            self.assertTrue(d["title_allowed"])

    def test_b_needs_anchor_or_story(self):
        d = ag.decide_level("B", "转会动态", channel="news")
        self.assertEqual(d["action"], "needs_anchor")
        self.assertTrue(d["requires_anchor"])
        # 已配锚点 → 放行
        self.assertEqual(ag.decide_level("B", "转会动态", channel="news",
                                         has_anchor=True)["action"], "pass")
        # 改走故事角度 → 放行
        self.assertEqual(ag.decide_level("B", "人物故事", channel="news")["action"], "pass")

    def test_c_demoted_out_of_news_flow(self):
        # C 级在新闻流一律不进
        for sec in ("转会动态", "战术榜单"):
            d = ag.decide_level("C", sec, channel="news")
            self.assertEqual(d["action"], "block")
            self.assertFalse(d["ok"])
        # 数据/榜单类通道可进但降权，且不得参与标题生成
        d = ag.decide_level("C", "战术榜单", channel="data_list")
        self.assertEqual(d["action"], "demote")
        self.assertTrue(d["ok"])
        self.assertTrue(d["demote"])
        self.assertFalse(d["title_allowed"])
        # 数据通道里 C 级仍不可落新闻流板块
        self.assertEqual(ag.decide_level("C", "转会动态", channel="data_list")["action"], "block")

    def test_unregistered_blocked_everywhere(self):
        for ch in ag.CHANNELS:
            for sec in ("转会动态", "战术榜单", "人物故事"):
                d = ag.decide_level(ag.UNREGISTERED, sec, channel=ch)
                self.assertEqual(d["action"], "block")
                self.assertIn("未登记", d["reason"])
        # 缺失值同样按未登记处理，绝不默认放行
        self.assertEqual(ag.decide_level("", "战术榜单", channel="data_list")["action"], "block")

    def test_selftest_all_cases_pass(self):
        self.assertEqual(ag.selftest(), [])
        self.assertIn("通过", ag.render_selftest())

    def test_decide_by_name_uses_map(self):
        emap = {"科莫": "C"}
        self.assertFalse(ag.decide("科莫", "转会动态", channel="news", emap=emap)["ok"])
        self.assertTrue(ag.decide("科莫", "战术榜单", channel="data_list", emap=emap)["ok"])
        self.assertEqual(ag.decide("查无此队", "战术榜单", channel="data_list",
                                   emap=emap)["action"], "block")


class TestGateCandidates(unittest.TestCase):
    def setUp(self):
        ag.reset_cache()
        self._orig = ag.load_map
        ag.load_map = lambda path=None: {"曼城": "A", "勒沃库森": "B", "科莫": "C"}

    def tearDown(self):
        ag.load_map = self._orig
        ag.reset_cache()

    def test_batch_annotates_and_drops(self):
        cands = [{"card": {"主体": "曼城"}, "板块": "战术榜单"},
                 {"card": {"主体": "勒沃库森"}, "板块": "转会动态"},
                 {"card": {"主体": "科莫"}, "板块": "战术榜单"},
                 {"card": {"主体": "查无此队"}, "板块": "战术榜单"}]
        kept, dropped, decisions = ag.gate_candidates(cands, channel="news")
        self.assertEqual([c["card"]["主体"] for c in kept], ["曼城", "勒沃库森"])
        self.assertEqual([d[0]["card"]["主体"] for d in dropped], ["科莫", "查无此队"])
        self.assertTrue(kept[0]["_awareness"]["ok"])
        self.assertTrue(kept[1]["_requires_anchor"])          # B 级待补锚点
        self.assertTrue(all("认知度门槛" in r for _, r in dropped))

    def test_data_channel_demotes_c(self):
        cands = [{"card": {"主体": "科莫"}, "板块": "战术榜单"}]
        kept, dropped, _ = ag.gate_candidates(cands, channel="data_list")
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["_demoted"])
        self.assertEqual(dropped, [])

    def test_empty_and_blank_subject_pass(self):
        kept, dropped, _ = ag.gate_candidates([{"板块": "战术榜单"}], channel="news")
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_summarize(self):
        cands = [{"card": {"主体": "曼城"}}, {"card": {"主体": "科莫"}},
                 {"card": {"主体": "查无此队"}}]
        _, _, decs = ag.gate_candidates(cands, channel="news")
        s = ag.summarize(decs)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["by_level"]["A"], 1)
        self.assertEqual(s["blocked"], 2)


class TestRankerIntegration(unittest.TestCase):
    """L1 预筛接入硬门槛（计划 11.2 L1 + 13.1）。"""

    def setUp(self):
        import ranker
        self.ranker = ranker
        self._orig = ranker._awareness_cache
        ranker._awareness_cache = {"曼城": "A", "勒沃库森": "B", "科莫": "C"}
        self.NOW = datetime(2026, 10, 10, 20, 0, tzinfo=CST)

    def tearDown(self):
        self.ranker._awareness_cache = self._orig

    def _card(self, subj, **kw):
        c = {"id": "c1", "主体": subj, "动作": "转会", "发生时间": "2026-10-10",
             "可用角度": ["a", "b", "c"], "生命周期": "进行中",
             "来源列表": [{"来源名": "zhibo8", "地址": "http://z"}]}
        c.update(kw)
        return c

    def test_unregistered_subject_blocked(self):
        kept, dropped = self.ranker.l1_prefilter(
            [{"card": self._card("查无此队"), "板块": "战术榜单"}],
            {"now": self.NOW, "max_age_hours": 72})
        self.assertEqual(kept, [])
        self.assertIn("未登记", dropped[0][1])

    def test_c_level_not_in_news_flow(self):
        kept, dropped = self.ranker.l1_prefilter(
            [{"card": self._card("科莫"), "板块": "战术榜单"}],
            {"now": self.NOW, "max_age_hours": 72})
        self.assertEqual(kept, [])
        self.assertIn("降权", dropped[0][1])

    def test_b_level_flagged_for_anchor(self):
        kept, dropped = self.ranker.l1_prefilter(
            [{"card": self._card("勒沃库森"), "板块": "转会动态"}],
            {"now": self.NOW, "max_age_hours": 72})
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["_requires_anchor"])

    def test_b_level_story_section_no_flag(self):
        kept, _ = self.ranker.l1_prefilter(
            [{"card": self._card("勒沃库森"), "板块": "人物故事"}],
            {"now": self.NOW, "max_age_hours": 72})
        self.assertEqual(len(kept), 1)
        self.assertFalse(kept[0].get("_requires_anchor"))

    def test_a_level_passes_unflagged(self):
        kept, _ = self.ranker.l1_prefilter(
            [{"card": self._card("曼城"), "板块": "战术榜单"}],
            {"now": self.NOW, "max_age_hours": 72})
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["_awareness"]["action"], "pass")


class TestDeterministicSupplyIntegration(unittest.TestCase):
    """一级·确定性节点接入硬门槛（计划 12.1 + 13.1）。"""

    def setUp(self):
        self.ds = _load("deterministic_supply",
                        "deterministic_supply.py")

    def _cal(self, *nodes):
        return {"season": "t", "nodes": list(nodes)}

    def test_missing_awareness_node_dropped(self):
        cal = self._cal({"id": "n1", "label": "无认知度节点", "date": "2027-02-02",
                         "column": "战术榜单"})
        dropped = []
        out = self.ds.build_node_topics(date(2027, 2, 1), horizon_days=7,
                                        calendar=cal, dropped=dropped)
        self.assertEqual(out, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn("未登记", dropped[0][1])

    def test_c_node_only_in_data_sections(self):
        cal = self._cal({"id": "c_news", "label": "C 级转会题", "date": "2027-02-02",
                         "column": "转会动态", "awareness": "C"},
                        {"id": "c_list", "label": "C 级榜单题", "date": "2027-02-02",
                         "column": "战术榜单", "awareness": "C"})
        out = self.ds.build_node_topics(date(2027, 2, 1), horizon_days=7, calendar=cal)
        ids = [t["node_id"] for t in out]
        self.assertNotIn("c_news", ids)
        self.assertIn("c_list", ids)
        node = next(t for t in out if t["node_id"] == "c_list")
        self.assertEqual(node["awareness_decision"], "demote")
        self.assertFalse(node["title_allowed"])

    def test_a_node_passes_and_records_decision(self):
        cal = self._cal({"id": "a1", "label": "A 级节点", "date": "2027-02-02",
                         "column": "转会动态", "awareness": "A"})
        out = self.ds.build_node_topics(date(2027, 2, 1), horizon_days=7, calendar=cal)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["awareness_decision"], "pass")
        self.assertTrue(out[0]["title_allowed"])

    def test_real_calendar_nodes_all_survive_gate(self):
        # 真实赛季日历不应因门槛被整批剔除（否则一级供给断供）
        out = self.ds.build_node_topics(date(2027, 1, 1), horizon_days=None)
        self.assertGreaterEqual(len(out), 3)
        self.assertTrue(all(t["awareness"] in ag.LEVELS for t in out))

    def test_stock_persists_awareness_trace(self):
        tmp = Path(tempfile.mkdtemp())
        orig_dir, orig_path = self.ds.STORE_DIR, self.ds.STORE_PATH
        self.ds.STORE_DIR, self.ds.STORE_PATH = tmp, tmp / "items.json"
        try:
            topic = {"topic_id": "node-x", "node_id": "x", "板块": "战术榜单",
                     "node_date": "2027-02-02", "publish_from": "2027-01-26",
                     "publish_by": "2027-02-02", "awareness": "C",
                     "awareness_decision": "demote", "title_allowed": False}
            self.ds.stock({"title": "t", "content": "x" * 300}, topic)
            item = self.ds._load_store()["items"][0]
            self.assertEqual(item["awareness"], "C")
            self.assertEqual(item["awareness_decision"], "demote")
            self.assertFalse(item["title_allowed"])
        finally:
            self.ds.STORE_DIR, self.ds.STORE_PATH = orig_dir, orig_path


class TestVerifyScript(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "verify_awareness_gate",
            str(Path(__file__).resolve().parent.parent / "scripts" / "verify_awareness_gate.py"))
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)

    def test_rule_selfcheck_is_hard_and_passes(self):
        c = self.script.check_rules()
        self.assertFalse(c.get("advisory"))
        self.assertTrue(c["ok"])

    def test_entity_map_check_passes(self):
        c = self.script.check_entity_map()
        self.assertFalse(c.get("advisory"))
        self.assertTrue(c["ok"])
        self.assertGreater(c["total"], 100)

    def test_supply_check_skips_when_no_artifacts(self):
        op, on = self.script.POOL_PATH, self.script.NODE_STORE
        self.script.POOL_PATH = Path("/nonexistent/pool.json")
        self.script.NODE_STORE = Path("/nonexistent/items.json")
        try:
            c = self.script.check_supply_artifacts()
        finally:
            self.script.POOL_PATH, self.script.NODE_STORE = op, on
        self.assertTrue(c["skipped"])
        self.assertTrue(c["ok"])

    def test_supply_check_flags_violations(self):
        tmp = Path(tempfile.mkdtemp())
        pool = tmp / "topic_pool.json"
        pool.write_text(json.dumps({"topics": [
            {"topic_id": "t1", "awareness_pair": "A/C"},
            {"topic_id": "t2", "awareness_pair": "C/C"},
            {"topic_id": "t3", "awareness_pair": "A/?"},
        ]}, ensure_ascii=False), encoding="utf-8")
        store = tmp / "items.json"
        store.write_text(json.dumps({"items": [
            {"id": "s1", "awareness": "A", "awareness_decision": "pass", "section": "转会动态"},
            {"id": "s2", "awareness": "C", "awareness_decision": "demote", "section": "转会动态"},
        ]}, ensure_ascii=False), encoding="utf-8")
        op, on = self.script.POOL_PATH, self.script.NODE_STORE
        self.script.POOL_PATH, self.script.NODE_STORE = pool, store
        try:
            c = self.script.check_supply_artifacts()
        finally:
            self.script.POOL_PATH, self.script.NODE_STORE = op, on
        self.assertFalse(c["ok"])
        kinds = {v["kind"] for v in c["violations"]}
        self.assertIn("C 级双主体仍进新闻流", kinds)
        self.assertIn("未登记/非法认知度", kinds)
        self.assertIn("C 级越板（非数据/榜单）", kinds)

    def test_distribution_is_advisory(self):
        c = self.script.check_distribution()
        self.assertTrue(c.get("advisory"))
        self.assertEqual(c["unregistered"], 0)


if __name__ == "__main__":
    unittest.main()
