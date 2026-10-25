#!/usr/bin/env python3
"""绩效反馈调权单测 —— 对准**现役实现** `orchestrator._apply_performance_boost()`。

历史背景（重要，别按「旧测试红了就删」处理）：

本文件原名测 `analyze_content_performance()` 与 `get_performance_boost()`，那属于
**旧乘数制**设计（各内容类型算一个乘数、clamp 到 1.5，并附带 top_keywords /
top_teams / top_players 关键词追踪）。这两个函数在
`90a9b29 refactor: 彻底简化架构，只保留 Pipeline A` 中被整体移除，现役链路改为
`_apply_performance_boost()` 的**加减制**：

    ratio = 该类型平均阅读 / 全局平均
    ratio > 1.2 → 权重 +0.3      ratio < 0.8 → 权重 -0.2
    结果 clamp 到 [0.3, 3.0]，且只调 weights 里已存在的类型

由 `load_season_weights()` 读完季节权重后当场叠加。旧测试因此长期 10 项全红——
是「测试还在测已删除的接口」，**不是功能缺失**。现在直接测现役规则，覆盖的仍是
同一个意图（历史效果 → 选题权重），覆盖面不降反升。

关键词追踪（top_keywords）在新架构里已无对应实现，属精简重构的取舍，不再断言。

⚠️ `_apply_performance_boost` 读写 `orchestrator.OUTPUT_DIR`。所有用例必须把它
patch 到临时目录——否则会读到真实的 output/performance_log.json 污染断言
（2026-10-25 的 test_season_weights 就是这么红的）。
"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

BASE_WEIGHTS = {"热点球评": 1.0, "转会资讯": 1.0, "战术解析": 1.0}


# ============================================================
# 测试夹具
# ============================================================

def _write_metadata(output_dir, date_str, types_by_index):
    """在 output_dir/date_str/metadata.json 写 index → content_type 映射。"""
    date_dir = Path(output_dir) / date_str
    date_dir.mkdir(parents=True, exist_ok=True)
    articles = [{"index": idx, "title": f"文章{idx}", "content_type": ct}
                for idx, ct in types_by_index.items()]
    (date_dir / "metadata.json").write_text(
        json.dumps({"total_articles": len(articles), "articles": articles},
                   ensure_ascii=False), encoding="utf-8")


def _write_perf_log(output_dir, rows):
    """rows: [(date, index, reads)] → output_dir/performance_log.json"""
    articles = {}
    for date_str, idx, reads in rows:
        articles[f"{date_str}/slug-{idx}"] = {"date": date_str, "index": idx,
                                              "reads": reads, "comments": 0, "likes": 0}
    (Path(output_dir) / "performance_log.json").write_text(
        json.dumps({"articles": articles}, ensure_ascii=False), encoding="utf-8")


def _boost(rows, types_by_index, weights=None):
    """在临时 OUTPUT_DIR 里跑一次现役调权，返回调整后的权重。"""
    import orchestrator as orch

    weights = dict(weights if weights is not None else BASE_WEIGHTS)
    orig_dir = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            _write_perf_log(tmpdir, rows)
            if types_by_index:
                by_date = {}
                for date_str, idx, _ in rows:
                    by_date.setdefault(date_str, {})[idx] = types_by_index.get(idx, "")
                for date_str, mapping in by_date.items():
                    _write_metadata(tmpdir, date_str, mapping)
            return orch._apply_performance_boost(weights)
    finally:
        orch.OUTPUT_DIR = orig_dir


# ============================================================
# 无数据 / 数据不足：权重必须原样返回
# ============================================================

def test_no_perf_log_keeps_weights():
    """performance_log.json 不存在 → 不动任何权重。"""
    import orchestrator as orch
    orig_dir = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            out = orch._apply_performance_boost(dict(BASE_WEIGHTS))
    finally:
        orch.OUTPUT_DIR = orig_dir
    assert out == BASE_WEIGHTS, f"无台账时不应改权重: {out}"
    print("  PASS test_no_perf_log_keeps_weights")


def test_empty_articles_keeps_weights():
    """articles 为空字典 → 不动权重。"""
    out = _boost([], {})
    assert out == BASE_WEIGHTS, f"空台账不应改权重: {out}"
    print("  PASS test_empty_articles_keeps_weights")


def test_zero_reads_keeps_weights():
    """reads 全为 0（导出了但后台还没结算）→ 视为无数据，不动权重。

    后台未结算时导出全是 0。若把 0 当真实低绩效，会把所有类型一起下调，
    既是无意义抖动，也违反「缺失不得当 0 用」的口径。
    """
    rows = [("2026-06-01", 1, 0), ("2026-06-01", 2, 0), ("2026-06-01", 3, 0)]
    out = _boost(rows, {1: "热点球评", 2: "转会资讯", 3: "战术解析"})
    assert out == BASE_WEIGHTS, f"全 0 阅读不应改权重: {out}"
    print("  PASS test_zero_reads_keeps_weights")


# ============================================================
# 加减制核心规则
# ============================================================

def test_high_reads_boosted_low_reads_penalised():
    """ratio>1.2 → +0.3；ratio<0.8 → -0.2。"""
    # 类型均值 5000 / 1000 / 1000，全局均值 2333.3：
    #   热点球评 ratio 2.14 → +0.3，另两个 ratio 0.43 → -0.2
    rows = [("2026-06-01", 1, 5000), ("2026-06-01", 2, 1000), ("2026-06-01", 3, 1000)]
    out = _boost(rows, {1: "热点球评", 2: "转会资讯", 3: "战术解析"})
    assert abs(out["热点球评"] - 1.3) < 1e-9, f"高阅读应 +0.3，实际 {out['热点球评']}"
    assert abs(out["转会资讯"] - 0.8) < 1e-9, f"低阅读应 -0.2，实际 {out['转会资讯']}"
    assert abs(out["战术解析"] - 0.8) < 1e-9
    print("  PASS test_high_reads_boosted_low_reads_penalised")


def test_mid_range_ratio_untouched():
    """0.8 <= ratio <= 1.2 是死区，权重不变。"""
    # 均值 1000 / 1000 / 1200，全局 1066.7：ratio 0.94 / 0.94 / 1.13 全在死区
    rows = [("2026-06-01", 1, 1000), ("2026-06-01", 2, 1000), ("2026-06-01", 3, 1200)]
    out = _boost(rows, {1: "热点球评", 2: "转会资讯", 3: "战术解析"})
    assert out == BASE_WEIGHTS, f"死区内不应调整: {out}"
    print("  PASS test_mid_range_ratio_untouched")


def test_clamped_at_upper_bound():
    """权重已在上限 3.0，即使高阅读也不得超过 3.0。"""
    rows = [("2026-06-01", 1, 9000), ("2026-06-01", 2, 100), ("2026-06-01", 3, 100)]
    out = _boost(rows, {1: "热点球评", 2: "转会资讯", 3: "战术解析"},
                 weights={"热点球评": 3.0, "转会资讯": 1.0, "战术解析": 1.0})
    assert out["热点球评"] == 3.0, f"应被 clamp 在 3.0，实际 {out['热点球评']}"
    print("  PASS test_clamped_at_upper_bound")


def test_clamped_at_lower_bound():
    """权重已在下限 0.3，即使低阅读也不得低于 0.3。"""
    rows = [("2026-06-01", 1, 200), ("2026-06-01", 2, 900), ("2026-06-01", 3, 900)]
    out = _boost(rows, {1: "热点球评", 2: "转会资讯", 3: "战术解析"},
                 weights={"热点球评": 0.3, "转会资讯": 1.0, "战术解析": 1.0})
    assert out["热点球评"] == 0.3, f"应被 clamp 在 0.3，实际 {out['热点球评']}"
    print("  PASS test_clamped_at_lower_bound")


def test_unknown_content_type_not_added():
    """台账里出现 weights 之外的内容类型 → 不新增 key（不凭空造权重项）。"""
    rows = [("2026-06-01", 1, 5000), ("2026-06-01", 2, 1000), ("2026-06-01", 3, 1000)]
    out = _boost(rows, {1: "热点球评", 2: "转会资讯", 3: "排行榜"})
    assert "排行榜" not in out, f"不应凭空新增未配置的类型: {out}"
    assert set(out) == set(BASE_WEIGHTS), f"键集合不应变化: {out}"
    print("  PASS test_unknown_content_type_not_added")


def test_aggregates_multiple_articles_per_type():
    """同一类型多篇取平均（不是求和——否则高产类型的篇数会冒充质量）。"""
    # 热点球评 [1000, 3000] → 均值 2000；转会 1000；战术 1000
    # 全局均值 1333.3 → 热点球评 ratio 1.5 → +0.3
    rows = [("2026-06-01", 1, 1000), ("2026-06-01", 2, 3000),
            ("2026-06-01", 3, 1000), ("2026-06-01", 4, 1000)]
    out = _boost(rows, {1: "热点球评", 2: "热点球评", 3: "转会资讯", 4: "战术解析"})
    assert abs(out["热点球评"] - 1.3) < 1e-9, f"应按均值 +0.3，实际 {out['热点球评']}"
    print("  PASS test_aggregates_multiple_articles_per_type")


# ============================================================
# 容错：脏台账不能把主流程带崩
# ============================================================

def test_missing_metadata_keeps_weights():
    """有 performance_log 但同日 metadata 缺失 → 跳过，不崩不改。"""
    rows = [("2026-06-01", 1, 5000)]
    out = _boost(rows, {})      # types_by_index 为空 → 不写 metadata
    assert out == BASE_WEIGHTS, f"metadata 缺失时应原样返回: {out}"
    print("  PASS test_missing_metadata_keeps_weights")


def test_corrupt_metadata_keeps_weights():
    """metadata.json 是坏 JSON → 异常吞掉，权重不变，不抛给调用方。"""
    import orchestrator as orch
    orig_dir = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            _write_perf_log(tmpdir, [("2026-06-01", 1, 5000)])
            d = Path(tmpdir) / "2026-06-01"
            d.mkdir(parents=True, exist_ok=True)
            (d / "metadata.json").write_text("{not json", encoding="utf-8")
            out = orch._apply_performance_boost(dict(BASE_WEIGHTS))
    finally:
        orch.OUTPUT_DIR = orig_dir
    assert out == BASE_WEIGHTS, f"坏 metadata 不应改权重: {out}"
    print("  PASS test_corrupt_metadata_keeps_weights")


# ============================================================
# 集成：确实现役链路真的调了它
# ============================================================

def test_load_season_weights_applies_boost():
    """load_season_weights 必须真正调用 _apply_performance_boost。

    只断言「读到了 config」不够：调权这一步一旦被悄悄摘掉，绩效反馈就整条
    断链，而季节权重测试照样全绿——那才是最危险的静默失效。
    """
    import orchestrator as orch
    calls = []
    real = orch._apply_performance_boost

    def spy(weights):
        calls.append(dict(weights))
        return real(weights)

    orig_dir, orig_fn = orch.OUTPUT_DIR, orch._apply_performance_boost
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)     # 空目录：调权原样返回，但能被观测到
            orch._apply_performance_boost = spy
            weights, label = orch.load_season_weights("2026-06-02")
    finally:
        orch.OUTPUT_DIR, orch._apply_performance_boost = orig_dir, orig_fn

    assert weights is not None, "应能从真实 config.yaml 读到季节权重"
    assert label, "应返回季节标签"
    assert calls, "load_season_weights 未调用 _apply_performance_boost，绩效反馈断链"
    print(f"  PASS test_load_season_weights_applies_boost (label={label})")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("绩效反馈调权测试（现役 _apply_performance_boost）")
    print("=" * 60)

    all_tests = [
        ("no perf_log → 权重不变", test_no_perf_log_keeps_weights),
        ("空 articles → 权重不变", test_empty_articles_keeps_weights),
        ("全 0 阅读 → 权重不变", test_zero_reads_keeps_weights),
        ("高阅读 +0.3 / 低阅读 -0.2", test_high_reads_boosted_low_reads_penalised),
        ("死区 0.8~1.2 不变", test_mid_range_ratio_untouched),
        ("clamp 上限 3.0", test_clamped_at_upper_bound),
        ("clamp 下限 0.3", test_clamped_at_lower_bound),
        ("未配置类型不新增", test_unknown_content_type_not_added),
        ("同类型多篇取均值", test_aggregates_multiple_articles_per_type),
        ("metadata 缺失 → 不改", test_missing_metadata_keeps_weights),
        ("metadata 损坏 → 不崩", test_corrupt_metadata_keeps_weights),
        ("load_season_weights 真调调权", test_load_season_weights_applies_boost),
    ]

    passed = failed = 0
    for name, fn in all_tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    sys.exit(1 if failed else 0)
