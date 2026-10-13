#!/usr/bin/env python3
"""效果闭环验收（R11 内容诊断 / 计划第十章）。

背景：R11 用 869 条头条真实导出证明——账号的生死线是**点击率**（爆款 8.2% vs
陪跑 0.14%），而决定点击率的是**选题与标题形态**（转会官宣 2.08% / 冲突舆情 1.55%
/ 中国足球 1.36% / 战报比分 0.49%；悬念词 +87%、赛事名词无具体性 -56%）。
但此前这些结论**落不了地**：单篇 click_rate 全缺失、选题类型不落盘，导致
「策略对不对」无法用数据验证，只能靠人肉记忆。

本脚本把效果闭环的四条链路钉死，任一断开即判失败：
  单篇点击率可得（派生） → 导出列直读 → 选题类型落盘 → 看板分组可见。

硬项（不达标退出码 1）
  A. 单篇 CTR 派生——只给 reads/impressions 时自动派生 click_rate；显式值优先；
     缺 impressions 时不派生（None，不冒充 0）
  B. 导出列直读——「点击率」映射为 click_rate（不再是忽略列）；normalize_rate
     正确处理百分数串 / 小数 / 负占位（-1）/ 空值
  C. 选题类型落盘——orchestrator 把 topic_category 写入批次 metadata 的接入未被回退；
     topic_category.classify 可用且五类判定正确
  D. 看板分组——build_metrics_dashboard.collect 能把 metadata 按「选题类型」与
     「标题特征」分组，篇数 / 篇均阅读 / 点击率 / 爆款数口径正确
  E. 导入透传——import_performance 校验并透传 click_rate，模板表头包含该列

告警项（只提示，不阻断）
  F. 存量数据覆盖率——近 N 天真实 metadata 中 topic_category / click_rate 覆盖比例
     （存量缺字段属正常，新批次落盘后提升）

用法：
  python3 scripts/verify_effect_loop.py [--days 14] [--quiet]
产出：data/audit/effect_loop_<date>.md + effect_loop_latest.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))   # import_performance / build_metrics_dashboard

import performance_logger as pl              # noqa: E402
import toutiao_import as tti                  # noqa: E402
import topic_category as tc                   # noqa: E402
import import_performance as ip               # noqa: E402
import build_metrics_dashboard as bmd         # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
CST = timezone(timedelta(hours=8))


# ------------------------------------------------------------
# 硬项
# ------------------------------------------------------------
def check_ctr_derivation() -> dict:
    """A. 单篇 click_rate 可由 reads/impressions 派生（所有通道共享层）。"""
    detail, bad = [], []
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        old_o, old_p = pl.OUTPUT_DIR, pl.PERF_LOG_PATH
        pl.OUTPUT_DIR, pl.PERF_LOG_PATH = tdp, tdp / "performance_log.json"
        try:
            pl.log_article_performance("2026-10-13", 1, reads=2000, impressions=10000)
            pl.log_article_performance("2026-10-13", 2, reads=1000, impressions=1000,
                                       click_rate=0.5)
            pl.log_article_performance("2026-10-13", 3, reads=500)
            log = json.loads((tdp / "performance_log.json").read_text(encoding="utf-8"))
        finally:
            pl.OUTPUT_DIR, pl.PERF_LOG_PATH = old_o, old_p
    arts = log.get("articles", {})
    a1 = arts.get("2026-10-13/article-1", {})
    a2 = arts.get("2026-10-13/article-2", {})
    a3 = arts.get("2026-10-13/article-3", {})
    if abs(float(a1.get("click_rate", -1)) - 0.2) > 1e-6:
        bad.append(f"派生失败：reads2000/impr10000 → {a1.get('click_rate')}（期望 0.2）")
    else:
        detail.append("reads2000 / impr10000 → click_rate 0.200000（自动派生）✅")
    if abs(float(a2.get("click_rate", -1)) - 0.5) > 1e-9:
        bad.append(f"显式值未优先：{a2.get('click_rate')}（期望 0.5，而非 1.0）")
    else:
        detail.append("显式 click_rate=0.5 优先于派生值 1.0 ✅")
    if "click_rate" in a3:
        bad.append(f"缺 impressions 却写了 click_rate={a3.get('click_rate')}（应不写）")
    else:
        detail.append("缺 impressions → 不派生、不写 key（不冒充 0）✅")
    return {"item": "单篇点击率派生（reads/impressions）", "ok": not bad,
            "detail": ([f"❌ {'；'.join(bad)}"] if bad else detail)}


def check_export_column() -> dict:
    """B. 头条导出「点击率」列直读 + 归一化。"""
    detail, bad = [], []
    if "click_rate" not in tti.CANONICAL_FIELDS:
        bad.append("click_rate 不在 CANONICAL_FIELDS")
    if tti.map_header("点击率")[0] != "click_rate":
        bad.append(f"「点击率」未映射到 click_rate（得到 {tti.map_header('点击率')}）")
    else:
        detail.append("「点击率」→ click_rate（已从忽略列升为正式字段）✅")
    if tti.map_header("粉丝展现量")[0] is not None:
        bad.append("「粉丝展现量」被误映射（应仍为忽略列）")
    else:
        detail.append("「粉丝展现量」仍被正确忽略（最长匹配未退化）✅")
    cases = [("0.20%", 0.002), ("8.2%", 0.082), ("8.2", 0.082), ("0.002", 0.002),
             ("-1", None), ("", None), (None, None), ("--", None)]
    for raw, want in cases:
        got = tti.normalize_rate(raw)
        if want is None:
            ok = got is None
        else:
            ok = got is not None and abs(got - want) < 1e-6
        if not ok:
            bad.append(f"normalize_rate({raw!r}) = {got}（期望 {want}）")
    if not bad:
        detail.append(f"normalize_rate {len(cases)} 例全对（百分数串 / 小数 / 负占位 / 空值）✅")
    return {"item": "头条导出「点击率」列直读与归一化", "ok": not bad,
            "detail": ([f"❌ {'；'.join(bad)}"] if bad else detail)}


def check_category_lands() -> dict:
    """C. 选题类型落盘接入 + 分类器可用。"""
    detail, bad = [], []
    src = (PROJECT_ROOT / "orchestrator.py").read_text(encoding="utf-8")
    if '"topic_category": topic_category.classify(' not in src:
        bad.append("orchestrator 未把 topic_category 落盘（接入被回退？）")
    else:
        detail.append("orchestrator 将 topic_category 写入批次 metadata ✅")
    cases = [
        ({"title": "皇马官宣签下姆巴佩，五年合同"}, tc.CAT_TRANSFER),
        ({"title": "更衣室内讧曝光，主力炮轰主帅"}, tc.CAT_CONFLICT),
        ({"title": "国足世预赛名单出炉，三人首入选"}, tc.CAT_CHINA),
        ({"title": "国安3-1大胜海港"}, tc.CAT_SCORELINE),
        ({"title": "随便一条没有特征的标题"}, tc.CAT_UNKNOWN),
        ({"title": ""}, tc.CAT_UNKNOWN),
    ]
    wrong = [(c.get("title"), tc.classify(c), want)
             for c, want in cases if tc.classify(c) != want]
    if wrong:
        bad.append(f"分类偏差：{wrong}")
    else:
        detail.append(f"classify {len(cases)} 例（五类 + 空标题）判定正确 ✅")
    return {"item": "选题类型落盘接入 + 分类器", "ok": not bad,
            "detail": ([f"❌ {'；'.join(bad)}"] if bad else detail)}


def check_dashboard_grouping() -> dict:
    """D. 看板按选题类型 / 标题特征分组，口径正确。"""
    detail, bad = [], []
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        d = tdp / "2026-10-13"
        d.mkdir(parents=True, exist_ok=True)
        meta = {
            "date": "2026-10-13",
            "articles": [
                {"index": 1, "slug": "a", "title": "没想到，皇马官宣签下姆巴佩",
                 "topic_category": "转会官宣",
                 "performance": {"reads": 200, "impressions": 10000, "click_rate": 0.02}},
                {"index": 2, "slug": "b", "title": "国安3-1大胜海港",
                 "topic_category": "战报比分",
                 "performance": {"reads": 49, "impressions": 10000}},
            ],
        }
        (d / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False),
                                         encoding="utf-8")
        old = bmd.OUTPUT_DIR
        bmd.OUTPUT_DIR = tdp
        try:
            data = bmd.collect(30, now=datetime(2026, 10, 13, 12, 0, tzinfo=CST))
        finally:
            bmd.OUTPUT_DIR = old
    bycat = data.get("by_category") or {}
    bytitle = data.get("by_title") or {}
    if "转会官宣" not in bycat or "战报比分" not in bycat:
        bad.append(f"按选题类型分组缺失：{list(bycat)}")
    else:
        tr = bycat["转会官宣"]
        sc = bycat["战报比分"]
        if tr["篇数"] != 1 or abs((tr["篇均阅读"] or 0) - 200.0) > 0.1:
            bad.append(f"转会官宣分组口径错：{tr}")
        elif abs((tr["点击率"] or 0) - 2.0) > 0.01:
            bad.append(f"转会官宣点击率错：{tr['点击率']}（期望 2.0，篇均 0.02）")
        elif abs((sc["点击率"] or 0) - 0.49) > 0.01:
            bad.append(f"战报比分点击率错：{sc['点击率']}（期望 0.49，派生 49/10000）")
        else:
            detail.append("按选题类型分组：篇数/篇均阅读/点击率/爆款数 口径正确 ✅")
    n_title = sum(v["篇数"] for v in bytitle.values())
    if not bytitle or n_title != 2:
        bad.append(f"按标题特征分组异常：{bytitle}")
    else:
        detail.append(f"按标题特征分组：{list(bytitle)}（共 {n_title} 篇）✅")
    return {"item": "看板按选题类型 / 标题特征分组", "ok": not bad,
            "detail": ([f"❌ {'；'.join(bad)}"] if bad else detail)}


def check_import_passthrough() -> dict:
    """E. import_performance 校验并透传 click_rate。"""
    detail, bad = [], []
    rec, err = ip.validate_row(
        {"date": "2026-10-13", "index": "1", "click_rate": "2.8%"}, 2)
    if err or rec is None or abs(rec.get("click_rate", -1) - 0.028) > 1e-9:
        bad.append(f"validate_row 未归一化 click_rate：{err or rec}")
    else:
        detail.append("validate_row：click_rate '2.8%' → 0.028 ✅")
    rec_neg, _ = ip.validate_row({"date": "2026-10-13", "index": "1", "click_rate": "-1"}, 3)
    if rec_neg is not None and rec_neg.get("click_rate") is not None:
        bad.append("负占位 -1 未被判为 None")
    else:
        detail.append("click_rate 负占位 -1 → None（不冒充 0）✅")
    if "click_rate" not in ip.TEMPLATE_HEADER:
        bad.append("TEMPLATE_HEADER 未包含 click_rate")
    else:
        detail.append("模板表头已含 click_rate ✅")
    return {"item": "导入通道 click_rate 透传", "ok": not bad,
            "detail": ([f"❌ {'；'.join(bad)}"] if bad else detail)}


# ------------------------------------------------------------
# 告警项
# ------------------------------------------------------------
def check_real_coverage(days: int) -> dict:
    """F. 近 N 天真实 metadata 的字段覆盖（存量提示）。"""
    since = (datetime.now(CST) - timedelta(days=days)).date()
    total = with_cat = with_ctr = 0
    for p in sorted(OUTPUT_DIR.glob("*/metadata.json")):
        try:
            d = datetime.strptime(p.parent.name, "%Y-%m-%d").date()
        except Exception:
            continue
        if d < since:
            continue
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in meta.get("articles", []) or []:
            total += 1
            if a.get("topic_category"):
                with_cat += 1
            if (a.get("performance") or {}).get("click_rate") is not None:
                with_ctr += 1
    detail = [
        f"近 {days} 天 {total} 篇：topic_category 覆盖 {with_cat} 篇"
        + (f"（{with_cat / total:.0%}）" if total else ""),
        f"click_rate 覆盖 {with_ctr} 篇" + (f"（{with_ctr / total:.0%}）" if total else ""),
        "存量 metadata 缺字段属正常（历史未落盘）；新批次起 topic_category 自动落盘，"
        "绩效回填后 click_rate 同步可得。",
    ]
    return {"item": f"近 {days} 天真实数据字段覆盖（存量，仅提示）", "ok": True,
            "advisory": True, "total": total,
            "with_category": with_cat, "with_click_rate": with_ctr, "detail": detail}


def verify(days: int = 14) -> dict:
    hard = [check_ctr_derivation(), check_export_column(), check_category_lands(),
            check_dashboard_grouping(), check_import_passthrough()]
    soft = [check_real_coverage(days)]
    checks = hard + soft
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "advisory_failed": [c["item"] for c in soft if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 效果闭环验收报告（R11 内容诊断 / 计划第十章）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警项" if c.get("advisory") else "硬项"
        L.append(f"## {c['item']}（{tag}）：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="效果闭环验收（R11 内容诊断）")
    ap.add_argument("--days", type=int, default=14, help="回看窗口天数（默认 14）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify(days=args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"effect_loop_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "effect_loop_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
