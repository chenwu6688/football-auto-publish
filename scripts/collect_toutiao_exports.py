#!/usr/bin/env python3
"""头条后台导出自动回收 —— 给 perf-ingest 投递箱当「自动投递员」。

问题：绩效/转粉数据回流全靠人工「登录头条后台 → 点导出 → 下载 → 丢 data/inbox/」，
忘一次就断一周数据，看板与调参样本永远滞后。

做法：用 publisher.py 同款登录态（TOUTIAO_AUTH_FILE / CI secret TOUTIAO_AUTH_GZ）和
同一套 playwright 基建，定时登录 mp.toutiao.com，在「作品数据」「粉丝分析」两个页面
各点一次导出，文件落 data/inbox/——之后的识别/幂等/入库全部由 perf-ingest 链路
（scripts/ingest_exports.py）负责，本脚本只管「把文件投进箱子」。

设计纪律（与手动通道的关系）：
  1. 自动化是增量不是替换——data/inbox/ 手动投递通道永远保留；collector 失败只告警，
     绝不阻塞、绝不清空 inbox；
  2. 失败必须响亮——登录过期 / 页面改版（导出按钮找不到）/ 下载超时，一律
     wxpusher 告警 + 非零退出 + 诊断截图，绝不静默跳过；
  3. 每次运行都有回执——成功也发 WxPusher 汇总（投递了什么、统计哪一天），
     失败的通知更详细（含恢复指引），用户对投递箱状态始终知情；
  4. 同名不覆盖——箱内已有同名文件时按 sha256 判重：内容相同 → 丢弃（真重复），
     内容不同（数据已更新的再次导出）→ 加 _2 后缀，绝不静默覆盖；
  5. 落盘文件名带统计日期——下载名本身不含日期时补 `_YYYY-MM-DD`（取导出窗口
     最后一天，即默认的「前一天」），箱内一眼可辨是哪天数据；含日期则不重复加；
  6. 选择器集中在本文件 PAGES 配置——头条后台改版时只需改这一处；
  7. 登录态失效与 publisher 同判据（URL 含 /auth/ 或 /login）——复用同一检测口径。
  8. 2026-10-14 真实后台实测台账（改版时先看这里，别再瞎猜）：
     - perf 页（作品数据→单篇）导出控件是右上角「⬇ 下载Excel」，是个 `<a>` 链接
       而不是 `<button>`——诊断脚本首版只抓 button，按钮清单里反而看不到它；
       点「下载Excel」**不直接下载**，而是弹确认框（发布时间范围 + 作品总数 +
       取消/开始下载），必须再点「**开始下载**」才真正触发下载（2026-10-14 二次
       实测：不点它就是干等 120 秒超时）；确认框里的日期输入框是只读日历控件，
       本脚本**显式填写近 7 天窗口**（fill → JS 注入 → 日历点选三级手段，
       读回值留证）——perf 导出行的 date 取自「发布时间」列，窗口内的作品
       才会被每日 upsert 刷新，窗口外作品永远停在旧值，所以不能缩成「仅昨天」；
       2026-10-14 三次实测：点了「开始下载」仍 120s 无下载——脚本随后补了
       「点击后证据采集」（新标签页 / 导出类网络响应 / toast 提示 / 日期读回值）
       与「异步导出快速失败」，下次失败一眼可辨是异步导出、新标签页还是按钮禁用；
     - growth 页（粉丝数据）**深链 URL 直达是白屏**（SPA 不认，内容区整个空白，
       只渲染左侧导航）——必须点左侧导航「粉丝数据」链接走客户端路由进去；
       页面还压着「被关注自动回复」营销弹窗（Escape 关不掉，但只要不遮挡导出
       控件就不管它——绝不点弹窗里的「立即体验」，那会导航跑飞）；
       2026-10-14 四次实测：导航 + 点「昨日」后导出成功（落箱
       粉丝趋势_2026-09-14-2026-10-13.xlsx；文件名跨度 30 天=后台默认区间，
       「昨日」点击是否真生效待首跑后核对文件内容）；
     - perf 页注明「每日 14:00 更新前一日数据」——06:00 导到的前一天是未结算值，
       次日 06:00 的导出会自动覆盖为结算值（upsert 自愈，不必等 14:00 再跑）。

频率：**每天 06:00 CST**（CI cron UTC 22:00，见 .github/workflows/toutiao-collector.yml），
默认统计**前一天**（days=1）——与 perf-ingest 09:10 消费、绩效台账按日 upsert
（同 date+slug 覆盖，不重复计数）、转粉日差跨度守卫（相邻登记日间隔 1 天）全部对齐。
频率仍保持克制：每天一次全量导出，不做高频轮询（风控克制）。

用法：
  python3 scripts/collect_toutiao_exports.py                 # CI 模式（headless，默认统计前一天）
  python3 scripts/collect_toutiao_exports.py --headed        # 本地调试（看得见浏览器）
  python3 scripts/collect_toutiao_exports.py --dry-run       # 只检测登录态，不导出
  python3 scripts/collect_toutiao_exports.py --days 14       # 导出窗口（天）
  python3 scripts/collect_toutiao_exports.py --perf-range-days 30   # 作品数据确认框填 30 天
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.sync_api import sync_playwright   # noqa: E402
from publisher import send_wxpusher               # noqa: E402

CST = timezone(timedelta(hours=8))

INBOX = Path(os.environ.get("COLLECTOR_INBOX", PROJECT_ROOT / "data" / "inbox"))
AUTH_FILE = Path(os.environ.get("TOUTIAO_AUTH_FILE", PROJECT_ROOT / "toutiao_auth.json"))
DIAG_DIR = Path(os.environ.get("COLLECTOR_DIAG_DIR", "/tmp/toutiao-collector-diag"))

LOGIN_MARKERS = ("/auth/", "/login")
DEFAULT_NAV_TIMEOUT_MS = 60_000
DEFAULT_DOWNLOAD_TIMEOUT_MS = 120_000

# 确认框日期输入框：值为单个日期或日期区间（2026-10-08 / 2026/10/8 ~ 2026-10-14）
SINGLE_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
RANGE_DATE_RE = re.compile(
    r"^(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*[~至\-]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})$")
# 值为空的日期框靠 placeholder/aria-label 认（如「开始日期」「请选择时间」）
DATE_HINT_WORDS = ("日期", "时间")
# 点「开始下载」后可能出现的提示（Arco/字节系组件 class 名多变，按关键词广覆盖）
TOAST_SELECTORS = ("[class*='toast']", "[class*='message']", "[class*='notice']",
                   "[class*='feedback']", "[role='alert']")
# 命中这些关键词 = 导出是异步的（文件进「下载中心」，不会触发浏览器下载事件）
ASYNC_EXPORT_HINTS = ("下载中心", "导出成功", "导出中", "导出任务", "消息中心", "扫码")
# React 受控输入框万能注入：原生 setter + input/change 事件（只读日历控件用）
JS_SET_VALUE = (
    "(el, v) => {"
    " const proto = el.tagName === 'TEXTAREA'"
    " ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;"
    " const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
    " setter.call(el, v);"
    " el.dispatchEvent(new Event('input', {bubbles: true}));"
    " el.dispatchEvent(new Event('change', {bubbles: true}));"
    "}"
)


@dataclass(frozen=True)
class PageSpec:
    """一个后台数据页的回收配置（改版只改这里）。"""

    name: str                 # perf / growth（与投递箱类型识别对应）
    url: str
    export_texts: tuple       # 导出按钮的候选文案（按序尝试，命中即止）
    range_mode: str = "none"  # none=该页无时间范围选择器 / dynamic=按 --days 动态点
    extra_wait_ms: int = 0    # 登录检测后的额外等待（图表渲染慢的页面用）
    confirm_modal: bool = False   # 点了导出按钮后是否还有确认框（需再点「开始下载」）
    nav_text: str = ""        # 非空=深链白屏，改点左侧导航该文案进入（客户端路由）
    modal_range_days: int = 0  # >0=确认框里显式填这么多天的日期窗口（0=不填，用后台默认）


PAGES = (
    PageSpec(
        name="perf",
        url="https://mp.toutiao.com/profile_v4/analysis/works-single/article",
        # 2026-10-14 实测：单篇 tab 的导出控件是右上角「⬇ 下载Excel」（<a> 链接，
        # 不是 <button>，所以诊断的按钮清单里根本看不到它）
        export_texts=("下载Excel", "导出数据", "导出Excel", "下载数据", "导出"),
        # 该页无独立时间范围选择器（范围在导出确认框里，显式填近 7 天）——
        # 绝不点「昨天」之类：文章列表里标题含「昨天」的作品会被误点导航跑飞。
        range_mode="none",
        # 点「下载Excel」后弹确认框（发布时间范围 + 作品总数 + 取消/开始下载），
        # 必须再点「开始下载」才真正下载（2026-10-14 实测：不点=干等 120s 超时）
        confirm_modal=True,
        # 确认框日期窗口显式填 7 天（三级手段 fill→JS 注入→日历点选，读回留证）：
        # perf 导出行 date 取自「发布时间」列，窗口内作品才被每日 upsert 刷新
        modal_range_days=7,
    ),
    PageSpec(
        name="growth",
        url="https://mp.toutiao.com/profile_v4/analysis/fans",
        # 粉丝页导出控件文案未经真实验证（深链白屏，从没渲染出来过）——多放候选，命中即止
        export_texts=("导出数据", "下载Excel", "导出Excel", "下载数据", "导出"),
        range_mode="dynamic",      # 若该页有时间范围选择器，按 --days 点（每日模式点「昨天」）
        extra_wait_ms=10000,       # 深链白屏 + 图表渲染慢，多等一会儿再找导出控件
        # 2026-10-14 实测：直接 goto 该 URL 内容区整个空白（SPA 不认深链），
        # 必须点左侧导航「粉丝数据」链接走客户端路由，页面才真正渲染
        nav_text="粉丝数据",
    ),
)


class ExportButtonMissing(Exception):
    """页面上找不到导出按钮（后台改版/风控页）。"""


class ExportModalMissing(Exception):
    """点了导出按钮但没弹出确认框（或框里没有「开始下载」/按钮禁用）。"""


class ExportAsyncRequired(Exception):
    """导出是异步模式：点「开始下载」后只出提示（导出成功/下载中心），
    文件不会触发浏览器下载事件——需要改走「下载中心」链路（下一步）。"""


class LoginExpired(Exception):
    """登录态失效（跳登录页）。"""


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(name: str) -> str:
    """下载文件名去掉路径分隔符与首尾空白，防止落到箱子外。"""
    cleaned = re.sub(r"[\\/\r\n\t]", "_", str(name)).strip() or "export.xlsx"
    return cleaned[:120]


def _yesterday_str(now: datetime | None = None) -> str:
    """统计目标日 = 前一天（CST）。CI 每天 06:00 跑，统计的是昨天一整天。"""
    now = now or datetime.now(CST)
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def _range_texts_for(days: int) -> tuple:
    """按导出窗口生成时间范围候选文案（找到了就点，找不到不阻断）。

    days=1（每日模式）优先「昨天」——头条后台的「昨天」= 昨天 00:00-23:59 整天，
    正是「统计前一天」的语义；找不到再退「近1天」。
    """
    if days <= 1:
        return ("昨天", "昨日", "近1天", "1天")
    return (f"近{days}天", f"最近{days}天", f"{days}天")


def _dated_name(name: str, date_tag: str) -> str:
    """给落盘文件名补统计日期；stem 已含该日期（2026-10-13 / 20261013）则不重复加。"""
    p = Path(name)
    stem, suffix = p.stem, p.suffix
    compact = date_tag.replace("-", "")
    if date_tag in stem or compact in stem:
        return name
    return f"{stem}_{date_tag}{suffix}"


def _try_click(page, texts) -> bool:
    """按候选文案依次尝试点击；命中返回 True，全部未命中返回 False。

    用 get_by_text + count 而非固定 CSS 选择器——后台改版时文案通常比结构稳定。
    """
    for text in texts:
        try:
            loc = page.get_by_text(text, exact=False)
            if loc.count() > 0:
                loc.first.click()
                return True
        except Exception:
            continue
    return False


def _click_first(page, texts) -> str | None:
    """与 _try_click 同逻辑，但返回实际命中的文案（未命中返回 None）——用于回执留痕。"""
    for text in texts:
        try:
            loc = page.get_by_text(text, exact=False)
            if loc.count() > 0:
                loc.first.click()
                return text
        except Exception:
            continue
    return None


def _dump_diagnostics(page, spec: PageSpec, note: str,
                      evidence: dict | None = None) -> str:
    """失败现场留证：页面截图 + 可见控件文本清单 + 点击后现场证据（供改版后修选择器）。

    2026-10-14 教训：首版只抓 `<button>`，而 perf 页导出控件「下载Excel」是 `<a>`
    链接——清单里反而看不到真正的导出按钮。现在 button / a / [role=button] 全抓。
    evidence：点击「开始下载」后的新标签页/导出类响应/toast/日期读回值。
    """
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    shot = DIAG_DIR / f"{spec.name}.png"
    try:
        page.screenshot(path=str(shot), full_page=False)
    except Exception:
        shot = None
    controls, seen = [], set()
    for selector in ("button", "a", "[role=button]"):
        try:
            for el in page.query_selector_all(selector):
                try:
                    t = (el.inner_text() or "").strip()
                except Exception:
                    continue
                if t and t not in seen:
                    seen.add(t)
                    controls.append(f"{selector}:{t[:40]}")
        except Exception:
            continue
    diag = DIAG_DIR / f"{spec.name}-diagnostics.txt"
    ev_line = ""
    if evidence:
        ev_line = (f"现场证据: {json.dumps(evidence, ensure_ascii=False, default=str)}\n")
    diag.write_text(
        f"页面: {spec.name} ({spec.url})\n原因: {note}\nURL: {getattr(page, 'url', '?')}\n"
        f"可见控件: {controls}\n{ev_line}截图: {shot}\n",
        encoding="utf-8")
    print(f"   🔍 诊断已留存: {diag}" + (f" + {shot}" if shot else ""))
    return str(diag)


def _assert_logged_in(page, spec: PageSpec, nav_timeout_ms: int) -> None:
    """登录态检测——dry-run 与正式回收共用的**唯一判据**。

    头条后台是 SPA：`domcontentloaded` 时应用壳已加载、URL 尚未变化，要等几秒
    客户端 session 校验（或风控挑战）后才可能跳登录页。2026-10-14 实测事故：
    dry-run 分支曾漏了这段等待，goto 后立即查 URL → 延迟跳转被漏检 →
    dry-run 两页全绿误报「登录态有效」，紧接的 headed 实跑第一页就跳登录页。

    判据只保留这一处：等 3 秒（与页面异步校验的典型时延对齐）后查 URL，
    两个模式共用，杜绝再次分叉。登录过期抛 LoginExpired。
    """
    page.goto(spec.url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
    page.wait_for_timeout(3000)
    url = (page.url or "").lower()
    if any(m in url for m in LOGIN_MARKERS):
        raise LoginExpired(f"{spec.name} 页面跳转到登录页（{url[:80]}）")


def _dismiss_popups(page) -> None:
    """关掉页面营销弹窗，避免遮挡导出控件或被误点。

    2026-10-14 实测：粉丝页压着「被关注自动回复功能上线」弹窗（内含「立即体验」，
    点它会被导航到别的页面）。只按 Escape——绝不点弹窗里的行动按钮。
    """
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass


# ---------- 确认框日期范围填写（三级手段） ----------

def _norm_date(s: str) -> str | None:
    """归一化单个日期串（2026/10/8 → 2026-10-08）；不是单日期返回 None。"""
    s = (s or "").strip().replace("/", "-")
    if not SINGLE_DATE_RE.match(s):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _modal_range_for(days: int, now: datetime | None = None) -> tuple[str, str]:
    """确认框里要填的日期窗口（CST）：至少 7 天、到今天为止。

    为什么至少 7 天：perf 导出行的 date 取自「发布时间」列（toutiao_import
    的列名映射），绩效台账按 (date, slug) upsert——窗口内的作品每天被重新
    导出、累计值刷新；窗口外的作品永远停在旧值。后台默认「近 7 天」正是
    这个语义，手动投递时代也是如此，别缩小成「仅昨天」。
    """
    now = now or datetime.now(CST)
    span = max(int(days), 7)
    end = now.date()
    start = end - timedelta(days=span - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _read_input(el) -> str:
    """读输入框当前值（property 优先，attribute 兜底）。"""
    for getter in (lambda: el.input_value(), lambda: el.get_attribute("value")):
        try:
            v = (getter() or "").strip()
        except Exception:
            continue
        if v:
            return v
    return ""


def _modal_date_inputs(page) -> list:
    """确认框里的日期输入框：可见、值为日期/区间、或 placeholder 提示日期的 input。

    值为空的日期框（改版后默认不预填）靠 placeholder 认——认不出就留证
    「未找到日期输入框」并按后台默认范围导出，不崩不猜。
    """
    found = []
    try:
        els = page.query_selector_all("input")
    except Exception:
        return found
    for el in els:
        val = _read_input(el)
        if val and (SINGLE_DATE_RE.match(val) or RANGE_DATE_RE.match(val)):
            found.append(el)
            continue
        try:
            hint = f"{el.get_attribute('placeholder') or ''}" \
                   f"{el.get_attribute('aria-label') or ''}"
        except Exception:
            hint = ""
        if any(w in hint for w in DATE_HINT_WORDS):
            found.append(el)
    return found


def _click_day_cell(page, day: str) -> bool:
    """日历面板里点文本恰为 day（如「8」）的可见单元格。"""
    for sel in ("[class*='calendar']", "[class*='picker']", "[class*='date']"):
        try:
            cells = page.query_selector_all(sel)
        except Exception:
            continue
        for cell in cells:
            try:
                if not cell.is_visible():
                    continue
                if (cell.inner_text() or "").strip() == day:
                    cell.click()
                    return True
            except Exception:
                continue
    return False


def _pick_calendar_day(page, date_str: str) -> bool:
    """只读日历控件选日：当前月视图找不到就先翻「上一月」（近 7 天最多跨 1 个月）。"""
    day = str(int(date_str.split("-")[2]))
    for _ in range(3):
        if _click_day_cell(page, day):
            _try_click(page, ("确定", "完成", "选择"))
            return True
        if not _try_click(page, ("上一月", "上个月", "上一页")):
            return False
        page.wait_for_timeout(300)
    return False


def _fill_date_input(page, el, date_str: str) -> str:
    """把单个日期输入框设成 date_str，返回读回值（三级手段依次尝试，读回才算数）。

    1. 非只读 → fill()（playwright 原生 setter，React 受控组件可感知）；
    2. 只读 → JS 原生 setter + input/change 事件（字节系日期组件多为此路）；
    3. 仍不符 → 点开日历选择器，翻月点目标日 + 「确定」。
    """
    try:
        readonly = el.get_attribute("readonly") is not None
    except Exception:
        readonly = False
    if not readonly:
        try:
            el.fill(date_str)
        except Exception:
            pass
        if _norm_date(_read_input(el)) == date_str:
            return _read_input(el)
    try:
        el.evaluate(JS_SET_VALUE, date_str)
    except Exception:
        pass
    if _norm_date(_read_input(el)) == date_str:
        return _read_input(el)
    try:
        el.click()
        page.wait_for_timeout(600)
        _pick_calendar_day(page, date_str)
    except Exception:
        pass
    return _read_input(el)


def _is_confirm_disabled(loc) -> bool:
    """「开始下载」是否禁用（按钮本体/祖先 button/aria-disabled/常见 class 都查）。"""
    try:
        return bool(loc.evaluate(
            "el => { const b = el.closest('button') || el;"
            " return !!(b.disabled || b.getAttribute('aria-disabled') === 'true'"
            " || b.classList.contains('is-disabled') || b.classList.contains('disabled')); }"))
    except Exception:
        return False


# ---------- 点击「开始下载」后的现场证据采集 ----------

def _watch_export_events(page, context) -> dict:
    """装监听，点击后回收现场证据（返回 dict，之后被事件填充）。

    2026-10-14 三次实测教训：点了「开始下载」干等 120s 超时，但超时前页面
    什么状态完全没记录（弹框还在？出了 toast？开了新标签页？导出 API 到底
    发没发？）——没有证据就只能瞎猜。现在三类证据全留：
      新标签页（导出可能在 popup 里触发）/ download 事件 / 导出类网络响应。
    """
    evidence = {"new_pages": [], "downloads": [], "download_objs": [],
                "responses": [], "toasts": []}

    def _on_page(new_page):
        try:
            evidence["new_pages"].append(getattr(new_page, "url", "?"))
        except Exception:
            evidence["new_pages"].append("?")

    def _on_download(dl):
        evidence["download_objs"].append(dl)
        try:
            evidence["downloads"].append(getattr(dl, "suggested_filename", "?"))
        except Exception:
            evidence["downloads"].append("?")

    def _on_response(resp):
        try:
            url = (resp.url or "").lower()
            if any(k in url for k in ("export", "download", "xlsx", "excel", "csv")):
                evidence["responses"].append(
                    f"{getattr(resp, 'status', '?')} {resp.url[:140]}")
        except Exception:
            pass

    try:
        if context is not None:
            context.on("page", _on_page)
            context.on("download", _on_download)
    except Exception:
        pass
    try:
        page.on("response", _on_response)
    except Exception:
        pass
    return evidence


def _collect_toasts(page) -> list:
    """页面上可见的提示类元素文本（toast/message/notice/alert）。"""
    out, seen = [], set()
    for sel in TOAST_SELECTORS:
        try:
            els = page.query_selector_all(sel)
        except Exception:
            continue
        for el in els:
            try:
                if not el.is_visible():
                    continue
                t = (el.inner_text() or "").strip()
            except Exception:
                continue
            if t and t not in seen:
                seen.add(t)
                out.append(t[:120])
    return out


def _detect_async_export(evidence: dict) -> str | None:
    """证据里有没有「异步导出」提示（命中关键词即返回该提示文本）。"""
    for t in evidence.get("toasts", []):
        if any(h in t for h in ASYNC_EXPORT_HINTS):
            return t
    return None


def _observe_after_click(page, evidence: dict, observe_ms: int = 4000) -> None:
    """点「开始下载」后的观察窗：等几秒收 toast/新标签页，有料就提前收手。

    识别到异步导出提示（导出成功/下载中心）立即抛 ExportAsyncRequired——
    不必再干等 120s，且失败原因一眼可辨（下一步就是接「下载中心」链路）。
    """
    waited = 0
    while waited < observe_ms:
        page.wait_for_timeout(1000)
        waited += 1000
        evidence["toasts"] = _collect_toasts(page)
        if evidence["toasts"] or evidence["new_pages"] or evidence["download_objs"]:
            break
    if evidence["toasts"]:
        print(f"   💬 页面提示: {' | '.join(evidence['toasts'])[:200]}")
    if evidence["new_pages"]:
        print(f"   🗂️ 点击后新开标签页: {evidence['new_pages']}")
    async_hint = _detect_async_export(evidence)
    if async_hint:
        raise ExportAsyncRequired(
            f"导出为异步模式（页面提示:「{async_hint}」）——文件进「下载中心」，"
            f"不会触发浏览器下载事件，需改走下载中心链路")


def _evidence_brief(ev: dict | None) -> str:
    """现场证据压缩成一行（进失败消息，超时也不再是光秃秃的 TimeoutError）。"""
    if not ev:
        return ""
    parts = []
    if ev.get("dates"):
        parts.append("日期框:" + ";".join(ev["dates"]))
    elif ev.get("date_inputs_found") is not None:
        parts.append(f"日期输入框×{ev['date_inputs_found']}")
    if ev.get("new_pages"):
        parts.append("新标签页:" + ",".join(ev["new_pages"]))
    if ev.get("responses"):
        parts.append("导出类响应:" + ";".join(ev["responses"][:3]))
    if ev.get("toasts"):
        parts.append("页面提示:" + " | ".join(ev["toasts"]))
    if ev.get("downloads"):
        parts.append("下载事件:" + ",".join(ev["downloads"]))
    return " ".join(parts)


def _confirm_export_modal(page, spec: PageSpec, range_days: int = 0) -> dict:
    """导出确认框：等它出现 →（可选）填日期范围 → 点「开始下载」。

    返回本次确认框的现场证据（日期输入框数量、目标值 vs 读回值）。

    2026-10-14 三次实测：点了「开始下载」仍 120s 无下载。日期范围改为
    **显式填写**（fill → JS 注入 → 日历点选，读回值留证）——只读日历控件
    不再是「不碰」的理由；按钮禁用（日期未过校验的典型症状）在点击前拦截。
    """
    evidence = {"date_inputs_found": None, "dates": []}
    try:
        page.get_by_text("开始下载", exact=False).first.wait_for(
            state="visible", timeout=20_000)
    except Exception:
        _dump_diagnostics(page, spec, "导出确认框未出现（点了导出按钮但没弹框）")
        raise ExportModalMissing(f"{spec.name} 页导出确认框未出现")

    if range_days > 0:
        start, end = _modal_range_for(range_days)
        inputs = _modal_date_inputs(page)
        evidence["date_inputs_found"] = len(inputs)
        if len(inputs) >= 2:
            # 两个框 = 起/止（值为空或单日期都按起止处理）
            v0 = _fill_date_input(page, inputs[0], start)
            v1 = _fill_date_input(page, inputs[1], end)
            evidence["dates"] = [f"起始 目标{start}→读回{v0 or '空'}",
                                 f"截止 目标{end}→读回{v1 or '空'}"]
            page.wait_for_timeout(1500)   # 等「作品总数」之类联动刷新
            print(f"   📅 确认框日期范围: {v0} ~ {v1}（目标 {start} ~ {end}）")
        elif len(inputs) == 1:
            # 单个区间输入框（改版形态）：整体灌 "start ~ end"，读回留证
            target = f"{start} ~ {end}"
            try:
                inputs[0].fill(target)
            except Exception:
                try:
                    inputs[0].evaluate(JS_SET_VALUE, target)
                except Exception:
                    pass
            v = _read_input(inputs[0])
            evidence["dates"] = [f"单框 目标{target}→读回{v or '空'}"]
            page.wait_for_timeout(1500)
        else:
            evidence["dates"] = [
                f"未找到日期输入框（目标 {start} ~ {end}），按后台默认范围导出"]

    btn = page.get_by_text("开始下载", exact=False).first
    if _is_confirm_disabled(btn):
        _dump_diagnostics(page, spec,
                          "「开始下载」按钮禁用（日期范围未通过校验？）",
                          evidence=evidence)
        raise ExportModalMissing(
            f"{spec.name} 页「开始下载」按钮禁用；日期框: {evidence.get('dates')}")
    btn.click()
    print("   🖱️ 已点击「开始下载」")
    return evidence


def _collect_one(page, context, spec: PageSpec, nav_timeout_ms: int,
                 download_timeout_ms: int, days: int = 1,
                 range_days: int = 0) -> tuple[Path, str | None]:
    """导航到 spec 页面并触发导出，返回 (下载的临时文件路径, 命中的时间范围文案)。

    失败抛 ExportButtonMissing / ExportModalMissing / ExportAsyncRequired /
    LoginExpired / 其他异常（含超时）——异常一律挂 evidence 属性（现场证据），
    由 run() 写进问题消息，超时不再是「干等 120s 什么都没留下」。
    """
    print(f"   🌐 打开 {spec.name} 页面: {spec.url}")
    _assert_logged_in(page, spec, nav_timeout_ms)
    _dismiss_popups(page)

    if spec.nav_text:
        # 深链白屏页（growth 实测）：SPA 不认直达 URL，内容区整个空白——
        # 改点左侧导航链接走客户端路由，页面才真正渲染
        print(f"   🧭 改走左侧导航进入: {spec.nav_text}")
        if not _try_click(page, (spec.nav_text,)):
            print(f"   ⚠️ 未找到导航项「{spec.nav_text}」，沿用直达 URL 的结果")
    if spec.extra_wait_ms:
        # 粉丝页图表渲染慢（深链时 3 秒内容区还是空白），多等一会儿再找导出控件
        page.wait_for_timeout(spec.extra_wait_ms)
    _dismiss_popups(page)   # 导航后营销弹窗可能再次出现

    range_hit = None
    if spec.range_mode == "dynamic":
        range_hit = _click_first(page, _range_texts_for(days))   # 找到了就点，找不到不阻断
        if range_hit:
            print(f"   📆 已选择时间范围: {range_hit}")
            page.wait_for_timeout(1000)

    evidence = _watch_export_events(page, context)
    tmp_dir = DIAG_DIR / "downloads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    download = None
    try:
        with page.expect_download(timeout=download_timeout_ms) as dl_info:
            if not _try_click(page, spec.export_texts):
                raise ExportButtonMissing(f"{spec.name} 页未找到导出按钮")
            if spec.confirm_modal:
                evidence.update(_confirm_export_modal(page, spec,
                                                     range_days=range_days))
            _observe_after_click(page, evidence)
        download = dl_info.value
    except Exception as e:
        try:
            e.evidence = evidence      # type: ignore[attr-defined]
        except Exception:
            pass
        if isinstance(e, TimeoutError) and evidence.get("download_objs"):
            # 下载是从新标签页触发的：page 级 expect_download 等不到，
            # 但 context 级监听已经收到——直接用，照样落袋为安。
            # 只认 TimeoutError：禁用/异步/按钮缺失这类「导出根本没触发」的
            # 失败必须响亮，绝不拿上下文里的下载事件蒙混过关。
            download = evidence["download_objs"][0]
            print("   🔀 下载由新标签页触发，已从 context 监听回收")
        else:
            _dump_diagnostics(page, spec,
                              f"导出等待失败: {type(e).__name__}: {e}",
                              evidence=evidence)
            raise

    name = _safe_name(getattr(download, "suggested_filename", "") or f"{spec.name}.xlsx")
    dest = tmp_dir / name
    download.save_as(str(dest))
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"{spec.name} 下载文件为空: {dest}")
    print(f"   📥 已下载: {name}（{dest.stat().st_size // 1024} KB）")
    return dest, range_hit


def _save_to_inbox(tmp_path: Path, inbox: Path, date_tag: str | None = None) -> dict:
    """把下载文件投进 inbox；同名按 sha256 判重，绝不静默覆盖。

    date_tag：统计日期（YYYY-MM-DD）。下载名本身不含日期时补进去，箱内一眼可辨；
    含日期则不重复加。判重按内容 sha256，与文件名无关。
    """
    inbox.mkdir(parents=True, exist_ok=True)
    name = _dated_name(tmp_path.name, date_tag) if date_tag else tmp_path.name
    target = inbox / name
    new_sha = sha256_of(tmp_path)
    if target.exists():
        if sha256_of(target) == new_sha:
            tmp_path.unlink(missing_ok=True)
            return {"file": name, "action": "duplicate", "sha256": new_sha[:12],
                    "note": "箱内已有同内容文件，丢弃（幂等）"}
        stem, suffix = target.stem, target.suffix
        n = 2
        while (inbox / f"{stem}_{n}{suffix}").exists():
            n += 1
        target = inbox / f"{stem}_{n}{suffix}"
        tmp_path.replace(target)
        return {"file": target.name, "action": "saved", "sha256": new_sha[:12],
                "note": f"同名文件内容已更新，另存为 {target.name}"}
    tmp_path.replace(target)
    return {"file": name, "action": "saved", "sha256": new_sha[:12], "note": ""}


def _notify_summary(result: dict, date_tag: str) -> None:
    """运行回执：成功时汇总投递了什么、统计哪一天（用户要求每次运行都有状态反馈）。"""
    lines = [f"统计日期: {date_tag}（前一天）",
             "作品数据按确认框内显式填写的近 7 天范围导出（窗口内作品每日 upsert 刷新累计值）"]
    for f in result.get("files", []):
        mark = "✅" if f["action"] == "saved" else "♻️"
        note = f.get("note") or ("已投递" if f["action"] == "saved" else "同内容重复，已跳过")
        rng = f"　范围: {f['range']}" if f.get("range") else ""
        lines.append(f"{mark} {f['file']}（{note}{rng}）")
    if not result.get("files"):
        lines.append("（无文件投递）")
    lines.append("perf-ingest 09:10 自动识别入库，看板/调参样本稍后自动更新。")
    send_wxpusher(f"足球自媒体 ✅ 导出回收完成（{date_tag}）", "\n".join(lines))


def run(auth_file: Path = AUTH_FILE, inbox: Path = INBOX, days: int = 1,
        headless: bool = True, nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
        download_timeout_ms: int = DEFAULT_DOWNLOAD_TIMEOUT_MS, dry_run: bool = False,
        perf_range_days: int | None = None,
        playwright_factory=sync_playwright) -> dict:
    """执行一轮回收。返回汇总 dict；ok=False 时调用方应非零退出。

    perf_range_days：覆盖作品数据确认框的日期窗口天数（None=按页面配置默认 7）。
    """
    result = {"ok": False, "status": "unknown", "files": [], "problems": [],
              "days": days, "dry_run": dry_run}
    date_tag = _yesterday_str()
    result["date"] = date_tag
    if not Path(auth_file).exists():
        msg = f"登录态文件不存在: {auth_file}（与 publisher 共用 TOUTIAO_AUTH_FILE）"
        print(f"❌ {msg}")
        result["status"] = "no_auth"
        result["problems"].append(msg)
        send_wxpusher("足球自媒体 ⚠️ 导出回收跳过", f"{msg}；今天请手动导出投递 data/inbox/")
        return result

    launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox",
                   "--disable-dev-shm-usage"]
    with playwright_factory() as p:
        browser = p.chromium.launch(headless=headless, args=launch_args)
        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "zh-CN",
            "storage_state": str(auth_file),
            "permissions": ["clipboard-read", "clipboard-write"],
        }
        if headless:
            ctx_kwargs["user_agent"] = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.accept())

        for spec in PAGES:
            try:
                if dry_run:
                    print(f"   🌐 [dry-run] 仅检测登录态: {spec.url}")
                    _assert_logged_in(page, spec, nav_timeout_ms)
                    print(f"   ✅ [dry-run] {spec.name} 登录态有效")
                    continue
                tmp, range_hit = _collect_one(page, context, spec, nav_timeout_ms,
                                              download_timeout_ms, days=days,
                                              range_days=(perf_range_days if perf_range_days is not None
                                                          else spec.modal_range_days))
                rec = _save_to_inbox(tmp, inbox, date_tag=date_tag)
                rec["range"] = range_hit
                result["files"].append(rec)
            except LoginExpired as e:
                msg = f"头条登录态已过期（{spec.name} 页）：{e}"
                print(f"   ⛔ {msg}")
                result["status"] = "login_expired"
                result["problems"].append(msg)
                send_wxpusher("足球自媒体 ⚠️ 导出回收跳过（登录态过期）",
                              f"{msg}。请重新登录更新 TOUTIAO_AUTH_GZ secret，"
                              f"或今天手动导出投递 data/inbox/")
                break
            except Exception as e:
                msg = f"{spec.name} 页导出失败: {type(e).__name__}: {e}"
                brief = _evidence_brief(getattr(e, "evidence", None))
                if brief:
                    msg = f"{msg}；{brief}"
                print(f"   ❌ {msg}")
                result["status"] = "export_failed"
                result["problems"].append(msg)
                # 一个页面失败不阻断另一个（尽力投递）；但最终 ok=False
        browser.close()

    saved = [f for f in result["files"] if f["action"] == "saved"]
    dups = [f for f in result["files"] if f["action"] == "duplicate"]
    if dry_run and not result["problems"]:
        result["status"] = "ok"
        result["ok"] = True
        return result
    if result["problems"]:
        if result["status"] == "unknown":
            result["status"] = "export_failed"
        send_wxpusher("足球自媒体 ⚠️ 导出回收未完成",
                      f"统计日期: {date_tag}\n状态: {result['status']}\n"
                      f"问题: {'; '.join(result['problems'])}\n"
                      f"已投递: {len(saved)} 个文件；今天请手动导出投递 data/inbox/ 兜底")
    else:
        result["status"] = "ok"
        result["ok"] = True
        _notify_summary(result, date_tag)
    print(f"📦 回收汇总: 投递 {len(saved)} 个（重复 {len(dups)} 个），"
          f"统计 {date_tag}，状态 {result['status']}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="头条后台导出自动回收（投递箱自动投递）")
    ap.add_argument("--days", type=int, default=1,
                    help="导出窗口天数（默认 1 = 统计前一天，与每日 06:00 节奏对齐）")
    ap.add_argument("--perf-range-days", type=int, default=None,
                    help="作品数据确认框日期窗口天数（至少 7；默认按页面配置=7）")
    ap.add_argument("--auth", default=str(AUTH_FILE), help="登录态文件路径")
    ap.add_argument("--inbox", default=str(INBOX), help="投递箱目录")
    ap.add_argument("--headed", action="store_true", help="有头模式（本地调试用）")
    ap.add_argument("--dry-run", action="store_true", help="只检测登录态，不导出")
    ap.add_argument("--nav-timeout", type=int, default=60, help="页面导航超时（秒）")
    ap.add_argument("--download-timeout", type=int, default=120, help="导出等待超时（秒）")
    args = ap.parse_args()

    result = run(
        auth_file=Path(args.auth),
        inbox=Path(args.inbox),
        days=args.days,
        headless=not args.headed,
        nav_timeout_ms=args.nav_timeout * 1000,
        download_timeout_ms=args.download_timeout * 1000,
        dry_run=args.dry_run,
        perf_range_days=args.perf_range_days,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
