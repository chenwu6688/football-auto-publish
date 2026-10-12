#!/usr/bin/env python3
"""验收指标与合规熔断验收（计划第十章）。

计划第十章把验收分两类：「增长指标看效果，合规指标看底线」，并给出唯一硬门槛：
    **任一阶段出现一次不实内容级违规，立即暂停自动发布，回到人工审核模式直至定位原因。**

本脚本校验这条红线「真的会熔断」，以及指标口径的两条纪律：
    1. 六项指标与计划目标值一致；
    2. 增长指标未接入时一律标注「待接入」，绝不用 0 冒充真实数据。

校验项：
  A. 合规红线熔断（硬项）—— 登记「不实内容级违规」即暂停；解除后恢复；
     疑似系统性失真达阈值自动熔断（用隔离状态文件，不污染真实状态）；
  B. 六项指标口径与计划一致（硬项）—— 看板 TARGETS 与计划第十章逐项对齐；
  C. 未接入不冒充（硬项）—— 无数据时增长指标为 None，渲染为「待接入」，不为 0；
  D. 合规硬门槛语义（硬项）—— 看板 red_line_ok 仅在 false_content_count == 0 时为真。

产出 data/audit/acceptance_metrics_*.md + acceptance_metrics_latest.json；硬项 FAIL 退出码 1。

用法：
  python3 scripts/verify_acceptance_metrics.py
  python3 scripts/verify_acceptance_metrics.py --quiet
"""

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import compliance_guard as cg  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

# 计划第十章「当前 / 30 天 / 90 天」六项指标目标
PLAN_TARGETS = {
    "篇均阅读": ("118 次", "200 次", "300 次"),
    "整体点击率": ("2.8%", "3.5%", "5.0%"),
    "平均完成率": ("33%", "40%", "45%"),
    "粉丝阅读占比": ("0.6%", "2.0%", "5.0%"),
    "溯源覆盖率": ("无", "100%", "100%"),
    "不实违规次数": ("未统计", "0", "0"),
}


