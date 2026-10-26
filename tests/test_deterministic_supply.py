"""一级·确定性节点供给单测（计划 12.1）。"""

import json
import tempfile
import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import deterministic_supply as ds

CST = timezone(timedelta(hours=8))

CAL = {
    "season": "test",
    "nodes": [
        {"id": "n_close", "type": "转会窗截止日", "label": "冬窗关闭",
         "date": "2027-02-02", "lead_days": 7, "column": "转会动态", "awareness": "A",
         "template": "冬窗只剩{days}天，谁最着急？"},
        {"id": "n_final", "type": "赛季节点", "label": "欧冠决赛",
         "date": "2027-05-29", "lead_days": 7, "column": "战术榜单", "awareness": "A",
         "template": "欧冠决赛还有{days}天"},
        {"id": "n_far", "type": "赛季节点", "label": "很远的节点",
         "date": "2030-01-01", "lead_days": 7, "column": "战术榜单", "awareness": "C",
         "template": "还有{days}天"},
    ],
}


class TestBuildNodeTopics(unittest.TestCase):
    def test_window_filter(self):
        # 2027-02-01 距冬窗关闭 1 天 → 命中；欧冠决赛 5-29 太远 → 不命中
        topics = ds.build_node_topics(date(2027, 2, 1), horizon_days=7, calendar=CAL)
        ids = [t["node_id"] for t in topics]
        self.assertIn("n_close", ids)
        self.assertNotIn("n_final", ids)
        self.assertNotIn("n_far", ids)

    def test_horizon_boundary(self):
        # 预生成窗口 = node_date ∈ [today, today+horizon]；lead 只决定可发布窗口
        # 2027-01-26 距冬窗关闭(02-02) 7 天 → 命中（horizon=7 边界）
        t0 = ds.build_node_topics(date(2027, 1, 26), horizon_days=7, calendar=CAL)
        self.assertIn("n_close", [t["node_id"] for t in t0])
        # 2027-01-25 距 02-02 为 8 天 → 超出预生成窗口
        t1 = ds.build_node_topics(date(2027, 1, 25), horizon_days=7, calendar=CAL)
        self.assertNotIn("n_close", [t["node_id"] for t in t1])

    def test_template_and_fields(self):
        topics = ds.build_node_topics(date(2027, 1, 30), horizon_days=7, calendar=CAL)
        t = next(x for x in topics if x["node_id"] == "n_close")
        self.assertEqual(t["days_to"], 3)
        self.assertIn("3", t["conflict"])
        self.assertEqual(t["source_layer"], "确定性")
        self.assertEqual(t["板块"], "转会动态")
        self.assertEqual(t["publish_by"], "2027-02-02")
        self.assertEqual(t["publish_from"], "2027-01-26")

    def test_node_date_passed_excluded(self):
        topics = ds.build_node_topics(date(2027, 2, 3), horizon_days=7, calendar=CAL)
        self.assertNotIn("n_close", [t["node_id"] for t in topics])


class TestStoreLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._p1 = mock.patch.object(ds, "STORE_DIR", self.tmp)
        self._p2 = mock.patch.object(ds, "STORE_PATH", self.tmp / "items.json")
        self._p1.start(); self._p2.start()

    def tearDown(self):
        self._p1.stop(); self._p2.stop()

    def _topic(self, node_id="n_close", pb="2027-02-02", pf="2027-01-26"):
        return {"topic_id": f"node-{node_id}-{pb}", "node_id": node_id,
                "node_type": "转会窗截止日", "node_date": pb,
                "publish_from": pf, "publish_by": pb, "板块": "转会动态"}

    def test_stock_available_fill(self):
        rid = ds.stock({"title": "冬窗倒计时", "content": "x" * 300}, self._topic())
        avail = ds.available(date_str="2027-02-01")
        self.assertEqual(len(avail), 1)
        picked = ds.fill_gap(1, date_str="2027-02-01")
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["id"], rid)
        # 取用后不再可用
        self.assertEqual(ds.available(date_str="2027-02-01"), [])

    def test_available_outside_window(self):
        ds.stock({"title": "冬窗倒计时", "content": "x" * 300}, self._topic())
        # 早于 publish_from
        self.assertEqual(ds.available(date_str="2027-01-01"), [])
        # 晚于 publish_by
        self.assertEqual(ds.available(date_str="2027-02-05"), [])

    def test_expire_marks_past(self):
        ds.stock({"title": "冬窗倒计时", "content": "x" * 300}, self._topic())
        n = ds.expire(now=datetime(2027, 2, 10, tzinfo=CST))
        self.assertEqual(n, 1)
        self.assertEqual(ds.available(date_str="2027-02-01"), [])

    def test_pending_excludes_stocked(self):
        cal = CAL
        before = ds.pending_nodes(date(2027, 2, 1), horizon_days=7, calendar=cal)
        self.assertIn("n_close", [t["node_id"] for t in before])
        ds.stock({"title": "x", "content": "y" * 300}, self._topic())
        after = ds.pending_nodes(date(2027, 2, 1), horizon_days=7, calendar=cal)
        self.assertNotIn("n_close", [t["node_id"] for t in after])


