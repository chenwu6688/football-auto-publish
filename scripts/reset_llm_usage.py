#!/usr/bin/env python3
"""重置 LLM 用量统计里的 `disabled` 标记（欠费恢复后必跑）。

背景
----
`utils.call_llm_json` 在遇到 HTTP 401/402/403 时会把该模型写入
`data/llm_usage.json` 并打上 `"disabled": true`，之后**永久跳过**该模型。
2026-09-26 TokenHub 账户欠费（402）时，一次性把全部候选模型都标成了 disabled，
导致——**即使后来充值，后续批次仍会因"所有模型被禁用"而直接失败**。

本脚本把 `disabled` 标记清掉（保留 token 用量数字），让模型重新可用。

用法
----
    python3 scripts/reset_llm_usage.py            # 清 disabled，保留用量
    python3 scripts/reset_llm_usage.py --dry-run  # 只看会改什么，不落盘
    python3 scripts/reset_llm_usage.py --purge    # 连用量一起清空（回到全新状态）
    python3 scripts/reset_llm_usage.py --list      # 只列出当前被禁用的模型

跑完后记得提交并推送，否则 CI 每次 checkout 到的还是旧的禁用状态：

    git add -f data/llm_usage.json
    git commit -m "chore: 充值后清除 LLM disabled 标记"
    git push origin main
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USAGE_FILE = PROJECT_ROOT / "data" / "llm_usage.json"


def _load(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️ 无法解析 {path}: {e}（按空处理）")
        return {}


def main():
    ap = argparse.ArgumentParser(description="清除 LLM 用量文件里的 disabled 标记")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    ap.add_argument("--purge", action="store_true", help="连用量数字一起清空")
    ap.add_argument("--list", action="store_true", help="只列出被禁用的模型")
    ap.add_argument("--file", default=str(USAGE_FILE), help="用量文件路径")
    args = ap.parse_args()

    path = Path(args.file)
    usage = _load(path)

    if not usage:
        print(f"✅ {path} 为空或不存在，无需处理")
        return 0

    disabled_models = [m for m, v in usage.items() if isinstance(v, dict) and v.get("disabled")]

    if args.list:
        if disabled_models:
            print(f"🚫 当前被禁用的模型（{len(disabled_models)} 个）:")
            for m in disabled_models:
                print(f"   - {m}")
        else:
            print("✅ 没有被禁用的模型")
        return 0

    if args.purge:
        new_usage = {}
        print(f"🧹 清空全部用量记录（原 {len(usage)} 个模型）")
    else:
        new_usage = {}
        for model, v in usage.items():
            if isinstance(v, dict):
                v = dict(v)
                v.pop("disabled", None)
            new_usage[model] = v
        if disabled_models:
            print(f"🔓 解除禁用（{len(disabled_models)} 个）:")
            for m in disabled_models:
                print(f"   - {m}")
        else:
            print("ℹ️ 没有 disabled 标记（仍会规范化文件格式）")

    if args.dry_run:
        print("\n[dry-run] 未写入。将变为:")
        print(json.dumps(new_usage, ensure_ascii=False, indent=2))
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new_usage, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 已写入 {path}")
    print("   别忘了提交推送：git add -f data/llm_usage.json && git commit -m '...' && git push origin main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
