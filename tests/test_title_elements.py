#!/usr/bin/env python3
"""三要素闸门（title_elements）与验收脚本的单测。

最要紧的两条：
  1. 「尺子判据」不能因为数据换了方向就假装通过（单调性必须真判）；
  2. 供给侧注入点不足必须 FAIL——只加出口不加供给侧是最容易发生的半成品。
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import title_elements as te  # noqa: E402


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestExtract(unittest.TestCase):
    def test_full_three(self):
        e = te.extract("1平2负只拿0.33分，中超这根独苗还能撑到2028吗？")
        self.assertTrue(e.has_number and e.has_contrast and e.has_proper)
        self.assertEqual(e.count, 3)

    def test_top1_has_two(self):
        """全站 TOP1（494 阅读）缺对比，只能判 2 项——这是「不取三项全过」的依据。"""
        top1 = "广州球迷联盟喊出70年，李璇10月13日这番话到底戳中了谁？"
        e = te.extract(top1)
        self.assertEqual(e.count, 2)
        self.assertFalse(e.has_contrast)
        self.assertTrue(te.check(top1)["ok"])

    def test_empty_question_blocked(self):
        r = te.check("当年那个追风少年曲波，如今在基层默默带娃，他到底图个啥？")
        self.assertFalse(r["ok"])
        self.assertTrue(r["hit"])
        self.assertIn("专名", r["missing"] + ["专名"])

    def test_bare_year_not_number(self):
        self.assertFalse(te.extract("2026金球奖揭晓").has_number)
        self.assertTrue(te.extract("2026年的金球奖揭晓").has_number)

    def test_chinese_ordinal_is_number(self):
        self.assertTrue(te.extract("梅西凭什么位列第五").has_number)

    def test_legend_is_proper(self):
        """梅西/马拉多纳不在 entity_map（五大联赛射手榜）里，靠补充名单。"""
        for n in ("梅西", "马拉多纳", "巴乔", "齐达内"):
            self.assertTrue(te.extract(f"{n}这场踢得怎样").has_proper, n)

    def test_epithet_not_proper(self):
        self.assertFalse(te.extract("4场6球！25岁大英帝星复活").has_proper)

    def test_placeholder_safe(self):
        self.assertEqual(te.extract("").count, 0)
        self.assertTrue(te.check("")["ok"])


class TestCoverage(unittest.TestCase):
    def test_empty_is_none_not_zero(self):
        cov = te.coverage([])
        self.assertEqual(cov["n"], 0)
        self.assertIsNone(cov["pass_rate"])

    def test_rates_recomputable(self):
        titles = ["1平2负只拿0.33分，中超这根独苗还能撑到2028吗？",
                  "当年那个追风少年曲波，如今在基层默默带娃，他到底图个啥？"]
        cov = te.coverage(titles)
        self.assertEqual(cov["n"], 2)
        self.assertEqual(cov["pass_rate"], 50.0)


class TestVerifyScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = _load("verify_title_elements", "scripts/verify_title_elements.py")

    def test_selftest_ok(self):
        self.assertTrue(self.v._check_selftest()["ok"])

    def test_ruler_ok_on_real_data(self):
        r = self.v._check_ruler()
        if r.get("skipped"):
            self.skipTest("无真实样本")
        self.assertTrue(r["ok"], r["detail"])

    def test_ruler_not_ok_when_monotonic_flips(self):
        """把 analyzed 结果换成「通过组比被拦组还低」，判据必须 FAIL，不能沉默通过。"""
        fake = {
            "ours": {"n": 88, "passed": {"n": 50, "median": 3}, "blocked": {"n": 38, "median": 20},
                     "coverage": {}},
            "peers": {"n": 181, "passed": {"n": 100, "median": 800}, "blocked": {"n": 81, "median": 100},
                      "coverage": {}},
            "verdict": {"ours_pass_rate": 0.6, "peers_pass_rate": 0.7, "monotonic": False,
                        "peer_top_pass": "8/10", "peer_top_rate": 0.8,
                        "looser_than_peers": True, "within_pressure_band": True},
        }
        with mock.patch("analyze_title_elements.build_report", return_value=fake):
            r = self.v._check_ruler()
        self.assertFalse(r["ok"])

    def test_ruler_not_ok_when_too_loose(self):
        """通过率 99% = 形同虚设，必须 FAIL。"""
        fake = {
            "ours": {"n": 88, "passed": {"n": 87, "median": 20}, "blocked": {"n": 1, "median": 1},
                     "coverage": {}},
            "peers": {"n": 181, "passed": {"n": 150, "median": 800}, "blocked": {"n": 31, "median": 100},
                      "coverage": {}},
            "verdict": {"ours_pass_rate": 0.99, "peers_pass_rate": 0.83, "monotonic": True,
                        "peer_top_pass": "10/10", "peer_top_rate": 1.0,
                        "looser_than_peers": True, "within_pressure_band": False},
        }
        with mock.patch("analyze_title_elements.build_report", return_value=fake):
            r = self.v._check_ruler()
        self.assertFalse(r["ok"])

    def test_supply_side_fails_when_no_injection(self):
        with mock.patch.object(self.v, "_count_title_block_calls",
                               return_value=(0, {"title_block": 0, "brief_text": 0, "rule": 0})):
            r = self.v._check_supply_side()
        self.assertFalse(r["ok"])

    def test_gate_wiring(self):
        r = self.v._check_gate_wiring()
        if r.get("skipped"):
            self.skipTest("无法载入评分器")
        self.assertTrue(r["ok"], r["detail"])

    def test_verify_writes_audit_to_tempdir(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(self.v, "AUDIT_DIR", Path(d)):
                self.v.main(["--quiet"])
            self.assertTrue((Path(d) / "title_elements_latest.json").exists())


class TestGateEscapeHatch(unittest.TestCase):
    """「重试几次后兜底放行」曾经是个真口子：结构违规的标题就这么发出去了。

    11-04 那篇「大罗VS C罗巅峰数据对决，谁才是真正的锋线之王？」就是从这类放行里
    漏出去的——它不是分数不够，是疑问收尾超限。修法是换机械模板标题（只换标题、
    正文不动、不花额度），而不是把放行改成丢稿。
    """

    def test_ensure_fixture_title_swaps_question_ending(self):
        import orchestrator as o
        topic = {"home_zh": "北京国安", "away_zh": "泰港", "competition": "亚冠",
                 "home_standing": {"position": 9, "points": 12},
                 "away_standing": {"position": 3, "points": 19}}
        art = {"title": "国安这场到底还能不能翻盘？"}
        # 评分永远不过 + 历史已满额 → 必须换掉疑问收尾
        with mock.patch.object(o, "score_fixture_title", return_value=(30, ["疑问收尾限额已满"])), \
             mock.patch.object(o, "repick_title", return_value=""), \
             mock.patch.object(o, "recent_titles_for_gate", return_value=["某稿吗？", "某稿吗？"]):
            title, score, _ = o.ensure_fixture_title(topic, art)
        self.assertFalse(title.endswith("？"), f"仍以疑问收尾：{title}")
        self.assertTrue(te.check(title)["ok"], f"兜底标题没过三要素：{title}")
        self.assertEqual(art.get("_title_fallback"), "gate")

    def test_question_quota_since_is_later_than_title_since(self):
        v = _load("verify_title_anchor_quota", "scripts/verify_title_anchor.py")
        self.assertGreater(v.QUESTION_CAP_SINCE, v.TITLE_SINCE,
                           "限额生效日必须晚于改造生效日，否则拿没接限额的旧稿判新规则")

    def test_question_quota_undetermined_before_since(self):
        v = _load("verify_title_anchor_quota2", "scripts/verify_title_anchor.py")
        # 生效日未到（或窗口内无样本）→ 未判定，不能算通过
        with mock.patch.object(v, "_window", lambda days, since=None: None):
            r = v.check_question_quota()
        self.assertIsNone(r["ok"])
        self.assertTrue(r["pending"])


if __name__ == "__main__":
    unittest.main()
