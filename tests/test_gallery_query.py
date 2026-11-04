"""图库英文 query 白名单（gallery_query）与接线守卫的单测。

背景（2026-11-04 拿到 Pexels Key 后第一次真跑）：
`search_images` 把 `build_bound_query()` 的**中文实体**直接丢给 Pixabay / Pexels /
Unsplash —— 三家都是英文标签索引。实测 query="皇家马德里" 第一条是酱油饺子，
query="欧冠" 是欧元纸币，query="Haaland" 是奥地利的一个村庄。

所以这里测的不是"能不能搜到图"，而是**会不会配错图**：
  1. 中文实体绝不进英文图库（白名单机制在，就得证明它在拦）；
  2. 球员名不查图库（图库没有球员肖像版权图，返回的一定是陌生人）；
  3. 返回图必须 alt 命中该队主场名，否则丢弃 —— 含重音折叠（Bernabéu）；
  4. 接线：`_gallery_*` 调用必须带 `accept`，且不再吃 `build_bound_query` 的中文结果。

负向断言是重点：只测「正常时通过」等于没测。
"""

import sys
import unittest
from unittest import mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import gallery_query as gq  # noqa: E402
import verify_gallery_entities as vge  # noqa: E402


class _FakeResp:
    def __init__(self, payload, status=200, text=""):
        self._p = payload
        self.status_code = status
        self.text = text or ""

    def json(self):
        return self._p


def _pexels_payload(*alts):
    return {"photos": [
        {"width": 2000, "height": 1200, "alt": a,
         "src": {"large2x": f"https://img.example/{i}.jpg"}}
        for i, a in enumerate(alts)]}


class TestWhitelist(unittest.TestCase):
    def test_whitelist_not_empty(self):
        self.assertGreaterEqual(len(gq.whitelist()), 5)

    def test_all_queries_english(self):
        """白名单 query 里出现一个中文字符，就会配出一张无关图。"""
        for name, item in gq.whitelist().items():
            self.assertFalse(gq._has_cjk(item["query"]), f"{name} 的 query 含中文：{item['query']}")

    def test_all_accept_non_empty(self):
        """accept 为空 = 无法校验 = 放行任意图，等于没有闸门。"""
        for name, item in gq.whitelist().items():
            self.assertTrue(item["accept"], f"{name} 没有 accept")

    def test_every_key_is_a_real_club(self):
        """登记名必须是 entity_map 里的 club，否则永远解析不到（静默失效）。"""
        alias = gq._alias_map()
        for name in gq.whitelist():
            self.assertEqual(alias.get(name) or alias.get(name.lower()), name,
                             f"{name} 不在 entity_map 的 club 名单里")

    def test_declared_verified_file(self):
        """配置里要有 verified 字段，且 ≥2（1 张等于没有冗余）。"""
        rep = vge.check_declared_yield()
        self.assertTrue(rep["ok"], rep["detail"])


class TestResolve(unittest.TestCase):
    def test_alias_to_canonical(self):
        got = gq.resolve(["皇家马德里"])
        self.assertIsNotNone(got)
        self.assertEqual(got["entity"], "皇马")
        self.assertEqual(got["query"], "Real Madrid CF")

    def test_player_never_queried(self):
        """图库没有球员肖像的版权图：20 张里 alt 命中该名字 0~2 张。"""
        self.assertIsNone(gq.resolve(["姆巴佩"]))
        self.assertIsNone(gq.resolve(["贝林厄姆", "哈兰德"]))

    def test_unlisted_club_not_queried(self):
        """尤文/热刺/马竞/纽卡 实测 0 张，不登记 → 不查（宁可数据卡）。"""
        self.assertIsNone(gq.resolve(["尤文"]))
        self.assertIsNone(gq.resolve(["热刺"]))

    def test_mixed_prefers_club(self):
        got = gq.resolve(["姆巴佩", "皇马"])
        self.assertEqual((got or {}).get("entity"), "皇马")

    def test_empty_inputs(self):
        for bad in (None, [], [""], [None, 1]):
            self.assertIsNone(gq.resolve(bad))

    def test_chinese_only_entities_never_used(self):
        """任何解析结果的 query 都必须能安全发出去（无 CJK）。"""
        for name in gq.whitelist():
            got = gq.resolve([name])
            self.assertFalse(gq._has_cjk(got["query"]))


