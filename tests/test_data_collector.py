#!/usr/bin/env python3
"""Unit tests for data_collector.py (round 2 fixes)."""

import sys, os, json, tempfile
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_fetch_standings_reuses_competition_ids():
    """#1 bugfix: standings should use COMPETITION_IDS, not hardcoded sid_map.

    Prior bug: sid_map was a separate hardcoded dict (English names → IDs)
    that duplicated COMPETITION_IDS. If one was updated but not the other,
    they'd silently diverge.
    """
    import data_collector as dc
    import inspect
    src = inspect.getsource(dc.fetch_recent_standings)
    # Must reference COMPETITION_IDS for the league→ID mapping
    assert 'COMPETITION_IDS' in src, \
        "fetch_recent_standings must use COMPETITION_IDS, not hardcoded sid_map"
    # Must have error logging, not silent pass
    assert 'pass' not in src.split('except Exception')[1].split('\n')[0] if 'except Exception' in src else True, \
        "Silent except:pass should be replaced with error logging"
    print("  PASS test_fetch_standings_reuses_competition_ids")


def test_fetch_scorers_reuses_competition_ids():
    """#1 bugfix: scorers should use COMPETITION_IDS, not hardcoded comp_map.

    Same duplication issue as standings — separate dict with Chinese→ID mapping
    that must stay in sync with COMPETITION_IDS."""
    import data_collector as dc
    import inspect
    src = inspect.getsource(dc.fetch_scorers)
    assert 'COMPETITION_IDS' in src, \
        "fetch_scorers must use COMPETITION_IDS, not hardcoded comp_map"
    print("  PASS test_fetch_scorers_reuses_competition_ids")


def test_rate_limit_sleep_increased():
    """#1 bugfix: API sleep interval should be >= 0.5s (was 0.3s).

    football-data.org free tier: 10 req/min. With 6 leagues per query,
    0.3s spacing may burst 6 requests in 1.8s, risking 429s."""
    import data_collector as dc
    import inspect
    for fn_name in ['fetch_recent_standings', 'fetch_scorers']:
        src = inspect.getsource(getattr(dc, fn_name))
        # Should have time.sleep with >= 0.5
        assert 'time.sleep(0.6)' in src or 'time.sleep(0.5)' in src, \
            f"{fn_name}: time.sleep should be >= 0.5s for rate limiting"
    print("  PASS test_rate_limit_sleep_increased")


def test_competition_ids_consistent():
    """COMPETITION_IDS keys should match Chinese league names used throughout."""
    from constants import COMPETITION_IDS
    assert "英超" in COMPETITION_IDS
    assert "欧冠" in COMPETITION_IDS
    assert len(COMPETITION_IDS) == 6  # 5 domestic + UCL
    print("  PASS test_competition_ids_consistent")


def test_load_prompt_template_exists():
    """#2 bugfix: load_prompt_template must be importable and functional.

    Prior: prompt files existed but were never loaded at runtime — editing
    article_generator.txt or topic_selector.txt had no effect. The orchestrator
    had all prompts hardcoded inline. Fix adds load_prompt_template() and uses
    it in system message construction.
    """
    from utils import load_prompt_template
    # Load topic_selector — should return non-empty content
    ts = load_prompt_template("topic_selector.txt")
    assert len(ts) > 500, f"topic_selector.txt too short ({len(ts)} chars)"
    assert "足球" in ts or "评分" in ts or "品类" in ts, \
        "topic_selector template should contain football/scoring/category content"

    # Load article_generator — should return non-empty content
    ag = load_prompt_template("article_generator.txt")
    assert len(ag) > 500, f"article_generator.txt too short ({len(ag)} chars)"
    assert "足球" in ag or "品类" in ag or "写作" in ag, \
        "article_generator template should contain football/category/writing content"

    # Missing file returns empty string gracefully
    empty = load_prompt_template("nonexistent.txt")
    assert empty == "", f"Missing file should return empty string, got: {empty[:50]}"
    print("  PASS test_load_prompt_template_exists")


def test_select_topics_json_type_safety():
    """#4 bugfix: select_topics should guard against dict (not list) LLM response.

    When safe_json_loads returns a dict instead of list, iterating `for t in topics`
    produces string keys, and t['title'] raises TypeError. Fix wraps the result
    in a list if it's a single dict.
    """
    import orchestrator as orch
    import inspect
    src = inspect.getsource(orch.select_topics)
    assert 'isinstance(topics, dict)' in src or 'isinstance(topics, list)' in src, \
        "select_topics must have type guard against dict LLM responses"
    print("  PASS test_select_topics_json_type_safety")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Round 2 Tests: data_collector.py")
    print("=" * 60)

    all_tests = [
        ("#1 standings uses COMPETITION_IDS", test_fetch_standings_reuses_competition_ids),
        ("#1 scorers uses COMPETITION_IDS", test_fetch_scorers_reuses_competition_ids),
        ("#1 rate limit sleep increased", test_rate_limit_sleep_increased),
        ("COMPETITION_IDS consistency", test_competition_ids_consistent),
        ("#2 prompt template loaded at runtime", test_load_prompt_template_exists),
        ("#4 JSON list type-guard in select_topics", test_select_topics_json_type_safety),
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
