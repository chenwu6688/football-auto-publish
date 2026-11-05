"""数据图（正文事实重绘）的契约测试。

为什么这些用例值得写：数据图是「零版权配图」方案的地基，一旦它开始画正文里
不存在的东西，这个方案就从「合规」变成「用长得像数据的假图骗读者」，比配错
新闻图更糟。所以下面一半用例是**负向**的：没有数据必须返回 None，绝不补画。
"""

import ast
import json
import tempfile
import unittest
from pathlib import Path

import data_chart as dc

ROOT = Path(__file__).resolve().parents[1]

TABLE = [
    {"position": 1, "team": "FC Barcelona", "points": 34, "played": 13, "goal_diff": 21},
    {"position": 2, "team": "Real Madrid CF", "points": 27, "played": 13, "goal_diff": 14},
    {"position": 3, "team": "Club Atlético de Madrid", "points": 25, "played": 13, "goal_diff": 11},
]

FIXTURE = {
    "home_team": "Real Madrid CF", "away_team": "Valencia CF",
    "home_score": 2, "away_score": 1, "league": "西甲",
    "goals": [
        {"minute": 23, "scorer": "姆巴佩", "scorer_team": "home"},
        {"minute": 67, "scorer": "索莱尔", "scorer_team": "away"},
        {"minute": 88, "scorer": "贝林厄姆", "scorer_team": "home"},
    ],
}


def _need_font(case: unittest.TestCase) -> None:
    ok, detail = dc.font_status()
    if not ok:
        case.skipTest(f"无中文字体，图画不出来也无法判定：{detail}")


class EntityResolutionTest(unittest.TestCase):
    """积分榜给英文名、正文写中文、正文还可能用全称——三者必须能归一。"""

    def test_english_team_resolves_to_chinese(self):
        item = dc.resolve_team("Real Madrid CF")
        self.assertIsNotNone(item, "英文队名反查失败 → 积分榜图永远出不来")
        self.assertEqual(item["name_zh"], "皇马")

    def test_full_chinese_name_is_a_valid_query(self):
        self.assertIsNotNone(dc.resolve_team("皇家马德里"),
                             "正文写全称时反查不到实体 → 相关性会被误判为「没提到」")

    def test_full_name_is_in_entity_names(self):
        item = dc.resolve_team("Real Madrid CF")
        names = dc.entity_names(item)
        self.assertIn("皇家马德里", names)
        self.assertIn("皇马", names)

    def test_unknown_name_returns_none(self):
        self.assertIsNone(dc.resolve_team("某支不存在的球队"),
                          "查不到就该返回 None，不能瞎猜一支队")


class NegativeTest(unittest.TestCase):
    """没有数据就不画。这半边比正向用例更重要。"""

    def test_goals_without_events_returns_none(self):
        _need_font(self)
        tmp = Path(tempfile.mkdtemp())
        got = dc.chart_goals({**FIXTURE, "goals": []}, out_dir=tmp, filename="x.jpg")
        self.assertIsNone(got, "没有进球事件却画了一张时间轴 = 画假图")

    def test_score_without_score_returns_none(self):
        _need_font(self)
        tmp = Path(tempfile.mkdtemp())
        got = dc.chart_score({"home_team": "A队", "away_team": "B队"},
                             out_dir=tmp, filename="x.jpg")
        self.assertIsNone(got, "缺比分却画了一张比分卡 = 画假图")

    def test_standings_without_points_returns_none(self):
        _need_font(self)
        tmp = Path(tempfile.mkdtemp())
        row = {"position": 2, "team": "Real Madrid CF", "points": None, "played": 13}
        got = dc.chart_standings(dc.resolve_team("Real Madrid CF"), row, TABLE, "西甲",
                                 out_dir=tmp, filename="x.jpg")
        self.assertIsNone(got)


