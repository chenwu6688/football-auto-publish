#!/usr/bin/env python3
"""单测门禁验收：tests/ 必须真的被 CI 挡在合并前，且门禁不得被软化。

背景（2026-10-25）
------------------------------------------------------------------
这个仓库原有 CI 门禁只有 `scripts/verify_*.py`（契约/产物/调度验收），
**一个 pytest 步骤都没有**。于是 tests/ 下 22 项失败长期无人处理：

  - 它们全是 `90a9b29「彻底简化架构，只保留 Pipeline A」` 留下的僵尸测试，
    测的是已被删除的函数（analyze_content_performance / get_performance_boost /
    _run_hupu_pipeline）、已搬走的常量（BATCH_TYPES → 死常量）、
    已改写的 prompt 文案；
  - 因为没有门禁，没人被逼着修，也没人敢删（怕是真功能缺失）；
  - 每次改代码只能人工比对「失败数还是 22 吗、按文件分布还一样吗」——
    真回归混进这 22 项里根本看不出来。

清理后全量 1021 passed / 0 failed，并新增 `.github/workflows/unit-tests.yml`
把它变成硬门禁。本脚本守住这条门禁本身：

  A. 门禁存在（硬项）—— workflow 文件在，且真正执行 pytest；
  B. 触发覆盖（硬项）—— 至少 push 或 pull_request 之一，否则等于摆设；
  C. 不软化（硬项）—— 禁止 `|| true` / `continue-on-error: true` /
     `--deselect` / `--continue-on-collection-errors` 等把红变绿的手段；
  D. 有超时（硬项）—— 防挂死占满 CI；
  E. 已知排除项有交代（硬项）—— 排除 test_video_pipeline.py 必须在文件里
     写明原因（缺 edge_tts），不能无声跳过；
  F. 基线可复现（告警项）—— 打印本地复跑命令，方便对照。

产出 data/audit/test_gate_<date>.md + test_gate_latest.json；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_test_gate.py [--quiet]
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "unit-tests.yml"
CST = timezone(timedelta(hours=8))

# 把红变绿的手段：出现在 workflow 里就说明门禁被软化
SOFTENING_PATTERNS = (
    ("|| true", "用 `|| true` 吞掉失败"),
    ("continue-on-error: true", "用 continue-on-error: true 吞掉失败"),
    ("--deselect", "用 --deselect 屏蔽具体用例"),
    ("--continue-on-collection-errors", "收集失败也算过"),
    ("-p no:randomly", None),  # 占位，不判定
)
SOFTENING = [p for p in SOFTENING_PATTERNS if p[1]]


def _run_blocks(text: str) -> str:
    """把 workflow 里所有 run: 块拼起来（只看脚本，不看注释头）。

    注释里出现 `|| true` 是合法说明，不能误判；所以只取 run 块内容。
    """
    return "\n".join(re.findall(r"run:\s*\|-?\n((?:\s{6,}.*\n)+)", text))


def check_workflow_runs_pytest() -> dict:
    detail, ok = [], True
    if not WORKFLOW.exists():
        return {"item": "单测门禁存在", "ok": False,
                "detail": [f"❌ 缺少 {WORKFLOW.relative_to(PROJECT_ROOT)}"
                           "（tests/ 不会被任何 CI 步骤执行）"]}
    text = WORKFLOW.read_text(encoding="utf-8")
    script = _run_blocks(text)
    if "pytest" not in script:
        ok = False
        detail.append("❌ workflow 里没有执行 pytest")
    else:
        detail.append("✅ workflow 执行 pytest")
    if "tests/" not in script:
        ok = False
        detail.append("❌ pytest 命令未指向 tests/")
    else:
        detail.append("✅ pytest 目标为 tests/")
    return {"item": "单测门禁存在", "ok": ok, "detail": detail}


def check_triggers() -> dict:
    text = WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.exists() else ""
    detail, ok = [], True
    has_push = re.search(r"^\s{2}push:", text, re.M) is not None
    has_pr = "pull_request" in text
    has_manual = "workflow_dispatch" in text
    if not (has_push or has_pr):
        ok = False
        detail.append("❌ 既无 push 也无 pull_request 触发——门禁等于摆设")
    else:
        detail.append(f"✅ 触发覆盖: push={has_push} pull_request={has_pr}")
    if has_manual:
        detail.append("✅ 支持 workflow_dispatch 手动复跑")
    # push 带 paths 过滤是刻意设计（chore 提交不回写代码），但要确认过滤含 **.py
    if has_push and "**.py" not in text:
        ok = False
        detail.append("❌ push 触发带 paths 过滤却未包含 '**.py'，改代码不会触发")
    elif has_push:
        detail.append("✅ push 的 paths 过滤包含 '**.py'")
    return {"item": "触发覆盖", "ok": ok, "detail": detail}


def check_not_softened() -> dict:
    """门禁不得被软化：任何把失败变成功的写法都算硬项 FAIL。"""
    if not WORKFLOW.exists():
        return {"item": "门禁不被软化", "ok": False, "detail": ["❌ workflow 不存在"]}
    text = WORKFLOW.read_text(encoding="utf-8")
    script = _run_blocks(text)
    detail, ok = [], True
    for pat, why in SOFTENING:
        if pat in script or (pat in text and pat.startswith("continue-on-error")):
            ok = False
            detail.append(f"❌ 检测到 {why}")
    if ok:
        detail.append("✅ 未见 || true / continue-on-error / --deselect 等软化手段")
    return {"item": "门禁不被软化", "ok": ok, "detail": detail}


def check_timeout() -> dict:
    if not WORKFLOW.exists():
        return {"item": "门禁有超时", "ok": False, "detail": ["❌ workflow 不存在"]}
    text = WORKFLOW.read_text(encoding="utf-8")
    m = re.search(r"timeout-minutes:\s*(\d+)", text)
    if not m:
        return {"item": "门禁有超时", "ok": False,
                "detail": ["❌ 未设 timeout-minutes，测试挂死会占满 runner"]}
    return {"item": "门禁有超时", "ok": True,
            "detail": [f"✅ timeout-minutes={m.group(1)}"]}


def check_exclusion_documented() -> dict:
    """排除 video_pipeline 必须写明原因，不能无声跳过。"""
    if not WORKFLOW.exists():
        return {"item": "排除项有交代", "ok": False, "detail": ["❌ workflow 不存在"]}
    text = WORKFLOW.read_text(encoding="utf-8")
    detail, ok = [], True
    if "test_video_pipeline" not in text:
        detail.append("✅ 未排除任何测试文件（全量执行）")
        return {"item": "排除项有交代", "ok": True, "detail": detail}
    detail.append("ℹ️ 排除了 tests/test_video_pipeline.py")
    if "edge_tts" in text or "edge-tts" in text:
        detail.append("✅ 已写明排除原因（edge_tts/ffmpeg 依赖）")
    else:
        ok = False
        detail.append("❌ 排除了测试却没写原因——后人无从判断该不该恢复")
    return {"item": "排除项有交代", "ok": ok, "detail": detail}


def check_local_repro_hint() -> dict:
    """告警项：给出本地复跑命令，方便把 CI 红项一次复现出来。"""
    return {"item": "本地复跑命令", "ok": True, "advisory": True,
            "detail": ["ℹ️ PYTHONPATH=. python -m pytest tests/ -q "
                       "--ignore=tests/test_video_pipeline.py"]}


def verify() -> dict:
    checks = [check_workflow_runs_pytest(), check_triggers(), check_not_softened(),
              check_timeout(), check_exclusion_documented(), check_local_repro_hint()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 单测门禁验收（tests/ 必须挡在合并前）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "ℹ️" if c.get("advisory") else ("✅" if c["ok"] else "❌")
        L.append(f"## {c['item']}：{tag}")
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="单测门禁验收")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()
    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"test_gate_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "test_gate_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
