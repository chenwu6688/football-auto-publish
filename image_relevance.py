"""配图相关性闸门：图里出现的东西，必须在正文里出现过。

为什么需要独立一个模块
----------------------
2026-11-05 用户指令：「要保证图片的相关性，不能偏离文章主题」。

「相关」这件事不能靠肉眼验收，也不能靠生成端的自觉——图是数据画的，数据来自
``match_data``，而 ``match_data`` 里有当天所有比赛，跟这一篇文章有关的往往只有
一场。不设闸门的结果就是「文章写皇马引援、配图配皇马 2-1 瓦伦西亚」。所以：

**每张图自带 anchor（画了谁、哪个数字、数据来源），入库前逐条对正文核。**

判定分两级（都是硬门禁）
------------------------
1. ``entities`` 全部命中正文。这是相关性的核心：图里但凡有一支队是文章没提的，
   这张图就跟本文无关。
   匹配走**最长匹配消解**——否则「米兰」会命中「国际米兰」，把 AC 米兰的图
   配到国米的文章上（项目里 ``_teams_in_content`` 踩过同一个坑）。
2. ``numbers`` 至少命中一个（有数字时）。article 讲这场比赛，不可能既不提比分
   也不提名次/积分。一个都没命中 = 这张图的事实跟正文不在同一个话题上。

``numbers`` 全命中记 high 置信，部分命中记 medium（例如正文写「两球」而数据是 2）。
两种都放行——放行的依据是「实体全命中」，数字只用来分档留痕。

宁缺毋滥
--------
闸门不通过就**不出这张图**，退回信息图/新闻图，绝不放宽标准。少一张图的代价
远小于配错图——头条信用分里「配图与内容相关性差」是明列的扣分项。
"""

from __future__ import annotations

import re

import data_chart as _dc

# 数字 token：正文里的「27分」「27 分」「2-1」都会切出 27 / 2 / 1
_RE_DIGITS = re.compile(r"\d+")


def _article_text(art: dict) -> str:
    """标题 + 正文拼成检索文本。标题权重高，但我们只做存在性判断，不排序。"""
    if not isinstance(art, dict):
        return ""
    return f"{art.get('title') or ''}\n{art.get('content') or ''}"


def _spans(text: str, forms: set[str]) -> list[tuple[int, int, str]]:
    """所有「写法 → 出现位置」区间。"""
    out = []
    for f in forms:
        if len(f) < 2:
            continue
        start = text.find(f)
        while start != -1:
            out.append((start, start + len(f), f))
            start = text.find(f, start + 1)
    return out


_GLOBAL_CACHE: dict = {"text": None, "spans": None}


def _global_spans(text_low: str) -> list[tuple[int, int, str, str]]:
    """正文里**全部已登记球队**的写法命中：(start, end, form, 规范名)。

    为什么不能只看 anchor 里的实体：正文写「国际米兰」时，AC米兰的别名「米兰」
    确实躺在里面。只扫 anchor 自己的写法，就会把国米的文章配上 AC 米兰的图。
    必须把全词典的命中都摆上来，才能判断某个短写法是不是被更长的队名吞掉了。
    """
    if _GLOBAL_CACHE["text"] == text_low:
        return _GLOBAL_CACHE["spans"]

    spans: list[tuple[int, int, str, str]] = []
    for item in _dc.all_teams():
        owner = str(item.get("name_zh") or "").strip()
        if not owner:
            continue
        for f in _dc.entity_names(item):
            if len(f) < 2:
                continue
            pos = text_low.find(f)
            while pos != -1:
                spans.append((pos, pos + len(f), f, owner))
                pos = text_low.find(f, pos + 1)
    _GLOBAL_CACHE["text"] = text_low
    _GLOBAL_CACHE["spans"] = spans
    return spans


