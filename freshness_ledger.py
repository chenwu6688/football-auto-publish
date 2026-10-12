#!/usr/bin/env python3
"""时效闸门台账（计划 4.1 / 7.2）。

计划 7.2 验收：**连续 14 天无旧闻新发类内容进入发布队列**。
计划 4.1 对策：**时效闸门——超 72 小时素材不得进新闻流**。

闸门本身在 ``utils.filter_fresh_news`` 实现；本模块是它的**可审计台账**：
每次闸门运行追加一条记录（检查数 / 剔除数 / 无法判定数 / 保留项最大时效），
供 ``scripts/verify_publish_gate.py`` 复核「连续 14 天无旧闻新发」。

纪律：
  - 无法判定发布时间的素材由闸门**保守保留**（计入 unknown），本模块如实记录，
    不把 unknown 当 0、也不假装闸门拦住了它；
  - 保留项若出现「已知时效 > 上限」（闸门本不该发生）→ 记为 ``over_limit`` 违规，供验收硬拦；
  - 台账写入失败一律不抛异常（绝不阻塞采集主流程）。

命令行：
  python3 freshness_ledger.py --summary
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LEDGER_PATH = PROJECT_ROOT / "data" / "freshness_ledger.json"

KEEP_DAYS = 30
CST = timezone(timedelta(hours=8))


def _now(date=None):
    if isinstance(date, datetime):
        return date
    return datetime.now(CST)


def _load():
    try:
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"records": []}


def _save(data):
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _prune(data, today=None):
    today = _now(today)
    floor = (today - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    data["records"] = [r for r in data.get("records", []) if (r.get("date") or "") >= floor]
    return data


def note(label="news", checked=0, dropped_ages=(), unknown=0, kept_ages=(), max_hours=72, date=None):
    """追加一条闸门运行记录。返回写入的记录 dict（失败也返回，不抛异常）。"""
    now = _now(date)
    dropped_ages = [a for a in (dropped_ages or []) if isinstance(a, (int, float))]
    kept_ages = [a for a in (kept_ages or []) if isinstance(a, (int, float))]
    over_limit = [a for a in kept_ages if a > max_hours]
    rec = {
        "ts": now.strftime("%Y-%m-%d %H:%M"),
        "date": now.strftime("%Y-%m-%d"),
        "label": label or "news",
        "checked": int(checked or 0),
        "dropped": len(dropped_ages),
        "dropped_max_age": round(max(dropped_ages), 1) if dropped_ages else None,
        "unknown": int(unknown or 0),
        "max_kept_age": round(max(kept_ages), 1) if kept_ages else None,
        "max_hours": max_hours,
        "over_limit": [round(a, 1) for a in over_limit],
    }
    data = _prune(_load(), now)
    data.setdefault("records", []).append(rec)
    _save(data)
    return rec


def load():
    return _load().get("records", [])


def reset():
    _save({"records": []})


def _days_back(days, today=None):
    today = _now(today)
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def summary(days=14, today=None):
    """近 N 天台账汇总。"""
    recs = load()
    floor = (today and _now(today) or datetime.now(CST)) - timedelta(days=days)
    floor_s = floor.strftime("%Y-%m-%d")
    recent = [r for r in recs if (r.get("date") or "") >= floor_s]
    dates = sorted({r["date"] for r in recent})
    checked = sum(r.get("checked", 0) for r in recent)
    dropped = sum(r.get("dropped", 0) for r in recent)
    unknown = sum(r.get("unknown", 0) for r in recent)
    over = [r for r in recent if r.get("over_limit")]
    return {"days": days, "runs": len(recent), "days_covered": len(dates),
            "dates": dates, "checked": checked, "dropped": dropped, "unknown": unknown,
            "over_limit_runs": len(over), "over_limit": over,
            "missing_days": [d for d in _days_back(days, today) if d not in dates]}


def violations(days=14, today=None):
    """近 N 天「已知时效 > 上限仍保留」的违规记录（应为空）。"""
    return [r for r in load()
            if r.get("over_limit")
            and (r.get("date") or "") >= ((_now(today) - timedelta(days=days)).strftime("%Y-%m-%d"))]


def _cli():
    ap = argparse.ArgumentParser(description="时效闸门台账（计划 4.1 / 7.2）")
    ap.add_argument("--summary", action="store_true", help="打印近 14 天汇总")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    s = summary(args.days)
    print(f"台账记录 {len(load())} 条；近 {args.days} 天：运行 {s['runs']} 次，"
          f"覆盖 {s['days_covered']}/{args.days} 天，检查 {s['checked']}，"
          f"剔除 {s['dropped']}，未知 {s['unknown']}，超限违规 {s['over_limit_runs']}")
    if s["missing_days"]:
        print(f"  缺记录日：{s['missing_days']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
