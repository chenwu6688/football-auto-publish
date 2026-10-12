#!/usr/bin/env python3
"""三层选题供给：占比口径、打标与供给充足性（计划 12.1）。

计划 12.1 把内容供给拆成三层，各层互为缓冲：

    一级·确定性  30%   赛程 / 转会窗截止日 / 赛季节点 / 开赛倒计时（可提前 7 天预生成）
    二级·突发    50%   转会、伤停、处罚、表态（受时效闸门管控，24h 内出稿）
    三级·软性    20%   人物故事、榜单、数据对比、历史回顾（蓄水池库存补充）

「把供给拆成三层，各层互为缓冲，日更 6 至 8 篇可稳定不断供。」

本模块是三层供给的唯一口径，供 orchestrator 打标、供 scripts/verify_supply_mix.py 验收：
  - classify(article)   判定一篇成稿所属层级（显式标签优先，其次按来源标志推断）
  - tag(article)        幂等写回 `_supply_layer`
  - summarize(articles) 统计各层篇数与占比、对照目标算偏差
  - check(summary)      供给分层完整性（硬项）与配比/篇数（告警项）

纪律：判定不了层级的成稿**不猜**——归入「未分层」并在验收中作为硬失败，
迫使上游补上来源标志，而不是用 0 或猜测掩盖。
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# 三层标识（与计划 12.1 一致的措辞）
L1 = "一级·确定性"
L2 = "二级·突发"
L3 = "三级·软性"
UNCLASSIFIED = "未分层"

LAYERS = (L1, L2, L3)

# 目标配比（计划 12.1）
TARGET_RATIO = {L1: 0.30, L2: 0.50, L3: 0.20}

# 日更篇数设计区间（计划 12.1：日更 6 至 8 篇可稳定不断供）
DAILY_MIN = 6
DAILY_MAX = 8

# 配比偏差容差（百分点）——三层互相缓冲，允许一定摆动；超容差仅告警
RATIO_TOL_PCT = 15.0

# 三级·软性涵盖的内容类型 / 板块（与 reservoir.SOFT_TYPES 同源）
_SOFT_CONTENT_TYPES = ("人物故事", "榜单", "数据对比", "历史回顾")
_SOFT_SECTIONS = ("人物故事", "战术榜单")
# 实际内容类型 → 三级·软性（榜单/数据/人物类）
_SOFT_BY_CONTENT_TYPE = {"排行榜"}


def _norm_layer(value: str | None) -> str | None:
    """把各种写法归一到 LAYERS 之一；无法识别返回 None。"""
    if not value:
        return None
    v = str(value)
    if v.startswith("一级"):
        return L1
    if v.startswith("二级"):
        return L2
    if v.startswith("三级"):
        return L3
    if v in LAYERS:
        return v
    # 兼容旧标签「确定性」
    if v in ("确定性", "赛程") or v.startswith("确定性"):
        return L1
    return None


def classify(article: dict) -> str:
    """判定成稿所属供给层级。

    优先级：显式 `_supply_layer` > 来源标志（蓄水池/赛程）> 内容类型与板块推断。
    判定不了返回 UNCLASSIFIED（不猜）。
    """
    if not isinstance(article, dict):
        return UNCLASSIFIED

    explicit = _norm_layer(article.get("_supply_layer") or article.get("supply_layer"))
    if explicit:
        return explicit

    # 三级·软性：蓄水池转正
    if article.get("_from_reservoir"):
        return L3
    # 一级·确定性：赛程/节点类供给
    if article.get("_is_fixture"):
        return L1

    ctype = (article.get("content_type") or "").strip()
    section = (article.get("_column_name") or article.get("column_name")
               or article.get("section") or "").strip()

    if ctype in _SOFT_CONTENT_TYPES or section in _SOFT_SECTIONS:
        return L3
    if ctype in _SOFT_BY_CONTENT_TYPE:
        return L3
    if ctype in ("热点球评", "转会资讯", "战术解析", "八卦趣事"):
        return L2
    # 既无来源标志、内容类型也不在册：不猜
    return UNCLASSIFIED


def tag(article: dict) -> str:
    """幂等写回 `_supply_layer`（已有合法标签则保留），返回层级。"""
    layer = classify(article)
    if isinstance(article, dict):
        try:
            article["_supply_layer"] = layer
        except Exception:
            pass
    return layer


def summarize(articles) -> dict:
    """统计三层供给构成。

    返回 {total, counts, ratios(0-100), deviations(百分点), unclassified}。
    """
    items = [a for a in (articles or []) if isinstance(a, dict)]
    counts = {layer: 0 for layer in LAYERS}
    unclassified = 0
    for a in items:
        layer = classify(a)
        if layer in counts:
            counts[layer] += 1
        else:
            unclassified += 1

    total = len(items)
    ratios = {layer: (round(counts[layer] / total * 100, 1) if total else 0.0)
              for layer in LAYERS}
    deviations = {layer: round(ratios[layer] - TARGET_RATIO[layer] * 100, 1)
                  for layer in LAYERS}
    return {
        "total": total,
        "counts": counts,
        "ratios": ratios,
        "targets": {layer: round(TARGET_RATIO[layer] * 100, 1) for layer in LAYERS},
        "deviations": deviations,
        "unclassified": unclassified,
    }


def check(summary: dict, *, tol_pct: float = RATIO_TOL_PCT,
          daily_min: int = DAILY_MIN, daily_max: int = DAILY_MAX) -> list[dict]:
    """按计划 12.1 生成校验项。

    硬项（ok=False 即验收不通过）：
      - 分层完整性：不得出现「未分层」成稿
    告警项（advisory=True）：
      - 三层齐备：每层至少 1 篇（供给结构退化的信号）
      - 配比偏差：各层占比相对 30/50/20 的偏差 ≤ tol_pct
      - 日更篇数：总篇数落在 [daily_min, daily_max]
    """
    checks: list[dict] = []
    total = summary.get("total", 0)

    # 硬项：分层完整性
    if total == 0:
        checks.append({"item": "供给分层完整性", "ok": True, "advisory": False,
                       "detail": ["当日无成稿（跳过）"]})
    else:
        unc = summary.get("unclassified", 0)
        checks.append({"item": "供给分层完整性", "ok": unc == 0, "advisory": False,
                       "detail": [f"未分层 {unc}/{total}"]})

    # 告警项：三层齐备
    missing = [layer for layer in LAYERS if total and summary["counts"].get(layer, 0) == 0]
    checks.append({"item": f"三层齐备（{L1}/{L2}/{L3}）", "ok": not missing, "advisory": True,
                   "detail": [f"缺层：{'、'.join(missing)}" if missing else "三层均有产出"]})

    # 告警项：配比偏差
    dev = summary.get("deviations", {})
    over = {k: v for k, v in dev.items() if abs(v) > tol_pct}
    checks.append({
        "item": f"层级配比偏差≤{tol_pct:.0f}pct", "ok": not over, "advisory": True,
        "detail": [f"实际：{summary.get('ratios')}", f"目标：{summary.get('targets')}",
                   f"偏差：{dev}"],
    })

    # 告警项：日更篇数
    in_range = total == 0 or (daily_min <= total <= daily_max)
    checks.append({
        "item": f"日更篇数 {daily_min}-{daily_max}", "ok": in_range, "advisory": True,
        "detail": [f"当日 {total} 篇（设计区间 {daily_min}-{daily_max}）"],
    })
    return checks


def render(summary: dict, checks: list[dict]) -> str:
    """把单日供给构成渲染为 Markdown 片段。"""
    L = ["| 层级 | 篇数 | 占比 | 目标 | 偏差 |", "|---|---|---|---|---|"]
    for layer in LAYERS:
        L.append(f"| {layer} | {summary['counts'][layer]} | {summary['ratios'][layer]}% | "
                 f"{summary['targets'][layer]}% | {summary['deviations'][layer]:+}pt |")
    if summary.get("unclassified"):
        L.append(f"| {UNCLASSIFIED} | {summary['unclassified']} | — | 0% | — |")
    L.append("")
    for c in checks:
        mark = "⚠️" if c.get("advisory") and not c["ok"] else ("✅" if c["ok"] else "❌")
        L.append(f"- {mark} {c['item']}：" + "；".join(c.get("detail", [])))
    return "\n".join(L)


def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="三层供给汇总（计划 12.1）")
    ap.add_argument("--date", help="读取 output/<date>/metadata.json")
    args = ap.parse_args()
    if not args.date:
        ap.error("需要 --date YYYY-MM-DD")
    meta_path = PROJECT_ROOT / "output" / args.date / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    s = summarize(meta.get("articles", []))
    print(render(s, check(s)))


if __name__ == "__main__":
    _cli()
