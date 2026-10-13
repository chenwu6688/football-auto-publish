import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import redfox_client as rc

CST = ZoneInfo("Asia/Shanghai")

DY_ITEM = {"workId": "7388", "title": "梅西倒钩太漂亮了", "workUrl": "https://v/1",
           "publishTime": "2026-10-13 10:00:00", "accountName": "足球哥",
           "followerCount": "6668", "likeCount": 100, "commentCount": 20,
           "shareCount": 5, "collectCount": 7, "crawlTime": "2026-10-13 12:00:00",
           "commentTopKeywords": ["梅西", "倒钩"]}

GZH_ITEM = {"workUuid": "U1", "title": "国足新大名单公布", "workUrl": "https://mp/1",
            "publishTime": "2026-10-13 09:00:00", "author": "体坛周刊",
            "readCount": 50000, "likeCount": 300, "watchCount": 100,
            "syncTime": "2026-10-13 12:00:00"}


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class TestNormalize(unittest.TestCase):
    """归一化口径：engagement 计算、缺字段不冒充、平台识别。"""

    def test_douyin_engagement_sum(self):
        rec = rc.normalize_work(DY_ITEM, "douyin")
        self.assertEqual(rec["engagement"], 125)            # 100+20+5
        self.assertEqual(rec["follower_count"], 6668)       # 字符串转 int
        self.assertEqual(rec["raw"]["collect"], 7)          # 原值保留
        self.assertEqual(rec["platform"], "douyin")

    def test_gzh_engagement_sum(self):
        rec = rc.normalize_work(GZH_ITEM, "gzh")
        self.assertEqual(rec["engagement"], 50400)          # 50000+300+100
        self.assertIsNone(rec["follower_count"])            # 缺就是缺，不造 0

    def test_missing_title_returns_none(self):
        self.assertIsNone(rc.normalize_work({"workId": "1"}, "douyin"))

    def test_unknown_platform_returns_none(self):
        self.assertIsNone(rc.normalize_work(DY_ITEM, "weibo"))

    def test_null_counts_become_none(self):
        item = dict(DY_ITEM, likeCount=None, commentCount="", shareCount="x")
        rec = rc.normalize_work(item, "douyin")
        self.assertIsNone(rec["raw"]["like"])
        self.assertIsNone(rec["raw"]["comment"])
        self.assertIsNone(rec["raw"]["share"])
        self.assertEqual(rec["engagement"], 0)              # 全缺=0 互动，但字段为 None


