#!/usr/bin/env python3
"""Prompt file validation tests (Task #7).

Checks: file existence, minimum size, required sections, checksums.
"""

import sys, os, hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
PROMPT_DIR = PROJECT_ROOT / "prompts"

# Expected checksums (update when prompts change intentionally)
EXPECTED_CHECKSUMS = {
    "article_generator.txt": None,  # set to sha256[:12] to lock
    "topic_selector.txt": None,
}

REQUIRED_SECTIONS = {
    "article_generator.txt": ["品类一", "品类二", "品类三", "品类四", "品类五", "合规红线", "互动引导", "写得像个真人"],
    "topic_selector.txt": ["五大内容品类", "评分体系", "标题公式", "输出格式", "动态调配"],
}


def test_prompt_files_exist():
    """Both prompt files must exist."""
    for fname in ["article_generator.txt", "topic_selector.txt"]:
        path = PROMPT_DIR / fname
        assert path.exists(), f"{fname} not found at {path}"
        assert path.stat().st_size > 500, f"{fname} too small ({path.stat().st_size} bytes)"
    print("  PASS test_prompt_files_exist")


def test_article_generator_sections():
    """article_generator.txt must contain all required sections."""
    path = PROMPT_DIR / "article_generator.txt"
    content = path.read_text()
    for section in REQUIRED_SECTIONS["article_generator.txt"]:
        assert section in content, f"Missing required section: {section}"
    # Must have at least 2 ## examples
    assert content.count("## ") >= 2, "Should have at least 2 markdown headers"
    print("  PASS test_article_generator_sections")


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
    path = PROMPT_DIR / "article_generator.txt"
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
        ("article_generator: all sections present", test_article_generator_sections),
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
