#!/usr/bin/env python3
"""周更一键闭环（R11 内容诊断 2026-10-13，建议5 + 建议7 的收口入口）。

用户的诉求原话：「我直接提交文件给你，剩下的你帮我搞定」。
所以本脚本是**唯一的周更入口**——用户只需要把头条后台导出的文件交出来
（丢进 `data/inbox/`，或 `--from <路径>` 指过来），剩下全自动：

  1. 收文件    → 从指定路径/目录收集到 `data/inbox/`（按 sha256 去重，不覆盖）
  2. 消费入库  → `ingest_exports.run()`：类型识别（作品数据 / 粉丝分析）→ 落台账 + 归档
  3. 滞后监控  → `check_perf_lag.report()`：绩效滞后 / 转粉滞后 / 回填覆盖率
  4. 刷新看板  → `build_metrics_dashboard`：dashboard_<date>.md/.html + latest.json
  5. 调参建议  → `ratio_tuner.compute_suggestion()`（**只算不写**，配置改动交人工）
  6. 汇总报告  → `data/audit/weekly_review_<date>.md` 一份人话摘要

纪律（与全仓一致）：
  - 识别不出类型的文件**原样留在 inbox**、不猜不导入，并在报告里显式点名；
  - 缺失指标一律「待接入」，**绝不用 0 冒充**；跨天区间净增不算单日涨粉（跨度守卫）；
  - 调参只出建议、**不自动改配置**（配比变更要人看）；
  - `--commit` 才提交推送，默认只落本地；
  - 任何一步失败不阻断其余步骤，失败项在摘要里列出（本脚本是收口工具，不是闸门）。

用法：
  python3 scripts/weekly_review.py                     # 消费 data/inbox/
  python3 scripts/weekly_review.py --from ~/下载        # 先把该目录的导出文件收进来
  python3 scripts/weekly_review.py --from ~/x.xlsx     # 单个文件也行
  python3 scripts/weekly_review.py --dry-run           # 只识别、不动库、不写产物
  python3 scripts/weekly_review.py --commit            # 顺带 git 提交推送数据变更
产出：data/audit/weekly_review_<date>.md（+ 看板产物、滞后台账）
退出码：0 = 全部步骤正常；1 = 有步骤失败或文件没识别出来（需人工看一眼）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import check_perf_lag as cpl              # noqa: E402
import ingest_exports as ie               # noqa: E402
import growth_import as gi                # noqa: E402

CST = timezone(timedelta(hours=8))
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
INBOX = ie.INBOX
SSH_KEY = os.environ.get("GIT_SSH_KEY", "/home/chenwu/.ssh/id_ed25519")


def now_cst() -> datetime:
    return datetime.now(CST)


def collect_from(src: Path | str, *, dry_run: bool = False) -> list[str]:
    """把 `--from` 指的文件/目录里的导出文件收进 data/inbox/（不改名、不覆盖已有同名同内容）。"""
    p = Path(src).expanduser()
    if not p.exists():
        return []
    files = ([q for q in sorted(p.iterdir()) if q.is_file() and q.suffix.lower() in ie.EXTS]
             if p.is_dir() else [p])
    INBOX.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for f in files:
        if f.suffix.lower() not in ie.EXTS or f.name.startswith("."):
            continue
        dest = INBOX / f.name
        if dest.exists() and ie.sha256_of(dest) == ie.sha256_of(f):
            continue                       # 同内容已在箱里 → 跳过（幂等）
        if not dry_run:
            if dest.exists():              # 同名不同内容 → 加哈希后缀，绝不覆盖
                dest = INBOX / f"{f.stem}_{ie.sha256_of(f)[:8]}{f.suffix}"
            shutil.copy2(f, dest)
        copied.append(dest.name)
    return copied


def _safe(label: str, fn, failures: list):
    """跑一步；异常不阻断后续，记进 failures。"""
    try:
        return fn()
    except Exception as e:  # pragma: no cover - 环境相关
        failures.append(f"{label} 失败：{type(e).__name__}: {e}")
        return None


def run(days: int = 30, *, src: Path | str | None = None, dry_run: bool = False,
        write_dashboard: bool = True, tuner: bool = True) -> dict:
    """跑完整周更闭环，返回结构化结果。"""
    failures: list[str] = []
    today = now_cst().strftime("%Y-%m-%d")
    out: dict = {"generated_at": now_cst().strftime("%Y-%m-%d %H:%M"), "dry_run": dry_run,
                 "collected": [], "failures": failures}

    # 1) 收文件（--from）
    if src:
        out["collected"] = _safe("收集文件", lambda: collect_from(src, dry_run=dry_run),
                                 failures) or []

    # 2) 消费投递箱
    out["inbox"] = _safe("消费投递箱", lambda: ie.run(dry_run=dry_run), failures)

    # 3) 滞后监控（不写自己的 audit，由本脚本统一落一份报告）
    out["lag"] = _safe("滞后监控", lambda: cpl.report(), failures)

    # 4) 看板
    if write_dashboard:
        out["dashboard"] = _safe("刷新看板", lambda: _build_dashboard(days, dry_run=dry_run),
                                 failures)

    # 5) 调参建议（只算不写）
    if tuner:
        out["tuner"] = _safe("调参建议", lambda: _tune_suggestion(), failures)

    # 6) 落汇总报告
    if not dry_run:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        p = AUDIT_DIR / f"weekly_review_{today}.md"
        p.write_text(render_md(out), encoding="utf-8")
        try:
            out["report"] = str(p.relative_to(PROJECT_ROOT))
        except ValueError:      # 审计目录被指到仓库外（测试/自定义）→ 记绝对路径
            out["report"] = str(p)

    out["ok"] = not failures and _inbox_clean(out.get("inbox"))
    return out


def _inbox_clean(inbox: dict | None) -> bool:
    """inbox 里还有 unknown/failed/unreadable → 不算干净（需人工看一眼）。"""
    if not inbox:
        return True
    c = inbox.get("counts") or {}
    return not (c.get("unknown") or c.get("failed") or c.get("unreadable"))


def _build_dashboard(days: int, *, dry_run: bool) -> dict:
    import build_metrics_dashboard as bmd
    data = bmd.collect(days)
    rep = bmd.build_report(data, bmd.compliance_block(), days)
    if not dry_run:
        out = Path(bmd.DEFAULT_OUT)
        out.mkdir(parents=True, exist_ok=True)
        stamp = now_cst().strftime("%Y-%m-%d")
        (out / f"dashboard_{stamp}.md").write_text(bmd.render_markdown(rep), encoding="utf-8")
        (out / f"dashboard_{stamp}.html").write_text(bmd.render_html(rep), encoding="utf-8")
        (out / "latest.json").write_text(
            __import__("json").dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    return rep


def _tune_suggestion() -> dict:
    """只调用纯计算部分——**绝不**调 ratio_tuner.main()（它会写配置）。"""
    import ratio_tuner as rt
    cfg = rt.load_config()
    window = (cfg.get("调整纪律", {}) or {}).get("滚动窗口天数", 14)
    obs = rt.collect_observations(window)
    return rt.compute_suggestion(cfg, obs)


def git_commit_push(message: str, *, push: bool = True) -> dict:
    """提交推送数据变更。顺序：add -A → commit → fetch → 同步 → push（**不 rebase**）。"""
    def g(*args, check=False):
        return subprocess.run(["git", "-c", f"core.sshCommand=ssh -i {SSH_KEY} -o "
                               "StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null", *args],
                              cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=check)
    res: dict = {"committed": False, "pushed": False, "detail": []}
    g("add", "-A")
    if g("diff", "--cached", "--quiet").returncode == 0:
        res["detail"].append("无变更，跳过提交")
        return res
    c = g("commit", "-m", message)
    if c.returncode != 0:
        res["detail"].append(f"提交失败：{(c.stderr or c.stdout).strip()[:200]}")
        return res
    res["committed"] = True
    if not push:
        res["detail"].append("已提交（未推送）")
        return res
    g("fetch", "origin")
    behind = g("rev-list", "--count", "HEAD..origin/main").stdout.strip()
    if behind not in ("", "0"):
        m = g("merge", "origin/main", "-m", "chore: 合并远端（周更数据）")
        res["detail"].append(f"远端领先 {behind}，已 merge（不用 rebase）" if m.returncode == 0
                             else f"merge 失败：{(m.stderr or '').strip()[:160]}")
    p = g("push", "origin", "HEAD:main")
    res["pushed"] = p.returncode == 0
    res["detail"].append("已推送" if res["pushed"]
                         else f"推送失败：{(p.stderr or p.stdout).strip()[:200]}")
    return res


def render_md(out: dict) -> str:
    L = [f"# 周更一键闭环报告（{out['generated_at']}）", ""]
    if out.get("dry_run"):
        L.append("> （dry-run：未写库、未落产物）")
        L.append("")
    if out.get("collected"):
        L.append(f"- 收进 inbox：{'、'.join(out['collected'])}")

    inbox = out.get("inbox") or {}
    files = inbox.get("results") or []
    L.append(f"- 投递箱：{inbox.get('files', 0)} 个文件 → "
             + ("、".join(f"{k} {v}" for k, v in sorted((inbox.get("counts") or {}).items()))
                or "无文件"))
    for r in files:
        bit = f"  - `{r['file']}`（{r.get('kind')}）{r.get('status')}"
        if r.get("imported") is not None:
            bit += f"　匹配 {r.get('matched')} 行 / 回填 {r.get('imported')} 行"
        if r.get("rows") is not None:
            bit += f"　涨粉行 {r.get('rows')}（差分 {r.get('derived')}）"
        L.append(bit)

    lag = out.get("lag") or {}
    if lag:
        L.append("")
        L.append(f"- 绩效：最新 {lag.get('绩效最新') or '（空）'}"
                 f"（滞后 {lag.get('绩效滞后天数')} 天）")
        L.append(f"- 转粉：最新 {lag.get('转粉最新') or '（待接入）'}"
                 f"（滞后 {lag.get('转粉滞后天数')} 天）")
        cov = lag.get("回填覆盖率") or {}
        L.append(f"- 回填覆盖率（近窗口）：{cov.get('rate')}%（{cov.get('filled')}/{cov.get('total')}）")
        for p in lag.get("problems") or []:
            L.append(f"  > ❌ {p}")
        for n in lag.get("notices") or []:
            L.append(f"  > ℹ️ {n}")

    rep = out.get("dashboard") or {}
    g = (rep.get("growth") or {}) if rep else {}
    if g:
        L.append("")
        L.append(f"- 看板：日均涨粉 {g.get('日均涨粉')}（{g.get('涨粉数据来源') or '待接入'}）；"
                 f"篇均阅读 {g.get('篇均阅读')}；整体点击率 {g.get('整体点击率')}")

    sug = out.get("tuner") or {}
    if sug:
        L.append("")
        L.append(f"- 调参建议（只算不写）：样本 {sug.get('样本总量')} 篇，"
                 f"自动执行={sug.get('是否自动执行')}；{sug.get('变更原因')}")
        if sug.get("建议配比"):
            L.append(f"  - 建议配比：{sug.get('建议配比')}")

    if out.get("failures"):
        L.append("")
        for f in out["failures"]:
            L.append(f"> ❌ {f}")

    L.append("")
    L.append(f"**结论：{'全部正常 ✅' if out.get('ok') else '需人工看一眼 ❌'}**")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="周更一键闭环（建议5+7 收口）")
    ap.add_argument("--from", dest="src", help="导出文件或所在目录（会收集进 data/inbox/）")
    ap.add_argument("--days", type=int, default=30, help="看板统计窗口（默认 30 天）")
    ap.add_argument("--dry-run", action="store_true", help="只识别，不动库、不写产物")
    ap.add_argument("--no-dashboard", action="store_true", help="跳过看板刷新")
    ap.add_argument("--no-tuner", action="store_true", help="跳过调参建议")
    ap.add_argument("--commit", action="store_true", help="顺带提交数据变更")
    ap.add_argument("--no-push", action="store_true", help="只提交不推送")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    out = run(args.days, src=args.src, dry_run=args.dry_run,
              write_dashboard=not args.no_dashboard, tuner=not args.no_tuner)

    if args.commit and not args.dry_run:
        stamp = now_cst().strftime("%Y-%m-%d")
        git_res = git_commit_push(f"chore(weekly-review): 周更数据入库与滞后监控 {stamp}",
                                  push=not args.no_push)
        out["git"] = git_res
        for d in git_res["detail"]:
            print(f"[git] {d}")

    if args.json:
        print(__import__("json").dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_md(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
