"""采集结果分类台账（scraper_diagnostics）与 media_scraper 失败路径的回归测试。

核心断言只有一条：**「被拦」不许再伪装成「没数据」**。
2026-10-26 懂球帝走 EdgeOne 返 HTTP 567 挑战页，旧实现不在 403/429/404 分支里，
重试耗尽后 `return ""` → 备源静默归零，日志只报「懂球帝文章: 0 篇」。
"""

import io
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import media_scraper as ms
import scraper_diagnostics as diag


class TestClassify(unittest.TestCase):
    def test_status_mapping(self):
        cases = {
            200: diag.KIND_OK,
            404: diag.KIND_EMPTY,      # 页面真没了 ≠ 被拦
            403: diag.KIND_BLOCKED,
            401: diag.KIND_BLOCKED,
            429: diag.KIND_CHALLENGE,  # 限流与挑战页处置相同，成因要能看见
            567: diag.KIND_CHALLENGE,  # EdgeOne
            521: diag.KIND_CHALLENGE,
            500: diag.KIND_HTTP_ERROR,
            502: diag.KIND_HTTP_ERROR,
        }
        for code, want in cases.items():
            self.assertEqual(diag.classify_status(code), want, f"status={code}")

    def test_challenge_needs_thin_page(self):
        thin = "<html><title>EdgeOne 安全验证</title>Just a moment</html>"
        self.assertTrue(diag.looks_like_challenge(thin))
        # 长文里出现「captcha」是正常文章，不能判成挑战页
        self.assertFalse(diag.looks_like_challenge("captcha " * 20000))
        self.assertFalse(diag.looks_like_challenge("<html>皇马 2-1 巴萨</html>"))
        self.assertFalse(diag.looks_like_challenge(""))
        self.assertFalse(diag.looks_like_challenge(None))

    def test_video_player_is_not_a_challenge(self):
        """直播吧文章页引用 `video-player2.js`，而 `video-` 里含子串 `eo-`。

        2026-11-04 事故现场：`"eo-"` 在特征串里时，每篇正常比赛稿（20~30KB，
        低于薄壳阈值）都被判成挑战页 → 抛 ScraperChallengeError → 文章被丢。
        当天台账 19 次 challenge 里 **17 次是误报**，进而得出「25% 采集失败」
        这个假结论，还差点据此去引 TLS 指纹库。判据里的短串比漏报更危险：
        漏报是「没抓到」，误报是「抓到了却当成没抓到」。
        """
        page = ('<html><head><title>让二追三！曼城3-2绝杀，库里尼亚3分钟2球</title>'
                '<script src="//static4style.duoduocdn.com/news/js/video-player2.js">'
                '</script></head><body><p>正文……</p></body></html>')
        self.assertFalse(diag.looks_like_challenge(page))

    def test_real_edgeone_challenge_still_caught(self):
        """修误报不能引进漏报：真实 EdgeOne 挑战页必须仍然命中。

        样本取自 2026-11-04 懂球帝 567 页（7,351 字节）的真实文案。
        """
        page = ('<!doctype html><html><body><p>请求 ID: </p>'
                '<p>若您是网站拥有者，请参阅 EdgeOne Web 安全分析官方文档，'
                '了解如何筛选请求 ID 查看样本日志与命中规则。</p></body></html>')
        self.assertTrue(diag.looks_like_challenge(page))

    def test_exception_mapping(self):
        self.assertEqual(diag.classify_exception(ms.ScraperChallengeError("x")),
                         diag.KIND_CHALLENGE)
        self.assertEqual(diag.classify_exception(ms.ScraperBlockedError("x")),
                         diag.KIND_BLOCKED)
        self.assertEqual(diag.classify_exception(TimeoutError("x")), diag.KIND_NETWORK)
        self.assertEqual(diag.classify_exception(ms.ScraperParseError("x")), diag.KIND_PARSE)


