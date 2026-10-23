#!/usr/bin/env python3
"""把「这次 heartbeat 是哪个调度触发的」解析成「该查哪一天、按午检还是终检」。

背景（2026-10-23 事故，用户收到的假警）：
  heartbeat 有两条 cron（UTC）：
    37 4 * * *   → 12:37 CST 午检，只要求 morning 完成
    17 14 * * *  → 22:17 CST 终检，要求 morning/noon/evening 全完成
  GitHub 的定时任务每天稳定延迟 4~5 小时（10-20~10-23 实测：14:05 的 collector
  都跑到 18:25~19:30，22:17 的终检都跑到 02:3x）。旧实现拿「运行时刻」当判据：

    TODAY  = TZ=Asia/Shanghai date +%F      # 运行日 ← 错
    PERIOD = 当前小时 < 15 ? midday : final # 运行小时 ← 错

  于是产生两类纯假警：
    1. 10-22 22:17 的终检延迟到 10-23 02:33 才跑 → TODAY 变成 10-23，
       而 10-23 晨读批次 07:00 才开始 → 「2026-10-23 应完成批次（morning）
       全部缺失」。这就是用户 10-23 02:33 收到的那条。
    2. 12:37 的午检延迟到 17:3x（10-21 17:33 / 10-22 17:34 实测都有）→
       小时 ≥15 被判成终检 → 还没开始的 evening 被误报缺失。

  该用的是**调度时刻**，不是运行时刻。GitHub 对 schedule 事件提供
  `github.event.schedule`（触发它的 cron 原文），配上当前时刻就能反推出
  最近一次「本应触发」的时刻。

口径：
  slot_utc = 当天该 cron 的 HH:MM（UTC）；若晚于当前时刻，则取前一天
  target   = (slot_utc + 8h) 的日期（CST）
  period   = slot 的 CST 小时 < 15 → midday，否则 final

手工 dispatch（没有 schedule）沿用同一套规则，但按「现在」定日期；
CST 06:00 之前一律视为前一天的终检——22:17 到次日 12:37 之间没有任何调度，
凌晨手动检查只可能想看前一天的终检结果。

用法：
  python3 scripts/resolve_check_slot.py --schedule '17 14 * * *'
  → "2026-10-22 final"
  python3 scripts/resolve_check_slot.py --schedule '37 4 * * *' --now 2026-10-22T09:33:00Z
  → "2026-10-22 midday"
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
# CST 15:00 之后触发的调度才算终检（12:37 午检 / 22:17 终检的分界）
MIDDAY_CUTOFF_HOUR = 15
# CST 凌晨（<6 点）不可能有调度：只可能是前一天 22:17 的终检被拖过来了
NIGHT_CUTOFF_HOUR = 6


def parse_cron(schedule: str) -> tuple[int, int] | None:
    """从 cron 串里取 (hour, minute)（UTC）；不是可解析的定时表达式返回 None。"""
    parts = (schedule or "").strip().split()
    if len(parts) < 2:
        return None
    try:
        minute, hour = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None          # 带 */n、范围等表达式的 cron 不猜，交给兜底分支
    if not (0 <= minute < 60 and 0 <= hour < 24):
        return None
    return hour, minute


def resolve_slot(schedule: str, now: datetime | None = None) -> tuple[str, str]:
    """返回 (目标日期 YYYY-MM-DD, 时段 midday|final)。纯函数，不做任何 IO。"""
    now_utc = (now or datetime.now(timezone.utc))
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)

    hm = parse_cron(schedule)
    if hm is None:
        local = now_utc.astimezone(CST)
        if local.hour < NIGHT_CUTOFF_HOUR:
            return (local - timedelta(days=1)).strftime("%Y-%m-%d"), "final"
        period = "midday" if local.hour < MIDDAY_CUTOFF_HOUR else "final"
        return local.strftime("%Y-%m-%d"), period

    hour, minute = hm
    slot = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > now_utc:
        # 今天的调度点还没到 → 这次跑的一定是昨天那一班（被 GitHub 拖过来的）
        slot -= timedelta(days=1)
    local = slot.astimezone(CST)
    period = "midday" if local.hour < MIDDAY_CUTOFF_HOUR else "final"
    return local.strftime("%Y-%m-%d"), period


def main() -> int:
    ap = argparse.ArgumentParser(description="解析 heartbeat 调度对应的目标日期与时段")
    ap.add_argument("--schedule", default="",
                    help="github.event.schedule（cron 原文）；手工 dispatch 传空")
    ap.add_argument("--now", default="",
                    help="当前 UTC 时刻（ISO8601，测试/补跑用；默认取系统时间）")
    args = ap.parse_args()
    now = None
    if args.now.strip():
        now = datetime.fromisoformat(args.now.strip().replace("Z", "+00:00"))
    target, period = resolve_slot(args.schedule, now)
    print(f"{target} {period}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
