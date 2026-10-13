#!/usr/bin/env python3
"""数据回收与配比调参作业（计划 3.5 / 7.3 / 8.1 / 11.5）

把「策略手册」从静态文档变为可版本化配置：回收阅读数据 → 按纪律产出配比建议 →
满足样本条件时自动升版，否则冻结并产建议交人工确认。

调参轴（R11 内容诊断，2026-10-13 起）：**选题类型**（转会官宣/冲突舆情/中国足球/
战报比分/未分类）。原「四板块」轴在 R11 回测中无区分度，已在错的轴上停止调参
（板块配比保留供赛程池构成校验 verify_fixture_pool，非调参目标）。

四道防呆（计划 11.5）：
  1. 样本阈值：单类型滚动 14 天样本 ≥30 篇才全自动，否则冻结产建议
  2. 探索配额：任何类型不低于 10%（保留 10%-15% 给当期非最优类型，防反馈回路）
  3. 死区：类型篇均阅读相对基准差异不足 20% 不调整
  4. 幅度与冷静期：单次变动 ≤10 个百分点；同一类型 2 周内不重复调整

用法：
  python3 scripts/ratio_tuner.py            # 回收数据 + 产出建议（满足条件则自动升版）
  python3 scripts/ratio_tuner.py --dry-run  # 只算不写
  python3 scripts/ratio_tuner.py --report   # 打印建议摘要
"""
import sys
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
CONFIG_PATH = PROJECT_ROOT / "config" / "ratio_config.yaml"
# 建议落档到 data/（随仓库持久化，便于人工查看与留档；output/ 属运行产物不入库）
SUGGESTION_PATH = PROJECT_ROOT / "data" / "ratio_suggestion.json"

# 调参轴（R11 内容诊断，2026-10-13 起）：由「四板块」改为「选题类型」。
#   R11 回测显示板块维度**无区分度**（四板块篇均阅读差异不显著），而选题类型有区分度
#   （转会官宣 CTR 2.08% / 冲突舆情 1.55% / 中国足球 1.36% / 战报比分 0.49% 且 0 爆款）。
#   **在错的轴上自动调参，越准越错**——故调参目标改用选题类型。
#   板块配比仍保留在 config/ratio_config.yaml，供赛程池构成校验
#   （scripts/verify_fixture_pool.py），不再作为调参目标。
#   分类口径见 config/topic_category.yaml / topic_category.py（形态优先于主题，
#   判定不了即「未分类」，不猜）。
PLAN_CATEGORIES = ["转会官宣", "冲突舆情", "中国足球", "战报比分", "未分类"]
CATEGORY_FALLBACK = "未分类"

# 栏目 → 选题类型（仅当 topic_category 字段缺失且现场判定失败时的最后兜底）
_COLUMN_FALLBACK = {
    "转会动态": "转会官宣", "转会资讯": "转会官宣", "转会密探": "转会官宣", "转会雷达": "转会官宣",
    "中国足球": "中国足球", "中超": "中国足球",
}


def resolve_category(title=None, topic_category=None, content_type=None, column_name=None):
    """把一篇文章映射到**选题类型**（调参轴，R11）。

    优先级：
      1) metadata 已落盘的 topic_category（新批次起，由 orchestrator 写入）；
      2) 现场用 topic_category.classify 判定（历史 metadata 无该字段，现场判定可补齐，
         避免整段历史数据落「未分类」而让调参失去样本）；
      3) 栏目兜底映射（转会类栏目 → 转会官宣）；
      4) 仍不可判 → 未分类（不猜）。
    """
    if topic_category and topic_category in PLAN_CATEGORIES:
        return topic_category
    try:
        import topic_category as tc
        # content_type 缺失时用栏目名顶上：classify 只认 content_type 字段，
        # 而「转会雷达」这类栏目名经关键词路径同样能判成转会类，避免全落未分类。
        cat = tc.classify({"title": title or "",
                           "content_type": content_type or column_name or ""})
        if cat in PLAN_CATEGORIES:
            return cat
    except Exception:
        pass
    for k in (content_type, column_name):
        if k and k in _COLUMN_FALLBACK:
            return _COLUMN_FALLBACK[k]
    return CATEGORY_FALLBACK


