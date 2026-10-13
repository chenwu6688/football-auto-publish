#!/usr/bin/env python3
"""选题分类与类别优先级（计划 5.1 / R11 内容诊断）。

背景（R11 内容诊断：869 条头条真实导出回测，2026-01-01~10-12）：
  有效选题 = 转会官宣 CTR 2.08% / 冲突舆情 1.55% / 中国足球 1.36%；
  战报比分式内容占产量 23.9%、CTR 仅 0.49%、0 爆款——必须减产。
本模块把该结论落成可执行、可验收的选题分类与类别优先级：
  - classify(topic)                 判定一条选题所属类别（只分类，不生成、不介入选题）
  - classify_detail(topic)          分类明细 {category, signals, degraded}（供验收/调试）
  - prioritize(topics)              按类别优先级重排 + 战报比分软上限裁剪 → (kept, dropped)
  - warn_category_distribution(topics)  非阻断分布监控（告警，不删题）

判定顺序（**形态优先于主题**）：
  ① 标题命中「比分模式」或「赛果模式」         → 战报比分
  ② content_type == "转会资讯" 或命中转会关键词  → 转会官宣
  ③ 命中冲突关键词                             → 冲突舆情
  ④ 命中中国足球关键词                         → 中国足球
  ⑤ 其余                                       → 未分类
先判形态的原因：一条带比分的「国安3-1大胜」即便主题是中国足球，其点击形态
仍是战报比分式（R11 回测中该形态 0 爆款）——先按形态归入战报比分；联赛名
不罚（中国足球词表照常命中主题类），中国足球本身是有效选题，只有比分/赛果
开场形态才减产。

纪律（与全仓一致）：
  - 配置驱动：词表 / 优先级 / 软上限 / 阈值全部在 config/topic_category.yaml；
  - 配置不可用 → 走 _FALLBACK 最小词表继续判定并标 degraded，绝不阻塞发布（计划九）；
  - 判定不了的一律「未分类」，绝不猜（与 supply_mix / awareness_gate 同纪律）；
  - 本模块只分类与排序，不生成内容、不改写选题。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "topic_category.yaml"

# 类别标识（与 config/topic_category.yaml 类别优先级同序）
CAT_SCORELINE = "战报比分"
CAT_TRANSFER = "转会官宣"
CAT_CONFLICT = "冲突舆情"
CAT_CHINA = "中国足球"
CAT_UNKNOWN = "未分类"

# 最小可用兜底词表（配置缺失时降级用；口径与 yaml 完全一致）
_FALLBACK = {
    "版本": "fallback",
    "生效日期": "",
    "口径出处": "",
    "类别优先级": [CAT_TRANSFER, CAT_CONFLICT, CAT_CHINA, CAT_UNKNOWN, CAT_SCORELINE],
    "单批软上限": {CAT_SCORELINE: 1},
    "监控": {"战报比分占比阈值": 0.5, "未分类占比阈值": 0.8},
    "比分模式": [r"\d+\s*[-:比]\s*\d+"],
    "赛果模式": ["大胜|血洗|绝杀|逆转|爆冷|告负|平局|取胜|击败|战胜|输给|横扫|惨败|小胜|战平|不敌|险胜|逼平|击溃|吊打|完胜|惜败|落败|破门|戴帽|晋级|出线"],
    "转会关键词": ["官宣", "签约", "加盟", "离队", "续约", "转会", "租借", "买断", "解约", "免签", "转会费", "新帅", "换帅", "下课"],
    "冲突关键词": ["冲突", "争议", "内讧", "炮轰", "怒斥", "互喷", "撕破脸", "质疑", "批评", "道歉", "处罚", "禁赛", "罚单", "种族歧视", "风波", "对峙", "叫板", "喊话", "回怼", "暗讽", "爆料"],
    "中国足球关键词": ["国足", "中国队", "中超", "中甲", "足协杯", "中国足球", "国安", "申花", "海港", "泰山", "蓉城", "三镇", "亚泰", "玉昆", "浙江队"],
}

_CACHE: dict = {}


def load_config(path: Path | str | None = None) -> dict | None:
    """读取分类配置；不可用返回 None（调用方据此走 _FALLBACK，不阻塞）。"""
    p = Path(path) if path else CONFIG_PATH
    key = str(p)
    if key in _CACHE:
        return _CACHE[key]
    cfg = None
    try:
        import yaml
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                cfg = data
    except Exception:
        cfg = None
    _CACHE[key] = cfg
    return cfg


def _patterns(cfg: dict, key: str) -> list:
    out = []
    for pat in (cfg.get(key) or []):
        try:
            out.append(re.compile(str(pat)))
        except re.error:
            continue
    return out


def _topic_fields(topic) -> tuple:
    """返回 (标题文本, 全字段文本)。全字段 = title/angle/why_pick/keywords_cn/content_type。"""
    if not isinstance(topic, dict):
        return "", ""

    def _s(v):
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            return " ".join(str(x) for x in v if x)
        return str(v)

    title = _s(topic.get("title"))
    blob = " ".join([title, _s(topic.get("angle")), _s(topic.get("why_pick")),
                     _s(topic.get("keywords_cn")), _s(topic.get("content_type"))])
    return title, blob


def classify_detail(topic: dict) -> dict:
    """分类明细：{category, signals(命中词), degraded}。判定不了返回未分类（不猜）。"""
    raw = load_config()
    degraded = raw is None
    cfg = raw or _FALLBACK
    title, blob = _topic_fields(topic)
    signals: list = []

    # ① 形态优先：比分/赛果模式只判标题
    if title.strip():
        for p in _patterns(cfg, "比分模式"):
            m = p.search(title)
            if m:
                signals.append(m.group(0))
                break
        if not signals:
            for p in _patterns(cfg, "赛果模式"):
                m = p.search(title)
                if m:
                    signals.append(m.group(0))
                    break
        if signals:
            return {"category": CAT_SCORELINE, "signals": signals, "degraded": degraded}

    # ② 转会官宣：content_type 直达或转会关键词（全字段）
    ct = str(topic.get("content_type") or "").strip() if isinstance(topic, dict) else ""
    if ct == "转会资讯":
        signals.append("content_type=转会资讯")
    signals += [k for k in (cfg.get("转会关键词") or []) if k and k in blob]
    if signals:
        return {"category": CAT_TRANSFER, "signals": signals, "degraded": degraded}

    # ③ 冲突舆情 / ④ 中国足球（全字段）
    hits = [k for k in (cfg.get("冲突关键词") or []) if k and k in blob]
    if hits:
        return {"category": CAT_CONFLICT, "signals": hits, "degraded": degraded}
    hits = [k for k in (cfg.get("中国足球关键词") or []) if k and k in blob]
    if hits:
        return {"category": CAT_CHINA, "signals": hits, "degraded": degraded}

    # ⑤ 未分类
    return {"category": CAT_UNKNOWN, "signals": [], "degraded": degraded}


def classify(topic: dict) -> str:
    """判定选题类别。扫描 title/angle/why_pick/keywords_cn/content_type；
    形态优先于主题（见模块 docstring）；判定不了 → 未分类（不猜）。"""
    return classify_detail(topic)["category"]


def _tier_index(category: str, priority: list) -> int:
    try:
        return priority.index(category)
    except ValueError:
        return len(priority)


def _score_of(topic) -> float:
    v = topic.get("score") if isinstance(topic, dict) else None
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def prioritize(topics: list, *, cap: int | None = None) -> tuple:
    """按类别优先级重排 + 战报比分软上限裁剪。

    - tier 序按配置「类别优先级」（列表下标，未列出的类别排最后）；
    - 组内按 score 降序（缺省 0；稳定排序保持原相对次序）；
    - 超出 cap 的战报比分移出（进 dropped，元素带 reason）；非战报类别永不删；
    - 不 mutate 输入列表；topic 无 _category 时内部现判；空列表 → ([], [])。
    """
    items = [t for t in (topics or []) if isinstance(t, dict)]
    if not items:
        return [], []
    cfg = load_config() or _FALLBACK
    priority = [str(x) for x in (cfg.get("类别优先级") or _FALLBACK["类别优先级"])]
    if cap is None:
        soft = (cfg.get("单批软上限") or {})
        cap = int(soft.get(CAT_SCORELINE, 1) or 0)
    cap = max(0, int(cap))

    def _cat(t):
        return t.get("_category") or classify(t)

    ordered = sorted(items, key=lambda t: (_tier_index(_cat(t), priority), -_score_of(t)))
    kept, dropped, n_scoreline = [], [], 0
    for t in ordered:
        if _cat(t) == CAT_SCORELINE:
            if n_scoreline >= cap:
                dropped.append({"topic": t, "category": CAT_SCORELINE,
                                "reason": f"战报比分超出单批软上限（cap={cap}，R11：该形态 CTR 0.49% 且 0 爆款，减产）"})
                continue
            n_scoreline += 1
        kept.append(t)
    return kept, dropped


def warn_category_distribution(topics) -> None:
    """非阻断：打印选题类别分布；战报比分 / 未分类占比超阈值时告警（不删题）。"""
    items = [t for t in (topics or []) if isinstance(t, dict)]
    if not items:
        return
    cfg = load_config() or _FALLBACK
    mon = cfg.get("监控") or {}
    sc_th = float(mon.get("战报比分占比阈值", 0.5))
    un_th = float(mon.get("未分类占比阈值", 0.8))
    counts: dict = {}
    for t in items:
        c = t.get("_category") or classify(t)
        counts[c] = counts.get(c, 0) + 1
    n = len(items)
    dist = "、".join(f"{k} {v}/{n}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    print(f"   📊 选题类别分布: {dist}")
    sc_ratio = counts.get(CAT_SCORELINE, 0) / n
    un_ratio = counts.get(CAT_UNKNOWN, 0) / n
    if sc_ratio > sc_th:
        print(f"   ⚠️ 战报比分占比偏高（{sc_ratio:.0%} > {sc_th:.0%}）——R11 回测该形态 CTR 0.49% 且 0 爆款，"
              f"建议提高转会官宣/冲突舆情/中国足球类选题供给")
    if un_ratio > un_th:
        print(f"   ⚠️ 未分类占比偏高（{un_ratio:.0%} > {un_th:.0%}）——词表覆盖不足，"
              f"建议回测支撑后扩充 config/topic_category.yaml 词表（遵循调参纪律）")
    if sc_ratio <= sc_th and un_ratio <= un_th:
        print(f"   ✅ 选题类别分布达标（战报比分 {sc_ratio:.0%} ≤ {sc_th:.0%}，"
              f"未分类 {un_ratio:.0%} ≤ {un_th:.0%}）")


def selftest() -> list:
    """冒烟自检：五类正例 / 形态优先 / content_type 路径 / 未分类 / prioritize / 降级。"""
    cases: list = []

    # ── 五类正例（每类 ≥2）──
    cases.append(("转会官宣·官宣签约",
                  classify({"title": "罗马诺：皇马官宣签下姆巴佩，五年合同锁死核心"}) == CAT_TRANSFER))
    cases.append(("转会官宣·转会费续约",
                  classify({"title": "曼城转会费创纪录，赖斯续约谈判进入最后阶段"}) == CAT_TRANSFER))
    cases.append(("转会官宣·content_type 直达",
                  classify({"title": "今晚有大事发生", "content_type": "转会资讯"}) == CAT_TRANSFER))
    cases.append(("冲突舆情·争议互喷",
                  classify({"title": "VAR争议判罚引爆全场，两队球迷赛后互喷"}) == CAT_CONFLICT))
    cases.append(("冲突舆情·内讧炮轰",
                  classify({"title": "更衣室内讧彻底曝光，主力前锋炮轰主帅战术"}) == CAT_CONFLICT))
    cases.append(("中国足球·国足",
                  classify({"title": "国足世预赛26人名单出炉，三名新人首次入选"}) == CAT_CHINA))
    cases.append(("中国足球·中超",
                  classify({"title": "中超争冠进入白热化，申花海港同分进入决胜时刻"}) == CAT_CHINA))
    cases.append(("战报比分·比分+惨败",
                  classify({"title": "亚泰主场1-5惨败，无锡吴钩狂轰5球"}) == CAT_SCORELINE))
    cases.append(("战报比分·比分开场",
                  classify({"title": "荷兰2-2日本：傲慢的代价"}) == CAT_SCORELINE))
    cases.append(("未分类·平淡话题",
                  classify({"title": "今晚的月亮真圆，适合静静看球"}) == CAT_UNKNOWN))
    cases.append(("未分类·空 dict", classify({}) == CAT_UNKNOWN))

    # ── 形态优先于主题：比分+中国足球词 → 战报比分 ──
    cases.append(("形态优先：国安3-1大胜仍判战报比分",
                  classify({"title": "国安3-1大胜鲁能，中超争冠主动权易手"}) == CAT_SCORELINE))
    # ── 主题词在 angle 字段也认 ──
    cases.append(("angle 字段转会词",
                  classify({"title": "一笔改变格局的交易", "angle": "官宣后的更衣室反应"}) == CAT_TRANSFER))

    # ── prioritize：tier 序 ──
    tps = [
        {"title": "中超争冠白热化，申花海港同分火并", "score": 80},
        {"title": "VAR争议判罚引爆全场，球迷互喷", "score": 90},
        {"title": "罗马诺：皇马官宣签下姆巴佩", "score": 70},
    ]
    kept, dropped = prioritize(tps)
    cases.append(("prioritize tier 序（转会→冲突→中国足球）",
                  [k["title"] for k in kept] == [tps[2]["title"], tps[1]["title"], tps[0]["title"]]))
    cases.append(("prioritize 无丢弃（非战报）", dropped == []))

    # ── prioritize：组内 score 降序 ──
    tps2 = [
        {"title": "罗马诺：官宣第一签", "score": 60},
        {"title": "曼城转会费创纪录", "score": 90},
    ]
    kept2, _d2 = prioritize(tps2)
    cases.append(("prioritize 组内 score 降序",
                  [k["title"] for k in kept2] == [tps2[1]["title"], tps2[0]["title"]]))

    # ── prioritize：战报比分 cap 裁剪 ──
    tps3 = [
        {"title": "亚泰主场1-5惨败", "score": 90},
        {"title": "荷兰2-2日本：傲慢的代价", "score": 80},
        {"title": "曼联点球6-5利兹联", "score": 70},
    ]
    kept3, dropped3 = prioritize(tps3)
    cases.append(("prioritize cap=1：3 条战报只留最高分",
                  len(kept3) == 1 and kept3[0]["title"] == tps3[0]["title"]))
    cases.append(("prioritize dropped 带原因",
                  len(dropped3) == 2 and all(d.get("reason") for d in dropped3)))
    cases.append(("prioritize 输入不被 mutate",
                  [t["title"] for t in tps3] == [tps3[0]["title"], tps3[1]["title"], tps3[2]["title"]]))
    cases.append(("prioritize 空列表", prioritize([]) == ([], [])))

    # ── 降级：load_config→None 仍可分类可排序 ──
    orig = load_config
    globals()["load_config"] = lambda *a, **k: None
    try:
        cases.append(("降级：classify 仍判战报比分",
                      classify({"title": "亚泰主场1-5惨败"}) == CAT_SCORELINE))
        cases.append(("降级：classify 仍判转会官宣",
                      classify({"title": "官宣：五年合同锁死"}) == CAT_TRANSFER))
        d = classify_detail({"title": "亚泰主场1-5惨败"})
        cases.append(("降级：degraded 标记", d["degraded"] is True))
        kept4, _d4 = prioritize(tps3)
        cases.append(("降级：prioritize 仍工作", len(kept4) == 1))
    finally:
        globals()["load_config"] = orig
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="选题分类与类别优先级（计划 5.1 / R11 内容诊断）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    raw = load_config()
    cfg = raw or _FALLBACK
    print(f"选题分类器 v{cfg.get('版本')}（口径出处：{cfg.get('口径出处', '')}）")
    if raw is None:
        print("  ⚠️ 配置不可用（config/topic_category.yaml 缺失或不可解析），当前走 _FALLBACK 词表")
    print(f"  类别优先级：{cfg.get('类别优先级')}")
    print(f"  单批软上限：{cfg.get('单批软上限')}")
    print(f"  词表规模：比分 {len(cfg.get('比分模式') or [])} / 赛果 {len(cfg.get('赛果模式') or [])} / "
          f"转会 {len(cfg.get('转会关键词') or [])} / 冲突 {len(cfg.get('冲突关键词') or [])} / "
          f"中国足球 {len(cfg.get('中国足球关键词') or [])}")
    # 与 title_antipattern.main() 同口径：默认即打印摘要 + 跑冒烟自检，退出码反映自检结果
    print("\n冒烟自检：")
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检结果：{'全部通过 ✅' if not bad else f'{bad} 项未通过 ❌'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
