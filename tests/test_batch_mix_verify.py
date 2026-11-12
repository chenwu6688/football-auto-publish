# -*- coding: utf-8 -*-
"""结构验收：判据从「攒 8 篇看 38%」改成「逐批降级 + 禁发类型」（2026-11-11）。

原来的 C 项（实际产出结构，硬项）有两个毛病：
① 38% 是「数据对比 30 + 排行榜 18 − 容差 10」的**合成口径**，红了定位不到缺哪类；
② 样本 8 篇时 ±1 篇 = ±12.5pp，比 10pp 容差还大 —— 红绿由单篇翻转决定。

现在的分工：
  · 硬项（成因侧）：每个成功批次的 `batch_mix.detail.降级` 必须为 0；禁发类型绝对为 0。
  · 观测项：实际占比与目标并排打印，**不参与判定、不进 pending**。

本文件守住 7 条：逐批降级能红且指得出批次、台账缺失按「无证据」不算 0、
样本按批计、生效时刻精确到批次（修复前的批不算）、禁发类型绝对判据、
观测项确实不进判定、观测项没有样本下限。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))
SCRIPT = ROOT / "scripts" / "verify_batch_mix.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location("verify_batch_mix_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _funnel_row(day: str, batch: str, degraded: int | None, *,
                requested: int = 2, at: str | None = None,
                slots: list | None = None) -> dict:
    """一条成功的批次记录。

    degraded=None      → 老记录（连「降级」子项都没有）
    slots=None         → 只有降级总数，**没有** `降级明细`（11-12 之前的形态）
    slots=["data 槽…"] → 有明细，能判「哪个槽降级」
    """
    det = {"请求": requested, "未选": 1}
    if degraded is not None:
        det["降级"] = degraded
    if slots is not None:
        det["降级明细"] = slots
    return {"date": day, "batch": batch, "status": "ok",
            "at": at or f"{day}T12:00:00+08:00",
            "stages": {"batch_mix": {"n": requested, "detail": det}}}


def _write_day(root: Path, day: str, types) -> None:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    meta = {"batch_mix": {"degraded": []},
            "articles": [{"title": f"{day}-{i}", "content_type": t}
                         for i, t in enumerate(types)]}
    (d / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


class _Base(unittest.TestCase):
    def setUp(self):
        self.vm = _load_verifier()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vm.OUTPUT_DIR = self.root
        self.vm.FUNNEL_PATH = self.root / "funnel.jsonl"
        self.today = datetime.now(CST).date()
        self.since_day = (self.today - timedelta(days=10)).strftime("%Y-%m-%d")
        self.vm.SLOT_SINCE_AT = f"{self.since_day}T00:00:00+08:00"
        self.day = self.today.strftime("%Y-%m-%d")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_funnel(self, rows):
        self.vm.FUNNEL_PATH.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")


class TestSlotFill(_Base):
    def test_data_slot_degraded_fails_and_names_the_batch(self):
        """data 槽降级 → 立刻 FAIL，且报告里能读出「哪天哪批」。"""
        self._write_funnel([
            _funnel_row(self.day, "晨读", 0, slots=[]),
            _funnel_row(self.day, "午间", 1, slots=["data 槽用次优类型补位"]),
        ])
        r = self.vm._check_slot_fill()
        self.assertIs(r["ok"], False, f"data 槽降级却没红：{r}")
        joined = " ".join(r["detail"])
        self.assertIn("午间", joined, "报告必须指名是哪一批，否则无法定位供给")
        self.assertIn("不要", joined, "处置文案要明确禁止调阈值/后移生效日")

    def test_main_slot_degraded_is_not_a_failure(self):
        """main 槽降级是热点型候选波动（DAILY_CAP 先撞闸），只观测不判红。

        11-12 晨读实测：请求 3 / 降级 2，两条都出自 main 槽 —— 旧判据
        「降级 > 0 即红」把它算成「data 槽塌了」，判据定位的事实是错的。
        """
        self._write_funnel([
            _funnel_row(self.day, b, 2, requested=3,
                        slots=["main 槽用次优类型补位", "main 槽用次优类型补位"])
            for b in ("晨读", "午间", "晚间")])
        r = self.vm._check_slot_fill()
        self.assertIs(r["ok"], True, f"main 槽降级被误判成 data 槽塌了：{r}")
        self.assertIn("main 槽", " ".join(r["detail"]), "要点出 main 槽降级留在观测里")

    def test_all_clean_after_min_batches_passes(self):
        """够 3 批且 data 槽都没降级 → 通过（不攒 8 篇）。"""
        self._write_funnel([_funnel_row(self.day, b, 0, slots=[])
                            for b in ("晨读", "午间", "晚间")])
        r = self.vm._check_slot_fill()
        self.assertIs(r["ok"], True, f"3 批 data 槽全绿应通过：{r}")

    def test_too_few_batches_is_pending(self):
        """只有 1 批且没降级 → 未判定（1 批不能证明什么）。"""
        self._write_funnel([_funnel_row(self.day, "晨读", 0, slots=[])])
        r = self.vm._check_slot_fill()
        self.assertIsNone(r["ok"])
        self.assertTrue(r.get("pending"))

    def test_rows_without_slot_detail_are_not_counted_as_zero(self):
        """只有降级总数、没有 `降级明细` → 未判定，绝不按「0 降级」补。

        分不清哪个槽降级时给出「通过」，就是在用一个总数冒充槽位证据
        （11-12 之前正是这么判的）。
        """
        self._write_funnel([_funnel_row(self.day, b, 0)
                            for b in ("晨读", "午间", "晚间")])
        r = self.vm._check_slot_fill()
        self.assertIsNone(r["ok"], "把「没有槽位明细」当成「data 槽没降级」")
        self.assertTrue(r.get("pending"))

    def test_legacy_rows_without_any_detail_are_skipped(self):
        """老记录没有「降级」子项 → 跳过，绝不按 0 补。"""
        self._write_funnel([_funnel_row(self.day, "晨读", None)])
        r = self.vm._check_slot_fill()
        self.assertIsNone(r["ok"], "把「没记降级」当成「没降级」= 拿缺失当事实")
        self.assertTrue(r.get("pending"))

    def test_missing_funnel_is_pending_not_pass(self):
        r = self.vm._check_slot_fill()
        self.assertIsNone(r["ok"])
        self.assertTrue(r.get("pending"))

    def test_batch_before_since_moment_is_excluded(self):
        """生效时刻之前的批次不算 —— 它跑在提示词补齐之前，必然降级。

        按「天」一刀切会把它算进来：这条检查从上线第一天起永久红，
        同时还会把修复后（12:01，data 槽已填）那批一起排掉。
        """
        self.vm.SLOT_SINCE_AT = f"{self.day}T11:58:00+08:00"
        self._write_funnel([
            _funnel_row(self.day, "晨读", 1, slots=["data 槽用次优类型补位"],
                        at=f"{self.day}T11:01:00+08:00"),   # 修复前
            _funnel_row(self.day, "午间", 0, slots=[], at=f"{self.day}T12:01:00+08:00"),
            _funnel_row(self.day, "晚间", 0, slots=[], at=f"{self.day}T18:01:00+08:00"),
            _funnel_row(self.day, "加发", 0, slots=[], at=f"{self.day}T20:01:00+08:00"),
        ])
        r = self.vm._check_slot_fill()
        self.assertIs(r["ok"], True, f"修复前那批被算进新判据了：{r}")


class TestBannedAndAdvisory(_Base):
    def test_banned_type_fails_without_sample_floor(self):
        """禁发类型是绝对判据：1 篇就够红，与样本量无关。"""
        _write_day(self.root, self.day, ["热点球评", "历史回顾"])
        r = self.vm._check_banned()
        self.assertIs(r["ok"], False, f"出现禁发类型却没红：{r}")

    def test_distribution_is_advisory_and_never_pending(self):
        """分布项必须 advisory，且不出现在 verify() 的 pending 里。"""
        _write_day(self.root, self.day, ["热点球评"] * 3 + ["数据对比"])
        c = self.vm._check_structure(7)
        self.assertTrue(c.get("advisory"), "分布是观测项，不该参与判定")
        self.assertIsNone(c["ok"])
        rep = self.vm.verify(7)
        self.assertNotIn("实际分布（观测）", rep["pending"],
                         "观测项进了 pending 就又是「攒样本」那套噪音")

    def test_distribution_reports_numbers_even_when_thin(self):
        """样本只有 4 篇也照常打印占比（观测项没有样本下限）。"""
        _write_day(self.root, self.day, ["热点球评"] * 3 + ["数据对比"])
        c = self.vm._check_structure(7)
        joined = " ".join(c["detail"])
        self.assertIn("25.0%", joined, f"数据型 1/4 应报 25.0%：{joined}")


if __name__ == "__main__":
    unittest.main()
