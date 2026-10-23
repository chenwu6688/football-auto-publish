#!/usr/bin/env python3
"""调度检查口径与告警可信度验收（2026-10-22 / 10-23 两次假警事故）。

两次事故暴露的是同一类问题：**告警说的和事实不一致**，而且从手机上看不出原因。

  1. 10-23 02:33 收到「2026-10-23 应完成批次（morning）全部缺失」——
     其实是 10-22 22:17 的终检被 GitHub 拖到跨日才跑，旧逻辑拿「运行日」当今天，
     而 10-23 的晨读 07:00 才开始 → 必然假警。
     同理 12:37 的午检拖到 17:3x 会被判成终检 → 误报 evening 缺失。
  2. 10-22 19:31 收到「头条导出自动回收运行失败（登录过期/页面改版/推送失败之一）」——
     真实原因是 perf 页表格区未渲染（CDN chunk 超时）。旧文案把人往「改版」上带，
     真实原因只写在 Actions 日志里，手机上根本看不到。

本脚本把这两条钉死：
  A. 调度口径（硬项）—— heartbeat 必须用 scripts/resolve_check_slot.py 反推
     目标日期/时段，且不得再用运行时刻（date +%Y-%m-%d / date +%H）判断；
  B. 跨日回归（硬项）—— 终检延迟到次日 02:33 仍解析为前一天的 final；
     午检延迟到 17:33 仍解析为当天 midday；
  C. 告警文案（硬项）—— heartbeat 不再写死「今日应完成批次」；
  D. 可诊断性（硬项）—— collector 失败通知必须读脚本落的状态文件取真实原因，
     而不是只给「登录过期/页面改版/推送失败之一」。

产出 data/audit/check_slot_<date>.md + check_slot_latest.json；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_check_slot.py [--quiet]
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import resolve_check_slot as rcs  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
HEARTBEAT = PROJECT_ROOT / ".github" / "workflows" / "heartbeat.yml"
COLLECTOR_WF = PROJECT_ROOT / ".github" / "workflows" / "toutiao-collector.yml"
CST = timezone(timedelta(hours=8))


def _run_blocks(path: Path) -> str:
    """把 workflow 里所有 run: 块拼起来（只看脚本，不看注释头）。"""
    text = path.read_text(encoding="utf-8")
    return "\n".join(re.findall(r"run:\s*\|-?\n((?:\s{6,}.*\n)+)", text))


def check_heartbeat_uses_slot() -> dict:
    text = HEARTBEAT.read_text(encoding="utf-8")
    script = _run_blocks(HEARTBEAT)
    detail, ok = [], True
    if "resolve_check_slot.py" not in script:
        ok = False
        detail.append("❌ heartbeat 未调用 scripts/resolve_check_slot.py（目标日期仍按运行时刻算）")
    else:
        detail.append("✅ heartbeat 调用 resolve_check_slot.py 反推目标日期/时段")
    if re.search(r"date\s+\+%Y-%m-%d", script):
        ok = False
        detail.append("❌ 仍有 `date +%Y-%m-%d` 当作检查日期（延迟跨日会查错天）")
    if re.search(r"date\s+\+%H", script):
        ok = False
        detail.append("❌ 仍有 `date +%H` 判断午检/终检（延迟过 15 点会误判成终检）")
    if "步骤.period" in text or "steps.period." in text:
        ok = False
        detail.append("❌ 仍引用已删除的 steps.period")
    return {"item": "调度口径按调度时刻而非运行时刻", "ok": ok, "detail": detail}


def check_delayed_slots() -> dict:
    final_cron, midday_cron = "17 14 * * *", "37 4 * * *"
    cases = [
        ("终检延迟跨日 02:33 CST", final_cron,
         datetime(2026, 10, 22, 18, 33, tzinfo=timezone.utc), ("2026-10-22", "final")),
        ("午检延迟到 17:33 CST", midday_cron,
         datetime(2026, 10, 22, 9, 33, tzinfo=timezone.utc), ("2026-10-22", "midday")),
        ("终检准点 22:20 CST", final_cron,
         datetime(2026, 10, 22, 14, 20, tzinfo=timezone.utc), ("2026-10-22", "final")),
    ]
    detail, ok = [], True
    for name, cron, now, want in cases:
        got = rcs.resolve_slot(cron, now)
        good = got == want
        ok = ok and good
        detail.append(f"{'✅' if good else '❌'} {name} → {got}（应 {want}）")
    return {"item": "延迟调度跨日回归", "ok": ok, "detail": detail}


def check_alert_wording() -> dict:
    text = HEARTBEAT.read_text(encoding="utf-8")
    ok = "今日应完成批次" not in text
    return {"item": "告警文案不写死「今日」", "ok": ok,
            "detail": ["✅ 告警按目标日期描述（可能是前一天）" if ok else
                       "❌ 仍写「今日应完成批次」——日期错位时文案会误导"]}


def _strip_yaml_comments(text: str) -> str:
    """去掉整行注释（注释里会引用旧文案作为事故说明，不能算「还在用」）。"""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def check_collector_alert_reason() -> dict:
    text = _strip_yaml_comments(COLLECTOR_WF.read_text(encoding="utf-8"))
    detail, ok = [], True
    if "collector-status.json" not in text:
        ok = False
        detail.append("❌ 失败通知未读 collector-status.json（只能发「登录过期/改版/推送之一」）")
    else:
        detail.append("✅ 失败通知读取脚本落的状态文件，告警带真实原因")
    if "登录过期/页面改版/推送失败之一" in text:
        ok = False
        detail.append("❌ 仍在用旧的模糊文案「登录过期/页面改版/推送失败之一」")
    return {"item": "collector 失败告警可诊断", "ok": ok, "detail": detail}


def verify() -> dict:
    checks = [check_heartbeat_uses_slot(), check_delayed_slots(),
              check_alert_wording(), check_collector_alert_reason()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 调度检查口径与告警可信度验收", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}：{'✅' if c['ok'] else '❌'}")
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="调度口径与告警可信度验收")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()
    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"check_slot_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "check_slot_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
