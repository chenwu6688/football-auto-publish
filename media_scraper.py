#!/usr/bin/env python3
"""体育媒体数据采集 — 纯 HTTP 方案（直播吧 + 懂球帝）

使用 requests + BeautifulSoup 采集体育媒体数据。
- 主源：直播吧 (zhibo8.cc) — 赛程+战报全文，可访问 ✅
- 备源：懂球帝 (dongqiudi.com) — 文章详情页
- 降级：football-data.org

数据由专业记者核实，改写时确保事实准确。

用法:
    from media_scraper import SportsScraper
    scraper = SportsScraper()
    matches = scraper.scrape_today_matches("2026-06-30")
    report = scraper.scrape_match_report("https://...matchXXX.htm")
    news = scraper.scrape_hot_news()

输出格式:
    match = {
        "source": "zhibo8",
        "match_url": "https://...",
        "home_team": "巴西", "away_team": "日本",
        "home_score": 2, "away_score": 1,
        "status": "FT",  # FT/LIVE/PRE
        "league": "FIFA World Cup",
        "match_date": "2026-06-30",
    }

    report = {
        "source": "zhibo8",
        "match_url": "https://...",
        "article_title": "巴西2-1绝杀日本",
        "article_text": "完整战报正文（记者已核实）",
        "home_team": "巴西", "away_team": "日本",
        "home_score": 2, "away_score": 1,
        "goals": [{"minute": 95, "scorer": "马丁内利", "scorer_team": "home"}],
        "data_confidence": "high",
    }
"""

import re, json, time, random
from datetime import datetime, timedelta
from typing import Optional
import requests
from bs4 import BeautifulSoup


class ScraperBlockedError(Exception):
    """Raised when the source blocks our requests."""
    pass


class ScraperParseError(Exception):
    """Raised when response HTML can't be parsed."""
    pass


