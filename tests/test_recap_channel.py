"""隔夜战报通道（P0-1）单测。

背景：赛程池只收「未开赛」场次（旧闻新发红线），已完赛比分**没有入口**，
08:00 晨读批只能拿蓄水池软性内容补位（10-12~10-25 晨读 14 篇里 12 篇软性、
0 篇战报）。本模块校验新增的「已完赛 → 战报」这一路产出。

反向用例是重点：把**进行中**的比分当最终赛果是重大事实错误，
与 10-25 major_events「进行中比赛不得当最终结果」同款纪律。
"""

import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CST = timezone(timedelta(hours=8))

_SPEC = importlib.util.spec_from_file_location(
    "fixture_library", str(ROOT / "scripts" / "fixture_library.py"))


def _load():
    mod = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(mod)
    return mod


EMAP = {"by_en": {
    "real madrid": {"name_zh": "皇家马德里", "awareness": "A", "aliases": ["皇马", "Real Madrid"]},
    "fc barcelona": {"name_zh": "巴塞罗那", "awareness": "A", "aliases": ["巴萨"]},
    "getafe": {"name_zh": "赫塔菲", "awareness": "B", "aliases": []},
    "elche": {"name_zh": "埃尔切", "awareness": "B", "aliases": []},
}}

NOW = datetime(2026, 10, 26, 8, 0, tzinfo=CST)


def _match(mid=1001, hours_ago=4.0, status="FINISHED", hg=2, ag=1,
           ht=(1, 0), home="Real Madrid", away="FC Barcelona", goals=None):
    utc = (NOW - timedelta(hours=hours_ago)).astimezone(timezone.utc)
    m = {
        "id": mid,
        "utcDate": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "competition": {"id": 2014, "name": "Primera Division"},
        "homeTeam": {"name": home},
        "awayTeam": {"name": away},
        "score": {
            "winner": "HOME" if hg > ag else ("AWAY" if ag > hg else "DRAW"),
            "fullTime": {"home": hg, "away": ag},
            "halfTime": {"home": ht[0] if ht else None, "away": ht[1] if ht else None},
        },
        "goals": goals if goals is not None else [],
    }
    return m


def _goal(minute, scorer, team="Real Madrid", gtype="REGULAR", injury=None):
    return {"minute": minute, "injuryTime": injury, "type": gtype,
            "team": {"name": team}, "scorer": {"name": scorer}}


