#!/usr/bin/env python3
"""三层供给占比与供给充足性验收（计划 12.1）。

计划 12.1 要求供给拆成三层（一级·确定性 30% / 二级·突发 50% / 三级·软性 20%），
并保证「日更 6 至 8 篇可稳定不断供」。本脚本对实际产出做常态化验收：

  硬项（不达标即退出码 1）
    A. 分层合法性——带 supply_layer 的成稿其值必须落在三层之内（不得出现非法标签）
    B. 无断供——窗口内每个「有产出目录」的日子篇数不得为 0

  告警项（只提示）
    C. 打标覆盖——窗口内成稿带 supply_layer 的比例（改造后应趋近 100%）
    D. 未分层——无法归层的成稿数（改造后应趋近 0）
    E. 三层齐备——每层至少 1 篇
    F. 层级配比——各层占比相对 30/50/20 的偏差 ≤ 15pct
    G. 日均篇数——窗口日均落在 6-8 篇

用法：python3 scripts/verify_supply_mix.py [--days 14] [--quiet]
产出：data/audit/supply_mix_<date>.md + data/audit/supply_mix_latest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import supply_mix  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "output"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = ZoneInfo("Asia/Shanghai")


def _load_day(date_str: str) -> list[dict]:
    p = OUTPUT_DIR / date_str / "metadata.json"
    if not p.exists():
        return []
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    # 预测类不发布到头条，不进供给配比口径（R13 决策④）：优先成稿级 is_prediction，
    # 历史数据回落 topics 级 _is_prediction 按标题对拍（与看板/配额验收同口径）
    pred_titles = {t.get("title") for t in meta.get("topics", []) or []
                   if t.get("_is_prediction")}
    return [a for a in (meta.get("articles") or [])
            if isinstance(a, dict)
            and not a.get("is_prediction")
            and a.get("title") not in pred_titles]


def collect(days: int, *, today: str | None = None) -> dict:
    """收集窗口内各日供给构成。"""
    today = today or datetime.now(CST).strftime("%Y-%m-%d")
    d0 = datetime.strptime(today, "%Y-%m-%d")
    per_day: list[dict] = []
    all_articles: list[dict] = []
    empty_days: list[str] = []
    for i in range(days):
        ds = (d0 - timedelta(days=i)).strftime("%Y-%m-%d")
        if not (OUTPUT_DIR / ds).exists():
            continue                     # 无产出目录：本机无该日运行痕迹，不判断供
        arts = _load_day(ds)
        per_day.append({"date": ds, **supply_mix.summarize(arts)})
        all_articles.extend(arts)
        if not arts:
            empty_days.append(ds)
    per_day.sort(key=lambda x: x["date"])
    return {"today": today, "window_days": days, "per_day": per_day,
            "empty_days": empty_days, "overall": supply_mix.summarize(all_articles),
            "all_articles": all_articles}


def verify(days: int = 14, *, today: str | None = None) -> dict:
    data = collect(days, today=today)
    arts = data["all_articles"]
    overall = data["overall"]

    checks: list[dict] = []

    # A. 分层合法性（硬）——带标签的成稿值必须合法
    illegal = [a.get("supply_layer") for a in arts
               if a.get("supply_layer") and a.get("supply_layer") not in supply_mix.LAYERS]
    unclassified_tagged = [a for a in arts if a.get("supply_layer") == supply_mix.UNCLASSIFIED]
    checks.append({
        "item": "分层合法性（硬）", "ok": not illegal and not unclassified_tagged,
        "advisory": False,
        "detail": [f"标签成稿 {sum(1 for a in arts if a.get('supply_layer'))} 篇",
                   f"非法标签 {len(illegal)} 篇" + (f"：{illegal[:5]}" if illegal else ""),
                   f"显式标为未分层 {len(unclassified_tagged)} 篇"],
    })

    # B. 无断供（硬）——有产出目录的日子不得 0 篇
    checks.append({
        "item": "无断供（硬）", "ok": not data["empty_days"], "advisory": False,
        "detail": [f"窗口内有产出目录 {len(data['per_day'])} 天",
                   f"零篇日：{'、'.join(data['empty_days']) if data['empty_days'] else '无'}"],
    })

    # C. 打标覆盖（告警）
    tagged = sum(1 for a in arts if a.get("supply_layer") in supply_mix.LAYERS)
    cov = round(tagged / len(arts) * 100, 1) if arts else 100.0
    checks.append({
        "item": "打标覆盖率（告警）", "ok": cov >= 95.0, "advisory": True,
        "detail": [f"{tagged}/{len(arts)} = {cov}%（改造前历史数据无此字段属预期）"],
    })

    # D. 未分层成稿（告警）——改造后应趋近 0；改造前因缺来源标志属预期
    checks.append({
        "item": "未分层成稿（告警）", "ok": overall["unclassified"] == 0, "advisory": True,
        "detail": [f"{overall['unclassified']}/{overall['total']} 篇无法归层"
                   f"（改造前数据缺来源标志；改造后上游应补齐，不得静默归 0）"],
    })

    # E-G：三层齐备 / 配比 / 日均篇数（复用 supply_mix.check 的告警项；
    #      「供给分层完整性」由上方 D 项以告警口径承担，不重复计入硬失败）
    checks.extend(c for c in supply_mix.check(overall)
                  if c["item"] != "供给分层完整性")

    hard_failed = [c["item"] for c in checks if not c.get("advisory") and not c["ok"]]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "window_days": days, "ok": not hard_failed, "hard_failed": hard_failed,
            "per_day": data["per_day"], "overall": overall, "checks": checks}


def render_md(rep: dict) -> str:
    L = [f"# 三层供给验收报告（近 {rep['window_days']} 天）", "",
         f"生成时间：{rep['generated_at']}　|　计划 12.1", "",
         f"总体结论：{'✅ 通过' if rep['ok'] else '❌ 硬项未通过：' + '、'.join(rep['hard_failed'])}",
         ""]
    L.append("## 逐日供给构成")
    L.append("| 日期 | 总篇数 | 一级·确定性 | 二级·突发 | 三级·软性 | 未分层 |")
    L.append("|---|---|---|---|---|---|")
    for d in rep["per_day"]:
        c = d["counts"]
        L.append(f"| {d['date']} | {d['total']} | {c[supply_mix.L1]} | {c[supply_mix.L2]} | "
                 f"{c[supply_mix.L3]} | {d['unclassified']} |")
    L.append("")

    ov = rep["overall"]
    L.append("## 窗口整体配比")
    L.append("| 层级 | 篇数 | 占比 | 目标 | 偏差 |")
    L.append("|---|---|---|---|---|")
    for layer in supply_mix.LAYERS:
        L.append(f"| {layer} | {ov['counts'][layer]} | {ov['ratios'][layer]}% | "
                 f"{ov['targets'][layer]}% | {ov['deviations'][layer]:+}pt |")
    L.append("")

    L.append("## 校验项")
    for c in rep["checks"]:
        mark = "❌" if not c["ok"] and not c.get("advisory") else ("⚠️" if not c["ok"] else "✅")
        L.append(f"### {c['item']}：{mark}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="三层供给占比与供给充足性验收（计划 12.1）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"supply_mix_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "supply_mix_latest.json").write_text(
        json.dumps({k: v for k, v in rep.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    if not rep["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
