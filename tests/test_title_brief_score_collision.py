"""`score` 同名字段撞车回归（2026-11-05 晚间批次 2/3 篇丢失的根因）。

线上形态：LLM 产出的选题自带 ``"score": 90``（选题**排序分**），而事实卡里的
``score`` 才是比分 ``{"home": 1, "away": 9}``。``title_brief.collect`` 用
``facts.get("score") or {}`` 取值 → 拿到 int 90 → ``sc.get("home")``
→ ``AttributeError: 'int' object has no attribute 'get'``。

更严重的是它发生在「标题重拟」这个只影响增量的环节，却把整篇稿子丢了：
两条稿各重试 3 次全崩，本批 3 篇只活下 1 篇预测稿，而预测稿不发头条 → 0 篇发布。

断言口径：
- 非 dict 一律当**缺失**（numbers 为空 + missing 含「数字」），绝不把排序分当比分；
- 真正是 dict 的比分不得受影响（防修完反而丢能力）。
"""

import pytest

import title_brief as tb


LLM_TOPIC = {
    "title": "1-9！朗斯主场被打成筛子，科莫这场球到底谁该脸红",
    "angle": "科莫血洗朗斯",
    "score": 90,                      # ← 选题排序分，不是比分
    "keywords_cn": ["朗斯", "科莫", "法甲"],
    "content_type": "热点球评",
}

FACT_CARD = {
    "home_zh": "朗斯",
    "away_zh": "科莫",
    "competition": "法甲",
    "score": {"home": 1, "away": 9},
    "home_standing": {"points": 22, "position": 7},
    "away_standing": {"points": 31, "position": 9},
}


class TestScoreCollision:
    @pytest.mark.parametrize("bad", [90, 90.5, "90", True, [1, 9]])
    def test_non_dict_score_does_not_raise(self, bad):
        """非 dict 的 score（排序分 / 字符串 / 布尔 / 列表）都不得抛异常。"""
        topic = dict(LLM_TOPIC, score=bad)
        c = tb.collect(topic)                 # 不抛就算过
        assert "numbers" in c and "missing" in c

    def test_non_dict_score_is_missing_not_zero(self):
        """缺失就写缺失：不得因为拿到 90 就拼出「朗斯90-0科莫」这类假比分。"""
        c = tb.collect(dict(LLM_TOPIC))
        assert c["numbers"] == []
        assert "数字" in c["missing"]
        assert not any("-" in n for n in c["numbers"])

    def test_dict_score_still_works(self):
        """真比分（dict）必须照旧取得到，修 bug 不能把能力改没。"""
        c = tb.collect(FACT_CARD)
        assert "朗斯1-9科莫" in c["numbers"]
        assert "朗斯22分" in c["numbers"]
        assert "1-9 的比分对照" in c["contrasts"]

    def test_int_score_with_dict_score_present(self):
        """topic 有排序分、事实卡有真比分：真比分优先（facts 覆盖 topic）。"""
        c = tb.collect(LLM_TOPIC, FACT_CARD)
        assert "朗斯1-9科莫" in c["numbers"]


class TestOtherSameShapeFields:
    @pytest.mark.parametrize("field,value", [
        ("goals", 3),                       # 总进球数（int）不是进球事件列表
        ("goals", "3"),
        ("home_standing", 7),               # 排名数字不是积分 dict
        ("away_standing", "9"),
        ("keywords_cn", 3),
    ])
    def test_bad_shape_field_does_not_raise(self, field, value):
        topic = dict(LLM_TOPIC, **{field: value})
        tb.collect(topic)

    def test_goals_list_with_non_dict_items(self):
        """进球列表里混入字符串/数字也不得抛。"""
        c = tb.collect(dict(FACT_CARD, goals=[{"player_zh": "穆萨"}, "unknown", 3, None]))
        assert "穆萨" in c["proper"]


class TestRenderPathsSurvive:
    """三个渲染入口都会被「标题重拟」调用，全部不得因撞车崩溃。"""

    def test_brief_text(self):
        text = tb.brief_text(LLM_TOPIC)
        assert "标题素材" in text
        assert "禁止自己造数" in text        # 无数字时明说没有，而不是编一个

    def test_candidates(self):
        assert isinstance(tb.candidates(LLM_TOPIC), list)

    def test_candidates_with_fact_card(self):
        assert isinstance(tb.candidates(LLM_TOPIC, FACT_CARD), list)
