#!/usr/bin/env python3
"""本批可发布 0 篇时，把「为什么 0 篇」定成一句话（写原因文件 + 打 stdout）。

为什么需要它（2026-11-11）
--------------------------
11-10 evening 与 11-11 morning 连续两批发布 0 篇，告警正文只有
「失败步骤：发布到头条号」。真实原因是当时的风险闸门把 2 篇全部转人工确认
（metadata.compliance_dropped 是空的），而 orchestrator 的降级文案一律写
「被合规审核全部拦下」——文案把排查方向直接指错，照着它去改合规判据永远改不到点。

2026-11-11 风险闸门已整体移除（选题都经过主流媒体核实，我们只做转写，不臆造），
保存层只剩一条丢稿通道：出口合规审核。所以现役成因只剩两种：

  上游空转（真没选题/没成稿）   → 兜底 cron 重跑有意义
  合规规则剔除全部成稿           → 重跑没用，要么人改规则，要么走补发

旧 metadata 里的 ``pending_review`` / ``风险闸门`` 仍能读懂（回放 11-10、11-11
那两批时要能解释当时发生了什么），但它已不再由任何代码产生。

判据一律读 orchestrator 写进 metadata 的 ``zero_publish_reason``（生成侧唯一出口），
这里只做「读 + 兜底推断」，不重新判一遍——同一结论两处各判一次，早晚只改一处。

用法：
    python3 scripts/publish_zero_reason.py <metadata.json>
退出码：0 = 有明确原因（已写原因文件）；2 = 读不到产物（按「上游空转」处理）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def reason_of(meta: dict) -> tuple[str, bool]:
    """返回 (一句话原因, 是否来自明确记录)。"""
    if not isinstance(meta, dict):
        return "本批 0 篇成稿（metadata 不可读，按上游空转处理）", False
    explicit = str(meta.get("zero_publish_reason") or "").strip()
    if explicit:
        return explicit, True

    # 兜底推断：只在有证据时才归因，绝不给「没记录」编一个成因
    arts = meta.get("articles") or []
    pending = meta.get("pending_review") or []
    dropped = meta.get("compliance_dropped") or []
    if not arts and dropped:
        return f"合规审核：{len(dropped)} 篇全部被剔除，发布集合 0 篇", True
    if not arts and pending:
        # 历史字段：风险闸门已于 2026-11-11 移除，这里只为让旧批次可回放
        return (f"风险闸门（已移除的历史闸门）：{len(pending)} 篇转人工确认，"
                f"发布集合 0 篇"), True
    return "本批 0 篇成稿（上游空转：没有选题或没产出成稿）", False


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else None
    meta = {}
    if path and path.exists():
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    text, explicit = reason_of(meta)
    # 直接写原因文件（不走 write_failure_reason 的 compliance_reason 前缀逻辑：
    # 那句前缀会把「风险闸门」包装成「合规熔断」，等于二次贴错标签）
    try:
        import write_failure_reason as wfr
        wfr.write(text)
    except Exception as exc:                      # 告警拼装失败绝不阻断
        print(f"⚠️ 写失败原因失败（忽略）：{type(exc).__name__}: {exc}", file=sys.stderr)

    print(text)
    return 0 if explicit else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
