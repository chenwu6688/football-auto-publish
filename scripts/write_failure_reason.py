#!/usr/bin/env python3
"""把「本批为什么失败」写成结构化原因，供失败通知读取。

背景（2026-10-25）：一天连收三次 WxPusher 告警，正文只有「noon 批次发布失败 /
日期 / 有个链接」。手机上看不到日志，等于没告警——排查只能回到电脑翻 40 秒的
运行记录。根因是告警只发了固定文案，没带任何原因。

约定：失败侧把原因写到 ``$RUNNER_TEMP/wb_fail_reason.txt``，通知侧
（``scripts/notify_failure.py``）读它拼进正文。写失败绝不抛异常——告警本身
不能因为「拼原因」而发不出去。

命令行：
  python3 scripts/write_failure_reason.py "合规熔断（未解除）"
  python3 scripts/write_failure_reason.py            # 仍读 compliance_guard 状态
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REASON_FILE = "wb_fail_reason.txt"


def reason_path() -> Path:
    """失败原因的固定落盘位置（CI 用 RUNNER_TEMP，本地退回项目临时目录）。"""
    base = os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base) / REASON_FILE


def compliance_reason(prefix: str = "") -> str:
    """从合规状态里拼一句能定位的原因；状态不可读时退回裸前缀。"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        import compliance_guard as g
        st = g.status()
        since = (st.get("since") or "")[:16].replace("T", " ")
        detail = (st.get("reason") or "").strip()
        head = prefix or "合规熔断"
        if detail:
            return f"{head}（{since} 触发）：{detail[:200]}" if since else f"{head}：{detail[:200]}"
        return head
    except Exception:
        return prefix or "合规熔断（原因读取失败，详见运行日志）"


def write(reason: str, path: Path | None = None) -> str:
    """写原因文件，返回写入的文本。任何异常都吞掉（不阻塞告警）。"""
    reason = (reason or "").strip() or "未知原因（详见运行日志）"
    p = path or reason_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(reason, encoding="utf-8")
    except Exception:
        pass
    return reason


def read(path: Path | None = None) -> str:
    """读原因文件；不存在则给出明确占位（不冒充「没有原因」）。"""
    p = path or reason_path()
    try:
        return p.read_text(encoding="utf-8").strip() or "未知原因（原因文件为空）"
    except Exception:
        return "详见运行日志（未捕获到结构化原因）"


def main(argv: list[str]) -> int:
    prefix = " ".join(argv[1:]).strip()
    print(f"已记录失败原因：{write(compliance_reason(prefix))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
