"""三级·软性内容蓄水池（计划 12.1）。

计划 12.1 把供给拆成三层：一级·确定性 30% / 二级·突发 50% / 三级·软性 20%。
三级（人物故事、榜单、数据对比）无时效压力、可提前备货，
角色是「内容蓄水池」：突发日供给不足时由池中补位，突发密集时池中内容顺延。

本模块只管库存：备货入库（stock）、取用补位（fill_gap）、过期回收（expire）。
备货作业见 scripts/refill_reservoir.py（提前量产入库）。

「历史回顾」已于 2026-11-01 禁发（见 BANNED_TYPES），不再属于软性类型。
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
SOFT_TYPES = ("人物故事", "榜单", "数据对比")
SOFT_SECTIONS = ("人物故事", "战术榜单")

# 禁发的内容类型（2026-11-01 月度分析结论，不是拍脑袋）：
# 「历史回顾」9 篇实测总阅读 42、篇均 4.7、最高 15 —— 天花板已被自己证明，不是写得
# 差，是没人看。此前只降权仍在发，10 月还占 5%，纯烧额度。这里改禁发：
#   - SOFT_TYPES 移除，备货侧不再补
#   - available()/fill_gap() 过滤，**存量条目不参与补位**（否则禁了还会被旧库存顶出来）
# 要恢复必须同时改这里 + scripts/refill_reservoir.py 的轮换池，并给出新的绩效证据。
BANNED_TYPES = ("历史回顾",)


def is_banned(content_type: str) -> bool:
    """该内容类型是否禁发。生成侧与库存侧共用同一份判据，避免两处口径漂移。"""
    return (content_type or "").strip() in BANNED_TYPES


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
    """把一篇软性成稿备货入库，返回条目 id（status=stocked）。

    2026-11-12：**入库前自检诱导话术**。蓄水池是 30% 稿源，一旦入池的稿子带
    「诱导交互 / 违规涨粉」话术，出口合规会整篇剔除（11-11 晚间 2 篇全丢、
    发布 0 篇，根因就是池子里 22 条带话术的稿子）。判定只用
    ``interaction_bait.check_inducement`` 这一个口径；命中不入库、返回 ""，
    由调用方决定重试还是跳过——**不要在这里替调用方编造 id**。
    """
    try:
        import interaction_bait
        _res = interaction_bait.check_inducement(article.get("content", "") or "")
    except Exception as exc:                       # 判定不可用不阻断（与交互闸门同口径）
        _res = {"ok": True, "degraded": True,
                "detail": [f"诱导话术判定不可用（{type(exc).__name__}），放行"]}
    if not (_res.get("ok") or _res.get("degraded")):
        groups = "、".join(sorted({str(h.get("group", "")) for h in (_res.get("hits") or [])}))
        print(f"   ⛔ 拒收带诱导话术的备货（{groups}）：{(article.get('title') or '')[:24]}")
        return ""
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
    """返回当前可用（未用且未过期、未被禁发）的备货条目。"""
    today = today or _now()
    out = []
    for it in _load().get("items", []):
        if it.get("status") != "stocked":
            continue
        if is_banned(it.get("content_type")):      # 禁发类型即使有存量也不放出来
            continue
        try:
            if datetime.fromisoformat(it.get("expires_at", "")) < today:
                continue
        except Exception:
            pass
        out.append(it)
    return out


def fill_gap(n: int, today: datetime | None = None, accept=None) -> list[dict]:
    """供给不足时从池中取 n 条补位（先进先出），标记 used，返回条目列表。

    禁发类型（BANNED_TYPES）不参与补位，且**不标记 used** —— 留在池里由 expire
    自然回收，避免「悄悄消耗库存却从不产出」这种看不见的漏斗。

    accept: 可选的准入判定 `accept(item) -> (bool, reason)`，**在标记 used 之前**调用。
    2026-11-05 加这条是因为蓄水池条目是 10 月存进来的，标题不过后来才生效的
    「疑问收尾限额 / 三要素」闸门，转正后直接把违规标题发出去（当天两篇）。
    判定不过的条目记 `status="rejected"` + reason，既不会每批重复重试，也不占用配额。
    """
    if n <= 0:
        return []
    today = today or _now()
    data = _load()
    picked = []
    changed = False
    for it in data.get("items", []):
        if len(picked) >= n:
            break
        if it.get("status") != "stocked":
            continue
        if is_banned(it.get("content_type")):
            continue
        try:
            if datetime.fromisoformat(it.get("expires_at", "")) < today:
                continue
        except Exception:
            pass
        if accept is not None:
            try:
                ok, reason = accept(it)
            except Exception as e:           # 闸门自己炸了不能当通过
                ok, reason = False, f"准入判定异常: {type(e).__name__}"
            if not ok:
                it["status"] = "rejected"
                it["rejected_at"] = today.isoformat(timespec="seconds")
                it["reject_reason"] = str(reason)[:200]
                changed = True                # 拒绝也必须落盘，否则下次又被取到
                print(f"   ⛔ 蓄水池条目准入未过（{str(reason)[:40]}）："
                      f"{(it.get('title') or '')[:30]}")
                continue
        it["status"] = "used"
        it["used_at"] = today.isoformat(timespec="seconds")
        picked.append(it)
    if picked or changed:
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
