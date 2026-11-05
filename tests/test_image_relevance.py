"""配图相关性闸门的契约测试。

用户原话：「要保证图片的相关性，不能偏离文章主题」。这条用嘴保证不了，
所以闸门必须有一批**负向**用例证明它真的会拦人——尤其是「国际米兰」冒充
「AC米兰」这类串味，光靠 String in text 是拦不住的。
"""

import unittest

import image_relevance as ir


def _art(title, content=""):
    return {"title": title, "content": content}


class EntityGateTest(unittest.TestCase):

    def test_all_entities_present_passes(self):
        r = ir.check({"entities": ["皇马", "瓦伦西亚"], "numbers": ["2", "1"]},
                     _art("皇马2-1瓦伦西亚，姆巴佩破荒", "皇家马德里主场拿下瓦伦西亚。"))
        self.assertTrue(r["ok"], r["reason"])
        self.assertEqual(r["confidence"], "high")

    def test_one_team_missing_is_rejected(self):
        """文章只讲皇马引援，却给皇马打瓦伦西亚的图——必须拒。"""
        r = ir.check({"entities": ["皇马", "瓦伦西亚"], "numbers": ["2", "1"]},
                     _art("皇马冬窗有意签下这名中卫", "皇马正在评估后防补强。"))
        self.assertFalse(r["ok"])
        self.assertIn("瓦伦西亚", r["entities_missing"])

    def test_full_name_in_article_matches_short_entity(self):
        r = ir.check({"entities": ["皇马"], "numbers": []},
                     _art("皇家马德里27分紧追不舍", "巴萨领跑。"))
        self.assertTrue(r["ok"], "正文写全称、实体是简称，必须能对上")

    def test_substring_collision_does_not_pass(self):
        """正文写「国际米兰」，不能让 AC 米兰的图蒙混过关。"""
        r = ir.check({"entities": ["AC米兰"], "numbers": []},
                     _art("国际米兰1-0小胜", "蓝黑军团拿下三分。"))
        self.assertFalse(r["ok"], "「米兰」被「国际米兰」吞掉，不能算 AC 米兰出现")

    def test_own_team_still_passes_after_collision_fix(self):
        r = ir.check({"entities": ["AC米兰"], "numbers": []},
                     _art("AC米兰1-0小胜", "红黑军团拿下三分。"))
        self.assertTrue(r["ok"], "别把串味修正做成「一律拒绝」")


class NumberGateTest(unittest.TestCase):

    def test_no_number_hit_is_rejected(self):
        r = ir.check({"entities": ["皇马"], "numbers": ["2", "27"]},
                     _art("皇马这场比赛怎么评价", "过程比结果难看。"))
        self.assertFalse(r["ok"])
        self.assertIn("数字", r["reason"])

    def test_partial_number_hit_downgrades_confidence(self):
        r = ir.check({"entities": ["皇马"], "numbers": ["27", "34"]},
                     _art("皇马27分紧追", "还差得远。"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["confidence"], "medium",
                         "只命中部分数字要降档留痕，不能假装 high")

    def test_numbers_compared_as_tokens(self):
        """正文写「2比1」，数据是 2/1 —— 必须命中，不能因为分隔符不同就判无关。"""
        r = ir.check({"entities": ["皇马"], "numbers": ["2", "1"]},
                     _art("皇马2比1拿下", ""))
        self.assertTrue(r["ok"])


class AnchorPresenceTest(unittest.TestCase):

    def test_missing_anchor_is_rejected(self):
        self.assertFalse(ir.check({}, _art("随便", "随便"))["ok"])
        self.assertFalse(ir.check(None, _art("随便", "随便"))["ok"])

    def test_anchor_without_entity_and_number_is_rejected(self):
        r = ir.check({"kind": "x"}, _art("皇马", "皇马"))
        self.assertFalse(r["ok"], "无法归因的图不允许进正文")

    def test_empty_article_is_not_a_pass(self):
        r = ir.check({"entities": ["皇马"]}, _art("", ""))
        self.assertFalse(r["ok"], "正文为空是「未判定」，不等于通过")


class FilterTest(unittest.TestCase):

    def test_split_into_kept_and_dropped_with_reason(self):
        cands = [
            {"kind": "goals", "anchor": {"entities": ["皇马"], "numbers": ["2"]}},
            {"kind": "standings", "anchor": {"entities": ["巴萨"], "numbers": ["34"]}},
        ]
        kept, dropped = ir.filter_candidates(cands, _art("皇马2球拿下", ""))
        self.assertEqual([c["kind"] for c in kept], ["goals"])
        self.assertEqual([c["kind"] for c in dropped], ["standings"])
        self.assertTrue(dropped[0]["reject_reason"], "被拒必须留下原因，否则又是黑箱")

    def test_kept_candidates_carry_relevance_proof(self):
        kept, _ = ir.filter_candidates(
            [{"kind": "goals", "anchor": {"entities": ["皇马"], "numbers": ["2"]}}],
            _art("皇马2球拿下", ""))
        self.assertEqual(kept[0]["relevance"]["confidence"], "high")


if __name__ == "__main__":
    unittest.main()
