#!/usr/bin/env python3
"""Unit tests for performance feedback loop (Task #11).

Tests:
  1. analyze_content_performance() aggregates metadata correctly
  2. get_performance_boost() computes correct boost multipliers
  3. Performance + season weights integration
  4. Empty data handling
"""

import sys, os, json, tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def create_metadata_with_performance(output_dir, date_str, articles):
    """Create metadata.json files with performance data."""
    date_dir = Path(output_dir) / date_str
    date_dir.mkdir(parents=True, exist_ok=True)
    meta = {"total_articles": len(articles), "articles": articles}
    meta_path = date_dir / "metadata.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return meta_path


def test_analyze_performance_empty():
    """No metadata → empty results."""
    import orchestrator as orch
    orig = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            result = orch.analyze_content_performance("2026-06-02", lookback_days=30)
            assert result["performance"] == {}
            assert result["type_stats"] == {}
            assert result["top_keywords"] == []
    finally:
        orch.OUTPUT_DIR = orig
    print("  PASS test_analyze_performance_empty")


def test_analyze_performance_basic():
    """Articles with no performance data → base score 1.0 each."""
    import orchestrator as orch
    orig = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            date_str = "2026-06-01"
            articles = [
                {"title": "阿森纳点球输巴黎", "content_type": "热点球评",
                 "keywords": ["Arsenal", "PSG"], "tags": ["UCL"]},
                {"title": "姆巴佩转会最新", "content_type": "转会资讯",
                 "keywords": ["Mbappe", "transfer"], "tags": ["transfer"]},
                {"title": "战术分析巴黎", "content_type": "战术解析",
                 "keywords": ["tactics", "PSG"], "tags": ["analysis"]},
                {"title": "又一条转会消息", "content_type": "转会资讯",
                 "keywords": ["transfer2"], "tags": ["transfer"]},
            ]
            create_metadata_with_performance(tmpdir, date_str, articles)

            result = orch.analyze_content_performance("2026-06-02", lookback_days=30)
            perf = result["performance"]
            assert len(perf) == 3  # 3 content types
            # All base score = 1.0 (no reads/comments)
            assert perf["热点球评"] == 1.0
            assert perf["战术解析"] == 1.0
            # 转会资讯 has 2 articles, avg = 2.0/2 = 1.0
            assert perf["转会资讯"] == 1.0
            # type_stats counts
            assert result["type_stats"]["转会资讯"]["count"] == 2
            assert result["type_stats"]["热点球评"]["count"] == 1
    finally:
        orch.OUTPUT_DIR = orig
    print("  PASS test_analyze_performance_basic")


def test_analyze_performance_with_reads():
    """Articles with performance data → weighted scores."""
    import orchestrator as orch
    orig = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            date_str = "2026-06-01"
            articles = [
                {"title": "热点文章", "content_type": "热点球评", "keywords": [],
                 "performance": {"reads": 5000, "comments": 50}},
                {"title": "转会文章", "content_type": "转会资讯", "keywords": [],
                 "performance": {"reads": 1000, "comments": 5}},
            ]
            create_metadata_with_performance(tmpdir, date_str, articles)

            result = orch.analyze_content_performance("2026-06-02", lookback_days=30)
            perf = result["performance"]

            # 热点球评: 1 + 5000/1000 + 50/10 = 1 + 5 + 5 = 11.0
            assert perf["热点球评"] == 11.0, f"Expected 11.0, got {perf['热点球评']}"
            # 转会资讯: 1 + 1000/1000 + 5/10 = 1 + 1 + 0.5 = 2.5
            assert perf["转会资讯"] == 2.5, f"Expected 2.5, got {perf['转会资讯']}"
    finally:
        orch.OUTPUT_DIR = orig
    print("  PASS test_analyze_performance_with_reads")


def test_get_performance_boost_balanced():
    """Equal performance → neutral boosts (1.0)."""
    import orchestrator as orch
    perf_data = {
        "performance": {"热点球评": 1.0, "转会资讯": 1.0, "战术解析": 1.0},
        "type_stats": {"热点球评": {"count": 1}, "转会资讯": {"count": 1}, "战术解析": {"count": 1}},
    }
    boosts = orch.get_performance_boost(perf_data)
    for ct, b in boosts.items():
        assert abs(b - 1.0) < 0.01, f"{ct}: expected ~1.0, got {b}"
    print("  PASS test_get_performance_boost_balanced")


def test_get_performance_boost_uneven():
    """Uneven performance → boosts reflect ratios."""
    import orchestrator as orch
    perf_data = {
        "performance": {"热点球评": 11.0, "转会资讯": 2.5},
        "type_stats": {"热点球评": {"count": 1}, "转会资讯": {"count": 1}},
    }
    boosts = orch.get_performance_boost(perf_data)
    # avg = (11 + 2.5) / 2 = 6.75
    # 热点球评: 11/6.75 = 1.63 → clamped to 1.5
    # 转会资讯: 2.5/6.75 = 0.37 → clamped to 0.5
    assert boosts["热点球评"] >= 1.4, f"Expected high boost for 热点球评, got {boosts}"
    assert boosts["转会资讯"] <= 0.6, f"Expected low boost for 转会资讯, got {boosts}"
    print(f"  PASS test_get_performance_boost_uneven (热点球评:{boosts['热点球评']}, 转会资讯:{boosts['转会资讯']})")