class TestSearchDegrade(unittest.TestCase):
    """降级路径：无 Key / 网络异常 / 非 2000 / 超日上限 / 配置缺失 → 一律 []。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = {"base_url": "https://x", "timeout": 5, "sort": "_4",
                    "per_keyword_limit": 5, "cache_ttl_hours": 12, "daily_call_cap": 30,
                    "endpoints": {"douyin_works": "/e"},
                    "endpoint_enabled": {"douyin_works": True},
                    "keywords": ["梅西"]}

    def _search(self, **kw):
        kw.setdefault("cache_path", self.tmp / "c.json")
        kw.setdefault("budget_path", self.tmp / "b.json")
        return rc.search_works("梅西", "douyin", cfg=self.cfg, **kw)

    def test_no_key_returns_empty(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(rc.ENV_KEY, None)
            with mock.patch("requests.post") as m:
                self.assertEqual(self._search(), [])
                m.assert_not_called()                       # 没 Key 连请求都不发

    def test_network_error_returns_empty(self):
        os.environ[rc.ENV_KEY] = "k"
        try:
            with mock.patch("requests.post", side_effect=RuntimeError("断网")):
                self.assertEqual(self._search(), [])
        finally:
            os.environ.pop(rc.ENV_KEY, None)

    def test_non_2000_returns_empty(self):
        os.environ[rc.ENV_KEY] = "k"
        try:
            with mock.patch("requests.post", return_value=_Resp({"code": 3201, "msg": "积分不足"})):
                self.assertEqual(self._search(), [])
        finally:
            os.environ.pop(rc.ENV_KEY, None)

    def test_daily_cap_blocks_call(self):
        os.environ[rc.ENV_KEY] = "k"
        b = self.tmp / "b.json"
        b.write_text(json.dumps({datetime.now(CST).strftime("%Y-%m-%d"): 999}),
                     encoding="utf-8")
        try:
            with mock.patch("requests.post") as m:
                self.assertEqual(self._search(budget_path=b), [])
                m.assert_not_called()                       # 硬闸：一次都不多发
        finally:
            os.environ.pop(rc.ENV_KEY, None)

    def test_endpoint_disabled_returns_empty(self):
        cfg = dict(self.cfg, endpoint_enabled={"douyin_works": False})
        os.environ[rc.ENV_KEY] = "k"
        try:
            with mock.patch("requests.post") as m:
                self.assertEqual(rc.search_works("梅西", "douyin", cfg=cfg,
                                                 cache_path=self.tmp / "c.json",
                                                 budget_path=self.tmp / "b.json"), [])
                m.assert_not_called()
        finally:
            os.environ.pop(rc.ENV_KEY, None)

    def test_config_missing_returns_empty(self):
        self.assertEqual(rc.search_works("梅西", "douyin", cfg=None,
                                         cache_path=self.tmp / "c.json"), [])


class TestCacheAndBudget(unittest.TestCase):
    """缓存 TTL 与台账记账：命中不调不计费；过期重调；台账只记真实调用。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = {"base_url": "https://x", "timeout": 5, "sort": "_4",
                    "per_keyword_limit": 5, "cache_ttl_hours": 12, "daily_call_cap": 30,
                    "endpoints": {"douyin_works": "/e"},
                    "endpoint_enabled": {"douyin_works": True},
                    "keywords": ["梅西"]}
        self.cpath = self.tmp / "cache.json"
        self.bpath = self.tmp / "budget.json"
        self.calls = {"n": 0}

        def _fake(url, headers=None, json=None, timeout=None):
            self.calls["n"] += 1
            return _Resp({"code": 2000, "data": {"list": [DY_ITEM]}})

        self._fake = _fake
        os.environ[rc.ENV_KEY] = "k"

    def tearDown(self):
        os.environ.pop(rc.ENV_KEY, None)

    def _search(self, keyword="梅西"):
        return rc.search_works(keyword, "douyin", cfg=self.cfg,
                               cache_path=self.cpath, budget_path=self.bpath)

    def test_cache_hit_avoids_call_and_charge(self):
        with mock.patch("requests.post", side_effect=self._fake):
            r1 = self._search()
            r2 = self._search()
        self.assertEqual(len(r1), 1)
        self.assertEqual(len(r2), 1)
        self.assertEqual(self.calls["n"], 1)                # TTL 内只调 1 次
        self.assertEqual(rc.calls_today(self.bpath), 1)     # 台账只记 1 次

    def test_expired_cache_refetches(self):
        with mock.patch("requests.post", side_effect=self._fake):
            self._search()
            # 把缓存时间改到 13 小时前（超 TTL）
            cache = json.loads(self.cpath.read_text(encoding="utf-8"))
            old = (datetime.now(CST).replace(microsecond=0)
                   - __import__("datetime").timedelta(hours=13))
            cache["douyin:梅西"]["fetched_at"] = old.strftime("%Y-%m-%d %H:%M:%S")
            self.cpath.write_text(json.dumps(cache), encoding="utf-8")
            self._search()
        self.assertEqual(self.calls["n"], 2)                # 过期重调
        self.assertEqual(rc.calls_today(self.bpath), 2)

    def test_budget_prunes_old_days(self):
        rc._record_call(self.bpath)
        rc._record_call(self.bpath)
        budget = json.loads(self.bpath.read_text(encoding="utf-8"))
        self.assertEqual(budget[datetime.now(CST).strftime("%Y-%m-%d")], 2)


class TestSignalsPayload(unittest.TestCase):
    """信号表构造：只含命中词、求和正确、结构完整。"""

    def test_entity_heat_only_hits(self):
        recs = [rc.normalize_work(DY_ITEM, "douyin"),
                rc.normalize_work(GZH_ITEM, "gzh")]
        eh = rc.entity_heat(recs, ["梅西", "国足", "热狗"])
        self.assertEqual(set(eh), {"梅西", "国足"})          # 无命中不出现
        self.assertEqual(eh["梅西"]["社媒讨论量"], 125)
        self.assertEqual(eh["国足"]["作品数"], 1)

    def test_payload_structure(self):
        recs = [rc.normalize_work(DY_ITEM, "douyin"),
                rc.normalize_work(GZH_ITEM, "gzh")]
        p = rc.build_signals_payload(recs, ["梅西", "国足"])
        self.assertEqual(p["record_count"], 2)
        self.assertEqual(p["works_top"][0]["title"], "国足新大名单公布")  # 互动降序
        self.assertTrue(any(w["word"] == "梅西" for w in p["hot_words"]))

    def test_write_and_read_roundtrip(self):
        tmp = Path(tempfile.mkdtemp()) / "rf.json"
        recs = [rc.normalize_work(DY_ITEM, "douyin")]
        rc.write_signals(rc.build_signals_payload(recs, ["梅西"]), tmp)
        import signal_sources as ss
        self.assertEqual(ss.load_redfox(tmp), {"梅西": {"社媒讨论量": 125}})


class TestSelftest(unittest.TestCase):
    def test_selftest_all_pass(self):
        bad = [n for n, ok in rc.selftest() if not ok]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
