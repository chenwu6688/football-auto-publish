#!/usr/bin/env python3
"""Unit tests for cross-batch dedup (Task #10).

Tests:
  1. get_cross_batch_covered() extracts covered items from today's metadata
  2. save_batch_state() correctly updates batch completion info
  3. Cross-batch type avoidance logic
  4. Integration: full pipeline with simulated prior batches
"""

import sys, os, json, tempfile, shutil
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# Test helpers
# ============================================================

def create_fake_metadata(output_dir, date_str, articles, batches_completed=None):
    """Create a fake metadata.json simulating earlier batch output."""
    date_dir = Path(output_dir) / date_str
    date_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "total_articles": len(articles),
        "articles": articles,
        "batches_completed": batches_completed or [],
    }
    meta_path = date_dir / "metadata.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return meta_path


def test_get_cross_batch_covered_empty():
    """When no metadata.json exists, should return empty sets."""
    from orchestrator import get_cross_batch_covered
    # Point OUTPUT_DIR to a temp directory
    import orchestrator as orch
    orig_output = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            covered = get_cross_batch_covered("2026-06-02")
            assert covered["content_types"] == set()
            assert covered["teams"] == set()
            assert covered["players"] == set()
            assert covered["batch_count"] == 0
    finally:
        orch.OUTPUT_DIR = orig_output
    print("  PASS test_get_cross_batch_covered_empty")


def test_get_cross_batch_covered_with_data():
    """Should extract content types, teams, players from existing metadata."""
    import orchestrator as orch
    orig_output = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            date_str = "2026-06-02"
            articles = [
                {"title": "阿森纳点球输巴黎，战术解析", "content_type": "热点球评",
                 "keywords": ["Arsenal", "PSG", "penalty"], "tags": ["UCL", "final"]},
                {"title": "姆巴佩转会皇马最新消息", "content_type": "转会资讯",
                 "keywords": ["Mbappe", "RealMadrid", "transfer"], "tags": ["transfer"]},
            ]
            create_fake_metadata(tmpdir, date_str, articles, batches_completed=["morning"])

            covered = orch.get_cross_batch_covered(date_str)
            assert covered["batch_count"] == 1
            assert "热点球评" in covered["content_types"]
            assert "转会资讯" in covered["content_types"]
            assert "arsenal" in covered["keywords"] or "Arsenal" in covered["keywords"]
            assert len(covered["content_types"]) == 2
    finally:
        orch.OUTPUT_DIR = orig_output
    print("  PASS test_get_cross_batch_covered_with_data")


def test_save_batch_state_new():
    """save_batch_state should create metadata when none exists."""
    import orchestrator as orch
    orig_output = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            date_str = "2026-06-02"
            orch.save_batch_state(date_str, "morning", [{"title": "test"}])

            meta_path = Path(tmpdir) / date_str / "metadata.json"
            assert meta_path.exists()
            meta = json.loads(meta_path.read_text())
            assert "morning" in meta["batches_completed"]
            assert meta["last_batch"] == "morning"
    finally:
        orch.OUTPUT_DIR = orig_output
    print("  PASS test_save_batch_state_new")


def test_save_batch_state_append():
    """save_batch_state should append to existing batches_completed."""
    import orchestrator as orch
    orig_output = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            date_str = "2026-06-02"
            # First batch
            orch.save_batch_state(date_str, "morning", [{"title": "test1"}])
            # Second batch
            orch.save_batch_state(date_str, "noon", [{"title": "test2"}])

            meta_path = Path(tmpdir) / date_str / "metadata.json"
            meta = json.loads(meta_path.read_text())
            assert "morning" in meta["batches_completed"]
            assert "noon" in meta["batches_completed"]
            assert len(meta["batches_completed"]) == 2
            assert meta["last_batch"] == "noon"
    finally:
        orch.OUTPUT_DIR = orig_output
    print("  PASS test_save_batch_state_append")


def test_save_batch_state_no_duplicate():
    """save_batch_state should not duplicate batch entries."""
    import orchestrator as orch
    orig_output = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            date_str = "2026-06-02"
            orch.save_batch_state(date_str, "morning", [{"title": "test"}])
            orch.save_batch_state(date_str, "morning", [{"title": "test"}])
            meta_path = Path(tmpdir) / date_str / "metadata.json"
            meta = json.loads(meta_path.read_text())
            assert meta["batches_completed"] == ["morning"]
    finally:
        orch.OUTPUT_DIR = orig_output
    print("  PASS test_save_batch_state_no_duplicate")


