"""维基共享取图（wiki_media）与接线测试。

这组测试的判据只有一条：**错图与违规图一张都不能进池子**。
覆盖率不是这里的事——配图率涨了而配错图，比没有图更糟（无图有数据卡兜底）。

本机访问 *.wikimedia.org 全部超时，所以联网部分一律走假响应；
真实取图在 CI 才能验，验收脚本按三态处理（不通 = 未验证，不是通过）。
"""

import json
import unittest
from unittest import mock

import wiki_media as wm


META_OK = {
    "LicenseShortName": {"value": "CC BY-SA 4.0"},
    "Artist": {"value": "<a href='https://example.org/u/1'>Jane Doe</a>"},
    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
    "ImageDescription": {"value": "Kylian Mbappe training"},
}


def _payload(pages, *, fv=2):
    if fv == 2:
        return {"query": {"pages": pages}}
    return {"query": {"pages": {str(i): p for i, p in enumerate(pages)}}}


def _page(title="File:Kylian Mbappe 2024.jpg", *, w=1600, h=1200,
          meta=None, cats=("Category:Association football players",)):
    return {
        "title": title,
        "imageinfo": [{"width": w, "height": h,
                       "thumburl": "https://upload.wikimedia.org/x/1200px.jpg",
                       "descriptionurl": "https://commons.wikimedia.org/wiki/" + title,
                       "extmetadata": dict(META_OK if meta is None else meta)}],
        "categories": [{"title": c} for c in cats],
    }


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.content = b"{}"
        self.text = json.dumps(payload)[:200]

    def json(self):
        return self._p


class _Sess:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(params or {})
        return self.resp


class TestLicense(unittest.TestCase):
    def test_allowed(self):
        for lic in ("CC0", "CC0 1.0", "Public domain", "CC BY 4.0",
                    "CC BY-SA 4.0", "CC BY-SA 3.0", "PD-US"):
            self.assertTrue(wm.license_ok(lic), lic)

    def test_rejected(self):
        for lic in ("CC BY-NC 4.0", "CC BY-ND 4.0", "Fair use", "Non-free",
                    "GFDL", "All rights reserved", "", None,
                    "Copyrighted free use"):
            self.assertFalse(wm.license_ok(lic), lic)

    def test_fail_closed_on_unknown(self):
        """不认识的许可必须拒绝——宁可这张不用，也不赌它自由。"""
        self.assertFalse(wm.license_ok("Some custom permission"))


class TestAttribution(unittest.TestCase):
    def test_strips_html(self):
        a = wm.attribution_of(META_OK)
        self.assertEqual(a["artist"], "Jane Doe")
        self.assertEqual(a["license"], "CC BY-SA 4.0")

    def test_no_artist_rejected(self):
        """CC BY 要求署名，没有作者 = 署不了名 = 不能用。"""
        m = {"LicenseShortName": {"value": "CC BY 4.0"}}
        self.assertIsNone(wm.attribution_of(m))

    def test_bad_license_rejected(self):
        m = {"LicenseShortName": {"value": "Fair use"}, "Artist": {"value": "x"}}
        self.assertIsNone(wm.attribution_of(m))

    def test_credit_line(self):
        line = wm.credit_line(wm.attribution_of(META_OK))
        self.assertIn("Jane Doe", line)
        self.assertIn("CC BY-SA 4.0", line)
        self.assertTrue(line.startswith("图片来源"))

    def test_append_credit_idempotent(self):
        line = wm.credit_line(wm.attribution_of(META_OK))
        once = wm.append_credit("正文", [line])
        self.assertTrue(once.startswith("正文"))
        self.assertIn("图片来源", once)
        self.assertEqual(wm.append_credit(once, [line]), once)


class TestSearch(unittest.TestCase):
    def test_parses_formatversion2(self):
        got = wm.search("Kylian Mbappe", 1, session=_Sess(_Resp(_payload([_page()]))))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["source"], "wikimedia")
        self.assertTrue(got[0]["attribution"]["artist"])

    def test_parses_formatversion1(self):
        got = wm.search("Kylian Mbappe", 1,
                        session=_Sess(_Resp(_payload([_page()], fv=1))))
        self.assertEqual(len(got), 1)

    def test_size_gate(self):
        small = _page(w=400, h=300)
        got = wm.search("Kylian Mbappe", 1, session=_Sess(_Resp(_payload([small]))))
        self.assertEqual(got, [])

    def test_license_gate_in_search(self):
        bad = _page(meta={"LicenseShortName": {"value": "Fair use"},
                          "Artist": {"value": "x"}})
        got = wm.search("Kylian Mbappe", 1, session=_Sess(_Resp(_payload([bad]))))
        self.assertEqual(got, [])

    def test_irrelevant_rejected(self):
        """检索词在文件名/描述/分类里都没出现 → 无法确认相关，丢弃。"""
        meta = {"LicenseShortName": {"value": "CC BY-SA 4.0"},
                "Artist": {"value": "Jane Doe"},
                "ImageDescription": {"value": "A quiet beach at sunset"}}
        other = _page(title="File:Random beach photo.jpg", meta=meta,
                      cats=("Category:Beaches",))
        got = wm.search("Kylian Mbappe", 1, session=_Sess(_Resp(_payload([other]))))
        self.assertEqual(got, [])

    def test_player_requires_football_category(self):
        """同名误配的主防线：球员类必须落在 football 分类下。"""
        bust = _page(title="File:Socrates bust.jpg", cats=("Category:Philosophers",))
        self.assertEqual(
            wm.search("Socrates", 1, require_sport_category=True,
                      session=_Sess(_Resp(_payload([bust])))), [])
        # 同一条图，不要求运动分类时（球队/球场检索）可以放行
        self.assertEqual(
            len(wm.search("Socrates", 1, session=_Sess(_Resp(_payload([bust]))))), 1)

    def test_cjk_query_rejected(self):
        sess = _Sess(_Resp(_payload([_page()])))
        self.assertEqual(wm.search("皇家马德里", 1, session=sess), [])
        self.assertEqual(sess.calls, [], "中文 query 不应发出请求")

    def test_http_error_recorded_not_raised(self):
        got = wm.search("Kylian Mbappe", 1, session=_Sess(_Resp({}, status=429)))
        self.assertEqual(got, [])

    def test_network_flag_distinguishes_empty(self):
        """「连不上」和「真没图」必须分开：前者要熔断，后者不能。"""
        class _Boom:
            def get(self, *a, **k):
                raise TimeoutError("timed out")
        self.assertEqual(wm.search("Kylian Mbappe", 1, session=_Boom()), [])
        self.assertTrue(wm.LAST["network"])

        wm.search("Kylian Mbappe", 1, session=_Sess(_Resp(_payload([]))))
        self.assertFalse(wm.LAST["network"])


