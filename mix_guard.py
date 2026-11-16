#!/usr/bin/env python3
"""选题类型配比出口守卫（**软闸门**，2026-11-16 改动点 3）。

## 病根

`config/ratio_config.yaml` 写明「战报比分 10%（R11：CTR 0.49%、0 爆款）」，
`scripts/ratio_tuner.py` 也会按它出调参建议 —— 但**运行期没有任何东西读它**。
`topic_category.prioritize` 只做「单批战报软上限 1 篇」，跨批/跨天完全不管，
于是 14 天实测战报 30%（目标 10%）、未分类 45%（目标 15%）。

不是参数没调对，是**参数没有生效路径**。

## 三件必须守住的事

1. **目标值只从 `config/ratio_config.yaml` 读。** 代码里不写第二份
   （同一量只能有一处定义 —— 这是本项目反复吃亏的地方）。
2. **只留证，不阻断。** 超配时：换不了题就照发，写 `metadata.mix_overshoot`。
   配比是流量优化项，平台不因此扣分；拿它卡发布就会重现 11-11 晚间「一句话术
   把整批打成 0 篇」。11-11 的教训写在这里，防止下一个人又来加硬拦。
3. **样本不足时 `ok=None`（未判定），不是通过。** 单日 2 篇的分母会让一篇战报
   直接翻转 50pt，所以占比一律按**滚动窗口**算（默认 14 天，与
   `verify_topic_mix.py` / `ratio_tuner` 同一个窗口），不按单批算。

## 数据源为什么用台账而不是 metadata

`save_articles_local` 落 metadata 是**覆盖写**：同一批只留本批几篇，一天三个批次
（晨读/午间/晚间）落到最后只剩晚间那 2 篇。拿它当分母，14 天窗口只有 28 篇，
且缺上午两个批次 —— 那会得出「战报占比 50%」这种纯由覆盖写造成的假数。
所以这里读 `data/audit/publish_ledger_YYYY-MM-DD.json`（日级聚合，含当日所有批次），
再叠加**本批内存候选**（还没发布，台账里没有）。

用法：`python3 mix_guard.py`（打印当前窗口配比快照）
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import topic_category as tc

PROJECT_ROOT = Path(__file__).resolve().parent
RATIO_PATH = PROJECT_ROOT / "config" / "ratio_config.yaml"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

DEFAULT_WINDOW_DAYS = 14
#: 低于这个样本数不判超配。20 篇是「单篇最多 5pt」的量级，
#: 少于它「超配 3pt」这种结论完全可能是噪声（与 ratio_tuner 最小样本 30 同向，取更宽的值，
#: 因为这里只是留证、不驱动调参）。
MIN_SAMPLE = 20


def load_targets(path: Path | str | None = None) -> dict | None:
    """读 `选题类型配比`。读不到返回 None（调用方按未判定处理，不阻断发布）。"""
    p = Path(path or RATIO_PATH)
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("选题类型配比")
    if not isinstance(block, dict) or not block:
        return None
    out = {}
    for k, v in block.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out or None


def _window(date_str: str | None, days: int) -> tuple[str, str]:
    try:
        end = (datetime.strptime(date_str, "%Y-%m-%d").date()
               if date_str else datetime.now(CST).date())
    except Exception:
        end = datetime.now(CST).date()
    return (end - timedelta(days=days - 1)).strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _cat_of(art: dict) -> str:
    """优先用已落盘的 topic_category；缺失则现判（口径唯一走 topic_category.classify）。"""
    c = str((art or {}).get("topic_category") or "").strip()
    if c:
        return c
    return tc.classify({"title": (art or {}).get("title") or "",
                        "content_type": (art or {}).get("content_type") or ""})


def ledger_counts(date_str: str | None = None, days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """窗口内台账已记录篇数 → {类别: 篇数}（日级聚合文件，只认 `publish_ledger_<date>.json`）。"""
    lo, hi = _window(date_str, days)
    counts: dict = {}
    if not AUDIT_DIR.exists():
        return counts
    for p in sorted(AUDIT_DIR.glob("publish_ledger_*.json")):
        name = p.stem
        if name == "publish_ledger_latest":
            continue
        day = name.replace("publish_ledger_", "")
        if len(day) != 10 or not (lo <= day <= hi):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in (data.get("records") or []):
            title = str((r or {}).get("title") or "").strip()
            if not title:
                continue
            c = tc.classify({"title": title})
            counts[c] = counts.get(c, 0) + 1
    return counts


def snapshot(articles=None, date_str: str | None = None,
             window_days: int = DEFAULT_WINDOW_DAYS, targets: dict | None = None) -> dict:
    """窗口配比快照。``articles`` 是**本批尚未发布**的成稿（会叠加到台账之上）。

    返回 {"ok", "degraded", "window", "n", "table", "overshoot"}：
      ok        True=全部在目标内／False=有超配／None=未判定（配置不可用或样本不足）
      table     {类别: {"count", "pct", "target", "over"}}
      overshoot [{"type", "actual", "target", "delta"}] —— 留给 metadata 的留证形状
    """
    tgt = targets if targets is not None else load_targets()
    lo, hi = _window(date_str, window_days)
    counts = ledger_counts(date_str, window_days)

    extra = [a for a in (articles or []) if isinstance(a, dict) and not a.get("_is_prediction")]
    for a in extra:
        c = _cat_of(a)
        counts[c] = counts.get(c, 0) + 1
    n = sum(counts.values())

    rep = {"degraded": tgt is None, "window": [lo, hi], "window_days": window_days,
           "n": n, "batch_n": len(extra), "table": {}, "overshoot": [], "ok": None}
    if tgt is None:
        rep["reason"] = "ratio_config.yaml 的「选题类型配比」读不到 → 未判定"
        return rep
    if n < MIN_SAMPLE:
        rep["reason"] = f"窗口样本 {n} 篇 < {MIN_SAMPLE}，占比无意义 → 未判定"
        return rep

    for cat, target in tgt.items():
        cnt = counts.get(cat, 0)
        pct = 100.0 * cnt / n
        rep["table"][cat] = {"count": cnt, "pct": round(pct, 1),
                             "target": target, "over": pct > target}
    for cat, cnt in counts.items():
        if cat not in tgt and cnt:
            rep["table"].setdefault(cat, {"count": cnt, "pct": round(100.0 * cnt / n, 1),
                                          "target": None, "over": None})
    for cat, row in rep["table"].items():
        if row["over"]:
            rep["overshoot"].append({"type": cat, "actual": row["pct"],
                                     "target": row["target"],
                                     "delta": round(row["pct"] - row["target"], 1)})
    rep["overshoot"].sort(key=lambda x: -x["delta"])
    rep["ok"] = not rep["overshoot"]
    return rep


def check(articles=None, date_str: str | None = None,
          window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """:func:`snapshot` 的语义别名（调用方读起来更像「判定」）。"""
    return snapshot(articles, date_str, window_days)


def render(rep: dict) -> str:
    L = [f"窗口 {rep['window'][0]} ~ {rep['window'][1]}｜{rep['n']} 篇"
         f"（本批 {rep.get('batch_n', 0)} 篇）"]
    if rep["ok"] is None:
        L.append(f"未判定：{rep.get('reason', '')}")
        return "\n".join(L)
    for cat, row in sorted(rep["table"].items(), key=lambda kv: -kv[1]["pct"]):
        tgt = f"{row['target']:.0f}%" if row["target"] is not None else "—"
        mark = "超配" if row["over"] else ("目标内" if row["over"] is False else "无目标")
        L.append(f"  {cat:<6} {row['count']:>3} 篇 {row['pct']:>5.1f}%  目标 {tgt:>4}  {mark}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="选题类型配比出口守卫（软）")
    ap.add_argument("--date", default=None, help="窗口结束日（默认今天）")
    ap.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    args = ap.parse_args()
    rep = snapshot(date_str=args.date, window_days=args.days)
    print(render(rep))
    print(json.dumps({"ok": rep["ok"], "overshoot": rep["overshoot"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
