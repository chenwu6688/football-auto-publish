"""三层供给占比与供给充足性单测（计划 12.1）。

覆盖：层级判定（显式标签/来源标志/内容类型推断/不猜）、幂等打标、
汇总与偏差、硬项（分层合法性、无断供）与告警项（配比、篇数）。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import supply_mix as sm  # noqa: E402
import verify_supply_mix as vsm  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_explicit_layer_wins(self):
        self.assertEqual(sm.classify({"_supply_layer": "一级·确定性·节点"}), sm.L1)
        self.assertEqual(sm.classify({"supply_layer": "三级·软性"}), sm.L3)

    def test_fixture_is_l1(self):
        self.assertEqual(sm.classify({"_is_fixture": True}), sm.L1)

    def test_reservoir_is_l3(self):
        self.assertEqual(sm.classify({"_from_reservoir": True}), sm.L3)

    def test_content_type_inference(self):
        self.assertEqual(sm.classify({"content_type": "热点球评"}), sm.L2)
        self.assertEqual(sm.classify({"content_type": "转会资讯"}), sm.L2)
        self.assertEqual(sm.classify({"content_type": "排行榜"}), sm.L3)
        self.assertEqual(sm.classify({"_column_name": "人物故事"}), sm.L3)

    def test_unclassified_never_guessed(self):
        # 既无来源标志、内容类型也不在册：不许猜
        self.assertEqual(sm.classify({}), sm.UNCLASSIFIED)
        self.assertEqual(sm.classify({"content_type": "未知类型"}), sm.UNCLASSIFIED)

    def test_reservoir_beats_content_type(self):
        # 蓄水池转正优先于内容类型推断
        self.assertEqual(sm.classify({"_from_reservoir": True, "content_type": "热点球评"}),
                         sm.L3)


class TestTag(unittest.TestCase):
    def test_tag_idempotent(self):
        a = {"_is_fixture": True}
        self.assertEqual(sm.tag(a), sm.L1)
        self.assertEqual(a["_supply_layer"], sm.L1)
        # 二次打标不覆盖已有合法标签
        self.assertEqual(sm.tag(a), sm.L1)

    def test_tag_sets_layer_for_plain(self):
        a = {"content_type": "转会资讯"}
        self.assertEqual(sm.tag(a), sm.L2)
        self.assertEqual(a["_supply_layer"], sm.L2)


class TestSummarize(unittest.TestCase):
    def test_counts_and_deviations(self):
        arts = ([{"_is_fixture": True}] * 3 +           # L1 = 3
                [{"content_type": "热点球评"}] * 5 +      # L2 = 5
                [{"_from_reservoir": True}] * 2)          # L3 = 2
        s = sm.summarize(arts)
        self.assertEqual(s["total"], 10)
        self.assertEqual(s["counts"][sm.L1], 3)
        self.assertEqual(s["counts"][sm.L2], 5)
        self.assertEqual(s["counts"][sm.L3], 2)
        self.assertEqual(s["ratios"][sm.L1], 30.0)
        self.assertEqual(s["deviations"][sm.L1], 0.0)

    def test_unclassified_counted(self):
        s = sm.summarize([{}])
        self.assertEqual(s["unclassified"], 1)

    def test_empty(self):
        s = sm.summarize([])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["ratios"][sm.L1], 0.0)


class TestCheck(unittest.TestCase):
    def test_unclassified_is_hard_fail(self):
        checks = sm.check(sm.summarize([{}]))
        hard = next(c for c in checks if c["item"] == "供给分层完整性")
        self.assertFalse(hard["ok"])
        self.assertFalse(hard["advisory"])

    def test_complete_is_hard_pass(self):
        checks = sm.check(sm.summarize([{"_is_fixture": True}]))
        hard = next(c for c in checks if c["item"] == "供给分层完整性")
        self.assertTrue(hard["ok"])

    def test_ratio_and_volume_advisory(self):
        s = sm.summarize([{"content_type": "热点球评"}] * 10)   # 全二级，偏配比
        checks = sm.check(s)
        ratio = next(c for c in checks if c["item"].startswith("层级配比偏差"))
        self.assertTrue(ratio["advisory"])
        self.assertFalse(ratio["ok"])
        vol = next(c for c in checks if c["item"].startswith("日更篇数"))
        self.assertFalse(vol["ok"])            # 10 篇超出 6-8

    def test_empty_day_skips_hard(self):
        checks = sm.check(sm.summarize([]))
        self.assertTrue(all(c["ok"] for c in checks if not c["advisory"]))

    def test_production_types_never_unclassified(self):
        """生成侧真会产的类型不得落到「未分层」——那是硬失败，不是数据质量问题。

        2026-11-04 排查出的相斥：`战术榜单` 只在 `_SOFT_SECTIONS`（按栏目名）里认，
        `紧急球评`/`战术解析` 又没做别名归一 —— 三者一旦缺显式 `_supply_layer`
        就会被判「未分层」，红的是验收而不是标签。
        """
        for ct in ("热点球评", "紧急球评", "数据对比", "排行榜", "榜单",
                   "战术榜单", "战术解析", "八卦趣事", "转会资讯", "人物故事"):
            self.assertNotEqual(sm.classify({"content_type": ct}), sm.UNCLASSIFIED, ct)


class TestL3Breakdown(unittest.TestCase):
    """三级·软性偏高必须能分清「结构要求」与「旧病复发」（2026-11-04 规则相斥排查）。

    计划 12.1 给三级的 20%，P1-1 的每批结构却**要求**每批产 1 篇数据型，稳态
    三级会到 ~48%。两个数字不可能同时满足，所以报告里必须能看出这 48% 是谁贡献的，
    否则这条告警天天亮、且没人能判断该不该动它。
    """

    def test_data_types_counted_as_structure_driven(self):
        arts = [{"content_type": "数据对比"}, {"content_type": "排行榜"},
                {"content_type": "战术榜单"}, {"content_type": "人物故事"}]
        lb = sm.summarize(arts)["l3_breakdown"]
        self.assertEqual(lb["data_driven"], 2)
        self.assertEqual(lb["other"], 2)          # 战术榜单/人物故事 归「应压缩」一侧

    def test_alias_normalized(self):
        """榜单 → 排行榜（归一后仍算数据型），否则同一类被拆成两半互相「合格」。"""
        lb = sm.summarize([{"content_type": "榜单"}])["l3_breakdown"]
        self.assertEqual(lb["data_driven"], 1)

    def test_ratio_check_reports_breakdown(self):
        s = sm.summarize([{"content_type": "数据对比"}] * 4 +
                         [{"content_type": "战术榜单"}] * 2 +
                         [{"content_type": "热点球评"}] * 4)
        ratio = next(c for c in sm.check(s) if c["item"].startswith("层级配比偏差"))
        self.assertTrue(any("拆解" in d for d in ratio["detail"]), ratio["detail"])
        line = next(d for d in ratio["detail"] if "拆解" in d)
        self.assertIn("数据型 4 篇", line)
        self.assertIn("其他 2 篇", line)


class _FakeOut:
    """临时 output 目录上下文，并接管 verify 脚本的 OUTPUT_DIR。"""

    def __init__(self, days: dict):
        self.days = days
        self.tmp = None

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for ds, arts in self.days.items():
            d = root / ds
            d.mkdir(parents=True, exist_ok=True)
            (d / "metadata.json").write_text(
                json.dumps({"date": ds, "articles": arts}, ensure_ascii=False),
                encoding="utf-8")
        self._old = vsm.OUTPUT_DIR
        vsm.OUTPUT_DIR = root
        return root

    def __exit__(self, *exc):
        vsm.OUTPUT_DIR = self._old
        self.tmp.cleanup()


class TestVerifyScript(unittest.TestCase):
    def test_no_outage_and_legal_layers_pass(self):
        days = {"2026-10-12": [{"_is_fixture": True, "supply_layer": sm.L1}] * 3
                + [{"content_type": "热点球评", "supply_layer": sm.L2}] * 5,
                "2026-10-11": [{"content_type": "转会资讯", "supply_layer": sm.L2}] * 6}
        with _FakeOut(days):
            rep = vsm.verify(days=7, today="2026-10-12")
        self.assertTrue(rep["ok"], rep["hard_failed"])

    def test_empty_day_is_outage(self):
        days = {"2026-10-12": [], "2026-10-11": [{"content_type": "热点球评"}] * 6}
        with _FakeOut(days):
            rep = vsm.verify(days=7, today="2026-10-12")
        self.assertFalse(rep["ok"])
        self.assertIn("无断供（硬）", rep["hard_failed"])

    def test_illegal_layer_is_hard_fail(self):
        days = {"2026-10-12": [{"supply_layer": "四级·野生"}] * 6}
        with _FakeOut(days):
            rep = vsm.verify(days=7, today="2026-10-12")
        self.assertFalse(rep["ok"])
        self.assertIn("分层合法性（硬）", rep["hard_failed"])


class TestMetadataPassthrough(unittest.TestCase):
    """save_metadata 需透传 supply_layers / degrade_events / pending_review 等字段。"""

    def test_extra_keys_persisted(self):
        import file_writer
        with tempfile.TemporaryDirectory() as tmp:
            fw = file_writer.FileWriter(base_dir=tmp)
            meta = {
                "total_articles": 1,
                "articles": [{"title": "t", "supply_layer": sm.L2}],
                "topics": [],
                "data_sources": {},
                "supply_layers": sm.summarize([{"content_type": "热点球评"}]),
                "degrade_events": [{"环节": "赛程源"}],
                "pending_review": [{"id": "x"}],
            }
            path = fw.save_metadata("2026-10-12", meta)
            got = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertIn("supply_layers", got)
        self.assertIn("degrade_events", got)
        self.assertIn("pending_review", got)
        self.assertEqual(got["date"], "2026-10-12")
        self.assertEqual(got["total_articles"], 1)


if __name__ == "__main__":
    unittest.main()
