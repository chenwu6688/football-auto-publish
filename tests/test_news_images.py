#!/usr/bin/env python3
"""新闻源正文图爬取（news_images）的单测。

这一层是「单篇至少 3 张配图」的第一道抓手，所以契约必须钉死：

1. **没有实体就不许爬** —— 否则等于自建随机图库（计划 2.2 红线）；
2. **只爬标题命中实体的报道** —— 图文相关靠这条保证，不是靠事后猜；
3. **正文容器外的图一律不要** —— 站内 logo/图标/占位图混进来就是「有图但不相关」；
4. **图床尺寸必须升级** —— 720×480 过不了下载闸门，表现是「爬到了但没落盘」的黑箱；
5. **任何网络/解析失败都返回空** —— 图源不是发布的前置条件。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import news_images as ni  # noqa: E402

INDEX_HTML = """
<ul>
  <li><a href="//news.zhibo8.com/zuqiu/2026-11-05/aaa111native.htm">皇马客战瓦伦西亚大名单出炉，姆巴佩领衔锋线</a></li>
  <li><a href="//news.zhibo8.com/zuqiu/2026-11-05/bbb222native.htm">中超最后两轮赛程调整，海港国安争冠悬念留到最后</a></li>
  <li><a href="//news.zhibo8.com/zuqiu/2026-11-05/aaa111native.htm?from=list">皇马客战瓦伦西亚大名单出炉，姆巴佩领衔锋线</a></li>
  <li><a href="//news.zhibo8.com/zuqiu/2026-11-04/ccc333native.htm">昨天的旧闻：皇马当时还没赢</a></li>
  <li><a href="/zuqiu/2026-11-05/ddd444native.htm">相对路径的短链不算</a></li>
</ul>"""

ARTICLE_HTML = """
<div class="content">
  <p>直播吧11月5日讯 皇马今天公布了客场大名单。</p>
  <img src="//static4style.duoduocdn.com/web/logo_o3x_03.png">
  <img src="//tu.duoduocdn.com/uploads/day_261104/abc_720.jpg">
  <img src="//tu.duoduocdn.com/uploads/day_261104/tiny_720.jpg" width="60">
  <img src="//tu.duoduocdn.com/uploads/day_261104/ad_720.gif">
  <img data-src="//tu.duoduocdn.com/uploads/day_261104/jkl_720.jpg">
