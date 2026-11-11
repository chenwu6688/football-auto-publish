# -*- coding: utf-8 -*-
"""结构验收的「未判定」必须有出口（2026-11-11）。

要钉死的缺陷：``verify_batch_mix.py`` 的模块文档与常量注释都写着
「累计 ≥20 篇且从未达标 → FAIL，堵『永远样本不足』的自我豁免」，
但 ``CUM_MIN_SAMPLE`` **在代码里从未被引用过** —— 承诺的出口只是注释。
只要把 ``--days`` 调小（或一直只跑单日窗口），这条检查就能永远 ⏸，
而 ⏸ 在报告里是绿色的（rc=0，只打 ::warning::）。

本文件守住三条：
1. ``CUM_MIN_SAMPLE`` 真的出现在 ``_check_structure`` 的代码里（AST 查 Name 载入，
   注释与文档字符串写一百遍也不算）；
2. 窗口样本不足、但累计样本够且不达标 → 必须判 FAIL，不能继续 ⏸；
3. 累计样本够且达标 → 保持 ⏸（豁免到期前不许误伤正在达标的批次）。
"""

from __future__ import annotations

import ast
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


def _write_day(root: Path, day: str, types) -> None:
    """造一天产出：带 batch_mix 字段 = 走过新结构的批次。"""
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "batch_mix": {"degraded": []},
        "articles": [{"title": f"{day}-{i}", "content_type": t}
                     for i, t in enumerate(types)],
    }
    (d / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


class TestCumulativeBackstop(unittest.TestCase):
    def setUp(self):
        self.vm = _load_verifier()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vm.OUTPUT_DIR = self.root
        self.today = datetime.now(CST).date()
        self.tsday = self.today.strftime("%Y-%m-%d")
        # 生效日推到 30 天前：累计窗口能覆盖到「历史那天」
        self.vm.SLOT_SINCE = (self.today - timedelta(days=30)).strftime("%Y-%m-%d")
        self.histday = (self.today - timedelta(days=5)).strftime("%Y-%m-%d")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cum_min_sample_is_really_used_in_code(self):
        """``CUM_MIN_SAMPLE`` 必须在 ``_check_structure`` 里被**求值**，不只是写在注释里。

        用 AST 查 Name 载入节点：注释与 docstring 不进 AST，写再多也不满足；
        而 ``CUM_MIN_SAMPLE = 20`` 这条赋值是 Store 而非 Load，同样不算。
        """
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_check_structure"), None)
        self.assertIsNotNone(fn, "找不到 _check_structure —— 验收脚本结构变了，本用例需同步")
        loads = {n.id for n in ast.walk(fn)
                 if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        self.assertIn(
            "CUM_MIN_SAMPLE", loads,
            "CUM_MIN_SAMPLE 只在注释/文档里被提到，代码里没有任何一处引用 —— "
            "「累计 ≥20 篇且从未达标 → FAIL」这条出口等于不存在，"
            "而窗口样本不足时的 ⏸ 会永远绿着")

    def test_thin_window_but_bad_cumulative_fails(self):
        """窗口 1 天样本不足，累计已 30 篇且全是战术榜单 → 判 FAIL，不再 ⏸。"""
        _write_day(self.root, self.histday, ["战术榜单"] * 24)
        _write_day(self.root, self.tsday, ["战术榜单"] * 6)
        r = self.vm._check_structure(1)
        self.assertIs(r["ok"], False,
                      f"累计 30 篇仍不达标却判 {r['ok']!r} —— 未判定变成了永久豁免：{r['detail']}")
        self.assertFalse(r.get("pending"), "判 FAIL 时不应再带 pending 标记")
        self.assertEqual(r["cumulative"]["total"], 30)

    def test_thin_window_but_good_cumulative_stays_pending(self):
        """累计够且达标 → 保持 ⏸（豁免到期前不许把正在达标的批次判红）。"""
        _write_day(self.root, self.histday, ["热点球评"] * 14 + ["数据对比"] * 10)
        _write_day(self.root, self.tsday, ["热点球评"] * 4 + ["数据对比"] * 2)
        r = self.vm._check_structure(1)
        self.assertIsNone(r["ok"], f"累计已达标却被判 {r['ok']!r}：{r['detail']}")
        self.assertTrue(r.get("pending"))
        self.assertEqual(r["cumulative"]["total"], 30)

    def test_pending_detail_reports_cumulative_progress(self):
        """未判定时要把「累计已几篇 / 达标线几篇」写进报告，否则读者无从判断还要等多久。"""
        _write_day(self.root, self.tsday, ["战术榜单"] * 4)
        r = self.vm._check_structure(1)
        self.assertIsNone(r["ok"])
        joined = " ".join(r["detail"])
        self.assertIn("累计已", joined)
        self.assertIn(str(self.vm.CUM_MIN_SAMPLE), joined)


class TestSinglePredicate(unittest.TestCase):
    def test_structure_predicate_is_not_duplicated(self):
        """达标判据只能有一处定义：窗口判定与累计硬线必须都调 ``_meets_target``。"""
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_check_structure")
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "_meets_target"]
        self.assertGreaterEqual(
            len(calls), 2,
            "窗口判定与累计硬线各用了一次达标判据，却只调了 "
            f"{len(calls)} 次 _meets_target —— 两处各写一套阈值，"
            "改一处忘另一处时报告会自相矛盾")


if __name__ == "__main__":
    unittest.main()