class TestCalendarIO(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        cal = ds.load_calendar(Path(tempfile.mkdtemp()) / "nope.yaml")
        self.assertEqual(cal["nodes"], [])

    def test_real_calendar_loads(self):
        cal = ds.load_calendar()
        self.assertTrue(len(cal.get("nodes", [])) >= 3)


# ============================================================
# 2026-10-26 新增：reveal_at（揭晓时刻）+ facts（已核实事实）
# 起因：金球奖颁奖礼 20:00 伦敦 = 03:00 北京，节点日只到「天」不够用；
# 且预生成长期不敢碰具体数字 → 节点稿空泛。两条一起补。
# ============================================================

CAL_TIMED = {
    "season": "test",
    "nodes": [
        {"id": "ceremony", "type": "赛季节点", "label": "颁奖礼",
         "date": "2026-10-26", "lead_days": 1, "column": "人物故事", "awareness": "A",
         "reveal_at": "2026-10-27 03:00",
         "template": "奖项要揭晓了，谁更该拿",
         "fact_source_url": "https://example.com/x",
         "facts": ["候选人 A 上赛季 36 球", "候选人 B 随队夺得世界杯"]},
        {"id": "no_facts", "type": "赛季节点", "label": "无事实节点",
         "date": "2026-10-28", "lead_days": 1, "column": "战术榜单", "awareness": "B",
         "template": "这个节点要来了"},
        {"id": "bad_reveal", "type": "赛季节点", "label": "坏时间戳",
         "date": "2026-10-28", "lead_days": 1, "column": "战术榜单", "awareness": "B",
         "reveal_at": "不是时间", "template": "坏时间戳节点"},
    ],
}


class TestRevealAt(unittest.TestCase):
    def test_before_reveal_still_generates(self):
        """揭晓前（10-26 12:00）应正常生成。"""
        topics = ds.build_node_topics(date(2026, 10, 26), horizon_days=7,
                                      calendar=CAL_TIMED,
                                      now=datetime(2026, 10, 26, 12, 0, tzinfo=CST))
        self.assertIn("ceremony", [t["node_id"] for t in topics])

    def test_after_reveal_stops(self):
        """过了揭晓时刻还写「谁该拿」＝旧闻新发，必须停止生成。"""
        dropped = []
        topics = ds.build_node_topics(date(2026, 10, 26), horizon_days=7,
                                      calendar=CAL_TIMED, dropped=dropped,
                                      now=datetime(2026, 10, 27, 3, 1, tzinfo=CST))
        self.assertNotIn("ceremony", [t["node_id"] for t in topics])
        self.assertTrue(any(n.get("id") == "ceremony" and "揭晓" in r for n, r in dropped))

    def test_reveal_boundary_exact(self):
        """恰好等于揭晓时刻即视为已揭晓（边界取 >=，不取 >）。"""
        topics = ds.build_node_topics(date(2026, 10, 26), horizon_days=7,
                                      calendar=CAL_TIMED,
                                      now=datetime(2026, 10, 27, 3, 0, tzinfo=CST))
        self.assertNotIn("ceremony", [t["node_id"] for t in topics])

    def test_malformed_reveal_is_ignored_not_fatal(self):
        """坏的时间戳不能把节点静默吃掉——解析失败按「没有这个字段」处理。"""
        topics = ds.build_node_topics(date(2026, 10, 28), horizon_days=7,
                                      calendar=CAL_TIMED,
                                      now=datetime(2026, 10, 28, 12, 0, tzinfo=CST))
        self.assertIn("bad_reveal", [t["node_id"] for t in topics])

    def test_as_dt(self):
        self.assertIsNone(ds._as_dt(""))
        self.assertIsNone(ds._as_dt("2026/10/26 20:00"))
        self.assertIsNone(ds._as_dt(None))
        self.assertEqual(ds._as_dt("2026-10-27 03:00").hour, 3)
        self.assertEqual(ds._as_dt(datetime(2026, 10, 27, 3, 0)).tzinfo, CST)


class TestFactsPassthrough(unittest.TestCase):
    def test_facts_carried_into_topic(self):
        t = next(x for x in ds.build_node_topics(date(2026, 10, 26), horizon_days=7,
                                                 calendar=CAL_TIMED,
                                                 now=datetime(2026, 10, 26, 12, 0, tzinfo=CST))
                 if x["node_id"] == "ceremony")
        self.assertEqual(len(t["facts"]), 2)
        self.assertEqual(t["reveal_at"], "2026-10-27 03:00")
        self.assertEqual(t["fact_source"]["url"], "https://example.com/x")

    def test_no_facts_gives_empty_list_not_none(self):
        """缺 facts 必须是空列表——None 会让下游 `or []` 与渲染各自为政。"""
        t = next(x for x in ds.build_node_topics(date(2026, 10, 28), horizon_days=7,
                                                 calendar=CAL_TIMED,
                                                 now=datetime(2026, 10, 28, 12, 0, tzinfo=CST))
                 if x["node_id"] == "no_facts")
        self.assertEqual(t["facts"], [])
        self.assertEqual(t["fact_source"]["url"], "")

    def test_facts_dropped_when_blank(self):
        cal = {"nodes": [{"id": "x", "type": "赛季节点", "label": "L", "date": "2026-10-26",
                          "column": "战术榜单", "awareness": "B", "template": "t",
                          "facts": ["  ", "", "有效事实"]}]}
        t = ds.build_node_topics(date(2026, 10, 26), horizon_days=7, calendar=cal,
                                 now=datetime(2026, 10, 26, 12, 0, tzinfo=CST))[0]
        self.assertEqual(t["facts"], ["有效事实"])


class TestRealCalendarNodes(unittest.TestCase):
    """真实日历的硬约束：写进日历的东西必须可信、可用。"""

    def test_ballon_dor_node_shape(self):
        cal = ds.load_calendar()
        n = next((x for x in cal["nodes"] if x.get("id") == "ballon_dor_2026"), None)
        self.assertIsNotNone(n, "金球奖节点应已写入日历")
        # 北京时间揭晓时刻 = 伦敦 20:00 的次日 03:00
        self.assertEqual(n["reveal_at"], "2026-10-27 03:00")
        # 节点日必须早于揭晓时刻，否则 publish_by 会把「赛前稿」放到开奖之后
        self.assertLess(n["date"], n["reveal_at"].split(" ")[0])
        # 人物故事必须有事实支撑，否则就是空泛抒情
        self.assertGreaterEqual(len(n.get("facts") or []), 5)
        self.assertTrue(n.get("fact_source_url", "").startswith("http"))

    def test_every_facts_node_has_source(self):
        """有 facts 就必须有来源链接——没来源的「事实」等于编造。"""
        for n in ds.load_calendar()["nodes"]:
            if n.get("facts"):
                self.assertTrue((n.get("fact_source_url") or "").startswith("http"),
                                f"节点 {n.get('id')} 有 facts 但缺 fact_source_url")

    def test_node_templates_have_no_forward_looking_banned_words(self):
        """节点模板不得用前瞻禁用词（计划 7.1：前瞻体占比为零）。"""
        bad = ("前瞻", "预览", "展望", "赛前预测", "首发预测")
        for n in ds.load_calendar()["nodes"]:
            t = n.get("template") or ""
            for w in bad:
                self.assertNotIn(w, t, f"节点 {n.get('id')} 模板含前瞻禁用词「{w}」")


if __name__ == "__main__":
    unittest.main()