def _resp(code, text="", headers=None):
    return types.SimpleNamespace(status_code=code, text=text,
                                 headers=headers or {}, encoding="utf-8",
                                 apparent_encoding="utf-8")


class TestHttpGetIsNotSilent(unittest.TestCase):
    """硬项：非 200/404 必须抛，不许 return ""。"""

    def setUp(self):
        diag.reset()
        self.s = ms.SportsScraper()
        self.s.MAX_RETRIES = 1          # 免重试等待，测试要快
        self.s.REQUEST_DELAY = 0

    def tearDown(self):
        diag.reset()

    def _mock(self, resp):
        self.s.session = mock.Mock()
        self.s.session.get.return_value = resp

    def test_567_raises_challenge_and_records(self):
        self._mock(_resp(567, "EdgeOne challenge"))
        with self.assertRaises(ms.ScraperChallengeError):
            self.s._http_get("https://www.dongqiudi.com/")
        kinds = [r["kind"] for r in diag.records()]
        self.assertIn(diag.KIND_CHALLENGE, kinds)
        self.assertNotEqual(self.s._http_get.__doc__, None)

    def test_500_raises_http_error(self):
        self._mock(_resp(500, "boom"))
        with self.assertRaises(Exception) as ctx:
            self.s._http_get("https://www.zhibo8.cc/")
        self.assertNotIsInstance(ctx.exception, SystemExit)
        self.assertIn(diag.KIND_HTTP_ERROR, [r["kind"] for r in diag.records()])

    def test_404_returns_empty_and_is_marked_empty_not_blocked(self):
        self._mock(_resp(404))
        self.assertEqual(self.s._http_get("https://www.zhibo8.cc/gone.htm"), "")
        rec = diag.records()[-1]
        self.assertEqual(rec["kind"], diag.KIND_EMPTY)
        self.assertEqual(rec["status"], 404)

    def test_200_challenge_shell_is_blocked(self):
        self._mock(_resp(200, "<html>EdgeOne 人机验证</html>"))
        with self.assertRaises(ms.ScraperChallengeError):
            self.s._http_get("https://www.dongqiudi.com/")

    def test_200_ok_records_ok(self):
        self._mock(_resp(200, "<html>" + "正文" * 500 + "</html>"))
        html = self.s._http_get("https://www.zhibo8.cc/")
        self.assertIn("正文", html)
        self.assertEqual(diag.records()[-1]["kind"], diag.KIND_OK)


class TestHttpGetPlainIsNotSilent(unittest.TestCase):
    """懂球帝专用的最小头通道：HTTPError 必须按 code 归类。"""

    def setUp(self):
        diag.reset()
        self.s = ms.SportsScraper()
        self.s.REQUEST_DELAY = 0

    def tearDown(self):
        diag.reset()

    def test_plain_567_raises_challenge(self):
        err = urllib.error.HTTPError("https://www.dongqiudi.com/", 567, "challenge", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ms.ScraperChallengeError):
                self.s._http_get_plain("https://www.dongqiudi.com/")
        self.assertEqual(diag.records()[-1]["kind"], diag.KIND_CHALLENGE)
        self.assertEqual(diag.records()[-1]["status"], 567)

    def test_plain_403_raises_blocked(self):
        err = urllib.error.HTTPError("https://example.com/a", 403, "forbidden", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ms.ScraperBlockedError):
                self.s._http_get_plain("https://example.com/a")
        self.assertEqual(diag.records()[-1]["kind"], diag.KIND_BLOCKED)

    def test_plain_404_returns_empty(self):
        err = urllib.error.HTTPError("https://example.com/b", 404, "gone", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertEqual(self.s._http_get_plain("https://example.com/a"), "")
        self.assertEqual(diag.records()[-1]["kind"], diag.KIND_EMPTY)

    def test_plain_shell_200_is_challenge(self):
        body = io.BytesIO("<html>cf-chl Just a moment</html>".encode("utf-8"))

        class _R:
            headers = {"Content-Type": "text/html; charset=utf-8"}

            def read(self_inner):
                return body.read()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_R()):
            with self.assertRaises(ms.ScraperChallengeError):
                self.s._http_get_plain("https://www.dongqiudi.com/")


