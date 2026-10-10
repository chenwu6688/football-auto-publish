"""反洗稿双保险（计划 11.3 / 12.3）单元测试。

两道防线：
- 相似度闸门：连续重合 > 12 字，或 8-gram 重合率 > 15% → 判洗稿；
- 信息增量强制：事实点数不少于最强源文，且至少新增 2 条源文没有的事实。
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import (
    check_similarity_gate,
    check_information_increment,
    extract_source_texts,
    _longest_shared_run,
    _gram_overlap_ratio,
)

SRC = ("曼城主场3比1战胜利物浦，哈兰德梅开二度，德布劳内送出两次助攻，"
       "曼城全场控球率62%，射门18次。这场比赛之后曼城积45分排名英超第二。")


class TestSimilarityGate(unittest.TestCase):
    def test_copied_text_is_flagged(self):
        passed, issues, metrics = check_similarity_gate({"content": SRC}, [SRC])
        self.assertFalse(passed)
        self.assertGreater(metrics["max_run"], 12)
        self.assertAlmostEqual(metrics["gram_ratio"], 1.0, places=2)

    def test_original_rewrite_passes(self):
        rewritten = {"content": "蓝月亮在伊蒂哈德球场拿下红军，比分定格在三比一。"
                                "挪威锋霸包办两粒进球，比利时中场两次喂饼。"
                                "主场球队控球略占上风，射门次数接近二十次。"
                                "此役过后，他们暂列积分榜次席，争冠悬念再起。"}
        passed, issues, _ = check_similarity_gate(rewritten, [SRC])
        self.assertTrue(passed, f"原创改写应通过，却被拦: {issues}")

    def test_no_source_texts_passes(self):
        passed, issues, _ = check_similarity_gate({"content": SRC}, [])
        self.assertTrue(passed)

    def test_longest_shared_run_basic(self):
        self.assertEqual(_longest_shared_run("abcdefghijklm", "xxabcdefghijklmyy"), 13)

    def test_gram_ratio_identical(self):
        self.assertAlmostEqual(_gram_overlap_ratio("aaaaaaaaaa", "aaaaaaaaaa", 8), 1.0)


class TestInformationIncrement(unittest.TestCase):
    def test_thin_rewrite_fails(self):
        thin = {"content": "曼城赢了利物浦，哈兰德进了两个球。"}
        passed, issues, metrics = check_information_increment(thin, [SRC])
        self.assertFalse(passed)
        self.assertLess(metrics["new_facts"], 2)

    def test_increment_with_two_new_facts(self):
        # 新增「第12粒联赛进球」「近5个主场不败」两条源文没有的事实
        art = {"content": "哈兰德本赛季已打进12粒联赛进球。曼城近5个主场对利物浦保持不败。"
                          "此役挪威人梅开二度。"}
        passed, issues, metrics = check_information_increment(
            art, [SRC], require_count_parity=False)
        self.assertTrue(passed, f"应通过，却被拦: {issues}")
        self.assertGreaterEqual(metrics["new_facts"], 2)

    def test_count_parity_flag(self):
        art = {"content": "哈兰德本赛季已打进12粒联赛进球，另有近5个主场不败纪录。"}
        p_off, _, _ = check_information_increment(art, [SRC], require_count_parity=False)
        p_on, issues_on, _ = check_information_increment(art, [SRC], require_count_parity=True)
        self.assertTrue(p_off)
        self.assertFalse(p_on)
        self.assertTrue(any("少于最强源文" in i for i in issues_on))


class TestExtractSourceTexts(unittest.TestCase):
    def test_collects_and_dedups(self):
        src = {"article_text": "A" * 120, "fixture": {"article_text": "B" * 120, "content": "B" * 120}}
        texts = extract_source_texts(src)
        self.assertEqual(len(texts), 2)   # fixture 内重复的 B 文本被去重

    def test_short_texts_ignored(self):
        self.assertEqual(extract_source_texts({"article_text": "短"}), [])


if __name__ == "__main__":
    unittest.main()
