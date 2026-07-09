#!/usr/bin/env python3
"""爬虫健康检测脚本 — 在管线运行前验证爬虫状态。

检测所有爬虫源（直播吧、懂球帝）的可访问性、HTML结构、内容解析。
输出结构化报告，用于：① 管线健康检查 ② 结构改版预警 ③ 自动降级决策。

Usage:
    python3 scripts/health_check_scraper.py
    python3 scripts/health_check_scraper.py --format json
    python3 scripts/health_check_scraper.py --alert --save-html /tmp/diag/
"""

import os, sys, json, re, argparse
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from media_scraper import SportsScraper
from bs4 import BeautifulSoup
import requests


def check_zhibo8(scraper, do_save_html=False, save_dir=None):
    """检测直播吧各功能点。返回 (status, checks_dict, errors_list)."""
    checks = {}
    errors = []

    # ── 1. 可访问性 ──
    try:
        html = scraper._http_get("https://www.zhibo8.cc/", check_block=True)
        is_ok = "直播吧" in html or "zhibo8" in html
        checks["accessible"] = is_ok
        if not is_ok:
            errors.append("zhibo8.cc 首页内容异常（不包含'直播吧'）")
    except Exception as e:
        checks["accessible"] = False
        errors.append(f"zhibo8.cc 不可达: {e}")
        return "broken", checks, errors

    # ── 2. 编码正确（UTF-8 decoded 后不应有乱码特征） ──
    garbled_markers = ["æ", "â", "ç", "ï", "éè"]
    garbled_count = sum(html.count(m) for m in garbled_markers)
    checks["encoding_ok"] = garbled_count < 50
    if not checks["encoding_ok"]:
        errors.append(f"编码异常: 发现 {garbled_count} 个乱码标记")

    # ── 3. 赛程HTML结构 ──
    soup = BeautifulSoup(html, "html.parser")
    schedule = soup.select_one(".schedule")
    checks["schedule_exists"] = schedule is not None
    if not checks["schedule_exists"]:
        errors.append(".schedule 选择器未匹配到任何元素")

    # ── 4. 足球比赛条目 ──
    football_items = []
    if schedule:
        football_items = schedule.find_all("li", attrs={"data-type": re.compile(r"football", re.I)})
    checks["football_items_count"] = len(football_items)
    if not football_items:
        errors.append(f"未找到足球比赛条目 (li[data-type='football']=0)")

    # ── 5. 队名span ──
    team_names_found = 0
    for li in football_items[:5]:
        teams_el = li.select_one("._teams")
        if teams_el and teams_el.get_text(strip=True):
            team_names_found += 1
    checks["team_names_found"] = team_names_found
    if team_names_found == 0 and football_items:
        errors.append("._teams 选择器未提取到队名")

    # ── 6. 联赛名span ──
    league_names_found = 0
    for li in football_items[:5]:
        league_el = li.select_one("._league")
        if league_el and league_el.get_text(strip=True):
            league_names_found += 1
    checks["league_names_found"] = league_names_found

    # ── 7. 比赛链接（a[href*="match"]） ──
    match_links = soup.find_all("a", href=re.compile(r"match", re.I))
    checks["match_links_found"] = len(match_links)
    if not match_links:
        errors.append("页面中无 a[href*='match'] 链接")

    # ── 8. 新闻战报链接（news.*match 或 native.htm） ──
    report_links = soup.find_all("a", href=re.compile(r"news\.zhibo8.*match", re.I))
    native_links = soup.find_all("a", href=re.compile(r"native\.htm", re.I))
    checks["report_links_found"] = len(report_links)
    checks["native_links_found"] = len(native_links)

    # ── 9. match解析测试：直接用 _parse_zhibo8_homepage 验证 ──
    parsed_matches = len(scraper._parse_zhibo8_homepage(html, datetime.now().strftime("%Y-%m-%d")))
    sample_match = ""
    if parsed_matches > 0:
        # 从新闻战报链接中取一个样本标题
        for a in soup.find_all("a", href=re.compile(r"news\.zhibo8.*match", re.I)):
            text = a.get_text(strip=True)
            if text and len(text) > 10:
                sample_match = text[:50]
                break
    checks["parsed_matches"] = parsed_matches
    checks["sample_match"] = sample_match
    if parsed_matches == 0:
        errors.append("_parse_match_from_link_text 无法解析任何比赛链接")

    # ── 10. 新闻页 ──
    try:
        news_articles = scraper.scrape_football_news(date_str=None, max_articles=5)
        checks["news_articles_found"] = len(news_articles)
        if news_articles:
            # 尝试取一篇正文确认可读
            content = scraper.scrape_zhibo8_article_content(news_articles[0]["url"])
            checks["news_content_ok"] = bool(content and len(content) > 100)
        else:
            checks["news_content_ok"] = False
            errors.append("news.zhibo8.com/zuqiu/ 未找到足球新闻文章")
    except Exception as e:
        checks["news_articles_found"] = 0
        checks["news_content_ok"] = False
        errors.append(f"新闻页异常: {e}")

    # ── 保存HTML快照 ──
    if do_save_html and save_dir:
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        Path(save_dir / f"zhibo8_homepage_{ts}.html").write_text(html, encoding="utf-8")
        checks["html_snapshot_saved"] = True

    # ── 总体状态 ──
    fatal = [
        not checks.get("accessible", True),
        not checks.get("encoding_ok", True),
        checks.get("football_items_count", 1) == 0 and checks.get("parsed_matches", 1) == 0,
    ]
    degraded = [
        checks.get("football_items_count", 1) == 0,
        checks.get("parsed_matches", 1) == 0,
        checks.get("news_articles_found", 5) < 3,
    ]
    if any(fatal):
        status = "broken"
    elif any(degraded):
        status = "degraded"
    else:
        status = "healthy"

    return status, checks, errors