class TestAltVerification(unittest.TestCase):
    def test_accent_folding(self):
        """Pexels 的 alt 写的是 Bernabéu，用 ascii 的 bernabeu 必须能命中。"""
        self.assertEqual(gq.fold("Santiago Bernabéu"), "santiago bernabeu")
        self.assertTrue(gq.is_accepted("Discover the iconic Santiago Bernabéu Stadium",
                                       ("bernabeu",)))

    def test_reject_other_stadium(self):
        self.assertFalse(gq.is_accepted("Wide shot of Camp Nou stadium", ("bernabeu",)))

    def test_reject_empty_alt(self):
        """空 alt = 无法校验 = 不可信（不是"当作通过"）。"""
        self.assertFalse(gq.is_accepted("", ("bernabeu",)))
        self.assertFalse(gq.is_accepted(None, ("bernabeu",)))

    def test_reject_empty_accept(self):
        self.assertFalse(gq.is_accepted("Bernabeu stadium", ()))


class TestGalleryCallHonoursAccept(unittest.TestCase):
    """`_gallery_pexels` 必须按 accept 过滤，且不通过的不占 need。"""

    def setUp(self):
        import data_collector
        self.dc = data_collector
        self._orig = data_collector.requests.get
        self._key = data_collector.PEXELS_KEY

    def tearDown(self):
        self.dc.requests.get = self._orig
        self.dc.PEXELS_KEY = self._key

    def _patch(self, payload, **kw):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append({"url": url, "params": dict(params or {}), "headers": headers})
            return _FakeResp(payload, **kw)

        self.dc.requests.get = fake_get
        return calls

    def test_filters_by_alt(self):
        self.dc.PEXELS_KEY = "k"
        calls = self._patch(_pexels_payload(
            "Wide shot of Camp Nou stadium",          # 巴萨的球场 → 必须拒
            "Empty stands of Santiago Bernabéu",      # 皇马 → 通过
            "Aerial view of Bernabeu renovation",     # 皇马 → 通过
        ))
        got = self.dc._gallery_pexels("Real Madrid CF", 5, accept=("bernabeu",))
        self.assertEqual(len(got), 2)
        self.assertTrue(all(i["source"] == "pexels" for i in got))
        self.assertEqual(calls[0]["params"]["query"], "Real Madrid CF")

    def test_all_rejected_returns_empty(self):
        self.dc.PEXELS_KEY = "k"
        self._patch(_pexels_payload("Pet cat on a sofa", "Mountain lake"))
        self.assertEqual(self.dc._gallery_pexels("Real Madrid CF", 5, accept=("bernabeu",)), [])

    def test_no_key_records_diagnostic(self):
        self.dc.PEXELS_KEY = ""
        self.assertEqual(self.dc._gallery_pexels("Real Madrid CF", 5, accept=("bernabeu",)), [])

    def test_low_res_rejected(self):
        self.dc.PEXELS_KEY = "k"
        self._patch({"photos": [{"width": 640, "height": 480, "alt": "Santiago Bernabeu",
                                 "src": {"large2x": "https://img.example/x.jpg"}}]})
        self.assertEqual(self.dc._gallery_pexels("Real Madrid CF", 5, accept=("bernabeu",)), [])


class TestSearchImagesNeverSendsChinese(unittest.TestCase):
    """端到端：中文标题进 `search_images`，发给图库的 query 必须是英文或干脆不查。"""

    def setUp(self):
        import data_collector
        self.dc = data_collector
        self._orig_get = data_collector.requests.get
        self._orig_key = data_collector.PEXELS_KEY
        self._orig_pb = data_collector.PIXABAY_KEY
        self._orig_wiki = (data_collector.search_wikipedia,
                           data_collector.search_wikipedia_entity)
        data_collector.search_wikipedia = lambda *a, **k: []
        data_collector.search_wikipedia_entity = lambda *a, **k: []
        data_collector.PEXELS_KEY = "k"
        data_collector.PIXABAY_KEY = ""
        self.queries = []

    def tearDown(self):
        self.dc.requests.get = self._orig_get
        self.dc.PEXELS_KEY = self._orig_key
        self.dc.PIXABAY_KEY = self._orig_pb
        (self.dc.search_wikipedia, self.dc.search_wikipedia_entity) = self._orig_wiki

    def _patch(self, payload):
        def fake_get(url, params=None, headers=None, timeout=None):
            if "pexels" in url or "pixabay" in url:
                self.queries.append((params or {}).get("query", ""))
            return _FakeResp(payload)
        self.dc.requests.get = fake_get

    def test_chinese_title_gets_english_query(self):
        self._patch(_pexels_payload("Discover the iconic Santiago Bernabéu Stadium"))
        imgs = self.dc.search_images({"title": "皇家马德里主场要大修", "keywords": []}, count=3)
        self.assertTrue(self.queries, "应该查了一次图库")
        for q in self.queries:
            self.assertFalse(gq._has_cjk(q), f"中文 query 被发给了英文图库：{q}")
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0]["source"], "pexels")

    def test_unlisted_entity_does_not_call_gallery_at_all(self):
        """尤文实测 0 张 → 一次请求都不该发（发了只会配错图）。"""
        self._patch(_pexels_payload("Pet cat on a sofa"))
        self.dc.search_images({"title": "尤文换帅风波", "keywords": []}, count=3)
        self.assertEqual(self.queries, [])


