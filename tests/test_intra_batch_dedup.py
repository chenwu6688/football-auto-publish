#!/usr/bin/env python3
"""Unit tests for intra-batch dedup in select_topics."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_dedup_no_overlap():
    """Two topics with completely different keywords should pass."""
    from orchestrator import _check_intra_batch_dedup

    topics = [
        {"title": "阿森纳绝杀曼联", "keywords": ["Arsenal", "ManUtd", "PremierLeague"],
         "keywords_cn": ["阿森纳", "曼联"], "score": 90},
        {"title": "姆巴佩转会皇马", "keywords": ["Mbappe", "RealMadrid", "Transfer"],
         "keywords_cn": ["姆巴佩", "皇马"], "score": 85},
    ]
    clean, warnings = _check_intra_batch_dedup(topics)
    assert len(warnings) == 0, f"Expected 0 warnings, got {len(warnings)}: {warnings}"
    assert len(clean) == 2
    print("  PASS test_dedup_no_overlap")


def test_dedup_high_overlap_warns():
    """Two topics sharing >40% keywords should trigger warning."""
    from orchestrator import _check_intra_batch_dedup

    topics = [
        {"title": "姆巴佩离开后巴黎两连冠", "keywords": ["Mbappe", "PSG", "ChampionsLeague"],
         "keywords_cn": ["姆巴佩", "巴黎"], "score": 90},
        {"title": "姆巴佩的欧冠诅咒", "keywords": ["Mbappe", "ChampionsLeague", "RealMadrid"],
         "keywords_cn": ["姆巴佩", "欧冠"], "score": 85},
    ]
    clean, warnings = _check_intra_batch_dedup(topics)
    # Mbappe + ChampionsLeague overlap = 2/3 = 66% > 40% → warning
    assert len(warnings) >= 1, f"Expected >=1 warning for 66% overlap, got {len(warnings)}"
    print("  PASS test_dedup_high_overlap_warns")


def test_dedup_single_topic_no_warning():
    """Single topic should never trigger dedup warning."""
    from orchestrator import _check_intra_batch_dedup

    topics = [{"title": "单篇测试", "keywords": ["test"], "keywords_cn": ["测试"], "score": 80}]
    clean, warnings = _check_intra_batch_dedup(topics)
    assert len(warnings) == 0
    assert len(clean) == 1
    print("  PASS test_dedup_single_topic_no_warning")


def test_dedup_empty_list():
    """Empty topic list should work fine."""
    from orchestrator import _check_intra_batch_dedup

    clean, warnings = _check_intra_batch_dedup([])
    assert len(warnings) == 0
    assert len(clean) == 0
    print("  PASS test_dedup_empty_list")


def test_dedup_low_overlap_passes():
    """Two topics sharing <40% keywords should not warn."""
    from orchestrator import _check_intra_batch_dedup

    topics = [
        {"title": "阿森纳定位球战术分析", "keywords": ["Arsenal", "SetPiece", "Tactics", "EPL"],
         "keywords_cn": ["阿森纳", "定位球"], "score": 88},
        {"title": "拉什福德转会巴萨", "keywords": ["Rashford", "Barcelona", "Transfer", "ManUtd"],
         "keywords_cn": ["拉什福德", "巴萨"], "score": 82},
    ]
    clean, warnings = _check_intra_batch_dedup(topics)
    # Only "ManUtd" might overlap if present, but ManUtd isn't in topic 1
    assert len(warnings) == 0, f"Expected 0 warnings, got: {warnings}"
    print("  PASS test_dedup_low_overlap_passes")


def test_dedup_prompt_has_core_subject_rule():
    """select_topics prompt must include intra-batch core-subject diversity rule."""
    import orchestrator as orch
    import inspect
    src = inspect.getsource(orch.select_topics)
    assert "不同核心主题" in src, "Prompt must require different core subjects"
    assert "核心关键词集合" in src, "Prompt must mention keyword set non-overlap"
    print("  PASS test_dedup_prompt_has_core_subject_rule")


def test_topic_selector_file_has_dedup_rule():
    """topic_selector.txt must include intra-batch dedup in 禁止选题 section."""
    from utils import load_prompt_template
    tmpl = load_prompt_template("topic_selector.txt")
    assert "同一批次" in tmpl, "topic_selector.txt must mention intra-batch dedup"
    assert "核心关键词" in tmpl, "topic_selector.txt must mention core keyword diversity"
    print("  PASS test_topic_selector_file_has_dedup_rule")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Intra-Batch Dedup Tests")
    print("=" * 60)

    all_tests = [
        ("dedup: no overlap passes", test_dedup_no_overlap),
        ("dedup: high overlap warns", test_dedup_high_overlap_warns),
        ("dedup: single topic ok", test_dedup_single_topic_no_warning),
        ("dedup: empty list ok", test_dedup_empty_list),
        ("dedup: low overlap ok", test_dedup_low_overlap_passes),
        ("dedup: select_topics prompt has rule", test_dedup_prompt_has_core_subject_rule),
        ("dedup: topic_selector.txt has rule", test_topic_selector_file_has_dedup_rule),
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
    sys.exit(0 if failed == 0 else 1)
