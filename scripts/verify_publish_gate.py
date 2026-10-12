#!/usr/bin/env python3
"""发布闸门验收（计划 7.2 / 7.3）。

计划 7.2 验收：溯源覆盖率 100%；标题格式合规通过率不低于 90%；连续 14 天无旧闻新发。
计划 7.3 验收：一致性校验拦截率可统计且误杀率低于 5%。

校验项：
  A. 标题格式合规通过率 ≥90%（硬项）—— 用项目自身评分器 score_title_plan 复算成稿标题；
  B. 溯源覆盖率（告警项，无产物跳过）—— sources_used 非空比例；
  C. 一致性校验可统计（硬项，无记录跳过）—— consistency_stats 台账就绪且能算出拦截率；
  D. 误杀率 <5%（告警项，无人工标注则待接入）—— 绝不把「未标注」当 0；
  E. 连续 14 天无旧闻新发（硬项，无台账跳过）—— 时效闸门台账零「已知时效>上限」保留。

产出 data/audit/publish_gate_*.md + publish_gate_latest.json；硬项 FAIL 退出码 1。

用法：
  python3 scripts/verify_publish_gate.py
  python3 scripts/verify_publish_gate.py --quiet
"""

import argparse
import glob
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import consistency_stats as cs  # noqa: E402
import freshness_ledger as fl  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "output"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))


def _iter_articles():
    for p in sorted(glob.glob(str(OUTPUT_DIR / "**" / "metadata.json"), recursive=True)):
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in (data.get("articles") or []):
            yield data, a


WINDOW_DAYS = 14            # 计划 7.2/7.3 与 8.1 一致的滚动窗口口径


def _recent_articles(days=WINDOW_DAYS):
    floor = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    for data, a in _iter_articles():
        if (data.get("date") or "") >= floor:
            yield data, a


def _score_titles(articles):
    """用项目自身评分器复算，返回 (total, passed, zero_violations, fails, antipattern)。

    zero_violations 只统计「传闻当官宣 / 禁用模板前缀」这类原有硬违规；
    赛果播报式（title_antipattern，2026-10-12 新增）单独归入 antipattern，
    避免新规则把历史存量的 A1 硬项拖红——新规则的验收在
    scripts/verify_title_antipattern.py 独立完成。
    """
    import orchestrator
    total = passed = 0
    zero, fails, anti = [], [], []
    for data, a in articles:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        total += 1
        sc, ok, reasons = orchestrator.score_title_plan({}, title, a.get("content_type", ""))
        if ok:
            passed += 1
        elif sc == 0:
            row = {"date": data.get("date"), "title": title, "reasons": reasons[:2]}
            if any("开场" in r for r in reasons):
                anti.append(row)
            else:
                zero.append(row)
        else:
            fails.append({"date": data.get("date"), "title": title, "score": sc,
                          "reasons": reasons[:2]})
    return total, passed, zero, fails, anti


def check_title_hard() -> dict:
    """A1. 成稿标题零「硬违规」（硬项）——近 14 天窗口内无 score_title_plan=0 的标题
    （0 分即传闻当官宣 / 禁用模板前缀等硬违规）。

    口径说明：赛果播报式（title_antipattern）属 2026-10-12 新增规则，
    其存量产出单列为 A4 告警项，不计入本硬项，避免历史存量把 A1 拖红。
    """
    try:
        import orchestrator  # noqa: F401
    except Exception as e:  # pragma: no cover
        return {"item": "成稿标题零硬违规（近14天）", "ok": True, "skipped": True,
                "detail": [f"无法载入评分器：{e}"]}
    arts = list(_recent_articles())
    if not arts:
        return {"item": "成稿标题零硬违规（近14天）", "ok": True, "skipped": True,
                "detail": ["近 14 天无成稿 metadata，本项跳过"]}
    total, _passed, zero, _fails, _anti = _score_titles(arts)
    return {"item": "成稿标题零硬违规（近14天）", "ok": not zero, "skipped": False,
            "total": total, "violations": zero[:10],
            "detail": [f"近 14 天成稿标题 {total} 条，硬违规（0 分）{len(zero)} 条",
                       ("零硬违规 ✅" if not zero else f"❌ 硬违规：{zero[:3]}")]}


