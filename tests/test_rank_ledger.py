"""L3 打分留证与跨批主键（ranker）。

两个 bug 一起回测：
  1. **跨批串号**：缓存键曾是 `topic-{i}`（批内序号），同一天第二批的 topic-0
     会命中第一批 topic-0 的 24h 缓存 → 复用别的选题的分，且「命中缓存不花
     token」，日志上看不出来。
  2. **分数不留证**：缓存按日重置（date 变了就 return 空 judged），L3 分数每天
     被覆盖丢弃 → 周审计永远只能退回规则分，一份「规则降级抽样」被当成
     「模型量表校准」存档（2026-10-10 那份维度均值 1.0/1.1 就是这么来的）。
"""

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import ranker

_CST = ZoneInfo("Asia/Shanghai")


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "weekly_rank_audit",
        str(Path(__file__).resolve().parent.parent / "scripts" / "weekly_rank_audit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _today() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_cache = ranker.CACHE_PATH
        ranker.CACHE_PATH = self.tmp / "ranker_cache.json"
        ranker._awareness_cache = {}

    def tearDown(self):
        ranker.CACHE_PATH = self._orig_cache
        ranker._awareness_cache = None


class TestStableKey(_Base):
    def test_batch_index_is_never_the_key(self):
        k1 = ranker.stable_topic_key({"id": "topic-0", "主体": "曼城", "动作": "热点球评",
                                      "发生时间": "2026-11-03"})
        k2 = ranker.stable_topic_key({"id": "topic-0", "主体": "皇马", "动作": "热点球评",
                                      "发生时间": "2026-11-03"})
        self.assertNotEqual(k1, k2, "不同主体共用 topic-0 时主键必须不同")
        self.assertTrue(k1.startswith("k_"))

    def test_same_topic_same_key(self):
        card = {"id": "topic-3", "主体": "曼城", "动作": "热点球评", "发生时间": "2026-11-03"}
        self.assertEqual(ranker.stable_topic_key(card), ranker.stable_topic_key(dict(card)))

    def test_explicit_id_wins(self):
        self.assertEqual(ranker.stable_topic_key({"fact_id": "F-123", "主体": "曼城"}),
                         "F-123")
        self.assertEqual(ranker.stable_topic_key({"id": "slug-abc"}), "slug-abc")


class TestNoCrossBatchReuse(_Base):
    def _cands(self, subject):
        return [{"id": "topic-0",
                 "card": {"id": "topic-0", "主体": subject, "动作": "热点球评",
                          "发生时间": _today()},
                 "板块": "热点球评", "title": subject}]

    def _rank(self, subject, calls):
        def fake(messages):
            calls.append(subject)
            return {"scores": [{"id": "topic-0", "冲突度": 4, "人物知名度": 4,
                                "时效性": 4, "话题延展性": 4, "粉丝画像匹配度": 4}]}, "m"
        return ranker.rank_candidates(self._cands(subject), use_llm=True,
                                      call_llm=fake, now=datetime.now(_CST))

    def test_second_batch_does_not_eat_first_batch_cache(self):
        calls = []
        with mock.patch.object(ranker, "awareness_of", return_value="A"):
            self._rank("曼城", calls)
            self._rank("皇马", calls)
        self.assertEqual(len(calls), 2, "两批不同选题必须各打一次分，不得共用缓存")

    def test_same_topic_hits_cache(self):
        calls = []
        with mock.patch.object(ranker, "awareness_of", return_value="A"):
            self._rank("曼城", calls)
            self._rank("曼城", calls)
        self.assertEqual(len(calls), 1, "同一选题 24h 内应命中缓存")


class TestLedger(_Base):
    def test_ledger_appends_not_overwrites(self):
        for i in range(3):
            ranker.append_l3_ledger([{"at": f"{_today()} 0{i}:00:00", "key": f"k{i}",
                                      "维度": {"冲突度": 3}, "总分": 3}])
        lines = ranker.ledger_path().read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 3)

    def test_rank_scores_land_in_ledger(self):
        def fake(messages):
            return {"scores": [{"id": "topic-0", "冲突度": 5, "人物知名度": 5,
                                "时效性": 5, "话题延展性": 5, "粉丝画像匹配度": 5}]}, "m"
        cands = [{"id": "topic-0",
                  "card": {"id": "topic-0", "主体": "曼城", "动作": "热点球评",
                           "发生时间": _today()},
                  "板块": "热点球评", "title": "曼城又赢了"}]
        with mock.patch.object(ranker, "awareness_of", return_value="A"):
            res = ranker.rank_candidates(cands, use_llm=True, call_llm=fake,
                                         now=datetime.now(_CST))
        self.assertEqual(res["mode"], "llm")
        rows = ranker.load_l3_ledger(7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["总分"], 25)
        self.assertEqual(rows[0]["主体"], "曼城")
        self.assertEqual(rows[0]["标题"], "曼城又赢了")

    def test_ledger_window_filter(self):
        ranker.append_l3_ledger([{"at": "2020-01-01 00:00:00", "总分": 1},
                                 {"at": f"{_today()} 09:00:00", "总分": 2}])
        rows = ranker.load_l3_ledger(7)
        self.assertEqual([r["总分"] for r in rows], [2])

    def test_broken_line_is_skipped(self):
        p = ranker.ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{不是 json}\n' + json.dumps({"at": f"{_today()} 09:00:00",
                                                   "总分": 7}) + "\n", encoding="utf-8")
        rows = ranker.load_l3_ledger(7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["总分"], 7)

    def test_ledger_write_never_raises(self):
        ranker.CACHE_PATH = Path("/proc/nonexistent/x.json")
        self.assertIsNone(ranker.append_l3_ledger([{"at": "x"}]))


class TestWeeklyAuditPrefersLedger(_Base):
    def setUp(self):
        super().setUp()
        self.mod = _load_audit()
        self.mod.OUTPUT_DIR = self.tmp / "output"       # 空目录，避免读真实 output
        self.mod.CACHE_PATH = self.tmp / "ranker_cache.json"
        self.mod.AUDIT_DIR = self.tmp / "rank_audit"
        self.mod.CALIB_PATH = self.mod.AUDIT_DIR / "calibration.json"

    def _write_ledger(self, n=2):
        self.mod.AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        rows = [{"at": f"{_today()} 1{i}:00:00", "key": f"k{i}", "主体": f"队{i}",
                 "标题": f"标题{i}", "板块": "热点球评",
                 "维度": {d: 3 + (i % 2) for d in ranker.DIMENSIONS}, "总分": 16} for i in range(n)]
        (self.mod.AUDIT_DIR / "l3_scores.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    def test_ledger_takes_priority_and_source_is_model(self):
        self._write_ledger(3)
        recs, src = self.mod.collect_records(7)
        self.assertEqual(src, "l3_ledger")
        self.assertEqual(len(recs), 3)
        self.assertTrue(all(r["来源"] == "model" for r in recs))
        self.assertEqual(recs[0]["标题"], "标题0")

    def test_falls_back_when_ledger_absent(self):
        recs, src = self.mod.collect_records(7)
        self.assertEqual(recs, [])
        self.assertIn(src, ("none", "metadata"))


if __name__ == "__main__":
    unittest.main()
