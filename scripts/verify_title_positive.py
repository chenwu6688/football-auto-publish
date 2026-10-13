#!/usr/bin/env python3
"""标题正向规则验收（计划 3.3 / R11 内容诊断结论落地）。

背景：2026-10-12 头条后台 869 条真实图文回测，定位出除「赛果播报式硬规则」
（title_antipattern）之外的可数据证实正/负向信号，本脚本把它们钉死，防止回归：
  - 悬念词（没想到/原来/竟然/居然/反转）→ +87% 点击率 → 加分
  - 标题长度 21-30 字 → 回测最优区间 → 加分
  - 赛事名词无具体人物/球队（世界杯/决赛/小组赛…）→ CTR 0.47% 垫底 → 减分
  - 情绪词（炸/崩/泪/狠/绝了）→ -15% 点击率 → 减分

本规则只加分减分、不硬阻塞；配置不可用则降级放行。reasons 以「R11数据:」前缀，
便于下游 verify_publish_gate 与硬违规分桶。

硬项（不达标退出码 1）
  A. 配置结构——config/title_positive.yaml 字段齐备、赛事名词不含联赛名
  B. 规则冒烟——title_positive.selftest() 全通过
  C. 回归样例——≥8 组标题对，逐条校验加分/减分方向正确
  D. 生成侧接入——orchestrator.score_title_plan / score_fixture_title 排序不变量成立
  E. 提示词一致性——topic_selector / rewrite_article 已改为 21-30 字且不再写 15-25

告警项（只提示）
  F. 近 14 天成稿命中统计（赛事名词/情绪/悬念分布，仅可观测）

用法：
  python3 scripts/verify_title_positive.py [--quiet]
产出：data/audit/title_positive_<date>.md + title_positive_latest.json
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import title_positive as tp  # noqa: E402

PROMPTS_DIR = PROJECT_ROOT / "prompts"
OUTPUT_DIR = PROJECT_ROOT / "output"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

# 回归样例：每组 (名称, 标题, 基线标题, 方向, 标题kwargs, 基线kwargs)
#   方向 "gt" → evaluate(标题) 的 bonus 应严格大于 evaluate(基线)；
#        "lt" → 应严格小于。用于固化「加分 / 减分」方向不回归。
REGRESSION_PAIRS = [
    ("悬念词加分高于中性", "没想到" + "某" * 22, "这场较量" + "某" * 21, "gt", {}, {}),
    ("21-30 最优带高于短标题", "这场较量" + "某" * 21, "这场较量" + "某" * 15, "gt", {}, {}),
    ("赛事名词无具体性低于含人名", "世界杯" + "某" * 14, "世界杯梅西" + "某" * 12, "lt", {}, {}),
    ("情绪词低于中性", "防线炸了", "防线稳固", "lt", {}, {}),
    ("崩盘豁免高于纯崩", "下半场崩盘", "下半场崩了", "gt", {}, {}),
    ("联赛名不罚高于赛事名词", "中超争冠白热化" + "某" * 6, "世界杯" + "某" * 10, "gt", {}, {}),
    ("known_teams 豁免高于无队名", "皇马的世界杯之旅" + "某" * 5, "世界杯之旅" + "某" * 7,
     "gt", {"known_teams": {"皇马"}}, {}),
    ("情绪词组合低于中性", "绝了这场比赛真是泪目", "这场比赛令人动容", "lt", {}, {}),
]


def check_config() -> dict:
    raw = tp.load_config()
    if raw is None:
        return {"item": "配置结构（config/title_positive.yaml）", "ok": False, "advisory": False,
                "detail": ["❌ 配置缺失或不可解析（规则将降级放行，属漂移隐患）"]}
    problems = []
    for key in ("版本", "生效日期", "口径出处", "悬念词", "最优长度带",
                "赛事名词", "情绪词", "加分", "减分"):
        if raw.get(key) in (None, "", [], {}):
            problems.append(f"缺字段 {key}")
    band = raw.get("最优长度带") or []
    if not (isinstance(band, (list, tuple)) and len(band) == 2
            and isinstance(band[0], int) and isinstance(band[1], int)):
        problems.append("最优长度带应为 [int, int]")
    add = raw.get("加分") or {}
    sub = raw.get("减分") or {}
    for k in ("悬念", "最优带"):
        if not isinstance(add.get(k), int):
            problems.append(f"加分.{k} 应为整数")
    for k in ("赛事名词", "情绪词"):
        if not isinstance(sub.get(k), int):
            problems.append(f"减分.{k} 应为整数")
    # 联赛名回归守护：赛事名词表不得含 中超/英超/西甲/意甲/德甲/法甲
    forbidden = {"中超", "英超", "西甲", "意甲", "德甲", "法甲"}
    leak = [x for x in (raw.get("赛事名词") or []) if x in forbidden]
    if leak:
        problems.append(f"赛事名词表含联赛名（应剔除）：{leak}")
    return {"item": "配置结构（config/title_positive.yaml）", "ok": not problems,
            "advisory": False, "version": raw.get("版本"),
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"配置 v{raw.get('版本')} 字段齐备；赛事名词 {len(raw.get('赛事名词') or [])} 个"
                             f"（不含联赛名）✅",
                             f"加分 {add}；减分 {sub} ✅"])}


def check_selftest() -> dict:
    cases = tp.selftest()
    bad = [n for n, ok in cases if not ok]
    return {"item": "规则冒烟（selftest 全通过）", "ok": not bad, "advisory": False,
            "total": len(cases),
            "detail": ([f"❌ 未通过：{bad}"] if bad
                       else [f"{len(cases)} 项冒烟全部通过 ✅"])}


def check_regression() -> dict:
    fails = []
    for name, title, base, op, tk, bk in REGRESSION_PAIRS:
        b_t = tp.evaluate(title, **tk)["bonus"]
        b_b = tp.evaluate(base, **bk)["bonus"]
        ok = (b_t > b_b) if op == "gt" else (b_t < b_b)
        if not ok:
            fails.append(f"{name}：标题 bonus={b_t} 基线 bonus={b_b}（期望 {op}）")
    return {"item": "回归样例（加分/减分方向）", "ok": not fails, "advisory": False,
            "total": len(REGRESSION_PAIRS),
            "detail": ([f"❌ {'；'.join(fails)}"] if fails
                       else [f"{len(REGRESSION_PAIRS)} 组标题对方向校验全部通过 ✅"])}


def check_orchestrator() -> dict:
    try:
        import orchestrator
    except Exception as e:  # pragma: no cover
        return {"item": "生成侧接入（score_title_plan / score_fixture_title）", "ok": False,
                "advisory": False, "detail": [f"❌ 无法载入 orchestrator：{e}"]}
    problems = []

    # 1) 含具体人名版本应高于仅赛事名词版本
    ev = "世界杯" + "某" * 14
    pe = "世界杯梅西" + "某" * 12
    s_ev, _, _ = orchestrator.score_title_plan({}, ev, "热点球评")
    s_pe, _, _ = orchestrator.score_title_plan({}, pe, "热点球评")
    if not (s_pe > s_ev):
        problems.append(f"含人名版本({s_pe})应 > 仅赛事名词版本({s_ev})")

    # 2) 悬念+最优带版本应高于纯中性版本
    plain = "这场较量" + "某" * 22      # 26 字（最优带内），无悬念
    susp = "没想到" + "某" * 23         # 26 字，含悬念
    s_p, _, _ = orchestrator.score_title_plan({}, plain, "热点球评")
    s_s, _, _ = orchestrator.score_title_plan({}, susp, "热点球评")
    if not (s_s > s_p):
        problems.append(f"悬念+最优带版本({s_s})应 > 中性版本({s_p})")

    # 3) 模板前缀仍归零（R11 块不得复活）
    t = "老六精准预测：" + "没想到" + "某" * 20
    sc, ok, _ = orchestrator.score_title_plan({}, t, "热点球评")
    if sc != 0 or ok:
        problems.append(f"模板前缀标题应判 0 分，实为 {sc}/{ok}")

    # 4) score_fixture_title 可达且加分生效（含悬念词版本高于中性）
    topic = {"home_zh": "皇马", "away_zh": "巴萨"}
    fp = "皇马巴萨" + "某" * 22 + "呢"          # 27 字，含双队，无悬念
    fs = "没想到皇马巴萨" + "某" * 19 + "呢"     # 27 字，含双队 + 悬念
    s_fp, _ = orchestrator.score_fixture_title(topic, fp)
    s_fs, _ = orchestrator.score_fixture_title(topic, fs)
    if not (s_fs > s_fp):
        problems.append(f"赛程含悬念版本({s_fs})应 > 中性版本({s_fp})")

    return {"item": "生成侧接入（score_title_plan / score_fixture_title）", "ok": not problems,
            "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["排序不变量成立：人名>赛事名词、悬念>中性、模板前缀仍0、赛程悬念加分生效 ✅"])}


def check_prompt() -> dict:
    problems = []
    for fn in ("topic_selector.txt", "rewrite_article.txt"):
        p = PROMPTS_DIR / fn
        if not p.exists():
            problems.append(f"{fn} 缺失")
            continue
        text = p.read_text(encoding="utf-8")
        if "21-30" not in text:
            problems.append(f"{fn} 未包含「21-30」字长度指引")
        if "15-25" in text:
            problems.append(f"{fn} 仍残留「15-25」旧长度指引")
    # 悬念词指引必须落到两侧（0% 采用率事件后补：仅「优先使用」软建议无效）
    ts = PROMPTS_DIR / "topic_selector.txt"
    if ts.exists():
        t = ts.read_text(encoding="utf-8")
        if "悬念词配额" not in t or "没想到" not in t:
            problems.append("topic_selector.txt 缺 R11 悬念词配额（仅软建议时生成侧采用率为 0）")
    ra = PROMPTS_DIR / "rewrite_article.txt"
    if ra.exists():
        t = ra.read_text(encoding="utf-8")
        if "反转揭示词" not in t or "没想到" not in t:
            problems.append("rewrite_article.txt 缺反转揭示词要求（改写侧才是最终定标题处）")
    return {"item": "提示词一致性（21-30 字 / 悬念词配额 / 不再 15-25）", "ok": not problems,
            "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["topic_selector.txt（批次配额）与 rewrite_article.txt（单篇强偏好）"
                             "均含反转揭示词要求，21-30 字口径对齐 ✅"])}


def check_recent_output(days: int = 14) -> dict:
    floor = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    total = event = emo = sus = band = 0
    for p in sorted(glob.glob(str(OUTPUT_DIR / "**" / "metadata.json"), recursive=True)):
        d = Path(p).parent.name
        if d < floor:
            continue
        try:
            meta = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in (meta.get("articles") or []):
            t = (a.get("title") or "").strip()
            if not t:
                continue
            total += 1
            r = tp.evaluate(t)
            for reason in r["reasons"]:
                if "赛事名词" in reason:
                    event += 1
                elif "情绪词" in reason:
                    emo += 1
                elif "悬念词" in reason:
                    sus += 1
            if tp._in_band(t, tp.load_config() or {}):
                band += 1
    cfg = tp.load_config() or {}
    mon = cfg.get("监控") or {}
    susp_th = float(mon.get("悬念词采用率阈值", 0.2))
    band_th = float(mon.get("最优带采用率阈值", 0.5))
    detail = []
    ok = True
    if total:
        sp, bp = sus / total, band / total
        detail.append(f"近 {days} 天成稿 {total} 条：悬念词 {sus}（{sp:.0%}，阈值 {susp_th:.0%}）| "
                      f"21-30字 {band}（{bp:.0%}，阈值 {band_th:.0%}）| "
                      f"赛事名词 {event} | 情绪词 {emo}")
        if sp < susp_th:
            detail.append(f"⚠️ 悬念词采用率 {sp:.0%} 低于阈值——R11 回测悬念词 CTR +87%，"
                          f"evaluate() 只加分不强制，需从 prompts/topic_selector.txt 强化")
            ok = False
        if bp < band_th:
            detail.append(f"⚠️ 21-30 字采用率 {bp:.0%} 低于阈值——R11 回测该区间为最优长度带")
            ok = False
    else:
        detail.append(f"近 {days} 天无成稿，本项跳过")
    return {"item": f"近 {days} 天成稿 R11 采用率（告警，仅可观测）", "ok": ok,
            "advisory": True, "total": total, "event_noun": event,
            "emotion": emo, "suspense": sus, "optimal_band": band,
            "detail": detail}


def verify(days: int = 14) -> dict:
    hard = [check_config(), check_selftest(), check_regression(),
            check_orchestrator(), check_prompt()]
    soft = [check_recent_output(days)]
    checks = hard + soft
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "advisory_failed": [c["item"] for c in soft if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 标题正向规则验收报告（计划 3.3 / R11 内容诊断）", "",
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
    ap = argparse.ArgumentParser(description="标题正向规则验收（计划 3.3 / R11）")
    ap.add_argument("--days", type=int, default=14, help="近 N 天成稿告警窗口（默认 14）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify(days=args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"title_positive_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "title_positive_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