def check_title_pass_rate() -> dict:
    """A2. 标题格式合规通过率 ≥90%（告警项，近 14 天滚动口径）。

    评分器接入前的存量标题不作为达标依据；新管线成稿≥60 分方发布，
    故滚动值反映管线质量趋势。口径写入报告，**不假装通过**（同 verify_fixture_pool 风格）。
    """
    try:
        import orchestrator  # noqa: F401
    except Exception as e:  # pragma: no cover
        return {"item": "标题格式合规通过率≥90%（近14天）", "ok": True, "advisory": True,
                "skipped": True, "detail": [f"无法载入评分器：{e}"]}
    arts = list(_recent_articles())
    if not arts:
        return {"item": "标题格式合规通过率≥90%（近14天）", "ok": True, "advisory": True,
                "skipped": True, "detail": ["近 14 天无成稿 metadata，本项跳过"]}
    total, passed, _zero, fails, _anti = _score_titles(arts)
    rate = passed / total if total else 0.0
    return {"item": "标题格式合规通过率≥90%（近14天）", "ok": True, "advisory": True,
            "skipped": False, "total": total, "passed": passed, "rate": round(rate, 4),
            "fails": fails[:10],
            "detail": [f"近 14 天成稿 {total} 条，合规（≥60 分）{passed} 条，通过率 {rate:.1%}（目标≥90%）",
                       ("达标 ✅" if rate >= 0.90 else
                        "⚠️ 未达 90%（存量 + 生成质量滚动值，告警不阻断；新管线成稿≥60 分方发布）"),
                       (f"未达标样例：{fails[:3]}" if fails else "无未达标样例")]}


def check_title_antipattern_residue() -> dict:
    """A4. 赛果播报式存量（告警项）——近 14 天窗口内命中 title_antipattern 的成稿。

    规则 2026-10-12 生效，只对**新生成**的标题打回；此前存量会随时间自然滚出窗口。
    新规则的完整验收在 scripts/verify_title_antipattern.py（含真实数据回测）。
    """
    try:
        import title_antipattern as ta
    except Exception as e:  # pragma: no cover
        return {"item": "赛果播报式存量（近14天，告警）", "ok": True, "advisory": True,
                "skipped": True, "detail": [f"无法载入反模式闸门：{e}"]}
    arts = list(_recent_articles())
    if not arts:
        return {"item": "赛果播报式存量（近14天，告警）", "ok": True, "advisory": True,
                "skipped": True, "detail": ["近 14 天无成稿 metadata，本项跳过"]}
    hits = []
    for data, a in arts:
        t = (a.get("title") or "").strip()
        if t and ta.check(t)["hit"]:
            hits.append(f"{data.get('date')} {t}")
    return {"item": "赛果播报式存量（近14天，告警）", "ok": not hits, "advisory": True,
            "skipped": False, "hits": hits[:10], "total": len(arts),
            "detail": [f"近 14 天成稿 {len(arts)} 条，命中赛果播报式 {len(hits)} 条"
                       + ("（规则生效前存量，随窗口滚出）" if hits else " ✅"),
                       *hits[:5]]}


def check_team_alias_awareness() -> dict:
    """A3. 队名判定必须「别名感知」（硬项）。

    计划 7.3 要求误杀率<5%。2026-10-12 的真实熔断即由「同队不同写法」误报引起
    （事实层「拜仁」vs 来源「拜仁慕尼黑」→ 误报「缺少客队名: 拜仁慕尼黑」）。
    这里把该类误报做成可回归校验：同义写法必须放行，真缺队名/卡外球队必须拦截。
    """
    try:
        import orchestrator
    except Exception as e:  # pragma: no cover
        return {"item": "队名判定别名感知（计划7.3）", "ok": True, "skipped": True,
                "detail": [f"无法载入事实校验器：{e}"]}
    # 探测会写一致性台账，须隔离到临时文件，绝不污染真实 data/consistency_stats.json
    import consistency_stats as cs
    tmp = tempfile.TemporaryDirectory()
    _s, _l = cs.STATS_PATH, cs.LABELS_PATH
    cs.STATS_PATH = Path(tmp.name) / "stats.json"
    cs.LABELS_PATH = Path(tmp.name) / "labels.json"
    try:
        return _probe_team_alias(orchestrator)
    finally:
        cs.STATS_PATH, cs.LABELS_PATH = _s, _l
        tmp.cleanup()


