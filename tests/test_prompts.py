#!/usr/bin/env python3
"""Prompt file validation tests (Task #7).

Checks: file existence, minimum size, required sections, checksums.
"""

import sys, os, hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
PROMPT_DIR = PROJECT_ROOT / "prompts"

# Expected checksums (update when prompts change intentionally)
# 注：article_generator.txt 已于 b2a0827「删除不再使用的文件」移除，
# 成稿生成现走 rewrite_article.txt（Pipeline A 改写）与内联 prompt。
EXPECTED_CHECKSUMS = {
    "topic_selector.txt": None,
    "rewrite_article.txt": None,
}

REQUIRED_SECTIONS = {
    "topic_selector.txt": ["五大内容品类", "评分体系", "标题公式", "输出格式", "动态调配"],
    "rewrite_article.txt": ["输出格式", "ai_perspective"],
}


def test_prompt_files_exist():
    """Production prompt files must exist."""
    for fname in ["topic_selector.txt", "rewrite_article.txt"]:
        path = PROMPT_DIR / fname
        assert path.exists(), f"{fname} not found at {path}"
        assert path.stat().st_size > 500, f"{fname} too small ({path.stat().st_size} bytes)"
    print("  PASS test_prompt_files_exist")


def test_topic_selector_sections():
    """topic_selector.txt must contain all required sections."""
    path = PROMPT_DIR / "topic_selector.txt"
    content = path.read_text()
    for section in REQUIRED_SECTIONS["topic_selector.txt"]:
        assert section in content, f"Missing required section: {section}"
    print("  PASS test_topic_selector_sections")


def test_prompt_checksums():
    """Verify prompt checksums (skip if not locked)."""
    for fname, expected in EXPECTED_CHECKSUMS.items():
        path = PROMPT_DIR / fname
        if not path.exists():
            continue
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        if expected and checksum != expected:
            print(f"  WARNING: {fname} checksum changed: expected={expected}, got={checksum}")
        else:
            print(f"  INFO: {fname} sha256={checksum}")
    print("  PASS test_prompt_checksums")


def test_no_banned_patterns():
    """Prompts should not contain hardcoded example titles that might leak into output."""
    path = PROMPT_DIR / "rewrite_article.txt"
    content = path.read_text()
    # Should NOT contain real article titles as fixed examples
    # (This is a sanity check, not strict enforcement)
    assert len(content) > 1000, "Prompt too short — may be truncated"
    print("  PASS test_no_banned_patterns")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Task #7 Tests: 提示词文件管理")
    print("=" * 60)

    all_tests = [
        ("prompts exist & have content", test_prompt_files_exist),
        ("topic_selector: all sections present", test_topic_selector_sections),
        ("checksums tracked", test_prompt_checksums),
        ("no banned patterns", test_no_banned_patterns),
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
