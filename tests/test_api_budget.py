import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import api_budget

CST = timezone(timedelta(hours=8))

_SPEC = importlib.util.spec_from_file_location(
    "fixture_library",
    str(Path(__file__).resolve().parent.parent / "scripts" / "fixture_library.py"))


def _load_fixture_library():
    mod = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(mod)
    return mod


class _TmpLedger:
    """把台账落盘路径指向临时目录，避免污染真实 data/api_budget.json。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp

    def __enter__(self):
        self._orig = api_budget.LEDGER_PATH
        api_budget.LEDGER_PATH = self.tmp / "api_budget.json"
        api_budget.reset()
        return api_budget

    def __exit__(self, *exc):
        api_budget.LEDGER_PATH = self._orig
        return False


def _fill_daily_budget():
    """把全局日额度用尽（2 次 football-data + 1 次 juhe = 3，计划 11.4 上限）。"""
    api_budget.record("football-data")
    api_budget.record("football-data")
    api_budget.record("juhe")


class TestLedgerCore(unittest.TestCase):
    """计划 11.4：每日额度台账 —— 按源按日计数 + 双闸门 + 落盘。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = api_budget.LEDGER_PATH
        api_budget.LEDGER_PATH = self.tmp / "api_budget.json"
        for k in ("API_DAILY_CAP", "APIB_FOOTBALL_DATA", "APIB_API_FOOTBALL", "APIB_JUHE"):
            os.environ.pop(k, None)

    def tearDown(self):
        api_budget.LEDGER_PATH = self._orig
        for k in ("API_DAILY_CAP", "APIB_FOOTBALL_DATA", "APIB_API_FOOTBALL", "APIB_JUHE"):
            os.environ.pop(k, None)

    def test_grant_then_persist(self):
        r = api_budget.record("football-data", note="窗口 1")
        self.assertTrue(r["ok"])
        self.assertEqual(r["used"], 1)
        self.assertEqual(r["total_used"], 1)
        # 落盘可复读
        self.assertTrue(api_budget.LEDGER_PATH.exists())
        self.assertEqual(api_budget.usage()["football-data"], 1)
        self.assertEqual(api_budget.total_used(), 1)

    def test_source_limit_rejects(self):
        self.assertTrue(api_budget.record("football-data")["ok"])
        self.assertTrue(api_budget.record("football-data")["ok"])
        r = api_budget.record("football-data")
        self.assertFalse(r["ok"])
        self.assertIn("当日额度", r["reason"])
        self.assertEqual(api_budget.usage()["football-data"], 2)   # 拒绝不计数

    def test_global_cap_rejects(self):
        # 计划 11.4：全局 3 次/日
        self.assertTrue(api_budget.record("football-data")["ok"])
        self.assertTrue(api_budget.record("football-data")["ok"])
        self.assertTrue(api_budget.record("juhe")["ok"])
        r = api_budget.record("api-football")
        self.assertFalse(r["ok"])
        self.assertEqual(api_budget.total_used(), 3)
        self.assertIn("全局当日额度", r["reason"])

    def test_unregistered_source_blocked(self):
        r = api_budget.record("unknown-source")
        self.assertFalse(r["ok"])
        self.assertEqual(api_budget.source_limit("unknown-source"), 0)

    def test_env_override_cap(self):
        os.environ["API_DAILY_CAP"] = "5"
        self.assertEqual(api_budget.daily_total_cap(), 5)
        os.environ["APIB_FOOTBALL_DATA"] = "1"
        self.assertEqual(api_budget.source_limit("football-data"), 1)
        self.assertTrue(api_budget.record("football-data")["ok"])
        self.assertFalse(api_budget.record("football-data")["ok"])

    def test_remaining_bounded_by_both_gates(self):
        api_budget.record("football-data")
        self.assertEqual(api_budget.remaining("football-data"), 1)   # 单源剩 1
        api_budget.record("football-data")
        self.assertEqual(api_budget.remaining("football-data"), 0)   # 单源用尽
        self.assertEqual(api_budget.remaining(), 1)                  # 全局还剩 1

    def test_reset_and_prune(self):
        api_budget.record("juhe")
        api_budget.reset()
        self.assertEqual(api_budget.total_used(), 0)
        # 旧日期被裁剪（落盘即裁剪，避免台账无界增长）
        old = (datetime.now(CST).date() - timedelta(days=api_budget.KEEP_DAYS + 5)).isoformat()
        data = api_budget._load()
        data[old] = {"juhe": 1}
        api_budget._save(data)
        self.assertNotIn(old, api_budget._load())
        # 边界：KEEP_DAYS 内的记录保留
        keep = (datetime.now(CST).date() - timedelta(days=api_budget.KEEP_DAYS - 1)).isoformat()
        data = api_budget._load()
        data[keep] = {"juhe": 1}
        api_budget._save(data)
        self.assertIn(keep, api_budget._load())

    def test_summary_and_render(self):
        api_budget.record("juhe")
        rep = api_budget.summary()
        self.assertEqual(rep["total_used"], 1)
        self.assertEqual(rep["total_cap"], api_budget.DEFAULT_DAILY_TOTAL_CAP)
        self.assertTrue(rep["ok"])
        md = api_budget.render(rep)
        self.assertIn("额度台账", md)
        self.assertIn("juhe", md)

    def test_record_never_raises_on_bad_path(self):
        api_budget.LEDGER_PATH = Path("/proc/definitely/not/writable/x.json")
        r = api_budget.record("juhe")          # 落盘失败也必须返回结果
        self.assertTrue(r["ok"])


