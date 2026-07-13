#!/usr/bin/env python3
"""头条号文章效果数据录入工具

用法:
  # 录入单篇文章效果
  python performance_logger_wrapper.py 2026-07-06 --all

  # 手动录入单篇
  python performance_logger_wrapper.py 2026-07-06 --article 1 --reads 5200 --comments 34 --likes 128 --shares 45 --retention 0.62

  # 交互式录入（逐个文章让你填）
  python performance_logger_wrapper.py 2026-07-06 --interactive

  # 查看某天的录入状态（哪些已填哪些还没填）
  python performance_logger_wrapper.py 2026-07-06 --status

  # 生成本周汇总报告
  python performance_logger_wrapper.py --report
"""

import json, os, sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "output"))
PERF_LOG_PATH = OUTPUT_DIR / "performance_log.json"


def load_perf():
    if PERF_LOG_PATH.exists():
        return json.loads(PERF_LOG_PATH.read_text())
    return {"articles": {}, "updated_at": None}


def save_perf(data):
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    PERF_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PERF_LOG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def get_articles_for_date(date_str):
    meta_path = OUTPUT_DIR / date_str / "metadata.json"
    if not meta_path.exists():
        print(f"  ❌ {date_str} 无 metadata.json")
        return []
    meta = json.loads(meta_path.read_text())
    return meta.get("articles", [])


def show_status(date_str):
    """Show which articles have performance data logged."""
    articles = get_articles_for_date(date_str)
    if not articles:
        return
    perf = load_perf()
    print(f"\n📊 {date_str} 文章效果录入状态")
    print(f"{'序号':>4} {'标题':<40} {'阅读':>6} {'评论':>4} {'点赞':>4} {'分享':>4} {'完读率':>6}")
    print("-" * 75)
    filled = 0
    for a in articles:
        idx = a.get("index", 0)
        title = a.get("title", "?")[:38]
        key = f"{date_str}/article-{idx}"
        if key in perf.get("articles", {}):
            p = perf["articles"][key]
            reads = p.get("reads", 0)
            comments = p.get("comments", 0)
            likes = p.get("likes", 0)
            shares = p.get("shares", 0)
            retention = p.get("retention_rate", 0)
            status = "✅"
            filled += 1
        else:
            reads = comments = likes = shares = 0
            retention = 0
            status = "⬜"
        ret_str = f"{retention:.0%}" if retention else "-"
        print(f"{status} {idx:>3} {title:<40} {reads:>6} {comments:>4} {likes:>4} {shares:>4} {ret_str:>6}")
    print(f"\n  已录入: {filled}/{len(articles)} 篇")


