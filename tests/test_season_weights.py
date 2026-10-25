#!/usr/bin/env python3
"""Unit tests for season weights integration (Task #9).

Tests:
  1. load_season_weights() returns correct weights for each season period
  2. Weight-based batch type adjustment logic
  3. Weight hint string formatting for LLM prompts
  4. Edge cases: missing config, invalid dates, empty weights
"""

import sys, os, json, tempfile, yaml
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# --- Helpers to simulate the weight adjustment logic (extracted from orchestrator) ---

SEASON_WEIGHTS_CONFIG = {
    "season_weights": [
        {"months": [8, 9], "weights": {"热点球评": 1.0, "转会资讯": 2.0, "排行榜": 1.0, "八卦趣事": 1.2, "战术解析": 0.8}, "label": "夏季转会窗"},
        {"months": [3, 4, 5], "weights": {"热点球评": 2.0, "转会资讯": 0.8, "排行榜": 1.2, "八卦趣事": 0.8, "战术解析": 1.5}, "label": "赛季争冠冲刺期"},
        {"months": [6, 7], "weights": {"热点球评": 0.5, "转会资讯": 1.5, "排行榜": 1.5, "八卦趣事": 2.0, "战术解析": 0.5}, "label": "休赛期"},
        {"months": [1], "weights": {"热点球评": 1.0, "转会资讯": 2.0, "排行榜": 1.0, "八卦趣事": 1.2, "战术解析": 0.8}, "label": "冬季转会窗"},
        {"months": [2, 10, 11, 12], "weights": {"热点球评": 1.0, "转会资讯": 1.0, "排行榜": 1.0, "八卦趣事": 1.0, "战术解析": 1.0}, "label": "常规赛季"},
    ]
}

BATCH_TYPES = {
    "morning": ["热点球评", "八卦趣事"],
    "noon": ["转会资讯", "排行榜"],
    "evening": ["战术解析", "八卦趣事"],
}

ALL_TYPES = ["八卦趣事", "转会资讯", "战术解析", "热点球评", "排行榜"]


def load_season_weights_from_config(cfg, date_str=None):
    """Replica of orchestrator.load_season_weights() for testing."""
    season_weights = cfg.get("season_weights", [])
    if not season_weights:
        return None
    dt = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now()
    month = dt.month
    for period in season_weights:
        if month in period.get("months", []):
            return period.get("weights", {})
    return {"热点球评": 1.0, "转会资讯": 1.0, "排行榜": 1.0, "八卦趣事": 1.0, "战术解析": 1.0}


def apply_season_weight_adjustment(target_types, season_weights):
    """Replica of the batch type adjustment logic in orchestrator.main()."""
    if not season_weights:
        return list(target_types), []
    adjusted = list(target_types)
    swaps = []
    for i, ct in enumerate(adjusted):
        w = season_weights.get(ct, 1.0)
        if w < 0.7:
            candidates = sorted(season_weights.items(), key=lambda x: -x[1])
            for alt_type, alt_w in candidates:
                if alt_w > 1.3 and alt_type not in adjusted:
                    swaps.append((ct, w, alt_type, alt_w))
                    adjusted[i] = alt_type
                    break
    return adjusted, swaps


def build_weight_hint(season_weights):
    """Replica of the weight_hint building logic."""
    if not season_weights:
        return ""
    high_types = [f"{ct}({w:.1f})" for ct, w in sorted(season_weights.items(), key=lambda x: -x[1]) if w >= 1.2]
    low_types = [f"{ct}({w:.1f})" for ct, w in sorted(season_weights.items(), key=lambda x: x[1]) if w < 0.8]
    hint = ""
    if high_types or low_types:
        hint = "\n## 赛季权重指引\n"
        if high_types:
            hint += f"优先选择: {', '.join(high_types)}\n"
        if low_types:
            hint += f"降低频率: {', '.join(low_types)}\n"
    return hint


# ============================================================
# Tests
# ============================================================

