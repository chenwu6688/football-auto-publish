#!/usr/bin/env python3
"""每批内容类型结构（P1-1，2026-11-04 阅读量诊断）。

## 为什么不是「调配比」而是「定结构」

`content_mix.TARGET_PCT` 给的是**月度占比**，落到「本批 2 篇该写什么」时是空的：
LLM 一次给 3 个候选，谁排前面就写谁，于是战术榜单一路涨到 44%（近 10 日 27/61），
而它是中位阅读 1 的类型。占比目标管不住单批，**单批要靠结构**。

## 结构从哪来（实测篇均，实际发布口径）

    热点球评 48.1 ｜ 排行榜 30.5 ｜ 战术榜单 21.2 ｜ 数据对比 17.1
    八卦趣事 14.7 ｜ 转会资讯 11.4 ｜ 人物故事 5.9 ｜ 历史回顾 0.9（已禁发）

所以每批 2 篇的骨架是：

    slot A（main）  热点球评 / 紧急球评   —— 唯一稳定过百的类型
    slot B（data）  数据对比 / 排行榜      —— 数据型，可复用事实卡，成本低

战术榜单只在 A/B 都无候选时补位（并记 degraded），低效类型（人物故事/转会/八卦）
不再进主结构，只允许「当日还没出现过」时补第 3 篇以后的空位。

## 与计划 12.1（30/50/20）的关系

数据对比/排行榜归「三级·软性」，A/B 结构会让三级占比升到 ~50%，超出计划的 20%±15pp。
这是**已知的、有数据依据的偏离**：计划的分层目标是「供给不断档」，而 P1-1 的目标是
「把配额压到有阅读的类型上」。两个目标在这里冲突，按用户 11-04 的决定执行 P1-1，
`supply_mix` 的配比告警**保持原样不软化**，报告里要能看出「三级上升是否由数据型贡献」。

纪律：
- 只重排、不删题：候选不够就少写，绝不用低效类型凑数（宁可 1 篇，不发 2 篇垃圾）；
- 同类型单日上限来自 `DAILY_CAP`，超限自动降级到次优类型；
- 类型归一复用 `content_mix.normalize`（战术解析→战术榜单、榜单→排行榜、紧急球评→热点球评），
  避免两处各写各的（10-24 比分口径事故的同款风险）。

命令行：
    python3 batch_mix.py --selftest
    python3 batch_mix.py --plan 2
"""

from __future__ import annotations

import argparse
import json

import content_mix

# 结构槽位：按顺序填，填不满就少写（不凑数）
STRUCTURE = ("main", "data")

# 槽位可接受的类型（已归一）。顺序 = 优先级。
SLOT_TYPES = {
    "main": ("热点球评", "紧急球评"),
    "data": ("数据对比", "排行榜"),
}
# 补位顺序：两个主槽都无候选时才用（战术榜单 21.2 篇均，比低效三兄弟高）
FALLBACK_TYPES = ("战术榜单",)
# 低效类型：不进主结构，只在「当天没出现过且主槽已填满」时补位
LOW_TYPES = ("人物故事", "转会资讯", "八卦趣事")

# 单日上限（篇）。超过即降级到次优类型，不硬发。
DAILY_CAP = {"数据对比": 3, "排行榜": 2, "战术榜单": 2,
             "人物故事": 1, "转会资讯": 1, "八卦趣事": 1, "热点球评": 6}


def _norm(topic: dict) -> str:
    return content_mix.normalize((topic or {}).get("content_type"))


def plan_slots(count: int) -> tuple[str, ...]:
    """返回长度为 count 的槽位序列（2 篇 = main+data，3 篇 = main+data+main）。"""
    n = max(0, int(count))
    out = []
    for i in range(n):
        out.append(STRUCTURE[i % len(STRUCTURE)])
    return tuple(out)


