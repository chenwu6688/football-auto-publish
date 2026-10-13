#!/usr/bin/env python3
"""选题分类与类别优先级验收（计划 5.1 / R11 内容诊断）。

背景（R11 内容诊断：869 条头条真实导出回测，2026-01-01~10-12）：
  有效选题 = 转会官宣 CTR 2.08% / 冲突舆情 1.55% / 中国足球 1.36%；
  战报比分式内容占产量 23.9%、CTR 仅 0.49%、0 爆款——减产（软上限，不硬禁）。
本脚本验收 topic_category 分类器与 orchestrator.select_topics 的接入，
防止分类规则漂移（词表误扩错杀有效选题 / 软上限失效 / 接入被回退）。

硬项（不达标退出码 1）
  A. 配置结构——config/topic_category.yaml 字段齐备、优先级含五类、
     各词表非空、形态正则可编译、监控阈值 0<v<1
  B. 规则冒烟——topic_category.selftest() 全通过（五类正例 / 形态优先 /
     prioritize tier+cap / 降级可用）
  C. 回归语料——脚本内嵌 ≥15 条带期望标签的选题，classify 准确率 100%
     （覆盖五类 + 形态优先反例 + 未分类）
  D. 编排接入——import orchestrator 成功（失败→skipped 不算失败）；
     模块命名空间可见 topic_category；近 14 天 output/*/metadata.json
     的 topics 全量 classify 不抛异常

告警项（只提示，不阻断）
  E. 近 14 天真实选题分类分布（战报比分占比 vs 阈值——存量提示）

用法：
  python3 scripts/verify_topic_category.py [--quiet]
产出：data/audit/topic_category_<date>.md + topic_category_latest.json
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

import topic_category as tc  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
CST = timezone(timedelta(hours=8))

FIVE = (tc.CAT_TRANSFER, tc.CAT_CONFLICT, tc.CAT_CHINA, tc.CAT_UNKNOWN, tc.CAT_SCORELINE)

# 回归语料：内嵌带期望标签的选题（topic dict 或标题均可）。
# 覆盖：五类各 ≥2 + 形态优先反例（比分+中国足球词→战报比分）+ content_type 路径 + 未分类。
REGRESSION_CASES = [
    ({"title": "罗马诺：皇马官宣签下姆巴佩，五年合同锁死核心"}, tc.CAT_TRANSFER),
    ({"title": "曼城转会费创纪录，赖斯续约谈判进入最后阶段"}, tc.CAT_TRANSFER),
    ({"title": "深夜官宣！新帅下课后俱乐部迎来第一笔签约"}, tc.CAT_TRANSFER),
    ({"title": "今晚有大事发生", "content_type": "转会资讯"}, tc.CAT_TRANSFER),
    ({"title": "VAR争议判罚引爆全场，两队球迷赛后互喷"}, tc.CAT_CONFLICT),
    ({"title": "更衣室内讧彻底曝光，主力前锋炮轰主帅战术"}, tc.CAT_CONFLICT),
    ({"title": "种族歧视风波发酵，足协开出重磅罚单"}, tc.CAT_CONFLICT),
    ({"title": "国足世预赛26人名单出炉，三名新人首次入选"}, tc.CAT_CHINA),
    ({"title": "中超争冠进入白热化，申花海港同分进入决胜时刻"}, tc.CAT_CHINA),
    ({"title": "足协杯半决赛对阵出炉，中甲黑马玉昆继续神奇"}, tc.CAT_CHINA),
    ({"title": "亚泰主场1-5惨败，无锡吴钩狂轰5球"}, tc.CAT_SCORELINE),
    ({"title": "荷兰2-2日本：傲慢的代价"}, tc.CAT_SCORELINE),
    ({"title": "3-0！蓝月亮全场碾压，多点开花拿下三分"}, tc.CAT_SCORELINE),
    # 形态优先：比分 + 中国足球词仍判战报比分（不因国安/中超改判中国足球）
    ({"title": "国安3-1大胜鲁能，中超争冠主动权易手"}, tc.CAT_SCORELINE),
    ({"title": "国足2-0取胜，世预赛关键三分落袋"}, tc.CAT_SCORELINE),
    # 未分类：无任何命中词
    ({"title": "今晚的月亮真圆，适合静静看球"}, tc.CAT_UNKNOWN),
    ({"title": "梅西与C罗的商业版图之争，谁更会做生意"}, tc.CAT_UNKNOWN),
]


def check_config() -> dict:
    raw = tc.load_config()
    if raw is None:
        return {"item": "配置结构（config/topic_category.yaml）", "ok": False, "advisory": False,
                "detail": ["❌ 配置缺失或不可解析（分类器将降级走 _FALLBACK，属漂移隐患）"]}
    problems = []
    for key in ("版本", "生效日期", "口径出处", "类别优先级", "单批软上限", "监控",
                "比分模式", "赛果模式", "转会关键词", "冲突关键词", "中国足球关键词"):
        if raw.get(key) in (None, "", [], {}):
            problems.append(f"缺字段 {key}")
    pr = [str(x) for x in (raw.get("类别优先级") or [])]
    for c in FIVE:
        if c not in pr:
            problems.append(f"类别优先级缺类别 {c}")
    soft = raw.get("单批软上限") or {}
    if not isinstance(soft, dict) or tc.CAT_SCORELINE not in soft:
        problems.append("单批软上限缺战报比分键（软上限失效）")
    for key in ("比分模式", "赛果模式", "转会关键词", "冲突关键词", "中国足球关键词"):
        if not (raw.get(key) or []):
            problems.append(f"{key} 为空（会导致大量未分类）")
    for key in ("比分模式", "赛果模式"):
        import re as _re
        for pat in (raw.get(key) or []):
            try:
                _re.compile(str(pat))
            except Exception as e:
                problems.append(f"{key} 正则不可编译：{pat}（{e}）")
    mon = raw.get("监控") or {}
    for k in ("战报比分占比阈值", "未分类占比阈值"):
        try:
            v = float(mon.get(k))
        except (TypeError, ValueError):
            problems.append(f"监控阈值 {k} 非数值：{mon.get(k)!r}")
            continue
        if not 0.0 < v < 1.0:
            problems.append(f"监控阈值 {k}={v} 越界（应 0<v<1）")
    return {"item": "配置结构（config/topic_category.yaml）", "ok": not problems,
            "advisory": False, "version": raw.get("版本"),
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"配置 v{raw.get('版本')} 字段齐备；优先级 {pr} ✅",
                             f"软上限 {soft}；词表 比分 {len(raw.get('比分模式') or [])} / "
                             f"赛果 {len(raw.get('赛果模式') or [])} / 转会 "
                             f"{len(raw.get('转会关键词') or [])} / 冲突 {len(raw.get('冲突关键词') or [])} / "
                             f"中国足球 {len(raw.get('中国足球关键词') or [])} ✅"])}


def check_selftest() -> dict:
    cases = tc.selftest()
    bad = [n for n, ok in cases if not ok]
    return {"item": "规则冒烟（五类正例 / 形态优先 / prioritize / 降级）", "ok": not bad,
            "advisory": False, "total": len(cases),
            "detail": ([f"❌ 未通过：{bad}"] if bad else [f"{len(cases)} 项冒烟全部通过 ✅"])}


def check_regression() -> dict:
    fails = []
    for topic, expect in REGRESSION_CASES:
        got = tc.classify(topic)
        if got != expect:
            fails.append(f"应为 {expect} 实为 {got}：{topic.get('title')!r}")
    return {"item": f"回归语料（{len(REGRESSION_CASES)} 条内嵌选题）", "ok": not fails,
            "advisory": False, "total": len(REGRESSION_CASES),
            "detail": ([f"❌ {'；'.join(fails)}"] if fails
                       else [f"{len(REGRESSION_CASES)} 条回归语料分类全部正确 ✅（含形态优先反例与 content_type 路径）"])}


def check_orchestrator(days: int = 14) -> dict:
    try:
        import orchestrator  # noqa: F401
    except Exception as e:
        return {"item": "编排接入（orchestrator.select_topics）", "ok": True, "advisory": False,
                "skipped": True, "detail": [f"无法载入 orchestrator（{e}），本项跳过（不算失败）"]}
    problems = []
    if not hasattr(orchestrator, "topic_category"):
        problems.append("orchestrator 模块命名空间不可见 topic_category（import 被回退？）")
    # 近 14 天真实选题全量 classify 不抛异常
    floor = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    n_topics = 0
    n_files = 0
    for p in sorted(glob.glob(str(OUTPUT_DIR / "**" / "metadata.json"), recursive=True)):
        d = Path(p).parent.name
        if d < floor:
            continue
        try:
            meta = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        topics = meta.get("topics")
        if not isinstance(topics, list):
            continue
        n_files += 1
        for t in topics:
            if not isinstance(t, dict):
                continue
            try:
                tc.classify(t)
            except Exception as e:
                problems.append(f"{d} 选题 classify 抛异常：{e}（{t.get('title')!r}）")
            n_topics += 1
    return {"item": "编排接入（import / 命名空间 / 真实选题回归）", "ok": not problems,
            "advisory": False, "files": n_files, "topics": n_topics,
            "detail": (problems if problems else
                       [f"orchestrator 命名空间可见 topic_category ✅",
                        f"近 {days} 天 {n_files} 个 metadata、{n_topics} 条真实选题 classify 全部无异常 ✅"])}


def check_recent_output(days: int = 14) -> dict:
    floor = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    counts: dict = {}
    n = 0
    for p in sorted(glob.glob(str(OUTPUT_DIR / "**" / "metadata.json"), recursive=True)):
        d = Path(p).parent.name
        if d < floor:
            continue
        try:
            meta = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        topics = meta.get("topics")
        if not isinstance(topics, list):
            continue
        for t in topics:
            if not isinstance(t, dict):
                continue
            c = tc.classify(t)
            counts[c] = counts.get(c, 0) + 1
            n += 1
    cfg = tc.load_config() or tc._FALLBACK
    mon = cfg.get("监控") or {}
    sc_th = float(mon.get("战报比分占比阈值", 0.5))
    un_th = float(mon.get("未分类占比阈值", 0.8))
    sc_ratio = counts.get(tc.CAT_SCORELINE, 0) / n if n else 0.0
    un_ratio = counts.get(tc.CAT_UNKNOWN, 0) / n if n else 0.0
    detail = [f"近 {days} 天真实选题 {n} 条：" +
              ("、".join(f"{k} {v}（{v / n:.0%}）" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
               if n else "无数据")]
    ok = True
    if n:
        if sc_ratio > sc_th:
            detail.append(f"⚠️ 战报比分占比 {sc_ratio:.0%} > {sc_th:.0%}——存量选题仍偏战报形态，"
                          f"随生成侧优先级生效将逐步稀释")
            ok = False
        if un_ratio > un_th:
            detail.append(f"⚠️ 未分类占比 {un_ratio:.0%} > {un_th:.0%}——词表覆盖不足，"
                          f"建议回测支撑后扩充词表（遵循调参纪律）")
            ok = False
    return {"item": f"近 {days} 天真实选题分类分布（存量，仅提示）", "ok": ok, "advisory": True,
            "total": n, "counts": counts, "detail": detail}


def verify(days: int = 14) -> dict:
    hard = [check_config(), check_selftest(), check_regression(), check_orchestrator(days)]
    soft = [check_recent_output(days)]
    checks = hard + soft
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "advisory_failed": [c["item"] for c in soft if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 选题分类与类别优先级验收报告（计划 5.1 / R11 内容诊断）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警项" if c.get("advisory") else ("硬项（跳过）" if c.get("skipped") else "硬项")
        L.append(f"## {c['item']}（{tag}）：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    if rep.get("advisory_failed"):
        L += [f"> 告警项待关注：{'、'.join(rep['advisory_failed'])}", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="选题分类与类别优先级验收（计划 5.1 / R11 内容诊断）")
    ap.add_argument("--days", type=int, default=14, help="回看窗口天数（默认 14）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify(days=args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"topic_category_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "topic_category_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
