"""内容类型配比：目标、实测偏差与硬约束（2026-11-01 月度分析）。

## 为什么单独有这个模块

`config/ratio_config.yaml` 的「选题类型配比」走的是 **topic_category** 轴
（转会官宣 / 中国足球 / 冲突舆情 / 未分类 / 战报比分），由 `ratio_tuner` 调参。
但 10 月实测发现另一个轴才是真问题：**content_type**（热点球评 / 战术榜单 /
人物故事 / 历史回顾 …）——10-27 之后的 32 篇里战术榜单占了 47%，
而人物故事 + 历史回顾从 23% 掉到 9% 只是「换了个靶子」，不是结构改善了。

两个轴不重叠，ratio_tuner 管不到 content_type，所以这里单独建一份。

## 配比从哪来（不是拍脑袋）

口径：**实际发布且拿到作品 ID 的作品**（10 月 93 篇，来自头条后台导出原始表），
不用「成稿数」——成稿口径会系统性高估弱类型（人物故事成稿口径 27.0、
发布口径只有 5.9，差 4.6 倍；因为被丢弃的批次更可能是弱内容）。

| content_type | n  | 篇均阅读 | 中位 | 10月占比 | 目标(11-04) |
| 热点球评     | 15 | 48.1     | 15   | 16%    | 40   |
| 数据对比     | 11 | 17.1     | 6    | 12%    | 25   |
| 排行榜       | 4  | 30.5     | 16   | 4%     | 15   |
| 战术榜单     | 13 | 21.2     | 1    | 14%    | 10   |
| 八卦趣事     | 15 | 14.7     | 8    | 16%    | 4    |
| 人物故事     | 15 | 5.9      | 1    | 16%    | 3    |
| 转会资讯     | 5  | 11.4     | 1    | 5%     | 3    |
| 历史回顾     | 9  | 0.9      | 0    | 10%    | 0 （禁发）|
| 榜单         | 4  | 0.5      | 0    | 4%     | 0 （并入排行榜）|
| 战术解析     | 1  | 0.0      | 0    | 1%     | 0 （并入战术榜单）|

纪律：
  - **只算不写**。本模块给出偏差与建议，配置变更人工审批（与 ratio_tuner 一致）；
  - 样本不足的类型不参与判定（<`MIN_SAMPLE` 篇），避免 1 篇定生死；
  - 偏差超容差只告警，**不阻断发布**——配比是慢变量，按天卡死会逼出凑数稿。
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# content_type 目标占比（百分点，合计 100）。依据见模块 docstring 的实测表。
#
# ▼ 2026-11-04 夜间重定（上一版 40/25/15/10/4/3/3）——**修「规则与阈值相斥」**：
#   上一版是「按篇均阅读排序」定出来的，但没人拿它跟 P1-1 的每批结构对一遍。
#   对一遍就露馅：`batch_mix.STRUCTURE = (main, data)` 每批固定 1 主 + 1 数据型，
#   一天 3 批（周四 4 批）的稳态份额是 **主 50% / 数据型 50%**，而旧目标写的是
#   主 40% / 数据 25% / 排行 15% —— 结构每天都在「超标 10pp」，排行榜还被 data 槽的
#   固定顺序挤到 0%，于是配比告警天天亮、且永远无法通过调度消除。**这不是数据问题，
#   是两个模块各写各的。**
#
#   新数值 = 结构可产出份额（模拟值，见 verify_batch_mix「结构模拟 vs 目标」硬项）
#   ± 给补位留的余量。排序（热点 > 数据 > 排行 > 战术 > 低效）完全保留，
#   所以「压战术榜单、抬数据型」的原意没有变，变的只是把数字改成结构真能产出的量：
#     热点球评 45（结构 50，留 5pp 给战术榜单补位主槽）
#     数据对比 30（结构 31.2，data 槽按目标缺口分，25:15 收敛到 5:3）
#     排行榜   18（结构 18.8 —— 旧值 15 时它**永远拿不到槽位**）
#     战术榜单  4（只做补位；旧值 10 意味着每天该有 0.6 篇，而结构上它拿不到主槽）
#     人物故事/转会资讯/八卦趣事 各 1（只在加发批第 3 篇以后补位）
#   历史回顾/榜单/战术解析仍为 0（禁发/已并入）。
TARGET_PCT: dict[str, float] = {
    "热点球评": 45.0,
    "数据对比": 30.0,
    "排行榜": 18.0,
    "战术榜单": 4.0,
    "人物故事": 1.0,
    "转会资讯": 1.0,
    "八卦趣事": 1.0,
    "历史回顾": 0.0,
    "榜单": 0.0,
    "战术解析": 0.0,
}

# 目标为 0 的类型（禁发或已并入其他类型）。与 reservoir.BANNED_TYPES 同源，
# 但这里是「content_type 轴」的禁发，两者要一起看：reservoir 管蓄水池库存，
# 本表管产出配比。新增禁发类型必须两处都改，否则会出现「库里禁了、配比还留着」。
ZERO_TARGET = tuple(k for k, v in TARGET_PCT.items() if v == 0)

# 已并入其他类型的别名：统计时先归一再判，避免同一类内容被拆成两半互相「合格」
#
# 「数据对照」必须并进「数据对比」（2026-11-06 发现）：赛程角度表
# `fixture_library.ANGLE_RULES` 的 BC 型落的是「数据对照」，而 TARGET_PCT 与
# DAILY_CAP 的键是「数据对比」—— 两个名字，同一类稿子。后果有两层：
#   1. 数据对照不进 TARGET_PCT 统计，永远「占比 0%」，而目标写着 30%；
#   2. DAILY_CAP 里查不到「数据对照」→ 返回 None → **没有单日上限**。
# 这正是「同一量两处定义」的老毛病，别名表是唯一该合并的地方。
ALIASES = {
    "战术解析": "战术榜单",
    "榜单": "排行榜",
    "紧急球评": "热点球评",
    "数据对照": "数据对比",
}

# 单类型占比容差（百分点）。超出仅告警：配比是慢变量，按天卡死会逼出凑数稿。
TOL_PCT = 10.0
# 参与判定所需最小样本（篇）。
MIN_SAMPLE = 5


def normalize(content_type: str | None) -> str:
    """把 content_type 归一到统计口径（别名合并）。未知类型原样返回。"""
    ct = (content_type or "").strip()
    if not ct:
        return "未分类"
    return ALIASES.get(ct, ct)


def check(articles: list[dict]) -> dict:
    """统计一批（通常为一天）成稿的 content_type 分布，对照目标算偏差。

    articles: 每项至少含 `content_type`；可选 `reads`（有则一并给篇均参考）。

    返回结构（供 verify 脚本与报告直接消费）：
      ok       是否有硬问题（出现目标为 0 的类型 = 硬失败）
      total    参与统计篇数
      zero_hits 命中的禁发类型 → {类型: 篇数}
      items    每类：{count, pct, target, dev, verdict}
      warnings 偏差超容差但未达硬失败的类型
    """
    counts: dict[str, int] = {}
    reads: dict[str, list] = {}
    for a in articles or []:
        ct = normalize(a.get("content_type"))
        counts[ct] = counts.get(ct, 0) + 1
        if a.get("reads") is not None:
            reads.setdefault(ct, []).append(a.get("reads") or 0)

    total = sum(counts.values())
    items: dict[str, dict] = {}
    zero_hits: dict[str, int] = {}
    warnings: list[str] = []

    for ct, n in sorted(counts.items(), key=lambda x: -x[1]):
        pct = (100.0 * n / total) if total else 0.0
        target = TARGET_PCT.get(ct)
        if target is None:
            # 目标表里没有的新类型：不算硬失败，但要显式暴露，别让它偷偷长起来
            items[ct] = {"count": n, "pct": round(pct, 1), "target": None,
                         "dev": None, "verdict": "未登记类型（请在 TARGET_PCT 补目标）",
                         "avg_reads": round(sum(reads[ct]) / len(reads[ct]), 1) if reads.get(ct) else None}
            warnings.append(f"{ct} 未在目标配比表登记（{n} 篇，{pct:.0f}%）")
            continue
        dev = pct - target
        if target == 0:
            zero_hits[ct] = n
            verdict = "禁发类型仍产出 ❌"
        elif n < MIN_SAMPLE:
            # 样本不足：只报数不下结论，避免「1 篇 100%」这种噪声
            verdict = f"样本不足（{n} < {MIN_SAMPLE}），不判定"
        elif abs(dev) > TOL_PCT:
            verdict = f"偏差 {dev:+.0f}pp 超容差 {TOL_PCT:.0f}"
            warnings.append(f"{ct} 占比 {pct:.0f}% 目标 {target:.0f}%（{dev:+.0f}pp）")
        else:
            verdict = "在容差内 ✅"
        items[ct] = {"count": n, "pct": round(pct, 1), "target": target,
                     "dev": round(dev, 1), "verdict": verdict,
                     "avg_reads": round(sum(reads[ct]) / len(reads[ct]), 1) if reads.get(ct) else None}

    return {
        "ok": not zero_hits,
        "total": total,
        "zero_hits": zero_hits,
        "items": items,
        "warnings": warnings,
        "target_pct": dict(TARGET_PCT),
        "tolerance_pct": TOL_PCT,
        "min_sample": MIN_SAMPLE,
    }


def render_md(rep: dict, *, title: str = "内容类型配比复核") -> str:
    """把 check() 结果渲染成人话摘要（供 batch 日志与审计报告复用）。"""
    L = [f"# {title}", "",
         f"- 统计篇数：{rep.get('total')}｜容差 {rep.get('tolerance_pct')}pp"
         f"｜最小样本 {rep.get('min_sample')} 篇",
         ""]
    L.append("| 类型 | 篇数 | 占比 | 目标 | 偏差 | 篇均阅读 | 判定 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for ct, it in (rep.get("items") or {}).items():
        tgt = "—" if it.get("target") is None else f"{it['target']:.0f}%"
        dev = "—" if it.get("dev") is None else f"{it['dev']:+.0f}pp"
        avg = "—" if it.get("avg_reads") is None else f"{it['avg_reads']}"
        L.append(f"| {ct} | {it['count']} | {it['pct']:.0f}% | {tgt} | {dev} | {avg} | {it['verdict']} |")
    if rep.get("zero_hits"):
        L.append("")
        L.append("> ❌ 禁发类型仍产出：" + "、".join(f"{k} {v} 篇" for k, v in rep["zero_hits"].items()))
    if rep.get("warnings"):
        L.append("")
        for w in rep["warnings"]:
            L.append(f"> ⚠️ {w}")
    L.append("")
    L.append(f"**结论：{'通过 ✅' if rep.get('ok') else '有禁发类型产出 ❌'}**")
    return "\n".join(L) + "\n"


def load_day_articles(output_dir: Path | str | None = None, date_str: str = "") -> list[dict]:
    """从 output/<date>/metadata.json 读成稿（标题/类型/绩效），供 verify 与报告使用。"""
    return load_range_articles(output_dir, date_str, date_str)


def load_range_articles(output_dir: Path | str | None = None,
                        date_from: str = "", date_to: str = "") -> list[dict]:
    """读 [date_from, date_to] 区间内每天的成稿。

    **配比必须按滚动窗口统计，不能按单日**：单日只有 6 篇，任何类型都到不了
    MIN_SAMPLE（5 篇），闸门会 100% 空转成「样本不足，不判定」——那就是写了等于没写。
    首版实现按天统计，实测在 10-27/10-31 两天全部空转，故改为区间。
    """
    base = Path(output_dir) if output_dir else (PROJECT_ROOT / "output")
    if not base.exists():
        return []
    lo, hi = date_from or "", date_to or date_from or ""
    out: list[dict] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        if len(name) != 10 or name[4] != "-":      # 只要 YYYY-MM-DD 形态，跳过其它目录
            continue
        if lo and name < lo:
            continue
        if hi and name > hi:
            continue
        mp = d / "metadata.json"
        if not mp.exists():
            continue
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in meta.get("articles") or []:
            perf = a.get("performance") or {}
            out.append({"date": name, "title": a.get("title", ""), "slug": a.get("slug", ""),
                        "content_type": a.get("content_type", ""),
                        "reads": perf.get("reads")})
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    from datetime import datetime, timedelta

    ap = argparse.ArgumentParser(description="内容类型配比复核（只算不写）")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="窗口结束日")
    ap.add_argument("--days", type=int, default=7, help="滚动窗口天数（默认 7；单日无统计意义）")
    ap.add_argument("--output-dir", default="", help="output 根目录（默认仓库内 output/）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    end = datetime.strptime(args.date, "%Y-%m-%d")
    start = end - timedelta(days=max(1, args.days) - 1)
    arts = load_range_articles(args.output_dir or None,
                               start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if not arts:
        print(f"（{start:%Y-%m-%d}~{end:%Y-%m-%d} 无成稿，跳过）")
        return 0
    rep = check(arts)
    rep["window"] = {"from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d"),
                     "days": max(1, args.days)}
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render_md(rep, title=f"内容类型配比复核（{start:%m-%d} ~ {end:%m-%d}）"))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
