"""溯源标注单测（计划第十章：溯源覆盖率）。

覆盖 orchestrator.resolve_provenance：
  - 确定性供给已填的来源原样保留、不重复；
  - LLM 路径从 `_source_fixture` 派生真实媒体来源（直播吧/懂球帝）；
  - `source_post` 取源文标题，取不到就留空（**绝不编造**）；
  - 完全没有来源信息时返回空（不冒充）。

纪律：只测纯函数，不触发 LLM / 网络 / 文件写入。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import orchestrator as orch  # noqa: E402


class TestResolveProvenance(unittest.TestCase):
    def test_deterministic_source_kept_as_is(self):
        used, post = orch.resolve_provenance(
            {"sources_used": ["足球数据 football-data.org"]})
        self.assertEqual(used, ["足球数据 football-data.org"])
        self.assertEqual(post, "")

    def test_llm_path_derives_media_source(self):
        used, post = orch.resolve_provenance(
            {"sources_used": [], "_source_fixture": {"source": "zhibo8"}})
        self.assertEqual(used, ["直播吧 zhibo8.cc"])

    def test_media_source_not_duplicated(self):
        used, _ = orch.resolve_provenance(
            {"sources_used": ["直播吧 zhibo8.cc"],
             "_source_fixture": {"source": "zhibo8"}})
        self.assertEqual(used, ["直播吧 zhibo8.cc"], "同一来源不应重复计入")

    def test_source_post_from_fixture_title(self):
        used, post = orch.resolve_provenance(
            {"_source_fixture": {"source": "dongqiudi", "article_title": "某源文标题"}})
        self.assertEqual(used, ["懂球帝 dongqiudi.com"])
        self.assertEqual(post, "某源文标题")

    def test_no_source_info_returns_empty_not_fabricated(self):
        used, post = orch.resolve_provenance({})
        self.assertEqual(used, [])
        self.assertEqual(post, "")

    def test_malformed_fixture_does_not_explode(self):
        used, post = orch.resolve_provenance({"_source_fixture": "不是字典"})
        self.assertEqual(used, [])
        self.assertEqual(post, "")

    def test_blank_entries_dropped(self):
        used, _ = orch.resolve_provenance({"sources_used": ["", "  ", "直播吧 zhibo8.cc"]})
        self.assertEqual(used, ["直播吧 zhibo8.cc"])


if __name__ == "__main__":
    unittest.main()