def load_config(path=None):
    p = Path(path or CONFIG_PATH)
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def save_config(cfg, path=None):
    p = Path(path or CONFIG_PATH)
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def _trimmed_mean(values):
    """剔除极值后的均值（计划 8.2：幂律分布，剔除极值）。"""
    v = sorted(x for x in values if x is not None and x > 0)
    if not v:
        return 0.0
    if len(v) >= 10:
        cut = max(1, int(len(v) * 0.1))
        v = v[cut:-cut] or v
    return sum(v) / len(v)


def _category_from_meta(meta, idx=None, slug=None):
    for a in (meta.get("articles") or []):
        hit = ((slug is not None and a.get("slug") == slug)
               or (slug is None and a.get("index") == idx))
        if hit:
            return resolve_category(
                title=a.get("title"), topic_category=a.get("topic_category"),
                content_type=a.get("content_type"), column_name=a.get("column_name"))
    return CATEGORY_FALLBACK


def collect_observations(window_days=14, today=None):
    """回收阅读数据：join 绩效源（reads）× metadata.json（选题类型）。

    绩效源按优先级：
      1) ``output/performance_log.json``——真导入台账（含最新回流）；
      2) 回落 ``metadata.json`` 的 ``articles[].performance``——该字段随仓库提交
         （.gitignore 仅放行 ``output/**/metadata.json``），故 CI 上无本地台账时
         仍能拿到真实数据（否则自动调参永远「无样本」，属结构性缺口）。
    同一 (日期, slug||index) 以台账为准，避免重复计数。
    """
    today = today or datetime.now()
    start = (today - timedelta(days=window_days)).strftime("%Y-%m-%d")
    obs: list[dict] = []
    seen: set = set()

    perf_path = OUTPUT_DIR / "performance_log.json"
    perf: dict = {}
    if perf_path.exists():
        try:
            perf = json.loads(perf_path.read_text(encoding="utf-8")).get("articles", {}) or {}
        except Exception:
            perf = {}

    for key, rec in perf.items():
        date_str = rec.get("date") or str(key).split("/")[0]
        if date_str < start:
            continue
        idx = rec.get("index")
        slug = rec.get("slug")
        category = CATEGORY_FALLBACK
        meta_path = OUTPUT_DIR / date_str / "metadata.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                category = _category_from_meta(meta, idx=idx, slug=slug)
            except Exception:
                pass
        obs.append({"date": date_str, "index": idx, "category": category,
                    "reads": rec.get("reads", 0),
                    "retention": rec.get("retention_rate", 0)})
        seen.add((date_str, slug if slug else idx))

    # 回落：metadata 内已回写的 performance（CI 上的主要来源）
    if OUTPUT_DIR.exists():
        for date_dir in sorted(OUTPUT_DIR.iterdir()):
            if not date_dir.is_dir():
                continue
            date_str = date_dir.name
            if len(date_str) != 10 or date_str[4] != "-" or date_str < start:
                continue
            meta_path = date_dir / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for a in (meta.get("articles") or []):
                p = a.get("performance") or {}
                if not isinstance(p, dict) or not p or p.get("reads") is None:
                    continue
                key = (date_str, a.get("slug") if a.get("slug") else a.get("index"))
                if key in seen:
                    continue
                obs.append({"date": date_str, "index": a.get("index"),
                            "category": resolve_category(
                                title=a.get("title"), topic_category=a.get("topic_category"),
                                content_type=a.get("content_type"),
                                column_name=a.get("column_name")),
                            "reads": p.get("reads") or 0,
                            "retention": p.get("retention_rate") or 0})
                seen.add(key)
    return obs


