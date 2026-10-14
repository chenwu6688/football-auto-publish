"""头条后台导出自动回收（scripts/collect_toutiao_exports.py）单测。

本地无头条登录态，全部路径用 fake playwright 覆盖：
  无登录态 / 登录过期（且不继续第二个页面）/ 导出成功 / 导出按钮缺失（一个页面失败
  不阻断另一个）/ 同名同内容判重 / 同名不同内容另存 / dry-run 只探活 / 失败留诊断。

纪律对齐：collector 是「自动投递员」，失败只告警不静默、同名绝不覆盖、
降级通道（手动 inbox）永远保留。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import collect_toutiao_exports as cte   # noqa: E402


class FakeDownload:
    def __init__(self, filename: str, content: bytes):
        self.suggested_filename = filename
        self._content = content

    def save_as(self, dest):
        Path(dest).write_bytes(self._content)


class _FakeDownloadCtx:
    def __init__(self, download):
        self._download = download
        self.value = None

    def __enter__(self):
        if self._download is None:
            raise TimeoutError("Timeout exceeded while waiting for download")
        self.value = self._download
        return self

    def __exit__(self, *exc):
        return False


class _FakeLocator:
    def __init__(self, page, text):
        self._page = page
        self._text = text

    def count(self):
        return 1 if self._text in self._page.button_texts else 0

    @property
    def first(self):
        return self

    def click(self):
        self._page.clicked.append(self._text)


class _FakeElement:
    def __init__(self, text):
        self._text = text

    def inner_text(self):
        return self._text


class FakePage:
    """按脚本的调用顺序模拟 mp.toutiao.com 数据页。

    login_redirect_after_ms：模拟 SPA 延迟跳转——goto 时 URL 不变（应用壳先加载），
    累计等待超过阈值后才跳登录页。用于回归 dry-run 竞态误报
    （2026-10-14 实测：没等待的检测把延迟跳转漏检，误报「登录态有效」）。
    """

    def __init__(self, *, url_map=None, button_texts=(), downloads=None,
                 goto_error=None, login_redirect_after_ms=None):
        self._url_map = dict(url_map or {})
        self.button_texts = list(button_texts)
        self._downloads = list(downloads or [])
        self.goto_error = goto_error
        self.url = "https://mp.toutiao.com/"
        self.clicked = []
        self.goto_calls = []
        self.screenshots = []
        self._redirect_after = login_redirect_after_ms
        self._waited_ms = 0

    def _maybe_redirect(self):
        """累计等待达标后才执行 url_map 里的跳转（SPA 异步校验的真实时序）。"""
        if (self._redirect_after is not None and self._waited_ms >= self._redirect_after
                and not any(m in self.url for m in ("/auth/", "/login"))):
            for key, val in self._url_map.items():
                if key in self.url:
                    self.url = val
                    break

    def goto(self, url, **kw):
        if self.goto_error:
            raise self.goto_error
        self.goto_calls.append(url)
        if self._redirect_after is None:
            final = url
            for key, val in self._url_map.items():
                if key in url:
                    final = val
                    break
            self.url = final
        else:
            self.url = url      # 延迟模式：初始不跳，等 wait_for_timeout 累计达标
            self._maybe_redirect()

    def wait_for_timeout(self, ms):
        self._waited_ms += ms or 0
        self._maybe_redirect()

    def get_by_text(self, text, exact=False):
        return _FakeLocator(self, text)

    def query_selector_all(self, selector):
        return [_FakeElement(t) for t in self.button_texts]

    def expect_download(self, timeout=None):
        download = self._downloads.pop(0) if self._downloads else None
        return _FakeDownloadCtx(download)

    def screenshot(self, path=None, full_page=False):
        self.screenshots.append(path)

    def on(self, event, handler):
        pass


class FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


class FakeBrowser:
    def __init__(self, page):
        self._ctx = FakeContext(page)
        self.closed = False

    def new_context(self, **kw):
        self.kwargs = kw
        return self._ctx

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    def launch(self, headless=True, args=None):
        self.launch_args = args
        return self._browser


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = FakeChromium(browser)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _factory(page):
    return lambda: FakePlaywright(FakeBrowser(page))


class CollectorTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.inbox = self.td / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.auth = self.td / "toutiao_auth.json"
        self.auth.write_text("{}", encoding="utf-8")
        self.diag = self.td / "diag"
        self.notifications = []
        # 台账/告警隔离：patch send_wxpusher 与 DIAG_DIR，不碰真实环境
        self._wx = mock.patch.object(cte, "send_wxpusher",
                                     lambda title, content: self.notifications.append(
                                         (title, content)))
        self._wx.start()
        self.addCleanup(self._wx.stop)
        self._diag = mock.patch.object(cte, "DIAG_DIR", self.diag)
        self._diag.start()
        self.addCleanup(self._diag.stop)

    def _run(self, page, **kw):
        opts = dict(auth_file=self.auth, inbox=self.inbox, days=1, headless=True,
                    playwright_factory=_factory(page))
        opts.update(kw)
        return cte.run(**opts)


class TestNoAuth(CollectorTestBase):
    def test_missing_auth_file_aborts_without_browser(self):
        def exploding_factory():
            raise AssertionError("无登录态时不应启动浏览器")

        result = cte.run(auth_file=self.td / "nonexistent.json", inbox=self.inbox,
                         playwright_factory=exploding_factory)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "no_auth")
        self.assertTrue(self.notifications, "缺登录态必须告警（回退手动通道）")
        self.assertEqual(list(self.inbox.iterdir()), [])


class TestLoginExpired(CollectorTestBase):
    def test_login_redirect_aborts_and_skips_remaining_pages(self):
        page = FakePage(
            url_map={"works-single": "https://mp.toutiao.com/auth/page/login/?redirect=x"},
            button_texts=("导出数据",),
            downloads=[FakeDownload("作品数据.xlsx", b"perf")],
        )
        result = self._run(page)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "login_expired")
        self.assertEqual(result["files"], [])
        self.assertEqual(len(page.goto_calls), 1, "登录过期应立即中止，不该继续第二个页面")
        self.assertTrue(any("过期" in f"{t}\n{c}" for t, c in self.notifications))

    def test_dry_run_detects_expired_login(self):
        page = FakePage(url_map={"works-single": "https://mp.toutiao.com/auth/page/login/"})
        result = self._run(page, dry_run=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "login_expired")


class TestExportSuccess(CollectorTestBase):
    def test_both_pages_saved_to_inbox(self):
        page = FakePage(
            url_map={
                "works-single": "https://mp.toutiao.com/profile_v4/analysis/works-single/article",
                "/fans": "https://mp.toutiao.com/profile_v4/analysis/fans",
            },
            button_texts=("导出数据", "近7天"),
            downloads=[
                FakeDownload("作品数据_2026-10-08至2026-10-14.xlsx", b"perf-content"),
                FakeDownload("粉丝分析_2026-10-08至2026-10-14.xlsx", b"growth-content"),
            ],
        )
        result = self._run(page)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([f["action"] for f in result["files"]], ["saved", "saved"])
        names = sorted(p.name for p in self.inbox.iterdir())
        self.assertEqual(len(names), 2)
        self.assertIn("导出数据", page.clicked)
        self.assertNotIn("导出数据\n\n", page.clicked)

    def test_range_text_clicked_when_present(self):
        page = FakePage(
            button_texts=("导出数据", "最近7天"),
            downloads=[FakeDownload("a.xlsx", b"1"), FakeDownload("b.xlsx", b"2")],
        )
        self._run(page, days=7)
        self.assertIn("最近7天", page.clicked)


class TestExportFailure(CollectorTestBase):
    def test_missing_export_button_alerts_and_keeps_other_page(self):
        """一个页面按钮缺失 → 告警 + 诊断留证，但不阻断另一个页面投递。"""
        page = FakePage(
            url_map={
                "works-single": "https://mp.toutiao.com/profile_v4/analysis/works-single/article",
                "/fans": "https://mp.toutiao.com/profile_v4/analysis/fans",
            },
            button_texts=(),          # 两页都没有导出按钮
            downloads=[],
        )
        result = self._run(page)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "export_failed")
        self.assertEqual(len(result["problems"]), 2)
        self.assertTrue(self.notifications)
        self.assertEqual(list(self.inbox.iterdir()), [], "失败不得留下半成品文件")

    def test_one_page_fails_other_still_saved_but_not_ok(self):
        calls = {"n": 0}

        class SelectivePage(FakePage):
            def expect_download(self, timeout=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TimeoutError("Timeout exceeded while waiting for download")
                return _FakeDownloadCtx(FakeDownload("粉丝分析.xlsx", b"growth"))

        page = SelectivePage(button_texts=("导出数据",))
        result = self._run(page)
        self.assertFalse(result["ok"], "有页面失败则整体不 ok")
        saved = [f for f in result["files"] if f["action"] == "saved"]
        self.assertEqual(len(saved), 1, "成功的页面仍要落袋为安")
        self.assertTrue(saved[0]["file"].startswith("粉丝分析"),
                        f"落盘名应带统计日期: {saved[0]['file']}")
        self.assertTrue(any("导出失败" in p for p in result["problems"]))

    def test_failure_leaves_diagnostics(self):
        page = FakePage(button_texts=(), downloads=[])
        self._run(page)
        diag_files = list(self.diag.glob("*-diagnostics.txt"))
        self.assertTrue(diag_files, "失败必须留存诊断（按钮清单）供改版后修选择器")


class TestInboxSemantics(CollectorTestBase):
    def test_same_name_same_content_is_duplicate(self):
        tmp = self.td / "dl"
        tmp.mkdir()
        f = tmp / "作品数据.xlsx"
        f.write_bytes(b"same")
        r1 = cte._save_to_inbox(f, self.inbox)
        self.assertEqual(r1["action"], "saved")
        f2 = tmp / "作品数据.xlsx"
        f2.write_bytes(b"same")
        r2 = cte._save_to_inbox(f2, self.inbox)
        self.assertEqual(r2["action"], "duplicate")
        self.assertEqual(len(list(self.inbox.iterdir())), 1)

    def test_same_name_updated_content_renamed_not_overwritten(self):
        tmp = self.td / "dl"
        tmp.mkdir()
        f = tmp / "作品数据.xlsx"
        f.write_bytes(b"old")
        cte._save_to_inbox(f, self.inbox)
        f.write_bytes(b"updated")
        r = cte._save_to_inbox(f, self.inbox)
        self.assertEqual(r["action"], "saved")
        self.assertTrue(r["file"].startswith("作品数据_2"))
        self.assertEqual((self.inbox / "作品数据.xlsx").read_bytes(), b"old",
                         "绝不静默覆盖箱内已有文件")

    def test_safe_name_strips_paths(self):
        self.assertEqual(cte._safe_name("../../etc/passwd.xlsx"),
                         ".._.._etc_passwd.xlsx")
        self.assertEqual(cte._safe_name(""), "export.xlsx")


class TestDryRun(CollectorTestBase):
    def test_dry_run_probes_login_only(self):
        page = FakePage(button_texts=("导出数据",))
        result = self._run(page, dry_run=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["files"], [])
        self.assertEqual(page.clicked, [], "dry-run 不得点击导出")
        self.assertEqual(len(page.goto_calls), 2, "dry-run 仍要逐页验证登录态")

    def test_dry_run_sends_no_summary_notification(self):
        page = FakePage(button_texts=("导出数据",))
        self._run(page, dry_run=True)
        self.assertEqual(self.notifications, [],
                         "dry-run 只探活，不发运行回执（避免噪音）")


class TestDelayedRedirect(CollectorTestBase):
    """SPA 延迟跳转竞态回归（2026-10-14 实测事故）。

    真实时序：goto 后 domcontentloaded 时 URL 未变，客户端 session 校验几秒后
    才跳登录页。曾因 dry-run 分支没等待 → 漏检误报「登录态有效」。
    """

    LOGIN_URL = "https://mp.toutiao.com/auth/page/login/?redirect=x"

    def _delayed_page(self, **kw):
        opts = dict(
            url_map={"works-single": self.LOGIN_URL, "/fans": self.LOGIN_URL},
            button_texts=("导出数据",),
            downloads=[FakeDownload("a.xlsx", b"1"), FakeDownload("b.xlsx", b"2")],
            login_redirect_after_ms=3000,   # 3 秒后才跳（SPA 真实时延）
        )
        opts.update(kw)
        return FakePage(**opts)

    def test_dry_run_detects_delayed_login_redirect(self):
        """修复前：dry-run 立即查 URL → 漏检 → ok=True 误报；修复后必须抓到。"""
        page = self._delayed_page()
        result = self._run(page, dry_run=True)
        self.assertFalse(result["ok"], "dry-run 必须能抓到延迟跳转的过期登录态")
        self.assertEqual(result["status"], "login_expired")

    def test_collect_mode_detects_delayed_login_redirect(self):
        page = self._delayed_page()
        result = self._run(page)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "login_expired")
        self.assertEqual(result["files"], [])
        self.assertEqual(len(page.goto_calls), 1, "过期即中止，不继续第二个页面")

    def test_no_redirect_not_false_positive(self):
        """没有跳转时不能误杀（判据不能矫枉过正）。"""
        page = self._delayed_page(login_redirect_after_ms=999_999)
        result = self._run(page, dry_run=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "ok")


class TestDailyMode(CollectorTestBase):
    """每日 06:00 模式：默认统计前一天，回执每次运行都发。"""

    def test_run_defaults_to_one_day(self):
        import inspect
        sig = inspect.signature(cte.run)
        self.assertEqual(sig.parameters["days"].default, 1,
                         "每日模式默认 days=1（统计前一天）")

    def test_yesterday_is_cst_based(self):
        # 06:00 CST 跑，统计目标 = 前一天
        self.assertEqual(cte._yesterday_str(datetime(2026, 10, 15, 6, 0,
                                                     tzinfo=cte.CST)), "2026-10-14")
        self.assertEqual(cte._yesterday_str(datetime(2026, 10, 15, 0, 30,
                                                     tzinfo=cte.CST)), "2026-10-14")

    def test_range_texts_switch_by_days(self):
        self.assertEqual(cte._range_texts_for(1)[0], "昨天", "每日模式优先「昨天」")
        self.assertIn("近7天", cte._range_texts_for(7))
        self.assertIn("近14天", cte._range_texts_for(14))

    def test_daily_mode_clicks_yesterday(self):
        page = FakePage(
            button_texts=("导出数据", "昨天", "近7天"),
            downloads=[FakeDownload("a.xlsx", b"1"), FakeDownload("b.xlsx", b"2")],
        )
        self._run(page)   # 默认 days=1
        self.assertIn("昨天", page.clicked, "每日模式应点「昨天」而非「近7天」")
        self.assertNotIn("近7天", page.clicked)

    def test_dated_name_appends_date_when_missing(self):
        self.assertEqual(cte._dated_name("作品数据.xlsx", "2026-10-13"),
                         "作品数据_2026-10-13.xlsx")

    def test_dated_name_keeps_existing_date(self):
        self.assertEqual(cte._dated_name("作品数据_2026-10-13.xlsx", "2026-10-13"),
                         "作品数据_2026-10-13.xlsx")
        self.assertEqual(cte._dated_name("作品数据_20261013.xlsx", "2026-10-13"),
                         "作品数据_20261013.xlsx")

    def test_inbox_file_carries_date_tag(self):
        with mock.patch.object(cte, "_yesterday_str", return_value="2026-10-13"):
            page = FakePage(
                button_texts=("导出数据", "昨天"),
                downloads=[FakeDownload("作品数据.xlsx", b"perf"),
                           FakeDownload("粉丝分析.xlsx", b"growth")],
            )
            result = self._run(page)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["date"], "2026-10-13")
        names = sorted(p.name for p in self.inbox.iterdir())
        self.assertEqual(names, ["作品数据_2026-10-13.xlsx", "粉丝分析_2026-10-13.xlsx"])
        ranges = [f.get("range") for f in result["files"]]
        self.assertEqual(ranges, ["昨天", "昨天"], "回执应记录实际点中的时间范围")

    def test_success_sends_summary_notification(self):
        with mock.patch.object(cte, "_yesterday_str", return_value="2026-10-13"):
            page = FakePage(
                button_texts=("导出数据", "昨天"),
                downloads=[FakeDownload("作品数据.xlsx", b"perf"),
                           FakeDownload("粉丝分析.xlsx", b"growth")],
            )
            result = self._run(page)
        titles = [t for t, _ in self.notifications]
        self.assertTrue(any("导出回收完成" in t for t in titles),
                        f"成功也要发回执（用户要求每次运行都有状态反馈）: {titles}")
        content = "\n".join(c for _, c in self.notifications)
        self.assertIn("2026-10-13", content)
        self.assertIn("作品数据_2026-10-13.xlsx", content)
        self.assertIn("perf-ingest 09:10", content)

    def test_duplicate_also_gets_receipt(self):
        """同内容重复投递（昨天跑过今天又跑）也发回执，标注已跳过。"""
        with mock.patch.object(cte, "_yesterday_str", return_value="2026-10-13"):
            for _ in range(2):
                page = FakePage(
                    button_texts=("导出数据", "昨天"),
                    downloads=[FakeDownload("作品数据.xlsx", b"perf"),
                               FakeDownload("粉丝分析.xlsx", b"growth")],
                )
                result = self._run(page)
        self.assertTrue(result["ok"])
        self.assertEqual([f["action"] for f in result["files"]],
                         ["duplicate", "duplicate"])
        content = "\n".join(c for _, c in self.notifications)
        self.assertIn("同内容", content, "重复投递的回执要标注「同内容跳过」")
        self.assertEqual(len(list(self.inbox.iterdir())), 2,
                         "同内容判重后箱里仍只有 2 个文件")


class TestConfig(unittest.TestCase):
    def test_pages_cover_perf_and_growth(self):
        names = [p.name for p in cte.PAGES]
        self.assertEqual(names, ["perf", "growth"])
        for spec in cte.PAGES:
            self.assertTrue(spec.url.startswith("https://mp.toutiao.com/"), spec.name)
            self.assertTrue(spec.export_texts, spec.name)


if __name__ == "__main__":
    unittest.main()
