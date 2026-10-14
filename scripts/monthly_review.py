#!/usr/bin/env python3
"""月度复盘一键闭环（用户 2026-10-14 需求：每月 1 号 15:00 统计上个月全部数据，
做一个月度看板，并根据数据提出修改建议）。

流水线（与周更 weekly_review 同构，但窗口是**自然月**）：
  1. 消费投递箱  → ingest_exports.run()：collector 每天 14:40 已入库，这里兜住
                   手动投递尚未消费的文件（幂等，重复跑无副作用）；
  2. 月度统计    → build_metrics_dashboard.collect(since=上月 1 号, until=上月月末)；
  3. 月度看板    → build_report(period_label="YYYY-MM 月度")，md/html/json 落 data/metrics/；
  4. 调参建议    → ratio_tuner.compute_suggestion()（**只算不写**，配置改动交人工）；
  5. 修改建议    → _monthly_recommendations()：把增长/供给/选题/标题/数据质量/合规
                   六个维度的数据转成**带依据**的具体建议（每条都注明数据出处）；
  6. 汇总报告    → data/audit/monthly_review_YYYY-MM.md；
  7. WxPusher 回执（publisher.send_wxpusher；CI 配 secret，本地无 env 时静默跳过）。

纪律（与全仓一致）：
  - 缺失指标一律「待接入」，**绝不用 0 冒充**——数据没接入时建议落在「接入/排查」类，
    而不是基于 0 的「优化」类；
  - 调参只出建议、**不自动改配置**（配比变更要人看）；
  - 任何一步失败不阻断其余步骤，失败项在报告里列出（本脚本是收口工具，不是闸门）；
  - git 提交推送由 CI workflow 层完成（GITHUB_TOKEN 认证），脚本本身不碰 git。

用法：
  python3 scripts/monthly_review.py                   # 统计上一个完整月（CST）
  python3 scripts/monthly_review.py --month 2026-09   # 指定月份（补跑用）
  python3 scripts/monthly_review.py --dry-run         # 只算，不写库、不落产物
产出：data/metrics/monthly_YYYY-MM.{md,html} + monthly_latest.json
      data/audit/monthly_review_YYYY-MM.md
退出码：0 = 全部步骤正常；1 = 有步骤失败（需人工看一眼）
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import build_metrics_dashboard as bmd     # noqa: E402
import check_perf_lag as cpl              # noqa: E402
import ingest_exports as ie               # noqa: E402
import ratio_tuner as rt                  # noqa: E402
from publisher import send_wxpusher       # noqa: E402

CST = ZoneInfo("Asia/Shanghai")
METRICS_DIR = PROJECT_ROOT / "data" / "metrics"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"

# 计划第十章验收目标（增长类用「30 天目标」做月度对照线）
TARGET_30D = {
    "篇均阅读": 200,        # 次
    "整体点击率": 3.5,      # %
    "平均完成率": 40.0,     # %
    "粉丝阅读占比": 2.0,    # %
}
DAILY_QUOTA = 6              # 计划口径：常态 6 篇/天
TRACE_TARGET = 100.0         # 溯源覆盖率目标 %
PERF_LAG_MAX = 3             # 绩效滞后告警线（天，与 check_perf_lag 默认一致）
GROWTH_LAG_MAX = 7           # 转粉滞后告警线（天）
HOT_READS = 1000             # 爆款线（阅读 ≥ 此值）


# ------------------------------------------------------------
# 月份边界
# ------------------------------------------------------------
def _month_bounds(month: str) -> tuple[date, date]:
    """'YYYY-MM' → (当月 1 号, 当月最后一天)。非法格式抛 ValueError。"""
    y, m = int(month[:4]), int(month[5:7])
    if not (1 <= m <= 12):
        raise ValueError(f"非法月份: {month}")
    last = calendar.monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, last)


def _default_month(now: datetime | None = None) -> str:
    """默认统计「上一个完整月」——1 号 15:00 跑时即上个月；月中途手动跑也是上月。"""
    now = now or datetime.now(CST)
    first_of_this = now.replace(day=1)
    last_of_prev = first_of_this - timedelta(days=1)
    return last_of_prev.strftime("%Y-%m")


def _month_label(month: str) -> str:
    return f"{month} 月度"


# ------------------------------------------------------------
# 数据采集
# ------------------------------------------------------------
def collect_month(month: str) -> dict:
    """按自然月边界采集看板数据（复用周看板的全部统计逻辑，只换窗口）。"""
    lo, hi = _month_bounds(month)
    days = (hi - lo).days + 1
    return bmd.collect(days, since=lo, until=hi)


def _tune_suggestion(month: str, *, today: date | None = None) -> dict:
    """调参建议：只用上月样本，**只算不写**（绝不调 ratio_tuner.main()，它会写配置）。"""
    lo, hi = _month_bounds(month)
    cfg = rt.load_config()
    # 窗口起点提前 40 天再按月过滤——collect_observations 只认「today - window」起算，
    # 直接传窗口天数会在月初切出不完整区间；过滤后 baseline 等指标纯用上月样本
    obs_all = rt.collect_observations(40, today=hi + timedelta(days=1))
    obs = [o for o in obs_all if lo.isoformat() <= str(o.get("date") or "") <= hi.isoformat()]
    return rt.compute_suggestion(cfg, obs, today=today)


# ------------------------------------------------------------
# 修改建议（核心增量：把数据转成带依据的建议）
# ------------------------------------------------------------
def _monthly_recommendations(stats: dict, tuner: dict | None,
                             lag: dict | None, month: str) -> list[dict]:
    """按维度产出修改建议。每条 = {维度, 优先级, 建议, 依据}。

    纪律：指标缺失（None）时建议落在「接入/排查」类，绝不用 0 冒充后给「优化」类建议。
    """
    growth, supply = stats.get("growth") or {}, stats.get("supply") or {}
    by_cat, by_title = stats.get("by_category") or {}, stats.get("by_title") or {}
    comp = stats.get("compliance") or {}
    recs: list[dict] = []

    def add(dim: str, pri: str, advice: str, basis: str) -> None:
        recs.append({"维度": dim, "优先级": pri, "建议": advice, "依据": basis})

    # ---- 合规（硬门槛，排最前）----
    if comp.get("suspended"):
        add("合规", "P0", "先人工解除熔断再谈一切优化（自动发布已暂停）",
            f"熔断中：{comp.get('reason') or '见 compliance_guard 状态'}")
    n_bad = comp.get("false_content_count")
    if n_bad:
        add("合规", "P0", "逐篇复盘不实内容并修正事实层流程，达标前不建议扩量",
            f"不实违规 {n_bad} 次（硬门槛 = 0）")

    # ---- 增长指标（对照 30 天目标）----
    reads = growth.get("篇均阅读")
    if reads is None:
        add("增长", "P1", "回填绩效数据：import_performance.py 导入头条后台作品导出",
            f"{month} 有发布但无 reads 回填（看板显示「待接入」）")
    elif reads < TARGET_30D["篇均阅读"]:
        add("增长", "P1", "选题向高阅读类型倾斜（见调参意向与选题分组），并加标题实验",
            f"篇均阅读 {reads} 次 < 30 天目标 {TARGET_30D['篇均阅读']} 次")
    else:
        add("增长", "P3", f"篇均阅读 {reads} 次已达 30 天目标，保持当前选题结构",
            f"篇均阅读实际 {reads} 次 ≥ 30 天目标 {TARGET_30D['篇均阅读']} 次")

    ctr = growth.get("整体点击率")
    if ctr is None:
        add("增长", "P1", "回填展现量 impressions（后台导出含此列，collector 已自动投递）",
            "点击率缺 impressions 无法计算（待接入，非 0）")
    elif ctr < TARGET_30D["整体点击率"]:
        add("增长", "P1", "做标题/封面对比实验（悬念词 +87% 结论待线上复现验证）",
            f"整体点击率 {ctr}% < 30 天目标 {TARGET_30D['整体点击率']}%")
    else:
        add("增长", "P3", f"点击率 {ctr}% 达标，标题策略可维持",
            f"目标 {TARGET_30D['整体点击率']}%，实际 {ctr}%")

    ret = growth.get("平均完成率")
    if ret is None:
        add("增长", "P2", "回填完读率 retention_rate（后台作品导出列）",
            "完成率待接入")
    elif ret < TARGET_30D["平均完成率"]:
        add("增长", "P1", "优化开头钩子与前 3 段结构，提升完读",
            f"平均完成率 {ret}% < 30 天目标 {TARGET_30D['平均完成率']}%")

    fr_share = growth.get("粉丝阅读占比")
    if fr_share is None:
        add("增长", "P2", "回填 follower_reads（粉丝阅读量）后评估粉丝粘性",
            "粉丝阅读占比待接入")
    elif fr_share < TARGET_30D["粉丝阅读占比"]:
        add("增长", "P2", "加强文末互动引导与系列化内容，提升粉丝阅读占比",
            f"粉丝阅读占比 {fr_share}% < 30 天目标 {TARGET_30D['粉丝阅读占比']}%")

    daily_f = growth.get("日均涨粉")
    if daily_f is None:
        add("增长", "P1", "检查粉丝分析通道：collector 每天 14:05 应自动投递粉丝趋势，"
                        "连续缺失则查登录态/页面改版",
            "日均涨粉无数据（待接入）")
    else:
        add("增长", "P3", f"日均涨粉 {daily_f}，来源：{growth.get('涨粉数据来源') or '未知'}",
            f"样本 {growth.get('涨粉样本天数') or '?'} 天")

    # ---- 供给（计划口径 6 篇/天、3 批/天）----
    per_day = supply.get("日均篇数")
    if per_day is not None and per_day < DAILY_QUOTA:
        add("供给", "P1", "排查缺批原因（风控/生成失败/供给不足），补足到计划日更 6 篇",
            f"日均 {per_day} 篇 < 计划 {DAILY_QUOTA} 篇/天（覆盖 {supply.get('覆盖天数')} 天）")
    br = supply.get("批次完成率")
    if br is not None and br < 100:
        add("供给", "P1", "核对 check_missing_batches 缺批清单并补发",
            f"批次完成率 {br}%（期望 3 批/天 × {supply.get('覆盖天数')} 天）")
    trace = supply.get("溯源覆盖率")
    if trace is not None and trace < TRACE_TARGET:
        add("供给", "P1", "补齐 sources_used/source_post 缺失篇目（溯源覆盖率是合规底线）",
            f"溯源覆盖率 {trace}% < 目标 {TRACE_TARGET}%")

    # ---- 选题/标题效果闭环 ----
    metrics = (tuner or {}).get("类别指标") or {}
    for sec, m in sorted(metrics.items()):
        if str(m.get("决策") or "").startswith("意向调整"):
            add("选题", "P2", f"{sec} 类按意向微调配比（人工确认后改 ratio_config，脚本不自动改）",
                f"样本 {m.get('样本数')} 篇，篇均 {m.get('篇均阅读')} vs 基准 "
                f"{m.get('基准篇均')}（差异 {m.get('差异'):+.1%}）")
    if by_cat:
        best = max(by_cat.items(), key=lambda kv: kv[1].get("篇均阅读") or 0)
        worst = min(by_cat.items(), key=lambda kv: kv[1].get("篇均阅读") or 1e18)
        if (best[1].get("篇均阅读") and worst[1].get("篇均阅读")
                and best[0] != worst[0]):
            add("选题", "P2", f"下月加码「{best[0]}」、压缩「{worst[0]}」的排产",
                f"{best[0]} 篇均 {best[1]['篇均阅读']} 次 vs {worst[0]} "
                f"{worst[1]['篇均阅读']} 次（{month} 实际数据）")
    if by_title:
        susp = by_title.get("含悬念词") or {}
        plain = by_title.get("赛事名词无具体性") or {}
        if susp.get("篇均阅读") and plain.get("篇均阅读"):
            gap = (susp["篇均阅读"] - plain["篇均阅读"]) / plain["篇均阅读"]
            advice = ("延续悬念词标题策略" if gap > 0
                      else "复查悬念词用法（本月未跑出正收益，勿盲目照搬旧结论）")
            add("标题", "P2", advice,
                f"悬念词组篇均 {susp['篇均阅读']} 次 vs 无具体性组 {plain['篇均阅读']} 次"
                f"（差异 {gap:+.0%}）")
    hot_total = sum((v or {}).get("爆款数") or 0 for v in list(by_cat.values()) + list(by_title.values()))
    n_pts = growth.get("_data_points") or 0
    if n_pts >= 20 and hot_total == 0:
        add("选题", "P2", "安排 1-2 组选题/标题 A/B 实验破爆款（当前月无 ≥1000 阅读作品）",
            f"有绩效样本 {n_pts} 篇，爆款 0 篇")

    # ---- 数据链路质量 ----
    lag = lag or {}
    lag_map = {"绩效": lag.get("绩效滞后天数"), "转粉": lag.get("转粉滞后天数")}
    limits = {"绩效": PERF_LAG_MAX, "转粉": GROWTH_LAG_MAX}
    for ch, val in lag_map.items():
        if val is not None and val > limits[ch]:
            add("数据链路", "P1", f"检查 collector 投递与 ingest 入库链路（{ch}滞后 {val} 天）",
                f"{ch}滞后 {val} 天 > 告警线 {limits[ch]} 天"
                f"（collector 每天 14:05 / ingest 14:40）")
    cov = (lag.get("回填覆盖率") or {}) if lag else {}
    rate = cov.get("rate") if isinstance(cov, dict) else None
    if rate is not None and rate < 80:
        add("数据链路", "P1", "集中回填历史绩效缺口（import_performance.py 批量导入）",
            f"回填覆盖率 {rate}%（{cov.get('filled')}/{cov.get('total')}）")
    for p in (lag.get("problems") or []):
        add("数据链路", "P1", "处理滞后监控告警", str(p))

    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    recs.sort(key=lambda r: order.get(r["优先级"], 9))
    return recs


# ------------------------------------------------------------
# 汇总
# ------------------------------------------------------------
def _safe(label: str, fn, failures: list):
    """跑一步；异常不阻断后续，记进 failures。"""
    try:
        return fn()
    except Exception as e:
        failures.append(f"{label} 失败：{type(e).__name__}: {e}")
        return None


def run(month: str | None = None, *, dry_run: bool = False,
        notify: bool = True, now: datetime | None = None) -> dict:
    """跑完整月度复盘闭环，返回结构化结果。"""
    now = now or datetime.now(CST)
    month = month or _default_month(now)
    failures: list[str] = []
    out: dict = {"month": month, "period": _month_label(month),
                 "generated_at": now.strftime("%Y-%m-%d %H:%M"),
                 "dry_run": dry_run, "failures": failures}

    # 1) 消费投递箱（兜住手动投递未消费的文件；幂等）
    out["inbox"] = _safe("消费投递箱", lambda: ie.run(dry_run=dry_run), failures)

    # 2) 月度统计 + 合规块
    stats = _safe("月度统计", lambda: collect_month(month), failures)
    if stats:
        stats["compliance"] = _safe("合规状态", bmd.compliance_block, failures) or {}
    out["stats"] = stats

    # 3) 月度看板（md/html/json）
    rep = _safe("月度看板", lambda: _build_dashboard(stats, month, dry_run=dry_run), failures)
    out["dashboard"] = rep

    # 4) 调参建议（只算不写）
    out["tuner"] = _safe("调参建议", lambda: _tune_suggestion(month, today=now.date()), failures)

    # 5) 修改建议
    out["recommendations"] = _safe(
        "修改建议", lambda: _monthly_recommendations(stats or {}, out["tuner"],
                                                     cpl.report(), month), failures) or []

    # 6) 滞后监控（建议已用，报告里也展示）
    out["lag"] = _safe("滞后监控", cpl.report, failures)

    # 7) 落产物（看板 md/html/json + 汇总报告）
    if not dry_run:
        _safe("落汇总报告", lambda: _write_reports(out, rep), failures)

    # 8) WxPusher 回执（dry-run 不发——避免把「试跑」当成「已跑」误导）
    if notify and not dry_run:
        _safe("状态回执", lambda: _notify_summary(out), failures)

    out["ok"] = not failures
    return out


def _build_dashboard(stats: dict | None, month: str, *, dry_run: bool) -> dict | None:
    if not stats:
        return None
    lo, hi = _month_bounds(month)
    days = (hi - lo).days + 1
    rep = bmd.build_report(stats, stats.get("compliance") or bmd.compliance_block(),
                           days, period_label=_month_label(month))
    if not dry_run:
        METRICS_DIR.mkdir(parents=True, exist_ok=True)
        (METRICS_DIR / f"monthly_{month}.md").write_text(
            bmd.render_markdown(rep), encoding="utf-8")
        (METRICS_DIR / f"monthly_{month}.html").write_text(
            bmd.render_html(rep), encoding="utf-8")
        # monthly_latest.json 由 _write_reports 统一落（含建议/tuner，信息更全）
    return rep


def _write_reports(out: dict, rep: dict | None) -> str:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    p = AUDIT_DIR / f"monthly_review_{out['month']}.md"
    payload = dict(out)
    payload["report_md"] = render_md(out)
    if rep is not None:
        (METRICS_DIR / "monthly_latest.json").write_text(
            json.dumps({"month": out["month"], "dashboard": rep,
                        "recommendations": out.get("recommendations") or [],
                        "tuner": out.get("tuner") or {},
                        "generated_at": out["generated_at"]},
                       ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    p.write_text(payload["report_md"], encoding="utf-8")
    return str(p)


def render_md(out: dict) -> str:
    L = [f"# 月度复盘报告（{out['period']}）", "",
         f"生成时间：{out['generated_at']}　|　统计窗口：{out['month']} 整月（自然月）", ""]
    if out.get("dry_run"):
        L += ["> （dry-run：未写库、未落产物）", ""]

    inbox = out.get("inbox") or {}
    if inbox:
        counts = inbox.get("counts") or {}
        L.append(f"- 投递箱：{inbox.get('files', 0)} 个文件 → "
                 + ("、".join(f"{k} {v}" for k, v in sorted(counts.items())) or "无文件"))

    stats = out.get("stats") or {}
    g = stats.get("growth") or {}
    s = stats.get("supply") or {}
    comp = stats.get("compliance") or {}
    L += ["", "## 一、月度数据摘要", ""]
    L.append(f"- 供给：总篇数 {s.get('总篇数')}（日均 {s.get('日均篇数')} 篇，"
             f"覆盖 {s.get('覆盖天数')} 天）；批次完成率 {s.get('批次完成率')}%；"
             f"溯源覆盖率 {s.get('溯源覆盖率')}%")
    if s.get("板块分布"):
        L.append("- 板块分布：" + "、".join(f"{k} {v} 篇" for k, v in s["板块分布"].items()))
    L.append(f"- 增长：篇均阅读 {_fmt(g.get('篇均阅读'), ' 次')}；"
             f"整体点击率 {_fmt(g.get('整体点击率'), '%')}；"
             f"平均完成率 {_fmt(g.get('平均完成率'), '%')}；"
             f"日均涨粉 {_fmt(g.get('日均涨粉'))}"
             f"（{g.get('涨粉数据来源') or '待接入'}，样本 {g.get('涨粉样本天数') or '—'} 天）")
    L.append(f"- 合规：不实违规 {comp.get('false_content_count') if comp.get('false_content_count') is not None else '—'} 次"
             f"（硬门槛 0）；{'⛔ 熔断中' if comp.get('suspended') else '✅ 未熔断'}")

    recs = out.get("recommendations") or []
    L += ["", "## 二、修改建议（按优先级；每条均注明数据依据）", ""]
    if not recs:
        L.append("（本月无触发建议）")
    for pri in ("P0", "P1", "P2", "P3"):
        rows = [r for r in recs if r["优先级"] == pri]
        if not rows:
            continue
        L.append(f"### {pri}")
        for r in rows:
            L.append(f"- **[{r['维度']}]** {r['建议']}")
            L.append(f"  - 依据：{r['依据']}")
        L.append("")

    sug = out.get("tuner") or {}
    if sug:
        L += ["## 三、调参建议（只算不写，改配置需人工确认）", ""]
        L.append(f"- 样本 {sug.get('样本总量')} 篇；自动执行 = {sug.get('是否自动执行')}；"
                 f"{sug.get('变更原因') or ''}")
        if sug.get("建议配比"):
            L.append(f"- 建议配比：{sug.get('建议配比')}")
        for sec, m in (sug.get("类别指标") or {}).items():
            L.append(f"  - {sec}：{m.get('决策')}（篇均 {m.get('篇均阅读')} vs 基准 "
                     f"{m.get('基准篇均')}，样本 {m.get('样本数')}）")
        L.append("")

    lag = out.get("lag") or {}
    if lag:
        L += ["## 四、数据链路状态", "",
              f"- 绩效最新 {lag.get('绩效最新') or '（空）'}（滞后 {lag.get('绩效滞后天数')} 天）",
              f"- 转粉最新 {lag.get('转粉最新') or '（待接入）'}（滞后 {lag.get('转粉滞后天数')} 天）"]
        cov = lag.get("回填覆盖率") or {}
        L.append(f"- 回填覆盖率（近窗口）：{cov.get('rate')}%"
                 f"（{cov.get('filled')}/{cov.get('total')}）")
        L.append("")

    if out.get("failures"):
        L += ["## 五、失败项（需人工）", ""]
        for f in out["failures"]:
            L.append(f"> ❌ {f}")
        L.append("")

    n_p0 = sum(1 for r in recs if r["优先级"] == "P0")
    n_p1 = sum(1 for r in recs if r["优先级"] == "P1")
    verdict = "全部正常 ✅" if out.get("ok") else "有步骤失败 ❌"
    L.append(f"**结论：{verdict}**（建议 {len(recs)} 条：P0×{n_p0}、P1×{n_p1}；"
             f"看板产物 data/metrics/monthly_{out['month']}.{{md,html}}）")
    return "\n".join(L) + "\n"


def _fmt(v, unit=""):
    return "—（待接入）" if v is None else f"{v}{unit}"


def _notify_summary(out: dict) -> None:
    """月度回执：数据摘要 + 建议条数 + 结论（成功/失败都发，状态始终知情）。"""
    stats = out.get("stats") or {}
    g = stats.get("growth") or {}
    s = stats.get("supply") or {}
    recs = out.get("recommendations") or []
    lines = [
        f"统计窗口：{out['month']} 整月",
        f"供给：{s.get('总篇数')} 篇（日均 {s.get('日均篇数')}），"
        f"溯源 {s.get('溯源覆盖率')}%",
        f"增长：篇均阅读 {_fmt(g.get('篇均阅读'), ' 次')}；"
        f"点击率 {_fmt(g.get('整体点击率'), '%')}；"
        f"日均涨粉 {_fmt(g.get('日均涨粉'))}",
        f"建议 {len(recs)} 条："
        + "、".join(f"{p}×{sum(1 for r in recs if r['优先级'] == p)}"
                    for p in ("P0", "P1", "P2", "P3")
                    if any(r["优先级"] == p for r in recs)),
        f"看板：data/metrics/monthly_{out['month']}.md（已随仓库提交）",
    ]
    for f in out.get("failures") or []:
        lines.append(f"❌ {f}")
    mark = "✅" if out.get("ok") else "⚠️"
    send_wxpusher(f"足球自媒体 {mark} 月度复盘（{out['month']}）", "\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description="月度复盘一键闭环（月度看板 + 修改建议）")
    ap.add_argument("--month", help="统计月份 YYYY-MM（默认上一个完整月）")
    ap.add_argument("--dry-run", action="store_true", help="只算，不写库、不落产物")
    ap.add_argument("--no-notify", action="store_true", help="不发 WxPusher 回执")
    ap.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    args = ap.parse_args()

    if args.month:
        try:
            _month_bounds(args.month)      # 提前校验格式，非法即退出
        except ValueError as e:
            print(f"❌ {e}（格式应为 YYYY-MM，如 2026-09）")
            return 2

    out = run(args.month, dry_run=args.dry_run, notify=not args.no_notify)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_md(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
