# -*- coding: utf-8 -*-
"""对标抓取解析的单测（2026-11-01）。

重点守住三件事：
  1. 主页文本解析不许把「简介/粉丝数」当成作品标题（首次实现就踩了）；
  2. 视频号的行形态（`03:06` 时长行 + `N播放`）必须能解析，且单位不许混算；
  3. 抓不到就返回空/None，绝不用 0 冒充（本项目硬纪律）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import title_features as tf  # noqa: E402
from scripts import benchmark_peers as bp  # noqa: E402


ARTICLE_PAGE = """关注
推荐
武汉
搜索
消息
发布
登录
西甲二人转
208.5万获赞23.0万粉丝701关注

认证：资深足球记者

简介：西甲以及欧洲足球最新消息，梅西C罗资讯

更多信息
关注
全部文章视频微头条小视频
0-1！曼城陷入泥潭：布莱顿反客为主，“蓝月亮”场内外危机交织
55阅读1评论3小时前
红黑军团降服蓝黑军团：莫雷拉闪耀圣西罗，国米八战德比难求一胜
8阅读3小时前
发泄怒火！巴萨6-1狂胜阿拉维斯，开启西甲“疗愈模式”
480阅读3小时前
"""

VIDEO_PAGE = """关注
推荐
登录
足球记忆FM
161.5万获赞30.7万粉丝15关注

认证：优质体育领域创作者

简介：追寻绿茵足迹，点燃激情人生

更多信息
全部文章视频微头条小视频
11:55
足坛十大遗憾—可惜足球没有如果！ #足球记忆 #足球的魅力
1.1万播放26评论前天18:56
03:06
曾经他是悲情的“凯恩叔叔”，如今他加冕金球先生 #凯恩
1.6万播放20评论5天前
皇马2-1巴萨，穆里尼奥重返国家德比 #国家德比
12万播放159评论6天前
"""


class TestParseArticle(unittest.TestCase):
    def test_rows_parsed(self):
        rows = bp.parse_home_text(ARTICLE_PAGE)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["views"], 55)
        self.assertEqual(rows[0]["comments"], 1)
        self.assertEqual(rows[1]["comments"], 0)
        self.assertEqual(rows[2]["views"], 480)

    def test_unit_is_read(self):
        rows = bp.parse_home_text(ARTICLE_PAGE)
        self.assertTrue(all(r["unit"] == "阅读" for r in rows))

    def test_profile_lines_not_taken_as_title(self):
        """账号简介、粉丝数行绝不能被当成作品标题（首次实现 bug）。"""
        titles = [r["title"] for r in bp.parse_home_text(ARTICLE_PAGE)]
        self.assertNotIn("简介：西甲以及欧洲足球最新消息，梅西C罗资讯", titles)
        for t in titles:
            self.assertNotIn("粉丝", t)
            self.assertNotIn("获赞", t)

    def test_dedup_by_title(self):
        dup = ARTICLE_PAGE + "\n55阅读1评论3小时前\n"
        rows = bp.parse_home_text(dup)
        titles = [r["title"] for r in rows]
        self.assertEqual(len(titles), len(set(titles)), "重复标题必须去重")


class TestParseVideo(unittest.TestCase):
    def test_video_rows_parsed(self):
        rows = bp.parse_home_text(VIDEO_PAGE)
        self.assertEqual(len(rows), 3, f"实际解析: {[r['title'][:16] for r in rows]}")
        self.assertTrue(all(r["unit"] == "播放" for r in rows))

    def test_duration_line_skipped(self):
        """`03:06` 这种时长行不是标题，必须回退到上一行取真标题。"""
        rows = bp.parse_home_text(VIDEO_PAGE)
        titles = [r["title"] for r in rows]
        for t in titles:
            self.assertNotRegex(t, r"^\d{1,2}:\d{2}$")
        self.assertIn("曾经他是悲情的“凯恩叔叔”，如今他加冕金球先生 #凯恩", titles)

    def test_wan_conversion(self):
        rows = bp.parse_home_text(VIDEO_PAGE)
        get = lambda kw: next(r for r in rows if kw in r["title"])  # noqa: E731
        self.assertEqual(get("皇马2-1巴萨")["views"], 120000)
        self.assertEqual(get("凯恩叔叔")["views"], 16000)
        self.assertEqual(get("足坛十大遗憾")["views"], 11000)


class TestFollowers(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(bp.parse_followers(ARTICLE_PAGE, "x"), 230000)
        self.assertEqual(bp.parse_followers(VIDEO_PAGE, "x"), 307000)

    def test_missing_returns_none_not_zero(self):
        """抓不到粉丝数就返回 None —— 填 0 会被下游当成"真 0 粉"统计。"""
        self.assertIsNone(bp.parse_followers("随便一段没有粉丝数的文本", "x"))


class TestReport(unittest.TestCase):
    def test_mixed_units_split(self):
        """同一账号混发图文+视频时，主单位取多数，且要标 mixed_units。"""
        peers = {"混发号": bp.parse_home_text(ARTICLE_PAGE) * 1 + bp.parse_home_text(VIDEO_PAGE) * 2}
        rep = bp.build_report(peers, [], {})
        s = rep["peers"]["混发号"]
        self.assertEqual(s["unit"], "播放")
        self.assertTrue(s["mixed_units"])

    def test_single_unit_not_mixed(self):
        rep = bp.build_report({"纯图文": bp.parse_home_text(ARTICLE_PAGE)}, [], {})
        s = rep["peers"]["纯图文"]
        self.assertEqual(s["unit"], "阅读")
        self.assertFalse(s["mixed_units"])

    def test_ours_empty_ok(self):
        """我方没有绩效数据时不许炸，且不给假数字。"""
        rep = bp.build_report({"纯图文": bp.parse_home_text(ARTICLE_PAGE)}, [], {})
        self.assertEqual(rep["ours"]["n"], 0)

    def test_md_renders_without_crash(self):
        rep = bp.build_report({"纯图文": bp.parse_home_text(ARTICLE_PAGE)},
                              [{"title": "让3追4！大逆转绝杀", "views": 100}], {"纯图文": 150000})
        md = bp.render_md(rep)
        self.assertIn("头部账号对标基线", md)
        self.assertIn("纯图文", md)
        self.assertIn("我方（同期）", md)
        self.assertIn("必须一起读的局限", md)  # 局限必须随报告一起出，不许省


class TestCaveatDiscipline(unittest.TestCase):
    def test_caveats_cover_known_traps(self):
        """四条已知陷阱必须在报告局限里出现，否则以后没人记得。"""
        rep = bp.build_report({"a": bp.parse_home_text(ARTICLE_PAGE)}, [], {})
        text = " ".join(rep["caveats"])
        for kw in ("口径", "滚动窗口", "跨账号", "曝光"):
            with self.subTest(kw=kw):
                self.assertIn(kw, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