class RenderTest(unittest.TestCase):

    def test_standings_anchor_matches_what_is_drawn(self):
        _need_font(self)
        tmp = Path(tempfile.mkdtemp())
        item = dc.resolve_team("Real Madrid CF")
        got = dc.chart_standings(item, TABLE[1], TABLE, "西甲", out_dir=tmp,
                                  filename="s.jpg")
        self.assertIsNotNone(got)
        self.assertTrue(Path(got["path"]).exists())
        self.assertEqual(set(got["anchor"]["numbers"]), {"2", "27"},
                         "anchor 的数字必须是图上真画出来的名次与积分")
        self.assertIn("皇马", got["anchor"]["entities"])

    def test_goals_anchor_covers_both_teams(self):
        _need_font(self)
        tmp = Path(tempfile.mkdtemp())
        got = dc.chart_goals(FIXTURE, out_dir=tmp, filename="g.jpg")
        self.assertIsNotNone(got)
        self.assertEqual(set(got["anchor"]["entities"]), {"皇马", "瓦伦西亚"},
                         "两队都要进 anchor，少一队就拦不住「配了另一场球」")

    def test_rendered_size_passes_body_gate(self):
        _need_font(self)
        from PIL import Image
        tmp = Path(tempfile.mkdtemp())
        got = dc.chart_score(FIXTURE, out_dir=tmp, filename="c.jpg")
        with Image.open(got["path"]) as im:
            self.assertGreaterEqual(im.size[0], 600)
            self.assertGreaterEqual(im.size[1], 400)


class CandidatesTest(unittest.TestCase):
    """candidates 只负责「图画不画得出」，相关性交给 image_relevance。"""

    def test_only_matches_articles_own_match(self):
        _need_font(self)
        tmp = Path(tempfile.mkdtemp())
        art = {"title": "皇马2-1瓦伦西亚，姆巴佩破荒",
               "content": "皇家马德里主场击败瓦伦西亚。"}
        got = dc.candidates(art, {"all_fixtures": [FIXTURE]}, out_dir=tmp, prefix="a")
        kinds = {c["kind"] for c in got}
        self.assertTrue(kinds & {"goals", "score"}, "正文提到两队却没出比赛图")

    def test_unrelated_article_gets_no_match_chart(self):
        _need_font(self)
        tmp = Path(tempfile.mkdtemp())
        art = {"title": "英超争冠悬念还剩多少", "content": "阿森纳与曼城的差距在缩小。"}
        got = dc.candidates(art, {"all_fixtures": [FIXTURE]}, out_dir=tmp, prefix="b")
        self.assertNotIn("goals", {c["kind"] for c in got})
        self.assertNotIn("score", {c["kind"] for c in got})

    def test_no_match_data_returns_empty(self):
        got = dc.candidates({"title": "皇马", "content": "皇马"}, {})
        self.assertEqual(got, [], "没有比赛数据就不该产出任何数据图")


class WiringTest(unittest.TestCase):
    """防止「模块写好了但没接上」——本项目此前吃过这个亏。"""

    def test_orchestrator_calls_data_chart_and_gate(self):
        tree = ast.parse((ROOT / "orchestrator.py").read_text(encoding="utf-8"))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                called.add(node.attr)
        self.assertIn("candidates", called, "保存层没有调用 data_chart.candidates")
        self.assertIn("filter_candidates", called,
                      "数据图没过相关性闸门就直接入库了")
        self.assertIn("image_anchors", called.union(
            {k for n in ast.walk(tree) if isinstance(n, ast.Constant)
             for k in [n.value] if isinstance(n.value, str)}),
            "metadata 没落 image_anchors，配图相关性不可追溯")


class SelfTestTest(unittest.TestCase):

    def test_module_selftest_passes(self):
        _need_font(self)
        failed = [name for name, ok in dc.selftest() if not ok]
        self.assertEqual(failed, [], f"模块自检未通过：{failed}")


if __name__ == "__main__":
    unittest.main()
