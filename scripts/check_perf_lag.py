#!/usr/bin/env python3
"""数据回填滞后监控（R11 内容诊断 2026-10-13「建议7」）。

动机：绩效回填靠人肉 → 数据永远滞后一周，闭环慢一拍。自动化之后还需要**知道它有没有在跑**：
本脚本量化两个滞后量并给出告警，避免「以为在自动同步，其实早就断了」。

指标
  绩效滞后 = 今天 − performance_log.json 里最新一天
  转粉滞后 = 今天 − data/growth/fans_daily.json 里最新一天
  回填覆盖率 = 近 N 天成稿中「已回填绩效」的篇数占比

阈值（可调）
  --perf-lag-max 3   绩效滞后超过 3 天告警（头条后台一般 T+1 可导出）
  --growth-lag-max 7 转粉滞后超过 7 天告警（粉丝分析按周导出也够用）

用法：
  python3 scripts/check_perf_lag.py [--days 14] [--json]
产出：data/audit/perf_lag_<date>.md + perf_lag_latest.json
退出码：0 = 在阈值内；1 = 滞后超阈值（调用方可当告警处理）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import growth_import as gi  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
PERF_LOG = OUTPUT_DIR / "performance_log.json"
CST = timezone(timedelta(hours=8))


def _latest_date(dates) -> str | None:
    valid = sorted(str(d)[:10] for d in dates if str(d or "").strip()
                   and str(d)[:4].isdigit())
    return valid[-1] if valid else None


def lag_days(latest: str | None, today: str) -> int | None:
    """滞后天数；无数据返回 None（= 待接入，不算滞后）。"""
    if not latest:
        return None
    try:
        a = datetime.strptime(latest, "%Y-%m-%d").date()
        b = datetime.strptime(today, "%Y-%m-%d").date()
    except Exception:
        return None
    return max(0, (b - a).days)


def perf_latest() -> str | None:
    if not PERF_LOG.exists():
        return None
    try:
        d = json.loads(PERF_LOG.read_text(encoding="utf-8"))
    except Exception:
        return None
    return _latest_date((v or {}).get("date") for v in (d.get("articles") or {}).values())


def coverage(days: int, today: str) -> dict:
    """近 N 天成稿的绩效回填覆盖率（预测稿不计）。"""
    floor = (datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=days)).isoformat()
    total = filled = 0
    for p in sorted(OUTPUT_DIR.glob("*/metadata.json")):
        d = p.parent.name
        if d < floor or d > today:
            continue
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pred = {t.get("title") for t in meta.get("topics", []) or [] if t.get("_is_prediction")}
        for a in (meta.get("articles") or []):
            if a.get("is_prediction") or a.get("title") in pred:
                continue
            total += 1
            perf = a.get("performance") or {}
            if perf.get("reads") is not None:
                filled += 1
    return {"total": total, "filled": filled,
            "rate": round(filled / total * 100, 1) if total else None}


def report(days: int = 14, perf_lag_max: int = 3, growth_lag_max: int = 7,
           today: str | None = None) -> dict:
    today = today or datetime.now(CST).strftime("%Y-%m-%d")
    p_latest = perf_latest()
    g_latest = _latest_date(gi.daily_new_followers().keys())
    p_lag = lag_days(p_latest, today)
    g_lag = lag_days(g_latest, today)
    cov = coverage(days, today)

    problems = []
    if p_lag is not None and p_lag > perf_lag_max:
        problems.append(f"绩效滞后 {p_lag} 天（阈值 {perf_lag_max}）：最新 {p_latest}")
    if g_lag is not None and g_lag > growth_lag_max:
        problems.append(f"转粉滞后 {g_lag} 天（阈值 {growth_lag_max}）：最新 {g_latest}")
    if p_lag is None:
        problems.append("绩效台账为空（从未回填）")
    if cov["rate"] is not None and cov["rate"] < 50:
        problems.append(f"近 {days} 天成稿回填覆盖率仅 {cov['rate']}%（{cov['filled']}/{cov['total']}）")

    notices = []
    if g_lag is None:
        notices.append("转粉通道尚未接入（请在头条后台导出「粉丝分析」后放入 data/inbox/）")

    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"), "today": today,
            "ok": not problems,
            "绩效最新": p_latest, "绩效滞后天数": p_lag, "绩效滞后阈值": perf_lag_max,
            "转粉最新": g_latest, "转粉滞后天数": g_lag, "转粉滞后阈值": growth_lag_max,
            "回填覆盖率": cov, "problems": problems, "notices": notices}


def render_md(rep: dict) -> str:
    L = ["# 数据回填滞后监控（建议7）", "",
         f"- 生成时间：{rep['generated_at']}　今天：{rep['today']}",
         f"- 结论：**{'正常 ✅' if rep['ok'] else '需关注 ❌'}**", "",
         f"- 绩效台账最新：{rep['绩效最新'] or '（空）'}　滞后："
         f"{rep['绩效滞后天数'] if rep['绩效滞后天数'] is not None else '—'} 天"
         f"（阈值 {rep['绩效滞后阈值']}）",
         f"- 转粉通道最新：{rep['转粉最新'] or '（待接入）'}　滞后："
         f"{rep['转粉滞后天数'] if rep['转粉滞后天数'] is not None else '—'} 天"
         f"（阈值 {rep['转粉滞后阈值']}）",
         f"- 近 {rep['回填覆盖率']['total']} 篇成稿回填覆盖率："
         f"{rep['回填覆盖率']['rate']}%" if rep["回填覆盖率"]["rate"] is not None
         else "- 近窗口无成稿，覆盖率 —", ""]
    for p in rep["problems"]:
        L.append(f"> ❌ {p}")
    for n in rep["notices"]:
        L.append(f"> ℹ️ {n}")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="数据回填滞后监控（建议7）")
    ap.add_argument("--days", type=int, default=14, help="回填覆盖率窗口（默认 14 天）")
    ap.add_argument("--perf-lag-max", type=int, default=3)
    ap.add_argument("--growth-lag-max", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true", help="不写 audit 产物")
    args = ap.parse_args()

    rep = report(args.days, args.perf_lag_max, args.growth_lag_max)
    if not args.no_write:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(CST).strftime("%Y-%m-%d")
        (AUDIT_DIR / f"perf_lag_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
        (AUDIT_DIR / "perf_lag_latest.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