def _load_bmd():
    spec = importlib.util.spec_from_file_location(
        "build_metrics_dashboard", PROJECT_ROOT / "scripts" / "build_metrics_dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------
# A. 合规红线熔断
# ------------------------------------------------------------
def check_compliance_redline() -> dict:
    """A. 合规红线熔断（硬项）。"""
    d = tempfile.TemporaryDirectory()
    orig = cg.STATE_PATH
    cg.STATE_PATH = Path(d.name) / "state.json"
    steps, bad = [], []
    try:
        if cg.is_suspended():
            bad.append("初始状态不应为熔断")
        # 疑似系统性失真：阈值以下不熔断，达到阈值熔断
        below = cg.auto_suspend_if_systemic(cg.AUTO_SUSPEND_REJECT_THRESHOLD - 1, path=cg.STATE_PATH)
        if below or cg.is_suspended(cg.STATE_PATH):
            bad.append("阈值以下不应熔断")
        steps.append(f"打回 {cg.AUTO_SUSPEND_REJECT_THRESHOLD - 1} 次 → 不熔断 ✅")
        at = cg.auto_suspend_if_systemic(cg.AUTO_SUSPEND_REJECT_THRESHOLD, path=cg.STATE_PATH)
        if not at or not cg.is_suspended(cg.STATE_PATH):
            bad.append("达到阈值应自动熔断")
        steps.append(f"打回 {cg.AUTO_SUSPEND_REJECT_THRESHOLD} 次 → 自动熔断 ✅")
        cg.clear("验收脚本", path=cg.STATE_PATH)
        # 不实内容级违规：出现一次即熔断
        cg.record_violation(cg.FALSE_CONTENT, "验收用例", path=cg.STATE_PATH)
        st = cg.status(cg.STATE_PATH)
        if not st["suspended"] or st["false_content_count"] != 1:
            bad.append("登记不实内容级违反应立即熔断")
        steps.append(f"登记「{cg.FALSE_CONTENT}」→ 暂停（count={st['false_content_count']}）✅")
        cg.clear("验收脚本", path=cg.STATE_PATH)
        if cg.is_suspended(cg.STATE_PATH):
            bad.append("解除熔断后应恢复")
        steps.append("人工定位后解除 → 恢复自动发布 ✅")
    finally:
        cg.STATE_PATH = orig
        d.cleanup()
    return {"item": "合规红线熔断（第十章）", "ok": not bad, "steps": steps,
            "detail": (steps if not bad else bad)}


# ------------------------------------------------------------
# B. 指标口径
# ------------------------------------------------------------
def check_targets() -> dict:
    """B. 六项指标口径与计划一致（硬项）。"""
    bmd = _load_bmd()
    tg = bmd.TARGETS
    problems = []
    if set(tg.keys()) != set(PLAN_TARGETS.keys()):
        problems.append(f"指标集合不一致：{sorted(tg.keys())}")
    for k, (cur, t30, t90) in PLAN_TARGETS.items():
        row = tg.get(k) or {}
        if (row.get("当前"), row.get("30天"), row.get("90天")) != (cur, t30, t90):
            problems.append(f"{k} 目标不符：{row}")
    return {"item": "六项指标口径与计划一致（第十章）", "ok": not problems,
            "targets": {k: [v["当前"], v["30天"], v["90天"]] for k, v in tg.items()},
            "detail": ([f"六项指标与计划第十章逐项对齐 ✅（{len(tg)} 项）"] if not problems else problems)}


# ------------------------------------------------------------
# C. 未接入不冒充
# ------------------------------------------------------------
def check_no_fake_zero() -> dict:
    """C. 未接入不冒充（硬项）。"""
    bmd = _load_bmd()
    d = tempfile.TemporaryDirectory()
    orig = bmd.OUTPUT_DIR
    bmd.OUTPUT_DIR = Path(d.name) / "output"     # 空目录 → 无任何数据
    try:
        data = bmd.collect(30)
        growth = data["growth"]
        zero_like = {k: v for k, v in growth.items()
                     if k != "_data_points" and v is not None}
        fmt_none = bmd._fmt(None)
    finally:
        bmd.OUTPUT_DIR = orig
        d.cleanup()
    ok = (not zero_like) and ("待接入" in fmt_none)
    return {"item": "未接入不冒充（第十章）", "ok": ok,
            "growth_when_empty": growth,
            "detail": ([f"无数据时六项增长指标均为 None（非 0）：{growth} ✅",
                        f"空值渲染为「{fmt_none}」✅"]
                       if ok else [f"❌ 疑似用 0 冒充：{zero_like}；_fmt(None)={fmt_none}"])}


# ------------------------------------------------------------
# D. 合规硬门槛语义
# ------------------------------------------------------------
def check_red_line_semantics() -> dict:
    """D. 合规硬门槛语义（硬项）。"""
    bmd = _load_bmd()
    d = tempfile.TemporaryDirectory()
    orig = cg.STATE_PATH
    cg.STATE_PATH = Path(d.name) / "state.json"
    try:
        clean = bmd.compliance_block()
        cg.record_violation(cg.FALSE_CONTENT, "验收用例2", path=cg.STATE_PATH)
        dirty = bmd.compliance_block()
    finally:
        cg.STATE_PATH = orig
        d.cleanup()
    ok = (clean.get("red_line_ok") is True and dirty.get("red_line_ok") is False)
    return {"item": "合规硬门槛语义（第十章）", "ok": ok,
            "detail": ([f"无违规 → red_line_ok=True；有 1 次不实违规 → red_line_ok=False ✅"]
                       if ok else [f"❌ red_line_ok 语义不符：clean={clean}, dirty={dirty}"])}


def verify() -> dict:
    """运行全部校验项（合规熔断的控制台输出在此静默，报告只回传结构化结论）。"""
    with contextlib.redirect_stdout(io.StringIO()):
        checks = [check_compliance_redline(), check_targets(),
                  check_no_fake_zero(), check_red_line_semantics()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 验收指标与合规熔断验收报告（计划第十章）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警" if c.get("advisory") else "硬项"
        L.append(f"## {c['item']}（{tag}）：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="验收指标与合规熔断验收（计划第十章）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"acceptance_metrics_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "acceptance_metrics_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
