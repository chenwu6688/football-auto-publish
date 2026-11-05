#!/usr/bin/env python3
"""闸门分级 v2 首日观察报告（2026-11-05 上线）。

读某天的 `output/<date>/metadata.json`，把 `_gate` 台账折算成四个可判读的指标：

1. **judged 比例**：事实卡是否真的能判。全为 false 说明事实卡供给有问题，
   事实校验形同虚设（不是「全过」，是「没判」）。
2. **legacy 非空但 contradictions 为空**：新口径放过的旧口径误报数。
   预期 = 全部 judged 篇数；若出现「两者都非空」才是真矛盾。
3. **fail_open 篇数**：批内矛盾配额被触发 = 判据可能有系统性误报，需人工看那几篇。
4. **降级事件分布**：看「记分」类是否真的取代了「丢弃」类。

用法：
    PYTHONPATH=. python3 scripts/report_gate_v2.py            # 今天
    PYTHONPATH=. python3 scripts/report_gate_v2.py 2026-11-05
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"


def _load(date: str):
    p = OUTPUT / date / "metadata.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def report(date: str) -> int:
    meta = _load(date)
    if meta is None:
        print(f"⚠️ 没有 {date} 的 metadata.json（批次未跑或尚未落盘）")
        return 2

    arts = [a for a in (meta.get("articles") or []) if isinstance(a, dict)]
    if not arts:
        print(f"⚠️ {date} 没有成稿")
        return 2

    judged = legacy_only = conflicts = fail_open = no_ledger = 0
    rows = []
    for a in arts:
        g = ((a.get("_gate") or {}).get("fact") or {}) if a.get("_gate") else {}
        if not g:
            no_ledger += 1
            rows.append((a.get("title", "")[:26], "无台账", "-", "-", "-"))
            continue
        j = bool(g.get("judged"))
        cons = g.get("contradictions") or []
        leg = g.get("legacy_issues") or []
        judged += 1 if j else 0
        conflicts += 1 if cons else 0
        if leg and not cons:
            legacy_only += 1
        if g.get("fail_open"):
            fail_open += 1
        rows.append((a.get("title", "")[:26],
                     "判定" if j else "未判定",
                     f"{len(cons)} 冲突" if cons else "无冲突",
                     f"旧判据 {len(leg)} 条" if leg else "-",
                     (a.get("_title_score") or {}).get("score", "-")))

    print(f"== {date} 闸门 v2 观察（{len(arts)} 篇）==\n")
    print(f"{'标题':28s}{'事实判定':8s}{'新判据':12s}{'旧判据对照':14s}{'标题分'}")
    for t, j, c, l, s in rows:
        print(f"{t:28s}{j:8s}{c:12s}{l:14s}{s}")

    print("\n-- 指标 --")
    tracked = len(arts) - no_ledger
    print(f"有台账        : {tracked}/{len(arts)}"
          + ("  ⚠️ 无台账的稿子走了旧路径或未接闸门" if no_ledger else ""))
    if tracked == 0:
        # 分母为 0 时不要用 max(1, ...) 凑一个「0/1」，那看起来像「判了但没过」
        print("事实可判(judged): 无可判样本（这批全是改造前产物或未接闸门）")
    else:
        print(f"事实可判(judged): {judged}/{tracked}"
              + ("  ⚠️ 大量未判定 = 事实卡供给有问题，校验形同虚设"
                 if judged == 0 else ""))
    print(f"真矛盾         : {conflicts} 篇")
    print(f"旧判据误报被放过: {legacy_only} 篇  ← v2 的收益")
    print(f"触发 fail_open : {fail_open} 篇"
          + ("  ⚠️ 判据疑似系统性误报，立刻人工看这几篇" if fail_open else ""))

    # 降级事件：记分 vs 丢弃
    ev = meta.get("degrade_events") or []
    stages = collections.Counter((e.get("环节") or "") for e in ev if isinstance(e, dict))
    if stages:
        print("\n-- 降级事件（按环节）--")
        for k, v in stages.most_common():
            print(f"  {k}: {v}")
        giving_up = sum(v for k, v in stages.items()
                        if any(x in k for x in ("丢弃", "评分连续不过")))
        print(f"  → 丢弃类合计: {giving_up}"
              + ("  ⚠️ v2 下应显著下降" if giving_up else "  ✅ 无丢弃"))

    n = ((meta.get("image_floor") or {}).get("articles_below"))
    if n is not None:
        print(f"\n配图低于下限: {n} 篇")
    return 0


def main(argv) -> int:
    import datetime
    date = argv[1] if len(argv) > 1 else datetime.datetime.now().strftime("%Y-%m-%d")
    return report(date)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
