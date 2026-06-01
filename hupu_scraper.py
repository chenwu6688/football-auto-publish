#!/usr/bin/env python3
"""虎扑足球球队专区热门讨论采集 — Playwright 浏览器自动化

替代百度贴吧方案。虎扑无强反爬机制，可直接 headless 采集。
"""

import time, re, json
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright


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
    MAX_DETAIL_THREADS = 15
    RECENCY_DAYS = 2

    def __init__(self, headless=True):
        self.headless = headless

    def _launch_browser(self):
        launch_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1366,768",
        ]
        p = sync_playwright().start()
        browser = p.chromium.launch(headless=self.headless, args=launch_args)
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()
        return p, browser, context, page

    def _parse_hupu_time(self, time_str: str, today: datetime) -> datetime:
        """Parse Hupu time format (MM-DD HH:MM) into datetime."""
        if not time_str:
            return today - timedelta(days=99)
        time_str = time_str.strip()
        patterns = [
            (r"(\d+)-(\d+)\s+(\d+):(\d+)", lambda m: datetime(
                today.year, int(m.group(1)), int(m.group(2)),
                int(m.group(3)), int(m.group(4)))),
            (r"(\d+)分钟前", lambda m: today - timedelta(minutes=int(m.group(1)))),
            (r"(\d+)小时前", lambda m: today - timedelta(hours=int(m.group(1)))),
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
        # Fallback: if time looks like a date, parse it
        try:
            return datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
        try:
            return datetime.strptime(time_str, "%Y-%m-%d")
        except ValueError:
            pass
        return today - timedelta(days=99)

    def scrape_board(self, page, team_name: str) -> list:
        """Scrape posts from a single Hupu team board."""
        slug = self.SUB_FORUMS.get(team_name, team_name)
        url = f"{self.BASE_URL}/{slug}"
        posts = []

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)

            if team_name == list(self.SUB_FORUMS.keys())[0]:
                print(f"   [DEBUG] 页面标题: {page.title()} | URL: {page.url[:100]}")

            items = page.locator("li.bbs-sl-web-post-body")
            count = items.count()

            if count == 0 and team_name == list(self.SUB_FORUMS.keys())[0]:
                try:
                    body_html = page.locator("body").inner_html()
                    print(f"   [DEBUG] Body HTML 前 400 字符: {body_html[:400]}")
                except Exception:
                    pass

            for i in range(min(count, 30)):
                try:
                    el = items.nth(i)

                    # Title and link
                    title_el = el.locator(".post-title a, a.p-title").first
                    title = (title_el.text_content() or "").strip()
                    href = (title_el.get_attribute("href") or "")
                    tid_match = re.search(r"/(\d+)\.html", href)
                    thread_id = tid_match.group(1) if tid_match else ""

                    # Reply/View count: "5 / 120"
                    datum_el = el.locator(".post-datum")
                    datum_text = (datum_el.text_content() or "0 / 0").strip() if datum_el.count() > 0 else "0 / 0"
                    parts = datum_text.split("/")
                    reply_num = int(parts[0].strip()) if len(parts) > 0 else 0

                    # Author
                    author_el = el.locator(".post-auth a")
                    author = (author_el.text_content() or "").strip() if author_el.count() > 0 else ""

                    # Time
                    time_el = el.locator(".post-time")
                    time_str = (time_el.text_content() or "").strip() if time_el.count() > 0 else ""

                    if not title or not thread_id:
                        continue

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
            print(f"   ⚠️  [{team_name}] 爬取失败: {e}")

        return posts

    def scrape_post_detail(self, page, thread_id: str) -> dict:
        """Scrape main post content and top replies for a thread."""
        url = f"{self.BASE_URL}/{thread_id}.html"
        result = {"thread_id": thread_id, "main_content": "", "replies": []}

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)

            # Extract main post content
            try:
                main_el = page.locator("[class*='post-content_bbs-post-content']").first
                if main_el.count() > 0 and main_el.is_visible(timeout=3000):
                    raw = (main_el.text_content() or "").strip()
                    # Remove the metadata prefix (author, level, time, location, etc.)
                    # The actual post content usually comes after the location info
                    parts = re.split(r"发布[在於].+?\s", raw, maxsplit=1)
                    if len(parts) > 1:
                        result["main_content"] = parts[1][:500]
                    else:
                        result["main_content"] = raw[:500]
            except Exception:
                pass

            # Extract replies
            try:
                containers = page.locator(".post-reply-list-container")
                reply_idx = 0
                for ci in range(min(containers.count(), 60)):
                    if reply_idx >= 15:
                        break
                    try:
                        text = containers.nth(ci).text_content().strip()
                        if not text or len(text) < 10:
                            continue

                        # Parse author: first word before timestamp
                        author = ""
                        time_str = ""
                        # Extract time: YYYY-MM-DD HH:MM:SS
                        time_match = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", text)
                        if time_match:
                            time_str = time_match.group(1)
                            # Author is text before the time
                            author = text[:time_match.start()].strip()

                        # Extract like count: 亮了(X)
                        agree = 0
                        like_match = re.search(r"亮了\((\d+)\)", text)
                        if like_match:
                            agree = int(like_match.group(1))

                        # Extract reply content: between time+location and "亮了" or end
                        content = ""
                        if time_match:
                            after_time = text[time_match.end():]
                            # Remove "发布于XX" location info
                            after_time = re.sub(r"^发布于\S+", "", after_time).strip()
                            # Remove "点灭只看此人举报"
                            after_time = re.sub(r"点灭.*?举报", "", after_time).strip()
                            # Remove floor number and "引用" blocks
                            after_time = re.sub(r"^\d+楼\s*", "", after_time).strip()
                            # Remove quoted text
                            after_time = re.sub(r"引用\s*@.*?发表的:.*?(?=\S{10})", "", after_time, flags=re.DOTALL)
                            # Remove 亮了(X)回复
                            after_time = re.sub(r"亮了\(\d+\)回复.*$", "", after_time).strip()
                            content = after_time[:300]

                        if content and len(content) >= 5:
                            result["replies"].append({
                                "content": content,
                                "agree_count": agree,
                                "author": author[:30],
                                "time_str": time_str,
                            })
                            reply_idx += 1
                    except Exception:
                        continue
            except Exception:
                pass

        except Exception as e:
            pass

        return result

    def collect_all(self, date_str: str) -> dict | None:
        """Full pipeline: scrape all sub-forums, get top posts, fetch details."""
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
        print(f"   启动浏览器采集虎扑数据 ({len(self.SUB_FORUMS)} 个球队专区)...")

        all_posts = []
        p = browser = context = None

        try:
            p, browser, context, page = self._launch_browser()

            for team_name in self.SUB_FORUMS:
                try:
                    posts = self.scrape_board(page, team_name)
                    # Filter by recency
                    recent = []
                    for post in posts:
                        dt = self._parse_hupu_time(
                            post.get("last_time_str", ""), target_date
                        )
                        cutoff = target_date - timedelta(days=self.RECENCY_DAYS)
                        if dt >= cutoff:
                            post["_parsed_time"] = dt
                            recent.append(post)
                    all_posts.extend(recent)
                    print(f"   [{team_name}] {len(posts)} 帖, 近期 {len(recent)} 帖")
                except Exception as e:
                    print(f"   ⚠️  [{team_name}] 异常: {e}")
                    continue
                time.sleep(self.REQUEST_DELAY)

            if not all_posts:
                print("   未采集到任何近期帖子")
                return None

            # Sort by reply_num descending, take top N
            all_posts.sort(key=lambda x: x.get("reply_num", 0), reverse=True)
            top_posts = all_posts[: self.MAX_DETAIL_THREADS]

            print(f"   共 {len(all_posts)} 条近期帖子，取 Top {len(top_posts)} 获取详情...")

            # Scrape post details for top threads
            detailed_posts = []
            for post in top_posts:
                tid = post["thread_id"]
                detail = self.scrape_post_detail(page, tid)
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

            # Filter out posts with no usable content
            valid = [
                p
                for p in detailed_posts
                if p.get("main_content") or p.get("top_replies")
            ]
            print(f"   有效帖子详情: {len(valid)}/{len(detailed_posts)}")

            return {
                "date": date_str,
                "sub_forums_scraped": len(self.SUB_FORUMS),
                "raw_posts": valid,
            }

        except Exception as e:
            print(f"   虎扑采集异常: {e}")
            return None
        finally:
            try:
                if browser:
                    browser.close()
                if p:
                    p.stop()
            except Exception:
                pass


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
