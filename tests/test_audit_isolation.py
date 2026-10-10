import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import scripts.audit_isolation as ai


class TestAuditIsolation(unittest.TestCase):
    """计划 11.3 · 物理隔离常态化审计。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._o_out, self._o_prompts = ai.OUTPUT_DIR, ai.PROMPTS_DIR
        ai.OUTPUT_DIR = self.tmp / "output"
        ai.PROMPTS_DIR = self.tmp / "prompts"
        ai.OUTPUT_DIR.mkdir(parents=True)
        ai.PROMPTS_DIR.mkdir(parents=True)

    def tearDown(self):
        ai.OUTPUT_DIR, ai.PROMPTS_DIR = self._o_out, self._o_prompts

    def _write_day(self, date_str, articles):
        d = ai.OUTPUT_DIR / date_str
        d.mkdir(parents=True, exist_ok=True)
        for i, body in enumerate(articles, 1):
            (d / f"article-{i}-t.md").write_text(body, encoding="utf-8")
        return d

    def test_prompt_isolation_pass_and_fail(self):
        (ai.PROMPTS_DIR / "rewrite_article.txt").write_text(
            "系统只提供结构化事实清单，你看不到任何原文句子。原文不可见。抽取器产出。",
            encoding="utf-8")
        self.assertTrue(ai.audit_prompt_isolation()["ok"])
        (ai.PROMPTS_DIR / "rewrite_article.txt").write_text(
            "请改写下面正文：{article_text}", encoding="utf-8")
        res = ai.audit_prompt_isolation()
        self.assertFalse(res["ok"])

    def test_duplication_detects_near_identical(self):
        common = "".join(f"第{i}个战术要点，说明球队在攻防转换中的细节处理。" for i in range(18))
        a = common + "阿森纳选择了收缩防守的稳妥策略。"
        b = common + "皇马选择压上进攻，赌一个进球。"
        self._write_day("2026-10-10", [a, b, "完全不同的另一篇内容。" * 30])
        now = datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        res = ai.audit_published_duplication(1, now=now)
        self.assertFalse(res["ok"])          # 高度相似 → 失败
        self.assertTrue(res["pairs"])
        self.assertGreaterEqual(res["pairs"][0]["jaccard"], ai.JACCARD_FAIL)

    def test_duplication_passes_when_distinct(self):
        self._write_day("2026-10-10", ["阿森纳" * 80, "皇家马德里" * 80])
        now = datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        res = ai.audit_published_duplication(1, now=now)
        self.assertTrue(res["ok"])

    def test_traceability_rate(self):
        d = ai.OUTPUT_DIR / "2026-10-10"
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(json.dumps({
            "articles": [{"sources_used": [{"url": "u"}]}, {"source_post": "x"}, {}],
        }, ensure_ascii=False), encoding="utf-8")
        now = datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        res = ai.audit_traceability(1, now=now)
        self.assertAlmostEqual(res["rate"], 66.7, places=1)


if __name__ == "__main__":
    unittest.main()