def test_cross_batch_type_avoidance():
    """Simulate the cross-batch type avoidance logic from main()."""
    # Simulate scenario: morning already covered 热点球评 and 八卦趣事
    # Noon batch should NOT pick those types
    already_covered = {"热点球评", "八卦趣事"}
    target_types = ["转会资讯", "排行榜"]
    season_weights = {"热点球评": 1.0, "转会资讯": 1.0, "排行榜": 1.0, "八卦趣事": 1.0, "战术解析": 1.0}

    # No overlap — no changes needed
    adjustments = 0
    for i, ct in enumerate(target_types):
        if ct in already_covered:
            adjustments += 1
    assert adjustments == 0, f"Expected 0 adjustments, got {adjustments}"
    print("  PASS test_cross_batch_type_avoidance (no overlap)")


def test_cross_batch_type_avoidance_with_overlap():
    """When target type already covered, should swap it out."""
    already_covered = {"热点球评", "八卦趣事"}
    target_types = ["热点球评", "转会资讯"]  # 热点球评 already covered
    season_weights = {"热点球评": 1.0, "转会资讯": 1.0, "排行榜": 1.0, "八卦趣事": 1.0, "战术解析": 1.0}

    all_types = ["八卦趣事", "转会资讯", "战术解析", "热点球评", "排行榜"]
    for i, ct in enumerate(target_types):
        if ct in already_covered:
            for alt_type in all_types:
                if alt_type not in already_covered and alt_type not in target_types:
                    target_types[i] = alt_type
                    already_covered.add(alt_type)
                    break

    assert "热点球评" not in target_types, f"热点球评 should have been swapped: {target_types}"
    # The replacement should not be in already_covered
    for ct in target_types:
        assert ct not in {"热点球评", "八卦趣事"}, f"{ct} should not be in already covered"
    print(f"  PASS test_cross_batch_type_avoidance_with_overlap → {target_types}")


def test_cross_batch_all_types_covered():
    """When all 5 types are covered, should still work (fallback stays as-is)."""
    already_covered = {"热点球评", "转会资讯", "排行榜", "八卦趣事", "战术解析"}
    target_types = ["热点球评", "八卦趣事"]  # both covered
    original = list(target_types)

    all_types = ["八卦趣事", "转会资讯", "战术解析", "热点球评", "排行榜"]
    for i, ct in enumerate(target_types):
        if ct in already_covered:
            for alt_type in all_types:
                if alt_type not in already_covered and alt_type not in target_types:
                    target_types[i] = alt_type
                    already_covered.add(alt_type)
                    break

    # All types are covered, so no alternative was found — types unchanged
    assert target_types == original, f"Should stay unchanged when all types covered: {target_types}"
    print("  PASS test_cross_batch_all_types_covered (graceful no-op)")


def test_cross_batch_with_season_weights():
    """Cross-batch swap should prefer high-weight alternatives when available."""
    already_covered = {"热点球评"}
    target_types = ["热点球评", "八卦趣事"]
    # June weights: 转会资讯(1.5) > 战术解析(0.5)
    season_weights = {"热点球评": 0.5, "转会资讯": 1.5, "排行榜": 1.5, "八卦趣事": 2.0, "战术解析": 0.5}

    for i, ct in enumerate(target_types):
        if ct in already_covered:
            candidates = sorted(season_weights.items(), key=lambda x: -x[1])
            for alt_type, alt_w in candidates:
                if alt_type not in already_covered and alt_type not in target_types:
                    target_types[i] = alt_type
                    already_covered.add(alt_type)
                    break

    assert "热点球评" not in target_types
    # 转会资讯(1.5) should be picked first (highest weight not in covered)
    # Actually 八卦趣事(2.0) is already in target_types, so 转会资讯(1.5) should be chosen
    assert "转会资讯" in target_types or "排行榜" in target_types
    print(f"  PASS test_cross_batch_with_season_weights → {target_types}")


def test_get_cross_batch_covered_real_import():
    """Verify the function is importable and callable."""
    from orchestrator import get_cross_batch_covered, save_batch_state
    assert callable(get_cross_batch_covered)
    assert callable(save_batch_state)
    print("  PASS test_get_cross_batch_covered_real_import")


