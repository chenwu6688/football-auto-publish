# -*- coding: utf-8 -*-
"""选题去重：**两道硬判据 + 一道告警**（2026-11-06 收敛）。

## 原来是什么样

`select_topics` 里串了 5 道过滤，全写在一个函数体里：

    批内关键词 40% → 跨批次 4 判据 → 昨日关键词 40% → 7 天事件指纹 → 7 天关键词 50%

问题不在于「道数多」，而在于**判据强弱不分**：

* 强判据只有两条 —— 「同一场比赛」（对阵键）和「同一件事」（事件键），
  它们与语言、措辞、标题写法都无关；
* 另外三道全是**关键词重叠率**。关键词是 LLM 自由生成的：换个说法就绕过
  （漏放），同一支球队不同事情就误杀（错杀）。
  11-06 实测：`/data/audit/topic_funnel.jsonl` 里 `dedup` 层吃掉一半候选，
  而供给本来就窄 —— 越窄越不敢错杀。

## 现在是什么样

    hard 1  同一场比赛：对阵键（今日 fixture_pairs）+ 赛果指纹（7 天）
    hard 2  同一件事  ：event_key = 实体 + 事件族（今日热点键 + 今日/7 天标题算出）
    warn    关键词重叠：只打印 + 记进漏斗，**不删题**

判据不用「配置开关」控制强弱 —— 强的就是强，弱的就是弱，写在代码里。
想放开某一条，必须先改这里，别在调用方加一个 `if`。

## 为什么事件键要包含「今日已发标题」

热点通道已经按 event_key 去重了（`covered["hot_event_keys"]`），但**主稿通道
从来不看这个键**：它只看关键词和赛果指纹。于是同一条「C罗退役」，热点通道
上午写过，午间主稿换一套关键词又写了一遍 —— 关键词重叠率算出来只有 20%，
两道 40% / 50% 的阈值都过了。
"""

from __future__ import annotations

import hot_topics
import team_names
from data_collector import is_event_duplicate

#: 关键词重叠的告警阈值（不再作为删除判据）。
#:
#: 保留三个口径是为了台账里能看出「哪一条差多少就被误杀过」——这是下一步
#: 决定要不要恢复硬拦的唯一依据。数字与历史实现保持一致，便于前后对比。
WARN_RATIOS = {"今日": 0.40, "昨日": 0.40, "近7天": 0.50}


def _kws(topic) -> set:
    return {str(k).lower() for k in
            list((topic or {}).get("keywords") or []) +
            list((topic or {}).get("keywords_cn") or []) if str(k).strip()}


def _teams(topic) -> frozenset:
    """候选涉及的对阵（跨语言归一口径与已发侧完全一致）。

    两个来源取并集：
      1. keywords / keywords_cn —— 已发侧就是这么存的，必须能对上；
      2. **标题里出现的队名** —— LLM 给的 keywords 常是英文（`Arsenal` / `ManUtd`），
         而 `teams_in_tokens` 只认登记过的写法，实测 `ManUtd` 认不出来，
         结果整个对阵判据对英文稿失效。中文标题反而是最可靠的一处。
    """
    toks = list((topic or {}).get("keywords") or []) + \
           list((topic or {}).get("keywords_cn") or [])
    ents = set(team_names.teams_in_tokens(toks))
    for e in hot_topics.entities_in(str((topic or {}).get("title") or "")):
        if team_names.canonical_team(e):
            ents.add(team_names.canonical_team(e))
    return frozenset(ents) if len(ents) >= 2 else frozenset()


def event_keys_of(titles) -> set:
    """把标题批量转成事件键（无实体/无事件族的返回空串，直接丢掉）。

    空串必须丢掉而不是收进集合：否则「所有没有实体的稿」会被判成同一件事，
    一天只能发一篇无实体稿。
    """
    out = set()
    for t in titles or ():
        key = hot_topics.event_key(str(t or ""))
        if key and not key.endswith("#"):
            out.add(key)
    return out


def build_context(*, cross_batch_covered=None, topic_history=None,
                  yesterday_keywords=None) -> dict:
    """把三处历史数据整理成判据需要的形状（只整理，不判定）。"""
    covered = cross_batch_covered or {}
    hist = topic_history or {}

    day_titles = {str(t) for t in (covered.get("titles") or set()) if t}
    hist_titles = {str(t) for t in (hist.get("titles") or set()) if t}

    day_keys = {str(k) for k in (covered.get("hot_event_keys") or set()) if k}
    day_keys |= event_keys_of(day_titles)
    hist_keys = event_keys_of(hist_titles)

    return {
        "fixture_pairs": set(covered.get("fixture_pairs") or set()),
        "event_sigs": set(covered.get("event_signatures") or set())
                      | set(hist.get("event_signatures") or set()),
        "event_keys": day_keys | hist_keys,
        # 以下只用于告警，不参与删除
        "kw_today": {str(k).lower() for k in (covered.get("keywords") or set()) if k},
        "kw_yesterday": {str(k).lower() for k in (yesterday_keywords or set()) if k},
        "kw_hist": {str(k).lower() for k in (hist.get("keywords") or set()) if k},
        "titles_today": day_titles,
    }