class TestFixtureWindowChunks(unittest.TestCase):
    """计划 11.4：14 天窗口按免费层 10 天上限分块。"""

    def setUp(self):
        self.mod = _load_fixture_library()

    def test_chunks_cover_14_days(self):
        ch = self.mod._fd_chunks(14)
        self.assertEqual(ch, [(0, 10), (10, 4)])
        self.assertEqual(sum(n for _, n in ch), 14)
        self.assertTrue(all(n <= self.mod.FD_MAX_WINDOW_DAYS for _, n in ch))

    def test_chunks_degenerate(self):
        self.assertEqual(self.mod._fd_chunks(1), [(0, 1)])
        self.assertEqual(self.mod._fd_chunks(10), [(0, 10)])
        self.assertEqual(self.mod._fd_chunks(0), [(0, 1)])
        self.assertEqual(sum(n for _, n in self.mod._fd_chunks(25)), 25)

    def test_target_window_is_14(self):
        self.assertEqual(self.mod.TARGET_WINDOW_DAYS, 14)
        self.assertEqual(self.mod.MAX_WINDOW_DAYS, self.mod.FD_MAX_WINDOW_DAYS)

    def test_supplement_rotation_deterministic(self):
        days = [datetime(2026, 10, 10 + i, tzinfo=CST).date() for i in range(4)]
        picks = [self.mod.todays_supplement(d) for d in days]
        self.assertEqual(len(set(picks)), 4)                      # 4 日不重复
        self.assertEqual(picks[0], self.mod.todays_supplement(days[0]))  # 幂等
        self.assertTrue(all(src in ("api-football", "juhe") for src, _ in picks))


