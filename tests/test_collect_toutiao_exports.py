"""头条后台导出自动回收（scripts/collect_toutiao_exports.py）单测。

本地无头条登录态，全部路径用 fake playwright 覆盖：
  无登录态 / 登录过期（且不继续第二个页面）/ 导出成功 / 导出按钮缺失（一个页面失败
  不阻断另一个）/ 同名同内容判重（字节同 + 仅 zip 元数据不同的逻辑同）/ 同名不同内容
  另存 / dry-run 只探活 / 失败留诊断 / 确认框日期范围三级填写（fill→JS 注入→日历点选）/
  按钮禁用拦截 / 异步导出快速失败 / 新标签页证据与 popup 下载回收 / 点击后 toast 采集
  （导航徽标噪声不打印、导出反馈才打印）。

纪律对齐：collector 是「自动投递员」，失败只告警不静默、同名绝不覆盖、
降级通道（手动 inbox）永远保留。
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import collect_toutiao_exports as cte   # noqa: E402


def _write_xlsx(path: Path, rows: list, zip_date: tuple) -> None:
    """最小 xlsx 写入器（无 openpyxl 依赖，inlineStr 单元格）。

    zip_date 控制 zip 条目时间戳——用来制造「逻辑内容相同、字节不同」的
    真实重复导出场景（头条后台每次导出都重新打包，时间戳必变）。
    """
    def esc(s) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    body = []
    for ri, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{chr(64 + ci)}{ri}" t="inlineStr"><is><t>{esc(v)}</t></is></c>'
            for ci, v in enumerate(row, start=1))
        body.append(f"<row>{cells}</row>")
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main">'
             f"<sheetData>{''.join(body)}</sheetData></worksheet>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("xl/worksheets/sheet1.xml", date_time=zip_date),
                   sheet)


class FakeDownload:
    def __init__(self, filename: str, content: bytes):
        self.suggested_filename = filename
        self._content = content

    def save_as(self, dest):
        Path(dest).write_bytes(self._content)


class _FakeDownloadCtx:
    def __init__(self, download, fire=None):
        self._download = download
        self._fire = fire
        self.value = None

    def __enter__(self):
        # 真实 playwright：__enter__ 只开始监听，超时在 __exit__ 才抛——
        # 之前 fake 在 __enter__ 就抛，导致 with 块里的点击/确认框逻辑根本没跑，
        # 测试「通过」但测的不是真路径（2026-10-14 补确认框时抓出来的 fake 缺陷）
        if self._download is not None:
            self.value = self._download
            if self._fire is not None:
                # 真实时序：download 事件在下载开始时（点击后、__exit__ 前）触发，
                # context 级监听此时就能收到（popup 下载靠它回收）
                self._fire()
        return self

    def __exit__(self, *exc):
        if self._download is None and exc[0] is None:
            raise TimeoutError("Timeout exceeded while waiting for download")
        return False


class _FakeInput:
    """日期输入框 / 日历单元格 fake。

    readonly：只读输入框 fill() 抛错（对齐真实 playwright 行为）；
    js_ok：JS 注入能否写进值（字节系受控组件多为此路）；
    placeholder：值为空时靠它被认成日期框（对齐 _modal_date_inputs 的提示词判据）；
    inner_text_value：作为日历单元格时 inner_text 返回的日文本（如「8」），
    点中后经 page.picker_day_values 把完整日期回填到刚才打开的输入框。
    """

    def __init__(self, value="", *, readonly=False, visible=True, js_ok=True,
                 inner_text_value=None, placeholder=""):
        self._value = value
        self.readonly = readonly
        self._visible = visible
        self.js_ok = js_ok
        self._inner = inner_text_value
        self.placeholder = placeholder
        self.clicks = 0
        self.fills = []
        self.page = None

    def is_visible(self):
        return self._visible

    def get_attribute(self, name):
        if name == "value":
            return self._value
        if name == "readonly":
            return "true" if self.readonly else None
        if name == "placeholder":
            return self.placeholder
        if name == "aria-label":
            return ""
        return None

    def input_value(self):
        return self._value

    def fill(self, v):
        if self.readonly:
            raise RuntimeError("Cannot fill a readonly input")
        self._value = v
        self.fills.append(v)

    def click(self):
        self.clicks += 1
        if self.page is None:
            return
        if self._inner is None:
            self.page._last_input_clicked = self    # 点开输入框（打开日历选择器）
        else:
            # 日历日单元格：把完整日期回填到刚才打开的输入框（真实选择器行为）
            target = self.page._last_input_clicked
            full = self.page.picker_day_values.get(self._inner)
            if target is not None and full:
                target._value = full

    def evaluate(self, js, arg=None):
        if self.js_ok and arg is not None:
            self._value = arg
        return None

    def inner_text(self):
        return self._inner if self._inner is not None else self._value


class _FakeNewPage:
    def __init__(self, url):
        self.url = url


class _FakeLocator:
    def __init__(self, page, text):
        self._page = page
        self._text = text

    def count(self):
        # 真实 playwright 的 get_by_text 匹配任何元素的文本，不限 button——
        # 「下载Excel」是 <a> 链接，fake 必须 button_texts + link_texts 都认
        return 1 if (self._text in self._page.button_texts
                     or self._text in self._page.link_texts) else 0

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        """模拟 locator.wait_for——文案不存在就抛 TimeoutError（确认框没弹出来）。"""
        if self.count() == 0:
            raise TimeoutError(f"wait_for({state}) 超时: 未找到「{self._text}」")

    def click(self):
        self._page.clicked.append(self._text)
        target = self._page._navigate_on_click.get(self._text)
        if target:
            self._page.url = target      # 模拟误点：点了某文案后页面导航跑飞
        if self._text == "开始下载" and self._page.popup_url and self._page.context:
            # 模拟：点「开始下载」后新开标签页（导出可能在 popup 里触发）
            self._page.context.fire("page", _FakeNewPage(self._page.popup_url))
            if self._page.popup_download is not None:
                self._page.context.fire("download", self._page.popup_download)

    def evaluate(self, js, arg=None):
        # 「开始下载」禁用态探测（_is_confirm_disabled 走这里）
        return self._page.confirm_disabled


class _FakeElement:
    def __init__(self, text):
        self._text = text

    def inner_text(self):
        return self._text

    def is_visible(self):
        return True

    def click(self):
        pass


class _FakeKeyboard:
    def __init__(self, page):
        self._page = page

    def press(self, key):
        self._page.pressed.append(key)


class FakePage:
    """按脚本的调用顺序模拟 mp.toutiao.com 数据页。

    login_redirect_after_ms：模拟 SPA 延迟跳转——goto 时 URL 不变（应用壳先加载），
    累计等待超过阈值后才跳登录页。用于回归 dry-run 竞态误报
    （2026-10-14 实测：没等待的检测把延迟跳转漏检，误报「登录态有效」）。
    link_texts：`<a>` 链接类控件文本——2026-10-14 实测 perf 页导出控件「下载Excel」
    就是 `<a>` 不是 `<button>`，诊断与点击都要能覆盖到。
    inputs：确认框里的日期输入框（2026-10-14 三次实测：点「开始下载」仍无下载，
    改为显式填日期范围）；picker_cells：日历选择器里的日单元格；toast_texts：
    点击后出现的提示；popup_url/popup_download：点「开始下载」后新开标签页
    （及从 popup 触发的下载）；confirm_disabled：「开始下载」禁用态。
    """

    def __init__(self, *, url_map=None, button_texts=(), link_texts=(), downloads=None,
                 goto_error=None, login_redirect_after_ms=None, navigate_on_click=None,
                 inputs=(), picker_cells=(), toast_texts=(), confirm_disabled=False,
                 popup_url=None, popup_download=None, picker_day_values=None):
        self._url_map = dict(url_map or {})
        self.button_texts = list(button_texts)
        self.link_texts = list(link_texts)
        self._downloads = list(downloads or [])
        self.goto_error = goto_error
        self.url = "https://mp.toutiao.com/"
        self.clicked = []
        self.goto_calls = []
        self.screenshots = []
        self.pressed = []
        self.keyboard = _FakeKeyboard(self)
        self._redirect_after = login_redirect_after_ms
        self._waited_ms = 0
        self._navigate_on_click = dict(navigate_on_click or {})
        self.inputs = [self._link(i) for i in inputs]
        self.picker_cells = [self._link(c) for c in picker_cells]
        self.picker_day_values = dict(picker_day_values or {})
        self._last_input_clicked = None
        self.toast_texts = list(toast_texts)
        self.confirm_disabled = confirm_disabled
        self.popup_url = popup_url
        self.popup_download = popup_download
        self.context = None
        self._handlers = {}

    def _link(self, el):
        """给输入框/日历单元格挂 page 回引（点中日单元格要能回填输入框）。"""
        if el.page is None:
            el.page = self
        return el

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
        if selector == "button":
            return [_FakeElement(t) for t in self.button_texts]
        if selector == "a":
            return [_FakeElement(t) for t in self.link_texts]
        if selector == "input":
            return [i for i in self.inputs if i.is_visible()]
        if any(k in selector for k in ("calendar", "picker", "date")):
            return [c for c in self.picker_cells if c.is_visible()]
        if any(k in selector for k in ("toast", "message", "notice", "feedback", "alert")):
            return [_FakeElement(t) for t in self.toast_texts if t]
        return []

    def expect_download(self, timeout=None):
        download = self._downloads.pop(0) if self._downloads else None
        fire = (lambda: self.context.fire("download", download)) if download else None
        return _FakeDownloadCtx(download, fire=fire)

    def screenshot(self, path=None, full_page=False):
        self.screenshots.append(path)

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def fire(self, event, payload):
        for h in self._handlers.get(event, []):
            h(payload)


class FakeContext:
    def __init__(self, page):
        self._page = page
        page.context = self          # 让 page 的 expect_download 能回发 download 事件
        self._handlers = {}

    def new_page(self):
        return self._page

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def fire(self, event, payload):
        for h in self._handlers.get(event, []):
            h(payload)


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
            button_texts=("导出数据", "开始下载", "近7天"),
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
        self.assertIn("开始下载", page.clicked, "确认框必须点「开始下载」才真下载")

    def test_range_text_clicked_when_present(self):
        page = FakePage(
            button_texts=("导出数据", "开始下载", "最近7天"),
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

        page = SelectivePage(button_texts=("导出数据", "开始下载"))
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

    def test_same_name_metadata_only_diff_is_duplicate(self):
        """后台重复导出：只有 zip 元数据（时间戳）不同，数据行完全一样——
        2026-10-14 实测 growth 同日重跑两份文件 31 行 0 差异。按字节判重会
        每天攒一个 _2 冗余文件，故名冲突先比逻辑行，同内容即幂等丢弃。"""
        rows = [["日期", "粉丝总数"], ["2026-10-13", "11733"]]
        dl1 = self.td / "dl1"
        dl1.mkdir()
        f1 = dl1 / "粉丝趋势.xlsx"
        _write_xlsx(f1, rows, (2026, 10, 14, 14, 54, 0))
        self.assertEqual(cte._save_to_inbox(f1, self.inbox)["action"], "saved")
        dl2 = self.td / "dl2"      # 每次运行的下载临时目录各不相同
        dl2.mkdir()
        f2 = dl2 / "粉丝趋势.xlsx"
        _write_xlsx(f2, rows, (2026, 10, 14, 17, 7, 0))   # 仅 zip 时间戳不同
        kept = self.inbox / "粉丝趋势.xlsx"
        self.assertNotEqual(cte.sha256_of(kept), cte.sha256_of(f2),
                            "前提：字节级 sha 必须不同，否则测不到逻辑判重")
        r2 = cte._save_to_inbox(f2, self.inbox)
        self.assertEqual(r2["action"], "duplicate")
        self.assertEqual(len(list(self.inbox.iterdir())), 1, "不攒 _2 冗余文件")

    def test_same_name_different_rows_still_renamed(self):
        """逻辑内容真的变了（数据行不同）→ 仍然另存 _2，绝不静默丢弃。"""
        tmp = self.td / "dl"
        tmp.mkdir()
        f1 = tmp / "作品数据.xlsx"
        _write_xlsx(f1, [["标题", "阅读量"], ["A", "100"]], (2026, 10, 14, 9, 0, 0))
        cte._save_to_inbox(f1, self.inbox)
        f2 = tmp / "作品数据.xlsx"
        _write_xlsx(f2, [["标题", "阅读量"], ["A", "260"]], (2026, 10, 14, 9, 0, 0))
        r2 = cte._save_to_inbox(f2, self.inbox)
        self.assertEqual(r2["action"], "saved")
        self.assertTrue(r2["file"].startswith("作品数据_2"),
                        f"数据行不同必须另存: {r2}")

    def test_unparseable_files_fall_back_to_byte_compare(self):
        """解析不了的文件（如旧版 .xls 明确报错）→ 证明不了相同就按不同另存，
        绝不把「不同」误判成「同内容」而丢弃新文件。"""
        tmp = self.td / "dl"
        tmp.mkdir()
        f = tmp / "旧数据.xls"
        f.write_bytes(b"\xd0\xcf\x11\xe0old")
        cte._save_to_inbox(f, self.inbox)
        f.write_bytes(b"\xd0\xcf\x11\xe0new")
        r = cte._save_to_inbox(f, self.inbox)
        self.assertEqual(r["action"], "saved")
        self.assertTrue(r["file"].startswith("旧数据_2"))

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


class TestRealPageControls(CollectorTestBase):
    """2026-10-14 真实头条后台实测回归（用户本地 --headed 跑出的结论）。

    现场：dry-run 与 headed 都不再报登录过期（登录态有效），但两页都
    ExportButtonMissing——perf 页导出控件是右上角「⬇ 下载Excel」（<a> 链接，
    候选文案里没有它）；growth 页主内容区 3 秒时还是空白 + 压着营销弹窗。
    """

    PERF_URL = "https://mp.toutiao.com/profile_v4/analysis/works-single/article"
    GROWTH_URL = "https://mp.toutiao.com/profile_v4/analysis/fans"

    def _specs(self):
        return {p.name: p for p in cte.PAGES}

    def test_perf_export_matches_download_excel_link(self):
        """perf 页导出控件是 <a>「下载Excel」——文案候选必须命中它。"""
        page = FakePage(
            url_map={"works-single": self.PERF_URL},
            button_texts=("开始下载",), link_texts=("下载Excel",),
            downloads=[FakeDownload("作品数据.xlsx", b"perf"),
                       FakeDownload("b.xlsx", b"growth")],
        )
        result = self._run(page)
        self.assertTrue(result["ok"], result)
        self.assertIn("下载Excel", page.clicked)
        self.assertIn("开始下载", page.clicked,
                      "点「下载Excel」只是弹确认框，必须再点「开始下载」")

    def test_perf_collect_never_clicks_range_texts(self):
        """perf 页无时间范围选择器——点了「昨天」可能误点文章标题导航跑飞。"""
        page = FakePage(
            url_map={"works-single": self.PERF_URL},
            button_texts=("导出数据", "开始下载", "昨天", "昨日", "近1天"),
            downloads=[FakeDownload("作品数据.xlsx", b"perf")],
        )
        spec = self._specs()["perf"]
        _tmp, range_hit = cte._collect_one(page, FakeContext(page), spec,
                                           60_000, 120_000, days=1)
        self.assertIsNone(range_hit, "perf 页没有时间范围选择器，不应有点击范围的动作")
        for t in ("昨天", "昨日", "近1天"):
            self.assertNotIn(t, page.clicked, f"perf 页不得点「{t}」（文章标题误触风险）")
        self.assertIn("导出数据", page.clicked)

    def test_popup_dismissed_with_escape_before_export(self):
        page = FakePage(
            url_map={"works-single": self.PERF_URL},
            button_texts=("导出数据",),
            downloads=[FakeDownload("作品数据.xlsx", b"perf"),
                       FakeDownload("b.xlsx", b"growth")],
        )
        self._run(page)
        self.assertIn("Escape", page.pressed,
                      "营销弹窗必须先 Escape 关掉，避免遮挡导出控件/被误点")

    def test_growth_page_gets_extra_wait(self):
        """粉丝页图表渲染慢（3 秒时空白）——必须比 perf 等更久。"""
        specs = self._specs()
        self.assertGreater(specs["growth"].extra_wait_ms, 0)
        self.assertGreater(specs["growth"].extra_wait_ms, specs["perf"].extra_wait_ms)

    def test_growth_range_mode_is_dynamic(self):
        """粉丝页若有时间范围选择器，每日模式应点「昨天」。"""
        specs = self._specs()
        self.assertEqual(specs["growth"].range_mode, "dynamic")
        self.assertEqual(specs["perf"].range_mode, "none")

    def test_diagnostics_capture_link_controls(self):
        """诊断必须采到 <a> 链接类控件（首版只抓 button，看不到「下载Excel」）。"""
        page = FakePage(button_texts=("查看详情",), link_texts=("下载Excel",))
        spec = self._specs()["perf"]
        cte._dump_diagnostics(page, spec, "测试留证")
        txt = (self.diag / "perf-diagnostics.txt").read_text(encoding="utf-8")
        self.assertIn("下载Excel", txt, "按钮清单必须包含 <a> 链接类导出控件")


class TestExportModal(CollectorTestBase):
    """2026-10-14 三次实测：点「下载Excel」只弹确认框，必须再点「开始下载」才真下载。"""

    PERF_URL = "https://mp.toutiao.com/profile_v4/analysis/works-single/article"

    def test_modal_missing_fails_loudly_with_diagnostics(self):
        """确认框没弹出来 → 明确失败 + 留诊断，绝不傻等超时还被当成普通失败。"""
        page = FakePage(
            url_map={"works-single": self.PERF_URL},
            link_texts=("下载Excel",),      # 有导出按钮，但弹不出确认框
            button_texts=(),
            downloads=[],
        )
        result = self._run(page)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "export_failed")
        self.assertTrue(any("确认框" in p for p in result["problems"]),
                        f"失败原因必须说清是确认框问题: {result['problems']}")
        self.assertTrue(list(self.diag.glob("perf-diagnostics.txt")),
                        "确认框缺失也要留诊断")


class TestModalDateRange(CollectorTestBase):
    """2026-10-14 四次实测：点「开始下载」仍 120s 无下载。

    修复：确认框日期范围**显式填写**（fill → JS 注入 → 日历点选三级手段，
    读回值留证）+ 点击后证据采集（新标签页/导出类响应/toast）+ 按钮禁用拦截
    + 异步导出快速失败。perf 导出行 date 取自「发布时间」列，窗口内作品才会
    被每日 upsert 刷新，所以窗口至少 7 天、不能缩成「仅昨天」。
    """

    PERF_URL = "https://mp.toutiao.com/profile_v4/analysis/works-single/article"

    def _page(self, **kw):
        opts = dict(
            url_map={"works-single": self.PERF_URL},
            link_texts=("下载Excel",),
            button_texts=("开始下载",),
            downloads=[FakeDownload("作品数据.xlsx", b"perf"),
                       FakeDownload("b.xlsx", b"growth")],
        )
        opts.update(kw)
        return FakePage(**opts)

    def test_range_for_is_at_least_seven_days_ending_today(self):
        start, end = cte._modal_range_for(
            1, now=datetime(2026, 10, 14, 6, 0, tzinfo=cte.CST))
        self.assertEqual((start, end), ("2026-10-08", "2026-10-14"),
                         "每日模式也填 7 天窗口（窗口外作品永远停在旧值）")
        start, end = cte._modal_range_for(
            14, now=datetime(2026, 10, 14, 6, 0, tzinfo=cte.CST))
        self.assertEqual((start, end), ("2026-10-01", "2026-10-14"))

    def test_readonly_inputs_filled_via_js(self):
        """只读日期框 → JS 注入写值，读回=目标才算填上，随后照常下载。"""
        page = self._page(inputs=[
            _FakeInput("2026-10-01", readonly=True),
            _FakeInput("2026-10-07", readonly=True),
        ])
        result = self._run(page)
        self.assertTrue(result["ok"], result)
        self.assertEqual([i._value for i in page.inputs],
                         ["2026-10-08", "2026-10-14"])
        self.assertIn("开始下载", page.clicked)

    def test_editable_inputs_filled_directly(self):
        """非只读输入框走 fill()（playwright 原生 setter）。"""
        page = self._page(inputs=[_FakeInput("", placeholder="开始日期"),
                                  _FakeInput("", placeholder="结束日期")])
        result = self._run(page)
        self.assertTrue(result["ok"], result)
        self.assertEqual([i._value for i in page.inputs],
                         ["2026-10-08", "2026-10-14"])
        self.assertEqual([i.fills for i in page.inputs],
                         [["2026-10-08"], ["2026-10-14"]])

    def test_picker_fallback_when_js_fails(self):
        """JS 注入无效 → 点开日历选日（点完还要能读到目标值）。"""
        page = self._page(
            inputs=[
                _FakeInput("2026-10-01", readonly=True, js_ok=False),
                _FakeInput("2026-10-07", readonly=True, js_ok=False),
            ],
            picker_cells=[_FakeInput("", inner_text_value="8"),
                          _FakeInput("", inner_text_value="14")],
            picker_day_values={"8": "2026-10-08", "14": "2026-10-14"},
        )
        result = self._run(page)
        self.assertTrue(result["ok"], result)
        self.assertEqual([i._value for i in page.inputs],
                         ["2026-10-08", "2026-10-14"])
        self.assertGreaterEqual(page.inputs[0].clicks, 1,
                                "只读且 JS 无效必须点开日历选择器")

    def test_dates_not_settable_recorded_in_evidence(self):
        """三级手段都失败也要留证（目标 vs 读回），绝不静默按默认范围走。"""
        page = self._page(inputs=[
            _FakeInput("2026-10-01", readonly=True, js_ok=False),
            _FakeInput("2026-10-07", readonly=True, js_ok=False),
        ])
        spec = {p.name: p for p in cte.PAGES}["perf"]
        with mock.patch.object(cte, "_modal_range_for",
                               return_value=("2026-10-08", "2026-10-14")):
            ev = cte._confirm_export_modal(page, spec, range_days=7)
        self.assertEqual(len(ev["dates"]), 2, "起止两个框都要留读回值")
        self.assertTrue(any("2026-10-08" in d and "2026-10-01" in d
                            for d in ev["dates"]),
                        f"读回值必须原样留证: {ev['dates']}")

    def test_confirm_disabled_blocks_before_click(self):
        """「开始下载」禁用（日期未过校验的典型症状）→ 点击前拦截，不傻等下载。"""
        page = self._page(confirm_disabled=True, inputs=[
            _FakeInput("2026-10-01", readonly=True),
            _FakeInput("2026-10-07", readonly=True),
        ])
        result = self._run(page)
        self.assertFalse(result["ok"])
        self.assertTrue(any("禁用" in p for p in result["problems"]),
                        f"失败原因必须说清按钮禁用: {result['problems']}")
        self.assertNotIn("开始下载", page.clicked, "禁用态不应点击")

    def test_no_date_inputs_falls_back_to_default_range(self):
        """确认框里找不到日期输入框（改版）→ 留证 + 按后台默认范围继续，不崩。"""
        page = self._page()
        result = self._run(page)
        self.assertTrue(result["ok"], result)

    def test_async_export_toast_fails_fast(self):
        """点击后出现「导出成功/下载中心」提示 → 立即抛 ExportAsyncRequired，
        不再干等 120s，且失败消息指明下一步（下载中心链路）。"""
        page = self._page(downloads=[],
                          toast_texts=("导出成功，请前往下载中心查看",))
        result = self._run(page)
        self.assertFalse(result["ok"])
        joined = ";".join(result["problems"])
        self.assertIn("下载中心", joined,
                      f"异步导出必须点名「下载中心」: {joined}")

    def test_benign_toast_does_not_trigger_async_path(self):
        """普通提示（不含异步关键词）不得误判成异步导出。"""
        page = self._page(downloads=[], toast_texts=("数据每日14:00更新",))
        result = self._run(page)
        self.assertFalse(result["ok"])     # 没排队下载，最终仍超时失败
        joined = ";".join(result["problems"])
        self.assertNotIn("异步", joined, f"普通提示不得误判异步导出: {joined}")

    def test_nav_badge_toast_not_printed_as_feedback(self):
        """顶部导航的消息徽标（「消息 6」）也会被 message 选择器抓进 toasts——
        那不是导出反馈，不能打印出来误导（2026-10-14 实测每页都打印）。"""
        page = self._page(downloads=[FakeDownload("作品数据.xlsx", b"perf"),
                                    FakeDownload("b.xlsx", b"growth")],
                          toast_texts=("消息", "6"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = self._run(page)
        self.assertTrue(result["ok"], result)
        self.assertNotIn("页面提示", buf.getvalue(),
                         f"导航徽标不是导出反馈，不应打印: {buf.getvalue()[-200:]}")

    def test_real_feedback_toast_is_printed(self):
        """真正的导出类提示（如排队中）要打印出来，肉眼可见。"""
        page = self._page(downloads=[FakeDownload("作品数据.xlsx", b"perf"),
                                    FakeDownload("b.xlsx", b"growth")],
                          toast_texts=("导出排队中，请稍候",))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = self._run(page)
        self.assertTrue(result["ok"], result)
        self.assertIn("页面提示", buf.getvalue())
        self.assertIn("导出排队中", buf.getvalue())

    def test_new_tab_recorded_in_failure_message(self):
        """点击后新开标签页 → 证据留 URL，失败消息不再光秃秃。"""
        page = self._page(downloads=[],
                          popup_url="https://mp.toutiao.com/download/center")
        result = self._run(page)
        self.assertFalse(result["ok"])
        joined = ";".join(result["problems"])
        self.assertIn("download/center", joined,
                      f"新标签页 URL 必须进失败消息: {joined}")

    def test_popup_download_recovered_from_context(self):
        """下载从新标签页触发：page 级 expect_download 等不到，但 context 级
        监听已收到 → 照样落袋为安（不把能拿到的数据当失败）。"""
        calls = {"n": 0}

        class PopupPage(FakePage):
            def expect_download(self, timeout=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    return _FakeDownloadCtx(None)   # perf：本页等不到（下载在 popup）
                return _FakeDownloadCtx(FakeDownload("粉丝分析.xlsx", b"growth"))

        page = PopupPage(
            url_map={"works-single": self.PERF_URL},
            link_texts=("下载Excel",),
            button_texts=("开始下载",),
            downloads=[],
            popup_url="https://mp.toutiao.com/export/popup",
            popup_download=FakeDownload("作品数据.xlsx", b"popup-perf"),
        )
        result = self._run(page)
        self.assertTrue(result["ok"], result)
        saved = [f for f in result["files"] if f["action"] == "saved"]
        self.assertEqual(len(saved), 2, "perf（popup 下载）+ growth 都应落箱")
        self.assertTrue(any(f["file"].startswith("作品数据") for f in saved))

    def test_config_perf_modal_range_days(self):
        """perf 页确认框显式填 7 天；growth 页无确认框不填。"""
        specs = {p.name: p for p in cte.PAGES}
        self.assertEqual(specs["perf"].modal_range_days, 7)
        self.assertEqual(specs["growth"].modal_range_days, 0)

    def test_run_accepts_perf_range_override(self):
        import inspect
        sig = inspect.signature(cte.run)
        self.assertEqual(sig.parameters["perf_range_days"].default, None,
                         "perf_range_days 默认 None=按页面配置")


class TestGrowthNavEntry(CollectorTestBase):
    """growth 页深链白屏——必须点左侧导航「粉丝数据」走客户端路由（2026-10-14 实测）。"""

    def test_growth_enters_via_nav_link(self):
        page = FakePage(
            url_map={
                "works-single": "https://mp.toutiao.com/profile_v4/analysis/works-single/article",
                "/fans": "https://mp.toutiao.com/profile_v4/analysis/fans",
            },
            button_texts=("导出数据", "开始下载", "粉丝数据"),
            downloads=[FakeDownload("作品数据.xlsx", b"p"), FakeDownload("粉丝分析.xlsx", b"g")],
        )
        result = self._run(page)
        self.assertTrue(result["ok"], result)
        self.assertIn("粉丝数据", page.clicked,
                      "growth 页必须点左侧导航进入（深链白屏）")

    def test_growth_nav_missing_still_attempts_export(self):
        """导航项找不到（改版）→ 告警但继续按直达 URL 试，不静默放弃。"""
        page = FakePage(
            url_map={
                "works-single": "https://mp.toutiao.com/profile_v4/analysis/works-single/article",
                "/fans": "https://mp.toutiao.com/profile_v4/analysis/fans",
            },
            button_texts=("导出数据", "开始下载"),
            downloads=[FakeDownload("作品数据.xlsx", b"p"), FakeDownload("粉丝分析.xlsx", b"g")],
        )
        result = self._run(page)
        self.assertTrue(result["ok"], result)
        self.assertNotIn("粉丝数据", page.clicked)

    def test_config_growth_nav_and_perf_modal(self):
        specs = {p.name: p for p in cte.PAGES}
        self.assertEqual(specs["growth"].nav_text, "粉丝数据")
        self.assertEqual(specs["perf"].nav_text, "", "perf 页直达 URL 正常，不走导航")
        self.assertTrue(specs["perf"].confirm_modal, "perf 导出有点「开始下载」的确认框")
        self.assertFalse(specs["growth"].confirm_modal)


class TestDailyMode(CollectorTestBase):
    """每日 14:05 模式：默认统计前一天，回执每次运行都发。"""

    def test_run_defaults_to_one_day(self):
        import inspect
        sig = inspect.signature(cte.run)
        self.assertEqual(sig.parameters["days"].default, 1,
                         "每日模式默认 days=1（统计前一天）")

    def test_yesterday_is_cst_based(self):
        # 14:05 CST 跑，统计目标 = 前一天
        self.assertEqual(cte._yesterday_str(datetime(2026, 10, 15, 14, 5,
                                                     tzinfo=cte.CST)), "2026-10-14")
        self.assertEqual(cte._yesterday_str(datetime(2026, 10, 15, 0, 30,
                                                     tzinfo=cte.CST)), "2026-10-14")

    def test_range_texts_switch_by_days(self):
        self.assertEqual(cte._range_texts_for(1)[0], "昨天", "每日模式优先「昨天」")
        self.assertIn("近7天", cte._range_texts_for(7))
        self.assertIn("近14天", cte._range_texts_for(14))

    def test_daily_mode_clicks_yesterday(self):
        page = FakePage(
            button_texts=("导出数据", "开始下载", "昨天", "近7天"),
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
                button_texts=("导出数据", "开始下载", "昨天"),
                downloads=[FakeDownload("作品数据.xlsx", b"perf"),
                           FakeDownload("粉丝分析.xlsx", b"growth")],
            )
            result = self._run(page)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["date"], "2026-10-13")
        names = sorted(p.name for p in self.inbox.iterdir())
        self.assertEqual(names, ["作品数据_2026-10-13.xlsx", "粉丝分析_2026-10-13.xlsx"])
        ranges = [f.get("range") for f in result["files"]]
        self.assertEqual(ranges, [None, "昨天"],
                         "perf 页无范围选择器（None），growth 页点「昨天」")

    def test_success_sends_summary_notification(self):
        with mock.patch.object(cte, "_yesterday_str", return_value="2026-10-13"):
            page = FakePage(
                button_texts=("导出数据", "开始下载", "昨天"),
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
        self.assertIn("perf-ingest 14:40", content)

    def test_duplicate_also_gets_receipt(self):
        """同内容重复投递（昨天跑过今天又跑）也发回执，标注已跳过。"""
        with mock.patch.object(cte, "_yesterday_str", return_value="2026-10-13"):
            for _ in range(2):
                page = FakePage(
                    button_texts=("导出数据", "开始下载", "昨天"),
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

    def test_perf_export_texts_include_download_excel(self):
        """2026-10-14 实测：perf 页导出控件文案是「下载Excel」（<a> 链接）。"""
        specs = {p.name: p for p in cte.PAGES}
        self.assertIn("下载Excel", specs["perf"].export_texts)
        self.assertEqual(specs["perf"].export_texts[0], "下载Excel",
                         "实测命中的文案排第一，少一次无效尝试")


if __name__ == "__main__":
    unittest.main()
