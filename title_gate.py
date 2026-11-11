#!/usr/bin/env python3
"""标题闸门（P0-1，2026-11-01 头部账号对标）：疑问收尾限额 + 过程锚点统计。

## 为什么要立这条闸门

2026-11-01 抓了 4 个头条足球头部号共 181 条真实作品，跟我方同期 76 篇对比：

    账号            粉丝    中位阅读  过千率   锚点覆盖  疑问句
    英超那些事     15.0万    5,526    100%    35.7%     0%
    我爱英超       10.3万    1,090     56%    13.1%    20.2%
    西甲二人转     23.0万       77     10%     7.2%     8.4%
    我方（~0 粉）      —          8      0%     2.6%    85.5%

差异最大的一维不是粉丝、不是字数，而是**标题有没有给人一个点开的理由**：
头部靠「让3追4」「第94分钟」「连丢3球」「球迷第80分钟退场」这种**过程反常**，
我方 76 篇里 0 篇有，却有 85.5% 以「…他到底图个啥？」这种**没有过程的空问**收尾。

## 更难看的事实：这是我们自己让模型这么写的

不是模型偏爱疑问句，是供给侧明写着「必须以疑问？收尾」：

    - orchestrator.generate_fixture_article   「…以疑问『？』收尾」×2（规则 + JSON 模板）
    - orchestrator.generate_recap_article     「…以疑问『？』收尾」×2
    - orchestrator.generate_event_result_article 「…以疑问『？』收尾」×2
    - orchestrator.score_title_plan           「疑问钩子 +20」（满分项之一）
    - orchestrator.score_fixture_title        「疑问收尾 30 分」（权重最高的一维）
    - prompts/topic_selector.txt              「疑问/悬念类占比 ≥ 60%」

所以本闸门**不是**唯一手段：prompt 与评分器已经同步改成「锚点优先、疑问限额」，
本模块只做出口兜底——因为模型仍会往容易的句式上收敛（同 10-13 句式闸门的教训）。

## 两条判定

1. **疑问收尾限额**（硬拦）：14 天内疑问收尾已满 ``MAX_QUESTION_PER_WINDOW``（2）篇
   → 本条再以疑问收尾即打回。这是计划 P0-1 第 3 条的原文口径。
2. **过程锚点只统计、不拦**（刻意）：锚点覆盖率不设逐篇门槛，理由有两条硬证据——
   - 头部 4.2 万阅读爆款「主场0-3大败！意甲劲旅彻底拉胯，11战仅2胜…」**没有锚点**
     （「11战仅2胜」是战绩不是过程）；
   - 视频号中位 10,008 播放，锚点覆盖率只有 2.5%。
   设成逐篇必要条件会误杀大量合理的战绩型标题。覆盖率是否达标由
   ``scripts/verify_title_anchor.py`` 按「近 7 天成稿 ≥40%」在**批次层面**验收。

## 纪律

- 配置不可用 → 放行并置 ``degraded``，绝不因读不到配置阻塞发布（计划九）；
- 历史标题读不到（本机没 output/）→ 放行，不因读不到历史而卡死；
- 锚点词表唯一来源是 ``title_features.PROCESS_ANCHOR_PATTERNS``，本模块不另写正则
  —— 全仓已经因为「比分口径三处各写各的」在 10-24 熔断过一次。

命令行：
    python3 title_gate.py --selftest
    python3 title_gate.py --title "第94分钟丢球，阿森纳遭读秒绝杀"
    python3 title_gate.py --stats --days 7
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import title_features

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "title_antipattern.yaml"
OUTPUT_DIR = PROJECT_ROOT / "output"

CST = timezone(timedelta(hours=8))

# 计划 P0-1 第 3 条原文：任意 14 天内疑问收尾 ≤2 篇。
WINDOW_DAYS = 14
MAX_QUESTION_PER_WINDOW = 2

# 出口复核生效日（2026-11-11）：save_articles_local 第一次真的判 title_gate。
#
# 为什么这个日期要单独定、而不是用 TITLE_SINCE：在此之前限额只在 4 个评分器里
# 「按通道」生效，出口（所有通道唯一汇聚点）根本没判过 —— 11-09「曼联4-4维拉，
# 多古补时救主还是遮羞？」就是从缺这条复核的通道漏出去的，而 metadata 里
# title_gate.pass 还是「不传 recent_titles」的裸分，事后完全看不出漏了。
# 两个验收脚本（verify_title_anchor 的 D 项、verify_publish_gate 的 A1）
# 都从这里取，避免「同一生效日在两处各写一遍」——那必然只改一处。
EXPORT_GATE_SINCE = "2026-11-11"

# 兜底：配置里没写时用这两个值（与 config/title_antipattern.yaml 的「规则三」同源）
_FALLBACK = {
    "规则名": "无过程锚点空问式",
    "窗口天数": WINDOW_DAYS,
    "限额": MAX_QUESTION_PER_WINDOW,
    "提示": ("标题不要再以疑问句收尾。把「过程」写进标题——第几分钟、连进几球、谁被换下、"
             "谁第几次上场；过程本身才是让人点开的理由，「他到底图个啥？」不是。"),
}


# ------------------------------------------------------------------ 基础判定
def is_question_ending(title: str) -> bool:
    """是否**以疑问收尾**。

    刻意不用「含问号」：中段设问（「凭什么是他？数据摆在这」）是有效手法，
    头部的疑问句样本里也多是这种。限额要卡的是「整句就是个空问」这一形态，
    所以判据落在句末。
    """
    t = (title or "").strip()
    if not t:
        return False
    t = t.rstrip("。.!！…~～ ")
    return t.endswith("？") or t.endswith("?") or t[-1:] in ("吗", "呢")


def anchor_kinds(title: str) -> tuple[str, ...]:
    """命中的过程锚点类别（唯一口径来自 title_features）。"""
    return title_features.extract(title).anchor_kinds


def has_anchor(title: str) -> bool:
    return title_features.extract(title).has_anchor


def count_question_endings(titles) -> int:
    return sum(1 for t in (titles or ()) if is_question_ending(t))


def quota_exhausted(titles, *, limit: int | None = None) -> bool:
    """窗口内疑问收尾是否已用满。"""
    lim = MAX_QUESTION_PER_WINDOW if limit is None else int(limit)
    return count_question_endings(titles) >= lim


# ------------------------------------------------------------------ 配置
_CACHE: dict = {}


def load_config(path: Path | str | None = None) -> dict | None:
    """读 config/title_antipattern.yaml 的「规则三」。

    整个文件读不到返回 None（调用方据此放行）；文件在但没有规则三，用 _FALLBACK。
    """
    p = Path(path or CONFIG_PATH)
    key = str(p)
    if key in _CACHE:
        return _CACHE[key]
    cfg: dict | None = None
    try:
        import yaml
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                block = data.get("规则三")
                cfg = block if isinstance(block, dict) and block else dict(_FALLBACK)
    except Exception:
        cfg = None
    _CACHE[key] = cfg
    return cfg


def anchors_declared(cfg: dict | None = None) -> list[str]:
    """配置里声明的锚点类别名（应与 title_features 的类别名一致，供验收脚本比对）。"""
    cfg = cfg if cfg is not None else (load_config() or _FALLBACK)
    return [str(x) for x in (cfg.get("锚点类别") or []) if str(x).strip()]


def available_anchor_kinds() -> list[str]:
    return [k for k, _ in title_features.PROCESS_ANCHOR_PATTERNS]


# ------------------------------------------------------------------ 闸门
def check(title: str, titles=None, *, cfg: dict | None = None) -> dict:
    """判定标题是否可以放行。

    title   待判标题
    titles  近 WINDOW_DAYS 天已发标题（含本批已生成的），用来算限额。
            传 None 表示「不判限额」（只看格式），用于单条冒烟。

    返回 dict：
      ok        True = 放行
      hit       bool，是否命中
      rule      命中的规则名
      degraded  配置不可用已放行
      reason / hint / detail
      quota     {"limit", "used", "exhausted"} 便于打回时把话说清
    """
    t = (title or "").strip()
    if not t:
        return {"ok": True, "hit": False, "degraded": False, "rule": None,
                "reason": "", "hint": "", "detail": ["标题为空，不判定"], "quota": None}

    raw = load_config()
    degraded = raw is None
    cfg = raw or _FALLBACK
    rule = cfg.get("规则名") or _FALLBACK["规则名"]
    lim = int(cfg.get("限额", MAX_QUESTION_PER_WINDOW) or MAX_QUESTION_PER_WINDOW)
    hint = str(cfg.get("提示") or _FALLBACK["提示"])

    if titles is None:
        return {"ok": True, "hit": False, "degraded": degraded, "rule": rule,
                "reason": "", "hint": hint,
                "detail": ["未提供历史标题（不判限额），放行"], "quota": None}

    used = count_question_endings(titles)
    quota = {"limit": lim, "used": used, "exhausted": used >= lim}
    # 配置读不到也照判：限额本身是代码常量，只是标记 degraded，
    # 让验收脚本能看出「这次判定用的是兜底口径」，而不是静默跳过。

    if is_question_ending(t) and quota["exhausted"]:
        kinds = anchor_kinds(t)
        return {
            "ok": False, "hit": True, "degraded": degraded, "rule": rule,
            "reason": (f"近 {WINDOW_DAYS} 天疑问收尾已 {used} 篇（限额 {lim} 篇），"
                       f"本篇又以疑问收尾"),
            "hint": hint, "quota": quota,
            "detail": [f"命中「{rule}」：疑问收尾限额已满",
                       f"本篇锚点：{('、'.join(kinds)) if kinds else '无'}"],
        }

    return {"ok": True, "hit": False, "degraded": degraded, "rule": rule,
            "reason": "", "hint": hint, "quota": quota,
            "detail": [f"疑问收尾 {used}/{lim}，放行"]}


# ------------------------------------------------------------------ 统计
def coverage(titles) -> dict:
    """批次层面的标题质量统计（验收脚本与周报共用）。

    返回 {"n", "anchors", "anchor_coverage", "question_endings", "question_pct",
          "anchor_kinds": {类别: 篇数}}
    空样本时各项为 None，**不填 0**——「没有样本」和「一条都没命中」是两件事。
    """
    items = [t for t in (titles or ()) if (t or "").strip()]
    if not items:
        return {"n": 0, "anchors": None, "anchor_coverage": None,
                "question_endings": None, "question_pct": None, "anchor_kinds": {}}
    kinds_count: dict[str, int] = {}
    anchors = 0
    for t in items:
        ks = anchor_kinds(t)
        if ks:
            anchors += 1
        for k in ks:
            kinds_count[k] = kinds_count.get(k, 0) + 1
    q = count_question_endings(items)
    return {
        "n": len(items),
        "anchors": anchors,
        "anchor_coverage": round(100 * anchors / len(items), 1),
        "question_endings": q,
        "question_pct": round(100 * q / len(items), 1),
        "anchor_kinds": kinds_count,
    }


def recent_titles(output_dir=None, end_date: str | None = None,
                  days: int = WINDOW_DAYS, since: str | None = None) -> list[str]:
    """近 N 天已发标题（复用 template_gate 的读取口径，预测稿不计）。

    读不到目录/日期非法 → 返回空列表（闸门据此放行，不阻塞）。
    """
    import template_gate
    end = end_date or datetime.now(CST).strftime("%Y-%m-%d")
    return template_gate.recent_titles(output_dir or OUTPUT_DIR, end, days=days, since=since)


def window_bounds(end_date: str | None = None, days: int = WINDOW_DAYS) -> tuple[str, str]:
    """返回 (since, until) 日期字符串，便于报告里把窗口写清楚。"""
    try:
        end = datetime.strptime(end_date or datetime.now(CST).strftime("%Y-%m-%d"),
                                "%Y-%m-%d").date()
    except Exception:
        end = datetime.now(CST).date()
    return (end - timedelta(days=days - 1)).strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# ------------------------------------------------------------------ 自检
def selftest() -> list[tuple[str, bool]]:
    """冒烟自检：正例必拦、负例必放、降级必放行。用例全部来自真实数据。"""
    cases: list[tuple[str, bool]] = []

    # 疑问收尾判定：我方真实标题（85.5% 都是这一类）
    for t in ("米兰德比谁先崩？国米22分压着米兰，两位主帅的底牌竟然差在这？",
              "当年那个追风少年曲波，如今在基层默默带娃，他到底图个啥？",
              "马拉多纳的上帝之手，英格兰到底冤不冤呢",
              "为什么说这场球输得比赢更值？"):
        cases.append((f"疑问收尾：{t[:18]}", is_question_ending(t) is True))

    # 非疑问收尾：头部爆款（含中段设问但句末不是问号）
    for t in ("让3追4！拜仁史诗级大逆转，30分钟连轰4球，凯恩替补完成绝杀！",
              "第94分钟丢球，阿森纳遭读秒绝杀！领先只剩3分，曼城看到希望！",
              "凭什么是他？金球奖这一夜，凯恩用73球堵住了所有嘴",
              "德国杯：拜仁6-1大胜马格德堡，多点开花挺进1/8决赛"):
        cases.append((f"非疑问收尾：{t[:18]}", is_question_ending(t) is False))

    # 限额：窗口内 2 篇疑问收尾即满额
    hist2 = ["他到底图个啥？", "这场谁更慌呢"]
    cases.append(("2 篇疑问收尾 → 限额用满", quota_exhausted(hist2) is True))
    cases.append(("1 篇疑问收尾 → 未满额", quota_exhausted(hist2[:1]) is False))
    cases.append(("空历史 → 未满额", quota_exhausted([]) is False))

    # 限额已满 → 疑问标题必须被拦
    r = check("这场德比到底谁先崩？", hist2)
    cases.append(("限额满 + 疑问收尾 → 拦", r["ok"] is False and r["hit"] is True))
    cases.append(("拦的理由写清了几篇/限额", "2 篇" in r["reason"] and "限额 2" in r["reason"]))

    # 限额已满 → 带锚点的标题照样放行（锚点不是门票，但也不该被疑问限额连累）
    r2 = check("第94分钟丢球，阿森纳遭读秒绝杀！领先只剩3分", hist2)
    cases.append(("限额满 + 有锚点非疑问 → 放", r2["ok"] is True))

    # 限额未满 → 疑问收尾放行
    r3 = check("这场德比到底谁先崩？", hist2[:1])
    cases.append((f"限额未满（{r3['quota']['used']}/{r3['quota']['limit']}）→ 放",
                  r3["ok"] is True))

    # titles=None 时只看格式，不判限额
    r4 = check("这场德比到底谁先崩？", None)
    cases.append(("不给历史 → 不判限额，放行", r4["ok"] is True and r4["quota"] is None))

    # 锚点口径与 title_features 同源（防止两处各写各的）
    cases.append(("锚点类别数 = title_features 类别数",
                  len(available_anchor_kinds()) == len(title_features.PROCESS_ANCHOR_PATTERNS)))
    cases.append(("「让3追4」命中锚点", has_anchor("让3追4！拜仁史诗级大逆转") is True))
    cases.append(("「11战仅2胜」不算锚点（战绩非过程）", has_anchor("11战仅2胜，头号球星带头反水") is False))

    # 覆盖率：空样本不填 0
    cov = coverage([])
    cases.append(("空样本覆盖率是 None 不是 0", cov["anchor_coverage"] is None))
    cov2 = coverage(["第94分钟丢球，阿森纳遭读秒绝杀", "他到底图个啥？", "德国杯：拜仁6-1大胜"])
    cases.append(("覆盖率可复算（1/3=33.3%）", cov2["anchor_coverage"] == 33.3))
    cases.append(("疑问收尾可复算（1/3=33.3%）", cov2["question_pct"] == 33.3))
    cases.append(("命中类别可分项统计", cov2["anchor_kinds"].get("补时/读秒") == 1))

    # 降级：配置不可用也必须按代码常量继续判限额，且标记 degraded
    _orig = globals()["load_config"]
    globals()["load_config"] = lambda *a, **k: None
    try:
        rd = check("这场德比到底谁先崩？", hist2)
        rd2 = check("第94分钟丢球，阿森纳遭读秒绝杀", hist2)
    finally:
        globals()["load_config"] = _orig
    cases.append(("配置不可用 → 限额仍生效且标记 degraded",
                  rd["ok"] is False and rd["degraded"] is True))
    cases.append(("配置不可用 → 非疑问照样放行", rd2["ok"] is True))

    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="标题闸门（疑问收尾限额 + 过程锚点统计）")
    ap.add_argument("--title", help="判定单条标题")
    ap.add_argument("--titles", nargs="*", help="作为「近 14 天历史标题」参与限额判定")
    ap.add_argument("--stats", action="store_true", help="统计近 N 天成稿标题质量")
    ap.add_argument("--days", type=int, default=7, help="统计窗口天数（默认 7）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        bad = 0
        for name, ok in selftest():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            bad += 0 if ok else 1
        print(f"\n自检结果：{'全部通过 ✅' if not bad else f'{bad} 项未通过 ❌'}")
        return 1 if bad else 0

    if args.title:
        r = check(args.title, args.titles)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1

    if args.stats:
        since, until = window_bounds(None, args.days)
        titles = recent_titles(days=args.days)
        cov = coverage(titles)
        print(f"窗口 {since} ~ {until}：样本 {cov['n']} 篇")
        if not cov["n"]:
            print("  （无成稿，无法统计——不拿 0 冒充）")
            return 2
        print(f"  过程锚点覆盖：{cov['anchors']}/{cov['n']} = {cov['anchor_coverage']}%")
        print(f"  疑问收尾：{cov['question_endings']}/{cov['n']} = {cov['question_pct']}%")
        if cov["anchor_kinds"]:
            top = sorted(cov["anchor_kinds"].items(), key=lambda kv: -kv[1])
            print("  命中类别：" + "、".join(f"{k}×{v}" for k, v in top))
        return 0

    cfg = load_config() or _FALLBACK
    print(f"标题闸门 v1（规则：{cfg.get('规则名', _FALLBACK['规则名'])}）")
    print(f"  窗口 {WINDOW_DAYS} 天 · 疑问收尾限额 {cfg.get('限额', MAX_QUESTION_PER_WINDOW)} 篇")
    print(f"  锚点类别 {len(available_anchor_kinds())} 类（唯一口径 title_features）")
    print(f"  配置声明类别：{'、'.join(anchors_declared(cfg)) or '（未声明）'}")
    print("\n冒烟自检：")
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检结果：{'全部通过 ✅' if not bad else f'{bad} 项未通过 ❌'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