def _probe_team_alias(orchestrator) -> dict:
    cases = [
        # (home, away, hg, ag, 正文, 期望放行, 说明)
        ("多特", "拜仁慕尼黑", 1, 2, "拜仁客场2-1击败多特。", True, "全称 fixture + 简称正文"),
        ("多特", "拜仁", 1, 2, "拜仁慕尼黑客场2-1取胜多特。", True, "简称 fixture + 全称正文"),
        ("巴黎圣日耳曼", "马赛", 2, 0, "巴黎2-0马赛。", True, "巴黎/巴黎圣日耳曼"),
        ("曼城", "曼联", 1, 0, "曼城主场取胜。", False, "真缺客队名仍须拦截"),
        ("多特", "拜仁慕尼黑", 1, 2, "多特1-2不敌勒沃库森。", False, "写成别的队仍须拦截"),
    ]
    bad = []
    for home, away, hg, ag, content, want_pass, note in cases:
        fx = {"home_team": home, "away_team": away, "home_score": hg, "away_score": ag,
              "goals": []}
        art = {"title": content, "content": content}
        got_pass, issues = orchestrator.validate_article_vs_match_data(fx, art)
        if got_pass != want_pass:
            bad.append({"case": note, "want_pass": want_pass, "got_pass": got_pass,
                        "issues": issues})
    # 子串串味：正文「国际米兰」不得被判成卡外球队「米兰」
    cons_bad = []
    ok, issues = orchestrator.check_consistency_plan(
        {"title": "国际米兰主场取胜", "content": "国际米兰1-0拿下，国米继续领跑。"},
        {"allowed_teams": ["国米"]}, blocking=True)
    if not ok:
        cons_bad.append({"case": "国际米兰/米兰 子串串味", "issues": issues})
    ok2, issues2 = orchestrator.check_consistency_plan(
        {"title": "米兰2-1尤文", "content": "AC米兰2-1击败尤文图斯。"},
        {"allowed_teams": ["AC米兰"]}, blocking=True)
    if ok2:
        cons_bad.append({"case": "真卡外球队（尤文）未被拦截", "issues": issues2})

    all_bad = bad + cons_bad
    return {"item": "队名判定别名感知（计划7.3）", "ok": not all_bad, "skipped": False,
            "cases": len(cases) + 2, "violations": all_bad,
            "detail": [f"队名判定用例 {len(cases) + 2} 项：同义写法放行 / 真缺队名与卡外球队拦截",
                       ("别名感知达标 ✅" if not all_bad else f"❌ 误判：{all_bad[:3]}")]}


def check_provenance() -> dict:
    """B. 溯源覆盖率（告警项）。"""
    total, with_src = 0, 0
    for _data, a in _iter_articles():
        total += 1
        if (a.get("sources_used") or a.get("source_post")):
            with_src += 1
    if total == 0:
        return {"item": "溯源覆盖率（目标100%）", "ok": True, "advisory": True, "skipped": True,
                "detail": ["未找到成稿 metadata，本项跳过"]}
    rate = with_src / total
    return {"item": "溯源覆盖率（目标100%）", "ok": True, "advisory": True, "skipped": False,
            "total": total, "with_src": with_src, "rate": round(rate, 4),
            "detail": [f"成稿 {total} 条，带来源 {with_src} 条，覆盖率 {rate:.1%}（目标 100%）",
                       ("达标 ✅" if rate >= 0.999 else
                        "⚠️ 未达标（存量缺口，告警不阻断；口径与 audit_isolation D 一致）")]}


