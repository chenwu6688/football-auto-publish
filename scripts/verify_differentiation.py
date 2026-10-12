#!/usr/bin/env python3
"""六个差异化方向验收（计划 13.5）。

校验项：
  A. 方向配置结构（硬项）—— 六方向齐备、字段完整；
  B. 代码 hook 落地（硬项）—— 每方向在指定文件中至少一处出现对应 marker。

产出 data/audit/differentiation_*.md + differentiation_latest.json；硬项 FAIL 退出码 1。

用法：
  python3 scripts/verify_differentiation.py
  python3 scripts/verify_differentiation.py --quiet
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import differentiation as df  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))


def check_structure() -> dict:
    bad = df.selftest()
    return {"item": f"六方向配置结构（{len(df.EXPECTED)} 方向）", "ok": not bad, "bad": bad,
            "detail": ([f"❌ 结构问题：{bad}"] if bad
                       else ["速度／地域深耕／怀旧与情感／数据再加工／评论区运营／观点增量 "
                             "齐备且字段完整 ✅"])}


def check_coverage() -> dict:
    cov = df.coverage()
    miss = [c["key"] for c in cov if not c["ok"]]
    return {"item": "六方向代码 hook 落地", "ok": not miss, "coverage": cov, "missing": miss,
            "detail": ([f"❌ 未落地方向：{miss}"] if miss
                       else [f"{c['key']} → {c['hit'][0]}" for c in cov])}


def verify() -> dict:
    checks = [check_structure(), check_coverage()]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in checks), "checks": checks,
            "hard_failed": [c["item"] for c in checks if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 六个差异化方向验收报告（计划 13.5）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}（硬项）：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="六个差异化方向验收（计划 13.5）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"differentiation_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "differentiation_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