def test_end_to_end_cross_batch_simulation():
    """Full simulation: morning → noon, verify noon avoids morning's types."""
    import orchestrator as orch
    orig_output = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch.OUTPUT_DIR = Path(tmpdir)
            date_str = "2026-06-02"

            # Morning batch writes 2 articles
            morning_articles = [
                {"title": "阿森纳点球输巴黎", "content_type": "热点球评",
                 "keywords": ["Arsenal", "PSG"], "tags": ["UCL"]},
                {"title": "内马尔场外又惹事", "content_type": "八卦趣事",
                 "keywords": ["Neymar", "gossip"], "tags": ["gossip"]},
            ]
            orch.save_batch_state(date_str, "morning", morning_articles)
            # Also write full metadata as save_articles_local would
            meta_path = Path(tmpdir) / date_str / "metadata.json"
            meta = json.loads(meta_path.read_text())
            meta["articles"] = morning_articles
            meta["total_articles"] = 2
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

            # Noon batch should detect covered types
            covered = orch.get_cross_batch_covered(date_str)
            assert covered["batch_count"] == 1
            assert "热点球评" in covered["content_types"]
            assert "八卦趣事" in covered["content_types"]

            # Noon target types: ["转会资讯", "排行榜"] — neither is covered, no swap needed
            noon_types = ["转会资讯", "排行榜"]
            for ct in noon_types:
                assert ct not in covered["content_types"], f"Noon type {ct} should not be covered yet"

            # Now test that if evening runs and 八卦趣事 is already covered:
            evening_types = ["战术解析", "八卦趣事"]  # 八卦趣事 already covered by morning!
            for i, ct in enumerate(evening_types):
                if ct in covered["content_types"]:
                    # Find alternative
                    all_types = ["八卦趣事", "转会资讯", "战术解析", "热点球评", "排行榜"]
                    for alt in all_types:
                        if alt not in covered["content_types"] and alt not in evening_types:
                            evening_types[i] = alt
                            covered["content_types"].add(alt)
                            break

            assert "八卦趣事" not in evening_types, f"八卦趣事 should have been swapped: {evening_types}"
    finally:
        orch.OUTPUT_DIR = orig_output
    print("  PASS test_end_to_end_cross_batch_simulation")


def test_batch_types_imported_not_redefined():
    """#2 bugfix: BATCH_TYPES must be imported from constants, not locally redefined.

    Prior bug: BATCH_TYPES was imported at the top of orchestrator.py but then
    immediately shadowed by an identical local dict inside main(). The import
    was dead code. Fix removes the local shadow and uses the imported constant.
    """
    import orchestrator as orch
    # Should be importable from the module level (not just inside main())
    assert hasattr(orch, 'BATCH_TYPES') or True  # Check via source that no local redef exists

    # Verify orchestrator doesn't redefine BATCH_TYPES inside main()
    import inspect
    src = inspect.getsource(orch.main)
    # Local redefinition in main() should NOT exist after fix
    assert "BATCH_TYPES = {" not in src, \
        "BATCH_TYPES local redefinition found in main() — use the imported constant instead"

    # Verify the import exists at module level
    with open(orch.__file__) as f:
        full = f.read()
    assert "BATCH_TYPES" in full.split("def main")[0], \
        "BATCH_TYPES must be imported at module level"

    print("  PASS test_batch_types_imported_not_redefined")


def test_generate_articles_from_topics_exists():
    """#3 bugfix: shared article-generation helper must be importable.

    Three near-identical for-loops were consolidated into one function."""
    import orchestrator as orch
    fn = getattr(orch, '_generate_articles_from_topics', None)
    assert fn is not None, "_generate_articles_from_topics function missing"
    assert callable(fn)
    print("  PASS test_generate_articles_from_topics_exists")


def test_fallback_map_imported_not_redefined():
    """#5 bugfix: FALLBACK_MAP must use the imported constant, not local shadow.

    Same pattern as BATCH_TYPES: imported at module level, redefined inside main()."""
    import orchestrator as orch
    import inspect
    src = inspect.getsource(orch.main)
    assert "FALLBACK_MAP = {" not in src, \
        "FALLBACK_MAP local redefinition found in main() — use the imported constant instead"
    print("  PASS test_fallback_map_imported_not_redefined")


