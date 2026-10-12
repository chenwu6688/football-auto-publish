#!/usr/bin/env python3
"""主调度（Cloudflare Worker）健康巡检 + 排障 runbook（计划 3.3 / 第十章）。

为什么需要它
------------
发布链是「主调度 Cloudflare Worker 准点触发 → batch.yml 兜底 cron 晚 30/50 分钟」。
**主调度失效是静默的**：兜底 cron 仍会把批次跑完，`heartbeat.yml` 看到三批次齐全就不告警，
于是「发布整体晚了 30-50 分钟」这件事没人发现——而实测晚间 18:00 档点击率 2.27% 是全时段峰值，
每拖一晚都是在拿峰值时段换低谷时段。

本脚本把「主调度是否还在准点干活」变成可判读的数字。

判读方法
--------
metadata 的 `generated_at` 是当日**最后一次生成**的时间戳（CI runner 为 UTC，需 +8 换算 CST）。
主调度健康时，它应稳定落在目标时刻附近（目标 + 数分钟生成耗时）；
若连续多日落进兜底窗口（目标 +30/+50 分钟）或彻底发散，即主调度疑似失效。

用法
----
    python3 scripts/check_scheduler_health.py            # 近 14 天
    python3 scripts/check_scheduler_health.py --days 30
    python3 scripts/check_scheduler_health.py --verbose  # 逐日明细

退出码：0 = 正常/无数据；1 = 发现死区内写入（真实违规，需立即处理）。

排障 runbook（主调度疑似失效时按序执行）
--------------------------------------
1. 看 Cloudflare Worker 是否还活着：
     cd external-trigger && npx wrangler tail          # 看有无 cron 触发日志
   无输出 / 报错 → Worker 未部署或密钥失效，重新 `npx wrangler deploy`。
2. 手动补一发验证链路（不依赖 Worker）：
   GitHub → Actions → 足球自媒体批次发布 → Run workflow → 选 batch/date。
   能跑通说明 worker 是唯一致命点，链路本身没坏。
3. 确认 GitHub 侧 cron 仍登记在案：
   Actions → 足球自媒体批次发布 → 左侧「足球自媒体批次发布」应有 6 条 schedule。
   GitHub 会对不活跃仓库停用 cron，需手动 re-enable。
4. 若当天批次已错过：用 workflow_dispatch 指定 date 补发（幂等保护会跳过已完成批次）。
5. 修好后重跑本脚本，确认写回时刻回到目标档。
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402

CST = datetime.now().astimezone().tzinfo
RATIO_CONFIG = PROJECT_ROOT / "config" / "ratio_config.yaml"
OUTPUT_DIR = PROJECT_ROOT / "output"

# 兜底 cron 相对主调度目标的偏移（分钟），与 .github/workflows/batch.yml 保持一致
FALLBACK_OFFSETS = (30, 50)
ON_TARGET_TOLERANCE = 15      # 目标 ±15 分钟视为准点
FALLBACK_TOLERANCE = 10       # 兜底窗口 ±10 分钟
DEAD_ZONE_HOUR = 6            # CST 00:00-05:59 为死区

# 晚间目标于 2026-10-12 由 17:30 调到 18:00（依 869 条真实数据，18:00 档点击率 2.27% 为峰值）。
# **判读历史日期必须套用当时生效的目标**，否则 9 月「17:40 = 17:30 + 10 分钟生成」会被误判成发散，
# 制造「主调度疑似失效」的假阳性。
EVENING_MOVED_ON = "2026-10-12"
EVENING_LEGACY_TARGET = "17:30"


def load_targets() -> list:
    """从 config/ratio_config.yaml 读时段目标（时刻唯一权威），返回 ["HH:MM", ...]。"""
    try:
        cfg = yaml.safe_load(RATIO_CONFIG.read_text(encoding="utf-8")) or {}
        win = (cfg.get("时段窗口") or {})
        targets = []
        for k in ("晨读", "午间", "晚间"):
            t = (win.get(k) or {}).get("目标")
            if t:
                targets.append(str(t))
        return targets or ["08:00", "12:00", "18:00"]
    except Exception as e:
        print(f"⚠️ 读取时段窗口失败（{type(e).__name__}: {e}），改用内置默认目标")
        return ["08:00", "12:00", "18:00"]


def _hhmm_to_min(s: str) -> int:
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def targets_for(date_str: str) -> list:
    """该日期**当时生效**的时段目标（晚间目标 2026-10-12 前后不同）。"""
    if date_str < EVENING_MOVED_ON:
        return [t for t in TARGETS if t != "18:00"] + [EVENING_LEGACY_TARGET]
    return list(TARGETS)


def classify(minute_of_day: int, targets: list) -> str:
    """把「当日最后一次生成的 CST 时刻」归类。"""
    if minute_of_day < DEAD_ZONE_HOUR * 60:
        return "dead_zone"
    for t in targets:
        base = _hhmm_to_min(t)
        if abs(minute_of_day - base) <= ON_TARGET_TOLERANCE:
            return "on_target"
    for t in targets:
        for off in FALLBACK_OFFSETS:
            if abs(minute_of_day - (_hhmm_to_min(t) + off)) <= FALLBACK_TOLERANCE:
                return "fallback"
    return "off_grid"


def collect(days: int) -> list:
    """近 N 天有 metadata 的日期 → [{date, cst, minute, cls, batches}]。"""
    floor = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for p in sorted(glob.glob(str(OUTPUT_DIR / "**" / "metadata.json"), recursive=True)):
        d = Path(p).parent.name
        if len(d) != 10 or d[4] != "-" or d < floor:
            continue
        try:
            meta = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        ts = meta.get("generated_at") or ""
        m = re.search(r"[ T](\d{2}):(\d{2})", str(ts))
        if not m:
            out.append({"date": d, "cst": None, "minute": None, "cls": "no_timestamp",
                        "batches": ",".join(meta.get("batches_completed") or [])})
            continue
        # CI runner 时区为 UTC → +8h 换算 CST（勿直接当 CST 用）
        minute = (int(m.group(1)) * 60 + int(m.group(2)) + 8 * 60) % (24 * 60)
        out.append({"date": d, "cst": f"{minute // 60:02d}:{minute % 60:02d}",
                    "minute": minute, "cls": classify(minute, targets_for(d)),
                    "batches": ",".join(meta.get("batches_completed") or [])})
    return out


def build_report(days: int, rows: list) -> dict:
    from collections import Counter
    cnt = Counter(r["cls"] for r in rows)
    total = len(rows)
    dead = [r for r in rows if r["cls"] == "dead_zone"]
    fallback = [r for r in rows if r["cls"] == "fallback"]
    off = [r for r in rows if r["cls"] == "off_grid"]

    # 判读：可用样本 ≥5 时，兜底+发散占比过半即主调度疑似失效
    suspect = total >= 5 and (len(fallback) + len(off)) * 2 > total

    detail = [f"近 {days} 天有 metadata 的 {total} 天",
              f"准点 {cnt.get('on_target', 0)}｜兜底窗口 {len(fallback)}｜"
              f"发散 {len(off)}｜死区 {len(dead)}｜无时间戳 {cnt.get('no_timestamp', 0)}"]
    if dead:
        detail.append(f"❌ 死区内写入 {len(dead)} 天：{', '.join(r['date'] for r in dead[:5])}")
    if suspect:
        detail.append("❌ 主调度疑似失效：写回时刻过半落在兜底窗口或发散，"
                      "请按脚本 docstring 的 runbook 排查 Cloudflare Worker")
    elif total:
        detail.append("✅ 主调度准点（写回时刻稳定落在目标档）")

    return {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "window_days": days, "days_seen": total, "by_class": dict(cnt),
            "main_scheduler_suspect": suspect, "dead_zone_days": [r["date"] for r in dead],
            "ok": not dead, "detail": detail, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="主调度健康巡检")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--verbose", action="store_true", help="逐日明细")
    args = ap.parse_args()

    rows = collect(args.days)
    rep = build_report(args.days, rows)
    print("=== 主调度健康巡检（计划 3.3）===")
    print(f"目标档：{' / '.join(TARGETS)}（容忍 ±{ON_TARGET_TOLERANCE} 分钟）；"
          f"兜底窗口 = 目标 +{'/+'.join(str(o) for o in FALLBACK_OFFSETS)} 分钟")
    print(f"注：{EVENING_MOVED_ON} 之前晚间目标为 {EVENING_LEGACY_TARGET}，"
          f"历史日期按当时生效的目标判读；本地手动跑 orchestrator/verify 会写当天 "
          f"metadata，当天一行不代表调度表现。")
    for d in rep["detail"]:
        print("  " + d)
    if args.verbose:
        print("\n日期         CST末次生成  归类        批次")
        for r in rows:
            print(f"{r['date']}   {str(r['cst'] or '-'):10s}  {r['cls']:11s}  {r['batches']}")
    return 0 if rep["ok"] else 1


TARGETS = load_targets()

if __name__ == "__main__":
    raise SystemExit(main())
