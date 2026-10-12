"""一级·确定性供给（计划 12.1）。

计划 12.1 把选题供给拆成三层：一级·确定性 30% / 二级·突发 50% / 三级·软性 20%。
一级·确定性来源四类：**赛程、转会窗截止日、赛季节点、开赛倒计时**；
特征「可提前 7 天预生成，无时效风险」；出稿要求「提前量产入库」。

分工：
  - 赛程类 → scripts/fixture_library.py 生成 data/fixtures/topic_pool.json（当日筛选消费）
  - 其余三类「节点事件」→ 本模块。节点日期来自 config/season_calendar.yaml（人工维护、
    官方公告为准），前向 7 天由 scripts/pregen_deterministic.py 量产成稿并入库存，
    发布日由 orchestrator 从库存取用（不再调用 LLM）。

本模块只做「节点选题 + 库存」两件事，不含 LLM（可离线单测）：
  build_node_topics / pending_nodes / stock / available / fill_gap / expire / stats
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

import awareness_gate

PROJECT_ROOT = Path(__file__).resolve().parent
CALENDAR_PATH = PROJECT_ROOT / "config" / "season_calendar.yaml"
STORE_DIR = PROJECT_ROOT / "data" / "deterministic"
STORE_PATH = STORE_DIR / "items.json"

CST = timezone(timedelta(hours=8))

DEFAULT_LEAD_DAYS = 7
DEFAULT_HORIZON_DAYS = 7

# 一级·确定性涵盖的板块（计划 6.2 固定四板块之一）
NODE_COLUMNS = ("转会动态", "战术榜单", "人物故事", "中国足球")


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(CST)


def _as_date(x) -> date | None:
    if x is None:
        return None
    if isinstance(x, datetime):
        return (x.astimezone(CST).date() if x.tzinfo else x.date())
    if isinstance(x, date):
        return x
    try:
        return datetime.strptime(str(x), "%Y-%m-%d").date()
    except Exception:
        return None


def load_calendar(path: Path | None = None) -> dict:
    """读取赛季节点日历；文件缺失或损坏返回 {"nodes": []}（不阻塞）。"""
    p = Path(path) if path else CALENDAR_PATH
    if not p.exists():
        return {"season": "", "nodes": []}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"season": "", "nodes": []}
    data.setdefault("nodes", [])
    return data


def _load_store() -> dict:
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": []}


def _save_store(data: dict) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_node_topics(today: date | None = None, horizon_days: int | None = DEFAULT_HORIZON_DAYS,
                      calendar: dict | None = None, dropped: list | None = None) -> list[dict]:
    """生成「节点事件」选题（一级·确定性）。

    窗口语义（计划 12.1「可提前 7 天预生成」）：
      - 预生成窗口：node_date ∈ [today, today + horizon_days]，即提前 horizon_days 天量产。
      - 可发布窗口：publish_from（node_date - lead_days）~ publish_by（node_date 当日）。
    已过节点（node_date < today）不生成；horizon_days=None 表示不限（仅用于排查）。

    认知度硬门槛（计划 13.1）：节点属于数据/榜单类供给通道，故
      - ``awareness`` 必须是 A/B/C —— 缺失或非法视为**未登记**，直接剔除，绝不默认放行；
      - C 级节点只允许落在数据类/榜单类板块，否则剔除；
      - 决策结果写回 ``awareness_decision``，供下游降权排序。
    被剔除的节点在传入 ``dropped`` 列表时一并记录（(node, reason)）。
    """
    today = _as_date(today) or _now().date()
    cal = calendar if calendar is not None else load_calendar()
    end = today + timedelta(days=horizon_days) if horizon_days is not None else None
    out: list[dict] = []
    for n in cal.get("nodes", []) or []:
        if not isinstance(n, dict):
            continue
        nd = _as_date(n.get("date"))
        if not nd:
            continue
        if nd < today:
            continue
        if end is not None and nd > end:
            continue
        lead = int(n.get("lead_days", DEFAULT_LEAD_DAYS) or DEFAULT_LEAD_DAYS)
        pub_from = nd - timedelta(days=lead)
        days_to = (nd - today).days
        label = n.get("label") or n.get("type") or "赛季节点"
        tmpl = n.get("template") or "{label}还有{days}天"
        conflict = tmpl.format(days=max(days_to, 0), label=label)
        column = n.get("column") or "战术榜单"
        board = column if column in NODE_COLUMNS else "战术榜单"

        aw = awareness_gate.classify(n.get("awareness"))
        dec = awareness_gate.decide_level(aw, board, channel="data_list",
                                          name=str(label))
        if not dec["ok"]:
            if dropped is not None:
                dropped.append((n, f"认知度门槛：{dec['reason']}"))
            continue

        out.append({
            "topic_id": f"node-{n.get('id')}-{nd.isoformat()}",
            "source_layer": "确定性",
            "node_id": n.get("id"),
            "node_type": n.get("type", "赛季节点"),
            "node_label": label,
            "node_date": nd.isoformat(),
            "days_to": days_to,
            "angle_type": n.get("type", "赛季节点"),
            "conflict": conflict,
            "suggested_column": column,
            "板块": board,
            "awareness": aw,
            "awareness_decision": dec["action"],
            "title_allowed": dec["title_allowed"],
            "publish_from": pub_from.isoformat(),
            "publish_by": nd.isoformat(),
            "fact_source": {
                "api": "config/season_calendar.yaml",
                "node_id": n.get("id"),
                "note": "赛季节点为公开日历事实，人工维护、官方公告为准",
            },
        })
    out.sort(key=lambda t: (t["node_date"], -{"A": 3, "B": 2, "C": 1}.get(t["awareness"], 1)))
    return out


def _stocked_keys() -> set[str]:
    return {it.get("topic_id", "") for it in _load_store().get("items", [])}


def pending_nodes(today: date | None = None, horizon_days: int = DEFAULT_HORIZON_DAYS,
                  calendar: dict | None = None) -> list[dict]:
    """窗口内、尚未入库的节点选题（供 pregen 量产）。"""
    done = _stocked_keys()
    return [t for t in build_node_topics(today, horizon_days, calendar)
            if t["topic_id"] not in done]


def stock(article: dict, topic: dict, *, now: datetime | None = None) -> str:
    """把一篇「提前量产」的节点成稿入库，返回条目 id。"""
    now = _now(now)
    data = _load_store()
    rid = f"dtn-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
    data["items"].append({
        "id": rid,
        "topic_id": topic.get("topic_id", ""),
        "node_id": topic.get("node_id", ""),
        "node_type": topic.get("node_type", ""),
        "node_date": topic.get("node_date", ""),
        "publish_from": topic.get("publish_from", ""),
        "publish_by": topic.get("publish_by", ""),
        "section": topic.get("板块") or topic.get("suggested_column") or "",
        # 计划 13.1：把认知度门槛决策随库存留痕，便于验收审计（未登记一律不入库）
        "awareness": topic.get("awareness", ""),
        "awareness_decision": topic.get("awareness_decision", ""),
        "title_allowed": topic.get("title_allowed"),
        "content_type": article.get("content_type", "") or topic.get("node_type", ""),
        "title": article.get("title", ""),
        "content": article.get("content", ""),
        "summary": article.get("summary", ""),
        "keywords": article.get("keywords", []),
        "keywords_cn": article.get("keywords_cn", []),
        "_title_score": article.get("_title_score"),
        "status": "stocked",
        "stocked_at": now.isoformat(timespec="seconds"),
    })
    _save_store(data)
    return rid


def expire(now: datetime | None = None) -> int:
    """回收已过 publish_by 的库存条目（节点日已过，时效失效）。"""
    today = _now(now).date()
    data = _load_store()
    n = 0
    for it in data.get("items", []):
        if it.get("status") != "stocked":
            continue
        pb = _as_date(it.get("publish_by"))
        if pb and pb < today:
            it["status"] = "expired"
            n += 1
    if n:
        _save_store(data)
    return n


def available(date_str: str | None = None, now: datetime | None = None) -> list[dict]:
    """当日可发布（未用、未过期、窗口命中）的节点成稿。"""
    today = _as_date(date_str) or _now(now).date()
    out = []
    for it in _load_store().get("items", []):
        if it.get("status") != "stocked":
            continue
        pf, pb = _as_date(it.get("publish_from")), _as_date(it.get("publish_by"))
        if pf and today < pf:
            continue
        if pb and today > pb:
            continue
        out.append(it)
    out.sort(key=lambda it: (it.get("publish_by", ""), -(it.get("_title_score") or 0)))
    return out


def fill_gap(n: int, date_str: str | None = None, now: datetime | None = None) -> list[dict]:
    """供给不足时从库存取 n 条补位（先进先出），标记 used，返回条目列表。"""
    if n <= 0:
        return []
    today = _as_date(date_str) or _now(now).date()
    data = _load_store()
    picked = []
    for it in data.get("items", []):
        if len(picked) >= n:
            break
        if it.get("status") != "stocked":
            continue
        pf, pb = _as_date(it.get("publish_from")), _as_date(it.get("publish_by"))
        if (pf and today < pf) or (pb and today > pb):
            continue
        it["status"] = "used"
        it["used_at"] = _now(now).isoformat(timespec="seconds")
        picked.append(it)
    if picked:
        _save_store(data)
    return picked


def stats(date_str: str | None = None, now: datetime | None = None,
          horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict:
    items = _load_store().get("items", [])
    by: dict[str, int] = {}
    for it in items:
        by[it.get("status", "?")] = by.get(it.get("status", "?"), 0) + 1
    return {"total": len(items), "by_status": by,
            "available": len(available(date_str, now)),
            "pending_nodes": len(pending_nodes(date_str, horizon_days))}


# ─── CLI ─────────────────────────────────────────────────

def _cli() -> None:
    ap = argparse.ArgumentParser(description="一级·确定性节点供给（计划 12.1）")
    ap.add_argument("--stats", action="store_true", help="库存统计")
    ap.add_argument("--list", action="store_true", help="列出当日可用")
    ap.add_argument("--nodes", action="store_true", help="列出窗口内节点选题")
    ap.add_argument("--expire", action="store_true", help="回收过期")
    ap.add_argument("--fill", type=int, metavar="N", help="取用 N 条")
    ap.add_argument("--days", type=int, default=DEFAULT_HORIZON_DAYS, help="前向天数")
    args = ap.parse_args()

    if args.stats:
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
        return
    if args.nodes:
        for t in build_node_topics(horizon_days=args.days):
            print(f"[{t['days_to']:>2}天] {t['node_date']} {t['node_type']:6s} "
                  f"{t['板块']} | {t['conflict'][:50]}")
        return
    if args.expire:
        print(f"已回收过期节点成稿 {expire()} 条")
        return
    if args.fill:
        for it in fill_gap(args.fill):
            print(f"[used] {it['id']}  {it['section']}  {it['title'][:40]}")
        return
    if args.list:
        items = available()
        if not items:
            print("（无可用节点成稿，请先运行 scripts/pregen_deterministic.py）")
        for it in items:
            print(f"[stocked] {it['id']}  {it['node_type']}  {it['title'][:40]}"
                  f"  截止 {it['publish_by']}")
        return
    ap.print_help()


if __name__ == "__main__":
    _cli()