</div>"""


class TestParseIndex(unittest.TestCase):
    def test_dedupes_by_article_id(self):
        items = ni.parse_index(INDEX_HTML)
        # aaa（同一篇两个链接，去重成 1）+ bbb + ccc = 3；相对路径那条不算
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["date"], "2026-11-05")

    def test_urls_are_absolute(self):
        self.assertTrue(all(i["url"].startswith("https://news.zhibo8.com/")
                            for i in ni.parse_index(INDEX_HTML)))

    def test_empty_html(self):
        self.assertEqual(ni.parse_index(""), [])
        self.assertEqual(ni.parse_index("<div>没有链接</div>"), [])


class TestPickArticles(unittest.TestCase):
    def setUp(self):
        self.items = ni.parse_index(INDEX_HTML)

    def test_only_entity_hits(self):
        got = ni.pick_articles(self.items, ["皇马"])
        self.assertEqual(len(got), 2)
        self.assertTrue(all("皇马" in i["title"] for i in got))

    def test_alias_hits_standard_name(self):
        """正文实体写「皇家马德里」、标题写「皇马」也要能命中（别名反查）。"""
        got = ni.pick_articles(self.items, ["皇家马德里"])
        self.assertTrue(got, "别名反查失效 → 明明有报道一条都挑不中")
        self.assertIn("皇马", got[0]["title"])

    def test_no_entity_no_pick(self):
        self.assertEqual(ni.pick_articles(self.items, []), [])
        self.assertEqual(ni.pick_articles(None, ["皇马"]), [])

    def test_unrelated_entity_no_pick(self):
        self.assertEqual(ni.pick_articles(self.items, ["拜仁"]), [])

    def test_limit(self):
        self.assertLessEqual(len(ni.pick_articles(self.items, ["皇马"], limit=1)), 1)


class TestParseArticleImages(unittest.TestCase):
    def test_filters_logo_gif_and_tiny(self):
        urls = ni.parse_article_images(ARTICLE_HTML, limit=5)
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("tu.duoduocdn.com" in u for u in urls))
        self.assertFalse(any("static" in u or u.endswith(".gif") for u in urls))

    def test_upgrades_size_for_gate(self):
        urls = ni.parse_article_images(ARTICLE_HTML, limit=5)
        self.assertTrue(all(u.endswith("_1280.jpg") for u in urls),
                        f"720 宽过不了下载闸门，必须升级：{urls}")

    def test_reads_data_src(self):
        self.assertTrue(any("jkl" in u for u in ni.parse_article_images(ARTICLE_HTML)))

    def test_no_container_returns_empty(self):
        """没有正文容器时不退回整个 body —— 否则列表页几十张小图全混进来。"""
        self.assertEqual(ni.parse_article_images(
            "<body><div class='list'><img src='//tu.duoduocdn.com/x_720.jpg'></div></body>"), [])

    def test_limit(self):
        self.assertEqual(len(ni.parse_article_images(ARTICLE_HTML, limit=1)), 1)


class TestUrlRewrite(unittest.TestCase):
    def test_upgrade_known_host(self):
        self.assertEqual(ni.upgrade_url("https://tu.duoduocdn.com/a_720.jpg"),
                         "https://tu.duoduocdn.com/a_1280.jpg")

    def test_upgrade_skips_big_and_unknown(self):
        self.assertEqual(ni.upgrade_url("https://tu.duoduocdn.com/a_1280.jpg"),
                         "https://tu.duoduocdn.com/a_1280.jpg")
        self.assertEqual(ni.upgrade_url("https://x.example/a_720.jpg"),
                         "https://x.example/a_720.jpg")

    def test_original_url_is_720_not_bare(self):
        """回退必须给 _720，不是「去掉后缀」——去后缀是 3MB 原图，会撞 5MB 上限。"""
        self.assertEqual(ni.original_url("https://tu.duoduocdn.com/a_1280.jpg"),
                         "https://tu.duoduocdn.com/a_720.jpg")


class TestFetch(unittest.TestCase):
    def test_no_entity_never_touches_network(self):
        with mock.patch.object(ni, "_get") as get:
            self.assertEqual(ni.fetch([], need=3), [])
        get.assert_not_called()

    def test_full_flow_with_injected_index(self):
        items = ni.parse_index(INDEX_HTML)
        with mock.patch.object(ni, "_get", return_value=mock.Mock(text=ARTICLE_HTML)):
            got = ni.fetch(["皇马"], need=2, session_index=items)
        self.assertEqual(len(got), 2)
        self.assertTrue(all(g["source"] == "news_body" for g in got))
        self.assertTrue(all(g.get("fallback", "").endswith("_720.jpg") for g in got))
        self.assertTrue(all(g["article_url"].startswith("https://") for g in got))

    def test_network_failure_returns_empty_not_raise(self):
        items = ni.parse_index(INDEX_HTML)
        with mock.patch.object(ni, "_get", return_value=None):
            self.assertEqual(ni.fetch(["皇马"], need=3, session_index=items), [])

    def test_parse_failure_returns_empty(self):
        items = ni.parse_index(INDEX_HTML)
        broken = mock.Mock(text="<html><body>维护中</body></html>")
        with mock.patch.object(ni, "_get", return_value=broken):
            self.assertEqual(ni.fetch(["皇马"], need=3, session_index=items), [])

    def test_selftest(self):
        bad = [n for n, ok in ni.selftest() if not ok]
        self.assertEqual(bad, [])


class TestWiring(unittest.TestCase):
    """只定义不调用 = 没接上（这项目的老毛病）。"""

    def test_search_images_calls_news_layer(self):
        import ast
        src = (ROOT / "data_collector.py").read_text(encoding="utf-8")
        fn = next((n for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.FunctionDef) and n.name == "search_images"), None)
        self.assertIsNotNone(fn)
        mods = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Import):
                mods.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module)
        self.assertIn("news_images", mods, "search_images 没有接入新闻图爬取层")


if __name__ == "__main__":
    unittest.main()
