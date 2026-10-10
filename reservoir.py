"""三级·软性内容蓄水池（计划 12.1）。

计划 12.1 把供给拆成三层：一级·确定性 30% / 二级·突发 50% / 三级·软性 20%。
三级（人物故事、榜单、数据对比、历史回顾）无时效压力、可提前备货，
角色是「内容蓄水池」：突发日供给不足时由池中补位，突发密集时池中内容顺延。

本模块只管库存：备货入库（stock）、取用补位（fill_gap）、过期回收（expire）。
备货作业见 scripts/refill_reservoir.py（提前量产入库）。
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
RESERVOIR_DIR = PROJECT_ROOT / "data" / "reservoir"
ITEMS_PATH = RESERVOIR_DIR / "items.json"

# 三级·软性涵盖的板块/内容类型（计划 12.1）
SOFT_TYPES = ("人物故事", "榜单", "数据对比", "历史回顾")
SOFT_SECTIONS = ("人物故事", "战术榜单")

DEFAULT_TTL_DAYS = 14


def _now() -> datetime:
    return datetime.now()


def _load() -> dict:
    if ITEMS_PATH.exists():
        try:
            return json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": []}


def _save(data: dict) -> None:
    RESERVOIR_DIR.mkdir(parents=True, exist_ok=True)
    ITEMS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def stock(article: dict, *, section: str = "", date_str: str = "",
          ttl_days: int = DEFAULT_TTL_DAYS, now: datetime | None = None) -> str:
    """把一篇软性成稿备货入库，返回条目 id（status=stocked）。"""
    now = now or _now()
    data = _load()
    rid = f"rsv-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
    data["items"].append({
        "id": rid,
        "section": section or article.get("_column_name", "") or (SOFT_SECTIONS[0]),
        "content_type": article.get("content_type", "") or "人物故事",
        "title": article.get("title", ""),
        "stocked_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(days=ttl_days)).isoformat(timespec="seconds"),
        "status": "stocked",
        "used_at": "",
        "article": article,
    })
    _save(data)
    return rid


def expire(today: datetime | None = None) -> int:
    """把过期货标记 expired，返回过期条数。"""
    today = today or _now()
    data = _load()
    n = 0
    for it in data.get("items", []):
        if it.get("status") != "stocked":
            continue
        try:
            if datetime.fromisoformat(it.get("expires_at", "")) < today:
                it["status"] = "expired"
                n += 1
        except Exception:
            continue
    if n:
        _save(data)
    return n


def available(today: datetime | None = None) -> list[dict]:
    """返回当前可用（未用且未过期）的备货条目。"""
    today = today or _now()
    out = []
    for it in _load().get("items", []):
        if it.get("status") != "stocked":
            continue
        try:
            if datetime.fromisoformat(it.get("expires_at", "")) < today:
                continue
        except Exception:
            pass
        out.append(it)
    return out


def fill_gap(n: int, today: datetime | None = None) -> list[dict]:
    """供给不足时从池中取 n 条补位（先进先出），标记 used，返回条目列表。"""
    if n <= 0:
        return []
    today = today or _now()
    data = _load()
    picked = []
    for it in data.get("items", []):
        if len(picked) >= n:
            break
        if it.get("status") != "stocked":
            continue
        try:
            if datetime.fromisoformat(it.get("expires_at", "")) < today:
                continue
        except Exception:
            pass
        it["status"] = "used"
        it["used_at"] = today.isoformat(timespec="seconds")
        picked.append(it)
    if picked:
        _save(data)
    return picked


def stats(today: datetime | None = None) -> dict:
    today = today or _now()
    items = _load().get("items", [])
    by = {}
    for it in items:
        by[it.get("status", "?")] = by.get(it.get("status", "?"), 0) + 1
    return {"total": len(items), "by_status": by, "available": len(available(today))}


# ─── CLI ─────────────────────────────────────────────────

def _cli() -> None:
    ap = argparse.ArgumentParser(description="三级·软性内容蓄水池（计划 12.1）")
    ap.add_argument("--list", action="store_true", help="列出可用备货")
    ap.add_argument("--stats", action="store_true", help="库存统计")
    ap.add_argument("--expire", action="store_true", help="回收过期备货")
    ap.add_argument("--fill", type=int, metavar="N", help="取用 N 条（补位）")
    args = ap.parse_args()

    if args.stats:
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
        return
    if args.expire:
        print(f"已回收过期备货 {expire()} 条")
        return
    if args.fill:
        picked = fill_gap(args.fill)
        for it in picked:
            print(f"[used] {it['id']}  {it['section']}  {it['title'][:40]}")
        print(f"共取用 {len(picked)} 条")
        return
    if args.list:
        items = available()
        if not items:
            print("（蓄水池为空，请先运行 scripts/refill_reservoir.py 备货）")
        for it in items:
            print(f"[stocked] {it['id']}  {it['section']}  {it['title'][:40]}  到期 {it['expires_at'][:10]}")
        return
    ap.print_help()


if __name__ == "__main__":
    _cli()
