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
    """performance_log.json：{ "2026-10-10/article-1": {reads,...} }（无则空）。"""
    data = _load_json(OUTPUT_DIR / "performance_log.json") or {}
    return data.get("articles", {}) if isinstance(data, dict) else {}


def collect(days: int, *, now=None) -> dict:
    """汇总时间窗内的内容与合规数据。"""
    now = now or datetime.now(CST)
    since = (now - timedelta(days=days)).date()

    perf = load_perf_log()

    total_articles = 0
    traced = 0                     # sources_used / source_post 非空的文章
    batch_done = 0
    days_with_output = 0
    columns: dict[str, int] = {}
    reads: list[int] = []
    retentions: list[float] = []
    followers_total = 0
    followers_days = 0

    for meta_path in sorted(OUTPUT_DIR.glob("*/metadata.json")):
        date_str = meta_path.parent.name
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue
        if d < since or d > now.date():
            continue
        meta = _load_json(meta_path) or {}
        arts = meta.get("articles", []) or []
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
            rec = a.get("performance") or perf.get(f"{date_str}/article-{a.get('index')}") or {}
            if rec:
                if rec.get("reads") is not None:
                    reads.append(int(rec.get("reads", 0)))
                if rec.get("retention_rate"):
                    retentions.append(float(rec["retention_rate"]))
        # 当日涨粉（以日为单位去重）
        day_f = 0
        for a in arts:
            rec = a.get("performance") or perf.get(f"{date_str}/article-{a.get('index')}") or {}
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
    }

    growth = {
        "篇均阅读": round(sum(reads) / len(reads), 1) if reads else None,
        "平均完成率": round(sum(retentions) / len(retentions) * 100, 1) if retentions else None,
        "日均涨粉": round(followers_total / followers_days, 1) if followers_days else None,
        "整体点击率": None,          # 需曝光量，头条后台未接入
        "粉丝阅读占比": None,        # 需粉丝/非粉丝拆分，未接入
        "_data_points": len(reads),
    }
    return {"growth": growth, "supply": supply}


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

    growth_rows = [
        ("篇均阅读", _fmt(growth["篇均阅读"], " 次"), "需 performance_log 回填"),
        ("整体点击率", _fmt(growth["整体点击率"], "%"), "需曝光量，未接入"),
        ("平均完成率", _fmt(growth["平均完成率"], "%"), "需完读率回填"),
        ("粉丝阅读占比", _fmt(growth["粉丝阅读占比"], "%"), "需粉丝拆分，未接入"),
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
    if supply["板块分布"]:
        L.append("板块分布：" + "、".join(f"{k} {v} 篇" for k, v in supply["板块分布"].items()))
        L.append("")
    if rep["growth"]["_data_points"] == 0:
        L.append("> ⚠️ 阅读类指标尚未接入：请用 `python3 performance_logger.py <日期> <序号> "
                 "--reads N --followers M --sync` 从头条后台回填，本看板即可显示真实值。")
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
    hint = ("<p class='hint'>⚠️ 阅读类指标尚未接入：用 "
            "<code>python3 performance_logger.py &lt;日期&gt; &lt;序号&gt; --reads N --followers M --sync</code> "
            "从头条后台回填后，本看板显示真实值。</p>" if rep["growth"]["_data_points"] == 0 else "")

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
 </div>

 <div class="card"><h2>二、增长指标 · 对照计划目标</h2>
   <table><thead><tr><th>指标</th><th>当前值</th><th>30 天目标</th><th>90 天目标</th><th>数据来源</th></tr></thead>
   <tbody>{''.join(rows)}</tbody></table>
   {hint}
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
