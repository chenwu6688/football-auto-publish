#!/usr/bin/env python3
"""头条后台增长指标批量回填通道（计划第十章）。

第十章验收指标（篇均阅读 / 点击率 / 完成率 / 粉丝阅读占比 / 涨粉）依赖头条后台真实数据。
此前只能逐篇 `performance_logger.py <日期> <序号> --reads N` 手动录入，量一大就没法用。

本通道支持从 CSV / JSON 批量导入，逐行校验后写入 output/performance_log.json，
并把结果回写 output/<日期>/metadata.json 的 article.performance（看板据此显示真实值）。

CSV 列（首行表头，缺列按 0；date/index 必填）：
    date,index,reads,comments,likes,shares,retention_rate,new_followers,impressions,follower_reads
  - date         YYYY-MM-DD
  - index        1-based 文章序号（对应 metadata.articles[].index）
  - retention_rate  完读率，0-1 小数或 0-100 百分数（>1 自动按百分数换算）
  - impressions  展现量（用于点击率 = reads/impressions）
  - follower_reads  粉丝阅读量（用于粉丝阅读占比 = follower_reads/reads）

JSON 格式：上述字段的对象数组，或 {"articles": [...]}。

用法：
    python3 scripts/import_performance.py --template > perf.csv   # 导出模板
    python3 scripts/import_performance.py data/perf_2026-10.csv
    python3 scripts/import_performance.py perf.json --dry-run
    python3 scripts/import_performance.py perf.csv --no-sync     # 只写日志不回写 metadata
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import performance_logger as pl  # noqa: E402

REQUIRED = ("date", "index")
INT_FIELDS = ("index", "reads", "comments", "likes", "shares", "new_followers",
              "impressions", "follower_reads")
TEMPLATE_HEADER = ["date", "index", "reads", "comments", "likes", "shares",
                   "retention_rate", "new_followers", "impressions", "follower_reads"]


def _parse_date(s) -> str | None:
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return None


def _to_float(v, default=0.0) -> float:
    try:
        return float(str(v).strip() or 0)
    except Exception:
        return default


def _to_int(v, default=0) -> int:
    try:
        return int(round(float(str(v).strip() or 0)))
    except Exception:
        return default


def validate_row(row: dict, lineno: int) -> tuple[dict | None, str | None]:
    """校验并规范化一行；返回 (记录, None) 或 (None, 错误原因)。"""
    d = _parse_date(row.get("date"))
    if not d:
        return None, f"第 {lineno} 行：date 非法（须 YYYY-MM-DD）"
    idx = _to_int(row.get("index"), 0)
    if idx <= 0:
        return None, f"第 {lineno} 行：index 非法（须 ≥1）"
    rec = {"date": d, "index": idx}
    for f in INT_FIELDS:
        if f == "index":
            continue
        v = _to_int(row.get(f), 0)
        if v < 0:
            return None, f"第 {lineno} 行：{f} 为负（{v}）"
        rec[f] = v
    r = _to_float(row.get("retention_rate"), 0.0)
    if r < 0:
        return None, f"第 {lineno} 行：retention_rate 为负"
    if r > 1.0:
        r = r / 100.0            # 百分数自动换算
    if r > 1.0:
        return None, f"第 {lineno} 行：retention_rate 超出范围（{row.get('retention_rate')}）"
    rec["retention_rate"] = round(r, 4)
    return rec, None


def load_rows(path: Path) -> list[dict]:
    """读 CSV / JSON，返回原始行 dict 列表（带 __lineno）。"""
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
                impressions=rec["impressions"], follower_reads=rec["follower_reads"])
            dates.add(rec["date"])
        if sync:
            for d in sorted(dates):
                pl.update_metadata_performance(d)

    return {"imported": len(ok), "errors": errors, "dates": sorted(dates),
            "dry_run": dry_run}


def main() -> int:
    ap = argparse.ArgumentParser(description="头条增长指标批量回填（计划第十章）")
    ap.add_argument("path", nargs="?", help="CSV / JSON 文件路径")
    ap.add_argument("--template", action="store_true", help="输出 CSV 模板并退出")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写入")
    ap.add_argument("--no-sync", action="store_true", help="不回写 metadata.json")
    args = ap.parse_args()

    if args.template:
        w = csv.writer(sys.stdout)
        w.writerow(TEMPLATE_HEADER)
        w.writerow(["2026-10-10", "1", "5000", "32", "120", "8", "0.41", "12", "180000", "300"])
        return 0
    if not args.path:
        ap.print_help()
        return 2

    p = Path(args.path)
    if not p.exists():
        print(f"❌ 文件不存在：{p}")
        return 2
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
