#!/usr/bin/env python3
"""验收指标看板（计划第十章）。

计划第十章把验收分两类：
    增长指标看效果，合规指标看底线。合规指标是硬性门槛，不达标即暂停自动发布。

本脚本把两类指标汇总成一份可读看板（HTML + Markdown + JSON），并逐项对照
计划给出的「当前 / 30 天 / 90 天」目标。

数据来源与诚实原则：
  - 合规指标：compliance_guard 状态（始终可得，硬门槛）。
  - 溯源覆盖率：metadata 的 sources_used / source_post（可得）。
  - 增长指标（篇均阅读 / 点击率 / 完成率 / 粉丝阅读占比）：依赖头条后台回填的
    performance_log.json。**未接入时一律标注「待接入」，绝不用 0 冒充真实数据。**

用法：
    python3 scripts/build_metrics_dashboard.py [--days 30] [--out data/metrics]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))   # 以便 import compliance_guard 等同级模块
OUTPUT_DIR = PROJECT_ROOT / "output"
DATA_DIR = PROJECT_ROOT / "data"
SIGNALS_DIR = DATA_DIR / "signals"
DEFAULT_OUT = PROJECT_ROOT / "data" / "metrics"

CST = ZoneInfo("Asia/Shanghai")

# 计划第十章验收目标（当前 / 30 天 / 90 天）
TARGETS = {
    "篇均阅读": {"当前": "118 次", "30天": "200 次", "90天": "300 次", "unit": "次"},
    "整体点击率": {"当前": "2.8%", "30天": "3.5%", "90天": "5.0%", "unit": "%"},
    "平均完成率": {"当前": "33%", "30天": "40%", "90天": "45%", "unit": "%"},
    "粉丝阅读占比": {"当前": "0.6%", "30天": "2.0%", "90天": "5.0%", "unit": "%"},
    "溯源覆盖率": {"当前": "无", "30天": "100%", "90天": "100%", "unit": "%"},
    "不实违规次数": {"当前": "未统计", "30天": "0", "90天": "0", "unit": "次"},
}

EXPECTED_BATCHES = ("morning", "noon", "evening")


# ------------------------------------------------------------
# 数据读取
# ------------------------------------------------------------
def _load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_perf_log() -> dict:
    """performance_log.json：{ "2026-10-10/<slug>": {reads,...} }（无则空）。"""
    data = _load_json(OUTPUT_DIR / "performance_log.json") or {}
    return data.get("articles", {}) if isinstance(data, dict) else {}


def _perf_lookup(perf: dict, date_str: str, art: dict) -> dict:
    """按 slug 主键取绩效记录，旧 article-<index> 键兜底（index 当天会重复，故不作首选）。"""
    slug = art.get("slug")
    if slug:
        rec = perf.get(f"{date_str}/{slug}")
        if isinstance(rec, dict):
            return rec
    rec = perf.get(f"{date_str}/article-{art.get('index')}")
    return rec if isinstance(rec, dict) else {}


def _title_bucket(title: str) -> str:
    """按 R11 标题特征分桶（复用 title_positive.evaluate 的 reasons 文案）。

    桶（互斥，优先级从上到下）：
      含悬念词 / 赛事名词无具体性 / 其他 / 未判定 / 空标题
    ——用于看板按标题形态分组统计效果，验证「悬念词 +87%、赛事名词 -56%」是否复现。
    """
    t = (title or "").strip()
    if not t:
        return "空标题"
    try:
        import title_positive
        res = title_positive.evaluate(t)
    except Exception:
        return "未判定"
    reasons = " ".join(res.get("reasons") or [])
    if "含悬念词" in reasons:
        return "含悬念词"
    if "且无具体人物/球队" in reasons:
        return "赛事名词无具体性"
    if reasons.strip():
        return "其他"
    return "未判定"


def _new_group() -> dict:
    """分组累加器：篇数 / 有绩效 / 阅读累计 / 篇均点击率累计 / 爆款（≥1000 阅读）。"""
    return {"n": 0, "pts": 0, "reads_sum": 0,
            "ctr_sum": 0.0, "ctr_cnt": 0, "hot": 0}


def _accumulate_group(g: dict, rec: dict) -> None:
    """把一条绩效记录累加进分组。rec 为空只计篇数（有绩效才算均值）。

    点击率按**篇**累计取均值（与「篇均阅读」同口径，便于与 R11 基准直接对比）：
    优先用记录里的 click_rate；缺失时由该篇 reads/impressions 派生。
    """
    g["n"] += 1
    if not rec:
        return
    r = rec.get("reads")
    im = rec.get("impressions")
    r = int(r) if r is not None else None
    im = int(im) if im is not None else None
    if r is not None:
        g["pts"] += 1
        g["reads_sum"] += r
        if r >= 1000:
            g["hot"] += 1
    ctr = rec.get("click_rate")
    if ctr is None and r is not None and im:
        ctr = r / im
    try:
        ctr = float(ctr) if ctr is not None else None
    except (TypeError, ValueError):
        ctr = None
    if ctr is not None:
        g["ctr_sum"] += ctr
        g["ctr_cnt"] += 1


def _summarize_groups(stat: dict) -> dict:
    """把分组累加器汇总为可渲染字典（篇数降序）。"""
    out = {}
    for k, g in sorted(stat.items(), key=lambda kv: (-kv[1]["n"], kv[0])):
        out[k] = {
            "篇数": g["n"],
            "有绩效": g["pts"],
            "篇均阅读": round(g["reads_sum"] / g["pts"], 1) if g["pts"] else None,
            "点击率": (round(g["ctr_sum"] / g["ctr_cnt"] * 100, 2)
                    if g["ctr_cnt"] else None),
            "爆款数": g["hot"],
        }
    return out


def hot_signals_block() -> dict:
    """RedFox 热点信号区（Phase 1 只读试点）——只读预热 JSON，绝不直连 API。

    无信号表 → 全部 None（看板显示「待接入」），绝不用 0 冒充。
    """
    sig = _load_json(SIGNALS_DIR / "redfox_signals.json") or {}
    budget = _load_json(DATA_DIR / "redfox_budget.json") or {}
    signals = sig.get("signals") or {}
    top = sorted(signals.items(), key=lambda kv: -(kv[1].get("社媒讨论量") or 0))[:3]
    works = sig.get("works_top") or []
    return {
        "生成时间": sig.get("generated_at"),
        "命中词数": len(signals) if sig else None,
        "作品数": sig.get("record_count"),
        "Top命中词": [{"词": k, "互动量": v.get("社媒讨论量")} for k, v in top] or None,
        "最高互动作品": ({"标题": works[0].get("title"), "互动量": works[0].get("engagement"),
                        "平台": works[0].get("platform")} if works else None),
        "今日调用": budget.get(datetime.now(CST).strftime("%Y-%m-%d")),
    }


def collect(days: int, *, now=None) -> dict:
    """汇总时间窗内的内容与合规数据。"""
    now = now or datetime.now(CST)
    since = (now - timedelta(days=days)).date()

    perf = load_perf_log()

    total_articles = 0
    pred_skipped = 0              # 预测类（晚间赛前预测稿，不发布到头条）剔除数
    traced = 0                     # sources_used / source_post 非空的文章
    batch_done = 0
    days_with_output = 0
    columns: dict[str, int] = {}
    reads: list[int] = []
    retentions: list[float] = []
    impressions_total = 0
    impressions_days = 0
    follower_reads_total = 0
    followers_total = 0
    followers_days = 0
    cat_stat: dict[str, dict] = {}      # 选题类型 → 分组累加
    title_stat: dict[str, dict] = {}    # 标题特征 → 分组累加

    for meta_path in sorted(OUTPUT_DIR.glob("*/metadata.json")):
        date_str = meta_path.parent.name
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue
        if d < since or d > now.date():
            continue
        meta = _load_json(meta_path) or {}
        arts_all = meta.get("articles", []) or []
        # R13 决策④：预测类不发布到头条，发布口径（总篇数/日均/板块/溯源/增长指标）
        # 一律剔除；历史数据无 is_prediction 标时按 topics 级 _is_prediction 标题对拍兜底
        pred_titles = {t.get("title") for t in meta.get("topics", []) or []
                       if t.get("_is_prediction")}
        arts = [a for a in arts_all
                if not a.get("is_prediction") and a.get("title") not in pred_titles]
        pred_skipped += len(arts_all) - len(arts)
        days_with_output += 1
        total_articles += len(arts)

        done = set(meta.get("batches_completed", []) or [])
        batch_done += len(done & set(EXPECTED_BATCHES))

        for a in arts:
            if a.get("sources_used") or a.get("source_post"):
                traced += 1
            col = a.get("column_name") or a.get("content_type") or "未分类"
            columns[col] = columns.get(col, 0) + 1

        # 增长指标：优先 metadata.performance，其次 performance_log.json
        for a in arts:
            rec = a.get("performance") or _perf_lookup(perf, date_str, a) or {}
            # 按「选题类型」与「标题特征」分组累计（效果闭环：让选题/标题策略有数据可依）
            _accumulate_group(cat_stat.setdefault(
                a.get("topic_category") or "未分类", _new_group()), rec)
            _accumulate_group(title_stat.setdefault(
                _title_bucket(a.get("title", "")), _new_group()), rec)
            if rec:
                if rec.get("reads") is not None:
                    reads.append(int(rec.get("reads", 0)))
                if rec.get("retention_rate"):
                    retentions.append(float(rec["retention_rate"]))
                if rec.get("impressions"):
                    impressions_total += int(rec.get("impressions", 0) or 0)
                    impressions_days = 1        # 有曝光量即可算点击率
                if rec.get("follower_reads"):
                    follower_reads_total += int(rec.get("follower_reads", 0) or 0)
        # 当日涨粉（以日为单位去重）
        day_f = 0
        for a in arts:
            rec = a.get("performance") or _perf_lookup(perf, date_str, a) or {}
            day_f += int((rec or {}).get("new_followers", 0) or 0)
        if day_f:
            followers_total += day_f
            followers_days += 1

    expected_batches = max(1, days_with_output * len(EXPECTED_BATCHES))
    supply = {
        "覆盖天数": days_with_output,
        "总篇数": total_articles,
        "日均篇数": round(total_articles / days_with_output, 1) if days_with_output else 0,
        "批次完成率": round(batch_done / expected_batches * 100, 1),
        "溯源覆盖率": round(traced / total_articles * 100, 1) if total_articles else 0.0,
        "板块分布": dict(sorted(columns.items(), key=lambda kv: -kv[1])),
        # 预测类剔除数（R13 决策④）：晚间赛前预测稿不发布到头条，不入发布口径
        "预测类剔除": pred_skipped,
    }

    growth = {
        "篇均阅读": round(sum(reads) / len(reads), 1) if reads else None,
        "平均完成率": round(sum(retentions) / len(retentions) * 100, 1) if retentions else None,
        "日均涨粉": round(followers_total / followers_days, 1) if followers_days else None,
        # 点击率 = 总阅读 / 总展现（需 impressions 回填，未接入则为 None，绝不用 0 冒充）
        "整体点击率": (round(sum(reads) / impressions_total * 100, 2)
                  if impressions_total and reads else None),
        # 粉丝阅读占比 = 总粉丝阅读 / 总阅读（需 follower_reads 回填）
        "粉丝阅读占比": (round(follower_reads_total / sum(reads) * 100, 2)
                   if follower_reads_total and reads else None),
        "_data_points": len(reads),
    }
    return {"growth": growth, "supply": supply, "hot": hot_signals_block(),
            # 效果闭环（R11）：按选题类型 / 标题特征分组的效果统计
            "by_category": _summarize_groups(cat_stat),
            "by_title": _summarize_groups(title_stat)}


def compliance_block() -> dict:
    try:
        import compliance_guard
        st = compliance_guard.status()
    except Exception:
        st = {"suspended": False, "false_content_count": None, "total_violations": None}
    # 不实违规硬门槛：>0 即红线
    n = st.get("false_content_count")
    st["red_line_ok"] = (n == 0) if n is not None else None
    return st


# ------------------------------------------------------------
# 渲染
# ------------------------------------------------------------
def _fmt(v, unit=""):
    if v is None:
        return "—（待接入）"
    return f"{v}{unit}"


def build_report(data: dict, comp: dict, days: int, *, now=None) -> dict:
    now = now or datetime.now(CST)
    growth, supply = data["growth"], data["supply"]
    by_category = data.get("by_category") or {}
    by_title = data.get("by_title") or {}

    growth_rows = [
        ("篇均阅读", _fmt(growth["篇均阅读"], " 次"),
         "已接入" if growth["篇均阅读"] is not None else "需 performance_log 回填"),
        ("整体点击率", _fmt(growth["整体点击率"], "%"),
         "已接入" if growth["整体点击率"] is not None else "需回填 impressions（展现量）"),
        ("平均完成率", _fmt(growth["平均完成率"], "%"),
         "已接入" if growth["平均完成率"] is not None else "需完读率回填"),
        ("粉丝阅读占比", _fmt(growth["粉丝阅读占比"], "%"),
         "已接入" if growth["粉丝阅读占比"] is not None else "需回填 follower_reads（粉丝阅读量）"),
        ("溯源覆盖率", _fmt(supply["溯源覆盖率"], "%"), "sources_used/source_post"),
        ("不实违规次数", (comp.get("false_content_count")
                      if comp.get("false_content_count") is not None else "—"), "硬门槛=0"),
    ]

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "window_days": days,
        "compliance": comp,
        "growth": growth,
        "supply": supply,
        "by_category": by_category,
        "by_title": by_title,
        "hot": (data or {}).get("hot") or hot_signals_block(),
        "growth_rows": growth_rows,
        "targets": TARGETS,
    }


def render_markdown(rep: dict) -> str:
    comp, supply = rep["compliance"], rep["supply"]
    L = [f"# 验收指标看板（近 {rep['window_days']} 天）", "",
         f"生成时间：{rep['generated_at']}　|　计划第十章", ""]

    L += ["## 一、合规红线（硬门槛，不达标即暂停自动发布）", ""]
    if comp.get("suspended"):
        L.append(f"- ⛔ **自动发布已熔断**：{comp.get('reason','')}（自 {comp.get('since','')}）")
    else:
        L.append("- ✅ 自动发布正常（未熔断）")
    n = comp.get("false_content_count")
    L.append(f"- 不实违规次数：**{n if n is not None else '—'}**（目标 0，>0 即熔断）")
    L.append("")

    L += ["## 二、增长指标（对照计划目标）", "",
          "| 指标 | 当前值 | 30 天目标 | 90 天目标 | 数据来源 |",
          "| --- | --- | --- | --- | --- |"]
    for name, val, note in rep["growth_rows"]:
        t = rep["targets"].get(name, {})
        L.append(f"| {name} | {val} | {t.get('30天','—')} | {t.get('90天','—')} | {note} |")
    L.append("")

    L += ["## 三、内容供给", "",
          f"- 覆盖天数：{supply['覆盖天数']}　总篇数：{supply['总篇数']}　日均：{supply['日均篇数']} 篇",
          f"- 批次完成率：{supply['批次完成率']}%（期望 3 批/天）",
          f"- 溯源覆盖率：{supply['溯源覆盖率']}%", ""]
    if supply.get("预测类剔除"):
        L.append(f"> 已剔除预测类（晚间赛前预测稿，不发布到头条）{supply['预测类剔除']} 篇，"
                 f"发布口径以头条实际发布为准。")
        L.append("")
    if supply["板块分布"]:
        L.append("板块分布：" + "、".join(f"{k} {v} 篇" for k, v in supply["板块分布"].items()))
        L.append("")

    hot = rep.get("hot") or {}
    L += ["## 四、热点信号（RedFox Phase 1 只读试点）", ""]
    if not hot.get("生成时间"):
        L += ["> 暂无信号表（未刷新或本地无 Key）。刷新：`source scripts/local_env.sh && "
              "python3 redfox_client.py --refresh`。本区只读预热 JSON，不直连 API。", ""]
    else:
        L.append(f"- 信号时间：{hot['生成时间']}；命中词 {hot.get('命中词数')} 个 / "
                 f"作品 {hot.get('作品数')} 条；今日 API 调用 {hot.get('今日调用')} 次")
        if hot.get("Top命中词"):
            L.append("- Top 命中词（互动量）：" + "、".join(
                f"{x['词']} {x['互动量']:,}" for x in hot["Top命中词"]))
        if hot.get("最高互动作品"):
            w = hot["最高互动作品"]
            L.append(f"- 最高互动作品：[{w['互动量']:,}] {w['标题'][:40]}（{w['平台']}）")
        L.append("")
    # 效果闭环（R11）：按选题类型 / 标题特征分组，验证「选题决定生死」是否在线上复现
    def _group_table(stat: dict, key_name: str) -> list:
        if not stat:
            return []
        rows = [f"| {key_name} | 篇数 | 有绩效 | 篇均阅读 | 点击率 | 爆款数 |",
                "| --- | --- | --- | --- | --- | --- |"]
        for k, v in stat.items():
            avg = "—" if v["篇均阅读"] is None else str(v["篇均阅读"])
            ctr = "—" if v["点击率"] is None else f"{v['点击率']}%"
            rows.append(f"| {k} | {v['篇数']} | {v['有绩效']} | {avg} | {ctr} | {v['爆款数']} |")
        return rows

    L += ["", "## 五、选题类型与标题效果（R11 效果闭环）", ""]
    cat_rows = _group_table(rep.get("by_category") or {}, "选题类型")
    title_rows = _group_table(rep.get("by_title") or {}, "标题特征")
    if cat_rows:
        L += ["### 5.1 按选题类型", ""] + cat_rows + [
            "",
            "> R11 基准：转会官宣 2.08% / 冲突舆情 1.55% / 中国足球 1.36% / "
            "战报比分 0.49%（0 爆款）。本表用于验证线上是否复现此排序。", ""]
    else:
        L += ["暂无选题类型数据（metadata 缺 topic_category，需新批次落盘后生效）。", ""]
    if title_rows:
        L += ["### 5.2 按标题特征", ""] + title_rows + [
            "", "> R11 基准：悬念词 +87%、赛事名词且无具体性 -56%。", ""]
    if rep["growth"]["_data_points"] == 0:
        L.append("> ⚠️ 阅读类指标尚未接入：请用 "
                 "`python3 scripts/import_performance.py <CSV/JSON>` 批量回填"
                 "（或 `python3 performance_logger.py <日期> <序号> --reads N --sync` 单篇），"
                 "本看板即可显示真实值。")
    return "\n".join(L)

def render_html(rep: dict) -> str:
    comp, supply = rep["compliance"], rep["supply"]
    suspended = comp.get("suspended")
    n = comp.get("false_content_count")
    red = suspended or (n not in (None, 0))

    def badge(ok):
        return ("<span style='color:#0a7d32;font-weight:600'>正常</span>" if ok
                else "<span style='color:#c62828;font-weight:600'>异常</span>")

    rows = []
    for name, val, note in rep["growth_rows"]:
        t = rep["targets"].get(name, {})
        warn = "（待接入）" in str(val)
        rows.append(
            f"<tr><td>{name}</td>"
            f"<td class='{'muted' if warn else 'strong'}'>{val}</td>"
            f"<td>{t.get('30天','—')}</td><td>{t.get('90天','—')}</td>"
            f"<td class='muted'>{note}</td></tr>")

    cols = "".join(f"<li>{k} <b>{v}</b> 篇</li>" for k, v in supply["板块分布"].items()) or "<li>—</li>"
    pred_note = (f"<p class='hint'>已剔除预测类（晚间赛前预测稿，不发布到头条）"
                 f"{supply['预测类剔除']} 篇，发布口径以头条实际发布为准。</p>"
                 if supply.get("预测类剔除") else "")
    hint = ("<p class='hint'>⚠️ 阅读类指标尚未接入：用 "
            "<code>python3 performance_logger.py &lt;日期&gt; &lt;序号&gt; --reads N --followers M --sync</code> "
            "从头条后台回填后，本看板显示真实值。</p>" if rep["growth"]["_data_points"] == 0 else "")

    hot = rep.get("hot") or {}
    if not hot.get("生成时间"):
        hot_html = ("<p class='hint'>暂无信号表（未刷新或本地无 Key）。刷新："
                    "<code>source scripts/local_env.sh && python3 redfox_client.py --refresh</code>。"
                    "本区只读预热 JSON，不直连 API。</p>")
    else:
        kws = "".join(f"<li>{x['词']} <b>{x['互动量']:,}</b></li>"
                      for x in (hot.get("Top命中词") or []))
        w = hot.get("最高互动作品") or {}
        top_line = (f"<p class='hint'>最高互动作品：[{w.get('互动量'):,}] "
                    f"{str(w.get('标题') or '')[:40]}（{w.get('平台')}）</p>" if w else "")
        hot_html = (
            f"<div class='kpis'>"
            f"<div class='kpi'><b>{hot.get('命中词数')}</b><span>命中词</span></div>"
            f"<div class='kpi'><b>{hot.get('作品数')}</b><span>作品数</span></div>"
            f"<div class='kpi'><b>{hot.get('今日调用')}</b><span>今日 API 调用</span></div>"
            f"</div>"
            f"<p class='hint'>信号时间：{hot.get('生成时间')}（只读预热 JSON，不直连 API）</p>"
            f"<ul style='margin-top:10px'>{kws or '<li>—</li>'}</ul>{top_line}")

    def _group_html(stat: dict, key_name: str) -> str:
        if not stat:
            return (f"<p class='hint'>暂无{key_name}数据"
                    "（metadata 缺 topic_category，需新批次落盘后生效）。</p>")
        head = (f"<tr><th>{key_name}</th><th>篇数</th><th>有绩效</th>"
                "<th>篇均阅读</th><th>点击率</th><th>爆款数</th></tr>")
        body = ""
        for k, v in stat.items():
            avg = "—" if v["篇均阅读"] is None else v["篇均阅读"]
            ctr = "—" if v["点击率"] is None else f"{v['点击率']}%"
            body += (f"<tr><td>{k}</td><td>{v['篇数']}</td><td>{v['有绩效']}</td>"
                     f"<td>{avg}</td><td>{ctr}</td><td>{v['爆款数']}</td></tr>")
        return f"<table><thead>{head}</thead><tbody>{body}</tbody></table>"

    cat_html = _group_html(rep.get("by_category") or {}, "选题类型")
    title_html = _group_html(rep.get("by_title") or {}, "标题特征")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>验收指标看板 · {rep['generated_at']}</title>
<style>
 body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;
   background:#f5f7fa;color:#1a1a1a}}
 .wrap{{max-width:920px;margin:0 auto;padding:28px 20px 48px}}
 h1{{font-size:22px;margin:0 0 4px}}
 .sub{{color:#6b7280;font-size:13px;margin-bottom:22px}}
 .card{{background:#fff;border-radius:12px;padding:20px 22px;margin-bottom:18px;
   box-shadow:0 1px 3px rgba(0,0,0,.06);border:1px solid #eceff3}}
 .card h2{{font-size:15px;margin:0 0 14px;color:#0d47a1}}
 .banner{{border-radius:12px;padding:16px 20px;margin-bottom:18px;font-size:14px}}
 .banner.ok{{background:#e8f5e9;color:#0a7d32;border:1px solid #c8e6c9}}
 .banner.bad{{background:#fdecea;color:#c62828;border:1px solid #f5c6c2}}
 table{{width:100%;border-collapse:collapse;font-size:13.5px}}
 th,td{{text-align:left;padding:9px 10px;border-bottom:1px solid #eef1f5}}
 th{{color:#0d47a1;background:#eaf3fb;font-weight:600}}
 .strong{{font-weight:600;color:#111}}
 .muted{{color:#9aa3af}}
 .kpis{{display:flex;gap:12px;flex-wrap:wrap;margin-top:4px}}
 .kpi{{flex:1 1 140px;background:#f7fafc;border:1px solid #eceff3;border-radius:10px;padding:12px 14px}}
 .kpi b{{display:block;font-size:20px;color:#0d47a1}}
 .kpi span{{font-size:12px;color:#6b7280}}
 ul{{margin:6px 0 0;padding-left:18px;font-size:13.5px;color:#374151}}
 .hint{{font-size:12.5px;color:#b45309;background:#fffbeb;border:1px solid #fde68a;
   border-radius:8px;padding:10px 12px;margin-top:12px}}
 code{{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:12px}}
</style></head><body><div class="wrap">
 <h1>验收指标看板</h1>
 <div class="sub">近 {rep['window_days']} 天　|　生成于 {rep['generated_at']}　|　《足球自媒体自动化选题与内容生产完善计划》第十章</div>

 <div class="banner {'bad' if red else 'ok'}">
   <b>合规红线</b>：{'⛔ 自动发布已熔断 —— ' + str(comp.get('reason','')) if suspended else '✅ 自动发布正常'}
   　｜　不实违规次数：<b>{n if n is not None else '—'}</b>（目标 0）
 </div>

  <div class="card"><h2>一、内容供给</h2>
  <div class="kpis">
    <div class="kpi"><b>{supply['覆盖天数']}</b><span>覆盖天数</span></div>
    <div class="kpi"><b>{supply['总篇数']}</b><span>总篇数</span></div>
    <div class="kpi"><b>{supply['日均篇数']}</b><span>日均篇数</span></div>
    <div class="kpi"><b>{supply['批次完成率']}%</b><span>批次完成率</span></div>
    <div class="kpi"><b>{supply['溯源覆盖率']}%</b><span>溯源覆盖率</span></div>
  </div>
  <ul style="margin-top:14px">{cols}</ul>
  {pred_note}
 </div>

 <div class="card"><h2>二、增长指标 · 对照计划目标</h2>
   <table><thead><tr><th>指标</th><th>当前值</th><th>30 天目标</th><th>90 天目标</th><th>数据来源</th></tr></thead>
   <tbody>{''.join(rows)}</tbody></table>
   {hint}
 </div>

 <div class="card"><h2>三、热点信号 · RedFox Phase 1 只读试点</h2>
{hot_html}
 </div>

 <div class="card"><h2>四、选题类型与标题效果 · R11 效果闭环</h2>
 <h3 style="font-size:13px;color:#374151;margin:2px 0 8px">按选题类型</h3>
 {cat_html}
 <h3 style="font-size:13px;color:#374151;margin:16px 0 8px">按标题特征</h3>
 {title_html}
 <p class="hint">R11 基准：转会官宣 2.08% / 冲突舆情 1.55% / 中国足球 1.36% / 战报比分 0.49%（0 爆款）；悬念词 +87%、赛事名词无具体性 -56%。本区用于验证线上排序是否复现。</p>
 </div>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="验收指标看板（计划第十章）")
    ap.add_argument("--days", type=int, default=30, help="统计窗口天数（默认 30）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    data = collect(args.days)
    comp = compliance_block()
    rep = build_report(data, comp, args.days)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    md = render_markdown(rep)
    html = render_html(rep)
    (out / f"dashboard_{stamp}.md").write_text(md, encoding="utf-8")
    (out / f"dashboard_{stamp}.html").write_text(html, encoding="utf-8")
    (out / "latest.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        print(md)
        print(f"\n已输出：{out}/dashboard_{stamp}.html")
    return rep


if __name__ == "__main__":
    main()