class TestBuildRecapTopics(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_finished_match_becomes_recap_with_real_score(self):
        m = _match(goals=[_goal(34, "Kylian Mbappé"),
                          _goal(61, "Lamine Yamal", team="FC Barcelona"),
                          _goal(87, "Kylian Mbappé")])
        recaps = self.mod.build_recap_topics([m], EMAP, now=NOW)
        self.assertEqual(len(recaps), 1)
        r = recaps[0]
        self.assertEqual(r["kind"], "recap")
        self.assertEqual(r["source_layer"], "突发")
        self.assertEqual((r["score"]["home"], r["score"]["away"]), (2, 1))
        self.assertEqual((r["score"]["ht_home"], r["score"]["ht_away"]), (1, 0))
        self.assertEqual(r["winner_zh"], "皇家马德里")
        self.assertEqual(r["goal_diff"], 1)
        self.assertTrue(r["late_goal"], "87 分钟进球应判定为关键球")
        self.assertEqual(len(r["goals"]), 3)
        # 球员名走映射（含重音归一化：'Kylian Mbappé' → 'kylian mbappe'）
        self.assertEqual(r["goals"][0]["scorer_zh"], "姆巴佩")
        self.assertTrue(r["goals"][0]["has_zh_name"])

    def test_in_play_match_is_not_a_result(self):
        """进行中的比分不是赛果——哪怕 7-0 也不能当终场。"""
        for status in ("IN_PLAY", "PAUSED", "LIVE", "SUSPENDED", "TIMED", "SCHEDULED"):
            with self.subTest(status=status):
                recaps = self.mod.build_recap_topics(
                    [_match(status=status, hg=7, ag=0)], EMAP, now=NOW)
                self.assertEqual(recaps, [], f"{status} 不得产出战报")

    def test_missing_fulltime_score_is_dropped_not_zero_filled(self):
        """比分缺失 → 丢弃，绝不用 0 代替（缺失标缺，不编造）。"""
        m = _match()
        m["score"]["fullTime"] = {"home": None, "away": None}
        self.assertEqual(self.mod.build_recap_topics([m], EMAP, now=NOW), [])
        m2 = _match()
        m2["score"]["fullTime"] = {"home": 2, "away": None}
        self.assertEqual(self.mod.build_recap_topics([m2], EMAP, now=NOW), [])

    def test_stale_result_outside_lookback_is_dropped(self):
        """超过 30h 的赛果不进战报（旧闻新发）。"""
        old = _match(hours_ago=40.0)
        self.assertEqual(self.mod.build_recap_topics([old], EMAP, now=NOW), [])
        fresh = _match(hours_ago=6.0)
        self.assertEqual(len(self.mod.build_recap_topics([fresh], EMAP, now=NOW)), 1)

    def test_unmapped_team_is_dropped(self):
        m = _match(home="Unknown FC")
        self.assertEqual(self.mod.build_recap_topics([m], EMAP, now=NOW), [])

    def test_unmapped_player_keeps_original_name(self):
        """查不到中文译名就保留原文，不许自行音译。"""
        m = _match(goals=[_goal(12, "Some Newcomer")])
        r = self.mod.build_recap_topics([m], EMAP, now=NOW)[0]
        self.assertEqual(r["goals"][0]["scorer_zh"], "Some Newcomer")
        self.assertFalse(r["goals"][0]["has_zh_name"])

    def test_draw_has_no_winner(self):
        m = _match(hg=1, ag=1, ht=(0, 0))
        r = self.mod.build_recap_topics([m], EMAP, now=NOW)[0]
        self.assertIsNone(r["winner_zh"])
        self.assertIn("打平", r["conflict"])

    def test_publish_by_uses_finish_plus_window(self):
        m = _match(hours_ago=4.0)
        r = self.mod.build_recap_topics([m], EMAP, now=NOW)[0]
        fin = datetime.strptime(r["final_at_cst"], "%Y-%m-%d %H:%M").replace(tzinfo=CST)
        pb = datetime.strptime(r["publish_by"], "%Y-%m-%d %H:%M").replace(tzinfo=CST)
        self.assertEqual((pb - fin).total_seconds() / 3600,
                         self.mod.RECAP_PUBLISH_WINDOW_HOURS)

    def test_a_tier_late_winner_outranks_b_tier_draw(self):
        """A 级对话的绝杀 > B 级球队的平局（认知度 + 戏剧性加权）。"""
        big = _match(mid=1, goals=[_goal(89, "Kylian Mbappé")])
        small = _match(mid=2, home="Getafe", away="Elche", hg=1, ag=1, ht=(1, 1))
        recaps = self.mod.build_recap_topics([small, big], EMAP, now=NOW)
        big["id"], small["id"] = 1, 2
        self.assertEqual(recaps[0]["match_id"], 1, "A 级绝杀应排最前")

    def test_recap_column_is_not_person_story(self):
        """战报归战术榜单——归人物故事正是要治的病（近 14 天占 45.6%）。"""
        r = self.mod.build_recap_topics([_match()], EMAP, now=NOW)[0]
        self.assertEqual(r["板块"], "战术榜单")
        self.assertNotEqual(r["suggested_column"], "人物故事")

    def test_recap_carries_chinese_aliases_only(self):
        """别名随选题下发（下游按标题去重必须别名感知），且只带中文别名。"""
        r = self.mod.build_recap_topics([_match()], EMAP, now=NOW)[0]
        self.assertIn("皇马", r["home_aliases"])
        self.assertNotIn("Real Madrid", r["home_aliases"], "英文别名不参与中文标题匹配")
        self.assertIn("巴萨", r["away_aliases"])


class TestWindowLookback(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_chunks_cover_lookback_day_within_free_tier(self):
        chunks = self.mod._fd_chunks(14, lookback=1)
        self.assertEqual(chunks[0][0], -1, "首块起点应前移 1 天")
        # 每块不得超过免费层 10 天窗口
        for _off, n in chunks:
            self.assertLessEqual(n, self.mod.FD_MAX_WINDOW_DAYS)
        covered = set()
        for off, n in chunks:
            covered.update(range(off, off + n))
        self.assertIn(-1, covered, "必须包含昨天")
        self.assertIn(13, covered, "仍须覆盖第 14 天")

    def test_no_lookback_keeps_old_behavior(self):
        self.assertEqual(self.mod._fd_chunks(14), [(0, 10), (10, 4)])


class TestCollectRecapTopics(unittest.TestCase):
    """筛选取用：时效闸门 + match_id 去重 + 同对阵去重。"""

    def setUp(self):
        import sys
        sys.path.insert(0, str(ROOT))
        import data_collector
        self.dc = data_collector

    def _write_pool(self, tmp, recaps):
        p = Path(tmp) / "topic_pool.json"
        p.write_text(json.dumps({"topics": [], "recaps": recaps}, ensure_ascii=False),
                     encoding="utf-8")
        return p

    def _recap(self, mid=1, pub_by=None, home="皇家马德里", away="巴塞罗那", prio=90.0):
        pub_by = pub_by or (datetime.now(CST) + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M")
        return {"match_id": mid, "home_zh": home, "away_zh": away,
                "home_aliases": ["皇马"] if home == "皇家马德里" else [],
                "away_aliases": ["巴萨"] if away == "巴塞罗那" else [],
                "competition": "西甲", "priority": prio, "publish_by": pub_by,
                "score": {"home": 2, "away": 1}, "goals": [], "winner_zh": home,
                "goal_diff": 1, "late_goal": True}

    def test_picks_freshest_recap(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_pool(tmp, [self._recap(1, prio=50), self._recap(2, prio=95)])
            old = self.dc.FIXTURE_POOL_PATH
            self.dc.FIXTURE_POOL_PATH = p
            try:
                got = self.dc.collect_recap_topics("2026-10-26", max_topics=1)
            finally:
                self.dc.FIXTURE_POOL_PATH = old
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["match_id"], 2)

    def test_expired_recap_is_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            past = (datetime.now(CST) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
            p = self._write_pool(tmp, [self._recap(1, pub_by=past)])
            old = self.dc.FIXTURE_POOL_PATH
            self.dc.FIXTURE_POOL_PATH = p
            try:
                self.assertEqual(self.dc.collect_recap_topics("2026-10-26"), [])
            finally:
                self.dc.FIXTURE_POOL_PATH = old

    def test_match_id_dedup_blocks_repost(self):
        """同一场次不得写两遍（赛前稿 + 赛后战报是两篇，但战报只发一次）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_pool(tmp, [self._recap(7)])
            old = self.dc.FIXTURE_POOL_PATH
            self.dc.FIXTURE_POOL_PATH = p
            try:
                self.assertEqual(
                    self.dc.collect_recap_topics("2026-10-26", sent_match_ids={"7"}), [])
                self.assertEqual(
                    len(self.dc.collect_recap_topics("2026-10-26", sent_match_ids={"8"})), 1)
            finally:
                self.dc.FIXTURE_POOL_PATH = old

    def test_team_dedup_does_not_kill_recap(self):
        """反例保护：**不能**用「近 7 天写过这两队」去重战报。

        昨天刚做过赛前前瞻的德比，恰恰是今天最值钱的战报。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_pool(tmp, [self._recap(9)])
            old = self.dc.FIXTURE_POOL_PATH
            self.dc.FIXTURE_POOL_PATH = p
            try:
                got = self.dc.collect_recap_topics(
                    "2026-10-26", covered_titles=set())
            finally:
                self.dc.FIXTURE_POOL_PATH = old
            self.assertEqual(len(got), 1, "球队历史不该拦战报")

    def test_same_fixture_title_dedup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_pool(tmp, [self._recap(11)])
            old = self.dc.FIXTURE_POOL_PATH
            self.dc.FIXTURE_POOL_PATH = p
            try:
                got = self.dc.collect_recap_topics(
                    "2026-10-26",
                    covered_titles={"皇马2-1巴萨：姆巴佩87分钟绝杀？"})
            finally:
                self.dc.FIXTURE_POOL_PATH = old
            self.assertEqual(got, [], "同对阵已有战报标题应跳过")

    def test_missing_pool_is_not_fatal(self):
        old = self.dc.FIXTURE_POOL_PATH
        self.dc.FIXTURE_POOL_PATH = Path("/nonexistent/topic_pool.json")
        try:
            self.assertEqual(self.dc.collect_recap_topics("2026-10-26"), [])
        finally:
            self.dc.FIXTURE_POOL_PATH = old


if __name__ == "__main__":
    unittest.main()
