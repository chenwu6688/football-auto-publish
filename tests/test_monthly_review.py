#!/usr/bin/env python3
"""月度复盘一键闭环单测（用户 2026-10-14 需求：每月 1 号 15:00 月度看板 + 修改建议）。

覆盖：
  - 月份边界：自然月起止（含闰年 2 月、跨年 12→1）、默认统计「上一个完整月」
  - collect_month：严格按月边界过滤（月外一天都不多统计）
  - 调参建议：只用上月样本、只算不写（绝不调 apply_suggestion/main）
  - 修改建议规则：P0 合规 / 增长达标与未达标 / 供给不足 / 溯源缺口 / 链路滞后；
    指标缺失时建议落在「接入/排查」类，**绝不用 0 冒充**给优化建议
  - run(dry_run)：不写库不落产物、不发回执
  - run 端到端：产物落盘（看板 md/html/latest.json + 汇总报告）+ 回执发送 + ok 语义
  - 失败不阻断：某一步抛异常 → failures 记录、ok=False、其余步骤照跑

纪律对齐：所有路径指到 tempfile（台账隔离，绝不碰真实 data/）；调参只出建议。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import build_metrics_dashboard as bmd    # noqa: E402
import check_perf_lag as cpl             # noqa: E402
import growth_import as gi               # noqa: E402
import ingest_exports as ie              # noqa: E402
import monthly_review as mr              # noqa: E402
import ratio_tuner as rt                 # noqa: E402

CST = ZoneInfo("Asia/Shanghai")


def _write_meta(output_dir: Path, date_str: str, articles: list[dict],
                batches=("morning", "noon", "evening")) -> None:
    d = output_dir / date_str
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(json.dumps(
        {"date": date_str, "articles": articles,
         "batches_completed": list(batches)}), encoding="utf-8")


class TempEnv:
    """把月度复盘链路的所有路径指到 tempfile（台账隔离，绝不碰真实 data/）。"""

    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.inbox = self.root / "inbox"
        self.processed = self.root / "processed"
        self.state = self.root / "ingest_state.json"
        self.growth = self.root / "growth" / "fans_daily.json"
        self.output = self.root / "output"
        self.metrics = self.root / "metrics"
        self.audit = self.root / "audit"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "performance_log.json").write_text(
            '{"articles": {}}', encoding="utf-8")
        self.growth.parent.mkdir(parents=True, exist_ok=True)
        self.growth.write_text(json.dumps({"days": {}}), encoding="utf-8")

    def __enter__(self):
        self._saved = dict(
            bmd_out=bmd.OUTPUT_DIR, gi_growth=gi.GROWTH_PATH,
            cpl_perf=cpl.PERF_LOG, cpl_out=cpl.OUTPUT_DIR,
            rt_out=rt.OUTPUT_DIR,
            ie_inbox=ie.INBOX, ie_processed=ie.PROCESSED, ie_state=ie.STATE_PATH,
            mr_metrics=mr.METRICS_DIR, mr_audit=mr.AUDIT_DIR,
        )
        bmd.OUTPUT_DIR = self.output
        gi.GROWTH_PATH = self.growth
        cpl.PERF_LOG, cpl.OUTPUT_DIR = self.output / "performance_log.json", self.output
        rt.OUTPUT_DIR = self.output
        ie.INBOX, ie.PROCESSED, ie.STATE_PATH = self.inbox, self.processed, self.state
        mr.METRICS_DIR, mr.AUDIT_DIR = self.metrics, self.audit
        return self

    def __exit__(self, *exc):
        s = self._saved
        bmd.OUTPUT_DIR = s["bmd_out"]
        gi.GROWTH_PATH = s["gi_growth"]
        cpl.PERF_LOG, cpl.OUTPUT_DIR = s["cpl_perf"], s["cpl_out"]
        rt.OUTPUT_DIR = s["rt_out"]
        ie.INBOX, ie.PROCESSED, ie.STATE_PATH = s["ie_inbox"], s["ie_processed"], s["ie_state"]
        mr.METRICS_DIR, mr.AUDIT_DIR = s["mr_metrics"], s["mr_audit"]
        self._td.cleanup()
        return False


class TestMonthBounds(unittest.TestCase):
    """自然月边界与默认月（1 号跑 = 上一个完整月）。"""

    def test_bounds_common_months(self):
        self.assertEqual(mr._month_bounds("2026-09"), (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(mr._month_bounds("2026-12"), (date(2026, 12, 1), date(2026, 12, 31)))
        self.assertEqual(mr._month_bounds("2026-01"), (date(2026, 1, 1), date(2026, 1, 31)))

    def test_bounds_february_leap_and_flat(self):
        self.assertEqual(mr._month_bounds("2026-02"), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(mr._month_bounds("2028-02"), (date(2028, 2, 1), date(2028, 2, 29)))

    def test_bounds_invalid_month(self):
        with self.assertRaises(ValueError):
            mr._month_bounds("2026-13")
        with self.assertRaises(ValueError):
            mr._month_bounds("bad")

    def test_default_month_is_previous_full_month(self):
        # 1 号 15:00 跑 → 上个月（时间契约：collector 1 号 14:40 已入库上月最后一天）
        self.assertEqual(
            mr._default_month(datetime(2026, 10, 1, 15, 0, tzinfo=CST)), "2026-09")
        # 月中手动跑 → 仍是上一个完整月（不是本月）
        self.assertEqual(
            mr._default_month(datetime(2026, 10, 15, 10, 0, tzinfo=CST)), "2026-09")
        # 跨年：1 月跑 → 去年 12 月
        self.assertEqual(
            mr._default_month(datetime(2026, 1, 1, 15, 0, tzinfo=CST)), "2025-12")


class TestCollectMonth(unittest.TestCase):
    """月度统计严格按月边界过滤（复用周看板逻辑，只换窗口）。"""

    def test_only_month_days_counted(self):
        with TempEnv() as env:
            for ds, n in (("2026-08-31", 1), ("2026-09-05", 2), ("2026-09-30", 1),
                          ("2026-10-01", 3)):
                _write_meta(env.output, ds, [
                    {"title": f"{ds}-{i}", "slug": f"{ds}-{i}",
                     "sources_used": ["s"], "topic_category": "转会官宣",
                     "performance": {"reads": 100}} for i in range(n)])
            stats = mr.collect_month("2026-09")
            self.assertEqual(stats["supply"]["覆盖天数"], 2)     # 只有 9-5 与 9-30
            self.assertEqual(stats["supply"]["总篇数"], 3)
            self.assertEqual(stats["supply"]["批次完成率"], 100.0)
            self.assertEqual(stats["supply"]["溯源覆盖率"], 100.0)
            self.assertEqual(stats["growth"]["篇均阅读"], 100.0)


class TestTuneSuggestion(unittest.TestCase):
    """调参建议：只用上月样本，只算不写。"""

    def test_observations_filtered_to_month_and_read_only(self):
        with TempEnv():
            with mock.patch.object(rt, "collect_observations") as obs_mock, \
                 mock.patch.object(rt, "compute_suggestion") as sug_mock, \
                 mock.patch.object(rt, "apply_suggestion") as apply_mock:
                obs_mock.return_value = [
                    {"date": "2026-08-20", "index": 0, "category": "战报比分",
                     "reads": 10, "retention": 0.3},
                    {"date": "2026-09-10", "index": 1, "category": "转会官宣",
                     "reads": 300, "retention": 0.4},
                    {"date": "2026-10-01", "index": 2, "category": "转会官宣",
                     "reads": 999, "retention": 0.5},
                ]
                sug_mock.return_value = {"样本总量": 1}
                out = mr._tune_suggestion("2026-09", today=date(2026, 10, 1))
            # 窗口起点 = 月末 + 1 天倒推 40 天（覆盖整月），再按月过滤
            self.assertEqual(obs_mock.call_args[0][0], 40)
            self.assertEqual(obs_mock.call_args[1]["today"], date(2026, 10, 1))
            obs_passed = sug_mock.call_args[0][1]
            self.assertEqual([o["date"] for o in obs_passed], ["2026-09-10"],
                             "只把上月样本交给调参计算")
            self.assertEqual(out, {"样本总量": 1})
            apply_mock.assert_not_called()   # 只算不写：绝不套用配比


class TestRecommendations(unittest.TestCase):
    """修改建议规则：每条带依据；缺失指标走「接入/排查」类，绝不用 0 冒充。"""

    def _stats(self, **kw):
        base = {
            "growth": {"篇均阅读": None, "整体点击率": None, "平均完成率": None,
                       "粉丝阅读占比": None, "日均涨粉": None, "_data_points": 0},
            "supply": {"日均篇数": 6, "批次完成率": 100.0, "溯源覆盖率": 100.0},
            "by_category": {}, "by_title": {}, "compliance": {},
        }
        for k, v in kw.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                base[k].update(v)
            else:
                base[k] = v
        return base

    def _dim_pri(self, recs):
        return [(r["维度"], r["优先级"]) for r in recs]

    def test_missing_metrics_give_onboarding_advice_not_zero_based(self):
        recs = mr._monthly_recommendations(self._stats(), None, None, "2026-09")
        joined = "\n".join(r["建议"] + r["依据"] for r in recs)
        self.assertIn("回填绩效数据", joined)
        self.assertIn("回填展现量", joined)
        self.assertIn("粉丝分析通道", joined)
        # 没有任何一条建议是基于「0 阅读/0 涨粉」得出的优化结论（那是 0 冒充）
        self.assertNotIn("0 次 <", joined)
        self.assertNotIn("0% <", joined.replace("溯源覆盖率 0.0% <", ""))

    def test_below_target_raises_p1_with_numbers(self):
        stats = self._stats(growth={"篇均阅读": 80.3, "整体点击率": 2.08,
                                    "平均完成率": 33.5})
        recs = mr._monthly_recommendations(stats, None, None, "2026-09")
        hits = [r for r in recs if r["维度"] == "增长" and r["优先级"] == "P1"]
        self.assertTrue(hits)
        reads_rec = [r for r in hits if "篇均阅读" in r["依据"]][0]
        self.assertIn("80.3 次 < 30 天目标 200 次", reads_rec["依据"])

    def test_meeting_target_gives_info_not_alarm(self):
        stats = self._stats(growth={"篇均阅读": 250})
        recs = mr._monthly_recommendations(stats, None, None, "2026-09")
        info = [r for r in recs if r["优先级"] == "P3" and "篇均阅读" in r["依据"]]
        self.assertTrue(info)
        self.assertIn("250 次", info[0]["依据"])

    def test_compliance_violation_is_p0_and_first(self):
        stats = self._stats(compliance={"false_content_count": 2})
        recs = mr._monthly_recommendations(stats, None, None, "2026-09")
        self.assertEqual(recs[0]["优先级"], "P0")
        self.assertEqual(recs[0]["维度"], "合规")
        self.assertIn("2 次", recs[0]["依据"])

    def test_suspended_breaker_is_p0(self):
        stats = self._stats(compliance={"suspended": True, "reason": "不实内容"})
        recs = mr._monthly_recommendations(stats, None, None, "2026-09")
        p0 = [r for r in recs if r["优先级"] == "P0"]
        self.assertTrue(any("熔断" in r["建议"] for r in p0))

    def test_supply_shortfall_and_trace_gap(self):
        stats = self._stats(supply={"日均篇数": 3.2, "批次完成率": 66.7,
                                    "溯源覆盖率": 42.0})
        recs = mr._monthly_recommendations(stats, None, None, "2026-09")
        supply = [r for r in recs if r["维度"] == "供给" and r["优先级"] == "P1"]
        self.assertEqual(len(supply), 3)
        self.assertTrue(any("计划 6 篇/天" in r["依据"] for r in supply))
        self.assertTrue(any("42.0%" in r["依据"] for r in supply))

    def test_lag_over_threshold_flags_pipeline(self):
        lag = {"绩效滞后天数": 5, "转粉滞后天数": 1, "回填覆盖率": {"rate": 95}}
        recs = mr._monthly_recommendations(self._stats(), None, lag, "2026-09")
        pipe = [r for r in recs if r["维度"] == "数据链路"]
        self.assertTrue(any("绩效滞后 5 天" in r["依据"] for r in pipe))
        self.assertFalse(any("转粉滞后" in r["依据"] for r in pipe))   # 1 天不告警

    def test_recommendations_sorted_by_priority(self):
        stats = self._stats(compliance={"false_content_count": 1},
                            growth={"篇均阅读": 50})
        recs = mr._monthly_recommendations(stats, None, None, "2026-09")
        pris = [r["优先级"] for r in recs]
        self.assertEqual(pris, sorted(pris, key=lambda p: {"P0": 0, "P1": 1, "P2": 2, "P3": 3}[p]))


class TestRunEndToEnd(unittest.TestCase):
    """run 端到端：dry-run 不落产物；正式跑落全部产物并发回执；失败不阻断。"""

    def _seed(self, env):
        for ds in ("2026-09-05", "2026-09-30"):
            _write_meta(env.output, ds, [
                {"title": f"{ds} 官宣", "slug": f"{ds}-官宣",
                 "topic_category": "转会官宣", "sources_used": ["s"],
                 "performance": {"reads": 120, "impressions": 6000,
                                 "retention_rate": 0.35, "follower_reads": 5}},
                {"title": f"{ds} 战报", "slug": f"{ds}-战报",
                 "topic_category": "战报比分", "sources_used": ["s"],
                 "performance": {"reads": 20, "impressions": 5000,
                                 "retention_rate": 0.30, "follower_reads": 1}},
            ])

    def test_dry_run_writes_nothing_and_no_notify(self):
        with TempEnv() as env:
            self._seed(env)
            with mock.patch.object(mr, "send_wxpusher") as notify:
                out = mr.run("2026-09", dry_run=True, notify=True)
            self.assertTrue(out["ok"], out["failures"])
            notify.assert_not_called()
            self.assertEqual(list(env.metrics.glob("*")), [])
            self.assertEqual(list(env.audit.glob("*")), [])

    def test_full_run_writes_artifacts_and_notifies(self):
        with TempEnv() as env:
            self._seed(env)
            with mock.patch.object(mr, "send_wxpusher") as notify:
                out = mr.run("2026-09", dry_run=False, notify=True,
                             now=datetime(2026, 10, 1, 15, 0, tzinfo=CST))
            self.assertTrue(out["ok"], out["failures"])
            # 看板三件 + 汇总报告
            self.assertTrue((env.metrics / "monthly_2026-09.md").exists())
            self.assertTrue((env.metrics / "monthly_2026-09.html").exists())
            self.assertTrue((env.metrics / "monthly_latest.json").exists())
            self.assertTrue((env.audit / "monthly_review_2026-09.md").exists())
            latest = json.loads((env.metrics / "monthly_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["month"], "2026-09")
            self.assertIn("recommendations", latest)
            self.assertIn("dashboard", latest)
            # 看板标题带月度周期标签（不是「近 30 天」）
            md = (env.metrics / "monthly_2026-09.md").read_text(encoding="utf-8")
            self.assertIn("2026-09 月度", md)
            # 回执发送
            notify.assert_called_once()
            title, content = notify.call_args[0]
            self.assertIn("月度复盘", title)
            self.assertIn("2026-09", content)

    def test_step_failure_does_not_block_others_and_ok_false(self):
        with TempEnv() as env:
            self._seed(env)
            with mock.patch.object(ie, "run", side_effect=RuntimeError("inbox 炸了")), \
                 mock.patch.object(mr, "send_wxpusher"):
                out = mr.run("2026-09", dry_run=False, notify=True,
                             now=datetime(2026, 10, 1, 15, 0, tzinfo=CST))
            self.assertFalse(out["ok"])
            self.assertTrue(any("inbox 炸了" in f for f in out["failures"]))
            # 失败不阻断：统计/看板/建议照常产出
            self.assertTrue((env.metrics / "monthly_2026-09.md").exists())
            self.assertTrue(out["recommendations"])
            report = (env.audit / "monthly_review_2026-09.md").read_text(encoding="utf-8")
            self.assertIn("inbox 炸了", report)
            self.assertIn("需人工", report)


if __name__ == "__main__":
    unittest.main()