class TestDeadLayersRemoved(unittest.TestCase):
    """DDG / Pixabay 两层已删除（2026-11-04），这里守的是「不许悄悄贴回来」。

    原来的 `TestDuckDuckGoEntityGate` 测的是「中文 query 必须靠标题命中实体兜住」——
    但那一层整个删掉了（本机代理超时、CI 拿不到 vqd、返回任意中文站点的图、
    零许可校验），测试也就失去了被测对象：那是一条僵尸测试。
    删除的理由必须可回归，所以改成守删除本身。
    """

    def test_no_request_to_dead_sources(self):
        import data_collector as dc
        calls = []
        orig = dc.requests.get
        dc.reset_wiki_breaker()
        try:
            def fake_get(url, params=None, headers=None, timeout=None):
                calls.append(url)
                return _FakeResp({})
            dc.requests.get = fake_get
            with mock.patch.object(dc, "search_wikipedia", return_value=[]), \
                 mock.patch.object(dc, "search_wikipedia_entity", return_value=None), \
                 mock.patch.object(dc, "_gallery_pexels", return_value=[]), \
                 mock.patch.object(dc._wm, "search_for_entity", return_value=[]):
                dc.search_images({"title": "皇马更衣室矛盾升级", "keywords": []}, count=3)
        finally:
            dc.requests.get = orig
            dc.reset_wiki_breaker()
        self.assertEqual([c for c in calls if "duckduckgo" in c], [])
        self.assertEqual([c for c in calls if "pixabay" in c], [])

    def test_call_sites_gone(self):
        import inspect
        src = inspect.getsource(__import__("data_collector").search_images)
        self.assertNotIn("duckduckgo.com/i.js", src)
        self.assertNotIn("_gallery_pixabay(gal", src)


class TestInvalidConfigIsLoud(unittest.TestCase):
    """配置写错必须报红，不能静默丢弃。

    这一条针对的是本项目反复踩的坑：配置/字段对不上 → 功能静默失效 → 几个月没人发现。
    所以 `gallery_query` 把非法条目单列，`verify_gallery_entities.check_config` 硬性报红。
    """

    def setUp(self):
        import tempfile
        self._orig = gq.WHITELIST
        self._d = tempfile.TemporaryDirectory()
        self.tmp = Path(self._d.name) / "g.yaml"

    def tearDown(self):
        gq.WHITELIST = self._orig
        gq.reload()
        self._d.cleanup()

    def _write(self, body):
        self.tmp.write_text(body, encoding="utf-8")
        gq.WHITELIST = self.tmp
        gq.reload()

    def test_blank_accept_is_reported(self):
        self._write("clubs:\n  皇马:\n    query: Real Madrid CF\n    accept: []\n")
        self.assertEqual(gq.whitelist(), {})
        self.assertTrue(gq.invalid(), "空 accept 必须进 invalid，不能静默丢弃")
        rep = vge.check_config()
        self.assertFalse(rep["ok"])
        self.assertTrue(any("accept" in d for d in rep["detail"]))

    def test_chinese_query_is_reported(self):
        self._write("clubs:\n  皇马:\n    query: 皇家马德里\n    accept: [bernabeu]\n")
        rep = vge.check_config()
        self.assertFalse(rep["ok"])
        self.assertTrue(any("中文" in d for d in rep["detail"]))

    def test_missing_file_is_reported(self):
        gq.WHITELIST = Path(self._d.name) / "not-exists.yaml"
        gq.reload()
        rep = vge.check_config()
        self.assertFalse(rep["ok"])

    def test_valid_entry_passes(self):
        self._write("clubs:\n  皇马:\n    query: Real Madrid CF\n    accept: [bernabeu]\n")
        self.assertEqual(gq.invalid(), [])
        rep = vge.check_config()
        # 只验「能解析」；alias 校验在真实配置上另有用例
        self.assertIn("皇马", gq.whitelist())


class TestWiring(unittest.TestCase):
    def test_all_gallery_calls_carry_accept(self):
        rep = vge.check_wiring()
        self.assertTrue(rep["ok"], rep["detail"])

    def test_fold_and_negative(self):
        rep = vge.check_fold()
        self.assertTrue(rep["ok"], rep["detail"])

    def test_verify_hard_checks_pass_without_network(self):
        rep = vge.verify()
        self.assertTrue(rep["ok"], rep.get("hard_failed"))