class SportsScraper:
    """体育媒体数据采集器（直播吧 + 懂球帝）

    爬取策略：
    1. 直播吧 — 赛程页取比赛列表，战报页取全文（优先）
    2. 懂球帝 — 直接访问文章详情页（备源）
    3. 都不可用 → ScraperBlockedError → 调用方降级
    """

    # ==================== 直播吧 ====================
    ZHIBO8_BASE = "https://www.zhibo8.cc"
    ZHIBO8_NEWS = "https://news.zhibo8.com"

    # 赛程页选择器
    ZHIBO8_MATCH_LINK_SEL = "a[href*='match']"  # 所有比赛链接
    ZHIBO8_SCHEDULE_SEL = ".schedule"  # 赛程容器
    ZHIBO8_CONTENT_SEL = ".content"  # 战报正文容器

    # ==================== 懂球帝 ====================
    DQ_BASE = "https://www.dongqiudi.com"
    DQ_ARTICLE_SEL = ".detail, .article-content, .content"

    # ==================== 反爬配置 ====================
    REQUEST_DELAY = 1.0
    MAX_RETRIES = 3
    TIMEOUT = 15
    BACKOFF_BASE = 2

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
    ]

    _consecutive_blocks = 0
    _last_request_time = 0
    BLOCK_THRESHOLD = 3

    # 联赛名映射：中文→系统标准名
    LEAGUE_MAP = {
        "英超": "Premier League", "西甲": "Primera Division",
        "意甲": "Serie A", "德甲": "Bundesliga", "法甲": "Ligue 1",
        "欧冠": "UEFA Champions League", "世界杯": "FIFA World Cup",
        "中超": "Chinese Super League", "足协杯": "FA Cup",
        "英联杯": "EFL Cup", "欧联": "UEFA Europa League",
        "欧协联": "UEFA Conference League",
    }

    def __init__(self):
        self.session = requests.Session()
        self._rotate_ua()

    # ------------------------------------------------------------------
    #  公开接口
    # ------------------------------------------------------------------

    def scrape_today_matches(self, date_str: str = None) -> list[dict]:
        """获取今日比赛列表（从直播吧首页解析）。

        返回比赛 dict 列表，含双方队名、比分（如有）、联赛名、战报链接。
        当天无比赛或无法获取时返回 []。
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        try:
            html = self._http_get(f"{self.ZHIBO8_BASE}/")
            matches = self._parse_zhibo8_homepage(html, date_str)
            if matches:
                self._consecutive_blocks = 0
                return matches
        except ScraperBlockedError:
            self._consecutive_blocks += 1
            if self._consecutive_blocks >= self.BLOCK_THRESHOLD:
                raise
        except Exception as e:
            print(f"   ⚠️ 直播吧首页解析异常: {e}")

        # 降级：从比赛战报页反推（尝试搜索当天文章）
        return self._scrape_matches_from_reports(date_str)

    def scrape_match_report(self, match_url: str) -> Optional[dict]:
        """获取比赛战报全文。

        Args:
            match_url: 比赛页面完整 URL，如 https://news.zhibo8.com/.../matchXXXX.htm

        Returns:
            战报 dict，含标题、正文、比分、进球。无可返回 None。
        """
        if not match_url:
            return None

        try:
            html = self._http_get(match_url, referer=self.ZHIBO8_BASE)
            return self._parse_zhibo8_report(html, match_url)
        except ScraperBlockedError:
            raise
        except Exception as e:
            print(f"   ⚠️ 战报解析异常 ({match_url[:60]}): {e}")
            return None

    def scrape_hot_news(self, page: int = 1) -> list[dict]:
        """获取热门体育新闻（从直播吧首页）。

        返回新闻列表，含标题、摘要、URL。
        """
        try:
            html = self._http_get(f"{self.ZHIBO8_BASE}/")
            return self._parse_zhibo8_news(html)
        except Exception as e:
            print(f"   ⚠️ 新闻解析异常: {e}")
            return []

    def scrape_dongqiudi_article(self, article_url: str) -> Optional[dict]:
        """从懂球帝获取文章内容（备源）。

        Args:
            article_url: 懂球帝文章 URL，如 https://www.dongqiudi.com/article/123456.html

        Returns:
            文章 dict，含标题、正文。
        """
        if not article_url or "dongqiudi" not in article_url:
            return None
        try:
            html = self._http_get(article_url, referer="https://www.dongqiudi.com/")
            soup = BeautifulSoup(html, "html.parser")
            title_el = soup.find("h1") or soup.find(class_=re.compile(r"title", re.I))
            content_el = soup.select_one(self.DQ_ARTICLE_SEL) if self.DQ_ARTICLE_SEL else None
            title = title_el.get_text(strip=True) if title_el else ""
            content = ""
            if content_el:
                for tag in content_el.find_all(["script", "style"]):
                    tag.decompose()
                content = content_el.get_text("\n", strip=True)
            if title and content:
                return {"source": "dongqiudi", "title": title, "article_text": content}
        except Exception as e:
            print(f"   ⚠️ 懂球帝文章解析异常: {e}")
        return None

    def check_available(self) -> bool:
        """检查直播吧是否可访问。"""
        try:
            html = self._http_get(f"{self.ZHIBO8_BASE}/", check_block=True)
            return "直播吧" in html or "zhibo8" in html
        except Exception:
            return False

    # ------------------------------------------------------------------
    #  直播吧 — 赛程页解析
    # ------------------------------------------------------------------

    def _parse_zhibo8_homepage(self, html: str, date_str: str) -> list[dict]:
        """解析直播吧首页，提取当天比赛和战报链接。

        直播吧首页有两种比赛链接：
        1. 直播链接 (zhibo.xxx) — 无比分，纯直播
        2. 战报链接 (news.zhibo8.com/.../matchXXX.htm) — 有比分和标题

        我们两种都取，优先取战报链接。
        """
        soup = BeautifulSoup(html, "html.parser")
        matches = []
        seen_urls = set()

        # 方法1：从 .schedule 取所有直播/比赛链接
        schedule = soup.select_one(self.ZHIBO8_SCHEDULE_SEL) or soup.find(class_=re.compile(r"schedule", re.I))
        if schedule:
            for a in schedule.find_all("a", href=re.compile(r"match", re.I)):
                self._process_match_link(a, date_str, matches, seen_urls)

        # 方法2：从全页找战报链接（news.zhibo8.com 的 match 页面）
        for a in soup.find_all("a", href=re.compile(r"news\.zhibo8.*match", re.I)):
            self._process_match_link(a, date_str, matches, seen_urls)

        return matches

    def _process_match_link(self, a, date_str: str, matches: list, seen_urls: set):
        """处理单个比赛链接，尝试解析为 match dict。"""
        href = a.get("href", "")
        text = a.get_text(strip=True)

        if not href or href in seen_urls:
            return
        seen_urls.add(href)

        # 拼接完整 URL
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/") and "news" in href:
            href = self.ZHIBO8_NEWS + href
        elif href.startswith("/"):
            href = self.ZHIBO8_BASE + href
        elif not href.startswith("http"):
            href = self.ZHIBO8_NEWS + "/" + href.lstrip("/")

        match = self._parse_match_from_link_text(text, href, date_str)
        if match:
            # Avoid duplicates
            key = (match["home_team"], match["away_team"])
            if not any(m["home_team"] == match["home_team"] and m["away_team"] == match["away_team"] for m in matches):
                matches.append(match)

    def _parse_match_from_link_text(self, text: str, url: str, date_str: str) -> Optional[dict]:
        """从链接文本中解析比赛信息。

        处理格式:
        - "巴西2-1绝杀日本" → home=巴西, score=2-1, away=日本
        - "德国点球大战4-5遭巴拉圭淘汰" → ...
        - "阿根廷vs巴西" → 无比分（未开始）
        """
        if not text:
            return None

        # 尝试提取比分 (X-Y 格式)
        score_m = re.search(r"(\d+)[-–:](\d+)", text)
        home_score = away_score = None
        status = "PRE"

        if score_m:
            home_score = int(score_m.group(1))
            away_score = int(score_m.group(2))
            status = "FT"  # 有比分通常是已结束

        # 提取双方队名（通过比分的前后文或 "vs" 分隔）
        home_team = ""
        away_team = ""

        if score_m:
            # 比分之前的文本是主队，之后是客队
            before = text[:score_m.start()].strip()
            after = text[score_m.end():].strip()

            # 去掉比分两边的非中文字符
            # 主队：从末尾往前找最后一个中文队名
            # 去掉比分前的中性描述词（非队名）
            home_suffixes = ["点球大战", "点球", "加时", "客场", "主场"]
            before_clean = before
            for suffix in home_suffixes:
                if before_clean.endswith(suffix):
                    before_clean = before_clean[:-len(suffix)]
                    break
            # 去前缀（标题性前缀如 "晋级16强！" "爆冷！"）
            # 去掉所有非队名前缀：以非中文开头的部分
            home_prefix_match = re.match(r"^[^一-鿿]+", before_clean)
            if home_prefix_match:
                before_clean = before_clean[home_prefix_match.end():]
            home_prefixes = ["爆冷", "大冷", "再爆冷"]
            for prefix in home_prefixes:
                if before_clean.startswith(prefix):
                    before_clean = before_clean[len(prefix):]
                    break
            home_match = re.search(r"([一-鿿]{2,6})$", before_clean)
            if home_match:
                home_team = home_match.group(1)

            # 客队：去掉常见前缀后找第一个中文队名
            away_prefixes = ["绝杀", "点杀", "逆转", "爆冷", "遭", "被",
                             "力克", "大胜", "小胜", "险胜", "战平", "逼平",
                             "横扫", "完胜", "击退", "斩杀", "淘汰",
                             "淘汰出局", "拒", "止步"]
            after_clean = after
            # 去掉比分和队名之间的描述词
            for mid in ["点球大战", "点球", "加时赛", "加时"]:
                if after_clean.startswith(mid):
                    after_clean = after_clean[len(mid):]
                    break
            for prefix in away_prefixes:
                if after_clean.startswith(prefix):
                    after_clean = after_clean[len(prefix):]
                    break
            away_match = re.search(r"^([一-鿿]{2,6})", after_clean)
            if away_match:
                away_team = away_match.group(1)
                # 去掉队名后的非队名后缀
                for suffix in ["淘汰", "淘汰出局", "绝杀", "出局", "噩梦"]:
                    if away_team.endswith(suffix):
                        away_team = away_team[:-len(suffix)]
                        break
        else:
            # 无比分，找 "vs" 或 "VS"
            vs_m = re.search(r"(.{2,6})\s*v[ssVS]\s*(.{2,6})", text)
            if vs_m:
                home_team = vs_m.group(1).strip()
                away_team = vs_m.group(2).strip()

        # 队名清理：去掉常见后缀
        for team in [home_team, away_team]:
            for suffix in ["直播", "视频", "录像", "回放", "集锦", "战报"]:
                if suffix in team:
                    return None  # 不是纯比赛链接

        if not home_team or not away_team:
            return None

        # 推断联赛名（从 URL 或上下文）
        league = self._infer_league(text, url)

        return {
            "source": "zhibo8",
            "match_url": url,
            "home_team": home_team,
            "away_team": away_team,
            "home_score": home_score,
            "away_score": away_score,
            "status": status,
            "league": league,
            "match_date": date_str,
        }

    def _infer_league(self, text: str, url: str) -> str:
        """从链接文本/URL 推断联赛名。"""
        # 先查 URL
        url_lower = url.lower()
        for keyword, league in [
            ("world", "FIFA World Cup"), ("wc", "FIFA World Cup"),
            ("premier", "Premier League"), ("epl", "Premier League"),
            ("laliga", "Primera Division"), ("serie", "Serie A"),
            ("bundesliga", "Bundesliga"), ("ligue", "Ligue 1"),
            ("champions", "UEFA Champions League"), ("ucl", "UEFA Champions League"),
            ("europa", "UEFA Europa League"),
        ]:
            if keyword in url_lower:
                return league

        # 再从文本查中文联赛名
        for cn, en in self.LEAGUE_MAP.items():
            if cn in text:
                return en

        return ""

    # ------------------------------------------------------------------
    #  直播吧 — 战报页解析
    # ------------------------------------------------------------------

    def _parse_zhibo8_report(self, html: str, url: str) -> Optional[dict]:
        """解析直播吧战报页面，提取标题+正文+比分。"""
        soup = BeautifulSoup(html, "html.parser")

        title = ""
        title_el = soup.find("h1") or soup.find("title")
        if title_el:
            title = title_el.get_text(strip=True)
            # 去掉站点名后缀
            for suffix in ["-直播吧", "_直播吧", "|直播吧"]:
                if suffix in title:
                    title = title.split(suffix)[0].strip()

        # 正文
        content_el = soup.select_one(self.ZHIBO8_CONTENT_SEL) or soup.find(class_=re.compile(r"content|article|detail|news", re.I))
        content = ""
        if content_el:
            for tag in content_el.find_all(["script", "style", "iframe"]):
                tag.decompose()
            content = content_el.get_text("\n", strip=True)

        if not content:
            return None

        # 从标题中提取比分
        home_team = away_team = ""
        home_score = away_score = None
        score_m = re.search(r"(\d+)[-–:](\d+)", title)
        if score_m:
            home_score = int(score_m.group(1))
            away_score = int(score_m.group(2))
            # 提取队名
            before = title[:score_m.start()].strip()
            after = title[score_m.end():].strip()
            hm = re.search(r"([一-鿿]{2,6})$", before)
            am = re.search(r"^([一-鿿]{2,6})", after)
            if hm:
                home_team = hm.group(1)
            if am:
                away_team = am.group(1)

        # 从正文中提取进球信息
        goals = self._extract_goals_from_text(content, home_team, away_team)

        # 从战报中提取配图
        images = []
        if content_el:
            seen_urls = set()
            for img in content_el.find_all("img"):
                src = img.get("src", "")
                if src and src.startswith("http") and src not in seen_urls:
                    # 过滤掉头像、icon等小图
                    w = img.get("width", "0")
                    if w.isdigit() and int(w) < 100:
                        continue
                    seen_urls.add(src)
                    images.append({"url": src, "source": "zhibo8"})

        # 从 URL/内容推断联赛
        league = self._infer_league(title + url, url)

        return {
            "source": "zhibo8",
            "match_url": url,
            "article_title": title,
            "article_text": content,
            "home_team": home_team,
            "away_team": away_team,
            "home_score": home_score,
            "away_score": away_score,
            "league": league,
            "goals": goals,
            "data_confidence": "high",
            "images": images,
        }

    @staticmethod
    def _extract_goals_from_text(text: str, home_team: str, away_team: str) -> list[dict]:
        """从战报正文提取进球信息。

        通过常见进球描述模式提取，如:
        - "卡塞米罗头球破门"
        - "马丁内利95分钟绝杀"
        - "佐野海舟贴地斩首开记录"
        """
        goals = []
        # 模式: 球员名 + 分钟 + 动作
        goal_patterns = [
            r"([一-鿿]{2,4})(\d+)['′]?(?:分钟)?(?:头球|破门|绝杀|进球|抽射|推射|远射|点射|补射|铲射|垫射)",
            r"(\d+)['′]?(?:分钟)?([一-鿿]{2,4})(?:头球|破门|绝杀|进球)",
            r"([一-鿿]{2,4})(?:梅开二度|独中两元|帽子戏法)",
            r"(\d+)['′]?(?:分钟)?(?:点球|点射).*?([一-鿿]{2,4})",
        ]

        for pattern in goal_patterns:
            for m in re.finditer(pattern, text):
                # 提取球员和分钟
                if m.lastindex >= 2:
                    # Try to identify which is minute and which is player
                    groups = [g for g in m.groups() if g]
                    player = ""
                    minute = None
                    for g in groups:
                        if g.isdigit():
                            minute = int(g)
                        elif re.match(r"^[一-鿿]+$", g):
                            player = g
                else:
                    player = m.group(1) if re.match(r"^[一-鿿]+$", m.group(1)) else ""
                    minute = int(m.group(1)) if m.group(1).isdigit() else None

                if not player:
                    continue

                # 判断主客队
                team = "home"
                if away_team and away_team in text:
                    # Check context around player name
                    idx = text.find(player)
                    context = text[max(0, idx - 50):idx + 50]
                    if away_team in context:
                        team = "away"

                # 避免重复
                is_dup = any(g["scorer"] == player and g["minute"] == minute for g in goals)
                if not is_dup:
                    goals.append({
                        "minute": minute,
                        "scorer": player,
                        "scorer_team": team,
                        "type": "GOAL",
                    })

        return goals

    # ------------------------------------------------------------------
    #  直播吧 — 新闻解析
    # ------------------------------------------------------------------

    def _parse_zhibo8_news(self, html: str) -> list[dict]:
        """从直播吧首页提取新闻链接。"""
        soup = BeautifulSoup(html, "html.parser")

        # 找新闻区域
        news_links = soup.find_all("a", href=re.compile(r"article|news", re.I))
        seen = set()
        news = []
        for a in news_links:
            href = a.get("href", "")
            text = a.get_text(strip=True)
            if not text or len(text) < 10:
                continue
            if href in seen:
                continue
            seen.add(href)

            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = self.ZHIBO8_BASE + href

            news.append({
                "title": text[:80],
                "url": href,
                "source": "zhibo8",
            })

        return news

    # ------------------------------------------------------------------
    #  降级：从战报页反推比赛（当天无赛程时的备源）
    # ------------------------------------------------------------------

    def _scrape_matches_from_reports(self, date_str: str) -> list[dict]:
        """从直播吧新闻区反推当天比赛。

        当赛程页没有比赛列表时，从首页新闻链接中找比赛战报。
        """
        try:
            html = self._http_get(f"{self.ZHIBO8_BASE}/")
            soup = BeautifulSoup(html, "html.parser")
            links = soup.find_all("a", href=re.compile(r"match\d+.*\.htm", re.I))

            matches = []
            seen = set()
            for a in links:
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if not href or href in seen:
                    continue
                seen.add(href)

                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = self.ZHIBO8_BASE + href
                elif not href.startswith("http"):
                    href = self.ZHIBO8_NEWS + "/" + href.lstrip("/")

                match = self._parse_match_from_link_text(text, href, date_str)
                if match:
                    matches.append(match)

            return matches
        except Exception:
            return []

    # ------------------------------------------------------------------
    #  HTTP 请求层
    # ------------------------------------------------------------------

    def _http_get(self, url: str, referer: str = None, check_block: bool = False) -> str:
        """带反爬措施的 HTTP GET 请求。"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.REQUEST_DELAY:
            time.sleep(self.REQUEST_DELAY - elapsed)

        last_error = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                headers = {
                    "User-Agent": self._current_ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                }
                if referer:
                    headers["Referer"] = referer

                resp = self.session.get(url, headers=headers, timeout=self.TIMEOUT)
                self._last_request_time = time.time()

                if resp.status_code == 200:
                    # Auto-detect encoding: many Chinese sites serve UTF-8 but
                    # declare ISO-8859-1 in headers, causing garbled text
                    if resp.encoding and resp.encoding.lower() == "iso-8859-1":
                        resp.encoding = resp.apparent_encoding or "utf-8"
                    text = resp.text
                    if not check_block:
                        block_indicators = ["验证", "访问频率", "captcha"]
                        for ind in block_indicators:
                            if ind in text:
                                raise ScraperBlockedError(f"触发封禁: {ind}")
                    return text
                elif resp.status_code == 403:
                    raise ScraperBlockedError(f"HTTP 403")
                elif resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", str(self.BACKOFF_BASE ** attempt)))
                    time.sleep(wait)
                    continue
                elif resp.status_code == 404:
                    return ""

            except requests.Timeout:
                last_error = f"timeout after {self.TIMEOUT}s"
            except requests.ConnectionError as e:
                last_error = f"connection error: {e}"
            except ScraperBlockedError:
                raise
            except Exception as e:
                last_error = str(e)

            if attempt < self.MAX_RETRIES:
                time.sleep(self.BACKOFF_BASE ** attempt + random.uniform(0, 0.5))
                self._rotate_ua()

        if last_error:
            raise requests.RequestException(last_error)
        return ""

    # ------------------------------------------------------------------
    #  UA 轮换
    # ------------------------------------------------------------------

    @property
    def _current_ua(self) -> str:
        return getattr(self, "__ua", self.USER_AGENTS[0])

    def _rotate_ua(self):
        self.__ua = random.choice(self.USER_AGENTS)