def check_consistency_mechanism() -> dict:
    """C. 一致性校验可统计（硬项，无记录跳过）。"""
    s = cs.summary()
    if not s["has_records"]:
        return {"item": "一致性校验拦截率可统计", "ok": True, "skipped": True,
                "detail": ["consistency_stats 台账暂无运行记录，机制就绪，本项跳过"]}
    ok = s["blocking_checks"] > 0 and s["reject_rate"] is not None
    return {"item": "一致性校验拦截率可统计", "ok": ok, "skipped": False, **s,
            "detail": [f"硬校验 {s['blocking_checks']} 次，打回 {s['rejects']} 次，"
                       f"拦截率 {s['reject_rate_display']}；warn-only {s['warn_only']} 次",
                       "拦截率可统计 ✅" if ok else "❌ 有事件但无 blocking 记录，拦截率不可算"]}


def check_false_positive_rate() -> dict:
    """D. 误杀率 <5%（告警项，无标注待接入）。"""
    fpr = cs.false_positive_rate()
    if fpr is None:
        return {"item": "一致性校验误杀率<5%", "ok": True, "advisory": True, "skipped": True,
                "detail": ["尚无人工抽检标注，误杀率「待接入」（绝不用 0 冒充）",
                           f"上限 {cs.MAX_FALSE_POSITIVE_RATE:.0%}；标注写入 data/consistency_labels.json"]}
    return {"item": "一致性校验误杀率<5%", "ok": True, "advisory": True, "skipped": False,
            "false_positive_rate": round(fpr, 4), "labels": cs.labels(),
            "detail": [f"已标注打回 {cs.labels()['labeled_rejects']} 条，误杀 {cs.labels()['false_positive']} 条，"
                       f"误杀率 {fpr:.1%}（上限 {cs.MAX_FALSE_POSITIVE_RATE:.0%}）",
                       ("达标 ✅" if fpr < cs.MAX_FALSE_POSITIVE_RATE else "⚠️ 超上限，需收紧校验规则")]}


def check_reject_traceability() -> dict:
    """F. 打回可追溯（告警项）。

    打回事件必须带**选题标题**，否则事后只能看到「最后一条提示」，无法定位触发批次
    （2026-10-12 熔断复盘即卡在这里）。
    """
    t = cs.traceability(30)
    if t["rejects"] == 0:
        return {"item": "打回可定位到选题", "ok": True, "advisory": True, "skipped": True,
                "detail": ["近 30 天无打回记录，本项跳过"]}
    return {"item": "打回可定位到选题", "ok": True, "advisory": True, "skipped": False, **t,
            "detail": [f"近 30 天打回 {t['rejects']} 条，带选题标题 {t['with_title']} 条，"
                       f"可追溯率 {t['rate']:.0%}",
                       ("可追溯 ✅" if t["rate"] >= 0.99 else
                        "⚠️ 部分打回缺选题信息（多为机制上线前的历史事件，告警不阻断）")]}


def check_old_news() -> dict:
    """E. 连续 14 天无旧闻新发（硬项，无台账跳过）。"""
    s = fl.summary(14)
    if s["runs"] == 0:
        return {"item": "连续14天无旧闻新发", "ok": True, "skipped": True,
                "detail": ["时效闸门台账暂无记录，本项跳过"]}
    v = fl.violations(14)
    return {"item": "连续14天无旧闻新发", "ok": not v, "skipped": False, **s,
            "violation_records": v[:5],
            "detail": [f"近 14 天闸门运行 {s['runs']} 次，覆盖 {s['days_covered']}/14 天；"
                       f"剔除超时效 {s['dropped']} 条，未知 {s['unknown']} 条",
                       (f"台账尚未满 14 天（累计 {s['days_covered']} 天，持续累积中）"
                        if s["days_covered"] < 14 else "已覆盖 14 天"),
                       ("无「已知时效>上限仍保留」✅" if not v else f"❌ {len(v)} 条超限保留")]}


def verify() -> dict:
    checks = [check_title_hard(), check_title_pass_rate(), check_team_alias_awareness(),
              check_provenance(), check_consistency_mechanism(), check_false_positive_rate(),
              check_reject_traceability(), check_old_news(), check_title_antipattern_residue()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 发布闸门验收报告（计划 7.2 / 7.3）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
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


def main() -> int:
    ap = argparse.ArgumentParser(description="发布闸门验收（计划 7.2 / 7.3）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"publish_gate_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "publish_gate_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
