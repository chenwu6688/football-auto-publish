#!/usr/bin/env python3
"""头条后台增长指标批量回填通道（计划第十章）。

第十章验收指标（篇均阅读 / 点击率 / 完成率 / 粉丝阅读占比 / 涨粉）依赖头条后台真实数据。
此前只能逐篇 `performance_logger.py <日期> <序号> --reads N` 手动录入，量一大就没法用。

本通道有两条入口：

1) **头条后台导出直通（--from-toutiao）** —— 推荐
   直接把后台「数据 → 作品数据 → 导出 Excel」的下载文件丢进来即可。
   适配层（toutiao_import.py）负责：
     - 无依赖解析 .xlsx / .csv / .tsv / .json；
     - 中文列名 → 规范字段（最长匹配，防止「粉丝阅读量」被「阅读量」吃掉）；
     - **按标题**对齐 output/<日期>/metadata.json 得到唯一键 slug
       （导出里没有我们的「日期/序号」，且 index 是批内序号、同一天会重复，不可做键）；
     - 未匹配 / 跨天同名歧义的行 **一律不写库**，只进报告。
   新增：`--map` 只看列映射与匹配结果；`--unmatched-out` 导出未匹配明细。

2) **规范 CSV / JSON（date,index 直填）** —— 已有通道，保持兼容
   列：date,index,reads,comments,likes,shares,retention_rate,new_followers,impressions,follower_reads
     - date         YYYY-MM-DD
     - index        批内 1-based 序号（同一天可能重复，建议改填 slug）
     - slug         metadata.articles[].slug，同一天内唯一（**推荐**，与 index 二者其一）
     - retention_rate  完读率，0-1 小数或 0-100 百分数（>1 自动按百分数换算）
     - impressions  展现量（点击率 = reads/impressions）
     - follower_reads  粉丝阅读量（粉丝阅读占比 = follower_reads/reads）

用法：
    python3 scripts/import_performance.py --template > perf.csv
    python3 scripts/import_performance.py --from-toutiao 作品数据.xlsx            # 头条导出直通
    python3 scripts/import_performance.py --from-toutiao 作品数据.xlsx --map      # 只看映射不写库
    python3 scripts/import_performance.py --from-toutiao 作品数据.csv --dry-run
    python3 scripts/import_performance.py perf.csv                                # 规范 CSV
    python3 scripts/import_performance.py perf.json --dry-run
    python3 scripts/import_performance.py perf.csv --no-sync                      # 只写日志不回写 metadata

诚实原则（计划第十章）：没有的字段一律留空由下游标「待接入」，绝不用 0 冒充。
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import performance_logger as pl          # noqa: E402
import toutiao_import as tti             # noqa: E402

REQUIRED = ("date",)
INT_FIELDS = ("index", "reads", "comments", "likes", "shares", "new_followers",
              "impressions", "follower_reads")
TEMPLATE_HEADER = ["date", "index", "slug", "reads", "comments", "likes", "shares",
                   "retention_rate", "new_followers", "impressions", "follower_reads"]


def _parse_date(s) -> str | None:
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return None


def _to_float(v, default=0.0) -> float:
    x = tti.clean_number(v)
    return default if x is None else float(x)


def _to_int(v, default=0) -> int:
    x = tti.clean_number(v)
    return default if x is None else int(round(x))


def validate_row(row: dict, lineno: int) -> tuple[dict | None, str | None]:
    """校验并规范化一行；返回 (记录, None) 或 (None, 错误原因)。

    主键要求：date 必填；index 与 slug 至少提供一个。
    （index 是批内序号、同一天可能重复，slug 才唯一，故 slug 优先。）
    """
    d = _parse_date(row.get("date"))
    if not d:
        return None, f"第 {lineno} 行：date 非法（须 YYYY-MM-DD）"
    slug = str(row.get("slug") or "").strip() or None
    idx = _to_int(row.get("index"), 0)
    if idx <= 0 and not slug:
        return None, f"第 {lineno} 行：index 非法（须 ≥1）且未提供 slug"
    rec = {"date": d, "index": idx, "slug": slug}
    for f in INT_FIELDS:
        if f == "index":
            continue
        v = _to_int(row.get(f), 0)
        if v < 0:
            return None, f"第 {lineno} 行：{f} 为负（{v}）"
        rec[f] = v
    raw_ret = row.get("retention_rate")
    if raw_ret is None or str(raw_ret).strip() == "":
        # 未提供完读率 → 存 None = 「待接入」，绝不用 0 冒充真实数据
        # （头条后台的 -1 / -1.00 占位符已在 toutiao_import 层判为 None）
        rec["retention_rate"] = None
    else:
        r = _to_float(raw_ret, 0.0)
        if r < 0:
            return None, f"第 {lineno} 行：retention_rate 为负（{raw_ret}）"
        if r > 1.0:
            r = r / 100.0            # 百分数自动换算
        if r > 1.0:
            return None, f"第 {lineno} 行：retention_rate 超出范围（{raw_ret}）"
        rec["retention_rate"] = round(r, 4)
    return rec, None


def load_rows(path: Path) -> list[dict]:
    """读规范 CSV / JSON，返回原始行 dict 列表（带 __lineno）。"""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        rows = data.get("articles", []) if isinstance(data, dict) else data
        return [dict(r, __lineno=i + 1) for i, r in enumerate(rows)]
    rows = list(csv.DictReader(text.splitlines()))
    return [dict(r, __lineno=i + 2) for i, r in enumerate(rows)]


def import_rows(rows: list[dict], *, dry_run: bool = False, sync: bool = True) -> dict:
    """逐行校验并写入 performance_log；返回统计。"""
    ok, errors = [], []
    for raw in rows:
        rec, err = validate_row(raw, raw.get("__lineno", 0))
        (errors if err else ok).append(err or rec)

    dates: set[str] = set()
    if not dry_run:
        for rec in ok:
            pl.log_article_performance(
                rec["date"], rec["index"], reads=rec["reads"], comments=rec["comments"],
                likes=rec["likes"], shares=rec["shares"],
                retention_rate=rec["retention_rate"], new_followers=rec["new_followers"],
                impressions=rec["impressions"], follower_reads=rec["follower_reads"],
                slug=rec.get("slug"))
            dates.add(rec["date"])
        if sync:
            for d in sorted(dates):
                pl.update_metadata_performance(d)

    return {"imported": len(ok), "errors": errors, "dates": sorted(dates),
            "dry_run": dry_run}


# ------------------------------------------------------------
# 头条后台导出直通
# ------------------------------------------------------------

def _dump_unmatched(rep: dict, out: Path) -> None:
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["row", "reason", "title", "date", "candidates"])
            for key in ("unmatched", "ambiguous"):
                for x in rep.get(key, []) or []:
                    w.writerow([x.get("row"), key, x.get("title"),
                                x.get("date") or "",
                                "|".join(x.get("candidates") or [])])
        print(f"   未匹配/歧义明细：{out}")
    except Exception as e:
        print(f"   ⚠️ 未匹配明细导出失败：{type(e).__name__}: {e}")


def render_toutiao_report(res: dict, src: Path, *, dry_run: bool) -> str:
    rep = res["report"]
    L = ["# 头条后台数据导入报告（计划第十章）", "",
         f"- 来源文件：`{src}`",
         f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
         f"- 模式：{'试运行（未写库）' if dry_run else '已写库'}", ""]

    if not rep.get("ok"):
        L += ["## ⛔ 未能导入", "", rep.get("error", "（无详细信息）"), ""]
        return "\n".join(L)

    L += [f"表头所在行：第 {rep['header_row'] + 1} 行", "", "## 列映射", "",
          "| 规范字段 | 导出列名 |", "| --- | --- |"]
    for f, h in sorted(rep["mapping"].items()):
        L.append(f"| {f} | {h} |")
    L.append("")
    if rep["unmapped"]:
        L += [f"- ⚠️ 未识别列（已忽略）：{'、'.join(rep['unmapped'])}", ""]

    L += ["## 匹配结果", "",
          f"- 命中可回填：**{len(rep['matched'])}** 行",
          f"- 未匹配（不在本地 metadata 中，**未写库**）：{len(rep['unmatched'])} 行",
          f"- 标题歧义（跨天同名，**未写库**）：{len(rep['ambiguous'])} 行",
          f"- 非图文作品（微头条 / 视频 / 问答，跳过）：{len(rep['non_article'])} 行", ""]

    for key, label in (("unmatched", "未匹配明细"), ("ambiguous", "标题歧义明细")):
        rows = rep.get(key) or []
        if not rows:
            continue
        L += [f"### {label}", ""]
        for x in rows:
            extra = f"　候选：{', '.join(x['candidates'])}" if x.get("candidates") else ""
            L.append(f"- 第 {x['row']} 行：{x['title']}{extra}")
        L.append("")

    L += ["> 纪律：未匹配 / 歧义的行一律不写库，绝不用 0 或猜测数据冒充真实数据。", ""]
    return "\n".join(L)


def run_toutiao(path: Path, args) -> int:
    try:
        rows = tti.read_table(path)
    except Exception as e:
        print(f"❌ 解析失败：{type(e).__name__}: {e}")
        return 2

    res = tti.adapt_rows(rows, output_dir=pl.OUTPUT_DIR)
    rep = res["report"]

    if rep.get("ok"):
        print(f"表头：第 {rep['header_row'] + 1} 行（1-based）")
        for f, h in sorted(rep["mapping"].items()):
            print(f"   {h}  →  {f}")
        if rep["unmapped"]:
            print(f"   ⚠️ 未识别列（已忽略）：{'、'.join(rep['unmapped'])}")
        print(f"命中 {len(rep['matched'])}｜未匹配 {len(rep['unmatched'])}｜"
              f"歧义 {len(rep['ambiguous'])}｜非图文 {len(rep['non_article'])}")
        for x in rep["unmatched"][:5]:
            print(f"   · 未匹配：{x['title']}")
        for x in rep["ambiguous"][:5]:
            print(f"   · 歧义：{x['title']} → {', '.join(x['candidates'])}")
    else:
        print(f"❌ {rep.get('error')}")

    if args.unmatched_out:
        _dump_unmatched(rep, Path(args.unmatched_out))

    report_path = (Path(args.report) if args.report else
                   PROJECT_ROOT / "data" / "audit"
                   / f"import_performance_{datetime.now():%Y-%m-%d}.md")
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            render_toutiao_report(res, path, dry_run=(args.dry_run or args.map)),
            encoding="utf-8")
        print(f"报告：{report_path}")
    except Exception as e:
        print(f"⚠️ 报告写入失败：{type(e).__name__}: {e}")

    if args.map or args.dry_run:
        print("（--map / --dry-run：未写库）")
        return 0 if rep.get("ok") else 1

    records = res["records"]
    if not records:
        print("没有可回填的行（未匹配的行不会被写入）")
        return 0 if rep.get("ok") else 1

    stats = import_rows(records, dry_run=False, sync=not args.no_sync)
    print(f"✅ 回填 {stats['imported']} 行，涉及 {len(stats['dates'])} 天")
    for e in stats["errors"]:
        print(f"   ⚠️ {e}")
    if stats["imported"] and not args.no_sync:
        print("   下一步：python3 scripts/build_metrics_dashboard.py")
    return 0 if not stats["errors"] else 1


# ------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="头条增长指标批量回填（计划第十章）")
    ap.add_argument("path", nargs="?", help="CSV / JSON / XLSX 文件路径")
    ap.add_argument("--template", action="store_true", help="输出规范 CSV 模板并退出")
    ap.add_argument("--from-toutiao", action="store_true",
                    help="按头条后台导出格式解析（中文列名，按标题匹配文章）")
    ap.add_argument("--map", action="store_true", help="只打印列映射与匹配结果，不写库")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写入")
    ap.add_argument("--no-sync", action="store_true", help="不回写 metadata.json")
    ap.add_argument("--unmatched-out", default=None, help="未匹配/歧义明细导出为 CSV")
    ap.add_argument("--report", default=None, help="报告输出路径（默认 data/audit/…）")
    args = ap.parse_args()

    if args.template:
        w = csv.writer(sys.stdout)
        w.writerow(TEMPLATE_HEADER)
        w.writerow(["2026-10-10", "1", "示例slug-abc123", "5000", "32", "120", "8",
                    "0.41", "12", "180000", "300"])
        return 0
    if not args.path:
        ap.print_help()
        return 2

    p = Path(args.path)
    if not p.exists():
        print(f"❌ 文件不存在：{p}")
        return 2

    if args.from_toutiao:
        return run_toutiao(p, args)

    try:
        rows = load_rows(p)
    except Exception as e:
        print(f"❌ 解析失败：{type(e).__name__}: {e}")
        return 2

    res = import_rows(rows, dry_run=args.dry_run, sync=not args.no_sync)
    print(f"{'（试运行）' if args.dry_run else ''}导入 {res['imported']} 行，"
          f"涉及日期 {len(res['dates'])} 天")
    for e in res["errors"]:
        print(f"   ⚠️ {e}")
    if not args.dry_run and res["imported"]:
        print("   下一步：运行 scripts/build_metrics_dashboard.py 生成看板")
    return 1 if res["errors"] and not res["imported"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
