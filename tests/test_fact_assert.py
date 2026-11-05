"""事实断言校验（fact_assert）的回归用例。

这个模块替代的是「正文必须出现某队名」这类存在性校验，所以用例的重心不是
「能不能查到问题」，而是**两类错误都不能犯**：

- 误杀：历史上 4 次自动熔断的现场，全部是「写法不同/没写」，必须放过；
- 漏检：比分方向写反、比分数值造假、进球数造假、编造对阵，必须拦住。

少任何一侧，这套判据就会退化成「要么太松要么太紧」，而那正是过去一个月的状态。
"""

import unittest

import fact_assert


def _fx(home, away, hg, ag, goals=None):
    return {"home_team": home, "away_team": away, "home_score": hg,
            "away_score": ag, "goals": goals or []}


class TestHistoricalFalsePositives(unittest.TestCase):
    """4 次真实熔断的现场：全部必须放过（ok=True）。"""

    def test_bayern_fullname_alias(self):
        # 10-12：事实层是「拜仁」，正文写「拜仁慕尼黑」，判成「缺少客队名」
        res = fact_assert.verify(
            {"content": "马竞在主场0-2输给拜仁，防线两次被打穿。"},
            _fx("马竞", "拜仁慕尼黑", 0, 2))
        self.assertTrue(res["ok"], res["contradictions"])
        self.assertTrue(res["judged"])

    def test_short_name_dirty_suffix(self):
        # 10-13：away='大田市民首发'，正文写简称「大田」
        res = fact_assert.verify(
            {"content": "海港3-1赢球，大田首发名单里有朴规现。"},
            _fx("海港", "大田市民首发", 3, 1))
        self.assertTrue(res["ok"], res["contradictions"])

    def test_chinese_score_separator(self):
        # 10-24：正文写「0比1」，旧实现只认 ASCII 的 - 和 :
        res = fact_assert.verify(
            {"content": "蓉城0比1输给英博，领先3分却掉了链子。"},
            _fx("蓉城", "英博", 0, 1))
        self.assertTrue(res["ok"], res["contradictions"])

    def test_result_verb_glued_to_team(self):
        # 11-05：采集把「血洗」切进队名字段；正文里还有一个别场的旧比分 4-0
        res = fact_assert.verify(
            {"content": "朗斯在主场1-9被科莫血洗，法布雷加斯赛后只提了4-0赢曼联的旧账。"},
            _fx("朗斯", "科莫血洗", 1, 9))
        self.assertTrue(res["ok"], res["contradictions"])


class TestRealContradictions(unittest.TestCase):
    """真错误必须拦住——否则这套改造就变成了「用放松换绿灯」。"""

    def test_score_direction_reversed(self):
        res = fact_assert.verify({"content": "蓉城1-0英博，拿下关键三分。"},
                                 _fx("蓉城", "英博", 0, 1))
        self.assertFalse(res["ok"])
        self.assertEqual(res["contradictions"][0]["type"], "score_direction")

    def test_score_value_fabricated(self):
        res = fact_assert.verify({"content": "朗斯主场0-2输给科莫，全场被压制。"},
                                 _fx("朗斯", "科莫", 1, 9))
        self.assertFalse(res["ok"])

    def test_goal_claim_exceeds_actual(self):
        # 姆巴佩实际 1 球，正文写「梅开二度」
        res = fact_assert.verify({"content": "皇马2-1巴萨，姆巴佩梅开二度。"},
                                 _fx("皇马", "巴萨", 2, 1, [{"scorer_name": "姆巴佩"}]))
        self.assertFalse(res["ok"])
        self.assertEqual(res["contradictions"][0]["type"], "player_claim")

    def test_invented_fixture(self):
        # 事实卡是蓉城/英博，正文通篇写皇马/巴萨
        res = fact_assert.verify({"content": "皇马3-0大胜巴萨，姆巴佩独造3球。"},
                                 _fx("蓉城", "英博", 0, 1))
        self.assertFalse(res["ok"])
        self.assertEqual(res["contradictions"][0]["type"], "team_mismatch")


class TestMissingIsNotContradiction(unittest.TestCase):
    """缺失只记分，不拦——这是整个改造的核心口径。"""

    def test_pronoun_only_body_passes(self):
        res = fact_assert.verify(
            {"content": "这场比赛的过程比比分更难看，主队上半场就乱了。"},
            _fx("蓉城", "英博", 0, 1))
        self.assertTrue(res["ok"])
        self.assertGreater(len(res["missing"]), 0)
        self.assertLess(res["coverage"], 0.2)

    def test_other_match_score_is_skipped(self):
        # 「4-0赢曼联」是旧账，不能归因到本场
        res = fact_assert.verify(
            {"content": "朗斯1-9科莫，赛后他只提了4-0赢曼联的旧账。"},
            _fx("朗斯", "科莫", 1, 9))
        self.assertTrue(res["ok"], res["contradictions"])
        self.assertEqual(res["suspicious"], [])

    def test_empty_fact_card_is_unjudged(self):
        res = fact_assert.verify({"content": "随便一段话"}, _fx("", "", None, None))
        self.assertFalse(res["judged"], "事实卡为空必须标「未判定」，不能算通过")

    def test_empty_fact_card_not_counted_as_covered(self):
        res = fact_assert.verify({"content": "随便一段话"}, _fx("", "", None, None))
        self.assertEqual(res["coverage"], 0.0)


if __name__ == "__main__":
    unittest.main()
