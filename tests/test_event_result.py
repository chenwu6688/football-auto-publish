"""揭晓型事件「结果稿」多源交叉验证单测（event_result + 结果期选题）。

## 为什么这块要用测试钉死

结果稿的事故形态只有一种，但极其致命：**把没揭晓的事写成已经揭晓**。
2026-10-26 10:28 懂球帝首页就摆着一条《【CBS Sports】姆巴佩夺金球》，
措辞完全是完成态，而真正的开奖在 10-27 03:00 —— 只凭它写稿就是把外媒预测当结论。

所以本测试的重点全在**反向用例**：
  - 揭晓前的预热度（哪怕措辞像结论）必须拒；
  - 预测/提名/悬念类标题必须拒；
  - 只有 1 个源必须拒（可能只是互相转载）；
  - 两个源说法不一致必须拒（事实冲突）；
  - 时间不明必须拒（不猜发布时间）。
正向只留几条，用来确认门槛不是「一律不发」。
"""

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CST = timezone(timedelta(hours=8))

_spec = importlib.util.spec_from_file_location("event_result", str(ROOT / "event_result.py"))
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)

import sys
sys.path.insert(0, str(ROOT))
import deterministic_supply as ds  # noqa: E402

# 金球奖 2026：伦敦 20:00 = 北京 10-27 03:00
REVEAL = datetime(2026, 10, 27, 3, 0, tzinfo=CST)
KW = ["金球奖", "金球"]


def _m(source, title, pub="2026-10-27 05:10:00", url="", text=""):
    return {"source": source, "title": title, "published_at": pub, "url": url, "text": text}


class TestTimeGate(unittest.TestCase):
    """时间闸门是第一道也是最硬的一道：赛前稿全部死在揭晓之前。"""

    def test_real_trap_headline_rejected_before_reveal(self):
        """真实陷阱：10-26 10:28《【CBS Sports】姆巴佩夺金球》措辞是完成态。

        但它发在揭晓前 16 小时 —— 只是外媒预测。这条必须被时间闸门挡掉，
        否则整条链路会把姆巴佩写成 2026 金球奖得主。
        """
        res = er.verify([
            _m("dongqiudi", "【CBS Sports】姆巴佩夺金球", pub="2026-10-26 10:28:14"),
            _m("zhibo8", "姆巴佩夺金球", pub="2026-10-26 11:00:00"),
        ], keywords=KW, reveal_at=REVEAL)
        self.assertFalse(res["ok"], f"揭晓前的预测稿不得当结果，却通过了：{res['winner']}")
        self.assertEqual(res["winner"], "")
        reasons = " ".join(d["reason"] for d in res["dropped"])
        self.assertIn("早于揭晓", reasons)

    def test_headline_after_reveal_accepted(self):
        """同一条标题、只把时间挪到揭晓后 → 应通过（证明拒的是时间不是措辞）。"""
        res = er.verify([
            _m("dongqiudi", "姆巴佩夺金球", pub="2026-10-27 05:10:00"),
            _m("zhibo8", "官方：姆巴佩荣膺2026金球奖", pub="2026-10-27 05:20:00"),
        ], keywords=KW, reveal_at=REVEAL)
        self.assertTrue(res["ok"])
        self.assertEqual(res["winner"], "姆巴佩")

    def test_url_date_used_when_no_time(self):
        """直播吧列表不给时间，只给 URL 日期；日期早于揭晓日 → 拒。"""
        res = er.verify([
            _m("zhibo8", "登贝莱获得2026年金球奖", pub="",
               url="https://news.zhibo8.com/zuqiu/2026-10-26/abc.htm"),
            _m("sina", "登贝莱当选2026金球奖", pub="", url="https://x/2026-10-26/1"),
        ], keywords=KW, reveal_at=REVEAL)
        self.assertFalse(res["ok"])

    def test_unknown_time_rejected_by_default(self):
        """时间完全拿不到 → 拒（不猜「大概刚发的」）。"""
        res = er.verify([
            _m("zhibo8", "登贝莱夺金球", pub=""),
            _m("dongqiudi", "登贝莱夺金球", pub=""),
        ], keywords=KW, reveal_at=REVEAL)
        self.assertFalse(res["ok"])
        self.assertIn("时间未知", " ".join(d["reason"] for d in res["dropped"]))

    def test_unknown_time_can_be_explicitly_allowed(self):
        """显式放行时间未知的条目（留给「源改版后时间字段全失」的应急开关）。"""
        res = er.verify([
            _m("zhibo8", "登贝莱夺金球", pub=""),
            _m("dongqiudi", "登贝莱夺金球", pub=""),
        ], keywords=KW, reveal_at=REVEAL, published_unknown_ok=True)
        self.assertTrue(res["ok"])


