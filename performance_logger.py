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


def log_article_performance(date_str, article_index, reads=0, comments=0, likes=0, shares=0, retention_rate=0.0):
    """Record performance metrics for a single article.

    Args:
        date_str: "2026-06-02"
        article_index: 1-based article index
        reads: 阅读量
        comments: 评论数
        likes: 点赞数
        shares: 分享数
        retention_rate: 完读率 (0.0-1.0)
    """
    log_data = load_performance_log()
    key = f"{date_str}/article-{article_index}"
    log_data["articles"][key] = {
        "date": date_str,
        "index": article_index,
        "reads": reads,
        "comments": comments,
        "likes": likes,
        "shares": shares,
        "retention_rate": retention_rate,
        "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_performance_log(log_data)
    print(f"   性能数据已记录: {key} — 阅读:{reads} 评论:{comments} 点赞:{likes}")
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

    updated = 0
    for article in meta.get("articles", []):
        idx = article.get("index", 0)
        key = f"{date_str}/article-{idx}"
        if key in log_data["articles"]:
            perf = log_data["articles"][key]
            article["performance"] = {
                "reads": perf["reads"],
                "comments": perf["comments"],
                "likes": perf["likes"],
                "shares": perf["shares"],
                "retention_rate": perf["retention_rate"],
            }
            updated += 1

    if updated > 0:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        print(f"   ✅ 已同步 {updated} 篇文章的性能数据到 metadata.json")
    else:
        print(f"   无新性能数据需要同步 ({date_str})")


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="头条性能数据录入")
    parser.add_argument("date", help="日期 (YYYY-MM-DD)")
    parser.add_argument("index", type=int, help="文章序号 (1-based)")
    parser.add_argument("--reads", type=int, default=0, help="阅读量")
    parser.add_argument("--comments", type=int, default=0, help="评论数")
    parser.add_argument("--likes", type=int, default=0, help="点赞数")
    parser.add_argument("--shares", type=int, default=0, help="分享数")
    parser.add_argument("--retention", type=float, default=0.0, help="完读率")
    parser.add_argument("--sync", action="store_true", help="同步到 metadata.json")

    args = parser.parse_args()

    log_article_performance(args.date, args.index,
                            reads=args.reads, comments=args.comments,
                            likes=args.likes, shares=args.shares,
                            retention_rate=args.retention)

    if args.sync:
        update_metadata_performance(args.date)
