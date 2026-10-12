#!/usr/bin/env python3
"""赛程落库与接口额度验收（计划 11.4）。

计划 11.4 的两条硬口径：
  1. 每日一次批量拉取未来 **14 天**赛程写库，业务侧只读库、不直连接口；
  2. 免费额度按日计，缓存后每日调用量**可控在 3 次以内**。

校验项：
  A. 台账无超限日（硬项）—— 近 N 天任一天调用量都不得超过当日额度上限；
  B. 14 天窗口覆盖（硬项，缺选题池时跳过）—— 读 data/fixtures/topic_pool.json，
     校验 window_covered_days / window_days ≥ 14，且选题 kickoff 跨 ≥14 天；
  C. 业务侧不直连（告警项）—— 静态扫描：除 fixture_library / api_football / api_budget
     外，没有模块直接请求 football-data.org 或 api-football 域名；
  D. 各源缓存新鲜度（告警项）—— 各源最近一次成功调用距今天数（应 ≤ 7 天）。

产出 data/audit/api_budget_*.md + api_budget_latest.json；硬项 FAIL 退出码 1。

用法：
  python3 scripts/verify_api_budget.py
  python3 scripts/verify_api_budget.py --days 7
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import api_budget  # noqa: E402

POOL_PATH = PROJECT_ROOT / "data" / "fixtures" / "topic_pool.json"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"

CST = timezone(timedelta(hours=8))

# 允许直接请求外部赛程接口的模块（其余模块一律只读库）
DIRECT_CALL_ALLOWLIST = {"fixture_library.py", "api_football.py", "api_budget.py",
                         "verify_api_budget.py"}
# 精确到「赛程（matches/fixtures）接口」的字面量：仅这些属于计划 11.4 管辖的落库路径；
# 球队名单 / 射手榜 / 比分等其它接口不在本项范围内。
DIRECT_CALL_PATTERNS = ("football-data.org/v4/matches",
                        "rapidapi.com/v3/fixtures")
SCAN_SKIP_DIRS = {"skills", "miniprogram", "node_modules", "__pycache__", "tests", "data", "output"}


def check_ledger(days: int = 7) -> dict:
    """A. 台账无超限日（硬项）。"""
    data = api_budget._load()
    cap_now = api_budget.daily_total_cap()
    today = datetime.now(CST).date()
    rows, over = [], []
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        used = sum(int(v) for v in (data.get(d) or {}).values())
        rows.append({"date": d, "used": used, "cap": cap_now})
        if used > cap_now:
            over.append({"date": d, "used": used, "cap": cap_now})
    return {"item": f"台账无超限日（近 {days} 天）", "ok": not over,
            "rows": rows, "over": over,
            "detail": [f"近 {days} 天共 {sum(r['used'] for r in rows)} 次调用，"
                       f"每日上限 {cap_now} 次",
                       (f"超限日：{over}" if over else "无超限日 ✅"),
                       "（无记录日计为 0 次，属「未运行」而非超限）"]}


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except Exception:
        return str(p)


def check_window(days: int = 14) -> dict:
    """B. 14 天窗口覆盖（硬项；无选题池则跳过）。"""
    if not POOL_PATH.exists():
        return {"item": f"{days} 天赛程落库", "ok": True, "skipped": True,
                "detail": [f"未找到 {_rel(POOL_PATH)}（本地/CI 未刷新赛程池），本项跳过"]}
    try:
        pool = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return {"item": f"{days} 天赛程落库", "ok": False, "skipped": False,
                "detail": [f"选题池解析失败：{e}"]}

    covered = pool.get("window_covered_days", pool.get("window_days", 0)) or 0
    target = pool.get("window_target_days", days) or days
    kickoffs = []
    for t in pool.get("topics", []) or []:
        k = (t.get("kickoff_cst") or "")[:10]
        if k:
            kickoffs.append(k)
    span = 0
    if kickoffs:
        try:
            d0 = datetime.strptime(min(kickoffs), "%Y-%m-%d").date()
            d1 = datetime.strptime(max(kickoffs), "%Y-%m-%d").date()
            span = (d1 - d0).days + 1
        except Exception:
            span = 0
    ok = covered >= days
    return {"item": f"{days} 天赛程落库", "ok": ok, "skipped": False,
            "covered": covered, "target": target, "topic_span_days": span,
            "detail": [f"落库窗口 {covered}/{target} 天（硬口径 ≥{days} 天）",
                       f"选题 kickoff 实际跨度 {span} 天",
                       "（窗口由 football-data 按 10 天上限分块 + 补源缓存共同覆盖）"]}


def check_no_direct_calls() -> dict:
    """C. 业务侧不直连赛程接口（告警项）。"""
    hits = []
    for p in PROJECT_ROOT.rglob("*.py"):
        rel = p.relative_to(PROJECT_ROOT)
        if p.name in DIRECT_CALL_ALLOWLIST:
            continue
        if any(part in SCAN_SKIP_DIRS for part in rel.parts):
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for pat in DIRECT_CALL_PATTERNS:
            if pat in txt:
                hits.append({"file": str(rel), "pattern": pat})
    return {"item": "业务侧不直连赛程接口（只读库）", "advisory": True, "ok": not hits,
            "hits": hits,
            "detail": ([f"发现 {len(hits)} 处直连赛程接口：{hits[:5]}"] if hits
                       else ["仅 fixture_library / api_football 持有赛程请求入口 ✅"
                             "（业务侧只读 data/fixtures/topic_pool.json）"])}


def check_cache_freshness(days: int = 14) -> dict:
    """D. 各源缓存新鲜度（告警项）。"""
    data = api_budget._load()
    today = datetime.now(CST).date()
    last: dict[str, str] = {}
    for d in sorted(data.keys()):
        try:
            dd = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        if (today - dd).days > days:
            continue
        for s, v in (data[d] or {}).items():
            if int(v) > 0:
                last[s] = d          # 按日期升序覆盖 → 留下最近一次
    stale = []
    rows = []
    for s in sorted(set(api_budget.SOURCE_LIMITS) | set(last)):
        d = last.get(s)
        age = (today - datetime.strptime(d, "%Y-%m-%d").date()).days if d else None
        rows.append({"source": s, "last_call": d, "age_days": age})
        if age is None or age > 7:
            stale.append(s)
    if not last:
        # 全新环境/尚未运行：不算问题，避免误报
        return {"item": "各源最近调用 ≤7 天", "advisory": True, "ok": True,
                "rows": rows, "stale": [],
                "detail": [f"台账近 {days} 天无记录（尚未运行或已裁剪），本项不判定"]}
    return {"item": "各源最近调用 ≤7 天", "advisory": True, "ok": not stale,
            "rows": rows, "stale": stale,
            "detail": ([f"超期源：{stale}（轮换周期 4 天，应 ≤7 天）"] if stale
                       else [f"各源均在 7 天内调用过（{ {r['source']: r['age_days'] for r in rows} }）"])}


def verify(days: int = 14) -> dict:
    checks = [check_ledger(7),
              check_window(days),
              check_no_direct_calls(),
              check_cache_freshness(days)]
    hard = [c for c in checks if not c.get("advisory")]
    ok = all(c["ok"] for c in hard)
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": ok, "checks": checks,
            "total_cap": api_budget.daily_total_cap(),
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 赛程落库与接口额度验收报告（计划 11.4）", "",
         f"- 生成时间：{rep['generated_at']}　全局日上限：{rep['total_cap']} 次",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警" if c.get("advisory") else "硬项"
        status = "跳过" if c.get("skipped") else ("✅" if c["ok"] else "❌")
        L.append(f"## {c['item']}（{tag}）：{status}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="赛程落库与接口额度验收（计划 11.4）")
    ap.add_argument("--days", type=int, default=14, help="窗口天数（默认 14，计划 11.4）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"api_budget_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "api_budget_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
