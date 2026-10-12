#!/usr/bin/env python3
"""头条数据反馈 — 性能数据采集与存储

提供:
  1. log_article_performance() — 手动录入头条后台数据
  2. load_performance_log() — 读取历史性能数据
  3. update_metadata_performance() — 将性能数据写入 metadata.json

用法:
  # 命令行: 录入某篇文章的阅读/评论数据
  python performance_logger.py 2026-06-02 1 --reads 5000 --comments 32

  # 编程:
  from performance_logger import log_article_performance, update_metadata_performance
  log_article_performance("2026-06-02", 1, reads=5000, comments=32)
  update_metadata_performance("2026-06-02")
"""

import json, sys, os
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "output"))
PERF_LOG_PATH = OUTPUT_DIR / "performance_log.json"


# ============================================================
# 主键与取数（2026-10-12 修复）
#
# 旧主键是 f"{date}/article-{index}"，但 index 是「每批从 1 开始」的批内序号，
# 同一天多批次必然重复（实测 2026-10-12 的 index 2/3 各出现两次），
# 用它做键会让绩效数据串号写到别的文章上。
# 新主键改用 slug（同一天内唯一），旧键仅作只读兜底以兼容历史数据。
# ============================================================

def perf_key(date_str: str, index=None, slug=None) -> str:
    """绩效记录主键：有 slug 用 slug，否则回退旧键（历史数据兼容）。"""
    if slug:
        return f"{date_str}/{slug}"
    return f"{date_str}/article-{index}"


def resolve_performance(log_data: dict, date_str: str, index=None, slug=None) -> dict:
    """取一条绩效记录：slug 键优先，旧 article-<index> 键兜底；取不到返回 {}。"""
    arts = (log_data or {}).get("articles", {}) or {}
    if slug:
        rec = arts.get(f"{date_str}/{slug}")
        if isinstance(rec, dict):
            return rec
    if index is not None:
        rec = arts.get(f"{date_str}/article-{index}")
        if isinstance(rec, dict):
            return rec
    return {}


def index_occurrences(meta: dict) -> dict:
    """统计 metadata 当天每个 index 出现次数（用于识别 index 不唯一 → 禁止按 index 回写）。"""
    counts: dict = {}
    for a in meta.get("articles", []) or []:
        i = a.get("index")
        if i is not None:
            counts[i] = counts.get(i, 0) + 1
    return counts


def load_performance_log():
    """Load the full performance log."""
    if not PERF_LOG_PATH.exists():
        return {"articles": {}, "updated_at": None}
    try:
        return json.loads(PERF_LOG_PATH.read_text())
    except Exception:
        return {"articles": {}, "updated_at": None}


def save_performance_log(log_data):
    """Save the performance log."""
    log_data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    PERF_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PERF_LOG_PATH.write_text(json.dumps(log_data, ensure_ascii=False, indent=2))


