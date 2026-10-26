#!/usr/bin/env python3
"""标题句式闸门（P0-3）：同句式 14 天内最多 2 篇。

病根不在「模型爱写主帅苦情」，而在供给侧：赛程稿按认知度组合分型，
A/B（一强一弱）是最常见组合，而它原来只对应**一条**模板，于是
「没想到XX主帅这场压力比YY还大」成了每天固定产出——近 14 天 26 篇赛程稿
里 19 篇都是这个句式。读者看到的就是「推文全是人物故事」。

两道防线：
  1. 源头（已做）：scripts/fixture_library.ANGLE_RULES 每型 3 条模板 + 按日轮换；
  2. 出口（本模块）：生成后按**句式特征**统计，超过阈值就打回重写。

为什么用句式特征而不是「模板字面匹配」：标题是 LLM 生成的，不会字面复刻模板
（「没想到亚特兰大主帅这场压力比AC米兰还大」vs「拉齐奥主帅这场竟比尤文更输不起」
字面差很远，但读者感到的是同一句话）。所以按「结构词共现」判定。

用法：
    import template_gate
    sat = template_gate.saturated_signatures(template_gate.recent_titles(OUTPUT_DIR, date_str))
    ... generate(..., avoid_signatures=sat)     # 命中 sat 的标题打回
    template_gate.signature_of("没想到罗马主帅这场压力比尤文还大")  → "主帅压力体"
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

# 14 天内同一句式最多出现次数（计划：标题禁止模板化，同句式 ≤2 篇）
MAX_PER_SIGNATURE = 2
DEFAULT_WINDOW_DAYS = 14

# 句式特征表：(名称, 必须同时出现的词, 任一出现的词)
# 顺序即优先级——首条命中即止，避免一个标题被反复计数。
# 新增病害句式时**只往下加**，不要改已有条目（历史统计口径才不会跳变）。
SIGNATURES: list[tuple[str, tuple, tuple]] = [
    # 「主帅+压力/输不起」：近 14 天 19 篇的重灾区，句式特征最明显
    ("主帅压力体", ("主帅",), ("压力", "输不起", "坐不住", "难熬")),
    # 「没想到/原来/竟然」+主帅：同一条模板换汤不换药
    ("没想到体", ("主帅",), ("没想到", "原来", "竟然", "居然")),
    # 强弱反差模板里反复出现的「憋着劲」
    ("憋劲体", (), ("憋着", "攒着劲", "攒劲", "攒了口气")),
    # 关注度落差模板的产物
    ("被看轻体", (), ("没人关注", "无人问津", "被看轻", "被低估", "没人看好")),
    # 「以为稳了 / 稳不稳」类
    ("以为稳体", (), ("以为稳", "稳了", "稳不稳", "别以为")),
    # 「输不起」单独成体（不含主帅时）
    ("输不起体", (), ("输不起",)),
    # 赛前合辑/前瞻腔（计划 7.1：前瞻体占比为零，这里再兜一层）
    ("前瞻腔", (), ("前瞻", "赛前分析", "赛前展望", "预测首发")),
]


def signature_of(title: str) -> str | None:
    """返回标题命中的第一个句式名；都不命中返回 None。"""
    t = (title or "").strip()
    if not t:
        return None
    for name, all_words, any_words in SIGNATURES:
        if all_words and not all(w in t for w in all_words):
            continue
        if any_words and not any(w in t for w in any_words):
            continue
        if not all_words and not any_words:
            continue
        return name
    return None


def count_signatures(titles) -> dict:
    """统计各句式命中次数（只返回命中的句式）。"""
    out: dict[str, int] = {}
    for t in titles or ():
        sig = signature_of(t)
        if sig:
            out[sig] = out.get(sig, 0) + 1
    return out


def saturated_signatures(titles, max_per_signature: int = MAX_PER_SIGNATURE) -> set:
    """已用满的句式集合——生成侧据此打回同款标题。"""
    return {sig for sig, n in count_signatures(titles).items() if n >= max_per_signature}


def check(titles, max_per_signature: int = MAX_PER_SIGNATURE) -> dict:
    """校验：任一句式超过阈值即 FAIL。返回 {ok, counts, violations}。"""
    counts = count_signatures(titles)
    violations = {k: v for k, v in counts.items()
                  if v > max_per_signature}
    return {"ok": not violations, "counts": counts, "violations": violations,
            "threshold": max_per_signature, "total": len(list(titles or ()))}


def recent_titles(output_dir, end_date: str, days: int = DEFAULT_WINDOW_DAYS,
                  since: str | None = None) -> list:
    """读近 N 天（含 end_date 当天）已发标题。

    since：只取 >= 该日期的成稿。用于「改造生效日」之后才校验——
    拿改造前的历史稿去判新规则，等于永远 FAIL，闸门就成噪音了。

    缺失日期直接跳过（不猜、不补），文件坏了也不阻塞——闸门宁可少算，
    不能因为一个坏 json 把整条流水线卡死。
    """
    out = []
    try:
        base = Path(output_dir)
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
        since_d = datetime.strptime(since, "%Y-%m-%d").date() if since else None
    except Exception:
        return out
    for i in range(days):
        d = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        if since_d and d < since_d.strftime("%Y-%m-%d"):
            continue
        p = base / d / "metadata.json"
        if not p.exists():
            continue
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in meta.get("articles") or []:
            if a.get("is_prediction"):
                continue      # 预测稿不发布，不进标题口径
            title = (a.get("title") or "").strip()
            if title:
                out.append(title)
    return out
