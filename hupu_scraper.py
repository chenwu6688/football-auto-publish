#!/usr/bin/env python3
"""虎扑足球球队专区热门讨论采集 — 纯 HTTP 方案

使用 requests + BeautifulSoup 抓取虎扑球队专区帖子列表及详情。
无浏览器依赖，避免 WAF 拦截，速度快，CI 稳定。
"""

import re, json, time
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from bs4 import BeautifulSoup


class HupuScraper:
    SUB_FORUMS = {
        "曼联": "manutd",
        "阿森纳": "arsenal",
        "利物浦": "liverpool",
        "切尔西": "chelsea",
        "皇马": "realmadrid",
        "巴萨": "barcelona",
        "曼城": "mancity",
        "拜仁": "bayern",
    }

    BASE_URL = "https://bbs.hupu.com"
    REQUEST_DELAY = 2.0
    MAX_POSTS_PER_FORUM = 5
    MAX_DETAIL_THREADS = 10
    RECENCY_DAYS = 2
    REQUEST_TIMEOUT = 15

    def __init__(self, headless=True):
        # headless parameter kept for API compatibility, unused in HTTP mode
        pass

    def _http_get(self, url: str, referer: str = None) -> str:
        """Make HTTP GET request with proper headers for Hupu."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        }
        if referer:
            headers["Referer"] = referer
        req = Request(url, headers=headers)
        resp = urlopen(req, timeout=self.REQUEST_TIMEOUT)
        raw = resp.read()
        # Hupu uses utf-8
        return raw.decode("utf-8", errors="ignore")

    def _parse_hupu_time(self, time_str: str, today: datetime) -> datetime:
        """Parse Hupu time format (MM-DD HH:MM) into datetime."""
        if not time_str:
            return today - timedelta(days=99)
        time_str = time_str.strip()
        patterns = [
            (r"(\d+)-(\d+)\s+(\d+):(\d+)", lambda m: datetime(
                today.year, int(m.group(1)), int(m.group(2)),
                int(m.group(3)), int(m.group(4)))),
            (r"(\d+)分钟前", lambda m: today + timedelta(hours=12) - timedelta(minutes=int(m.group(1)))),
            (r"(\d+)小时前", lambda m: today + timedelta(hours=12) - timedelta(hours=int(m.group(1)))),
            (r"昨天\s*(\d+):(\d+)", lambda m: (today - timedelta(days=1)).replace(
                hour=int(m.group(1)), minute=int(m.group(2)))),
            (r"前天\s*(\d+):(\d+)", lambda m: (today - timedelta(days=2)).replace(
                hour=int(m.group(1)), minute=int(m.group(2)))),
        ]
        for pattern, fn in patterns:
            m = re.search(pattern, time_str)
            if m:
                try:
                    return fn(m)
                except Exception:
                    pass
        try:
            return datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
        try:
            return datetime.strptime(time_str, "%Y-%m-%d")
        except ValueError:
            pass
        return today - timedelta(days=99)

    def scrape_board(self, team_name: str) -> list:
        """Scrape posts from a single Hupu team board via HTTP."""
        slug = self.SUB_FORUMS.get(team_name, team_name)
        url = f"{self.BASE_URL}/{slug}"
        posts = []

        try:
            html = self._http_get(url, referer=self.BASE_URL)
            soup = BeautifulSoup(html, "html.parser")

            if team_name == list(self.SUB_FORUMS.keys())[0]:
                title_tag = soup.find("title")
                print(f"   [DEBUG HTTP] 页面标题: {title_tag.text if title_tag else 'N/A'}", flush=True)

            items = soup.select("li.bbs-sl-web-post-body")
            if not items and team_name == list(self.SUB_FORUMS.keys())[0]:
                body_text = soup.get_text()[:400]
                print(f"   [DEBUG HTTP] Body 前 400 字符: {body_text}", flush=True)

            for item in items[:30]:
                try:
                    # Title and link
                    title_el = item.select_one(".post-title a, a.p-title")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    href = title_el.get("href", "")
                    tid_match = re.search(r"/(\d+)\.html", href)
                    thread_id = tid_match.group(1) if tid_match else ""
                    if not title or not thread_id:
                        continue

                    # Reply/View count
                    datum_el = item.select_one(".post-datum")
                    datum_text = datum_el.get_text(strip=True) if datum_el else "0 / 0"
                    parts = datum_text.split("/")
                    reply_num = int(parts[0].strip()) if parts else 0

                    # Author
                    author_el = item.select_one(".post-auth a")
                    author = author_el.get_text(strip=True) if author_el else ""

                    # Time
                    time_el = item.select_one(".post-time")
                    time_str = time_el.get_text(strip=True) if time_el else ""

                    posts.append({
                        "team": team_name,
                        "thread_id": thread_id,
                        "title": title,
                        "reply_num": reply_num,
                        "author": author,
                        "last_time_str": time_str,
                    })
                except Exception:
                    continue

        except Exception as e:
            print(f"   ⚠️  [{team_name}] HTTP请求失败: {e}", flush=True)

        return posts

    def scrape_post_detail(self, thread_id: str) -> dict:
        """Scrape main post content, top replies, and images via HTTP."""
        url = f"{self.BASE_URL}/{thread_id}.html"
        result = {"thread_id": thread_id, "main_content": "", "replies": [], "images": []}

        try:
            html = self._http_get(url, referer=f"{self.BASE_URL}/manutd")
            soup = BeautifulSoup(html, "html.parser")

            # Extract main post content
            main_el = soup.select_one(
                "[class*='post-content_bbs-post-content'], .thread-content-detail"
            )
            if main_el:
                raw = main_el.get_text(separator="\n", strip=True)
                # Remove metadata prefix (author, level, time, location)
                parts = re.split(r"发布[在於].+?\s", raw, maxsplit=1)
                result["main_content"] = parts[1][:500] if len(parts) > 1 else raw[:500]

                # Extract images from main post
                for img in main_el.find_all("img"):
                    src = img.get("src") or img.get("data-src") or ""
                    if src and any(domain in src for domain in ["hoopchina", "hupu"]):
                        result["images"].append(src)

            # Extract replies - they are in the HTML as text blocks
            # Look for reply containers and extract user, content, likes
            reply_containers = soup.select(
                ".post-reply-list-container, [class*='post-reply'], li[class*='reply']"
            )

            # If not found with specific selectors, try broader search
            if not reply_containers:
                # Find all text after the main post
                body_text = soup.get_text()
                # Parse reply blocks from text
                reply_blocks = re.findall(
                    r"(\S{2,20})\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})[^亮]*亮了\((\d+)\)[^引]*(.{10,300}?)(?=\S{2,20}\s*\d{4}-\d{2}-\d{2}|$)",
                    body_text
                )
                for author, time_str, agree, content in reply_blocks[:15]:
                    content = re.sub(r"引用\s*@.*?发表的:.*?(?=\S{5})", "", content, flags=re.DOTALL)
                    content = re.sub(r"点灭.*?举报", "", content)
                    content = re.sub(r"^\d+楼\s*", "", content)
                    content = content.strip()[:300]
                    if len(content) >= 5:
                        result["replies"].append({
                            "content": content,
                            "agree_count": int(agree) if agree.isdigit() else 0,
                            "author": author[:30],
                            "time_str": time_str,
                        })
            else:
                reply_idx = 0
                for container in reply_containers[:60]:
                    if reply_idx >= 15:
                        break
                    try:
                        text = container.get_text(strip=True)
                        if not text or len(text) < 10:
                            continue

                        time_match = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", text)
                        author = text[:time_match.start()].strip() if time_match else ""
                        agree = 0
                        like_match = re.search(r"亮了\((\d+)\)", text)
                        if like_match:
                            agree = int(like_match.group(1))

                        content = ""
                        if time_match:
                            after_time = text[time_match.end():]
                            after_time = re.sub(r"^发布于\S+", "", after_time).strip()
                            after_time = re.sub(r"点灭.*?举报", "", after_time).strip()
                            after_time = re.sub(r"^\d+楼\s*", "", after_time).strip()
                            after_time = re.sub(r"引用\s*@.*?发表的:.*?(?=\S{10})", "", after_time, flags=re.DOTALL)
                            after_time = re.sub(r"亮了\(\d+\)回复.*$", "", after_time).strip()
                            content = after_time[:300]

                        if content and len(content) >= 5:
                            result["replies"].append({
                                "content": content,
                                "agree_count": agree,
                                "author": author[:30],
                                "time_str": time_match.group(1) if time_match else "",
                            })
                            reply_idx += 1
                    except Exception:
                        continue

        except Exception as e:
            pass  # individual post failure is non-fatal

        return result

    def collect_all(self, date_str: str) -> dict | None:
        """Full pipeline: scrape all sub-forums via HTTP, get top posts, fetch details."""
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
        print(f"   启动HTTP采集虎扑数据 ({len(self.SUB_FORUMS)} 个球队专区)...", flush=True)

        all_posts = []
        for team_name in self.SUB_FORUMS:
            try:
                posts = self.scrape_board(team_name)
                recent = []
                for post in posts:
                    dt = self._parse_hupu_time(post.get("last_time_str", ""), target_date)
                    cutoff = target_date - timedelta(days=self.RECENCY_DAYS)
                    if dt >= cutoff:
                        post["_parsed_time"] = dt
                        recent.append(post)
                all_posts.extend(recent)
                print(f"   [{team_name}] {len(posts)} 帖, 近期 {len(recent)} 帖", flush=True)
            except Exception as e:
                print(f"   ⚠️  [{team_name}] 异常: {e}", flush=True)
                continue
            time.sleep(self.REQUEST_DELAY)

        if not all_posts:
            print("   未采集到任何近期帖子", flush=True)
            return None

        all_posts.sort(key=lambda x: x.get("reply_num", 0), reverse=True)
        top_posts = all_posts[: self.MAX_DETAIL_THREADS]

        print(f"   共 {len(all_posts)} 条近期帖子，取 Top {len(top_posts)} 获取详情...", flush=True)

        detailed_posts = []
        for post in top_posts:
            tid = post["thread_id"]
            detail = self.scrape_post_detail(tid)
            detail["team"] = post["team"]
            detail["title"] = post["title"]
            detail["reply_num"] = post["reply_num"]
            detail["author"] = post.get("author", "")
            detail["last_time_str"] = post.get("last_time_str", "")
            detail["top_replies"] = sorted(
                detail.get("replies", []),
                key=lambda r: r.get("agree_count", 0),
                reverse=True,
            )[:5]
            detailed_posts.append(detail)
            time.sleep(self.REQUEST_DELAY)

        valid = [p for p in detailed_posts if p.get("main_content") or p.get("top_replies")]
        print(f"   有效帖子详情: {len(valid)}/{len(detailed_posts)}", flush=True)

        return {
            "date": date_str,
            "sub_forums_scraped": len(self.SUB_FORUMS),
            "raw_posts": valid,
        }


if __name__ == "__main__":
    scraper = HupuScraper(headless=True)
    today = datetime.now().strftime("%Y-%m-%d")
    data = scraper.collect_all(today)
    if data:
        print(f"\n采集成功: {len(data['raw_posts'])} 条帖子")
        for i, p in enumerate(data["raw_posts"][:5]):
            print(f"\n{i+1}. [{p['team']}] {p['title'][:60]} ({p['reply_num']}回复)")
            print(f"   主帖: {p.get('main_content', '')[:100]}")
            for r in p.get("top_replies", [])[:2]:
                print(f"   👍{r['agree_count']} {r['content'][:80]}")
    else:
        print("采集失败或无数据")
