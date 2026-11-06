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


def hits_all(text: str, entities: list[str]) -> tuple[bool, list[str]]:
    """entities 是否全部在 text 里出现（最长匹配消解串味）。

    实现已下沉到 ``data_chart.entity_hits``——**绘图侧与验收侧必须是同一个函数**。
    2026-11-06 实证：绘图侧（``data_chart.candidates``）用裸 ``in`` 判断，放行了
    数据源给的残缺队名「拉格斯拉维亚」（恰好是「布拉格斯拉维亚」的子串，图上因此
    印了错字）；验收侧这里用别名集判它「不在正文」，同一张图一边过一边不过，
    硬项 E 失败把整批发布挡掉，当天 0 篇。两套判据不可能对得上，只能留一套。

    返回 (是否全部命中, 未命中的实体)。
    """
    return _dc.entity_hits(text, entities)


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
