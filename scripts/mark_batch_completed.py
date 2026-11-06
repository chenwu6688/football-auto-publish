#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把某个批次标记为「已完成」——**只允许在发布步骤成功之后调用**。

## 为什么必须单独一个脚本

`batches_completed` 是幂等保护的唯一依据（batch.yml「确定批次和日期」步骤读它）。
2026-11-06 之前，这个字段由生成侧 `orchestrator.save_batch_state` 在**保存成稿时**
就写入，于是：

    noon 批次 → 生成 2 篇 → 配图相关性硬项失败 → 发布步骤整段 skipped
              → 但 metadata 早在「生成完成元数据回写」时就把 noon 提交进仓库
              → 幂等保护认为「noon 已完成」→ 兜底 cron 不重跑、人工补发被跳过
              → 当批两篇稿永久丢失

所以现在拆成两个字段：生成侧只写 `batches_generated`，`batches_completed` 由本脚本
在发布成功之后显式写入。语义是「稿子真的发出去了」，而不是「稿子生成了」。

## 用法

    python3 scripts/mark_batch_completed.py --date 2026-11-06 --batch noon
    python3 scripts/mark_batch_completed.py --date 2026-11-06 --batch noon --unmark
    python3 scripts/mark_batch_completed.py --date 2026-11-06 --batch noon --path /tmp/meta.json

`--unmark` 是运维补救口：发布实际失败的批次被误标完成时，先解除标记才能补发
（2026-11-05 晚间就是这么手工改 json 救回来的，这次固化成脚本）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
CST = timezone(timedelta(hours=8))


def mark(meta_path: Path, batch: str, unmark: bool = False) -> int:
    if not meta_path.exists():
        print(f"⚠️ {meta_path} 不存在，跳过（不创建空台账）")
        return 0

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"❌ {meta_path} 解析失败：{exc}")
        return 1
    if not isinstance(meta, dict):
        print(f"❌ {meta_path} 不是对象，拒绝改写")
        return 1

    done = [str(b) for b in (meta.get("batches_completed") or [])]
    before = list(done)

    if unmark:
        done = [b for b in done if b != batch]
        changed = done != before
        if not changed:
            print(f"ℹ️ completed 里本来就没有 {batch}")
            return 0
    else:
        if batch in done:
            print(f"ℹ️ {batch} 已在 completed 里，无需重复标记")
            return 0
        done.append(batch)
        changed = True

    meta["batches_completed"] = done
    meta["last_updated"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if changed:
        verb = "解除" if unmark else "标记"
        print(f"✅ 已{verb} {batch} 完成：completed={', '.join(done) or '（无）'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="标记/解除批次完成状态")
    ap.add_argument("--date", required=True, help="日期 YYYY-MM-DD")
    ap.add_argument("--batch", required=True, help="批次名 morning/noon/evening")
    ap.add_argument("--unmark", action="store_true", help="从 completed 里移除（补发前解除）")
    ap.add_argument("--path", default="", help="直接指定 metadata 路径（测试/运维用）")
    a = ap.parse_args()

    try:
        datetime.strptime(a.date, "%Y-%m-%d")
    except ValueError:
        print(f"❌ 日期格式非法: {a.date!r}（应为 YYYY-MM-DD）")
        return 1

    meta_path = Path(a.path) if a.path else OUTPUT_DIR / a.date / "metadata.json"
    return mark(meta_path, a.batch.strip(), unmark=a.unmark)


if __name__ == "__main__":
    sys.exit(main())