def test_load_season_weights_june_world_cup():
    """June (month 6) should return 休赛期 weights."""
    weights = load_season_weights_from_config(SEASON_WEIGHTS_CONFIG, "2026-06-02")
    assert weights is not None
    assert weights["热点球评"] == 0.5, f"Expected 0.5 for 热点球评, got {weights['热点球评']}"
    assert weights["八卦趣事"] == 2.0, f"Expected 2.0 for 八卦趣事, got {weights['八卦趣事']}"
    assert weights["转会资讯"] == 1.5
    assert weights["战术解析"] == 0.5
    assert weights["排行榜"] == 1.5
    print("  PASS test_load_season_weights_june_world_cup")


def test_load_season_weights_august_transfer():
    """August (month 8) should return 夏季转会窗 weights."""
    weights = load_season_weights_from_config(SEASON_WEIGHTS_CONFIG, "2026-08-15")
    assert weights is not None
    assert weights["转会资讯"] == 2.0, f"Expected 2.0 for 转会资讯, got {weights['转会资讯']}"
    assert weights["热点球评"] == 1.0
    assert weights["战术解析"] == 0.8
    print("  PASS test_load_season_weights_august_transfer")


def test_load_season_weights_march_title_run():
    """March (month 3) should return 赛季争冠冲刺期 weights."""
    weights = load_season_weights_from_config(SEASON_WEIGHTS_CONFIG, "2026-03-10")
    assert weights is not None
    assert weights["热点球评"] == 2.0
    assert weights["战术解析"] == 1.5
    assert weights["转会资讯"] == 0.8
    assert weights["八卦趣事"] == 0.8
    print("  PASS test_load_season_weights_march_title_run")


def test_load_season_weights_february_default():
    """February (month 2) should return 常规赛季 (default) weights."""
    weights = load_season_weights_from_config(SEASON_WEIGHTS_CONFIG, "2026-02-10")
    assert weights is not None
    assert weights["热点球评"] == 1.0
    assert weights["转会资讯"] == 1.0
    assert weights["排行榜"] == 1.0
    assert weights["八卦趣事"] == 1.0
    assert weights["战术解析"] == 1.0
    print("  PASS test_load_season_weights_february_default")


def test_load_season_weights_january_winter():
    """January (month 1) should return 冬季转会窗 weights."""
    weights = load_season_weights_from_config(SEASON_WEIGHTS_CONFIG, "2026-01-20")
    assert weights is not None
    assert weights["转会资讯"] == 2.0
    assert weights["热点球评"] == 1.0
    print("  PASS test_load_season_weights_january_winter")


def test_load_season_weights_all_months():
    """Every month 1-12 should return a valid weight dict."""
    for month in range(1, 13):
        date_str = f"2026-{month:02d}-15"
        weights = load_season_weights_from_config(SEASON_WEIGHTS_CONFIG, date_str)
        assert weights is not None, f"No weights for month {month}"
        assert len(weights) == 5, f"Expected 5 categories, got {len(weights)} for month {month}"
        for ct in ["热点球评", "转会资讯", "排行榜", "八卦趣事", "战术解析"]:
            assert ct in weights, f"Missing {ct} in month {month}"
            assert isinstance(weights[ct], (int, float)), f"Non-numeric weight for {ct} in month {month}"
    print("  PASS test_load_season_weights_all_months")


def test_weight_adjustment_offseason_morning():
    """In June (休赛期), morning batch [热点球评(0.5), 八卦趣事(2.0)] should swap 热点球评 out."""
    weights = load_season_weights_from_config(SEASON_WEIGHTS_CONFIG, "2026-06-02")
    target_types = ["热点球评", "八卦趣事"]
    adjusted, swaps = apply_season_weight_adjustment(target_types, weights)
    assert len(swaps) > 0, f"Expected at least 1 swap in June morning batch, got {swaps}"
    swapped_out = swaps[0][0]
    assert swapped_out == "热点球评", f"Expected 热点球评 to be swapped, got {swapped_out}"
    assert "热点球评" not in adjusted, f"热点球评 (weight 0.5) should have been removed: {adjusted}"
    # Should have been replaced by a high-weight type
    new_type = swaps[0][2]
    assert weights.get(new_type, 1.0) > 1.3, f"Replacement {new_type} should have weight > 1.3"
    print(f"  PASS test_weight_adjustment_offseason_morning (swap: {swapped_out} → {new_type})")