def interactive_input(date_str):
    """交互式录入：逐个文章让你填数据。"""
    articles = get_articles_for_date(date_str)
    if not articles:
        print(f"  ❌ {date_str} 没有文章需要录入")
        return
    perf = load_perf()
    print(f"\n📝 交互录入 — {date_str} ({len(articles)} 篇)")
    for a in articles:
        idx = a.get("index", 0)
        title = a.get("title", "?")[:40]
        key = f"{date_str}/article-{idx}"
        existing = perf.get("articles", {}).get(key, {})
        default_reads = existing.get("reads", "")
        default_comments = existing.get("comments", "")
        default_likes = existing.get("likes", "")
        default_shares = existing.get("shares", "")
        default_retention = existing.get("retention_rate", "")

        print(f"\n--- 第 {idx} 篇: {title} ---")
        try:
            reads = input(f"  阅读量 [{default_reads}]: ") or default_reads
            comments = input(f"  评论数 [{default_comments}]: ") or default_comments
            likes = input(f"  点赞数 [{default_likes}]: ") or default_likes
            shares = input(f"  分享数 [{default_shares}]: ") or default_shares
            retention = input(f"  完读率(0.00-1.00) [{default_retention}]: ") or default_retention
        except (EOFError, KeyboardInterrupt):
            print("\n  中断")
            break

        reads = int(reads) if reads else 0
        comments = int(comments) if comments else 0
        likes = int(likes) if likes else 0
        shares = int(shares) if shares else 0
        retention = float(retention) if retention else 0.0

        perf.setdefault("articles", {})[key] = {
            "date": date_str, "index": idx,
            "reads": reads, "comments": comments,
            "likes": likes, "shares": shares,
            "retention_rate": retention,
            "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        save_perf(perf)
        print(f"  ✅ 已保存")

    print(f"\n  ✅ {date_str} 录入完成！")
    print(f"  下次查看: python {__file__} {date_str} --status")


def generate_report():
    """生成阅读量趋势报告。"""
    perf = load_perf()
    articles_data = perf.get("articles", {})
    if not articles_data:
        print("  ❌ 还没有录入任何效果数据")
        print(f"  请先运行: python {__file__} YYYY-MM-DD --interactive")
        return

    # Group by date
    by_date = {}
    for key, p in articles_data.items():
        d = p.get("date", "?")
        by_date.setdefault(d, []).append(p)

    print("\n" + "=" * 60)
    print("📈 足球自媒体 — 文章效果汇总报告")
    print("=" * 60)

    all_reads = []
    for date_str in sorted(by_date.keys()):
        articles = by_date[date_str]
        total_reads = sum(a.get("reads", 0) for a in articles)
        total_comments = sum(a.get("comments", 0) for a in articles)
        total_likes = sum(a.get("likes", 0) for a in articles)
        total_shares = sum(a.get("shares", 0) for a in articles)
        avg_retention = sum(a.get("retention_rate", 0) for a in articles) / max(len(articles), 1)
        all_reads.extend(a.get("reads", 0) for a in articles)

        print(f"\n  📅 {date_str} ({len(articles)} 篇)")
        print(f"     总阅读: {total_reads} | 均阅读: {total_reads//max(len(articles),1)}")
        print(f"     总评论: {total_comments} | 总点赞: {total_likes} | 总分享: {total_shares}")
        print(f"     平均完读率: {avg_retention:.1%}")

    if all_reads:
        avg_reads = sum(all_reads) / len(all_reads)
        max_reads = max(all_reads)
        print(f"\n  📊 全局统计")
        print(f"     文章总数: {len(all_reads)}")
        print(f"     平均阅读: {avg_reads:.0f}")
        print(f"     最高阅读: {max_reads}")
    print()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="头条号效果数据录入")
    parser.add_argument("date", nargs="?", help="日期 (YYYY-MM-DD)")
    parser.add_argument("--interactive", action="store_true", help="交互式录入")
    parser.add_argument("--status", action="store_true", help="查看录入状态")
    parser.add_argument("--report", action="store_true", help="生成汇总报告")
    parser.add_argument("--all", action="store_true", help="录入某天全部文章(需跟date)")

    args = parser.parse_args()

    if args.report:
        generate_report()
    elif args.date and args.interactive:
        interactive_input(args.date)
    elif args.date and args.status:
        show_status(args.date)
    elif args.date and args.all:
        articles = get_articles_for_date(args.date)
        if articles:
            print(f"\n📝 快速录入 — {args.date} ({len(articles)} 篇)")
            perf = load_perf()
            for a in articles:
                idx = a.get("index", 0)
                title = a.get("title", "?")[:40]
                key = f"{args.date}/article-{idx}"
                print(f"\n--- {idx}. {title} ---")
                try:
                    reads = input(f"  阅读量: ").strip() or "0"
                    comments = input(f"  评论数: ").strip() or "0"
                except (EOFError, KeyboardInterrupt):
                    break
                perf.setdefault("articles", {})[key] = {
                    "date": args.date, "index": idx,
                    "reads": int(reads), "comments": int(comments),
                    "likes": 0, "shares": 0, "retention_rate": 0.0,
                    "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                save_perf(perf)
            print(f"\n  ✅ {args.date} 录入完成！")
    else:
        parser.print_help()