def last_adjust_dates(cfg, today=None):
    """每个选题类型最近一次「权重发生变化」的日期，用于冷静期判定。

    兼容两种 history 形态：
      1) 含「变更前配比 / 变更后配比」（apply_suggestion 写入）——直接比对；
      2) 仅含「选题类型配比」（变更前状态）——与下一条状态或当前配比比对。
    与本调参轴无关的历史条目（如只改篇数、或旧「板块配比」轴）一律跳过，
    避免遗留条目把所有类型误标成「刚调整过」而集体进冷静期。
    由新到旧遍历，先命中者即最近一次。
    """
    hist = cfg.get("history", []) or []
    cur = cfg.get("选题类型配比", {}) or {}
    last = {}
    n = len(hist)
    for i in range(n - 1, -1, -1):
        entry = hist[i]
        before = entry.get("变更前配比")
        after = entry.get("变更后配比")
        if before is None or after is None:
            if "选题类型配比" not in entry:
                continue          # 非本调参轴的历史条目，跳过
            before = entry.get("选题类型配比") or {}
            after = (hist[i + 1].get("选题类型配比") if i + 1 < n else cur) or {}
        for sec, w in after.items():
            if before.get(sec) != w and sec not in last:
                last[sec] = entry.get("生效日期")
    return last


def _rebalance(cur, intents, fixed, max_step, floor):
    """零和再分配：每类型实际变动 ≤ max_step、合计 100、且不低于 floor。

    fixed（样本冻结 / 冷静期）类型保持原权重不动；其余类型按意向做零和调整，
    再逐个 1 个百分点修正取整误差，且不得突破 ±max_step 与 floor 边界。
    """
    sections = list(cur.keys())
    active = [s for s in sections if s not in fixed]
    d = {s: (0 if s in fixed else max(-max_step, min(max_step, intents.get(s, 0))))
         for s in sections}
    if active:                      # 平移使 active 增量均值为 0（零和）
        m = sum(d[s] for s in active) / len(active)
        for s in active:
            d[s] -= m
    d = {s: max(-max_step, min(max_step, int(round(d[s])))) for s in sections}
    w = {s: (cur[s] if s in fixed else max(floor, cur[s] + d[s])) for s in sections}
    drift, guard = 100 - sum(w.values()), 0
    while drift != 0 and guard < 500:
        guard += 1
        step = 1 if drift > 0 else -1
        cands = [s for s in active
                 if abs((w[s] + step) - cur[s]) <= max_step and w[s] + step >= floor]
        if not cands:
            break
        s = max(cands, key=lambda x: abs(intents.get(x, 0)))
        w[s] += step
        drift -= step
    return w


