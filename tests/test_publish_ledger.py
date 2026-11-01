"""发布结果台账与 platform_id 绑定单测（2026-11-01 月度分析落地）。

背景（月度分析查出的最大的洞）：10 月成稿 242 篇，实际发布只有 93 篇，
而这件事**一个月都没人发现**——因为 `publish_all` 只把结果拼成一段人话发
WxPusher，从不落库。同时 `publish_article` 把响应里现成的 pgc_id 丢掉了，
导致 platform_id 只能靠「下次导出按标题反查」补，而导出只回溯 7 天。

本文件钉死三件事：
  A. `_first_pgc_id` 能取到 ID，且不把占位符（-1/0/None）当 ID；
  B. `bind_platform_ids` 按 slug/title 绑定，**匹配不上不猜**，且不覆盖为非法值；
  C. `write_publish_ledger` 的计数口径正确，published_without_id 必须单独计。
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import publisher  # noqa: E402


class TestFirstPgcId(unittest.TestCase):
    def test_picks_first_valid(self):
        self.assertEqual(publisher._first_pgc_id([{"code": 0, "pgc_id": "770123"}]), "770123")

    def test_empty_returns_blank(self):
        self.assertEqual(publisher._first_pgc_id([]), "")
        self.assertEqual(publisher._first_pgc_id(None), "")

    def test_placeholder_not_treated_as_id(self):
        """负向：占位符「-1」/「0」/「None」不是作品 ID，必须跳过取下一个。"""
        rows = [{"code": 0, "pgc_id": "-1"}, {"code": 0, "pgc_id": "0"},
                {"code": 0, "pgc_id": "None"}, {"code": 0, "pgc_id": "770456"}]
        self.assertEqual(publisher._first_pgc_id(rows), "770456")

    def test_all_placeholder_returns_blank(self):
        self.assertEqual(publisher._first_pgc_id([{"code": 0, "pgc_id": ""},
                                                  {"code": -1, "pgc_id": "-1"}]), "")

    def test_none_value_skipped(self):
        self.assertEqual(publisher._first_pgc_id([{"code": 0, "pgc_id": None},
                                                  {"code": 0, "pgc_id": " 770789 "}]), "770789")


class TestBindPlatformIds(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "out"
        d = self.out / "2026-11-01"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(json.dumps({"date": "2026-11-01", "articles": [
            {"index": 1, "title": "甲队崩了？", "slug": "jia-beng-abc123"},
            {"index": 2, "title": "乙队赢了？", "slug": "yi-ying-def456"},
        ]}, ensure_ascii=False), encoding="utf-8")
        self._orig = publisher.OUTPUT_BASE
        publisher.OUTPUT_BASE = self.out

    def tearDown(self):
        publisher.OUTPUT_BASE = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _meta(self):
        return json.loads((self.out / "2026-11-01" / "metadata.json").read_text(encoding="utf-8"))

    def test_bind_by_slug(self):
        r = publisher.bind_platform_ids("2026-11-01", [
            {"slug": "jia-beng-abc123", "title": "甲队崩了？",
             "platform_id": "7700000000000000001", "source": "publish_response"}])
        self.assertEqual(r["bound"], 1)
        art = self._meta()["articles"][0]
        self.assertEqual(art["platform_id"], "7700000000000000001")
        self.assertEqual(art["platform_id_source"], "publish_response")

    def test_bind_falls_back_to_title(self):
        """slug 缺失时按标题兜底（老成稿可能没写 slug）。"""
        r = publisher.bind_platform_ids("2026-11-01", [
            {"slug": "", "title": "乙队赢了？", "platform_id": "7700000000000000002"}])
        self.assertEqual(r["bound"], 1)
        self.assertEqual(self._meta()["articles"][1]["platform_id"], "7700000000000000002")

    def test_unmatched_reported_not_guessed(self):
        """负向核心：匹配不上要报出来，**绝不**按 index 乱绑（index 会重复串号）。"""
        r = publisher.bind_platform_ids("2026-11-01", [
            {"slug": "不存在的", "title": "也不存在", "platform_id": "770999"}])
        self.assertEqual(r["bound"], 0)
        self.assertEqual(r["unmatched"], ["不存在的"])
        for a in self._meta()["articles"]:
            self.assertNotIn("platform_id", a)

    def test_blank_id_skipped(self):
        r = publisher.bind_platform_ids("2026-11-01", [
            {"slug": "jia-beng-abc123", "title": "甲队崩了？", "platform_id": ""}])
        self.assertEqual(r["bound"], 0)

    def test_missing_metadata_reports(self):
        r = publisher.bind_platform_ids("2026-12-31", [
            {"slug": "x", "title": "x", "platform_id": "1"}])
        self.assertTrue(r["missing_meta"])


class TestPublishLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = publisher.AUDIT_DIR
        publisher.AUDIT_DIR = self.tmp / "audit"

    def tearDown(self):
        publisher.AUDIT_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recs(self):
        return [
            {"status": "published", "title": "甲", "slug": "a",
             "batch_name": "晨读", "platform_id": "770001", "reason": ""},
            {"status": "published", "title": "乙", "slug": "b",
             "batch_name": "晨读", "platform_id": "", "reason": "平台未返回 pgc_id"},
            {"status": "failed", "title": "丙", "slug": "c",
             "batch_name": "午间", "platform_id": "", "reason": "未找到发布确认按钮"},
            {"status": "skipped_prediction", "title": "丁", "slug": "d",
             "batch_name": "晚间", "platform_id": "", "reason": "预测稿不发布"},
        ]

    def test_counts(self):
        p = publisher.write_publish_ledger("2026-11-01", self._recs(), batch_filter="morning")
        d = json.loads(p.read_text(encoding="utf-8"))
        c = d["counts"]
        self.assertEqual(c["total"], 4)
        self.assertEqual(c["published"], 2)
        self.assertEqual(c["failed"], 1)
        self.assertEqual(c["skipped_prediction"], 1)
        # 关键：发布成功但没 ID 必须单列，否则「发布成功」会掩盖数据断链
        self.assertEqual(c["published_without_id"], 1)

    def test_latest_alias_written(self):
        publisher.write_publish_ledger("2026-11-01", self._recs())
        self.assertTrue((publisher.AUDIT_DIR / "publish_ledger_latest.json").exists())

    def test_records_preserved_verbatim(self):
        p = publisher.write_publish_ledger("2026-11-01", self._recs())
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(len(d["records"]), 4)
        self.assertEqual([r["status"] for r in d["records"]],
                         ["published", "published", "failed", "skipped_prediction"])

    def test_binding_embedded(self):
        p = publisher.write_publish_ledger("2026-11-01", self._recs(),
                                           binding={"bound": 1, "unmatched": ["x"]})
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(d["platform_id_binding"]["bound"], 1)

    def test_empty_records_still_writes(self):
        """空批次也要落盘——否则「这天没发东西」和「台账没跑」分不清。"""
        p = publisher.write_publish_ledger("2026-11-01", [])
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(d["counts"]["total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
