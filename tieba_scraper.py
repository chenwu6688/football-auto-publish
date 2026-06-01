#!/usr/bin/env python3
"""百度贴吧足球球队吧热门讨论采集 — Playwright 浏览器自动化

反爬策略：贴吧对标准 HTTP 库直接返回 403 验证码，必须使用真实浏览器环境。
"""

import time, re, json
from datetime import datetime, timedelta
from urllib.parse import quote
from playwright.sync_api import sync_playwright


class TiebaScraper:
    SUB_FORUMS = {
        "曼联": "曼联",
        "阿森纳": "阿森纳",
        "利物浦": "利物浦",
        "切尔西": "切尔西",
        "皇马": "皇家马德里",
        "巴萨": "巴塞罗那",
        "巴黎": "巴黎圣日耳曼",
        "拜仁": "拜仁慕尼黑",
    }

    BASE_URL = "https://tieba.baidu.com"
    REQUEST_DELAY = 3.0
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
        ]
        p = sync_playwright().start()
        browser = p.chromium.launch(headless=self.headless, args=launch_args)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        return p, browser, context, page

    def _parse_last_time(self, time_str: str, today: datetime) -> datetime:
        """Parse Chinese relative time strings into approximate datetime."""
        if not time_str:
            return today - timedelta(days=99)
        now = today + timedelta(hours=12)
        patterns = [
            (r"(\d+)分钟前", lambda m: now - timedelta(minutes=int(m))),
            (r"(\d+)小时前", lambda m: now - timedelta(hours=int(m))),
            (r"昨天\s*(\d+):(\d+)", lambda m: (today - timedelta(days=1)).replace(
                hour=int(m.group(1)), minute=int(m.group(2)))),
            (r"前天\s*(\d+):(\d+)", lambda m: (today - timedelta(days=2)).replace(
                hour=int(m.group(1)), minute=int(m.group(2)))),
            (r"(\d+)-(\d+)\s+(\d+):(\d+)", lambda m: datetime(today.year, int(m.group(1)),
                int(m.group(2)), int(m.group(3)), int(m.group(4)))),
            (r"(\d+)月(\d+)日", lambda m: datetime(today.year, int(m.group(1)),
                int(m.group(2)))),
        ]
        for pattern, fn in patterns:
            m = re.search(pattern, time_str)
            if m:
                try:
                    return fn(m)
                except Exception:
                    pass
        return today - timedelta(days=99)

    def scrape_sub_forum(self, page, team_name: str) -> list:
        """Scrape hot posts from a single Tieba sub-forum."""
        kw = self.SUB_FORUMS.get(team_name, team_name)
        url = f"{self.BASE_URL}/f?kw={quote(kw)}&ie=utf-8"
        posts = []

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)

            # Debug: log page state on first sub-forum
            if team_name == list(self.SUB_FORUMS.keys())[0]:
                page_title = page.title()
                page_url = page.url
                print(f"   [DEBUG] 页面标题: {page_title} | URL: {page_url[:100]}")

            # Try multiple selector strategies
            thread_items = page.locator("li.j_thread_list[data-field]")
            count = thread_items.count()

            # Fallback: try alternative selectors
            if count == 0:
                thread_items = page.locator("li[data-field]")
                count = thread_items.count()
            if count == 0:
                thread_items = page.locator("[data-field]")
                count = thread_items.count()

            # Debug on first sub-forum: dump some HTML if no items found
            if count == 0 and team_name == list(self.SUB_FORUMS.keys())[0]:
                try:
                    body_html = page.locator("body").inner_html()
                    print(f"   [DEBUG] Body HTML 前 800 字符: {body_html[:800]}")
                except Exception:
                    pass

            for i in range(min(count, 20)):
                try:
                    el = thread_items.nth(i)
                    raw = el.get_attribute("data-field") or ""
                    data = json.loads(raw) if raw else {}
                    if not data.get("id"):
                        continue

                    title_el = el.locator(".j_th_tit, a.j_th_tit, a[class*='tit']").first
                    title = ""
                    try:
                        title = (title_el.get_attribute("title") or title_el.text_content() or "").strip()
                    except Exception:
                        pass

                    if not title:
                        continue

                    posts.append({
                        "team": team_name,
                        "thread_id": str(data.get("id", "")),
                        "title": title,
                        "reply_num": data.get("reply_num", 0),
                        "author": (data.get("author") or {}).get("name_show",
                                (data.get("author") or {}).get("name", "")),
                        "last_time_str": data.get("last_time_str", ""),
                    })
                except Exception:
                    continue

        except Exception as e:
            print(f"   ⚠️  [{team_name}吧] 爬取失败: {e}")

        return posts

    def scrape_post_detail(self, page, thread_id: str) -> dict:
        """Scrape main post content and top replies for a thread."""
        url = f"{self.BASE_URL}/p/{thread_id}"
        result = {"thread_id": thread_id, "main_content": "", "replies": []}

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)

            # Extract main post (first floor)
            try:
                main_el = page.locator(".d_post_content_firstfloor, .j_d_post_content").first
                if main_el.is_visible(timeout=3000):
                    result["main_content"] = (main_el.text_content() or "").strip()[:500]
            except Exception:
                pass

            # Extract replies with agree counts
            try:
                floor_items = page.locator(".l_post.j_l_post").all()
                for fi in floor_items[1:11]:  # skip first floor (main post), max 10 replies
                    try:
                        content_el = fi.locator(".d_post_content").first
                        content = (content_el.text_content() or "").strip()[:300]
                        if not content:
                            continue

                        # Extract agree count
                        agree = 0
                        try:
                            agree_el = fi.locator(".ilike_icon .ilike_count, [class*='agree']")
                            if agree_el.count() > 0:
                                agree_text = (agree_el.first.text_content() or "").strip()
                                agree = int(re.sub(r"\D", "", agree_text) or 0)
                        except Exception:
                            pass

                        # Extract author
                        author = ""
                        try:
                            author_el = fi.locator(".d_name a, .p_author_name")
                            if author_el.count() > 0:
                                author = (author_el.first.text_content() or "").strip()
                        except Exception:
                            pass

                        result["replies"].append({
                            "content": content,
                            "agree_count": agree,
                            "author": author,
                        })
                    except Exception:
                        continue
            except Exception:
                pass

        except Exception as e:
            pass  # individual post detail failure is non-fatal

        return result

    def collect_all(self, date_str: str) -> dict | None:
        """Full pipeline: scrape all sub-forums, get top posts, fetch details."""
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
        print(f"   启动浏览器采集贴吧数据 ({len(self.SUB_FORUMS)} 个球队吧)...")

        all_posts = []
        p = browser = context = None

        try:
            p, browser, context, page = self._launch_browser()

            for team_name in self.SUB_FORUMS:
                try:
                    posts = self.scrape_sub_forum(page, team_name)
                    # Filter by recency
                    recent = []
                    for post in posts:
                        dt = self._parse_last_time(post.get("last_time_str", ""), target_date)
                        cutoff = target_date - timedelta(days=self.RECENCY_DAYS)
                        if dt >= cutoff:
                            post["_parsed_time"] = dt
                            recent.append(post)
                    all_posts.extend(recent)
                    print(f"   [{team_name}吧] {len(posts)} 帖, 近期 {len(recent)} 帖")
                except Exception as e:
                    print(f"   ⚠️  [{team_name}吧] 异常: {e}")
                    continue
                time.sleep(self.REQUEST_DELAY)

            if not all_posts:
                print("   未采集到任何近期帖子")
                return None

            # Sort by reply_num descending, take top N
            all_posts.sort(key=lambda x: x.get("reply_num", 0), reverse=True)
            top_posts = all_posts[:self.MAX_DETAIL_THREADS]

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
            valid = [p for p in detailed_posts
                     if p.get("main_content") or p.get("top_replies")]
            print(f"   有效帖子详情: {len(valid)}/{len(detailed_posts)}")

            return {
                "date": date_str,
                "sub_forums_scraped": len(self.SUB_FORUMS),
                "raw_posts": valid,
            }

        except Exception as e:
            print(f"   贴吧采集异常: {e}")
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
    scraper = TiebaScraper(headless=True)
    today = datetime.now().strftime("%Y-%m-%d")
    data = scraper.collect_all(today)
    if data:
        print(f"\n采集成功: {len(data['raw_posts'])} 条帖子")
        for i, p in enumerate(data["raw_posts"][:5]):
            print(f"\n{i+1}. [{p['team']}吧] {p['title'][:60]} ({p['reply_num']}回复)")
            print(f"   主帖: {p.get('main_content', '')[:100]}")
            for r in p.get("top_replies", [])[:2]:
                print(f"   👍{r['agree_count']} {r['content'][:80]}")
    else:
        print("采集失败或无数��")
