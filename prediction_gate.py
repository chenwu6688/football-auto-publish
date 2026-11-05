#!/usr/bin/env python3
"""赛前预测稿的发布判定（**唯一**口径）。

## 为什么要有这个模块（2026-11-05 晚间事故）

一批 3 篇成稿，2 篇被 `score` 字段撞车丢掉，唯一活下来的是赛前预测稿；
而 publisher 对预测稿是**整类硬跳过**（R13 决策④）。于是本批 0 篇可发
→ `sys.exit(1)` → 整批判失败，批次还被写成「已完成」，补发被幂等跳过。

复盘真正的问题不是「预测稿该不该发」，而是**用体裁替代了质量判据**：

  · 该拦的是「无据断言」——"稳了""必赢""精准预测 2-0"。说错了无法追溯，
    下一次读者就不信了；
  · 不该拦的是「有据前瞻」——近期战绩、积分排名、历史交锋摆在前面，
    结论跟着数据走，本质上是一篇信息整理。

当前预测稿之所以该拦，是因为它**生成时就没有任何事实输入**：`collect_future_matches`
只返回对阵名单（league/home/away/utc），LLM 只能写"从近期表现来看"。
所以在现状下"拦"是对的，但"按体裁拦"是错的——一旦哪天真给它喂了积分榜，
这条规则会继续把它挡在门外，而且没人能从日志里看出为什么。

## 三档（按依据分级，不按体裁分级）

| tier             | 判据                                                     | 发 |
|------------------|----------------------------------------------------------|----|
| not_prediction   | 非预测稿                                                  | 发 |
| sourced_forward  | basis ≥ 2 项 且 标题无断言词 且 不押具体比分               | 发（批内 ≤1）|
| opinion_only     | basis < 2 项（只有对阵名单，观点稿）                        | 不发 |
| assertive        | 标题出现「必赢/稳了/精准预测」等断言词                      | 不发 |
| score_guess      | 标题或正文押了具体比分（"我看 2-0"）                        | 不发 |

「不发」不等于「失败」：这是设计内的终态，必须落台账并写清原因
（`skipped_prediction` + reason 里带 tier），否则就是又一次「有结论没证据」。

## 为什么 basis 从 frontmatter 读而不是现场重算

发布阶段的进程里没有采集上下文（`match_data` 在生成进程里）。所以生成侧把
**已经核对过的事实条数**写进 frontmatter（`prediction_basis: 3` + 明细
`prediction_basis_items: 积分排名, 近期战绩`），发布侧只做判定，不重新采集。
明细一起落，是为了让「3 项依据」可核对而不是自报一个数字。

## 与配额/看板的口径

`excluded()` 是「这篇算不算内容供给」的唯一判据，被 publisher 与
verify_batch_quota / verify_supply_mix / build_metrics_dashboard / check_perf_lag 共用。
此前这四处各写一遍 `not a.get("is_prediction")`，是同一规则写四遍——
改了生成侧而漏改其中一处，分母就会各说各话。
"""

from __future__ import annotations

import re

#: 预测稿标题里**不许出现**的断言词。命中即不发，原因写 assertive。
#: 只收「把不确定说成确定」的词；「能否/会不会」这类留白不算断言。
ASSERTION_KW = (
    "必赢", "必胜", "稳赢", "稳了", "包赢", "稳胆", "铁定",
    "十拿九稳", "锁定胜局", "毫无悬念", "必进", "必爆",
    "精准预测", "百分百", "包中",
)

#: 押具体比分（"我看 2-0""预测 1比1"）。比分是最容易被打脸、也最无法校验的断言形态：
#: fact_assert 对前瞻本来就是 warn-only，等于没人核。故整类不发。
#:
#: ⚠️ 触发词只能收**前瞻性**的（预测/看好/押/估计/会是）。不能用「打成/打出/比分」：
#: 它们是**回顾**语境（"上回合打成 2-0"），那是可核对的事实，不是打赌。
#: 自检里专门有一条钉这个区分——第一版就是这么误判的。
_SCORE_GUESS_RE = re.compile(
    r"(?:预测|看好|押|估计|会是|我觉得|老六觉得)[^。！？\n]{0,8}?"
    r"(\d{1,2})\s*[-–—:：比]\s*(\d{1,2})"
)

#: 批内最多允许几篇「有据前瞻」进发布队列。配额口径是「常态 2 篇/批」，
#: 预测稿占掉 1 篇就等于把主稿挤掉 1 篇，所以固定上限 1。
MAX_PUBLISHABLE_PER_BATCH = 1

#: 有据前瞻的 basis 下限：1 项只能支撑一句描述，撑不起一篇 600 字。
MIN_BASIS = 2


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("true", "1", "yes")


def is_prediction(meta: dict) -> bool:
    """成稿级 / 选题级两个标记都认（历史数据只有选题级）。"""
    m = meta or {}
    return _truthy(m.get("is_prediction")) or _truthy(m.get("_is_prediction"))


def basis_of(meta: dict) -> list[str]:
    """读「这篇稿子手里有几项已核对事实」。

    只认结构化字段，不做作文本推断：从正文里"感觉"它引用了数据，等于自己给自己发证。
    兼容两种写法：`prediction_basis`（条数）+ `prediction_basis_items`（明细）。
    条数与明细不一致时**取小**——宁可少算，也不让一个数字把稿子送出门。
    """
    m = meta or {}
    items_raw = m.get("prediction_basis_items") or ""
    if isinstance(items_raw, (list, tuple)):
        items = [str(x).strip() for x in items_raw if str(x).strip()]
    else:
        items = [x.strip() for x in re.split(r"[,，、;；|]", str(items_raw)) if x.strip()]
    try:
        n = int(str(m.get("prediction_basis") or 0).strip())
    except (TypeError, ValueError):
        n = 0
    n = max(0, n)
    if items:
        n = min(n, len(items)) if n else len(items)
    return items[:n] if n else []