def _alive_owners(text_low: str) -> set[str]:
    """正文里"真正被提到"的球队（长写法优先吃掉短写法）。"""
    spans = sorted(_global_spans(text_low),
                   key=lambda x: (-(x[1] - x[0]), x[0]))
    covered: list[tuple[int, int, int]] = []      # (start, end, 长度)
    alive: set[str] = set()
    for s, t, _f, owner in spans:
        ln = t - s
        # 只有**严格更长**的区间能吞掉它；同长同位置（「皇马」与「皇家马德里」
        # 这种等价写法）都保留。
        if any(s >= cs and t <= ce and ln < cl for cs, ce, cl in covered):
            continue
        covered.append((s, t, ln))
        alive.add(owner)
    return alive


def hits_all(text: str, entities: list[str]) -> tuple[bool, list[str]]:
    """entities 是否全部在 text 里出现（最长匹配消解串味）。

    返回 (是否全部命中, 未命中的实体)。
    """
    text_low = (text or "").lower()
    if not text_low or not entities:
        return False, list(entities or [])

    alive = _alive_owners(text_low)
    missing = []
    for e in entities:
        item = _dc.resolve_team(e)
        if item:
            if str(item.get("name_zh") or "") in alive:
                continue
            missing.append(e)
        else:
            # 未登记球队：无法判断串味，退化为字面包含（宁可少配图也不错配）
            if e.lower() not in text_low:
                missing.append(e)
    return (not missing), missing


def numbers_hit(text: str, numbers: list[str]) -> tuple[int, list[str]]:
    """返回 (命中个数, 未命中的数字)。"""
    present = set(_RE_DIGITS.findall(text or ""))
    want = [str(n) for n in (numbers or [])]
    missing = [n for n in want if n not in present]
    return len(want) - len(missing), missing


def check(anchor: dict, art: dict) -> dict:
    """校验一张图的 anchor 是否与本文一致。

    返回 ``{ok, reason, confidence, entities_missing, numbers_missing}``。
    ``ok=False`` 的图**不得**进入正文，调用方应把 reason 记进台账。
    """
    result = {"ok": False, "reason": "", "confidence": "none",
              "entities_missing": [], "numbers_missing": []}

    if not isinstance(anchor, dict) or not anchor:
        result["reason"] = "缺 anchor，无法证明相关性"
        return result

    text = _article_text(art)
    if not text.strip():
        result["reason"] = "正文为空，未判定"
        return result

    entities = [str(e) for e in (anchor.get("entities") or []) if str(e).strip()]
    if not entities:
        # 没有实体的图（例如纯数字信息图）无法用实体判相关性，
        # 交给数字判据；两个判据都空 = 这张图无法归因，直接拒。
        if not (anchor.get("numbers") or []):
            result["reason"] = "anchor 既无实体也无数字，无法归因"
            return result
    else:
        ok_ent, missing = hits_all(text, entities)
        result["entities_missing"] = missing
        if not ok_ent:
            result["reason"] = "实体不在正文：" + "、".join(missing)
            return result

    numbers = [str(n) for n in (anchor.get("numbers") or []) if str(n).strip()]
    if numbers:
        n_hit, n_missing = numbers_hit(text, numbers)
        result["numbers_missing"] = n_missing
        if n_hit == 0:
            result["reason"] = ("数字与正文不符：" + "、".join(numbers)
                                + " 一个都没出现")
            return result
        result["confidence"] = "high" if not n_missing else "medium"
    else:
        result["confidence"] = "high"     # 无数字锚点的图，实体命中即视为相关

    result["ok"] = True
    result["reason"] = "实体全部命中" + ("；%d/%d 数字命中" % (len(numbers) -
                                             len(result["numbers_missing"]), len(numbers))
                                         if numbers else "")
    return result


def filter_candidates(cands: list[dict], art: dict) -> tuple[list[dict], list[dict]]:
    """把候选图分成 (通过的, 被拒的)。

    被拒的会带上 ``reject_reason``，调用方写进 ``degrade_events``——
    配图变少的时候必须能回答「为什么少」，否则又是黑箱。
    """
    kept, dropped = [], []
    for c in cands or []:
        r = check(c.get("anchor") or {}, art)
        if r["ok"]:
            kept.append({**c, "relevance": r})
        else:
            dropped.append({**c, "reject_reason": r["reason"], "relevance": r})
    return kept, dropped
