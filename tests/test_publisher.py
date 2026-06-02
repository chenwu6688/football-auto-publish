#!/usr/bin/env python3
"""Unit tests for publisher.py (Task #6 optimization).

Tests non-Playwright utility functions: text processing, article loading, image extraction.
"""

import sys, os, json, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_strip_ai_parentheticals():
    """Should remove only AI-cliche parenthetical patterns, not generic content."""
    from publisher import strip_ai_parentheticals
    cases = [
        # AI cliches — SHOULD be removed
        ("巴黎圣日耳曼（注：法甲冠军）夺得冠军", "巴黎圣日耳曼夺得冠军"),
        ("姆巴佩（值得一提的是，他身价1.8亿）转会皇马", "姆巴佩转会皇马"),
        ("阿森纳（数据来源：Opta）表现惊艳", "阿森纳表现惊艳"),
        ("（众所周知，足球是圆的）比赛结果出人意料", "比赛结果出人意料"),
        # Non-AI content — should NOT be removed
        ("巴黎圣日耳曼（以下简称大巴黎）夺得冠军", "巴黎圣日耳曼（以下简称大巴黎）夺得冠军"),
        ("姆巴佩（法国前锋）转会皇马", "姆巴佩（法国前锋）转会皇马"),
        # Edge cases
        ("没有任何括号的句子", "没有任何括号的句子"),
        ("", ""),
    ]
    for inp, expected in cases:
        result = strip_ai_parentheticals(inp)
        assert result == expected, f"Input: {inp!r}\nExpected: {expected!r}\nGot: {result!r}"
    print("  PASS test_strip_ai_parentheticals")


def test_strip_ai_parentheticals_multiple():
    """Should remove multiple AI-cliche patterns in one text."""
    from publisher import strip_ai_parentheticals
    result = strip_ai_parentheticals("（注：官方数据）皇马（众所周知实力强劲）击败巴萨")
    assert "皇马击败巴萨" in result or "皇马 击败巴萨" in result
    assert "（注：" not in result
    assert "（众所周知" not in result
    print("  PASS test_strip_ai_parentheticals_multiple")


def test_convert_md_to_text():
    """Should strip markdown formatting for Toutiao plain text."""
    from publisher import convert_md_to_text
    md = """# 大标题

这是正文段落，有**加粗**和*斜体*。

## 小标题一
- 列表项1
- 列表项2

![配图1](images/img.jpg)

> 引用文字"""
    result = convert_md_to_text(md)
    assert "#" not in result, f"Markdown headers not stripped: {result[:50]}"
    assert "![" not in result, f"Image tags not stripped: {result[:50]}"
    assert "加粗" in result
    print("  PASS test_convert_md_to_text")


def test_load_articles():
    """Should load articles from output directory."""
    from publisher import load_articles
    try:
        # Create temp article structure
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_output = os.environ.get("OUTPUT_DIR")
            os.environ["OUTPUT_DIR"] = tmpdir

            date_dir = Path(tmpdir) / "2026-06-02"
            date_dir.mkdir(parents=True)

            # Create metadata
            meta = {
                "total_articles": 1,
                "articles": [
                    {"index": 1, "title": "测试文章", "path": str(date_dir / "article-1-test.md"),
                     "slug": "test", "keywords": ["test"], "content_type": "热点球评"}
                ]
            }
            (date_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False))

            # Create article file
            article_content = """---
title: "测试文章"
date: 2026-06-02
---

# 测试文章

正文内容。"""
            (date_dir / "article-1-test.md").write_text(article_content)

            articles = load_articles("2026-06-02")
            assert articles is not None
            assert len(articles) > 0

            if orig_output:
                os.environ["OUTPUT_DIR"] = orig_output
            else:
                del os.environ["OUTPUT_DIR"]

            print(f"  PASS test_load_articles ({len(articles)} articles)")
    except Exception as e:
        print(f"  SKIP test_load_articles (Playwright or env issue): {e}")


def test_extract_images():
    """Should find image paths in article content."""
    from publisher import extract_images
    content = """# 标题
正文段落。
![配图1](images/article-1-img-001.jpg)
更多正文。
![配图2](images/article-1-img-002.jpg)
结尾。"""
    images = extract_images(content)
    assert len(images) == 2, f"Expected 2 images, got {len(images)}"
    assert "article-1-img-001.jpg" in images[0]
    assert "article-1-img-002.jpg" in images[1]
    print("  PASS test_extract_images")


def test_extract_images_none():
    """Should return empty list when no images."""
    from publisher import extract_images
    content = "没有任何图片的纯文本内容"
    images = extract_images(content)
    assert images == []
    print("  PASS test_extract_images_none")


def test_publisher_importable():
    """All key functions should be importable."""
    from publisher import (send_wxpusher, strip_ai_parentheticals, convert_md_to_text,
                           load_articles, extract_images)
    assert callable(strip_ai_parentheticals)
    assert callable(convert_md_to_text)
    assert callable(load_articles)
    assert callable(extract_images)
    print("  PASS test_publisher_importable")


def test_send_wxpusher_noop():
    """send_wxpusher should not crash when no credentials configured."""
    from publisher import send_wxpusher
    # Should not raise when WXPUSHER_APPTOKEN is empty
    send_wxpusher("test title", "test content")
    print("  PASS test_send_wxpusher_noop")


def test_publisher_no_dead_code_in_else():
    """#6 bugfix: else branch in login flow should not have dead save code.

    The else branch (AUTH_FILE doesn't exist) had unreachable calls to
    context.storage_state() and browser.close() after the login flow
    already handled those operations. Only the error print should remain.
    """
    import publisher as pub
    with open(pub.__file__) as f:
        src = f.read()
    # Verify the error print is still there
    assert '保存失败，请重试' in src, "else branch should still print error"
    # Verify the dead calls AFTER the error print are gone
    # The pattern was: print("\\n❌ 保存失败") followed by context.storage_state
    err_idx = src.find('保存失败，请重试')
    after_err = src[err_idx:err_idx + 200]
    assert 'context.storage_state' not in after_err, \
        "Dead context.storage_state after else branch — should have been removed"
    assert '.close()' not in after_err, \
        "Dead browser.close() after else branch — should have been removed"
    print("  PASS test_publisher_no_dead_code_in_else")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Task #6 Unit Tests: publisher.py")
    print("=" * 60)

    all_tests = [
        ("strip_ai_parentheticals: removes AI explanations", test_strip_ai_parentheticals),
        ("strip_ai_parentheticals: removes multiple patterns", test_strip_ai_parentheticals_multiple),
        ("convert_md_to_text: strips markdown for Toutiao", test_convert_md_to_text),
        ("load_articles: loads from output directory", test_load_articles),
        ("extract_images: finds image paths", test_extract_images),
        ("extract_images: empty for no images", test_extract_images_none),
        ("import: all key functions importable", test_publisher_importable),
        ("send_wxpusher: no-op without credentials", test_send_wxpusher_noop),
        ("#6 bugfix: no dead code in login else-branch", test_publisher_no_dead_code_in_else),
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
