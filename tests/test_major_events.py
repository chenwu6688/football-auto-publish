#!/usr/bin/env python3
"""Unit tests for major event detection & emergency articles (Task #12).

Tests:
  1. detect_major_events() detects high-scoring matches, blowouts, etc.
  2. Event urgency scoring is correct
  3. Event deduplication works
  4. Breaking news detection from GZH trends
  5. Empty data handling
  6. generate_emergency_article() prompt generation (dry-run)
"""

import sys, os, json, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_fixture(home, away, hg, ag, league="英超"):
    return {"home_team": home, "away_team": away, "home_score": hg, "away_score": ag}


def make_match_data(fixtures_by_league):
    return {"date": "2026-06-02", "total_matches": sum(len(v) for v in fixtures_by_league.values()),
            "fixtures_by_league": fixtures_by_league, "all_fixtures": [], "standings": {}}


def test_detect_high_scoring_match():
    """5+ goal matches should be detected as 进球大战."""
    from orchestrator import detect_major_events
    data = make_match_data({"英超": [make_fixture("利物浦", "热刺", 4, 3)]})
    events = detect_major_events(data)
    assert len(events) >= 1
    goal_fest = [e for e in events if e["type"] == "进球大战"]
    assert len(goal_fest) >= 1
    assert goal_fest[0]["urgency"] >= 85
    assert "利物浦" in goal_fest[0]["detail"]
    print("  PASS test_detect_high_scoring_match")


def test_detect_blowout():
    """4+ goal difference should be detected as 惨案."""
    from orchestrator import detect_major_events
    data = make_match_data({"西甲": [make_fixture("皇马", "巴拉多利德", 7, 0)]})
    events = detect_major_events(data)
    blowout = [e for e in events if e["type"] == "惨案"]
    assert len(blowout) >= 1
    assert blowout[0]["urgency"] >= 75
    assert "皇马" in blowout[0]["detail"]
    print("  PASS test_detect_blowout")


def test_detect_both_teams_scoring_high():
    """Both teams scoring 3+ should be detected as 神仙打架."""
    from orchestrator import detect_major_events
    data = make_match_data({"德甲": [make_fixture("拜仁", "多特", 3, 3)]})
    events = detect_major_events(data)
    thriller = [e for e in events if e["type"] == "神仙打架"]
    assert len(thriller) >= 1
    # Also should be detected as 进球大战 (6 goals >= 5)
    goal_fest = [e for e in events if e["type"] == "进球大战"]
    assert len(goal_fest) >= 1
    print("  PASS test_detect_both_teams_scoring_high")


def test_detect_shutout_blowout():
    """3-0 or larger shutout should be detected as 碾压局."""
    from orchestrator import detect_major_events
    data = make_match_data({"意甲": [make_fixture("国米", "AC米兰", 4, 0)]})
    events = detect_major_events(data)
    shutout = [e for e in events if e["type"] == "碾压局"]
    assert len(shutout) >= 1
    # Also 惨案 (4 goal diff)
    blowout = [e for e in events if e["type"] == "惨案"]
    assert len(blowout) >= 1
    print("  PASS test_detect_shutout_blowout")


def test_no_events_normal_match():
    """A normal 1-0 match should not trigger any events."""
    from orchestrator import detect_major_events
    data = make_match_data({"法甲": [make_fixture("巴黎", "朗斯", 1, 0)]})
    events = detect_major_events(data)
    assert len(events) == 0
    print("  PASS test_no_events_normal_match")


def test_no_events_unplayed_match():
    """Unplayed matches (None scores) should not trigger events."""
    from orchestrator import detect_major_events
    data = make_match_data({"英超": [
        {"home_team": "曼联", "away_team": "切尔西", "home_score": None, "away_score": None}
    ]})
    events = detect_major_events(data)
    assert len(events) == 0
    print("  PASS test_no_events_unplayed_match")


def test_event_deduplication():
    """Same event should not appear twice even if matching multiple criteria."""
    from orchestrator import detect_major_events
    # 5-0 match matches: 进球大战(5 goals), 惨案(5 diff), 碾压局(5-0 shutout)
    data = make_match_data({"英超": [make_fixture("曼城", "伯恩利", 5, 0)]})
    events = detect_major_events(data)
    # Should detect all 3 types
    types_found = {e["type"] for e in events}
    assert "进球大战" in types_found
    assert "惨案" in types_found
    assert "碾压局" in types_found
    # Each type should appear exactly once
    from collections import Counter
    type_counts = Counter(e["type"] for e in events)
    for t, c in type_counts.items():
        assert c == 1, f"Type {t} appears {c} times (should be deduped to 1)"
    print(f"  PASS test_event_deduplication (types: {types_found})")


