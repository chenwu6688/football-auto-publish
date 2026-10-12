#!/usr/bin/env python3
"""一致性校验统计台账（计划 7.3）。

计划 7.3 验收：**一致性校验拦截率可统计且误杀率低于 5%**。
一致性校验本身在 ``orchestrator.check_consistency_plan`` 实现；本模块是它的**可审计台账**：
每次校验（blocking 硬校验 / warn-only 放行）追加一条事件，support：

  - **拦截率** = 硬校验中被打回的条数 / 硬校验总条数（可统计，自动算出）；
  - **误杀率** = 人工抽检标注为「误杀」的条数 / 已标注的打回条数（需人工标注，无标注则
    如实标 ``未接入``，绝不用 0 冒充）。

标注由每周人工抽查（``scripts/calibration-reminder.py`` 流程）写入
``data/consistency_labels.json``：``{"labeled_rejects": n, "false_positive": n}``。

写入失败一律不抛异常（绝不阻塞生成主流程）。

命令行：
  python3 consistency_stats.py --summary
  python3 consistency_stats.py --label 3 --false-positive 0   # 登记人工抽检结果（累加）
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
STATS_PATH = PROJECT_ROOT / "data" / "consistency_stats.json"
LABELS_PATH = PROJECT_ROOT / "data" / "consistency_labels.json"

KEEP_DAYS = 60
CST = timezone(timedelta(hours=8))

#: 计划 7.3 的误杀率上限
MAX_FALSE_POSITIVE_RATE = 0.05


def _now(date=None):
    return date if isinstance(date, datetime) else datetime.now(CST)


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _prune(data, today=None):
    today = _now(today)
    floor = (today - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    data["events"] = [e for e in data.get("events", []) if (e.get("date") or "") >= floor]
    return data


def record(blocking, passed, issues=(), tier=None, date=None, title="", subject=""):
    """追加一条一致性校验事件。返回事件 dict（写入失败也返回，不抛异常）。

    ``title`` / ``subject``：被打回时用于**定位到具体选题**。2026-10-12 的熔断事后
    无法定位是哪一篇触发（台账只存 issues 摘要），故补上标题与主体。
    """
    now = _now(date)
    issues = list(issues or [])
    ev = {"ts": now.strftime("%Y-%m-%d %H:%M"), "date": now.strftime("%Y-%m-%d"),
          "blocking": bool(blocking), "passed": bool(passed),
          "n_issues": len(issues), "issues": issues[:5], "tier": tier or ""}
    if not passed:  # 仅在打回时记录定位信息，通过项不占空间
        ev["title"] = str(title or "")[:80]
        ev["subject"] = str(subject or "")[:80]
    data = _prune(_load(STATS_PATH, {"events": []}), now)
    data.setdefault("events", []).append(ev)
    _save(STATS_PATH, data)
    return ev


def rejects(days=None, today=None):
    """打成回事件列表（新→旧），供熔断证据与人工复盘使用。"""
    evs = [e for e in events() if e.get("blocking") and not e.get("passed")]
    if days:
        floor = (_now(today) - timedelta(days=days)).strftime("%Y-%m-%d")
        evs = [e for e in evs if (e.get("date") or "") >= floor]
    return list(reversed(evs))


def traceability(days=None, today=None):
    """打回可追溯性：returns {rejects, with_title, rate}（无记录 → rate=None）。"""
    rs = rejects(days, today)
    if not rs:
        return {"rejects": 0, "with_title": 0, "rate": None}
    with_title = sum(1 for e in rs if (e.get("title") or "").strip())
    return {"rejects": len(rs), "with_title": with_title, "rate": with_title / len(rs)}


def reset():
    _save(STATS_PATH, {"events": []})


def events():
    return _load(STATS_PATH, {"events": []}).get("events", [])


def summary(days=None, today=None):
    """汇总：拦截率（blocking 打回 / blocking 总数）可自动统计。"""
    evs = events()
    if days:
        floor = (_now(today) - timedelta(days=days)).strftime("%Y-%m-%d")
        evs = [e for e in evs if (e.get("date") or "") >= floor]
    blocking = [e for e in evs if e.get("blocking")]
    rejects = [e for e in blocking if not e.get("passed")]
    warn = [e for e in evs if not e.get("blocking")]
    rate = (len(rejects) / len(blocking)) if blocking else None
    return {"events": len(evs), "blocking_checks": len(blocking), "rejects": len(rejects),
            "warn_only": len(warn),
            "reject_rate": round(rate, 4) if rate is not None else None,
            "reject_rate_display": f"{rate:.1%}" if rate is not None else "未接入",
            "has_records": bool(evs)}


# ------------------------------------------------------------
# 误杀率（人工标注）
# ------------------------------------------------------------
def labels():
    d = _load(LABELS_PATH, {})
    return {"labeled_rejects": int(d.get("labeled_rejects", 0) or 0),
            "false_positive": int(d.get("false_positive", 0) or 0)}


def record_labels(labeled_rejects=0, false_positive=0):
    """累加登记人工抽检结果。返回新的标签聚合。"""
    cur = labels()
    cur["labeled_rejects"] += max(0, int(labeled_rejects or 0))
    cur["false_positive"] += max(0, int(false_positive or 0))
    _save(LABELS_PATH, cur)
    return cur


def false_positive_rate():
    """误杀率 = 误杀 / 已标注打回；无标注 → ``None``（未接入，不冒充 0）。"""
    lab = labels()
    if lab["labeled_rejects"] <= 0:
        return None
    return lab["false_positive"] / lab["labeled_rejects"]


def _cli():
    ap = argparse.ArgumentParser(description="一致性校验统计台账（计划 7.3）")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--label", type=int, default=0, help="本次抽检的打回条数（累加）")
    ap.add_argument("--false-positive", type=int, default=0, help="其中判为误杀的条数（累加）")
    args = ap.parse_args()

    if args.label or args.false_positive:
        lab = record_labels(args.label, args.false_positive)
        print(f"已登记：累计标注打回 {lab['labeled_rejects']}，误杀 {lab['false_positive']}")

    s = summary(args.days)
    fpr = false_positive_rate()
    print(f"拦截率：{s['reject_rate_display']}（blocking {s['blocking_checks']} 打回 {s['rejects']}）")
    print(f"误杀率：{'未接入（需人工标注）' if fpr is None else f'{fpr:.1%}'}"
          f"　上限 {MAX_FALSE_POSITIVE_RATE:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
