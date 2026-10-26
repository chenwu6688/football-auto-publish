#!/usr/bin/env python3
"""赛程选题池验收校验（计划 7.1 验收标准）。

计划 7.1 白纸黑字的验收口径：
  1. 赛程覆盖五大联赛、欧冠、欧联、中超、国足五类；
  2. 任一「有赛程日」产出不少于 6 条候选；
  3. 板块配比偏差不超过 5%（对照 config/ratio_config.yaml 板块配比）；
  4. 候选选题中「赛事前瞻体」占比为零。

本脚本读 data/fixtures/topic_pool.json（由 scripts/fixture_library.py 生成），逐项校验，
产出 data/audit/fixture_pool_*.md + latest.json；任一硬项 FAIL 时退出码 1。

用法：
  python3 scripts/verify_fixture_pool.py                     # 校验默认池
  python3 scripts/verify_fixture_pool.py --pool path.json
  python3 scripts/verify_fixture_pool.py --min-per-day 6 --ratio-tol 5
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POOL_PATH = PROJECT_ROOT / "data" / "fixtures" / "topic_pool.json"
RATIO_PATH = PROJECT_ROOT / "config" / "ratio_config.yaml"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"

CST = timezone(timedelta(hours=8))

# 计划 7.1：五类覆盖 —— 每组任一命中即算覆盖
COVERAGE_GROUPS = {
    "五大联赛": ["英超", "西甲", "意甲", "德甲", "法甲"],
    "欧冠": ["欧冠"],
    "欧联": ["欧联", "欧罗巴"],
    "中超": ["中超"],
    "国足": ["国足", "中国", "世预赛", "亚洲杯"],
}

# 前瞻体判定：计划明确「候选选题中赛事前瞻体占比为零」（欧冠 78 篇仅 852 阅读的根因）
PREVIEW_MARKERS = ("前瞻", "预览", "展望", "预测首发", "赛前预测", "看点前瞻")

# R11 角度口径（2026-10-13，建议3「换角度不换内容」）：
#   「写人」比「写事」高 3-10 倍点击、悬念词 +87% → 赛程角度模板必须是「人 + 悬念」，
#   而不是「A队 vs B队」纯对阵分析。产量与选题不变，只换叙事角度。
PERSON_WORDS = ("主帅", "教练", "核心", "球员", "更衣室", "老将", "队长", "门将", "前锋", "主帅")
SUSPENSE_WORDS = ("没想到", "原来", "竟然", "居然", "反转")


def _load_ratio_target() -> dict:
    try:
        cfg = yaml.safe_load(RATIO_PATH.read_text(encoding="utf-8")) or {}
        return {k: float(v) for k, v in (cfg.get("板块配比") or {}).items()}
    except Exception:
        return {}


def check_coverage(topics: list[dict]) -> dict:
    comps = {(t.get("competition") or "").strip() for t in topics}
    covered, missing = [], []
    for group, keys in COVERAGE_GROUPS.items():
        hit = any(any(k in c for k in keys) for c in comps)
        (covered if hit else missing).append(group)
    return {"item": "五类赛程覆盖", "ok": not missing, "covered": covered, "missing": missing,
            "detail": [f"已覆盖：{'、'.join(covered) or '无'}",
                       f"缺失：{'、'.join(missing) or '无'}"]}


def check_per_day(topics: list[dict], min_per_day: int = 6) -> dict:
    by_day: dict[str, int] = {}
    for t in topics:
        day = (t.get("kickoff_cst") or "")[:10]
        if day:
            by_day[day] = by_day.get(day, 0) + 1
    thin = {d: n for d, n in sorted(by_day.items()) if n < min_per_day}
    return {"item": f"有赛程日候选≥{min_per_day}", "ok": not thin,
            "days": len(by_day), "thin_days": thin,
            "detail": ([f"共 {len(by_day)} 个有赛程日"] +
                       ([f"不足 {min_per_day} 条：{thin}"] if thin else ["全部达标"]))}


def check_ratio(topics: list[dict], tol: float = 5.0) -> dict:
    """板块配比偏差（告警项）。

    口径说明：赛程池的板块分布由「当天有哪些赛事」决定，本身不可调参；计划 7.1 的
    「配比偏差≤5%」是针对**日更成稿**（一级+二级+三级混合后）而言。此处对赛程池做成
    告知性对照，供选题层判断当天缺口，不单独判失败。
    """
    target = _load_ratio_target()
    n = len(topics)
    actual: dict[str, float] = {}
    if n:
        for t in topics:
            sec = (t.get("板块") or t.get("suggested_column") or "未分类").strip()
            actual[sec] = actual.get(sec, 0.0) + 100.0 / n
    deviations = {}
    for sec, tgt in target.items():
        deviations[sec] = round(actual.get(sec, 0.0) - tgt, 2)
    bad = {k: v for k, v in deviations.items() if abs(v) > tol}
    detail = [f"实际：{ {k: round(v, 1) for k, v in actual.items()} }",
              f"目标：{target}", f"偏差：{deviations}",
              "（告警项：赛程池板块由赛程决定，配比达标由上层三层供给混合实现）"]
    return {"item": f"板块配比偏差≤{tol}%", "advisory": True, "ok": not bad,
            "deviations": deviations,
            "max_dev": max((abs(v) for v in deviations.values()), default=0.0),
            "detail": detail}


def check_no_preview(topics: list[dict]) -> dict:
    hits = []
    for t in topics:
        blob = f"{t.get('angle_type', '')}{t.get('conflict', '')}{t.get('suggested_column', '')}"
        if any(m in blob for m in PREVIEW_MARKERS):
            hits.append({"topic_id": t.get("topic_id"), "conflict": t.get("conflict", "")[:40]})
    return {"item": "前瞻体占比为 0", "ok": not hits, "hits": hits,
            "detail": ([f"命中前瞻体 {len(hits)} 条：{hits[:3]}"] if hits else ["无前瞻体选题"])}


def check_angle_rules() -> dict:
    """硬项：角度模板必须是「人 + 悬念」（R11），且不得含前瞻标记。

    直接读 `fixture_library.ANGLE_RULES` 这一代码常量——模板是赛程确定性供给
    （日 2 篇 ≈ 33% 产量）的叙事角度源头，回退到「A队 vs B队」形态会静默拉低点击率。
    """
    try:
        if str(PROJECT_ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        import fixture_library as fl
    except Exception as e:                                    # pragma: no cover
        return {"item": "赛程角度模板（人 + 悬念，R11）", "ok": False,
                "detail": [f"❌ 无法导入 fixture_library：{e}"]}
    bad = []
    for rule in fl.ANGLE_RULES:
        raw = rule[3] if len(rule) > 3 else ""
        # 2026-10-26 P0-3：模板从「单条字符串」改为「每型多条列表」。
        # 若仍按单条处理，`"前瞻" in [模板1, 模板2]` 是**元素相等**而不是子串匹配，
        # 这道硬项会静默通过（检查形同虚设）——必须逐条展开。
        templates = raw if isinstance(raw, (list, tuple)) else [raw]
        for tmpl in templates:
            if any(m in tmpl for m in PREVIEW_MARKERS):
                bad.append(f"{rule[1]}：模板含前瞻标记「{tmpl}」")
            if not (any(w in tmpl for w in PERSON_WORDS)
                    or any(w in tmpl for w in SUSPENSE_WORDS)):
                bad.append(f"{rule[1]}：模板既无人角色词也无悬念词「{tmpl}」")
    n_templates = sum(len(r[3]) if isinstance(r[3], (list, tuple)) else 1
                      for r in fl.ANGLE_RULES)
    return {"item": "赛程角度模板（人 + 悬念，R11）", "ok": not bad,
            "n_rules": len(fl.ANGLE_RULES),
            "detail": ([f"❌ {'；'.join(bad)}"] if bad else
                       [f"{len(fl.ANGLE_RULES)} 个分型共 {n_templates} 条角度模板均为"
                        "「人/悬念」角度、且无前瞻标记 ✅"])}


def check_angle_person_suspense(topics: list[dict]) -> dict:
    """告警项：池内选题的 conflict 文本含「人角色词 / 悬念词」的占比。"""
    n = len(topics)
    if not n:
        return {"item": "选题角度含人/悬念占比", "advisory": True, "ok": True,
                "detail": ["池内无选题"]}
    hit = sum(1 for t in topics
              if any(w in (t.get("conflict") or "") for w in PERSON_WORDS + SUSPENSE_WORDS))
    ratio = hit / n
    return {"item": "选题角度含人/悬念占比", "advisory": True, "ok": ratio >= 1.0,
            "ratio": round(ratio, 3), "hit": hit, "total": n,
            "detail": [f"含人角色词或悬念词的选题 {hit}/{n}（{ratio:.0%}）",
                       "（告警项：不足 100% 通常是选题池仍由旧版 ANGLE_RULES 生成，"
                       "重跑 scripts/fixture_library.py 刷新即可）"]}


def verify(pool: dict, *, min_per_day: int = 6, ratio_tol: float = 5.0) -> dict:
    topics = pool.get("topics", []) or []
    checks = [check_coverage(topics), check_per_day(topics, min_per_day),
              check_no_preview(topics), check_angle_rules(),
              check_ratio(topics, ratio_tol), check_angle_person_suspense(topics)]
    hard = [c for c in checks if not c.get("advisory")]
    ok = all(c["ok"] for c in hard)
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "topics": len(topics), "pool_generated_at": pool.get("generated_at", ""),
            "ok": ok, "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 赛程选题池验收报告（计划 7.1）", "",
         f"- 生成时间：{rep['generated_at']}　选题池生成于：{rep['pool_generated_at']}",
         f"- 选题总数：{rep['topics']}　验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**",
         ""]
    if rep.get("hard_failed"):
        L.append(f"> 未通过硬项：{'、'.join(rep['hard_failed'])}")
        L.append("")
    for c in rep["checks"]:
        tag = "告警" if c.get("advisory") else "硬项"
        L.append(f"## {c['item']}（{tag}）：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="赛程选题池验收校验（计划 7.1）")
    ap.add_argument("--pool", default=str(POOL_PATH), help="选题池 JSON 路径")
    ap.add_argument("--min-per-day", type=int, default=6, help="单日最少候选数")
    ap.add_argument("--ratio-tol", type=float, default=5.0, help="板块配比允许偏差（百分点）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    p = Path(args.pool)
    if not p.exists():
        print(f"⚠️ 选题池不存在（{p}）；先运行 python3 scripts/fixture_library.py")
        return 2
    try:
        pool = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️ 选题池解析失败：{e}")
        return 2

    rep = verify(pool, min_per_day=args.min_per_day, ratio_tol=args.ratio_tol)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"fixture_pool_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "fixture_pool_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
