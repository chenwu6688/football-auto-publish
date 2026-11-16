#!/usr/bin/env python3
"""封面来源分层点击率（2026-11-16 改动点 5，纯观测）。

## 为什么

上一轮把「零配图」打到 0%（11-05 兜底生效后 48 篇全有封面），但**封面来源从没分层算过**：
`image_provenance.kind` 实测分布是 `card:title 18 / card:points 33 / card:numbers 26 /
news_body 60 / wikipedia+wikimedia 9 / pexels 1` —— 数据卡已经是主力封面来源。
「零配图 0%」有可能只是把「无封面」换成了「低质封面」，聚合点击率看不出来。

## 两条口径（都踩过坑）

1. **封面 = `images[0]`**（`publisher.extract_images` 保留顺序，第一张即封面）。
2. **按文件名 join，不按下标对**：`images` 是完整路径字符串、`image_provenance`
   是 basename，两边顺序不保证一致。按下标配对会把「新闻图」的数算到「数据卡」头上。

## 判定

`card:*` 组聚合 CTR 若低于 `news_body` 组 1.5 倍以上**且样本各 ≥15 篇** → 建议立项改
`cover_card` 版式；否则只观测，不立项（样本不足时只打印样本量，不给结论）。

另外单列一栏「封面尺寸是否 ≥800×600」：`published_without_cover=2`（11-02~11-04）
说明「有图」不等于「有合格封面」，尺寸这一层以前没人看。

用法：python3 scripts/cover_strata.py [--days 14] [--sample 10]
产出：data/audit/cover_strata_latest.json + cover_strata_<date>.md
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_DIR = PROJECT_ROOT / "output"
PERF_LOG = OUTPUT_DIR / "performance_log.json"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

#: 判「版式要不要改」的两道门：倍数差 + 最小样本（样本不足时任何差值都是噪声）
RATIO_GAP = 1.5
MIN_SAMPLE_PER_GROUP = 15

COVER_MIN_W, COVER_MIN_H = 800, 600


def _family(kind: str) -> str:
    k = str(kind or "").strip().lower()
    if k.startswith("card:"):
        return "信息卡(card:*)"
    if k.startswith("chart:"):
        return "数据图(chart:*)"
    if k.startswith("news") or k in ("news_body", "news_cover"):
        return "新闻图(news_body)"
    if "wiki" in k:
        return "维基/共享(wiki)"
    if k in ("pexels", "unsplash", "pixabay", "gallery"):
        return "免费图库"
    if not k:
        return "来源缺失"
    return f"其他({k})"


def _window(days: int) -> tuple[str, str]:
    end = datetime.now(CST).date()
    return (end - timedelta(days=days - 1)).strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def load_rows(days: int = 14) -> list[dict]:
    """[{date, title, images: n, cover_kind, cover_family, cover_w, cover_h}]。"""
    lo, hi = _window(days)
    if not OUTPUT_DIR.exists():
        return []
    rows = []
    for day in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
        if not (lo <= day.name <= hi):
            continue
        f = day / "metadata.json"
        if not f.exists():
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in (meta.get("articles") or []):
            if not isinstance(a, dict) or a.get("is_prediction"):
                continue
            imgs = a.get("images") or []
            prov_rows = [p for p in (a.get("image_provenance") or []) if isinstance(p, dict)]
            prov = {str((p or {}).get("file") or ""): (p or {}) for p in prov_rows}
            kind, w, h = "", None, None
            name, join_failed = "", False
            if imgs:
                first = imgs[0]
                name = (first.get("filename") or first.get("file") or
                        Path(str(first)).name) if isinstance(first, dict) else Path(str(first)).name
                hit = prov.get(str(name)) or {}
                kind = str(hit.get("kind") or "")
                w, h = hit.get("width"), hit.get("height")
                join_failed = bool(prov_rows) and not hit
            if not imgs:
                fam = "无封面"
            elif join_failed:
                fam = "查不到(join 失败)"
            elif not prov_rows:
                # 11-06 之前生成的产物没有 image_provenance（那栏是 11-06 才加的），
                # 归「来源缺失」而不是硬猜成新闻图。
                fam = "来源缺失(改造前)"
            else:
                fam = _family(kind)
            rows.append({"date": day.name, "title": str(a.get("title") or ""),
                         "slug": a.get("slug") or "", "images": len(imgs),
                         "cover_file": name if imgs else "",
                         "cover_kind": kind, "cover_family": fam,
                         "cover_w": w, "cover_h": h})
    return rows


def join_perf(rows: list[dict]) -> list[dict]:
    """把绩效日志的展示/阅读按 (date, slug) 挂上去。缺记录的篇不伪造 0。"""
    if not PERF_LOG.exists():
        return []
    try:
        perf = json.loads(PERF_LOG.read_text(encoding="utf-8")).get("articles") or {}
    except Exception:
        return []
    out = []
    for r in rows:
        p = perf.get(f"{r['date']}/{r['slug']}") or {}
        if not p:
            continue
        r2 = dict(r)
        r2["impressions"] = int(p.get("impressions") or 0)
        r2["reads"] = int(p.get("reads") or 0)
        out.append(r2)
    return out


def _agg(rs: list[dict]) -> dict:
    n = len(rs)
    imp = sum(r["impressions"] for r in rs)
    rd = sum(r["reads"] for r in rs)
    ctrs = [100.0 * r["reads"] / r["impressions"] for r in rs if r["impressions"]]
    sized = [r for r in rs if r["cover_w"] and r["cover_h"]]
    ok_size = [r for r in sized if r["cover_w"] >= COVER_MIN_W and r["cover_h"] >= COVER_MIN_H]
    return {"n": n, "impressions": imp, "reads": rd,
            "ctr": round(100.0 * rd / imp, 2) if imp else None,
            "median_ctr": round(st.median(ctrs), 2) if ctrs else None,
            "median_reads": st.median([r["reads"] for r in rs]) if rs else None,
            "size_known": len(sized),
            "size_ok": len(ok_size),
            "size_ok_pct": round(100.0 * len(ok_size) / len(sized), 1) if sized else None}


def analyze(days: int = 14) -> dict:
    rows = join_perf(load_rows(days))
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["cover_family"], []).append(r)
    table = {g: _agg(rs) for g, rs in groups.items()}

    verdict, note = "observe", ""
    card = table.get("信息卡(card:*)") or {}
    news = table.get("新闻图(news_body)") or {}
    if (card.get("n", 0) >= MIN_SAMPLE_PER_GROUP and news.get("n", 0) >= MIN_SAMPLE_PER_GROUP
            and card.get("ctr") and news.get("ctr") and news["ctr"] > 0):
        gap = news["ctr"] / card["ctr"] if card["ctr"] else None
        if gap and gap >= RATIO_GAP:
            verdict = "action"
            note = (f"新闻图 CTR {news['ctr']}% 是信息卡 {card['ctr']}% 的 {gap:.2f} 倍"
                    f"（≥{RATIO_GAP}×，样本 {news['n']}/{card['n']}）→ 建议立项改 cover_card 版式")
        else:
            note = (f"新闻图 {news['ctr']}% vs 信息卡 {card['ctr']}%：差 "
                    f"{gap:.2f}×<{RATIO_GAP}×，只观测")
    else:
        note = (f"样本不足（信息卡 {card.get('n', 0)} / 新闻图 {news.get('n', 0)}，"
                f"各需 ≥{MIN_SAMPLE_PER_GROUP}）→ 不给结论")
    chart = table.get("数据图(chart:*)") or {}
    if chart.get("n", 0) >= 5 and chart.get("ctr") is not None:
        note += f"；数据图 {chart['n']} 篇 CTR {chart['ctr']}%（单独观测）"

    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            "window": list(_window(days)), "days": days,
            "n_articles": len(rows), "table": table,
            "verdict": verdict, "note": note,
            "unknown_kind": sum(1 for r in rows
                                if r["cover_family"] in ("来源缺失(改造前)", "查不到(join 失败)"))}


def render_md(rep: dict, sample: int = 0, rows_sample: list | None = None) -> str:
    L = ["# 封面来源分层点击率（2026-11-16 改动点 5）", "",
         f"- 生成时间：{rep['generated_at']}｜窗口 {rep['window'][0]} ~ {rep['window'][1]}"
         f"（{rep['days']} 天）｜有绩效记录的篇数 {rep['n_articles']}",
         f"- 判定：**{rep['verdict']}** —— {rep['note']}", "",
         "| 封面来源 | 篇 | Σ展现 | Σ阅读 | 聚合 CTR | 中位 CTR | 中位阅读 | 尺寸≥800×600 |",
         "|---|---|---|---|---|---|---|---|"]
    for g, a in sorted(rep["table"].items(), key=lambda kv: -(kv[1]["n"] or 0)):
        size = (f"{a['size_ok']}/{a['size_known']}（{a['size_ok_pct']}%）"
                if a["size_known"] else "missing")
        L.append(f"| {g} | {a['n']} | {a['impressions']} | {a['reads']} | "
                 f"{a['ctr'] if a['ctr'] is not None else 'missing'}% | "
                 f"{a['median_ctr'] if a['median_ctr'] is not None else 'missing'}% | "
                 f"{a['median_reads'] if a['median_reads'] is not None else 'missing'} | {size} |")
    L += ["", f"- 来源字段缺失（`image_provenance` 里查不到封面文件名）：{rep['unknown_kind']} 篇",
          "- 口径：封面 = `images[0]`；按**文件名**与 `image_provenance` join（不按下标对）。",
          "- 只看有绩效记录的篇：`performance_log` 落库滞后 0~6 天，窗口末几天会缺。"]
    if sample and rows_sample:
        L += ["", f"## 前 {min(sample, len(rows_sample))} 篇人工核对（封面文件 → 来源 → 尺寸）", ""]
        for r in rows_sample[:sample]:
            L.append(f"- {r['date']} `{r.get('cover_file', '')}` → {r['cover_kind'] or 'missing'}"
                     f" ({r.get('cover_w')}×{r.get('cover_h')})｜{r['title'][:26]}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="封面来源分层点击率（纯观测）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--sample", type=int, default=10, help="打印 N 篇供人工核对封面配对（0=不打印）")
    args = ap.parse_args()
    rep = analyze(args.days)
    rows = join_perf(load_rows(args.days))
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / "cover_strata_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (AUDIT_DIR / f"cover_strata_{stamp}.md").write_text(
        render_md(rep, args.sample, rows), encoding="utf-8")
    print(render_md(rep, args.sample, rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