class TestFetchWindowLedger(unittest.TestCase):
    """fetch_window_report：分块请求 + 每块过台账 + 去重合并。"""

    class _Resp:
        def __init__(self, payload, status=200):
            self._p = payload
            self.status_code = status
            self.headers = {"x-requests-available-minute": "9"}

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mod = _load_fixture_library()
        self._orig_dir = self.mod.FIXTURE_DIR
        self.mod.FIXTURE_DIR = self.tmp / "fixtures"
        self.mod.FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        self._orig_ledger = api_budget.LEDGER_PATH
        api_budget.LEDGER_PATH = self.tmp / "api_budget.json"
        os.environ["FOOTBALL_DATA_KEY"] = "test-key"

    def tearDown(self):
        self.mod.FIXTURE_DIR = self._orig_dir
        api_budget.LEDGER_PATH = self._orig_ledger
        os.environ.pop("FOOTBALL_DATA_KEY", None)

    def test_two_chunks_two_ledger_records_and_dedup(self):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=20):
            calls.append(params)
            if len(calls) == 1:
                return self._Resp({"resultSet": {"count": 2}, "matches": [{"id": 1}, {"id": 2}]})
            return self._Resp({"resultSet": {"count": 2}, "matches": [{"id": 2}, {"id": 3}]})

        with mock.patch("requests.get", side_effect=fake_get):
            rep = self.mod.fetch_window_report(14, {"by_en": {}}, refresh=True)

        self.assertEqual(len(calls), 2)                       # 14 天 = 两块
        self.assertEqual(rep["window_days"], 14)
        self.assertEqual(rep["granted_calls"], 2)
        self.assertEqual(rep["refused_calls"], 0)
        self.assertEqual([m["id"] for m in rep["matches"]], [1, 2, 3])   # 去重
        self.assertEqual(api_budget.usage()["football-data"], 2)
        # 缓存落盘（当日 + 补块）
        stamp = datetime.now(CST).strftime("%Y-%m-%d")
        self.assertTrue((self.mod.FIXTURE_DIR / f"raw_{stamp}.json").exists())
        self.assertTrue((self.mod.FIXTURE_DIR / f"raw_{stamp}_c1.json").exists())

    def test_second_run_reads_cache_no_extra_calls(self):
        def fake_get(url, params=None, headers=None, timeout=20):
            ids = [{"id": 1}] if "dateFrom" in (params or {}) else []
            return self._Resp({"resultSet": {"count": len(ids)}, "matches": ids})

        with mock.patch("requests.get", side_effect=fake_get) as g:
            self.mod.fetch_window_report(14, {}, refresh=True)
            n_first = g.call_count
            self.mod.fetch_window_report(14, {}, refresh=False)   # 吃当日缓存
        self.assertEqual(n_first, 2)
        self.assertEqual(api_budget.usage()["football-data"], 2)   # 未新增额度
        self.assertEqual(self.mod.fetch_window_report(14, {}) is not None, True)

    def test_ledger_refusal_degrades_not_block(self):
        # 预先把 football-data 额度用尽 → 分块请求被拒绝，降级不抛异常
        api_budget.record("football-data")
        api_budget.record("football-data")
        with mock.patch("requests.get", side_effect=AssertionError("不应联网")):
            rep = self.mod.fetch_window_report(14, {}, refresh=True)
        self.assertEqual(rep["matches"], [])
        self.assertEqual(rep["window_days"], 0)
        self.assertEqual(rep["refused_calls"], 2)
        self.assertEqual(api_budget.usage()["football-data"], 2)


class TestJuheLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mod = _load_fixture_library()
        self._orig_dir = self.mod.FIXTURE_DIR
        self.mod.FIXTURE_DIR = self.tmp / "fixtures"
        self._orig_ledger = api_budget.LEDGER_PATH
        api_budget.LEDGER_PATH = self.tmp / "api_budget.json"

    def tearDown(self):
        self.mod.FIXTURE_DIR = self._orig_dir
        api_budget.LEDGER_PATH = self._orig_ledger
        os.environ.pop("JUHE_API_KEY", None)

    def test_read_only_mode_never_consumes(self):
        os.environ["JUHE_API_KEY"] = "k"
        out = self.mod.fetch_juhe_fixtures(refresh=False, allow_network=False)
        self.assertEqual(out, [])
        self.assertEqual(api_budget.total_used(), 0)          # 非轮换日 0 消耗

    def test_refused_by_ledger(self):
        os.environ["JUHE_API_KEY"] = "k"
        _fill_daily_budget()
        out = self.mod.fetch_juhe_fixtures(refresh=True, allow_network=True)
        self.assertEqual(out, [])
        self.assertEqual(api_budget.usage().get("juhe", 0), 1)   # 仅此前填充的 1 次
        self.assertEqual(api_budget.total_used(), 3)


