#!/usr/bin/env python3
"""单批篇数纪律（策略手册 第五章 / 完善计划 12.1）。

把「日更 6 至 8 篇」落成可执行、可验收的单批上限：

  常态   每批 2 篇（晨读 2 / 午间 2 / 晚间 2），日合计 6 篇；
  加发   仅「周四」或「S 级事件」允许单批提到手册上限
         （晨读 3 / 午间 3 / 晚间 2——手册对晚间固定 2 篇，故晚间不加发），
         加发须写明原因并留痕，周内不超过 2 次。

数值权威来源是 constants.BATCH_QUOTA，本模块只做判定与记账，不重复硬编码。
口径出处：策略手册第五章（晨读 2-3、午间 2-3、晚间 2）与完善计划 12.1（日更 6 至 8 篇）。

纪律（与全仓一致）：台账读写失败一律降级告警，绝不阻塞发布（计划九）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# 加发留痕台账（可被测试改写为临时路径）
LEDGER_PATH = PROJECT_ROOT / "data" / "uplift_ledger.json"

# 不依赖 constants 的兜底值：constants 不可用时仍能按纪律回落到常态 2 篇
_FALLBACK = {
    "normal_max": 2,
    "uplift": {"morning": 3, "noon": 3, "evening": 2},
    "daily_min": 6,
    "daily_max": 8,
    "uplift_reasons": ("thursday_boost", "s_level_event"),
    "uplift_weekly_limit": 2,
    "thursday_weekday": 3,
    "source": "策略手册 第五章 / 完善计划 12.1",
}

VALID_BATCHES = ("morning", "noon", "evening")


def policy() -> dict:
    """返回当前生效的篇数纪律配置（读 constants，失败回落内置副本）。"""
    try:
        from constants import BATCH_QUOTA  # type: ignore
        if isinstance(BATCH_QUOTA, dict) and BATCH_QUOTA:
            merged = dict(_FALLBACK)
            merged.update(BATCH_QUOTA)
            return merged
    except Exception:
        pass
    return dict(_FALLBACK)


def normal_max() -> int:
    """常态单批上限（篇）。"""
    return int(policy()["normal_max"])


def uplift_max(batch: str) -> int:
    """该批次的加发上限（篇）；未登记的批次不加发，即等于常态上限。"""
    pol = policy()
    table = pol.get("uplift") or {}
    return int(table.get(batch, pol["normal_max"]))


def daily_bounds() -> tuple[int, int]:
    """日更篇数区间（计划 12.1：6 至 8 篇）。"""
    pol = policy()
    return int(pol["daily_min"]), int(pol["daily_max"])


def allowed_reasons() -> tuple[str, ...]:
    """允许的加发原因白名单。"""
    return tuple(policy().get("uplift_reasons") or ())


def weekly_limit() -> int:
    """周内加发次数上限。"""
    return int(policy()["uplift_weekly_limit"])


def is_uplift_reason(reason: str | None) -> bool:
    """判断原因是否在允许白名单内（不猜、不做近义词匹配）。"""
    if not reason:
        return False
    return str(reason) in allowed_reasons()


def auto_reason(weekday: int | None) -> str | None:
    """按星期自动判定加发原因；只有周四自动成立，其余返回 None。"""
    if weekday is None:
        return None
    if int(weekday) == int(policy()["thursday_weekday"]):
        return "thursday_boost"
    return None


def decide(batch: str, *, weekday: int | None = None, uplift_reason: str | None = None,
           weekly_uplifted: int = 0) -> dict:
    """决定本批篇数上限。

    参数：
      batch            批次名（morning / noon / evening）
      weekday          星期（0=周一，None 表示未知，不自动加发）
      uplift_reason    显式加发原因；None 时按星期自动判定（周四）
      weekly_uplifted  本周已用加发次数

    返回 dict：max_articles / uplifted / reason / normal_max / uplift_max /
              weekly_limit / weekly_used / detail（供打印与验收）
    """
    pol = policy()
    normal = int(pol["normal_max"])
    cap = uplift_max(batch)
    limit = int(pol["uplift_weekly_limit"])
    used = int(weekly_uplifted or 0)

    reason = uplift_reason or auto_reason(weekday)
    reason_ok = is_uplift_reason(reason)
    room = used < limit
    can_uplift = cap > normal and reason_ok and room

    if can_uplift:
        max_articles = cap
        detail = (f"加发 {cap} 篇（原因 {reason}，周内 {used + 1}/{limit}）")
    else:
        max_articles = normal
        if reason and not reason_ok:
            detail = f"常态 {normal} 篇（原因 {reason} 不在白名单，不加发）"
        elif reason_ok and not room:
            detail = f"常态 {normal} 篇（周内加发已用满 {used}/{limit}）"
        elif normal >= cap:
            detail = f"常态 {normal} 篇（该批次不加发）"
        else:
            detail = f"常态 {normal} 篇"

    return {
        "batch": batch,
        "max_articles": max_articles,
        "uplifted": bool(can_uplift),
        "reason": reason if can_uplift else None,
        "requested_reason": reason,
        "normal_max": normal,
        "uplift_max": cap,
        "weekly_limit": limit,
        "weekly_used": used + (1 if can_uplift else 0),
        "detail": detail,
        "source": pol.get("source", ""),
    }


def _load(ledger_path: Path | None = None) -> dict:
    p = Path(ledger_path or LEDGER_PATH)
    if not p.exists():
        return {"records": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"records": []}
    if not isinstance(data, dict):
        return {"records": []}
    data.setdefault("records", [])
    return data


def records(ledger_path: Path | None = None) -> list[dict]:
    """读取全部加发留痕记录。"""
    return [r for r in _load(ledger_path).get("records", []) if isinstance(r, dict)]


def _week_key(date_str: str) -> str:
    """把日期归一到 ISO 周键（年-周），用于周内计数。"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def weekly_uplifted(date_str: str, ledger_path: Path | None = None) -> int:
    """本周（ISO 周）已用加发次数。台账不可读时返回 0（按最宽松解读，不阻塞）。"""
    if not date_str:
        return 0
    try:
        key = _week_key(date_str)
    except Exception:
        return 0
    n = 0
    for r in records(ledger_path):
        d = r.get("date")
        if not d:
            continue
        try:
            if _week_key(str(d)) == key:
                n += 1
        except Exception:
            continue
    return n