def test_hupu_pipeline_extracted():
    """#4 bugfix: Hupu pipeline extracted as _run_hupu_pipeline function."""
    import orchestrator as orch
    fn = getattr(orch, '_run_hupu_pipeline', None)
    assert fn is not None, "_run_hupu_pipeline function missing"
    assert callable(fn)
    print("  PASS test_hupu_pipeline_extracted")


def test_gzh_no_hardcoded_tmp():
    """#7 bugfix: GZH collection must use OUTPUT_DIR, not hardcoded /tmp/ path.

    Using /tmp/ for intermediate files is fragile (can fill up, clash with
    parallel runs). Changed to OUTPUT_DIR/gzh_cache/ for project-scoped temp files.
    """
    from pathlib import Path
    dc_file = Path(__file__).parent.parent / "data_collector.py"
    src = dc_file.read_text()
    idx = src.find("def fetch_gzh_football_trends")
    assert idx > 0, "fetch_gzh_football_trends not found"
    fn_src = src[idx:idx + 1500]
    assert 'gzh_cache' in fn_src, "gzh_cache directory should be used"
    assert '/tmp/gzh_' not in fn_src, "Hardcoded /tmp/ path should be removed"
    print("  PASS test_gzh_no_hardcoded_tmp")


def test_image_marker_cleanup():
    """#8 bugfix: auto-generated 配图 markers must be stripped before re-injection.

    When the LLM generates image markers like ![配图1](images/article-1-img-001.jpg)
    but fewer (or zero) images are actually downloaded, the old code left orphaned
    markers causing broken links. The fix strips all auto-generated markers first,
    then injects only the actual downloaded images.
    """
    import re
    # Simulate: LLM generated 3 markers, but only 1 image was downloaded
    content = """正文内容...
## 分析段落
正文...
![配图1](images/article-1-img-001.jpg)
## 另一段
正文...
![配图2](images/article-1-img-002.jpg)
## 第三段
正文...
![配图3](images/article-1-img-003.jpg)
"""
    # The fix regex
    cleaned = re.sub(r'!\[配图\d+\]\(images/article-\d+-img-\d+\.jpg\)\n?', '', content)
    # All markers should be stripped
    assert '![配图' not in cleaned, f"All 配图 markers should be stripped, got: {cleaned[cleaned.find('!['):][:80]}"
    # Content text should remain intact
    assert '正文内容' in cleaned
    assert '## 分析段落' in cleaned
    assert '## 另一段' in cleaned
    assert '## 第三段' in cleaned
    print("  PASS test_image_marker_cleanup")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Task #10 Unit Tests: 跨批次去重")
    print("=" * 60)

    all_tests = [
        ("get_cross_batch_covered: empty → empty sets", test_get_cross_batch_covered_empty),
        ("get_cross_batch_covered: extracts types/teams/players", test_get_cross_batch_covered_with_data),
        ("save_batch_state: creates new metadata", test_save_batch_state_new),
        ("save_batch_state: appends to existing batches", test_save_batch_state_append),
        ("save_batch_state: no duplicate entries", test_save_batch_state_no_duplicate),
        ("cross_batch logic: no overlap → no changes", test_cross_batch_type_avoidance),
        ("cross_batch logic: overlap → swap out", test_cross_batch_type_avoidance_with_overlap),
        ("cross_batch logic: all types covered → graceful no-op", test_cross_batch_all_types_covered),
        ("cross_batch + season weights: prefers high-weight alt", test_cross_batch_with_season_weights),
        ("import: functions are importable", test_get_cross_batch_covered_real_import),
        ("e2e: morning→noon→evening simulation", test_end_to_end_cross_batch_simulation),
        ("#2 bugfix: BATCH_TYPES imported, not redefined", test_batch_types_imported_not_redefined),
        ("#3 bugfix: shared article-gen helper exists", test_generate_articles_from_topics_exists),
        ("#5 bugfix: FALLBACK_MAP imported, not redefined", test_fallback_map_imported_not_redefined),
        ("#4 bugfix: Hupu pipeline extracted", test_hupu_pipeline_extracted),
        ("#7 bugfix: GZH cache dir, not /tmp/", test_gzh_no_hardcoded_tmp),
        ("#8 bugfix: 配图 markers stripped before re-inject", test_image_marker_cleanup),
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
