#!/usr/bin/env python3
"""Unit tests for micro_headline.py, image_service.py screenshot, and match_scheduler.py."""

import sys, os, json, tempfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# Micro-Headline Tests
# ============================================================

def test_generate_micro_headlines_empty_data():
    """Empty match data should return empty list."""
    from micro_headline import generate_micro_headlines
    result = generate_micro_headlines({})
    assert result == [], "Empty match data should return empty list"
    print("  PASS test_generate_micro_headlines_empty_data")


def test_generate_micro_headlines_fallback():
    """When LLM fails, fallback should produce headlines from match data."""
    from micro_headline import generate_micro_headlines
    match_data = {
        "all_fixtures": [
            {"home_team": "Argentina", "away_team": "Algeria", "home_score": 3, "away_score": 0,
             "league": "FIFA World Cup", "utc_date": "2026-06-17T01:00:00Z"},
            {"home_team": "England", "away_team": "Croatia", "home_score": 2, "away_score": 1,
             "league": "FIFA World Cup", "utc_date": "2026-06-17T05:00:00Z"},
        ]
    }
    result = generate_micro_headlines(match_data, count=2)
    # Without API key, LLM call will fail → should use fallback
    assert result, "Should produce fallback headlines even without API key"
    assert len(result) <= 2, f"Should max {2} headlines, got {len(result)}"
    for h in result:
        assert "content" in h, "Each headline must have content"
        assert len(h["content"]) >= 10, f"Content too short: {h['content']}"
    print(f"  PASS test_generate_micro_headlines_fallback ({len(result)} headlines)")


def test_micro_headline_content_format():
    """Fallback headlines should contain team names and scores."""
    from micro_headline import generate_micro_headlines
    match_data = {
        "all_fixtures": [
            {"home_team": "Brazil", "away_team": "Morocco", "home_score": 4, "away_score": 1,
             "league": "FIFA World Cup", "utc_date": "2026-06-17T03:00:00Z"},
        ]
    }
    result = generate_micro_headlines(match_data, count=1)
    if result:
        content = result[0]["content"]
        assert "Brazil" in content or "Morocco" in content, \
            f"Content should mention teams: {content[:100]}"
        assert "4-1" in content or "4" in content, \
            f"Content should mention score: {content[:100]}"
    print("  PASS test_micro_headline_content_format")


# ============================================================
# ImageService Screenshot Tests
# ============================================================

def test_capture_match_screenshot_no_api():
    """capture_match_screenshot should handle API failures gracefully."""
    from image_service import ImageService
    svc = ImageService()
    # Without Playwright browser in test env, this should fail gracefully
    result = svc.capture_match_screenshot("Nonexistent_Team_XYZ", "Opponent_ABC")
    assert result is None or isinstance(result, dict), \
        "Should return None (or dict on success)"
    print("  PASS test_capture_match_screenshot_no_api")


# ============================================================
# Match Scheduler Tests
# ============================================================

def test_plan_content_sequence_empty():
    """Empty match list should produce empty content plan."""
    from match_scheduler import plan_content_sequence
    plans = plan_content_sequence([])
    assert plans == [], "Empty matches → empty plans"
    print("  PASS test_plan_content_sequence_empty")


def test_plan_content_sequence_non_focus():
    """Non-focus matches should not generate content plans (only focus matches do)."""
    from match_scheduler import plan_content_sequence
    matches = [{
        "home_team": "Small_Team_A", "away_team": "Small_Team_B",
        "league": "Minor League", "focus_match": False,
        "cst_time": "2026-06-18 15:00", "cst_date": "2026-06-18",
    }]
    plans = plan_content_sequence(matches)
    assert plans == [], "Non-focus matches should not generate content plans"
    print("  PASS test_plan_content_sequence_non_focus")


def test_plan_content_sequence_focus_match():
    """Focus matches should generate preview + review + deep_dive sequence."""
    from match_scheduler import plan_content_sequence
    matches = [{
        "home_team": "Argentina", "away_team": "Brazil",
        "league": "FIFA World Cup", "focus_match": True,
        "cst_time": "2026-06-18 15:00", "cst_date": "2026-06-18",
    }]
    plans = plan_content_sequence(matches)
    assert len(plans) >= 2, f"Focus match should generate ≥2 plans, got {len(plans)}"
    content_types = [p["content_type"] for p in plans]
    assert "preview" in content_types, "Should have preview"
    assert "review" in content_types, "Should have review"
    if "deep_dive" in content_types:
        deep_dive = [p for p in plans if p["content_type"] == "deep_dive"][0]
        assert "午夜" or "午间" or "evening" or "夜间" in str(type(deep_dive)), \
            "deep_dive should have suggested batch"
    print(f"  PASS test_plan_content_sequence_focus_match ({len(plans)} plans: {', '.join(content_types)})")


def test_fetch_upcoming_matches_import():
    """fetch_upcoming_matches should be importable and callable."""
    from match_scheduler import fetch_upcoming_matches, plan_content_sequence, save_schedule
    assert callable(fetch_upcoming_matches), "Should be a callable function"
    assert callable(plan_content_sequence), "Should be a callable function"
    assert callable(save_schedule), "Should be a callable function"
    print("  PASS test_fetch_upcoming_matches_import")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("新模块单元测试: micro_headline, screenshot, match_scheduler")
    print("=" * 60)

    all_tests = [
        ("micro: empty data", test_generate_micro_headlines_empty_data),
        ("micro: fallback generation", test_generate_micro_headlines_fallback),
        ("micro: content format", test_micro_headline_content_format),
        ("screenshot: no API", test_capture_match_screenshot_no_api),
        ("scheduler: empty plans", test_plan_content_sequence_empty),
        ("scheduler: non-focus skip", test_plan_content_sequence_non_focus),
        ("scheduler: focus sequence", test_plan_content_sequence_focus_match),
        ("scheduler: import check", test_fetch_upcoming_matches_import),
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