def judge(topic, ctx: dict) -> tuple[str, str]:
    """单条判定 → (verdict, reason)。verdict ∈ {"ok", "drop"}。

    reason 是给台账用的短标签：``fixture`` / ``event`` / ``title`` / ``ok``。
    """
    title = str((topic or {}).get("title") or "")
    pair = _teams(topic)
    if pair and pair in (ctx.get("fixture_pairs") or set()):
        return "drop", "fixture"
    if pair and len(pair) == 2:
        # 赛果指纹（两队 + 比分）：换措辞重发同一场也拦得住
        if is_event_duplicate(title, ctx.get("event_sigs") or set()):
            return "drop", "fixture"
    if title[:30] and title[:30] in (ctx.get("titles_today") or set()):
        return "drop", "title"
    key = hot_topics.event_key(title)
    if key and not key.endswith("#") and key in (ctx.get("event_keys") or set()):
        return "drop", "event"
    return "ok", "ok"


def soft_warnings(topics, ctx: dict) -> list[dict]:
    """关键词重叠的**告警**（不删题）。返回 [{title, ratio, window}]。"""
    out = []
    buckets = (("今日", ctx.get("kw_today") or set(), WARN_RATIOS["今日"]),
               ("昨日", ctx.get("kw_yesterday") or set(), WARN_RATIOS["昨日"]),
               ("近7天", ctx.get("kw_hist") or set(), WARN_RATIOS["近7天"]))
    for t in topics or []:
        kws = _kws(t)
        if not kws:
            continue
        for name, pool, thr in buckets:
            if not pool:
                continue
            ratio = len(kws & pool) / max(len(kws), 1)
            if ratio >= thr:
                out.append({"title": str((t or {}).get("title") or "")[:30],
                            "ratio": round(ratio, 2), "window": name})
                break                            # 一条候选只报最严重的那一档
    return out


def apply(topics, ctx: dict) -> tuple[list[dict], dict]:
    """按顺序过滤，返回 (保留的候选, 漏斗 detail)。

    逐条处理而不是整批比对：**保留一条就把它的对阵键/事件键加进判据集合**，
    于是「批内重复」自动被"同一件事"覆盖，不需要单独一道批内 40% 过滤。
    """
    pairs = set(ctx.get("fixture_pairs") or set())
    keys = set(ctx.get("event_keys") or set())
    stats = {"同场": 0, "同事件": 0, "同标题": 0, "弱判据告警": 0}
    label = {"fixture": "同场", "event": "同事件", "title": "同标题"}
    kept: list = []
    for t in topics or []:
        verdict, reason = judge(t, {**ctx, "fixture_pairs": pairs, "event_keys": keys})
        if verdict == "drop":
            stats[label.get(reason, "同事件")] += 1
            print(f"   🗑️ 去重[{label.get(reason, reason)}]: 丢弃「{str((t or {}).get('title') or '')[:30]}」")
            continue
        kept.append(t)
        pair = _teams(t)
        if pair:
            pairs.add(pair)
        key = hot_topics.event_key(str((t or {}).get("title") or ""))
        if key and not key.endswith("#"):
            keys.add(key)

    warns = soft_warnings(kept, ctx)
    stats["弱判据告警"] = len(warns)
    for w in warns:
        print(f"   ⚠️ 关键词重叠 {w['ratio']:.0%}（{w['window']}）但不再删题：{w['title']}")
    return kept, stats


def selftest() -> list[tuple[str, bool]]:
    cases: list[tuple[str, bool]] = []
    ctx = build_context(
        cross_batch_covered={"fixture_pairs": {frozenset(["那不勒斯", "拉齐奥"])},
                             "titles": {"领跑，那不勒斯这一关"}},
        topic_history={"titles": {"C罗宣布退出国家队"}},
        yesterday_keywords={"拉齐奥", "那不勒斯", "意甲"},
    )
    T = lambda title, kws: {"title": title, "keywords": kws}  # noqa: E731

    # 同场（中文对阵）→ 丢
    v, r = judge(T("登顶遇上那不勒斯", ["拉齐奥", "那不勒斯"]), ctx)
    cases.append(("同场按对阵判重", v == "drop" and r == "fixture"))
    # 同事件（跨措辞）→ 丢
    v2, r2 = judge(T("C罗退出国家队后首度发声", ["C罗"]), ctx)
    cases.append(("同事件按事件键判重", v2 == "drop" and r2 == "event"))
    # 同一队、不同事情 → 关键词重叠很高也**不删**
    t3 = T("那不勒斯主帅下课", ["那不勒斯", "意甲", "主帅"])
    v3, _ = judge(t3, ctx)
    st = soft_warnings([t3], ctx)
    cases.append(("同队不同事不删（仅告警）", v3 == "ok" and bool(st)))
    # 无实体标题不参与事件判重（否则一天只能发一篇无实体稿）
    cases.append(("无实体标题不进事件键集合", event_keys_of({"某队宣布换帅"}) == set()))
    return cases


def main() -> int:
    import sys
    if "--selftest" in sys.argv:
        bad = 0
        for name, ok in selftest():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            bad += 0 if ok else 1
        print(f"\n自检 {len(selftest())} 项，失败 {bad} 项")
        return 1 if bad else 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
