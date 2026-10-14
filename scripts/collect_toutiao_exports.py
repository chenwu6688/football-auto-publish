#!/usr/bin/env python3
"""头条后台导出自动回收 —— 给 perf-ingest 投递箱当「自动投递员」。

问题：绩效/转粉数据回流全靠每周人工「登录头条后台 → 点导出 → 下载 → 丢 data/inbox/」，
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
  3. 同名不覆盖——箱内已有同名文件时按 sha256 判重：内容相同 → 丢弃（真重复），
     内容不同（数据已更新的再次导出）→ 加 _2 后缀，绝不静默覆盖；
  4. 选择器集中在本文件 PAGES 配置——头条后台改版时只需改这一处；
  5. 登录态失效与 publisher 同判据（URL 含 /auth/ 或 /login）——复用同一检测口径。

频率：每周一次全量导出（CI 周一 09:00 CST，见 .github/workflows/toutiao-collector.yml），
不做高频轮询（风控克制）。

用法：
  python3 scripts/collect_toutiao_exports.py                 # CI 模式（headless，默认 7 天）
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
    range_texts: tuple        # 时间范围候选文案（找到了就点，找不到不阻断）


PAGES = (
    PageSpec(
        name="perf",
        url="https://mp.toutiao.com/profile_v4/analysis/works-single/article",
        export_texts=("导出数据", "导出"),
        range_texts=("近7天", "最近7天", "7天"),
    ),
    PageSpec(
        name="growth",
        url="https://mp.toutiao.com/profile_v4/analysis/fans",
        export_texts=("导出数据", "导出"),
        range_texts=(),
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


def _dump_diagnostics(page, spec: PageSpec, note: str) -> str:
    """失败现场留证：页面截图 + 可见按钮文本清单（供改版后修选择器）。"""
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    shot = DIAG_DIR / f"{spec.name}.png"
    try:
        page.screenshot(path=str(shot), full_page=False)
    except Exception:
        shot = None
    buttons = []
    try:
        for el in page.query_selector_all("button"):
            try:
                t = (el.inner_text() or "").strip()
                if t:
                    buttons.append(t[:40])
            except Exception:
                continue
    except Exception:
        pass
    diag = DIAG_DIR / f"{spec.name}-diagnostics.txt"
    diag.write_text(
        f"页面: {spec.name} ({spec.url})\n原因: {note}\nURL: {getattr(page, 'url', '?')}\n"
        f"可见按钮: {buttons}\n截图: {shot}\n",
        encoding="utf-8")
    print(f"   🔍 诊断已留存: {diag}" + (f" + {shot}" if shot else ""))
    return str(diag)


def _collect_one(page, spec: PageSpec, nav_timeout_ms: int,
                 download_timeout_ms: int) -> Path:
    """导航到 spec 页面并触发导出，返回下载的临时文件路径。

    失败抛 ExportButtonMissing / LoginExpired / 其他异常（含超时）。
    """
    print(f"   🌐 打开 {spec.name} 页面: {spec.url}")
    page.goto(spec.url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
    page.wait_for_timeout(3000)
    url = (page.url or "").lower()
    if any(m in url for m in LOGIN_MARKERS):
        raise LoginExpired(f"{spec.name} 页面跳转到登录页（{url[:80]}）")

    _try_click(page, spec.range_texts)   # 尽力命中导出窗口；找不到不阻断
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
    return dest


def _save_to_inbox(tmp_path: Path, inbox: Path) -> dict:
    """把下载文件投进 inbox；同名按 sha256 判重，绝不静默覆盖。"""
    inbox.mkdir(parents=True, exist_ok=True)
    name = tmp_path.name
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


def run(auth_file: Path = AUTH_FILE, inbox: Path = INBOX, days: int = 7,
        headless: bool = True, nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
        download_timeout_ms: int = DEFAULT_DOWNLOAD_TIMEOUT_MS, dry_run: bool = False,
        playwright_factory=sync_playwright) -> dict:
    """执行一轮回收。返回汇总 dict；ok=False 时调用方应非零退出。"""
    result = {"ok": False, "status": "unknown", "files": [], "problems": [],
              "days": days, "dry_run": dry_run}
    if not Path(auth_file).exists():
        msg = f"登录态文件不存在: {auth_file}（与 publisher 共用 TOUTIAO_AUTH_FILE）"
        print(f"❌ {msg}")
        result["status"] = "no_auth"
        result["problems"].append(msg)
        send_wxpusher("足球自媒体 ⚠️ 导出回收跳过", f"{msg}；本周请手动导出投递 data/inbox/")
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
                    page.goto(spec.url, wait_until="domcontentloaded",
                              timeout=nav_timeout_ms)
                    url = (page.url or "").lower()
                    if any(m in url for m in LOGIN_MARKERS):
                        raise LoginExpired(f"{spec.name} 页面跳转到登录页")
                    print(f"   ✅ [dry-run] {spec.name} 登录态有效")
                    continue
                tmp = _collect_one(page, spec, nav_timeout_ms, download_timeout_ms)
                result["files"].append(_save_to_inbox(tmp, inbox))
            except LoginExpired as e:
                msg = f"头条登录态已过期（{spec.name} 页）：{e}"
                print(f"   ⛔ {msg}")
                result["status"] = "login_expired"
                result["problems"].append(msg)
                send_wxpusher("足球自媒体 ⚠️ 导出回收跳过（登录态过期）",
                              f"{msg}。请重新登录更新 TOUTIAO_AUTH_GZ secret，"
                              f"或本周手动导出投递 data/inbox/")
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
                      f"状态: {result['status']}\n问题: {'; '.join(result['problems'])}\n"
                      f"已投递: {len(saved)} 个文件；请手动导出投递 data/inbox/ 兜底")
    else:
        result["status"] = "ok"
        result["ok"] = True
    print(f"📦 回收汇总: 投递 {len(saved)} 个（重复 {len(dups)} 个），状态 {result['status']}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="头条后台导出自动回收（投递箱自动投递）")
    ap.add_argument("--days", type=int, default=7, help="导出窗口天数（默认 7）")
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