def test_event_urgency_ordering():
    """Events should be sorted by urgency (highest first)."""
    from orchestrator import detect_major_events
    data = make_match_data({
        "英超": [make_fixture("利物浦", "热刺", 4, 3)],  # 进球大战 + 神仙打架
        "西甲": [make_fixture("皇马", "巴萨", 1, 0)],    # normal, no event
    })
    events = detect_major_events(data)
    if len(events) >= 2:
        for i in range(len(events) - 1):
            assert events[i]["urgency"] >= events[i+1]["urgency"], \
                f"Event {i} urgency {events[i]['urgency']} < event {i+1} urgency {events[i+1]['urgency']}"
    print(f"  PASS test_event_urgency_ordering ({len(events)} events)")


def test_detect_gzh_breaking_news():
    """GZH articles with breaking keywords should be detected."""
    from orchestrator import detect_major_events
    data = make_match_data({"英超": [make_fixture("曼联", "阿森纳", 1, 0)]})
    gzh = [
        {"title": "重磅！瓜迪奥拉宣布离开曼城", "summary": "曼城主帅正式宣布赛季末离任",
         "clicksCount": 50000, "accountName": "足球报"},
        {"title": "英超第30轮战报：平淡一轮", "summary": "各队均无建树",
         "clicksCount": 1000, "accountName": "平凡足球"},
    ]
    events = detect_major_events(data, gzh)
    breaking = [e for e in events if e["type"] == "突发新闻"]
    assert len(breaking) >= 1
    assert "瓜迪奥拉" in breaking[0]["detail"]
    assert breaking[0]["urgency"] >= 60
    print(f"  PASS test_detect_gzh_breaking_news ({len(breaking)} breaking events)")


def test_event_empty_data():
    """Empty match data should not raise errors."""
    from orchestrator import detect_major_events
    data = make_match_data({})
    events = detect_major_events(data)
    assert events == []
    print("  PASS test_event_empty_data")


def test_emergency_article_function_exists():
    """generate_emergency_article should be importable."""
    from orchestrator import detect_major_events, generate_emergency_article
    assert callable(detect_major_events)
    assert callable(generate_emergency_article)
    print("  PASS test_emergency_article_function_exists")


def test_urgent_filter_threshold():
    """Only events with urgency >= 70 should be considered 'urgent'."""
    from orchestrator import detect_major_events
    # Low-key match: 1-0, no events
    data = make_match_data({"法甲": [make_fixture("尼斯", "兰斯", 1, 0)]})
    events = detect_major_events(data)
    urgent = [e for e in events if e["urgency"] >= 70]
    assert len(urgent) == 0

    # High-scoring match should produce urgent events
    data2 = make_match_data({"英超": [make_fixture("利物浦", "纽卡", 4, 4)]})
    events2 = detect_major_events(data2)
    urgent2 = [e for e in events2 if e["urgency"] >= 70]
    assert len(urgent2) >= 1
    print(f"  PASS test_urgent_filter_threshold (urgent: {len(urgent2)}/{len(events2)})")


def test_multiple_leagues_events():
    """Events from multiple leagues should all be detected."""
    from orchestrator import detect_major_events
    data = make_match_data({
        "英超": [make_fixture("利物浦", "热刺", 4, 3)],
        "西甲": [make_fixture("皇马", "巴拉多利德", 6, 0)],
        "意甲": [make_fixture("尤文", "那不勒斯", 1, 0)],  # normal
    })
    events = detect_major_events(data)
    # Should find events from at least 2 different leagues
    leagues = set()
    for e in events:
        if e.get("league"):
            leagues.add(e["league"])
    assert len(leagues) >= 2, f"Expected events from 2+ leagues, got {leagues}"
    assert "意甲" not in leagues or len([e for e in events if e.get("league") == "意甲"]) == 0
    print(f"  PASS test_multiple_leagues_events (leagues: {leagues}, total: {len(events)} events)")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Task #12 Unit Tests: 重大事件触发紧急球评")
    print("=" * 60)

    all_tests = [
        ("detect: 5+ goal match → 进球大战", test_detect_high_scoring_match),
        ("detect: 4+ goal diff → 惨案", test_detect_blowout),
        ("detect: both score 3+ → 神仙打架", test_detect_both_teams_scoring_high),
        ("detect: 3-0+ shutout → 碾压局", test_detect_shutout_blowout),
        ("detect: normal 1-0 → no events", test_no_events_normal_match),
        ("detect: unplayed match → no events", test_no_events_unplayed_match),
        ("detect: deduplication works", test_event_deduplication),
        ("detect: urgency ordering (desc)", test_event_urgency_ordering),
        ("detect: GZH breaking news keywords", test_detect_gzh_breaking_news),
        ("detect: empty data → no crash", test_event_empty_data),
        ("detect: multiple leagues combined", test_multiple_leagues_events),
        ("filter: urgency >= 70 threshold", test_urgent_filter_threshold),
        ("import: functions importable", test_emergency_article_function_exists),
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