class TestRumorRejection(unittest.TestCase):
    """预测/提名/悬念：措辞再像也不能当结果。"""

    def test_prediction_markers_rejected(self):
        for title in (
            "明晨新王加冕？历届金球奖得主：梅西8次C罗5次",
            "2026金球奖前瞻：登贝莱与亚马尔谁更有戏",
            "金球奖提名名单公布，登贝莱领衔",
            "金球奖得主预计明日揭晓",
            "名帅：他将来能拿金球奖",
        ):
            with self.subTest(title=title):
                ok, why = er.is_result_sentence(title)
                self.assertFalse(ok, f"预测类标题被当结果句：{title}")
                self.assertTrue(why)

    def test_done_verbs_accepted(self):
        for title in (
            "登贝莱获得2026年金球奖",
            "官方：登贝莱荣膺2026金球奖",
            "2026金球奖揭晓：登贝莱当选",
            "金球奖得主登贝莱：这是我职业生涯最好的一年",
            "登贝莱首夺金球奖",
        ):
            with self.subTest(title=title):
                ok, _ = er.is_result_sentence(title)
                self.assertTrue(ok, f"完成态标题被误判：{title}")


class TestNameExtraction(unittest.TestCase):
    """姓名抽取：错了就是写错人，比不发严重得多。"""

    def test_extracts_across_common_phrasings(self):
        cases = {
            "登贝莱获得2026年金球奖，首次捧杯": "登贝莱",
            "官方：登贝莱荣膺2026金球奖": "登贝莱",
            "2026金球奖揭晓：登贝莱当选": "登贝莱",
            "金球奖得主登贝莱：最好的一年": "登贝莱",
            "登贝莱首夺金球奖 成为法国第6位得主": "登贝莱",
            "登贝莱夺金球": "登贝莱",
            "【CBS Sports】姆巴佩夺金球": "姆巴佩",
            "队报：登贝莱荣获2026年金球奖": "登贝莱",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(er.extract_winner(text), want)

    def test_no_name_extracted_from_comparison_sentence(self):
        """「凯恩力压登贝莱折桂」没有词边界，宁可不抽也不抽错。"""
        self.assertIsNone(er.extract_winner("2026金球奖：凯恩力压登贝莱折桂"))

    def test_ordinal_suffix_stripped(self):
        """「登贝莱首」是多吞了「首」字，必须剥掉。"""
        self.assertEqual(er.extract_winner("登贝莱首夺金球奖"), "登贝莱")

    def test_pronoun_not_treated_as_name(self):
        self.assertIsNone(er.extract_winner("名帅：他将来能拿金球奖"))

    def test_normalize_handles_middle_dot(self):
        self.assertEqual(er.normalize_name("奥斯曼·登贝莱"), er.normalize_name("奥斯曼登贝莱"))


class TestMultiSource(unittest.TestCase):
    """多源一致：这是把「单条消息」变成「可写事实」的唯一关口。"""

    def test_two_sources_same_name_passes(self):
        res = er.verify([
            _m("zhibo8", "官方：登贝莱荣膺2026金球奖"),
            _m("dongqiudi", "2026金球奖揭晓：登贝莱当选"),
        ], keywords=KW, reveal_at=REVEAL)
        self.assertTrue(res["ok"])
        self.assertEqual(res["winner"], "登贝莱")
        self.assertEqual(sorted(res["sources"]), ["dongqiudi", "zhibo8"])

    def test_single_source_rejected(self):
        """单源不发：可能只是一家转载另一家，也可能就是错的。"""
        res = er.verify([_m("zhibo8", "官方：登贝莱荣膺2026金球奖")],
                        keywords=KW, reveal_at=REVEAL)
        self.assertFalse(res["ok"])
        self.assertIn("独立源不足", res["reason"])

    def test_same_source_twice_still_single(self):
        """同一个站点的两条不能算两个源 —— 这是最容易自欺的一种「交叉验证」。"""
        res = er.verify([
            _m("zhibo8", "登贝莱获得2026年金球奖"),
            _m("zhibo8", "登贝莱夺金球"),
        ], keywords=KW, reveal_at=REVEAL)
        self.assertFalse(res["ok"])

    def test_conflicting_names_rejected(self):
        """两家说登贝莱、另两家说亚马尔 → 事实冲突，一篇都不发。"""
        res = er.verify([
            _m("zhibo8", "官方：登贝莱荣膺2026金球奖"),
            _m("dongqiudi", "登贝莱当选2026金球奖"),
            _m("sina", "2026金球奖揭晓：亚马尔当选"),
            _m("netease", "金球奖得主亚马尔"),
        ], keywords=KW, reveal_at=REVEAL)
        self.assertFalse(res["ok"])
        self.assertIn("不一致", res["reason"])

    def test_noisy_single_source_does_not_block_valid_winner(self):
        """1 个源的杂音不该把 2 个源确认的结果判成冲突（避免误杀）。"""
        res = er.verify([
            _m("zhibo8", "官方：登贝莱荣膺2026金球奖"),
            _m("dongqiudi", "登贝莱当选2026金球奖"),
            _m("sina", "2026金球奖揭晓：亚马尔当选"),
        ], keywords=KW, reveal_at=REVEAL)
        self.assertTrue(res["ok"])
        self.assertEqual(res["winner"], "登贝莱")

    def test_keyword_miss_returns_clear_reason(self):
        res = er.verify([_m("zhibo8", "曼联0-3伯恩茅斯")], keywords=KW, reveal_at=REVEAL)
        self.assertFalse(res["ok"])
        self.assertIn("未抓到含关键词", res["reason"])


class TestResultTopics(unittest.TestCase):
    """节点从「赛前稿」切到「结果稿」的时刻必须精确。"""

    def _ids(self, now):
        return [t["node_id"] for t in ds.result_topics(now=now)]

    def test_before_reveal_only_preview_channel(self):
        now = datetime(2026, 10, 26, 10, 0, tzinfo=CST)
        self.assertEqual(self._ids(now), [])
        pre = ds.build_node_topics(today=now.date(), horizon_days=7, now=now)
        self.assertIn("ballon_dor_2026", [t["node_id"] for t in pre])

    def test_after_reveal_switches_to_result(self):
        now = datetime(2026, 10, 27, 8, 30, tzinfo=CST)
        self.assertIn("ballon_dor_2026", self._ids(now))
        # 同刻赛前稿必须已经消失（不能再写「谁该拿」）
        pre = ds.build_node_topics(today=now.date(), horizon_days=7, now=now)
        self.assertNotIn("ballon_dor_2026", [t["node_id"] for t in pre])

    def test_result_window_closes(self):
        """开奖 12h 后关窗：晚间批再说「今晨谁拿奖」就是旧闻。"""
        now = datetime(2026, 10, 27, 20, 0, tzinfo=CST)
        self.assertEqual(self._ids(now), [])

    def test_result_topic_carries_keywords_and_facts(self):
        now = datetime(2026, 10, 27, 8, 30, tzinfo=CST)
        t = [x for x in ds.result_topics(now=now) if x["node_id"] == "ballon_dor_2026"][0]
        self.assertEqual(t["phase"], "result")
        self.assertTrue(t["result_keywords"])
        self.assertGreaterEqual(t["min_sources"], 2)
        # 背景数据必须带出来，否则结果稿只能写「XX 获奖了」
        self.assertTrue(t["facts"])
        self.assertEqual(t["板块"], "人物故事")


class TestFactCard(unittest.TestCase):
    def test_card_separates_verified_from_background(self):
        import orchestrator  # noqa: F401  延迟导入，避免拖慢无关用例
        topic = {"node_label": "金球奖颁奖礼", "reveal_at": "2026-10-27 03:00",
                 "facts": ["哈里·凯恩上赛季打进 61 球"]}
        verdict = {"ok": True, "winner": "登贝莱",
                   "kept": [{"source": "zhibo8", "title": "官方：登贝莱荣膺2026金球奖",
                             "url": "u1", "published_at": "2026-10-27 05:10"}]}
        card = orchestrator._build_event_result_fact_card(topic, verdict)
        self.assertIn("已揭晓结果", card)
        self.assertIn("登贝莱", card)
        self.assertIn("背景数据", card)
        self.assertIn("61 球", card)
        # 得票数这类最容易编的东西必须显式禁止
        self.assertIn("得票数", card)


class TestResultArticleGuards(unittest.TestCase):
    """生成侧硬校验：不点名获奖者的稿子一律丢弃。"""

    def test_returns_none_without_verdict(self):
        import orchestrator
        self.assertIsNone(orchestrator.generate_event_result_article(
            {"node_id": "x"}, {"ok": False, "winner": ""}))

    def test_rejects_article_not_naming_winner(self):
        import orchestrator
        from unittest import mock
        topic = {"node_id": "ballon_dor_2026", "板块": "人物故事", "node_label": "金球奖"}
        verdict = {"ok": True, "winner": "登贝莱", "kept": []}
        bad = {"title": "2026金球奖终于揭晓，这个结果谁想到了？",
               "content": "某人拿奖了。" * 60,
               "interaction_type": "站队式", "interaction_bait": "你服吗"}
        with mock.patch.object(orchestrator, "call_llm_json", return_value=(bad, "m")):
            self.assertIsNone(orchestrator.generate_event_result_article(topic, verdict))


if __name__ == "__main__":
    unittest.main()


class TestAddEventResultArticles(unittest.TestCase):
    """poc 内「取用结果稿」分支的集成行为。

    这段是全链路唯一由机器自己认定事实的地方（谁获奖是从多家标题里对出来的），
    所以除了「通过时能发」，更要钉死「不通过时一篇都不许进 articles」。
    """

    def _run(self, *, verdicts, result_topics, sent=None, det_cap=2, added=0,
             generated=None):
        import orchestrator
        from unittest import mock
        articles, topics, images_map, stats = [], [], {}, {"generated": 0, "valid": 0}
        arts = generated if generated is not None else [
            {"title": "他终于在第七次提名后拿到这座奖杯，凭什么？",
             "content": "x" * 300, "content_type": "人物故事",
             "_event_result_key": f"ballon_dor_2026|{v.get('winner', '')}",
             "_event_winner": v.get("winner", "")}
            for v in verdicts]
        with mock.patch.object(orchestrator, "search_images", return_value=["i"]), \
             mock.patch.object(orchestrator, "_event_result_sent", return_value=set(sent or [])), \
             mock.patch.object(orchestrator, "generate_event_result_article",
                               side_effect=arts), \
             mock.patch("deterministic_supply.result_topics", return_value=result_topics), \
             mock.patch("event_result.resolve", side_effect=verdicts):
            n = orchestrator._add_event_result_articles(
                articles=articles, topics=topics, images_map=images_map, stats=stats,
                added=added, det_cap=det_cap, date_str="2026-10-27", batch_mode="morning",
                topic_history={}, avoid_sigs=set())
        return n, articles, topics, images_map, stats

    TOPIC = {"node_id": "ballon_dor_2026", "node_label": "金球奖颁奖礼",
             "板块": "人物故事", "min_sources": 2, "reveal_at": "2026-10-27 03:00"}

    def test_verified_result_is_appended(self):
        n, arts, topics, imgs, stats = self._run(
            verdicts=[{"ok": True, "winner": "登贝莱", "sources": ["zhibo8", "dongqiudi"],
                       "kept": []}],
            result_topics=[self.TOPIC])
        self.assertEqual(n, 1)
        self.assertEqual(len(arts), 1)
        self.assertEqual(stats["generated"], 1)
        self.assertIn(0, imgs)
        self.assertEqual(topics[0]["_supply_layer"], "一级·确定性·事件结果")

    def test_unverified_result_never_appended(self):
        """取证没过（外媒预测/单源/冲突）→ 一篇都不许进批次。"""
        n, arts, topics, imgs, _ = self._run(
            verdicts=[{"ok": False, "winner": "", "sources": [],
                       "reason": "独立源不足 2 个"}],
            result_topics=[self.TOPIC])
        self.assertEqual(n, 0)
        self.assertEqual(arts, [])
        self.assertEqual(topics, [])
        self.assertEqual(imgs, {})

    def test_already_sent_is_skipped(self):
        """同一场颁奖不能写两遍（跨批/跨天的去重靠 event_result_key 前缀）。"""
        n, arts, _, _, _ = self._run(
            verdicts=[],
            result_topics=[self.TOPIC],
            sent=["ballon_dor_2026|登贝莱"])
        self.assertEqual(n, 0)
        self.assertEqual(arts, [])

    def test_quota_full_blocks(self):
        """配额已满（det_cap=0）→ 不去抓也不去生成（省额度）。"""
        n, arts, _, _, _ = self._run(verdicts=[], result_topics=[self.TOPIC], det_cap=0)
        self.assertEqual(n, 0)

    def test_respects_remaining_quota(self):
        """det_cap 只剩 1 席 → 即便两场事件都取证通过也只发 1 篇。"""
        t2 = dict(self.TOPIC, node_id="other_node", node_label="另一场揭晓")
        n, arts, _, _, _ = self._run(
            verdicts=[{"ok": True, "winner": "登贝莱", "sources": ["a", "b"], "kept": []},
                      {"ok": True, "winner": "亚马尔", "sources": ["a", "b"], "kept": []}],
            result_topics=[self.TOPIC, t2], det_cap=1)
        self.assertEqual(n, 1)
        self.assertEqual(len(arts), 1)
