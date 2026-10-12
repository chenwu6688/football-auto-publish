"""平台作品ID绑定与「不冒充0」单测（计划第十章）。

覆盖：
  - 平台ID规范化：纯数字 / 作品链接提取 / 占位符不认；
  - 平台ID优先于标题匹配：**平台改标题后仍能命中**；
  - ID命中会回写进 metadata，形成跨导入的稳定主键；
  - CLI 不传指标时不写 0（诚实原则：未提供 = 待接入）。

纪律：全部在临时目录内构造合成数据，不读也不写真实 output/。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import toutiao_import as ti            # noqa: E402
import performance_logger as pl        # noqa: E402
import scripts.import_performance as ip  # noqa: E402


def _write_meta(outdir: Path, date_str: str, articles: list) -> None:
    d = outdir / date_str
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(
        json.dumps({"date": date_str, "articles": articles}, ensure_ascii=False),
        encoding="utf-8")


class TestNormalizePlatformId(unittest.TestCase):
    def test_plain_digits_kept(self):
        self.assertEqual(ti.normalize_platform_id("7695627775839846946"),
                         "7695627775839846946")

    def test_link_extracts_id(self):
        self.assertEqual(
            ti.normalize_platform_id("https://toutiao.com/i7695627775839846946/"),
            "7695627775839846946")

    def test_placeholder_not_an_id(self):
        self.assertEqual(ti.normalize_platform_id("-1"), "")
        self.assertEqual(ti.normalize_platform_id("--"), "")
        self.assertEqual(ti.normalize_platform_id(""), "")
        self.assertEqual(ti.normalize_platform_id(None), "")

    def test_column_mapping(self):
        self.assertEqual(ti.map_header("ID")[0], "platform_id")
        self.assertEqual(ti.map_header("链接")[0], "platform_id")


class TestIdMatch(unittest.TestCase):
    """平台改标题后仍能按ID命中——这是本次改动的核心价值。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _write_meta(self.tmp, "2026-09-15", [
            {"index": 1, "slug": "abc-1", "title": "原标题甲",
             "platform_id": "7695627775839846946"},
            {"index": 2, "slug": "abc-2", "title": "另一篇乙", "platform_id": "222"},
        ])

    def test_title_changed_still_matches_by_id(self):
        rows = [["标题", "ID", "阅读量"],
                ["平台改过的新标题", "7695627775839846946", "3210"]]
        res = ti.adapt_rows(rows, output_dir=self.tmp)
        rep = res["report"]
        self.assertEqual(len(res["records"]), 1, "平台改标题后仍应按ID命中")
        self.assertEqual(rep["matched_by_id"], 1)
        rec = res["records"][0]
        self.assertEqual(rec["slug"], "abc-1")
        self.assertEqual(rec["date"], "2026-09-15")
        self.assertEqual(rec["reads"], 3210)

    def test_id_wins_over_conflicting_title(self):
        """标题指向另一篇、ID却指向本篇时，以ID为准（ID是跨导出稳定键）。"""
        rows = [["标题", "ID", "阅读量"],
                ["另一篇乙", "7695627775839846946", "999"]]
        res = ti.adapt_rows(rows, output_dir=self.tmp)
        self.assertEqual(res["records"][0]["slug"], "abc-1",
                         "ID 应压过标题歧义")

    def test_unknown_id_falls_back_to_title(self):
        rows = [["标题", "ID", "阅读量"],
                ["另一篇乙", "999888777", "999"]]
        res = ti.adapt_rows(rows, output_dir=self.tmp)
        rep = res["report"]
        self.assertEqual(rep["matched_by_id"], 0)
        self.assertEqual(len(res["records"]), 1, "ID 不认识时应回退标题匹配")
        self.assertEqual(res["records"][0]["slug"], "abc-2")

    def test_unknown_id_and_unknown_title_is_unmatched(self):
        rows = [["标题", "ID", "阅读量"],
                ["从未见过的标题", "999888777", "999"]]
        rep = ti.adapt_rows(rows, output_dir=self.tmp)["report"]
        self.assertEqual(len(rep["matched"]), 0)
        self.assertEqual(len(rep["unmatched"]), 1)


class TestPlatformIdBinding(unittest.TestCase):
    """ID命中 → 回写 metadata → 下次导入可认（闭环）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._p1 = None
        self._p2 = None

    def _patch(self):
        import unittest.mock as mock
        self._p1 = mock.patch.object(pl, "OUTPUT_DIR", self.tmp)
        self._p2 = mock.patch.object(pl, "PERF_LOG_PATH",
                                     self.tmp / "performance_log.json")
        self._p1.start()
        self._p2.start()

    def _unpatch(self):
        if self._p1:
            self._p1.stop()
        if self._p2:
            self._p2.stop()

    def test_id_round_trip_into_metadata(self):
        self._patch()
        try:
            _write_meta(self.tmp, "2026-09-15",
                        [{"index": 1, "slug": "abc-1", "title": "甲"}])
            pl.log_article_performance("2026-09-15", 1, reads=3210, slug="abc-1",
                                       platform_id="7695627775839846946")
            log = json.loads((self.tmp / "performance_log.json").read_text())
            rec = log["articles"]["2026-09-15/abc-1"]
            self.assertEqual(rec["platform_id"], "7695627775839846946")

            pl.update_metadata_performance("2026-09-15")
            meta = json.loads(
                (self.tmp / "2026-09-15" / "metadata.json").read_text())
            art = meta["articles"][0]
            self.assertEqual(art.get("platform_id"), "7695627775839846946",
                             "ID 必须回写进 metadata，供下次导入按ID匹配")
            self.assertEqual(art["performance"]["reads"], 3210)

            # 下一轮导入：元数据已带ID → 直接可建 ID 索引
            idx = ti.build_id_index(self.tmp)
            self.assertIn("7695627775839846946", idx)
            self.assertEqual(idx["7695627775839846946"]["slug"], "abc-1")
        finally:
            self._unpatch()


class TestNoFakeZero(unittest.TestCase):
    """未提供的指标绝不写成 0（诚实原则）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run_cli(self, *argv):
        env = dict(os.environ, OUTPUT_DIR=str(self.tmp))
        return subprocess.run(
            [sys.executable, str(ROOT / "performance_logger.py"), *argv],
            env=env, capture_output=True, text=True)

    def test_cli_omitted_metrics_are_absent_not_zero(self):
        r = self._run_cli("2026-10-12", "1", "--reads", "500")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = json.loads((self.tmp / "performance_log.json").read_text())
        rec = log["articles"]["2026-10-12/article-1"]
        self.assertEqual(rec["reads"], 500)
        for f in ("comments", "likes", "shares", "impressions",
                  "follower_reads", "retention_rate", "new_followers"):
            self.assertNotIn(f, rec, f"{f} 未提供时不应写 0 冒充真实数据")

    def test_cli_real_zero_is_preserved(self):
        r = self._run_cli("2026-10-12", "1", "--reads", "0", "--comments", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = json.loads((self.tmp / "performance_log.json").read_text())[
            "articles"]["2026-10-12/article-1"]
        self.assertEqual(rec["reads"], 0, "真实 0 应保留为 0")
        self.assertEqual(rec["comments"], 0)

    def test_validate_row_carries_platform_id(self):
        rec, err = ip.validate_row(
            {"date": "2026-09-15", "slug": "abc-1", "reads": "3210",
             "platform_id": "https://toutiao.com/i7695627775839846946/"}, 2)
        self.assertIsNone(err)
        self.assertEqual(rec["platform_id"], "7695627775839846946")


if __name__ == "__main__":
    unittest.main()