def check_dongqiudi():
    """检测懂球帝可访问性和文章。返回 (status, checks_dict, errors_list)."""
    checks = {}
    errors = []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        resp = requests.get("https://www.dongqiudi.com/", headers=headers, timeout=15)
        checks["accessible"] = resp.status_code == 200
        if not checks["accessible"]:
            errors.append(f"HTTP {resp.status_code}")
            return "broken", checks, errors

        blocked = any(ind in resp.text for ind in ["验证", "访问频率", "captcha", "5秒后"])
        checks["not_blocked"] = not blocked
        if blocked:
            errors.append("触发反爬验证")

        soup = BeautifulSoup(resp.text, "html.parser")
        # 找文章标题元素
        titles = soup.find_all(["h1", "h2", "h3", "a"],
                                string=lambda s: s and len(s.strip()) > 20)
        checks["articles_found"] = min(len(titles), 50)
        if len(titles) < 3:
            errors.append(f"文章标题不足 ({len(titles)} 个)")

        sample = titles[0].get_text(strip=True)[:50] if titles else ""
        checks["sample_title"] = sample

    except Exception as e:
        checks["accessible"] = False
        errors.append(f"懂球帝异常: {e}")
        return "broken", checks, errors

    fatal = [not checks.get("accessible", True), checks.get("articles_found", 5) < 3]
    status = "broken" if any(fatal) else "healthy"
    return status, checks, errors


def decide_recommendation(z_status, d_status, z_checks):
    """根据各源健康状态给出管线建议。"""
    if z_status == "healthy" or (z_status == "degraded" and z_checks.get("news_articles_found", 0) >= 3):
        return "proceed"
    elif z_status == "degraded":
        return "fallback_news"
    elif d_status == "healthy":
        return "fallback_dongqiudi"
    else:
        return "abort"


def format_text(report):
    """格式化为人类可读的文本输出。"""
    emoji = {"healthy": "✅", "degraded": "⚠️", "broken": "❌"}
    lines = []
    lines.append(f"\n{'='*55}")
    lines.append(f"  爬虫健康检测报告 — {report['timestamp']}")
    lines.append(f"  整体状态: {emoji.get(report['overall'], '?')} {report['overall']}")
    lines.append(f"  建议: {report['recommendation']}")
    lines.append(f"{'='*55}")

    for src_name in ["zhibo8", "dongqiudi"]:
        src = report["sources"].get(src_name)
        if not src:
            continue
        s = emoji.get(src["status"], "?")
        lines.append(f"\n{s} {src_name}:")
        for k, v in src.get("checks", {}).items():
            if isinstance(v, bool):
                lines.append(f"    {k}: {'✅' if v else '❌'}")
            else:
                lines.append(f"    {k}: {v}")
        if src.get("errors"):
            for e in src["errors"]:
                lines.append(f"    🚨 {e}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="爬虫健康检测")
    parser.add_argument("--format", choices=["text", "json"], default="text",
                        help="输出格式")
    parser.add_argument("--output", type=str, default=None,
                        help="写入文件路径")
    parser.add_argument("--alert", action="store_true",
                        help="失败时发送 WxPusher 通知")
    parser.add_argument("--save-html", type=str, default=None,
                        help="失败时保存HTML快照到目录")
    args = parser.parse_args()

    scraper = SportsScraper()
    save_dir = Path(args.save_html) if args.save_html else None

    # 检测
    z_status, z_checks, z_errors = check_zhibo8(
        scraper, do_save_html=bool(args.save_html), save_dir=save_dir)
    d_status, d_checks, d_errors = check_dongqiudi()

    recommendation = decide_recommendation(z_status, d_status, z_checks)

    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "overall": "healthy" if (z_status == "healthy" and d_status == "healthy") else
                   "degraded" if (z_status in ("healthy", "degraded") or d_status == "healthy") else
                   "broken",
        "recommendation": recommendation,
        "sources": {
            "zhibo8": {
                "status": z_status,
                "checks": z_checks,
                "errors": z_errors,
            },
            "dongqiudi": {
                "status": d_status,
                "checks": d_checks,
                "errors": d_errors,
            },
        },
    }

    # 输出
    if args.format == "json":
        output = json.dumps(report, ensure_ascii=False, indent=2)
        # JSON 模式：只输 JSON 到 stdout，日志到 stderr
        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
            print(f"报告已写入: {args.output}", file=sys.stderr)
        else:
            print(output)
    else:
        output = format_text(report)
        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
            print(f"报告已写入: {args.output}")
        else:
            print(output)

    # 告警
    is_broken = report["overall"] == "broken"
    is_degraded = report["overall"] == "degraded"
    if args.alert and (is_broken or is_degraded):
        try:
            from constants import WXPUSHER_APPTOKEN, WXPUSHER_UID
            level = "🚨 爬虫故障" if is_broken else "⚠️ 爬虫降级"
            details = []
            for src_name, src in report["sources"].items():
                if src["errors"]:
                    details.append(f"{src_name}: {'; '.join(src['errors'][:3])}")
            body = f"{report['timestamp']}\n" + "\n".join(details)
            # send wxpusher with raw request (same pattern as scripts/send_wxpusher.py)
            import requests as req
            req.post("https://wxpusher.zjiecode.com/api/send/message",
                     json={"appToken": WXPUSHER_APPTOKEN,
                           "content": f"{level}\n\n{body[:500]}",
                           "contentType": 1,
                           "uids": [WXPUSHER_UID]},
                     timeout=10)
        except Exception as e:
            print(f"  ⚠️ WxPusher 通知失败: {e}")

    # 退出码
    if is_broken:
        sys.exit(2)
    elif is_degraded:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
