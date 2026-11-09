"""时效闸门的「比例型盲区」（2026-11-09 深夜）。

## 它守的是什么

`verify_publish_gate` 的 E 项原来只查 `over_limit`（已知时效超上限仍被保留），
而这恰好是闸门**唯一看得见**的部分。真正的盲区是 `unknown`：发布时间读不出来
的素材被**保守放行**，既不计数也不报警。

实测近 5 天 678 条里 189 条（28%）时间无法判定，全部来自「热点媒体」；
有时间的 489 条里拦掉 51 条（10.4%），说明闸门本身有效。

风险形态很具体：**源站改版把时间字段弄丢，unknown 会从 28% 涨到 100%，
而 `over_limit` 仍为空、E 项仍绿** —— 闸门彻底失效却无人知晓。

## 阈值为什么是 35% / 50%

- 50%（硬线）不是新发明的数：全仓对「未判定」的一贯口径就是「过半即不能算
  通过」（`verify_compliance` 的 rc=2 判据即 `len(judged) * 2 < len(block_rules)`）。
  同一量两处定义不同阈值，就是 11-04「规则相斥」事故的起手式。
- 35%（告警线）：实测 27~28%，留 7 个点余量，日常不吵；越过就说明源站在退化。
"""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import freshness_ledger as fl  # noqa: E402
from scripts import verify_publish_gate as pg  # noqa: E402


class _LedgerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = fl.LEDGER_PATH
        fl.LEDGER_PATH = Path(self._tmp.name) / "freshness.json"

    def tearDown(self):
        fl.LEDGER_PATH = self._orig
        self._tmp.cleanup()

    def _note(self, checked, unknown, kept_ages=(), dropped_ages=(), **kw):
        fl.note(label="热点媒体", checked=checked, unknown=unknown,
                kept_ages=list(kept_ages), dropped_ages=list(dropped_ages),
                date=datetime.now(), **kw)


class TestUnknownRatio(_LedgerCase):
    def test_low_ratio_passes(self):
        self._note(checked=200, unknown=20, kept_ages=[5, 10])
        res = pg.check_old_news()
        self.assertIs(res["ok"], True)
        self.assertAlmostEqual(res["unknown_ratio"], 0.10, places=3)
        self.assertIn("在告警线", res["detail"][-1])

    def test_actual_baseline_28pct_passes(self):
        """当前真实水位（27~28%）必须绿，否则这条一上线就成天响。"""
        self._note(checked=200, unknown=56, kept_ages=[5])
        self.assertIs(pg.check_old_news()["ok"], True)

    def test_warn_band_still_passes_but_flags(self):
        self._note(checked=200, unknown=90, kept_ages=[5])  # 45%
        res = pg.check_old_news()
        self.assertIs(res["ok"], True)
        self.assertIn("高于告警线", res["detail"][-1])

    def test_over_hard_limit_fails(self):
        """源站把时间弄丢 → unknown 涨到 60% 必须红，不能继续报绿。"""
        self._note(checked=200, unknown=120, kept_ages=[5])
        res = pg.check_old_news()
        self.assertIs(res["ok"], False)
        self.assertIn("超过上限", res["detail"][-1])

    def test_exactly_at_hard_limit_passes(self):
        """边界取 `>`：恰好 50% 不判红（否则阈值等于悄悄收紧一档）。"""
        self._note(checked=200, unknown=100)
        self.assertIs(pg.check_old_news()["ok"], True)

    def test_small_sample_is_unjudged_not_pass(self):
        """样本不足不下结论：给 0 会让「一次都没检查」看起来像满分。"""
        self._note(checked=20, unknown=16)  # 80% 但样本不足
        res = pg.check_old_news()
        self.assertIsNone(res["ok"])
        self.assertIn("未判定", res["detail"][-1])

    def test_known_violation_wins_over_ratio(self):
        """已知超时仍被保留是铁证，优先级高于比例样本够不够。"""
        self._note(checked=10, unknown=2, kept_ages=[100])
        res = pg.check_old_news()
        self.assertIs(res["ok"], False)
        self.assertIn("超限保留", res["detail"][-1])

    def test_no_ledger_is_skipped(self):
        res = pg.check_old_news()
        self.assertTrue(res["skipped"])
        self.assertIsNone(res["unknown_ratio"])

    def test_zero_checked_is_unjudged(self):
        """台账有记录但一条都没检查 → 比例无意义，未判定而非满分。"""
        self._note(checked=0, unknown=0)
        res = pg.check_old_news()
        self.assertIsNone(res["ok"])
        self.assertIsNone(res["unknown_ratio"])


class TestThresholdsAreSingleSourced(unittest.TestCase):
    def test_thresholds_live_in_module_constants(self):
        """阈值不许散落在函数体里——验收线一旦写死两处，改一处就漏一处。"""
        self.assertGreater(pg.MAX_UNKNOWN_RATIO, pg.WARN_UNKNOWN_RATIO)
        self.assertEqual(pg.MAX_UNKNOWN_RATIO, 0.50)
        self.assertEqual(pg.WARN_UNKNOWN_RATIO, 0.35)

    def test_hard_ratio_matches_project_half_rule(self):
        """与 verify_compliance 的「过半未判定不能算通过」保持一致。"""
        from scripts import verify_compliance as vc
        rules = vc.load_rules()["rules"]
        block = [r for r in rules if str(r.get("tier")) in ("block", "rewrite")]
        # 恰好一半判过 → rc 0；差一条 → rc 2。即分界线在 50%。
        rows = [{"id": r["id"], "tier": str(r.get("tier")), "status": vc.PASS,
                 "detail": "", "hits": []} for r in block]
        for row in rows[: len(rows) // 2]:
            row["status"] = vc.UNJ
        rep = {"articles": 1, "rules": rows, "blocking": [], "risk_score": 0}
        self.assertEqual(vc.exit_code(rep), 0)
        self.assertEqual(pg.MAX_UNKNOWN_RATIO, 0.50)


if __name__ == "__main__":
    unittest.main()