def assess(meta: dict, body: str = "") -> dict:
    """判定一篇成稿的发布归宿。

    返回 ``{publish, tier, reasons, basis}``；``publish=False`` 一律带原因，
    调用方把它写进台账的 reason，而不是只写"预测稿"三个字。
    """
    m = meta or {}
    if not is_prediction(m):
        return {"publish": True, "tier": "not_prediction", "reasons": [], "basis": []}

    title = str(m.get("title") or "")
    basis = basis_of(m)

    hit = [w for w in ASSERTION_KW if w in title]
    if hit:
        return {"publish": False, "tier": "assertive", "basis": basis,
                "reasons": [f"标题用了断言词「{'、'.join(hit[:3])}」："
                            f"赛前把不确定说成确定，说错无法追责"]}

    sg = _SCORE_GUESS_RE.search(title) or _SCORE_GUESS_RE.search(body or "")
    if sg:
        return {"publish": False, "tier": "score_guess", "basis": basis,
                "reasons": [f"押了具体比分（{sg.group(1)}-{sg.group(2)}）："
                            f"比分是唯一无法自证的预测形态，不打这个赌"]}

    if len(basis) >= MIN_BASIS:
        return {"publish": True, "tier": "sourced_forward", "basis": basis,
                "reasons": [f"有据前瞻（{len(basis)} 项：" + "、".join(basis) + "）"]}

    return {"publish": False, "tier": "opinion_only", "basis": basis,
            "reasons": [f"依据不足（{len(basis)}/{MIN_BASIS} 项，只有对阵名单）："
                        f"写出来只能是模型观点，不发"]}


def excluded(meta: dict, body: str = "") -> bool:
    """这篇**不计入内容供给**（配额/配比/看板的分母）→ True。

    只有「预测稿且判定不发」才排除；有据前瞻一旦能发，它就是正常内容，
    不能继续从分母里剔掉（否则「今天发了几篇」会少算一篇）。
    """
    if not is_prediction(meta):
        return False
    return not assess(meta, body)["publish"]


def counts_as_content(article: dict, pred_titles=()) -> bool:
    """这篇**算不算一条内容供给**（配额/配比/看板/绩效的分母）。True = 算。

    看板、配额验收、供给配比、绩效滞后四处此前各写一遍
    ``not a.get("is_prediction")``，是同一规则写四遍。改成体裁分级以后，
    那四处必须**同时**跟着变，否则「今天发了几篇」会各说各话——
    所以口径收敛到这里，谁要判就调这里。

    优先级：
      1. 成稿级字段在（metadata 里有 ``is_prediction`` 键）→ 按依据分级判；
      2. 历史数据没有这个键 → 用选题级 ``_is_prediction`` 的标题对拍兜底
         （那时唯一的证据就是选题上的标记）。
    """
    a = article or {}
    if "is_prediction" in a:
        return not excluded(a)
    t = (a.get("title") or "").strip()
    return t not in set(pred_titles or ())


def quota_left(already_publishable: int) -> int:
    """本批还能放几篇预测稿进来。"""
    return max(0, MAX_PUBLISHABLE_PER_BATCH - int(already_publishable or 0))


def selftest() -> list:
    """正反例自检；改词表/阈值必须同步改这里。返回 [(name, ok)]。"""
    cases = []

    def ck(name, cond):
        cases.append((name, bool(cond)))

    base = {"title": "勒沃库森近5轮4胜，马赛客场只丢3球", "is_prediction": "true"}
    ck("非预测稿直接放行",
       assess({"title": "x", "is_prediction": "false"})["publish"] is True)
    ck("只有对阵名单 → 不发（依据不足）",
       assess(base)["tier"] == "opinion_only")
    ck("断言词 → 不发",
       assess({**base, "title": "这场稳了，勒沃库森必赢马赛"})["tier"] == "assertive")
    ck("押比分 → 不发",
       assess(base, body="我看好 2-0，勒沃库森赢两球。")["tier"] == "score_guess")
    ck("比分在前瞻里出现但不是押注（'上回合 2-0'）不算",
       assess(base, body="上回合两队打成 2-0，这次马赛换了三中卫。")["tier"] == "opinion_only")
    ok = assess({**base, "prediction_basis": "3",
                 "prediction_basis_items": "积分排名,近期战绩,历史交锋"})
    ck("三项依据 → 可发", ok["publish"] is True and ok["tier"] == "sourced_forward")
    ck("只报条数没明细 → 按明细算，不算自报",
       assess({**base, "prediction_basis": "3"})["publish"] is False)
    ck("明细多于条数 → 取条数",
       len(basis_of({**base, "prediction_basis": "1",
                     "prediction_basis_items": "a,b,c"})) == 1)
    ck("条数为脏值 → 当缺失",
       basis_of({**base, "prediction_basis": "abc"}) == [])
    ck("excluded：有据前瞻不进排除",
       excluded({**base, "prediction_basis": "2",
                 "prediction_basis_items": "积分排名,近期战绩"}) is False)
    ck("excluded：观点稿排除", excluded(base) is True)
    ck("excluded：非预测稿不排除", excluded({"title": "x"}) is False)
    ck("配额：批内只放 1 篇", quota_left(0) == 1 and quota_left(1) == 0)
    return cases


if __name__ == "__main__":
    import sys
    bad = [n for n, ok in selftest() if not ok]
    for n, ok in selftest():
        print(("  PASS " if ok else "  FAIL ") + n)
    print(f"\n{len(selftest()) - len(bad)}/{len(selftest())} 通过")
    sys.exit(1 if bad else 0)
