# -*- coding: utf-8 -*-
"""主稿生成失败原因归类（供选题漏斗 `generated` 层落 detail）。

## 为什么要有这个模块

`_generate_articles_from_topics` 一直把失败原因写进 `stats["issues"]` 并打印，
但漏斗只记了「失败几篇」，没记「为什么」。2026-11-09 复盘时就卡在这里：

* 11-05 单日 8 次失败（失败率 80%），想查成因；
* 原因只存在于 runner 的 stdout —— Actions 日志匿名拿不到（403），
  而那几批在 CI 眼里是 `success`（0 篇不算失败），连告警都没有；
* 本地日志只有初始化行。

结果就是**有原因、没证据**。所以把归类口径固定成一处，由漏斗落 `失败原因` detail，
下次不用再翻日志。

## 口径

**按代码自己的错误字面量前缀归类**，不做关键词模糊匹配。
下面的错误串全部来自 `generate_article_with_retry` / `_rewrite_with_retry` /
`_gen_one` 的 `return` 语句，是**闭集**，不是 LLM 自由文本，所以前缀匹配是安全的。
新增 `return {}, "...失败..."` 文案时这里要同步 —— 单测会红，不会静默漏。

三种兜底要分开，别合并（「返回空必须区分真没数据与被拦」的同款）：

* `原因缺失` —— 失败但没带 error 串，是**漏记**，不是「其他原因」；
* `其他` —— 有 error 串但不认识，说明出现新文案，**该补词表**；
* 正常类别 —— 命中闭集。

把 `原因缺失` 混进 `其他` 会让「词表没跟上」和「压根没传原因」这两种完全不同的
缺陷长得一样，查的时候又得靠猜。
"""

from __future__ import annotations

#: 失败类别（顺序即匹配优先级，先命中者胜）。
#: 前缀全部取自 orchestrator 的真实 return 文案，改动那边要回来同步。
KINDS: tuple[tuple[str, str], ...] = (
    ("熔断", "合规熔断中"),
    ("预检跳过", "未找到与话题"),
    ("源文缺失", "Pipeline A：未找到"),
    ("数据源不可用", "Pipeline A 不可用"),
    ("事实闸门", "事实矛盾"),
    ("反洗稿", "反洗稿检查未过"),
    ("标题评分", "标题评分未过"),
    ("诱导话术", "诱导话术未过"),
    ("改写异常", "改写异常"),
    ("改写失败", "改写失败"),
)

#: 失败但没带原因串（漏记）。
MISSING = "原因缺失"
#: 有原因串但不在闭集内（该补 KINDS）。
UNKNOWN = "其他"


def classify(error) -> str:
    """把一条失败 error 串归到类别。空/非字符串 → `原因缺失`。"""
    if not isinstance(error, str):
        return MISSING
    text = error.strip()
    if not text:
        return MISSING
    for kind, prefix in KINDS:
        if text.startswith(prefix):
            return kind
    return UNKNOWN


def count(errors) -> dict:
    """统计一组 error 的原因分布。只返回出现过的类别，按次数降序、同数按名排序。

    返回空 dict 而不是各类补 0 —— 「这一层没调过」和「调了但零失败」必须能区分开
    （漏斗台账的老规矩：未调用记缺失不记 0）。
    """
    tally: dict = {}
    for e in errors or ():
        kind = classify(e)
        tally[kind] = tally.get(kind, 0) + 1
    return dict(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])))


def selftest() -> list[tuple[str, bool]]:
    """冒烟自检（不依赖 orchestrator，纯函数）。"""
    cases: list[tuple[str, bool]] = []
    cases.append(("闭集全覆盖", all(
        classify(prefix) == kind for kind, prefix in KINDS)))
    cases.append(("空串记原因缺失", classify("") == MISSING))
    cases.append(("None 记原因缺失", classify(None) == MISSING))
    cases.append(("陌生文案记其他", classify("某个还没登记的新失败") == UNKNOWN))
    cases.append(("不按子串误判", classify("顺带提了一句事实矛盾但开头不是它") == UNKNOWN))
    cases.append(("计数只列出现过的类别", count(["事实矛盾: x"]) == {"事实闸门": 1}))
    cases.append(("空输入返回空 dict", count([]) == {}))
    return cases


if __name__ == "__main__":  # pragma: no cover
    bad = [n for n, ok in selftest() if not ok]
    print("failure_taxonomy 自检：" + ("全绿" if not bad else "失败 " + "、".join(bad)))
    raise SystemExit(1 if bad else 0)
