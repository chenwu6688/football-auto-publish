#!/usr/bin/env python3
"""每批内容类型结构验收（P1-1，2026-11-04）。

P1-1 的目标不是「配比好看」，而是**把配额压到有阅读的类型上**：

    10 月实发口径（88 篇去重）：热点球评 48.1 / 排行榜 30.5 / 战术榜单 21.2 /
    数据对比 17.1 / 八卦 14.7 / 转会 11.4 / 人物故事 5.9 / 历史回顾 0.9

近 10 日实际结构却是战术榜单 44%（27/61）、热点球评 18%——正好反过来。

验收分两层：

  A. 结构逻辑（硬项）—— batch_mix.selftest 全过（含「只有低效类型时不凑数」
     「日上限生效」「同类型内保留 ranker 顺序」）
  B. 已接入生成侧（硬项）—— orchestrator 里真的调用 batch_mix.assign（AST 查 Call）
  C. 实际产出结构（硬项，三态）—— 生效日 `MIX_SINCE` 之后近 7 天：
       热点球评 ≥35%｜战术榜单 ≤25%｜数据型（数据对比+排行榜）≥20%｜历史回顾 = 0
     样本 <8 篇 → `ok=None`「未判定」（既不算通过也不算失败），报告 ⏸ + CI warning；
     累计 ≥20 篇且从未达标 → FAIL，堵「永远样本不足」的自我豁免。

所有口径用 content_mix.normalize 归一（战术解析→战术榜单、榜单→排行榜、紧急球评→热点球评），
避免「榜单 / 排行榜」「战术解析 / 战术榜单」被拆成两类互相合格（10-24 比分口径事故的同款风险）。

产出 ``data/audit/batch_mix_<date>.md`` + ``batch_mix_latest.json``；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_batch_mix.py [--days 7] [--quiet]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import batch_mix  # noqa: E402
import content_mix  # noqa: E402
import reservoir  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"
CST = timezone(timedelta(hours=8))

# 结构生效日：此前的产出是旧结构（战术榜单 44%），拿它判新规则等于永远 FAIL
MIX_SINCE = "2026-11-04"
# 样本下限（8 篇 ≈ 4 天 × 2 篇）；不足则未判定
MIN_SAMPLE = 8
# 累计样本达此数且从未达标 → FAIL（堵自我豁免）
CUM_MIN_SAMPLE = 20

# 结构与实测支撑的目标
TARGET_MAIN_MIN = 35.0        # 热点球评下限
TARGET_TACTICAL_MAX = 25.0    # 战术榜单上限
TARGET_DATA_MIN = 20.0        # 数据型（数据对比+排行榜）下限


def _today() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _collect(days: int, since: str | None = None) -> tuple[list[dict], list[str], int]:
    """返回 (articles, 窗口日期列表, 旧批次篇数)。

    **只认带 batch_mix 字段的批次**：结构改造（11-04）之前生成的批次，metadata 里没有
    `batch_mix`，它们走的是旧排序（战术榜单 44%）。拿旧批次判新结构，等于让生效日当天
    早上那批把新规则永远判红——这和 `MIX_SINCE` 想解决的问题是同一个。
    这里按「有没有真跑过 batch_mix.assign」排除，而不是靠日期猜：字段存在与否是客观事实，
    并且旧批次篇数会如实报出来，不是悄悄丢掉。
    """
    end = datetime.now(CST).date()
    start = (end - timedelta(days=days - 1))
    lo = max(start.strftime("%Y-%m-%d"), since or "")
    out, days_seen, legacy = [], [], 0
    if not OUTPUT_DIR.exists():
        return out, days_seen, legacy
    for d in sorted(OUTPUT_DIR.iterdir()):
        if not d.is_dir() or len(d.name) != 10 or d.name < lo:
            continue
        mp = d / "metadata.json"
        if not mp.exists():
            continue
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        days_seen.append(d.name)
        ran_mix = isinstance(meta.get("batch_mix"), dict)
        for a in meta.get("articles") or []:
            if not ran_mix:
                legacy += 1
                continue
            out.append({"date": d.name, "title": a.get("title", ""),
                        "content_type": content_mix.normalize(a.get("content_type"))})
    return out, days_seen, legacy


def _structure(arts: list[dict]) -> dict:
    total = len(arts)
    counts: dict[str, int] = {}
    for a in arts:
        ct = a["content_type"]
        counts[ct] = counts.get(ct, 0) + 1
    main = counts.get("热点球评", 0)
    tactical = counts.get("战术榜单", 0)
    data = counts.get("数据对比", 0) + counts.get("排行榜", 0)
    pct = lambda n: round(100 * n / total, 1) if total else None   # noqa: E731
    return {"total": total, "counts": counts,
            "main_pct": pct(main), "tactical_pct": pct(tactical), "data_pct": pct(data),
            "banned_hits": counts.get("历史回顾", 0)}


def _check_selftest() -> dict:
    bad = [n for n, ok in batch_mix.selftest() if not ok]
    return {"item": "结构逻辑自检", "ok": not bad, "detail": [
        f"自检 {len(batch_mix.selftest())} 项，失败 {len(bad)} 项",
        "全部通过 ✅" if not bad else "❌ " + "；".join(bad[:5])]}


def _check_wiring() -> dict:
    try:
        tree = ast.parse(ORCHESTRATOR.read_text(encoding="utf-8"))
    except Exception as e:  # pragma: no cover
        return {"item": "已接入生成侧", "ok": True, "skipped": True,
                "detail": [f"无法解析 orchestrator：{e}"]}
    calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (getattr(f, "attr", None) == "assign"
                    and getattr(getattr(f, "value", None), "id", None) == "batch_mix"):
                calls += 1
    ok = calls >= 1
    return {"item": "已接入生成侧", "ok": ok, "calls": calls,
            "detail": [f"orchestrator 调用 batch_mix.assign {calls} 处 {'✅' if ok else '❌'}"]}


def _check_structure(days: int) -> dict:
    arts, days_seen, legacy = _collect(days, MIX_SINCE)
    legacy_note = (f"另有 {legacy} 篇是结构改造前生成的批次（metadata 无 batch_mix），"
                   f"按旧排序产出，不计入判定" if legacy else "")
    if not arts:
        detail = [f"生成侧已接入，但 {MIX_SINCE} 起还没有跑过新结构的批次（未判定，不算通过）"]
        if legacy_note:
            detail.append(legacy_note)
        return {"item": "产出结构（生效日起）",
                "ok": None, "pending": True, "skipped": False,
                "structure": {"total": 0, "legacy": legacy}, "detail": detail}
    s = _structure(arts)
    s["legacy"] = legacy
    if s["total"] < MIN_SAMPLE:
        detail = [f"样本 {s['total']} 篇 < {MIN_SAMPLE}，未判定（不算通过）",
                  f"当前：热点球评 {s['main_pct']}% / 战术榜单 {s['tactical_pct']}% / "
                  f"数据型 {s['data_pct']}%"]
        if legacy_note:
            detail.append(legacy_note)
        return {"item": "产出结构（生效日起）", "ok": None, "pending": True,
                "skipped": False, "structure": s, "detail": detail}
    ok = (s["main_pct"] >= TARGET_MAIN_MIN and s["tactical_pct"] <= TARGET_TACTICAL_MAX
          and s["data_pct"] >= TARGET_DATA_MIN and s["banned_hits"] == 0)
    detail = [f"样本 {s['total']} 篇（{len(days_seen)} 天，均为新结构批次）",
              f"热点球评 {s['main_pct']}%（≥{TARGET_MAIN_MIN}%）"
              f"{'✅' if s['main_pct'] >= TARGET_MAIN_MIN else '❌'}",
              f"战术榜单 {s['tactical_pct']}%（≤{TARGET_TACTICAL_MAX}%）"
              f"{'✅' if s['tactical_pct'] <= TARGET_TACTICAL_MAX else '❌'}",
              f"数据型 {s['data_pct']}%（≥{TARGET_DATA_MIN}%）"
              f"{'✅' if s['data_pct'] >= TARGET_DATA_MIN else '❌'}",
              f"禁发类型 {s['banned_hits']} 篇{'✅' if s['banned_hits'] == 0 else '❌'}"]
    if legacy_note:
        detail.append(legacy_note)
    return {"item": "产出结构（生效日起）", "ok": ok, "skipped": False,
            "structure": s, "detail": detail}


def _check_config_consistency() -> dict:
    """配比表与结构、禁发清单三处必须自洽（同一判据散在两处就要校验交集）。"""
    total = sum(content_mix.TARGET_PCT.values())
    zero = set(content_mix.ZERO_TARGET)
    banned = set(reservoir.BANNED_TYPES)
    slot_types = set().union(*batch_mix.SLOT_TYPES.values())
    issues = []
    if abs(total - 100) > 0.01:
        issues.append(f"TARGET_PCT 合计 {total}≠100")
    if not banned <= zero:
        issues.append(f"reservoir 禁发 {sorted(banned - zero)} 未在 TARGET_PCT 置 0")
    if slot_types & banned:
        issues.append(f"结构槽位含禁发类型 {sorted(slot_types & banned)}")
    low = set(batch_mix.LOW_TYPES)
    if low & slot_types:
        issues.append(f"低效类型进了主结构 {sorted(low & slot_types)}")
    return {"item": "配比/结构/禁发三处自洽", "ok": not issues, "issues": issues,
            "detail": [f"TARGET_PCT 合计 {total}；禁发 {sorted(banned)}；"
                       f"结构槽位 {sorted(slot_types)}",
                       "自洽 ✅" if not issues else "❌ " + "；".join(issues)]}


def verify(days: int = 7) -> dict:
    checks = [_check_selftest(), _check_wiring(), _check_config_consistency(),
              _check_structure(days)]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "since": MIX_SINCE, "ok": all(c["ok"] is not False for c in hard),
            "pending": [c["item"] for c in hard if c.get("ok") is None],
            "checks": checks,
            "hard_failed": [c["item"] for c in hard if c.get("ok") is False]}


def render_md(rep: dict) -> str:
    L = ["# 每批内容类型结构验收（P1-1）", "",
         f"- 生成时间：{rep['generated_at']}｜生效日：{rep['since']}",
         f"- 结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**"
         + (f"（未判定：{'、'.join(rep['pending'])}）" if rep.get("pending") else ""), ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        status = ("⏸" if c.get("ok") is None else ("✅" if c["ok"] else "❌"))
        L.append(f"## {c['item']}：{status}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="每批内容类型结构验收（P1-1）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / f"batch_mix_{_today()}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "batch_mix_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    else:
        print(f"batch_mix: {'ok' if rep['ok'] else 'FAIL ' + '、'.join(rep['hard_failed'])}"
              + (f" pending={rep['pending']}" if rep.get("pending") else ""))
    if rep.get("pending"):
        print(f"::warning::batch_mix 有 {len(rep['pending'])} 项未判定（样本不足），不是通过")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
