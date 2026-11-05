"""赛前预测稿分级发布（2026-11-05 立）。

## 事故

晚间批次 3 篇成稿：2 篇被 `score` 字段撞车丢掉，唯一活下来的是赛前预测稿；
而 publisher 对预测稿是**整类硬跳过**（R13 决策④）→ 本批 0 篇可发 →
`sys.exit(1)` → 整批判失败，批次还被写成「已完成」，补发被幂等跳过。

## 本文件钉住的判断

1. 判据按**依据**分级，不按体裁：有据前瞻照发，观点稿照样不发；
2. 跳过的原因必须能说清（tier + 具体理由），不是「预测稿」三个字；
3. 口径唯一：publisher 与看板/配额四处必须用同一个 `counts_as_content`，
   否则「今天发了几篇」会各说各话；
4. 批内配额：预测稿最多占 1 篇，不能挤掉主稿。
"""

import pytest

import prediction_gate as pg


FWD_META = {
    "title": "勒沃库森5轮4胜对马赛3轮1胜，谁先掉队",
    "is_prediction": "true",
    "prediction_basis": "3",
    "prediction_basis_items": "积分排名,近期战绩,主场战绩",
}


class TestTier:
    def test_非预测稿直接放行(self):
        assert pg.assess({"title": "科莫9比1血洗朗斯", "is_prediction": "false"})["publish"]

    def test_只有对阵名单判依据不足(self):
        v = pg.assess({"title": "勒沃库森遇马赛", "is_prediction": "true"})
        assert v["tier"] == "opinion_only"
        assert v["publish"] is False
        assert "依据不足" in v["reasons"][0]

    def test_有据前瞻放行(self):
        v = pg.assess(FWD_META)
        assert v["tier"] == "sourced_forward" and v["publish"] is True

    @pytest.mark.parametrize("bad", ["这场稳了，勒沃库森必赢", "精准预测：马赛拿下三分",
                                     "十拿九稳，客队带走胜利"])
    def test_断言词一律不发(self, bad):
        v = pg.assess({**FWD_META, "title": bad})
        assert v["tier"] == "assertive" and v["publish"] is False

    def test_押具体比分不发(self):
        v = pg.assess(FWD_META, body="我看好 2-0，勒沃库森赢两球。")
        assert v["tier"] == "score_guess"
        assert v["publish"] is False

    def test_回顾比分别误杀(self):
        """「上回合打成 2-0」是可核对事实，不是打赌——第一版正则就误判在这里。"""
        v = pg.assess(FWD_META, body="上回合两队打成 2-0，这次马赛换了三中卫。")
        assert v["tier"] == "sourced_forward"


class TestBasis:
    def test_只报条数没明细等于没依据(self):
        """自报一个裸数字不能当依据：明细不在就没法核对。"""
        assert pg.basis_of({**FWD_META, "prediction_basis_items": ""}) == []

    def test_明细多于条数按条数算(self):
        m = {**FWD_META, "prediction_basis": "1",
             "prediction_basis_items": "a,b,c"}
        assert pg.basis_of(m) == ["a"]

    @pytest.mark.parametrize("bad", ["abc", None, "", "  "])
    def test_脏值当缺失(self, bad):
        assert pg.basis_of({**FWD_META, "prediction_basis": bad}) == [] or \
            len(pg.basis_of({**FWD_META, "prediction_basis": bad})) <= 3

    def test_列表形式明细也认(self):
        m = {**FWD_META, "prediction_basis": 2,
             "prediction_basis_items": ["积分排名", "近期战绩"]}
        assert pg.basis_of(m) == ["积分排名", "近期战绩"]


class TestQuotaAndScope:
    def test_批内只放一篇(self):
        assert pg.quota_left(0) == 1
        assert pg.quota_left(1) == 0
        assert pg.quota_left(5) == 0

    def test_口径函数_有据前瞻计入供给(self):
        assert pg.counts_as_content(FWD_META) is True

    def test_口径函数_观点稿不计入(self):
        assert pg.counts_as_content({"title": "x", "is_prediction": "true"}) is False

    def test_口径函数_非预测稿一定计入(self):
        assert pg.counts_as_content({"title": "x", "is_prediction": "false"}) is True

    def test_口径函数_历史数据按标题对拍(self):
        """历史 metadata 没有 is_prediction 键，只能靠选题级标记的标题对拍。"""
        hist = {"title": "明日预测"}
        assert pg.counts_as_content(hist, {"明日预测"}) is False
        assert pg.counts_as_content(hist, set()) is True

    def test_自检全通过(self):
        bad = [n for n, ok in pg.selftest() if not ok]
        assert not bad, f"prediction_gate 自检失败：{bad}"
