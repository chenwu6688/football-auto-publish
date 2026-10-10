#!/usr/bin/env python3
"""预热社媒 / 搜索热度信号（计划 11.2 L2）。

主流程只读 data/signals/external_signals.json，本脚本负责把该文件「预热」好：
  - 微博热搜榜 → 社媒讨论量
  - 百度搜索联想 → 搜索指数

只对真实命中的实体写入信号；未命中不写（绝不假装有信号）。

用法：
    python3 scripts/refresh_signals.py [--level A|AB|all] [--limit N] [--workers 8]
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import signal_sources  # noqa: E402

ENTITY_MAP = PROJECT_ROOT / "config" / "entity_map.json"


def _entity_names(level: str) -> list[str]:
    try:
        data = json.loads(ENTITY_MAP.read_text(encoding="utf-8"))
    except Exception:
        return []
    want = {"A", "B"} if level == "AB" else ({"A", "B", "C"} if level == "all" else {"A"})
    names = []
    for v in (data.get("entities") or {}).values():
        if v.get("name_zh") and v.get("awareness", "C") in want:
            names.append(v["name_zh"])
    return names


def main():
    ap = argparse.ArgumentParser(description="预热社媒/搜索热度信号")
    ap.add_argument("--level", default="A", choices=["A", "AB", "all"],
                    help="按认知度级别选取实体（默认 A）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少实体（0=不限）")
    ap.add_argument("--workers", type=int, default=8, help="并发抓取线程数")
    args = ap.parse_args()

    names = _entity_names(args.level)
    if args.limit:
        names = names[:args.limit]
    if not names:
        print("⚠️ 未取到实体（检查 config/entity_map.json）")
        return

    print(f"🌐 预热信号：{len(names)} 个实体（级别 {args.level}）")
    hot = signal_sources.fetch_hot()
    print(f"   热搜榜：{len(hot)} 条")

    per_term: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(signal_sources.fetch_baidu_suggest, n): n for n in names}
        for fu in as_completed(futs):
            n = futs[fu]
            try:
                per_term[n] = fu.result()
            except Exception:
                per_term[n] = []

    signals = signal_sources.build_signals(names, hot=hot, per_term=per_term)
    p = signal_sources.write_external(signals, entities=names)
    social = sum(1 for v in signals.values() if "社媒讨论量" in v)
    search = sum(1 for v in signals.values() if "搜索指数" in v)
    print(f"✅ 命中：社媒 {social} 个 / 搜索 {search} 个 → {p}")


if __name__ == "__main__":
    main()