def log_article_performance(date_str, article_index=None, reads=0, comments=0, likes=0, shares=0, retention_rate=0.0, new_followers=0, impressions=0, follower_reads=0, slug=None):
    """Record performance metrics for a single article.

    Args:
        date_str: "2026-06-02"
        article_index: 批内 1-based 序号（**同一天可能重复，故仅作旧键兜底**）
        slug: metadata.articles[].slug，同一天内唯一；有则用它做主键（推荐）
        reads: 阅读量
        comments: 评论数
        likes: 点赞数
        shares: 分享数
        retention_rate: 完读率 (0.0-1.0)
        new_followers: 当日涨粉数（计划 11.6：配比归因必需字段）
        impressions: 展现量/曝光量（计划第十章：点击率 = reads / impressions）
        follower_reads: 粉丝阅读量（计划第十章：粉丝阅读占比 = follower_reads / reads）
    """
    log_data = load_performance_log()
    key = perf_key(date_str, index=article_index, slug=slug)
    entry = {
        "date": date_str,
        "index": article_index,
        "slug": slug,
    }
    # 缺数据的指标（None = 待接入）不写 key，绝不用 0 冒充真实读数
    for field, value in (("reads", reads), ("comments", comments), ("likes", likes),
                         ("shares", shares), ("retention_rate", retention_rate),
                         ("new_followers", new_followers), ("impressions", impressions),
                         ("follower_reads", follower_reads)):
        if value is not None:
            entry[field] = value
    entry["logged_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_data["articles"][key] = entry
    save_performance_log(log_data)
    print(f"   性能数据已记录: {key} — 阅读:{reads} 评论:{comments} 点赞:{likes} 涨粉:{new_followers}")
    return log_data


def update_metadata_performance(date_str):
    """Sync performance data from performance_log.json into metadata.json articles.

    This allows analyze_content_performance() to pick up real Toutiao data.
    """
    meta_path = OUTPUT_DIR / date_str / "metadata.json"
    if not meta_path.exists():
        print(f"   ⚠️  metadata.json not found for {date_str}")
        return

    log_data = load_performance_log()
    meta = json.loads(meta_path.read_text())
    counts = index_occurrences(meta)

    updated = 0
    skipped_ambiguous = 0
    for article in meta.get("articles", []):
        idx = article.get("index", 0)
        slug = article.get("slug")

        perf = resolve_performance(log_data, date_str, slug=slug) if slug else {}
        if not perf:
            # 旧键兜底：只有当天 index 唯一时才敢按 index 回写，
            # 否则同一 index 会有多篇文章，写下去就是串号。
            if counts.get(idx, 0) == 1:
                perf = resolve_performance(log_data, date_str, index=idx)
            elif counts.get(idx, 0) > 1:
                skipped_ambiguous += 1
        if perf:
            # 只回写台账里真实存在的指标；缺数据的字段不落 0（避免把「无数据」写成 0）
            fields = ("reads", "comments", "likes", "shares", "retention_rate",
                      "new_followers", "impressions", "follower_reads")
            article["performance"] = {k: perf[k] for k in fields if k in perf}
            updated += 1

    if updated > 0:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        print(f"   ✅ 已同步 {updated} 篇文章的性能数据到 metadata.json")
    else:
        print(f"   无新性能数据需要同步 ({date_str})")
    if skipped_ambiguous:
        print(f"   ⚠️  {skipped_ambiguous} 篇因 index 当天不唯一且无 slug 匹配被跳过（避免串号）")


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="头条性能数据录入")
    parser.add_argument("date", help="日期 (YYYY-MM-DD)")
    parser.add_argument("index", type=int, nargs="?", default=None,
                        help="批内序号 (1-based)；同一天可能重复，建议改用 --slug")
    parser.add_argument("--slug", default=None,
                        help="文章 slug（metadata.articles[].slug，同一天内唯一，推荐）")
    parser.add_argument("--reads", type=int, default=0, help="阅读量")
    parser.add_argument("--comments", type=int, default=0, help="评论数")
    parser.add_argument("--likes", type=int, default=0, help="点赞数")
    parser.add_argument("--shares", type=int, default=0, help="分享数")
    parser.add_argument("--retention", type=float, default=0.0, help="完读率")
    parser.add_argument("--followers", type=int, default=0, help="当日涨粉数（计划 11.6）")
    parser.add_argument("--impressions", type=int, default=0, help="展现量（点击率 = 阅读/展现）")
    parser.add_argument("--follower-reads", type=int, default=0, help="粉丝阅读量（粉丝阅读占比）")
    parser.add_argument("--sync", action="store_true", help="同步到 metadata.json")

    args = parser.parse_args()

    log_article_performance(args.date, args.index,
                            reads=args.reads, comments=args.comments,
                            likes=args.likes, shares=args.shares,
                            retention_rate=args.retention,
                            new_followers=args.followers,
                            impressions=args.impressions,
                            follower_reads=args.follower_reads,
                            slug=args.slug)

    if args.sync:
        update_metadata_performance(args.date)