def test_weight_adjustment_title_run_morning():
    """In March (争冠期), morning batch [热点球评(2.0), 八卦趣事(0.8)] should swap 八卦趣事 out."""
    weights = load_season_weights_from_config(SEASON_WEIGHTS_CONFIG, "2026-03-10")
    target_types = ["热点球评", "八卦趣事"]
    adjusted, swaps = apply_season_weight_adjustment(target_types, weights)
    # 八卦趣事 weight 0.8 is NOT < 0.7, so no swap should happen
    assert len(swaps) == 0, f"Expected no swaps (八卦趣事 weight 0.8 >= 0.7 threshold): {swaps}"
    print("  PASS test_weight_adjustment_title_run_morning (no swap, weight 0.8 above threshold)")


def test_weight_adjustment_no_swap_when_balanced():
    """In February (常规赛季), all weights are 1.0 — no swaps should occur."""
    weights = load_season_weights_from_config(SEASON_WEIGHTS_CONFIG, "2026-02-10")
    target_types = ["热点球评", "八卦趣事"]
    adjusted, swaps = apply_season_weight_adjustment(target_types, weights)
    assert len(swaps) == 0, f"Expected no swaps in balanced season: {swaps}"
    assert adjusted == target_types
    print("  PASS test_weight_adjustment_no_swap_when_balanced")


def test_weight_adjustment_none_weights():
    """If season_weights is None, adjustment should be a no-op."""
    adjusted, swaps = apply_season_weight_adjustment(["热点球评", "八卦趣事"], None)
    assert len(swaps) == 0
    assert adjusted == ["热点球评", "八卦趣事"]
    print("  PASS test_weight_adjustment_none_weights")


def test_weight_hint_formatting():
    """Weight hint should properly format high and low priority types."""
    weights = {"热点球评": 0.5, "转会资讯": 1.5, "排行榜": 1.5, "八卦趣事": 2.0, "战术解析": 0.5}
    hint = build_weight_hint(weights)
    assert "赛季权重指引" in hint
    assert "优先选择" in hint
    assert "降低频率" in hint
    # High weight types (>=1.2)
    assert "八卦趣事(2.0)" in hint
    assert "转会资讯(1.5)" in hint
    # Low weight types (<0.8)
    assert "热点球评(0.5)" in hint
    assert "战术解析(0.5)" in hint
    print("  PASS test_weight_hint_formatting")


def test_weight_hint_balanced():
    """Balanced weights (all 1.0) should produce empty hint."""
    weights = {"热点球评": 1.0, "转会资讯": 1.0, "排行榜": 1.0, "八卦趣事": 1.0, "战术解析": 1.0}
    hint = build_weight_hint(weights)
    assert hint == "", f"Expected empty hint for balanced weights, got: {hint}"
    print("  PASS test_weight_hint_balanced")


def test_weight_hint_none():
    """None weights should produce empty hint."""
    hint = build_weight_hint(None)
    assert hint == ""
    print("  PASS test_weight_hint_none")


def test_orchestrator_function_exists():
    """Verify load_season_weights is importable from orchestrator."""
    from orchestrator import load_season_weights
    assert callable(load_season_weights)
    print("  PASS test_orchestrator_function_exists")


def _expected_weights_for_month(month):
    """从真实 config/config.yaml 读出该月份应得的权重与标签（不写死数值）。

    写死倍数的断言在每次调参后都会变红，逼人去改测试——真正该锁的是
    「按月份匹配到正确的季节段」这个逻辑，而不是某个具体倍数。
    """
    cfg_path = Path(__file__).parent.parent / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    for period in cfg.get("season_weights", []):
        if month in period.get("months", []):
            return dict(period.get("weights", {})), period.get("label", "")
    return ({"热点球评": 1.0, "转会资讯": 1.0, "排行榜": 1.0,
             "八卦趣事": 1.0, "战术解析": 1.0}, "常规赛季")


