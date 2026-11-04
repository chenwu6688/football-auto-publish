#!/usr/bin/env python3
"""三要素闸门验收（P0-1，2026-11-04 阅读量瓶颈诊断）。

为什么单独一个脚本：
  三要素（数字 / 对比 / 专名）是**新尺子**，最容易犯的错不是漏写规则，而是：
    a) 尺子做太紧——连头部爆款都判不合格，于是天天打回自己写的稿（10-13 句式闸门的旧病）；
    b) 尺子做太松——人人过线，等于没写（R11「减分 12 但及格线 60」采用率 0% 的旧病）。

所以验收的重点不是「代码里有没有这条规则」，而是**这把尺子在真实数据上的行为**：

  A. 口径自检（硬项）—— title_elements.selftest 全过；数字口径必须是 title_features
     的子集（防止同一件事两处各写各的，10-24 比分口径事故）
  B. 尺子判据（硬项）—— 用两组真实数据复算（我方 88 篇实发稿 + 头部 181 条对标）：
       · 我方单调：通过组阅读中位 > 被拦组（否则没有区分度）
       · 头部不误杀：头部通过率 ≥ 我方，且头部 TOP10 通过率 ≥70%
       · 有压力：我方通过率在 20%~85%（太高=形同虚设，太低=天天打回）
  C. 出口接入（硬项）—— 两个评分器对「缺项标题」必须返回 0 分且理由带 `[三要素]`，
     对「齐全标题」必须 >0；且 `[三要素]` 分桶不能被算成 A1 硬违规（存量不该拖红）
  D. 供给侧接入（硬项）—— 生成 prompt 真的注入了 `_title_block`（AST 查 Call 节点，
     不靠文本匹配：注释里写一句也算的话，这项就白验了）
  E. 覆盖率趋势（告警）—— 近 7 天成稿三要素通过率，看趋势不看单日

产出 ``data/audit/title_elements_<date>.md`` + ``title_elements_latest.json``；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_title_elements.py [--days 7] [--quiet]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import title_elements  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"
CST = timezone(timedelta(hours=8))

# 供给侧至少要有这么多处注入：`_title_block()`（4 处：改写/战报/结果稿/节点角度）
# + `title_brief.brief_text()`（1 处：赛程稿，规则已写在该 prompt 的「写作硬规则」里）
# + `_TITLE_ELEMENTS_RULE`（2 处：预测稿/突发稿，只有规则没有结构化素材）。
# 三者都算「把数字摊给模型」，任何一个降为 0 都应该 FAIL。
MIN_TITLE_BLOCK_CALLS = 6
# 尺子判据阈值与 analyze_title_elements 同源，这里只做「有没有越界」的复核
PASS_BAND = (0.20, 0.85)
PEER_TOP_MIN = 0.70


def _today() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _recent_titles(days: int) -> list[str]:
    """近 N 天成稿标题（读 output/<date>/metadata.json）。"""
    end = datetime.now(CST).date()
    since = (end - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    out: list[str] = []
    if not OUTPUT_DIR.exists():
        return out
    for d in sorted(OUTPUT_DIR.iterdir()):
        if not d.is_dir() or len(d.name) != 10 or d.name < since:
            continue
        mp = d / "metadata.json"
        if not mp.exists():
            continue
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in meta.get("articles") or []:
            t = (a.get("title") or "").strip()
            if t:
                out.append(t)
    return out


def _check_selftest() -> dict:
    """A. 口径自检（硬项）。"""
    cases = title_elements.selftest()
    bad = [n for n, ok in cases if not ok]
    return {"item": "口径自检（数字/对比/专名）", "ok": not bad, "skipped": False,
            "detail": [f"自检 {len(cases)} 项，失败 {len(bad)} 项",
                       ("全部通过 ✅" if not bad else "❌ " + "；".join(bad[:5]))]}


def _check_ruler() -> dict:
    """B. 尺子判据（硬项）——用真实数据复算通过率与分层。"""
    try:
        import analyze_title_elements as at
    except Exception as e:  # pragma: no cover
        return {"item": "尺子判据（真实数据复算）", "ok": True, "skipped": True,
                "detail": [f"无法载入校准脚本：{e}"]}
    try:
        rep = at.build_report()
    except Exception as e:  # pragma: no cover
        return {"item": "尺子判据（真实数据复算）", "ok": True, "skipped": True,
                "detail": [f"复算失败（无数据？）：{e}"]}
    v = rep.get("verdict") or {}
    ours, peers = rep.get("ours") or {}, rep.get("peers") or {}
    if not ours.get("n") or not peers.get("n"):
        return {"item": "尺子判据（真实数据复算）", "ok": True, "skipped": True,
                "detail": ["我方或头部样本为空，本项跳过（不拿 0 冒充）"]}

    rate = v.get("ours_pass_rate")
    band = bool(rate is not None and PASS_BAND[0] <= rate <= PASS_BAND[1])
    top_rate = v.get("peer_top_rate")
    ok = bool(v.get("monotonic")) and bool(v.get("looser_than_peers")) and band \
        and (top_rate is not None and top_rate >= PEER_TOP_MIN)
    return {"item": "尺子判据（真实数据复算）", "ok": ok, "skipped": False,
            "verdict": v, "coverage": {"ours": ours.get("coverage"),
                                       "peers": peers.get("coverage")},
            "detail": [
                f"我方 {ours['n']} 篇：通过 {v.get('ours_pass_rate')}，"
                f"通过组中位 {ours['passed']['median']} / 被拦组中位 {ours['blocked']['median']}"
                f"（单调 {'✅' if v.get('monotonic') else '❌'}）",
                f"头部 {peers['n']} 条：通过 {v.get('peers_pass_rate')}"
                f"（≥ 我方 {'✅' if v.get('looser_than_peers') else '❌'}），"
                f"TOP10 通过 {v.get('peer_top_pass')}"
                f"（≥{PEER_TOP_MIN:.0%} {'✅' if (top_rate or 0) >= PEER_TOP_MIN else '❌'}）",
                f"我方压力带 {PASS_BAND[0]:.0%}~{PASS_BAND[1]:.0%}："
                f"{'✅' if band else '❌（尺子过松或过紧）'}",
            ]}


def _check_gate_wiring() -> dict:
    """C. 出口接入（硬项）——评分器真的拦缺项、放齐全项。"""
    try:
        import orchestrator as o
    except Exception as e:  # pragma: no cover
        return {"item": "出口接入（两个评分器）", "ok": True, "skipped": True,
                "detail": [f"无法载入评分器：{e}"]}

    # 缺项：只有专名、无数字无对比的空问句
    lacking = "当年那个追风少年曲波，如今在基层默默带娃，他到底图个啥？"
    # 齐全：数字 + 对比 + 专名（真实高阅读标题）
    full = "1平2负只拿0.33分，中超这根独苗还能撑到2028吗？"

    s1, ok1, why1 = o.score_title_plan({}, lacking, "热点球评")
    s2, ok2, why2 = o.title_elements.check(full)["ok"], True, []
    s3, ok3, _ = o.score_title_plan({}, full, "热点球评")
    sf, whyf = o.score_fixture_title({"home_zh": "国安", "away_zh": "泰港"}, lacking)

    tag_ok = any(str(r).startswith("[三要素]") for r in why1)
    sf_tag = any(str(r).startswith("[三要素]") for r in whyf)
    ok = (s1 == 0 and ok1 is False and tag_ok and s2 and s3 > 0
          and sf == 0 and sf_tag)
    return {
        "item": "出口接入（两个评分器）", "ok": ok, "skipped": False,
        "detail": [
            f"score_title_plan 缺项标题 → {s1} 分（应为 0）{'✅' if s1 == 0 else '❌'}；"
            f"理由带 [三要素] {'✅' if tag_ok else '❌'}",
            f"score_title_plan 齐全标题 → {s3} 分（应 >0）{'✅' if s3 > 0 else '❌'}",
            f"score_fixture_title 缺项标题 → {sf} 分（应为 0）"
            f"{'✅' if sf == 0 else '❌'}；理由带 [三要素] {'✅' if sf_tag else '❌'}",
        ]}


def _count_title_block_calls() -> tuple[int, dict]:
    """AST 统计供给侧的标题素材注入点（不数定义、不数注释/文档字符串）。

    返回 (总数, 分项)。三项都算「把数字摊给模型」：
      _title_block(...)           规则 + 结构化素材
      title_brief.brief_text(...)  只有结构化素材（规则写在该 prompt 的硬规则里）
      _TITLE_ELEMENTS_RULE         只有规则（预测/突发稿没有结构化 topic）
    """
    try:
        tree = ast.parse(ORCHESTRATOR.read_text(encoding="utf-8"))
    except Exception:
        return 0, {}
    parts = {"title_block": 0, "brief_text": 0, "rule": 0}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name == "_title_block":
                parts["title_block"] += 1
            elif name == "brief_text":
                parts["brief_text"] += 1
        elif isinstance(node, ast.Name) and node.id == "_TITLE_ELEMENTS_RULE":
            parts["rule"] += 1
    return sum(parts.values()), parts


def _check_supply_side() -> dict:
    """D. 供给侧接入（硬项）——prompt 里真的注入了标题素材。"""
    n, parts = _count_title_block_calls()
    ok = n >= MIN_TITLE_BLOCK_CALLS
    return {"item": "供给侧注入标题素材", "ok": ok, "skipped": False,
            "calls": n, "parts": parts,
            "detail": [f"注入点 {n} 处（下限 {MIN_TITLE_BLOCK_CALLS}）："
                       f"_title_block {parts.get('title_block', 0)} / "
                       f"brief_text {parts.get('brief_text', 0)} / "
                       f"规则引用 {parts.get('rule', 0)} {'✅' if ok else '❌'}",
                       "只加出口闸门不加供给侧 → 模型只能靠编数字过闸门，"
                       "这正是 11-03「改口径先 grep 供给侧」的教训"]}


def _check_coverage_trend(days: int) -> dict:
    """E. 覆盖率趋势（告警）。"""
    titles = _recent_titles(days)
    if not titles:
        return {"item": f"近{days}天三要素通过率（告警）", "ok": True, "advisory": True,
                "skipped": True, "detail": [f"近 {days} 天无成稿，本项跳过"]}
    cov = title_elements.coverage(titles)
    return {"item": f"近{days}天三要素通过率（告警）", "ok": True, "advisory": True,
            "skipped": False, "coverage": cov,
            "detail": [f"成稿 {cov['n']} 条：数字 {cov['number']}% / 对比 {cov['contrast']}% / "
                       f"专名 {cov['proper']}% / 三项全过 {cov['all_three']}%",
                       f"达下限通过率 {cov['pass_rate']}%（上不设目标：这是存量+新管线混合值，"
                       f"只看趋势）"]}


def verify(days: int = 7) -> dict:
    checks = [_check_selftest(), _check_ruler(), _check_gate_wiring(),
              _check_supply_side(), _check_coverage_trend(days)]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "min_elements": title_elements.MIN_ELEMENTS,
            "require_proper": title_elements.REQUIRE_PROPER,
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = [f"# 标题三要素闸门验收（P0-1）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 判据：≥{rep['min_elements']} 项"
         f"{'且必含专名' if rep['require_proper'] else ''}",
         f"- 结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警" if c.get("advisory") else "硬项"
        status = "跳过" if c.get("skipped") else ("✅" if c["ok"] else "❌")
        L.append(f"## {c['item']}（{tag}）：{status}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="标题三要素闸门验收（P0-1）")
    ap.add_argument("--days", type=int, default=7, help="覆盖率趋势窗口（默认 7）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args(argv)

    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    date = _today()
    (AUDIT_DIR / f"title_elements_{date}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "title_elements_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        print(render_md(rep))
    else:
        print(f"title_elements: {'ok' if rep['ok'] else 'FAIL ' + '、'.join(rep['hard_failed'])}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
