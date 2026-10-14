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
       该页**没有**时间范围选择器，下载即全量作品快照（按 slug upsert，每日刷新正好）；
     - perf 页注明「每日 14:00 更新前一日数据」——06:00 导到的前一天是未结算值，
       次日 06:00 的导出会自动覆盖为结算值（upsert 自愈，不必等 14:00 再跑）；
     - growth 页（粉丝数据）主内容区渲染慢（3 秒时还是空白）且压着「被关注自动回复」
       营销弹窗——已加额外等待 + Escape 关弹窗；导出控件文案待首跑成功后才能确认。

频率：**每天 06:00 CST**（CI cron UTC 22:00，见 .github/workflows/toutiao-collector.yml），
默认统计**前一天**（days=1）——与 perf-ingest 09:10 消费、绩效台账按日 upsert
（同 date+slug 覆盖，不重复计数）、转粉日差跨度守卫（相邻登记日间隔 1 天）全部对齐。
频率仍保持克制：每天一次全量导出，不做高频轮询（风控克制）。

用法：
  python3 scripts/collect_toutiao_exports.py                 # CI 模式（headless，默认统计前一天）
  python3 scripts/collect_toutiao_exports.py --headed        # 本地调试（看得见浏览器）
  python3 scripts/collect_toutiao_exports.py --dry-run       # 只检测登录态，不导出
  python3 scripts/collect_toutiao_exports.py --days 14       # 导出窗口（天）
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


@dataclass(frozen=True)
class PageSpec:
    """一个后台数据页的回收配置（改版只改这里）。"""

    name: str                 # perf / growth（与投递箱类型识别对应）
    url: str
    export_texts: tuple       # 导出按钮的候选文案（按序尝试，命中即止）
    range_mode: str = "none"  # none=该页无时间范围选择器 / dynamic=按 --days 动态点
    extra_wait_ms: int = 0    # 登录检测后的额外等待（图表渲染慢的页面用）


PAGES = (
    PageSpec(
        name="perf",
        url="https://mp.toutiao.com/profile_v4/analysis/works-single/article",
        # 2026-10-14 实测：单篇 tab 的导出控件是右上角「⬇ 下载Excel」（<a> 链接，
        # 不是 <button>，所以诊断的按钮清单里根本看不到它）
        export_texts=("下载Excel", "导出数据", "导出Excel", "下载数据", "导出"),
        # 该页无时间范围选择器（实测截图确认）：下载即全量作品快照。
        # 绝不点「昨天」之类——文章列表里标题含「昨天」的作品会被误点导航跑飞。
        range_mode="none",
    ),
    PageSpec(
        name="growth",
        url="https://mp.toutiao.com/profile_v4/analysis/fans",
        # 粉丝页导出控件文案未经真实验证（页面还没渲染出来过）——多放候选，命中即止
        export_texts=("导出数据", "下载Excel", "导出Excel", "下载数据", "导出"),
        range_mode="dynamic",      # 若该页有时间范围选择器，按 --days 点（每日模式点「昨天」）
        extra_wait_ms=7000,        # 实测 3 秒时主内容区还是空白（图表渲染慢）+ 有营销弹窗
    ),
)


class ExportButtonMissing(Exception):
    """页面上找不到导出按钮（后台改版/风控页）。"""


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


def _dump_diagnostics(page, spec: PageSpec, note: str) -> str:
    """失败现场留证：页面截图 + 可见控件文本清单（供改版后修选择器）。

    2026-10-14 教训：首版只抓 `<button>`，而 perf 页导出控件「下载Excel」是 `<a>`
    链接——清单里反而看不到真正的导出按钮。现在 button / a / [role=button] 全抓。
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
    diag.write_text(
        f"页面: {spec.name} ({spec.url})\n原因: {note}\nURL: {getattr(page, 'url', '?')}\n"
        f"可见控件: {controls}\n截图: {shot}\n",
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


def _collect_one(page, spec: PageSpec, nav_timeout_ms: int,
                 download_timeout_ms: int, days: int = 1) -> tuple[Path, str | None]:
    """导航到 spec 页面并触发导出，返回 (下载的临时文件路径, 命中的时间范围文案)。

    失败抛 ExportButtonMissing / LoginExpired / 其他异常（含超时）。
    """
    print(f"   🌐 打开 {spec.name} 页面: {spec.url}")
    _assert_logged_in(page, spec, nav_timeout_ms)
    _dismiss_popups(page)
    if spec.extra_wait_ms:
        # 粉丝页图表渲染慢（3 秒时主内容区还是空白），多等一会儿再找导出控件
        page.wait_for_timeout(spec.extra_wait_ms)

    range_hit = None
    if spec.range_mode == "dynamic":
        range_hit = _click_first(page, _range_texts_for(days))   # 找到了就点，找不到不阻断
        if range_hit:
            print(f"   📆 已选择时间范围: {range_hit}")
            page.wait_for_timeout(1000)

    tmp_dir = DIAG_DIR / "downloads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        with page.expect_download(timeout=download_timeout_ms) as dl_info:
            if not _try_click(page, spec.export_texts):
                raise ExportButtonMissing(f"{spec.name} 页未找到导出按钮")
        download = dl_info.value
    except ExportButtonMissing:
        _dump_diagnostics(page, spec, "未找到导出按钮（后台改版或风控页）")
        raise
    except Exception as e:
        _dump_diagnostics(page, spec, f"导出等待失败: {type(e).__name__}: {e}")
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
    lines = [f"统计日期: {date_tag}（前一天）　导出窗口: {result.get('days', 1)} 天"]
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
        playwright_factory=sync_playwright) -> dict:
    """执行一轮回收。返回汇总 dict；ok=False 时调用方应非零退出。"""
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
                tmp, range_hit = _collect_one(page, spec, nav_timeout_ms,
                                              download_timeout_ms, days=days)
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
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