def record(date_str: str, batch: str, reason: str, articles: int = 0,
           note: str = "", ledger_path: Path | None = None) -> dict:
    """写入一条加发留痕。失败不抛出（计划九：降级不阻塞）。"""
    entry = {
        "date": date_str,
        "batch": batch,
        "reason": reason,
        "articles": int(articles or 0),
        "note": note,
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        p = Path(ledger_path or LEDGER_PATH)
        data = _load(p)
        data.setdefault("records", []).append(entry)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # 降级：只告警
        print(f"   ⚠️ 加发留痕写入失败（不影响发布）: {e}")
    return entry


def summarize(ledger_path: Path | None = None, *, date_str: str | None = None) -> dict:
    """汇总加发台账（供验收脚本使用）。"""
    recs = records(ledger_path)
    limit = weekly_limit()
    by_reason: dict[str, int] = {}
    for r in recs:
        by_reason[str(r.get("reason") or "?")] = by_reason.get(str(r.get("reason") or "?"), 0) + 1
    this_week = weekly_uplifted(date_str, ledger_path) if date_str else 0
    return {
        "total": len(recs),
        "this_week": this_week,
        "weekly_limit": limit,
        "within_limit": this_week <= limit,
        "by_reason": by_reason,
        "incomplete": [r for r in recs if not (r.get("date") and r.get("batch") and r.get("reason"))],
    }


def selftest() -> list[tuple[str, bool]]:
    """冒烟自检：常态 / 周四加发 / 白名单外 / 额度用尽 / 晚间不加发。"""
    cases: list[tuple[str, bool]] = []

    d = decide("morning", weekday=0)
    cases.append(("常态为 2 篇", d["max_articles"] == normal_max() and not d["uplifted"]))

    d = decide("morning", weekday=3)
    cases.append(("周四自动加发到 3 篇",
                  d["max_articles"] == uplift_max("morning") == 3 and d["uplifted"]
                  and d["reason"] == "thursday_boost"))

    d = decide("noon", weekday=1, uplift_reason="s_level_event")
    cases.append(("S 级事件可加发", d["max_articles"] == 3 and d["uplifted"]))

    d = decide("noon", weekday=1, uplift_reason="想多发点")
    cases.append(("白名单外原因不加发", d["max_articles"] == normal_max() and not d["uplifted"]))

    d = decide("morning", weekday=3, weekly_uplifted=weekly_limit())
    cases.append(("周内额度用尽不加发", d["max_articles"] == normal_max() and not d["uplifted"]))

    d = decide("evening", weekday=3)
    cases.append(("晚间不加发（手册固定 2 篇）",
                  d["max_articles"] == normal_max() and not d["uplifted"]))

    total = sum(decide(b, weekday=3)["max_articles"] for b in VALID_BATCHES)
    cases.append(("周四全加发日合计不超过计划上限 8",
                  total <= daily_bounds()[1] and total >= daily_bounds()[0]))

    cases.append(("白名单内判断不做近义词猜测",
                  is_uplift_reason("thursday_boost") and not is_uplift_reason("thursday")))
    return cases


def main() -> int:
    """命令行入口：打印当前纪律与决策示例，供人工核对。"""
    pol = policy()
    lo, hi = daily_bounds()
    print("单批篇数纪律")
    print(f"  常态单批上限：{normal_max()} 篇")
    print(f"  加发上限：{pol.get('uplift')}（晚间不加发）")
    print(f"  日更区间：{lo}-{hi} 篇（计划 12.1）")
    print(f"  加发原因白名单：{list(allowed_reasons())}，周内上限 {weekly_limit()} 次")
    print(f"  口径出处：{pol.get('source', '')}")
    print("\n决策示例（今日为周四、周内尚未加发）：")
    for b in VALID_BATCHES:
        d = decide(b, weekday=3)
        print(f"  {b:8s} → 上限 {d['max_articles']} 篇　{d['detail']}")
    print("\n冒烟自检：")
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n台账：{summarize()['total']} 条加发记录")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
