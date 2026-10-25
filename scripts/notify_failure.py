#!/usr/bin/env python3
"""失败告警（WxPusher）：正文必须带「为什么失败」。

2026-10-25 事故：一天三次「批次发布失败」告警都没有原因，人只能回电脑翻日志。
本脚本把失败原因（``scripts/write_failure_reason.py`` 落的结构化原因）拼进正文，
手机上就能直接判断是熔断、登录过期还是发布失败，不必等打开电脑。

命令行（CI 用环境变量）：
  WXPUSHER_APPTOKEN=x WXPUSHER_UID=y WB_BATCH=noon WB_TODAY=2026-10-25 \
  WB_REPO=o/r WB_RUN_ID=123 python3 scripts/notify_failure.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import write_failure_reason as wfr  # noqa: E402

WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"


def build_content(batch: str, day: str, reason: str, repo: str, run_id: str) -> str:
    """告警正文：批次 / 日期 / 原因 / 链接，原因永不为空。"""
    reason = (reason or "").strip() or "详见运行日志（未捕获到结构化原因）"
    return (f"⚠️ {batch} 批次发布失败\n"
            f"日期: {day}\n"
            f"原因: {reason}\n"
            f"查看: https://github.com/{repo}/actions/runs/{run_id}")


def main() -> int:
    env = os.environ
    content = build_content(env.get("WB_BATCH", ""), env.get("WB_TODAY", ""),
                            wfr.read(), env.get("WB_REPO", ""), env.get("WB_RUN_ID", ""))
    token, uid = env.get("WXPUSHER_APPTOKEN", ""), env.get("WXPUSHER_UID", "")
    if not token or not uid:
        print("未配置 WXPUSHER_APPTOKEN / WXPUSHER_UID，跳过推送：\n" + content)
        return 0
    try:
        import requests
        r = requests.post(WXPUSHER_API, json={"appToken": token, "content": content,
                                             "contentType": 1, "uids": [uid]}, timeout=20)
        print(f"告警已发送: HTTP {r.status_code} {r.text[:120]}")
    except Exception as e:  # 告警失败不得再抛，避免掩盖原始失败
        print(f"告警发送失败: {e}\n{content}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