def assign(topics, count: int, *, day_counts: dict | None = None) -> tuple[list[dict], dict]:
    """按结构重排选题，返回 (选中的 topics, 报告)。

    topics 的**相对顺序被保留**（ranker 已按 L3 排过序，同类型内不要打乱）。
    day_counts: 当日已发各类型篇数（用于 DAILY_CAP）。
    """
    items = [t for t in (topics or []) if isinstance(t, dict)]
    day = dict(day_counts or {})
    used: dict[str, int] = {}
    chosen: list[dict] = []
    report = {"slots": list(plan_slots(count)), "picked": [], "degraded": [],
              "skipped": [], "day_counts": dict(day)}

    def _avail(t: dict, ct: str) -> bool:
        cap = DAILY_CAP.get(ct)
        if cap is None:
            return True
        return day.get(ct, 0) + used.get(ct, 0) < cap

    def _take(cands, slot: str) -> bool:
        for t in cands:
            if t in chosen:
                continue
            ct = _norm(t)
            if not _avail(t, ct):
                continue
            chosen.append(t)
            used[ct] = used.get(ct, 0) + 1
            report["picked"].append({"slot": slot, "type": ct, "title": (t.get("title") or "")[:28]})
            return True
        return False

    for slot in plan_slots(count):
        pool = [t for t in items if _norm(t) in SLOT_TYPES[slot]]
        if _take(pool, slot):
            continue
        # 主槽没有候选：先用 FALLBACK（战术榜单），再退到任意未用类型
        if _take([t for t in items if _norm(t) in FALLBACK_TYPES], slot):
            report["degraded"].append(f"{slot} 槽无 {SLOT_TYPES[slot]} 候选，用战术榜单补位")
            continue
        if _take([t for t in items if _norm(t) not in LOW_TYPES], slot):
            report["degraded"].append(f"{slot} 槽用次优类型补位")
            continue
        report["degraded"].append(f"{slot} 槽无可用候选（不凑数，本批少写 1 篇）")

    # 结构填满后若还有空额（count 大于 2 的情况），低效类型可补，但受 DAILY_CAP。
    # 前提是 chosen 非空：整批只有低效类型时宁可 0 篇，也不发一整批垃圾
    # （P1-1「停发低效类型」；同时避免「为了凑数把 2 篇都写成人物故事」）。
    while chosen and len(chosen) < min(count, len(items)):
        before = len(chosen)
        _take([t for t in items if _norm(t) in LOW_TYPES], "extra")
        if len(chosen) == before:
            break

    skipped = [t for t in items if t not in chosen]
    report["skipped"] = [{"type": _norm(t), "title": (t.get("title") or "")[:28]} for t in skipped]
    report["selected"] = len(chosen)
    report["requested"] = int(count)
    return chosen, report


def day_type_counts(articles) -> dict:
    """统计当日已产出的各类型篇数（喂给 assign 的 day_counts）。"""
    out: dict[str, int] = {}
    for a in articles or []:
        ct = content_mix.normalize((a or {}).get("content_type"))
        out[ct] = out.get(ct, 0) + 1
    return out


def selftest() -> list[tuple[str, bool]]:
    cases: list[tuple[str, bool]] = []

    def T(ct, name):
        return {"content_type": ct, "title": f"{name}标题"}

    # 标准 2 篇：1 热点球评 + 1 数据对比
    picked, rep = assign([T("战术榜单", "甲的"), T("数据对比", "乙的"), T("热点球评", "丙的")], 2)
    cases.append(("2 篇 = 1 main + 1 data", [content_mix.normalize(t["content_type"]) for t in picked]
                  == ["热点球评", "数据对比"]))
    cases.append(("未选中的进 skipped", len(rep["skipped"]) == 1))

    # 没有数据型 → 战术榜单补位并记 degraded
    picked2, rep2 = assign([T("战术榜单", "甲"), T("人物故事", "乙")], 2)
    types2 = [content_mix.normalize(t["content_type"]) for t in picked2]
    cases.append(("缺数据型 → 战术榜单补位", "战术榜单" in types2))
    cases.append(("补位要留痕", any("战术榜单补位" in d for d in rep2["degraded"])))

    # 只有低效类型 → 不凑数（宁可少写）
    picked3, rep3 = assign([T("人物故事", "甲")], 2)
    cases.append(("只有低效类型时不凑数", len(picked3) == 0))

    # 日上限：战术榜单当日已 2 篇 → 不能再补位
    picked4, _ = assign([T("战术榜单", "甲"), T("热点球评", "乙")], 2,
                        day_counts={"战术榜单": 2})
    cases.append(("战术榜单日上限生效", all(
        content_mix.normalize(t["content_type"]) != "战术榜单" for t in picked4)))

    # 类型归一：战术解析/榜单 要能识别
    picked5, _ = assign([T("榜单", "甲"), T("热点球评", "乙")], 2)
    cases.append(("榜单归一为排行榜", [content_mix.normalize(t["content_type"]) for t in picked5]
                  == ["热点球评", "排行榜"]))

    # 稳定：同类型内保留 ranker 顺序
    picked6, _ = assign([T("热点球评", "先"), T("热点球评", "后"), T("数据对比", "数")], 2)
    cases.append(("同类型内保留原顺序", picked6[0]["title"] == "先标题"))

    # 3 篇 = main + data + main
    cases.append(("3 篇槽位 main/data/main", plan_slots(3) == ("main", "data", "main")))
    cases.append(("空候选不抛", assign([], 2)[0] == []))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="每批内容类型结构（P1-1）")
    ap.add_argument("--plan", type=int, help="打印 N 篇的槽位序列")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.plan:
        print(f"{args.plan} 篇：{' + '.join(plan_slots(args.plan))}")
        print(json.dumps({k: list(v) for k, v in SLOT_TYPES.items()}, ensure_ascii=False))
        print(f"补位：{'、'.join(FALLBACK_TYPES)}；低效（不进主结构）：{'、'.join(LOW_TYPES)}")
        return 0
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检 {len(selftest())} 项，失败 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