class TestResolve(unittest.TestCase):
    def test_zh_to_en(self):
        en, cat = wm.resolve("皇家马德里")
        self.assertTrue(en.lower().startswith("real madrid"))
        self.assertEqual(cat, "club")

    def test_player_category(self):
        en, cat = wm.resolve("哈兰德")
        self.assertIn("haaland", en.lower())
        self.assertEqual(cat, "player")

    def test_unknown_cjk_dropped(self):
        self.assertEqual(wm.resolve("某个不存在的球员"), ("", ""))

    def test_ascii_passthrough(self):
        self.assertEqual(wm.resolve("Real Madrid"), ("Real Madrid", ""))


class TestWiring(unittest.TestCase):
    """接线层：data_collector 是否真的用了这一层、以及熔断是否生效。"""

    def setUp(self):
        import data_collector as dc
        self.dc = dc
        dc.reset_wiki_breaker()
        # 上游两层打空，专测 Commons 这一层
        self.p1 = mock.patch.object(dc, "search_wikipedia", return_value=[])
        self.p2 = mock.patch.object(dc, "search_wikipedia_entity", return_value=None)
        self.p3 = mock.patch.object(dc, "_gallery_pexels", return_value=[])
        self.p1.start(); self.p2.start(); self.p3.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop(); self.p3.stop()
        self.dc.reset_wiki_breaker()

    def _topic(self):
        return {"title": "皇马3-1巴萨，第70分钟换人后连丢2球",
                "keywords": ["football"], "keywords_cn": ["皇马", "巴萨"]}

    def test_commons_result_used(self):
        img = {"url": "https://upload.wikimedia.org/a.jpg", "source": "wikimedia",
               "alt": "Santiago Bernabeu", "query": "Real Madrid CF",
               "attribution": {"artist": "Jane Doe", "license": "CC BY-SA 4.0"}}
        with mock.patch.object(self.dc._wm, "search_for_entity", return_value=[img]):
            out = self.dc.search_images(self._topic(), count=3)
        self.assertTrue(any(i.get("source") == "wikimedia" for i in out), out)

    def test_breaker_trips_on_network_failure(self):
        """连不上 → 熔断，后续实体不再白等（本机真实情况就是这样）。"""
        calls = []

        def fake(ent, need, **kw):
            calls.append(ent)
            self.dc._wm.LAST.update({"network": True, "count": 0})
            return []

        with mock.patch.object(self.dc._wm, "search_for_entity", side_effect=fake):
            self.dc.search_images(self._topic(), count=3)
            first = len(calls)
            self.dc.search_images(self._topic(), count=3)
        self.assertGreaterEqual(first, 1)
        self.assertEqual(len(calls), first, "熔断后不应再请求")

    def test_empty_result_does_not_trip(self):
        """取空（真没图）不能熔断，否则一个冷门实体就把整批图源关掉。"""
        with mock.patch.object(self.dc._wm, "search_for_entity", return_value=[]):
            with mock.patch.object(self.dc._wm, "LAST", {"network": False, "count": 0}):
                self.dc.search_images(self._topic(), count=3)
                self.assertTrue(self.dc._wiki_available())

    def test_wikimedia_is_bound(self):
        import cover_binding as cb
        img = {"url": "https://upload.wikimedia.org/a.jpg", "source": "wikimedia",
               "alt": "Santiago Bernabeu", "query": ""}
        self.assertTrue(cb.image_is_bound(img, ["皇马"]))


class TestDeadSourcesRemoved(unittest.TestCase):
    def test_pixabay_not_called(self):
        """Pixabay 元数据只有 tags，accept 校验必挂 —— 停用后不该再发请求。"""
        import inspect
        src = inspect.getsource(__import__("data_collector").search_images)
        self.assertNotIn("_gallery_pixabay(gal", src)

    def test_duckduckgo_removed(self):
        import inspect
        src = inspect.getsource(__import__("data_collector").search_images)
        self.assertNotIn("duckduckgo.com/i.js", src)


if __name__ == "__main__":
    unittest.main()