class TestApiFootballLedger(unittest.TestCase):
    def setUp(self):
        import api_football as af
        self.af = af
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = af.FIXTURE_DIR
        self._orig_cache = af.LEAGUE_CACHE
        self._orig_ledger = api_budget.LEDGER_PATH
        af.FIXTURE_DIR = self.tmp
        af.LEAGUE_CACHE = self.tmp / "af_leagues.json"
        api_budget.LEDGER_PATH = self.tmp / "api_budget.json"
        os.environ["API_FOOTBALL_KEY"] = "test-key"

    def tearDown(self):
        self.af.FIXTURE_DIR = self._orig_dir
        self.af.LEAGUE_CACHE = self._orig_cache
        api_budget.LEDGER_PATH = self._orig_ledger
        os.environ.pop("API_FOOTBALL_KEY", None)

    def test_refresh_gated_by_ledger(self):
        self.af.LEAGUE_CACHE.write_text(
            json.dumps({"欧联": {"id": 3, "season": 2026}}), encoding="utf-8")
        _fill_daily_budget()
        with mock.patch.object(self.af, "_get", side_effect=AssertionError("不应联网")):
            out = self.af.fetch_league_fixtures("欧联", 14, refresh=True)
        self.assertEqual(out, [])
        self.assertEqual(api_budget.usage().get("api-football", 0), 0)
        self.assertEqual(api_budget.total_used(), 3)          # 未超计划 11.4 上限

    def test_cross_day_cache_is_read_without_call(self):
        self.af.LEAGUE_CACHE.write_text(
            json.dumps({"欧联": {"id": 3, "season": 2026}}), encoding="utf-8")
        old = (datetime.now(CST).date() - timedelta(days=2)).isoformat()
        (self.tmp / f"af_欧联_{old}.json").write_text(
            json.dumps([{"id": 7, "source": "api-football"}]), encoding="utf-8")
        with mock.patch.object(self.af, "_get", side_effect=AssertionError("不应联网")):
            out = self.af.fetch_league_fixtures("欧联", 14, refresh=False)
        self.assertEqual(len(out), 1)
        self.assertEqual(api_budget.total_used(), 0)          # 吃库不消耗额度

    def test_all_cached_merges_latest(self):
        old = (datetime.now(CST).date() - timedelta(days=1)).isoformat()
        (self.tmp / f"af_亚冠_{old}.json").write_text(
            json.dumps([{"id": 9, "source": "api-football"}]), encoding="utf-8")
        out = self.af.fetch_all_cached()
        self.assertEqual([m["id"] for m in out], [9])


class TestVerifyApiBudgetScript(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "verify_api_budget",
            str(Path(__file__).resolve().parent.parent / "scripts" / "verify_api_budget.py"))
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)

    def test_ledger_check_no_over(self):
        c = self.script.check_ledger(3)
        self.assertIn("ok", c)
        self.assertFalse(c.get("advisory"))

    def test_window_check_skips_without_pool(self):
        orig = self.script.POOL_PATH
        self.script.POOL_PATH = Path("/nonexistent/topic_pool.json")
        try:
            c = self.script.check_window(14)
        finally:
            self.script.POOL_PATH = orig
        self.assertTrue(c["skipped"])
        self.assertTrue(c["ok"])

    def test_window_check_passes_on_14_day_pool(self):
        today = datetime.now(CST).date()
        pool = {"window_covered_days": 14, "window_target_days": 14,
                "topics": [{"kickoff_cst": (today + timedelta(days=i)).strftime("%Y-%m-%d 20:00")}
                           for i in range(14)]}
        p = Path(tempfile.mkdtemp()) / "pool.json"
        p.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
        orig = self.script.POOL_PATH
        self.script.POOL_PATH = p
        try:
            c = self.script.check_window(14)
        finally:
            self.script.POOL_PATH = orig
        self.assertFalse(c["skipped"])
        self.assertTrue(c["ok"])
        self.assertEqual(c["topic_span_days"], 14)

    def test_window_check_fails_on_short_pool(self):
        pool = {"window_covered_days": 10, "topics": []}
        p = Path(tempfile.mkdtemp()) / "pool.json"
        p.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
        orig = self.script.POOL_PATH
        self.script.POOL_PATH = p
        try:
            c = self.script.check_window(14)
        finally:
            self.script.POOL_PATH = orig
        self.assertFalse(c["ok"])

    def test_no_direct_calls_is_advisory(self):
        c = self.script.check_no_direct_calls()
        self.assertTrue(c.get("advisory"))


class TestBudgetConfigFile(unittest.TestCase):
    def test_default_config_matches_plan(self):
        cfg = Path(__file__).resolve().parent.parent / "config" / "api_budget.yaml"
        self.assertTrue(cfg.exists())
        import yaml
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["daily_total_cap"], api_budget.DEFAULT_DAILY_TOTAL_CAP)
        total = sum(data["source_limits"].values())
        self.assertLessEqual(api_budget.DEFAULT_DAILY_TOTAL_CAP,
                             total, "全局上限不应大于各源额度之和")
        self.assertEqual(data["source_limits"]["football-data"], 2)   # 14 天须两块


if __name__ == "__main__":
    unittest.main()