def test_orchestrator_real_config():
    """load_season_weights 必须按月份匹配到 config.yaml 里正确的季节段。

    两个坑：
    1. 它内部会调 `_apply_performance_boost()`，后者读 output/performance_log.json
       对权重做 ±0.3 微调。真实台账一旦有数据，这里「config 原值」的断言必然红
       （2026-10-25 现场：八卦趣事 1.0 → 0.8，测试假红一整周）。必须把
       OUTPUT_DIR 隔离到空目录。
    2. 断言别写死具体倍数——调参是日常操作。改成与 config.yaml 同月份配置动态
       对照，验证「选对季节段」这个逻辑本身。
    """
    import orchestrator as orch
    from orchestrator import load_season_weights

    orig_dir = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)      # 空目录 → 绩效调权原样返回

            for date_str, month in (("2026-06-02", 6), ("2026-08-15", 8),
                                    ("2026-03-01", 3), ("2026-12-25", 12)):
                exp_w, exp_label = _expected_weights_for_month(month)
                got_w, got_label = load_season_weights(date_str)
                assert got_w is not None, f"{date_str} 应能从真实 config.yaml 读到权重"
                assert got_w == exp_w, f"{date_str}(月{month}) 权重不符: {got_w} != {exp_w}"
                assert got_label == exp_label, f"{date_str} 标签不符: {got_label} != {exp_label}"

            # 分段必须真的生效：6 月（世界杯月）与 8 月（新赛季）权重不能一样
            w_june, _ = load_season_weights("2026-06-02")
            w_aug, _ = load_season_weights("2026-08-15")
            assert w_june != w_aug, "6 月与 8 月权重相同——季节分段可能失效或 config 被压平"
    finally:
        orch.OUTPUT_DIR = orig_dir

    print("  PASS test_orchestrator_real_config")


def test_batch_type_adjustment_all_batches_offseason():
    """All 3 batches in June should have low-weight types swapped."""
    weights = load_season_weights_from_config(SEASON_WEIGHTS_CONFIG, "2026-06-02")

    results = {}
    for batch_name, types in BATCH_TYPES.items():
        adjusted, swaps = apply_season_weight_adjustment(types, weights)
        results[batch_name] = {"original": types, "adjusted": adjusted, "swaps": swaps}

    # Morning: 热点球评(0.5) should be swapped
    assert len(results["morning"]["swaps"]) > 0
    # Noon: 转会资讯(1.5) and 排行榜(1.5) — no swaps needed
    assert len(results["noon"]["swaps"]) == 0
    # Evening: 战术解析(0.5) should be swapped
    assert len(results["evening"]["swaps"]) > 0
    assert "战术解析" not in results["evening"]["adjusted"]

    print(f"  PASS test_batch_type_adjustment_all_batches_offseason ({results['morning']['swaps']}, {results['evening']['swaps']})")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Task #9 Unit Tests: 赛季权重接入 orchestrator")
    print("=" * 60)

    all_tests = [
        # load_season_weights accuracy
        ("load_season_weights: June → 世界杯月", test_load_season_weights_june_world_cup),
        ("load_season_weights: August → 夏季转会窗", test_load_season_weights_august_transfer),
        ("load_season_weights: March → 争冠冲刺期", test_load_season_weights_march_title_run),
        ("load_season_weights: February → 常规赛季", test_load_season_weights_february_default),
        ("load_season_weights: January → 冬季转会窗", test_load_season_weights_january_winter),
        ("load_season_weights: All 12 months valid", test_load_season_weights_all_months),
        # Weight-based batch adjustment
        ("adjustment: June morning swaps 热点球评 out", test_weight_adjustment_offseason_morning),
        ("adjustment: March no swap (weight 0.8 ≥ 0.7)", test_weight_adjustment_title_run_morning),
        ("adjustment: February balanced no swap", test_weight_adjustment_no_swap_when_balanced),
        ("adjustment: None weights is no-op", test_weight_adjustment_none_weights),
        # Weight hint formatting
        ("weight_hint: formats high/low types", test_weight_hint_formatting),
        ("weight_hint: balanced → empty", test_weight_hint_balanced),
        ("weight_hint: None → empty", test_weight_hint_none),
        # Integration
        ("orchestrator: function importable", test_orchestrator_function_exists),
        ("orchestrator: real config.yaml", test_orchestrator_real_config),
        ("batch_adjustment: all 3 batches in June", test_batch_type_adjustment_all_batches_offseason),
    ]

    passed = 0
    failed = 0
    for name, test_fn in all_tests:
        try:
            test_fn()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    if failed > 0:
        print("SOME TESTS FAILED!")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED!")
        sys.exit(0)