class TestLedger(unittest.TestCase):
    def setUp(self):
        diag.reset()
        self.tmp = Path(tempfile.mkdtemp()) / "sub" / "scraper_fetch_latest.json"
        self._orig = diag.LEDGER_PATH
        diag.LEDGER_PATH = self.tmp

    def tearDown(self):
        diag.LEDGER_PATH = self._orig
        diag.reset()

    def test_summary_flags_attention_sources(self):
        diag.record("dongqiudi", "u1", diag.KIND_CHALLENGE, status=567)
        diag.record("zhibo8", "u2", diag.KIND_OK, status=200, count=10)
        diag.record("zhibo8", "u3", diag.KIND_EMPTY, status=404)
        s = diag.summary()
        self.assertFalse(s["ok"])
        self.assertEqual(s["attention_sources"], {"dongqiudi": diag.KIND_CHALLENGE})
        self.assertEqual(s["by_kind"][diag.KIND_OK], 1)

    def test_empty_alone_is_not_failure(self):
        diag.record("zhibo8", "u", diag.KIND_EMPTY, status=404)
        s = diag.summary()
        self.assertTrue(s["ok"], "真空数据不该被判成失败")
        self.assertEqual(s["attention_sources"], {})

    def test_flush_writes_json_and_md(self):
        diag.record("dongqiudi", "u", diag.KIND_CHALLENGE, status=567, detail="HTTP 567")
        p = diag.flush()
        self.assertIsNotNone(p)
        self.assertTrue(p.exists())
        import json
        rep = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(rep["records"][0]["kind"], diag.KIND_CHALLENGE)
        self.assertTrue(p.with_suffix(".md").exists())

    def test_flush_failure_does_not_raise(self):
        diag.LEDGER_PATH = Path("/proc/nonexistent-dir/x.json")
        diag.record("x", "u", diag.KIND_OK)
        self.assertIsNone(diag.flush())     # 观测层不许把主流程带崩

    def test_record_dedupes_same_second_duplicate(self):
        diag.record("dongqiudi", "u", diag.KIND_CHALLENGE, status=567, detail="a")
        diag.record("dongqiudi", "u", diag.KIND_CHALLENGE, status=567, detail="b")
        self.assertEqual(len(diag.records()), 1)
        diag.record("dongqiudi", "u", diag.KIND_OK, status=200)
        self.assertEqual(len(diag.records()), 2)

    def test_unknown_kind_falls_back_to_parse(self):
        diag.record("x", "u", "not-a-kind")
        self.assertEqual(diag.records()[-1]["kind"], diag.KIND_PARSE)


class TestCallersRecordEmptiness(unittest.TestCase):
    """抓取方法返回空时，必须能回答「是没内容还是被拦」。"""

    def setUp(self):
        diag.reset()

    def tearDown(self):
        diag.reset()

    def test_dongqiudi_headlines_records_challenge_not_silent_zero(self):
        s = ms.SportsScraper()
        with mock.patch.object(s, "_http_get_plain",
                               side_effect=ms.ScraperChallengeError("HTTP 567")):
            out = s.scrape_dongqiudi_headlines()
        self.assertEqual(out, [])
        kinds = [r["kind"] for r in diag.records()]
        self.assertIn(diag.KIND_CHALLENGE, kinds)

    def test_dongqiudi_headlines_records_empty_when_page_has_no_articles(self):
        s = ms.SportsScraper()
        with mock.patch.object(s, "_http_get_plain", return_value="<html>无链接</html>"):
            out = s.scrape_dongqiudi_headlines()
        self.assertEqual(out, [])
        self.assertEqual(diag.records()[-1]["kind"], diag.KIND_EMPTY)


if __name__ == "__main__":
    unittest.main()