def test_get_performance_boost_empty():
    """Empty performance → empty boosts."""
    import orchestrator as orch
    boosts = orch.get_performance_boost({"performance": {}, "type_stats": {}})
    assert boosts == {}
    print("  PASS test_get_performance_boost_empty")


def test_performance_season_weights_merge():
    """Performance boost should combine with season weights."""
    season_weights = {"热点球评": 2.0, "转会资讯": 0.8, "排行榜": 1.0, "八卦趣事": 0.8, "战术解析": 1.5}
    performance_boost = {"热点球评": 1.2, "转会资讯": 0.6, "战术解析": 1.0}  # 排行榜 and 八卦趣事 not in perf data

    # Merge: effective = season_weight * performance_boost
    effective = dict(season_weights)
    for ct, boost in performance_boost.items():
        if ct in effective:
            effective[ct] = round(effective[ct] * boost, 2)

    assert effective["热点球评"] == 2.4   # 2.0 * 1.2
    assert effective["转会资讯"] == 0.48  # 0.8 * 0.6
    assert effective["战术解析"] == 1.5   # 1.5 * 1.0
    assert effective["排行榜"] == 1.0     # unchanged (not in boost)
    assert effective["八卦趣事"] == 0.8   # unchanged (not in boost)
    print("  PASS test_performance_season_weights_merge")


def test_analyze_performance_keyword_tracking():
    """Performance analysis should track keyword/team/player frequency."""
    import orchestrator as orch
    orig = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            date_str = "2026-06-01"
            articles = [
                {"title": "阿森纳逆转巴黎夺欧冠", "content_type": "热点球评",
                 "keywords": ["Arsenal", "PSG", "comeback"], "tags": ["UCL", "final"]},
                {"title": "姆巴佩帽子戏法震惊世界", "content_type": "热点球评",
                 "keywords": ["Mbappe", "hattrick", "PSG"], "tags": ["Ligue1"]},
                {"title": "阿森纳防线崩溃的三大原因", "content_type": "战术解析",
                 "keywords": ["Arsenal", "defense", "tactics"], "tags": ["analysis"]},
            ]
            create_metadata_with_performance(tmpdir, date_str, articles)

            result = orch.analyze_content_performance("2026-06-02", lookback_days=30)

            # Keyword frequency (lowercase)
            assert ("arsenal", 2) in result["top_keywords"] or ("arsenal", 3) in result["top_keywords"]
            # Teams in titles
            assert ("阿森纳", 2) in result["top_teams"] or ("阿森纳", 3) in result["top_teams"]
            assert ("巴黎", 1) in result["top_teams"]
            # Players in titles
            assert ("姆巴佩", 1) in result["top_players"]
    finally:
        orch.OUTPUT_DIR = orig
    print("  PASS test_analyze_performance_keyword_tracking")


def test_import_functions():
    """All performance functions should be importable."""
    from orchestrator import analyze_content_performance, get_performance_boost
    assert callable(analyze_content_performance)
    assert callable(get_performance_boost)
    print("  PASS test_import_functions")


def test_performance_lookback_respected():
    """Only articles within lookback window should be counted."""
    import orchestrator as orch
    orig = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            # Create article from 40 days ago (outside 30-day lookback)
            old_date = (datetime.strptime("2026-06-02", "%Y-%m-%d") - timedelta(days=40)).strftime("%Y-%m-%d")
            articles_old = [
                {"title": "旧文章", "content_type": "八卦趣事", "keywords": ["old"],
                 "performance": {"reads": 100000, "comments": 1000}},
            ]
            create_metadata_with_performance(tmpdir, old_date, articles_old)

            # Create article from 5 days ago (inside 30-day lookback)
            recent_date = (datetime.strptime("2026-06-02", "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
            articles_recent = [
                {"title": "新文章", "content_type": "热点球评", "keywords": ["new"]},
            ]
            create_metadata_with_performance(tmpdir, recent_date, articles_recent)

            result = orch.analyze_content_performance("2026-06-02", lookback_days=30)
            perf = result["performance"]

            # Only 热点球评 (recent) should appear; 八卦趣事 (40 days ago) should be excluded
            assert "热点球评" in perf
            assert "八卦趣事" not in perf
    finally:
        orch.OUTPUT_DIR = orig
    print("  PASS test_performance_lookback_respected")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Task #11 Unit Tests: 头条数据反馈闭环")
    print("=" * 60)

    all_tests = [
        ("analyze: empty metadata → empty results", test_analyze_performance_empty),
        ("analyze: basic articles → base scores 1.0", test_analyze_performance_basic),
        ("analyze: with reads/comments → weighted scores", test_analyze_performance_with_reads),
        ("analyze: keyword/team/player frequency tracking", test_analyze_performance_keyword_tracking),
        ("analyze: lookback window respected", test_performance_lookback_respected),
        ("boost: equal performance → neutral 1.0", test_get_performance_boost_balanced),
        ("boost: uneven performance → reflects ratios", test_get_performance_boost_uneven),
        ("boost: empty data → empty dict", test_get_performance_boost_empty),
        ("merge: performance + season weights combined", test_performance_season_weights_merge),
        ("import: functions importable", test_import_functions),
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
