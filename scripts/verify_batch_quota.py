#!/usr/bin/env python3
"""单批篇数纪律验收（策略手册 第五章 / 完善计划 12.1）。

背景：策略手册第五章要求「日更从 8-11 篇压到 6-8 篇（晨读 2-3 / 午间 2-3 / 晚间 2）」，
完善计划 12.1 要求「日更 6 至 8 篇可稳定不断供」。改造前运行时上限为 4/4/5（日上限 13 篇），
且无任何验收覆盖，属必然漂移的隐患。本脚本把口径钉死，防止再漂回去。

硬项（不达标退出码 1）
  A. 常态上限——每批运行时上限 = 常态值 2 篇，且不高于手册每批上限
  B. 加发上限——每批加发上限 ≤ 手册每批上限，且 ≥ 常态上限；晚间不加发
  C. 日合计——常态合计 = 计划下限 6，全加发合计 ≤ 计划上限 8
  D. 加发原因白名单非空，且周四只对应 thursday_boost
  E. 三处口径一致——constants.BATCH_QUOTA / config.yaml article_count / ratio_config 日更篇数
  F. decide() 冒烟——常态/周四/S 级/白名单外/额度用尽/晚间六种情形裁定正确

告警项（只提示）
  G. 加发台账——周内加发次数 ≤ 2，留痕字段完整
  H. 窗口内单批实际篇数——改造前遗留的高单批产出（仅提示，不阻断）

用法：python3 scripts/verify_batch_quota.py [--days 14] [--quiet]
产出：data/audit/batch_quota_<date>.md + data/audit/batch_quota_latest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import batch_quota as bq  # noqa: E402
from constants import BATCH_CONFIG  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
CST = timezone(timedelta(hours=8))

# 手册侧参照上限（策略手册 第五章：晨读 2-3 篇、午间 2-3 篇、晚间 2 篇）。
# 这是外部规格，故意写在本脚本而非 constants，用来独立校验运行时配置没有越界。
MANUAL_BATCH_CAP = {"morning": 3, "noon": 3, "evening": 2}
MANUAL_DAILY_MIN, MANUAL_DAILY_MAX = 6, 8


def _runtime_caps() -> dict:
    return {b: int(BATCH_CONFIG[b].get("max_articles", -1))
            for b in MANUAL_BATCH_CAP if b in BATCH_CONFIG}


def check_normal_cap() -> dict:
    caps = _runtime_caps()
    pol = bq.policy()
    normal = int(pol["normal_max"])
    bad = [f"{b} 运行时 {v} 篇" for b, v in caps.items()
           if v != normal or v > MANUAL_BATCH_CAP[b]]
    return {"item": "常态上限（每批 = 常态值且不超手册）", "ok": not bad,
            "advisory": False, "caps": caps, "normal_max": normal,
            "manual_cap": MANUAL_BATCH_CAP,
            "detail": ([f"❌ 越界：{'；'.join(bad)}"] if bad
                       else [f"常态 {normal} 篇，运行时 {caps}，手册上限 {MANUAL_BATCH_CAP} ✅"])}


def check_uplift_cap() -> dict:
    pol = bq.policy()
    table = pol.get("uplift") or {}
    bad = []
    for b, manual in MANUAL_BATCH_CAP.items():
        up = int(table.get(b, pol["normal_max"]))
        if up > manual:
            bad.append(f"{b} 加发 {up} > 手册 {manual}")
        if up < int(pol["normal_max"]):
            bad.append(f"{b} 加发 {up} < 常态 {pol['normal_max']}")
    if int(table.get("evening", -1)) != int(pol["normal_max"]):
        bad.append("晚间未按手册固定 2 篇（应不加发）")
    return {"item": "加发上限不超手册（晚间不加发）", "ok": not bad,
            "advisory": False, "uplift": table, "manual_cap": MANUAL_BATCH_CAP,
            "detail": ([f"❌ {'；'.join(bad)}"] if bad
                       else [f"加发上限 {table}，晚间按手册固定 2 篇 ✅"])}


def check_daily_total() -> dict:
    normal = bq.normal_max()
    pol = bq.policy()
    table = pol.get("uplift") or {}
    normal_total = sum(int(BATCH_CONFIG.get(b, {}).get("max_articles", normal))
                       for b in MANUAL_BATCH_CAP)
    uplift_total = sum(int(table.get(b, normal)) for b in MANUAL_BATCH_CAP)
    ok = (normal_total == MANUAL_DAILY_MIN and uplift_total <= MANUAL_DAILY_MAX)
    return {"item": f"日合计（常态 ={MANUAL_DAILY_MIN}，全加发 <={MANUAL_DAILY_MAX}）", "ok": ok,
            "advisory": False, "normal_total": normal_total, "uplift_total": uplift_total,
            "detail": [f"常态合计 {normal_total} 篇，全加发合计 {uplift_total} 篇"
                       f"（计划 12.1 区间 {MANUAL_DAILY_MIN}-{MANUAL_DAILY_MAX}）"
                       + ("✅" if ok else " ❌")]}


def check_reasons() -> dict:
    reasons = bq.allowed_reasons()
    th = bq.policy().get("thursday_weekday")
    auto = bq.auto_reason(3)
    thursday_ok = (th is None) or (auto == "thursday_boost" and bq.is_uplift_reason("thursday_boost"))
    ok = bool(reasons) and thursday_ok
    return {"item": "加发原因白名单与周四映射", "ok": ok, "advisory": False,
            "reasons": list(reasons), "thursday_weekday": th,
            "detail": [f"白名单 {list(reasons)}，周四 → {auto}"
                       + ("✅" if ok else " ❌")]}


def check_consistency() -> dict:
    """三处口径必须一致：constants / config.yaml / ratio_config。"""
    import yaml
    normal = bq.normal_max()
    problems = []
    pol = bq.policy()
    table = pol.get("uplift") or {}
    uplift_total = sum(int(table.get(b, normal)) for b in MANUAL_BATCH_CAP)

    try:
        sched = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
        for name, cfg in (sched.get("schedule", {}).get("batches", {}) or {}).items():
            cnt = cfg.get("article_count")
            if cnt is not None and int(cnt) != normal:
                problems.append(f"config.yaml {name}.article_count={cnt} ≠ 常态 {normal}")
    except Exception as e:
        problems.append(f"config.yaml 读取失败: {e}")

    try:
        rc = yaml.safe_load((PROJECT_ROOT / "config" / "ratio_config.yaml").read_text(encoding="utf-8"))
        base = (rc.get("日更篇数") or {}).get("基准")
        if base is not None and int(base) != MANUAL_DAILY_MIN:
            problems.append(f"ratio_config 日更基准={base} ≠ 计划下限 {MANUAL_DAILY_MIN}")
        wd = (rc.get("日更篇数") or {}).get("按星期浮动") or {}
        peak = max([int(v) for v in wd.values()] or [0])
        if peak and peak != uplift_total:
            problems.append(f"ratio_config 按星期浮动峰值={peak} ≠ 全加发合计 {uplift_total}")
        weights = rc.get("时段权重") or {}
        if weights:
            s = sum(float(v) for v in weights.values())
            if abs(s - 1.0) > 0.02:
                problems.append(f"ratio_config 时段权重合计 {round(s, 3)} ≠ 1")
    except Exception as e:
        problems.append(f"ratio_config.yaml 读取失败: {e}")

    return {"item": "三处口径一致（constants / config / ratio_config）", "ok": not problems,
            "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"constants 常态 {normal}、uplift 合计 {uplift_total}，"
                             "config.yaml 与 ratio_config 均已对齐 ✅"])}


def check_selftest() -> dict:
    bad = [name for name, ok in bq.selftest() if not ok]
    return {"item": "decide() 冒烟（六种情形）", "ok": not bad, "advisory": False,
            "detail": ([f"❌ 未通过：{bad}"] if bad
                       else ["常态 / 周四加发 / S 级事件 / 白名单外 / 额度用尽 / 晚间不加发 全部正确 ✅"])}


def check_ledger() -> dict:
    today = datetime.now(CST).strftime("%Y-%m-%d")
    s = bq.summarize(date_str=today)
    return {"item": "加发台账（周内 ≤ 2 次且留痕完整）", "ok": s["within_limit"] and not s["incomplete"],
            "advisory": True, "summary": s,
            "detail": [f"累计 {s['total']} 条加发，本周 {s['this_week']}/{s['weekly_limit']}",
                       f"按原因：{s['by_reason'] or '无'}",
                       (f"❌ 留痕不完整：{s['incomplete']}" if s["incomplete"] else "留痕字段完整 ✅")]}


def check_actual_batches(days: int) -> dict:
    """窗口内单批实际篇数（改造前遗留会超限，仅提示）。"""
    today = datetime.now(CST)
    violations, batches = [], 0
    # 元数据里的 batch_name 是中文批次名，需映射回手册上限
    cap_by_name = {"晨读": MANUAL_BATCH_CAP["morning"],
                   "午间": MANUAL_BATCH_CAP["noon"],
                   "晚间": MANUAL_BATCH_CAP["evening"]}
    for i in range(days):
        ds = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        p = OUTPUT_DIR / ds / "metadata.json"
        if not p.exists():
            continue
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        per: dict[str, int] = {}
        for a in meta.get("articles", []) or []:
            name = a.get("batch_name") or "?"
            per[name] = per.get(name, 0) + 1
        for name, cnt in per.items():
            batches += 1
            cap = cap_by_name.get(name)
            if cap is not None and cnt > cap:
                violations.append(f"{ds} {name} {cnt} 篇")
    return {"item": f"窗口内单批实际篇数（{days} 天，仅提示）", "ok": not violations,
            "advisory": True, "violations": violations,
            "detail": [f"窗口内 {batches} 个批次；超出手册上限 {len(violations)} 个"
                       + ("（多为改造前遗留产出）" if violations else " ✅"),
                       *violations[:10]]}


def verify(days: int = 14) -> dict:
    hard = [check_normal_cap(), check_uplift_cap(), check_daily_total(),
            check_reasons(), check_consistency(), check_selftest()]
    soft = [check_ledger(), check_actual_batches(days)]
    checks = hard + soft
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "advisory_failed": [c["item"] for c in soft if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 单批篇数纪律验收报告（策略手册 第五章 / 计划 12.1）", "",
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
    ap = argparse.ArgumentParser(description="单批篇数纪律验收（策略手册第五章 / 计划 12.1）")
    ap.add_argument("--days", type=int, default=14, help="统计窗口天数（默认 14）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify(days=args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"batch_quota_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "batch_quota_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
