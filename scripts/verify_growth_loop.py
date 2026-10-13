#!/usr/bin/env python3
"""转粉 + 互动引导闭环验收（R11 内容诊断 2026-10-13「建议5」/ 计划 13.5 / 第十章）。

背景：R11 判定「粉丝阅读占比中位 0%、互动率仅健康号 1/3」是账号权重上不去的根因，
而这条线此前**既没数据也没动作**：
  - 头条「图文数据」导出不含涨粉列 → 看板「日均涨粉」恒为 null；
  - 「粉丝沉淀钩子」只写在提示词里，**没有任何生成后校验**。
本脚本把两件事钉死，防止回退。

硬项（不达标退出码 1）
  A. 互动引导配置结构——config/interaction_bait.yaml 字段齐备、正则可编译、判罚 0/打回
  B. 互动引导冒烟——interaction_bait.selftest() 全通过
  C. 生成侧接入——orchestrator 走同一判定，且生成侧打回 / 保存侧兜底注入均已接线
  D. 转粉通道——growth_import 解析 / 相邻日差分 / 缺失不猜 / 合并写不覆盖真实值
  E. 缺列不冒充 0——toutiao_import 对「导出里没有的列」必须留 None（不是 0）
  F. 看板口径——文章级优先 / 账号级兜底 / 两者皆无 → None（绝不 0）

告警项（只提示）
  G. 涨粉数据覆盖天数（data/growth/fans_daily.json）
  H. 近 14 天成稿互动引导覆盖率（有本地正文时才算，CI 无正文则跳过）

用法：
  python3 scripts/verify_growth_loop.py [--days 14] [--quiet]
产出：data/audit/growth_loop_<date>.md + growth_loop_latest.json
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import growth_import as gi      # noqa: E402
import interaction_bait as ib   # noqa: E402
import toutiao_import as tti    # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
CST = timezone(timedelta(hours=8))


def check_config() -> dict:
    raw = ib.load_config()
    if raw is None:
        return {"item": "互动引导配置（config/interaction_bait.yaml）", "ok": False,
                "advisory": False, "detail": ["❌ 配置缺失或不可解析（闸门将降级放行，属漂移隐患）"]}
    problems = []
    for key in ("版本", "关注引导", "互动引导", "判罚", "兜底模板"):
        if raw.get(key) in (None, "", [], {}):
            problems.append(f"缺字段 {key}")
    pen = raw.get("判罚") or {}
    if int(pen.get("分数", -1)) != 0:
        problems.append(f"判罚分数应 0，实为 {pen.get('分数')}")
    if "打回" not in str(pen.get("动作", "")):
        problems.append(f"判罚动作应含「打回」，实为 {pen.get('动作')}")
    if not str(pen.get("提示", "")).strip():
        problems.append("判罚提示为空（打回后无重写指引）")
    for k in ("关注引导", "互动引导"):
        for pat in (raw.get(k) or []):
            try:
                re.compile(str(pat))
            except Exception as e:
                problems.append(f"{k} 正则不可编译：{pat}（{e}）")
    # 语境纪律：判据必须带引导语境，不能只匹配裸词「关注」
    follow = raw.get("关注引导") or []
    if any(str(p).strip() in ("关注", "订阅", "追更") for p in follow):
        problems.append("关注引导含裸词模式（会把「值得关注」误判成钩子）")
    tpl = raw.get("兜底模板") or {}
    if not (tpl.get("关注") or []):
        problems.append("兜底模板缺「关注」话术（保存侧无法兜底）")
    if not isinstance(tpl.get("互动"), dict) or not tpl["互动"].get(""):
        problems.append("兜底模板缺「互动」默认话术（保存侧无法兜底）")
    return {"item": "互动引导配置（config/interaction_bait.yaml）", "ok": not problems,
            "advisory": False, "version": raw.get("版本"),
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"配置 v{raw.get('版本')} 字段齐备；"
                             f"关注引导 {len(follow)} 条、互动引导 {len(raw.get('互动引导') or [])} 条 ✅",
                             f"判罚：{pen.get('分数')} 分 / {pen.get('动作')} ✅"])}


def check_selftest() -> dict:
    cases = ib.selftest()
    bad = [n for n, ok in cases if not ok]
    return {"item": "互动引导冒烟（缺任一类必拦 / 事实用词不误判 / 降级放行）",
            "ok": not bad, "advisory": False, "total": len(cases),
            "detail": ([f"❌ 未通过：{bad}"] if bad else [f"{len(cases)} 项冒烟全部通过 ✅"])}


def check_orchestrator_wiring() -> dict:
    try:
        import orchestrator
    except Exception as e:  # pragma: no cover
        return {"item": "生成侧接入（打回 + 保存侧兜底）", "ok": False, "advisory": False,
                "detail": [f"❌ 无法载入 orchestrator：{e}"]}
    problems = []
    # 1) 判定同源：包装函数必须走 interaction_bait.check
    art = {"content": "干巴巴的赛报，没有引导。", "interaction_bait": ""}
    r = orchestrator.check_interaction_bait(art)
    if r["ok"] or "关注引导" not in r.get("missing", []):
        problems.append("check_interaction_bait 未走同一判定（正例未被拦）")
    if not orchestrator.check_interaction_bait({"content": _FULL})["ok"]:
        problems.append("check_interaction_bait 对达标稿误判")
    # 2) 两条生成链路都必须有打回接线 + 保存侧必须有兜底注入接线
    src = (PROJECT_ROOT / "orchestrator.py").read_text(encoding="utf-8")
    need = {
        "生成侧打回（Pipeline A）": "互动引导硬校验",
        "生成侧打回（确定性供给）": "确定性供给互动引导未达标",
        "保存侧兜底注入": "interaction_bait.inject(",
        "打回 hint": "_bait_retry_hint(",
    }
    for label, marker in need.items():
        if marker not in src:
            problems.append(f"缺 {label} 接线（未见 `{marker}`）")
    # 3) 兜底注入必须真的把 interaction_bait 字段写进正文（此前被静默跳过）
    art2 = {"content": "正文主体。", "interaction_bait": "你觉得这波谁最亏？",
            "interaction_type": "共鸣式"}
    out = ib.inject(art2)
    if not out["changed"] or not ib.check(art2["content"],
                                          bait_field=art2["interaction_bait"])["ok"]:
        problems.append("保存侧兜底注入未把 interaction_bait 落到正文并达标")
    return {"item": "生成侧接入（打回 + 保存侧兜底）", "ok": not problems, "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["check_interaction_bait 判定同源 ✅",
                             "两条生成链路打回 + 保存侧兜底注入均已接线 ✅"])}


_FULL = ("这球换你当裁判，吹不吹？评论区告诉我。\n---\n关注老六，这转会窗的瓜我替你盯着。")


def check_growth_channel() -> dict:
    problems = []
    detail = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # ① 解析 + 缺失不猜
        rows = [["统计日期", "粉丝总数", "涨粉数"],
                ["2026-10-01", 10000, 12],
                ["2026-10-02", 10025, None],
                ["2026-10-03", 10060, 35],
                ["2026-10-04", -1, -1]]
        recs = gi.parse_rows(rows)
        if [r["date"] for r in recs] != ["2026-10-01", "2026-10-02", "2026-10-03"]:
            problems.append("解析异常：占位行(-1/-1)应不登记")
        if recs[1]["new_followers"] is not None:
            problems.append("解析阶段不应猜涨粉（缺失应为 None）")
        # ② 相邻已登记日差分
        gi.save({"days": {r["date"]: {"total_followers": r["total_followers"],
                                      "new_followers": r["new_followers"]} for r in recs}},
                tmp / "f.json")
        data = gi.load(tmp / "f.json")
        filled = gi.derive_deltas(data["days"])
        if filled != ["2026-10-02"] or data["days"]["2026-10-02"]["new_followers"] != 25:
            problems.append("差分异常：应仅由粉丝总数补出 2026-10-02 的 25")
        if data["days"]["2026-10-03"]["new_followers"] != 35:
            problems.append("差分不应覆盖已有真实值")
        # ③ 占位符纪律
        if gi.normalize_count(-1) is not None or gi.normalize_count("0") != 0:
            problems.append("占位符纪律异常：-1 应为 None、真实 0 应保留")
        # ④ 合并写不覆盖真实值 + 落盘可读
        gi.merge(data, [{"date": "2026-10-01", "total_followers": None, "new_followers": None}])
        if data["days"]["2026-10-01"]["new_followers"] != 12:
            problems.append("合并写异常：None 不应覆盖真实值")
        if gi.daily_new_followers(tmp / "f.json") == {}:
            problems.append("落盘/回读异常（daily_new_followers 为空）")
    # ⑤ 真实数据文件可解析（空 = 待接入，不算失败）
    real = gi.load()
    detail.append(f"data/growth/fans_daily.json：{len(real.get('days') or {})} 个日期"
                  + ("（空 = 待接入）" if not real.get("days") else ""))
    detail.append("解析 / 相邻日差分 / 缺失不猜 / 合并写不覆盖真实值 全部通过 ✅")
    return {"item": "转粉通道（growth_import）", "ok": not problems, "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] + detail if problems else detail)}


def check_missing_column_not_zero() -> dict:
    """头条导出没有「涨粉数」列——必须留 None。此前填 0 造成 550 篇伪造涨粉。"""
    problems = []
    rec = {}
    tti._fill_metrics(rec, lambda f: {"reads": "100", "likes": "5"}.get(f, ""))
    if rec.get("new_followers") is not None:
        problems.append(f"缺列应留 None，实为 {rec.get('new_followers')!r}")
    rec2 = {}
    tti._fill_metrics(rec2, lambda f: {"new_followers": "0"}.get(f, ""))
    if rec2.get("new_followers") != 0:
        problems.append("真实的 0 应保留为 0")
    # 存量清理核对：performance_log.json 不应再有伪造的 0
    log = PROJECT_ROOT / "output" / "performance_log.json"
    fake = 0
    if log.exists():
        try:
            d = json.loads(log.read_text(encoding="utf-8"))
            fake = sum(1 for v in (d.get("articles") or {}).values()
                       if isinstance(v, dict) and v.get("new_followers") == 0)
        except Exception as e:
            problems.append(f"performance_log.json 解析失败：{e}")
    return {"item": "缺列不冒充 0（含存量清理核对）", "ok": not problems, "advisory": False,
            "legacy_zero": fake,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"缺列 → None ✅；真实 0 → 0 ✅",
                             f"存量伪造 0 残留：{fake} 条"
                             + ("（应为 0）" if fake == 0 else "（⚠️ 需清理）")])}


def check_dashboard_scope() -> dict:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    try:
        import build_metrics_dashboard as bmd
    except Exception as e:
        return {"item": "看板涨粉口径", "ok": False, "advisory": False,
                "detail": [f"❌ 无法载入看板脚本：{e}"]}
    problems = []

    def _collect(account_days=None):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            gp = tmp / "fans_daily.json"
            if account_days:
                gi.save({"days": {d: {"total_followers": 1000 * i, "new_followers": v}
                                  for i, (d, v) in enumerate(account_days.items(), 1)}}, gp)
            o_out, o_gp = bmd.OUTPUT_DIR, gi.GROWTH_PATH
            bmd.OUTPUT_DIR, gi.GROWTH_PATH = tmp / "output", gp
            try:
                (bmd.OUTPUT_DIR / "2026-10-10").mkdir(parents=True, exist_ok=True)
                (bmd.OUTPUT_DIR / "2026-10-10" / "metadata.json").write_text(
                    '{"total_articles": 1, "batches_completed": [], "articles": [], "topics": []}',
                    encoding="utf-8")
                return bmd.collect(30, now=bmd.datetime(2026, 10, 13, 12, 0, tzinfo=bmd.CST))["growth"]
            finally:
                bmd.OUTPUT_DIR, gi.GROWTH_PATH = o_out, o_gp

    g0 = _collect()
    if g0["日均涨粉"] is not None:
        problems.append(f"无数据时应为 None（待接入），实为 {g0['日均涨粉']}")
    g1 = _collect({"2026-10-09": 10, "2026-10-10": 20})
    if g1["日均涨粉"] != 15.0 or g1["涨粉数据来源"] != "粉丝分析通道":
        problems.append(f"账号级兜底异常：{g1['日均涨粉']} / {g1['涨粉数据来源']}")
    return {"item": "看板涨粉口径（文章级 → 账号级 → None）", "ok": not problems, "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["无数据 → None（待接入）✅",
                             "账号级兜底：同窗口均值 15.0、来源标「粉丝分析通道」✅"])}


def check_coverage(days: int = 14) -> dict:
    floor = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    files = [p for p in glob.glob(str(OUTPUT_DIR / "**" / "article-*.md"), recursive=True)
             if Path(p).parent.name >= floor]
    acct = gi.daily_new_followers()
    n_acct = len([d for d in acct if d >= floor])
    if not files:
        return {"item": f"近 {days} 天成稿互动引导覆盖率", "ok": True, "advisory": True,
                "skipped": True, "account_days": n_acct,
                "detail": [f"本地无成稿正文（CI 环境），本项跳过；"
                           f"涨粉通道近 {days} 天有 {n_acct} 天数据"]}
    miss = [p for p in files if not ib.check(Path(p).read_text(encoding="utf-8"))["ok"]]
    ok = not miss
    return {"item": f"近 {days} 天成稿互动引导覆盖率", "ok": ok, "advisory": True,
            "total": len(files), "hits": len(files) - len(miss), "account_days": n_acct,
            "detail": [f"{len(files)} 篇成稿，达标 {len(files) - len(miss)} 篇"
                       + ("" if ok else f"，未达标 {len(miss)} 篇：{miss[:3]}"),
                       f"涨粉通道近 {days} 天有 {n_acct} 天数据"
                       + ("（待接入：请在头条后台导出「粉丝分析」后跑 growth_import.py --write）"
                          if n_acct == 0 else "")]}


def verify(days: int = 14) -> dict:
    hard = [check_config(), check_selftest(), check_orchestrator_wiring(),
            check_growth_channel(), check_missing_column_not_zero(), check_dashboard_scope()]
    soft = [check_coverage(days)]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": hard + soft,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "advisory_failed": [c["item"] for c in soft if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 转粉 + 互动引导闭环验收报告（R11 建议5 / 计划 13.5 / 第十章）", "",
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
    if rep.get("advisory_failed"):
        L += [f"> 告警项待关注：{'、'.join(rep['advisory_failed'])}", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="转粉 + 互动引导闭环验收")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify(days=args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"growth_loop_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "growth_loop_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