def compute_suggestion(cfg, obs, today=None):
    """按纪律产出配比建议。返回含触发指标与数值的结构化 dict。"""
    today = today or datetime.now()
    discipline = cfg.get("调整纪律", {})
    window = discipline.get("滚动窗口天数", 14)
    min_sample = discipline.get("最小样本", 30)
    max_step = discipline.get("单次调整上限", 10)
    cooldown = discipline.get("冷静期天数", 14)
    dead_zone = discipline.get("死区阈值", 0.20)
    explore_lo, explore_hi = (discipline.get("探索配额") or [0.10, 0.15])[:2]

    cur = cfg.get("选题类型配比", {})
    by_cat = {}
    for o in obs:
        by_cat.setdefault(o["category"], []).append(o)

    baseline = _trimmed_mean([o["reads"] for o in obs])
    last_adj = last_adjust_dates(cfg, today)

    metrics, intents = {}, {}
    sample_frozen, cooldown_fixed = set(), set()
    floor = int(round(explore_lo * 100))
    for sec in PLAN_CATEGORIES:
        recs = by_cat.get(sec, [])
        n = len(recs)
        mean = _trimmed_mean([r["reads"] for r in recs])
        diff = (mean - baseline) / baseline if baseline else 0.0
        metrics[sec] = {"样本数": n, "篇均阅读": round(mean, 1),
                        "基准篇均": round(baseline, 1), "差异": round(diff, 4)}
        if n < min_sample:
            sample_frozen.add(sec)
            metrics[sec]["决策"] = f"冻结（样本 {n} < {min_sample}），产建议交人工确认"
            continue
        la = last_adj.get(sec)
        in_cd = False
        if la:
            try:
                in_cd = (today - datetime.strptime(la[:10], "%Y-%m-%d")).days < cooldown
            except Exception:
                in_cd = False
        if in_cd:
            cooldown_fixed.add(sec)
            metrics[sec]["决策"] = f"冷静期（{la} 调整过，未满 {cooldown} 天）"
            continue
        if abs(diff) < dead_zone:
            metrics[sec]["决策"] = f"死区（差异 {diff:+.1%} < {dead_zone:.0%}）"
            continue
        intents[sec] = max(-max_step, min(max_step, int(round(diff * 10))))
        metrics[sec]["决策"] = f"意向调整（差异 {diff:+.1%}）"

    fixed = sample_frozen | cooldown_fixed
    proposed = _rebalance(cur, intents, fixed, max_step, floor)

    # 用实际变动刷新决策文案 + 探索配额托底识别
    floored = []
    for sec in PLAN_CATEGORIES:
        actual = proposed[sec] - cur.get(sec, 0)
        if actual:
            metrics[sec]["决策"] = f"实调 {actual:+d} 个百分点（差异 {metrics[sec]['差异']:+.1%}）"
        if (sec not in fixed and proposed[sec] == floor
                and cur.get(sec, 0) + intents.get(sec, 0) < floor):
            floored.append(sec)

    auto = (len(sample_frozen) == 0) and any(
        proposed[s] != cur.get(s, 0) for s in PLAN_CATEGORIES)
    reasons = []
    for sec in PLAN_CATEGORIES:
        if proposed[sec] != cur.get(sec, 0):
            reasons.append(f"{sec} {cur.get(sec,0)}%→{proposed[sec]}%（{metrics[sec]['决策']}）")
    if sample_frozen:
        reasons.append("冻结类型：" + "、".join(sorted(sample_frozen)))
    if floored:
        reasons.append("探索配额托底：" + "、".join(floored) + f"（≥{floor}%）")

    return {
        "生成时间": today.strftime("%Y-%m-%d %H:%M"),
        "滚动窗口天数": window,
        "样本总量": len(obs),
        "基准篇均阅读": round(baseline, 1),
        "当前配比": cur,
        "建议配比": proposed,
        "类别指标": metrics,
        "是否自动执行": bool(auto),
        "冻结类型": sorted(sample_frozen),
        "探索配额": [explore_lo, explore_hi],
        "变更原因": "；".join(reasons) or "无满足条件的调整（全部落在死区或冷静期）",
    }


def apply_suggestion(cfg, suggestion, today=None):
    """把建议写入配置：更新 选题类型配比 / 生效日期 / 变更原因，并追加 history。"""
    today = today or datetime.now()
    hist = cfg.setdefault("history", [])
    hist.append({
        "生效日期": today.strftime("%Y-%m-%d"),
        "变更前配比": dict(cfg.get("选题类型配比", {})),
        "变更后配比": dict(suggestion["建议配比"]),
        "变更原因": suggestion["变更原因"],
        "触发指标": {s: m for s, m in suggestion["类别指标"].items()},
    })
    cfg["选题类型配比"] = suggestion["建议配比"]
    cfg["生效日期"] = today.strftime("%Y-%m-%d")
    cfg["变更原因"] = suggestion["变更原因"]
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只计算不写文件")
    ap.add_argument("--report", action="store_true", help="打印建议摘要")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    window = (cfg.get("调整纪律", {}) or {}).get("滚动窗口天数", 14)
    obs = collect_observations(window)
    sug = compute_suggestion(cfg, obs)

    print(f"[配比调参] 窗口 {window} 天，样本 {sug['样本总量']} 篇，基准篇均 {sug['基准篇均阅读']}")
    print(f"  建议配比: {sug['建议配比']}  (自动执行={sug['是否自动执行']})")
    print(f"  变更原因: {sug['变更原因']}")

    if args.report:
        print(json.dumps(sug, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("  [dry-run] 未写入任何文件")
        return

    SUGGESTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUGGESTION_PATH.write_text(json.dumps(sug, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  建议已写出: {SUGGESTION_PATH}")

    if sug["是否自动执行"]:
        cfg = apply_suggestion(cfg, sug)
        save_config(cfg, args.config)
        print("  ✅ 样本充足，已自动升版配比配置（历史已留档）")
    else:
        print("  ⏸️ 未满足全自动条件（样本不足或无有效调整），建议交人工确认")
    return sug


if __name__ == "__main__":
    main()
