#!/usr/bin/env python3
"""足球自媒体 — 数据采集模块

负责: 比赛数据采集、公众号爆款趋势、积分榜/射手榜、图片搜索。
"""

import os, json, sys, subprocess, requests, time, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

from constants import (PROJECT_ROOT, OUTPUT_DIR, GZH_SCRIPT,
                       FOOTBALL_DATA_KEY, FOOTBALL_DATA_BASE,
                       COMPETITION_IDS, GZH_KEYWORD_GROUPS, GZH_TRANSFER_KEYWORDS,
                       GZH_NOISE_PATTERNS, WIKI_PLAYERS, WIKI_TEAMS, FOOTYRENDERS_PLAYERS,
                       UNSPLASH_KEY, PIXABAY_KEY, PEXELS_KEY)

from utils import retry, filter_fresh_news, NEWS_MAX_AGE_HOURS, CST

import cover_binding as _cb  # 计划 2.2/4.1：封面与正文绑定（禁用随机图库配图）
import scraper_diagnostics as _diag  # 图源失败必须留痕（2026-11-04：401 静默吞掉过久）
import wiki_media as _wm  # 维基共享：足球领域唯一免费真人图源（许可+署名由模块内保证）
import gallery_query as _gq  # 图库是英文标签索引，中文 query 会配出饺子/欧元（2026-11-04）
import team_names  # 队名跨语言归一（napoli ↔ 那不勒斯）：赛程去重靠它，别各写一份
import data_chart as _dc  # 「文本里提到了哪支队」的唯一实现（alive_owners，最长匹配消解串味）


# ============================================================
# Event-fingerprint dedup (P0-1: 语义去重，杜绝「换说法重发」)
# ============================================================
# 球队别名 → 规范名（解决「马竞/马德里竞技」「国米/国际米兰」等别名导致
# 同一场比赛被识别成不同球队、从而漏过去重的问题）。
# 覆盖：英超/西甲/意甲/德甲/法甲/荷甲/葡超/欧战/国家队/中超/美职联/南美常见队。
TEAM_ALIASES = {
    # 英超
    "阿森纳": "阿森纳", "曼联": "曼联", "曼城": "曼城", "利物浦": "利物浦", "切尔西": "切尔西",
    "热刺": "热刺", "纽卡": "纽卡斯尔", "纽卡斯尔": "纽卡斯尔", "埃弗顿": "埃弗顿",
    "莱斯特城": "莱斯特城", "布莱顿": "布莱顿", "西汉姆": "西汉姆", "狼队": "狼队",
    "维拉": "阿斯顿维拉", "阿斯顿维拉": "阿斯顿维拉", "水晶宫": "水晶宫",
    "布伦特福德": "布伦特福德", "富勒姆": "富勒姆", "伯恩茅斯": "伯恩茅斯",
    "诺丁汉森林": "诺丁汉森林", "卢顿": "卢顿", "伊普斯维奇": "伊普斯维奇", "南安普顿": "南安普顿",
    "考文垂": "考文垂", "谢菲联": "谢菲联", "伯恩利": "伯恩利", "利兹联": "利兹联",
    # 西甲
    "巴萨": "巴萨", "皇马": "皇马", "马竞": "马德里竞技", "马德里竞技": "马德里竞技",
    "毕尔巴鄂": "毕尔巴鄂", "塞维利亚": "塞维利亚", "比利亚雷亚尔": "比利亚雷亚尔",
    "赫罗纳": "赫罗纳", "皇家社会": "皇家社会", "瓦伦西亚": "瓦伦西亚", "巴伦西亚": "瓦伦西亚",
    "赫塔菲": "赫塔菲", "塞尔塔": "塞尔塔", "奥萨苏纳": "奥萨苏纳",
    # 意甲
    "国米": "国际米兰", "国际米兰": "国际米兰", "米兰": "AC米兰", "AC米兰": "AC米兰",
    "尤文": "尤文图斯", "尤文图斯": "尤文图斯", "那不勒斯": "那不勒斯", "拉齐奥": "拉齐奥",
    "罗马": "罗马", "亚特兰大": "亚特兰大", "佛罗伦萨": "佛罗伦萨", "博洛尼亚": "博洛尼亚", "都灵": "都灵",
    # 德甲
    "拜仁": "拜仁", "多特": "多特", "勒沃库森": "勒沃库森", "莱比锡": "莱比锡",
    "法兰克福": "法兰克福", "门兴": "门兴", "沃尔夫斯堡": "沃尔夫斯堡", "斯图加特": "斯图加特",
    "柏林联合": "柏林联合", "弗赖堡": "弗赖堡", "霍芬海姆": "霍芬海姆", "美因茨": "美因茨",
    "科隆": "科隆", "不来梅": "不来梅",
    # 法甲
    "巴黎": "巴黎圣日耳曼", "巴黎圣日耳曼": "巴黎圣日耳曼", "马赛": "马赛", "里昂": "里昂",
    "摩纳哥": "摩纳哥", "里尔": "里尔", "尼斯": "尼斯", "雷恩": "雷恩", "斯特拉斯堡": "斯特拉斯堡",
    "朗斯": "朗斯", "南特": "南特", "图卢兹": "图卢兹", "布雷斯特": "布雷斯特",
    # 荷甲/葡超/其他欧战
    "阿贾克斯": "阿贾克斯", "埃因霍温": "埃因霍温", "费耶诺德": "费耶诺德",
    "本菲卡": "本菲卡", "波尔图": "波尔图", "葡萄牙体育": "葡萄牙体育", "里斯本竞技": "葡萄牙体育",
    "布拉加": "布拉加", "凯尔特人": "凯尔特人", "流浪者": "格拉斯哥流浪者",
    "格拉斯哥流浪者": "格拉斯哥流浪者", "布鲁日": "布鲁日", "费内巴切": "费内巴切",
    "加拉塔萨雷": "加拉塔萨雷", "萨尔茨堡红牛": "萨尔茨堡红牛",
    # 国家队
    "阿根廷": "阿根廷", "巴西": "巴西", "法国": "法国", "英格兰": "英格兰", "德国": "德国",
    "西班牙": "西班牙", "葡萄牙": "葡萄牙", "荷兰": "荷兰", "意大利": "意大利",
    "比利时": "比利时", "克罗地亚": "克罗地亚", "乌拉圭": "乌拉圭", "墨西哥": "墨西哥",
    "美国": "美国", "日本": "日本", "韩国": "韩国", "沙特": "沙特", "伊朗": "伊朗",
    "澳大利亚": "澳大利亚", "加拿大": "加拿大", "摩洛哥": "摩洛哥", "丹麦": "丹麦",
    "瑞士": "瑞士", "奥地利": "奥地利", "波兰": "波兰", "瑞典": "瑞典", "塞尔维亚": "塞尔维亚",
    "捷克": "捷克", "土耳其": "土耳其", "希腊": "希腊", "挪威": "挪威", "乌克兰": "乌克兰",
    # 中超/中国
    "海港": "上海海港", "上海海港": "上海海港", "申花": "上海申花", "上海申花": "上海申花",
    "国安": "北京国安", "北京国安": "北京国安", "泰山": "山东泰山", "山东泰山": "山东泰山",
    "蓉城": "成都蓉城", "成都蓉城": "成都蓉城", "三镇": "武汉三镇", "武汉三镇": "武汉三镇",
    "浙江": "浙江队", "浙江队": "浙江队", "津门虎": "天津津门虎", "天津津门虎": "天津津门虎",
    "河南": "河南队", "河南队": "河南队", "海牛": "青岛海牛", "青岛海牛": "青岛海牛",
    "亚泰": "长春亚泰", "长春亚泰": "长春亚泰", "大连": "大连人", "大连人": "大连人",
    "广州": "广州队", "广州队": "广州队",
    # 美职联/南美/其他
    "迈阿密": "迈阿密国际", "迈阿密国际": "迈阿密国际", "洛杉矶FC": "洛杉矶FC",
    "西雅图海湾": "西雅图海湾", "河床": "河床", "博卡青年": "博卡青年", "弗拉门戈": "弗拉门戈",
    "帕尔梅拉斯": "帕尔梅拉斯", "科林蒂安": "科林蒂安", "桑托斯": "桑托斯", "圣保罗": "圣保罗",
}

# 比分正则：匹配标题中的 "X-Y" / "X：Y" / "X:Y"（1-2 位整数）
SCORE_RE = re.compile(r"(\d{1,2})\s*[-：:]\s*(\d{1,2})")


def extract_match_teams(text):
    """从文本中提取规范化的球队名（基于别名词典）。返回去重、保序的规范名列表。

    只做词典匹配，不依赖外部 NLP；未在词典中的队名不会被识别（自动回退到
    关键词去重，不会误杀）。"""
    found = []
    for alias, canon in TEAM_ALIASES.items():
        if alias in text:
            found.append(canon)
    seen = set()
    out = []
    for c in found:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def build_match_signature(title):
    """为「比赛复盘/赛果」类选题生成事件指纹。

    仅当标题含 >=2 支球队 且 含比分(X-Y) 时返回有效指纹 ("match", 规范队集合, 比分)，
    否则返回 None —— 交给关键词去重处理，避免对转会/八卦/无比分内容误杀。

    指纹以「同两支球队 + 同比分」定义同一场比赛，与措辞无关，因此可精准拦截
    「切尔西4-3布莱顿：进球大战」与「切尔西4-3布莱顿：老六直呼过瘾」这类换说法重发。
    """
    if not title:
        return None
    teams = extract_match_teams(title)
    if len(teams) < 2:
        return None
    m = SCORE_RE.search(title)
    if not m:
        return None
    score = (int(m.group(1)), int(m.group(2)))
    return ("match", frozenset(teams), score)


def is_event_duplicate(candidate_title, event_signatures):
    """判断候选标题是否与历史事件指纹集合中的某场比赛重复（硬去重）。"""
    if not event_signatures:
        return False
    sig = build_match_signature(candidate_title)
    return bool(sig and sig in event_signatures)


# ============================================================
# Data Collection
# ============================================================


def collect_real_matches(date_str):
    """采集比赛数据。平行采集直播吧和懂球帝，保证即使一个源改版另一个仍正常工作。

    - 直播吧：比赛战报（含比分、队名、战报全文）+ 新闻文章兜底
    - 懂球帝：新闻文章（独立采集，不依赖直播吧状态）
    - 两个源的结果合并返回，其中一个失败不影响另一个
    - 两源都失败时降级到 football-data.org
    """
    print(f"[1/5] 采集真实比赛数据 ({date_str})...")

    from media_scraper import SportsScraper

    # ── 直播吧采集 ──
    zhibo8_result = None
    zhibo8_error = None
    try:
        scraper = SportsScraper()
        if scraper.check_available():
            zhibo8_result = _collect_from_media(scraper, date_str)
            if zhibo8_result and zhibo8_result.get("total_matches", 0) > 0:
                print(f"   ✅ 直播吧: {zhibo8_result['total_matches']} 场比赛, 含战报")
            else:
                print("   ⚠️ 直播吧未获取到有效比赛")
                zhibo8_error = "empty"
        else:
            print("   ⚠️ 直播吧不可达")
            zhibo8_error = "unreachable"
    except Exception as e:
        print(f"   ⚠️ 直播吧异常: {e}")
        zhibo8_error = str(e)

    # ── 懂球帝采集（平行，不依赖直播吧） ──
    dongqiudi_result = None
    dq_error = None
    try:
        dq_scraper = SportsScraper()
        dongqiudi_result = _collect_from_dongqiudi(dq_scraper, date_str)
        if dongqiudi_result and dongqiudi_result.get("total_matches", 0) > 0:
            print(f"   ✅ 懂球帝: {dongqiudi_result['total_matches']} 篇文章")
        else:
            print("   ⚠️ 懂球帝未获取到有效文章")
            dq_error = "empty"
    except Exception as e:
        print(f"   ⚠️ 懂球帝异常: {e}")
        dq_error = str(e)

    # ── 合并结果 ──
    if zhibo8_result or dongqiudi_result:
        merged = {
            "date": date_str,
            "total_matches": 0,
            "fixtures_by_league": {},
            "all_fixtures": [],
            "standings": {},
            "data_source": "zhibo8",  # 默认直播吧，只有懂球帝时才变
            "media_reports": {},
            "news_articles": [],
            "transfer_news": [],
        }

        # 合并直播吧数据
        if zhibo8_result:
            merged["all_fixtures"].extend(zhibo8_result.get("all_fixtures", []))
            merged["fixtures_by_league"] = zhibo8_result.get("fixtures_by_league", {})
            merged["media_reports"] = zhibo8_result.get("media_reports", {})
            merged["news_articles"] = zhibo8_result.get("news_articles", [])

        # 合并懂球帝数据
        if dongqiudi_result:
            # 如果直播吧完全没数据，data_source 切为懂球帝
            if not zhibo8_result or not zhibo8_result.get("all_fixtures"):
                merged["data_source"] = "dongqiudi"
            merged["all_fixtures"].extend(dongqiudi_result.get("all_fixtures", []))

        merged["total_matches"] = len(merged["all_fixtures"])

        # 采集转会快讯（使用独立抓取，确保转会类文章被标记）
        try:
            merged["transfer_news"] = collect_transfer_news(date_str)
        except Exception as e:
            print(f"   ⚠️ 转会快讯采集异常: {e}")

        # 时效闸门（计划 4.1 / 7.2）：新闻流统一过闸，超 72h 素材不得进发布流。
        # 降级原则（计划九）：闸门自身异常时放行，绝不因拦截逻辑阻塞发布。
        try:
            merged["news_articles"], _d_n, _u_n = filter_fresh_news(
                merged.get("news_articles", []), NEWS_MAX_AGE_HOURS, label="新闻流")
            merged["transfer_news"], _d_t, _u_t = filter_fresh_news(
                merged.get("transfer_news", []), NEWS_MAX_AGE_HOURS, label="转会流")
            if _d_n or _d_t:
                print(f"   ⏱️ 时效闸门：新闻流剔 {len(_d_n)} 条、转会流剔 {len(_d_t)} 条"
                      f"（无法判定时间各保留 {_u_n}/{_u_t} 条）")
        except Exception as e:
            print(f"   ⚠️ 时效闸门异常（降级放行）: {e}")

        _n_fix = _normalize_zh_team_names(merged["all_fixtures"])
        if _n_fix:
            print(f"   🔤 队名归一：{_n_fix} 处残缺中文名已按实体表修正（原值留 raw_*）")

        return merged

    # ── 两源都失败 → football-data.org ──
    print("   ⚠️ 直播吧和懂球帝均不可用，降级到 football-data.org")
    return _collect_from_football_data(date_str)


def collect_transfer_news(date_str):
    """独立采集转会/流言类新闻文章。

    从直播吧和懂球帝的新闻文章中过滤出转会相关的内容，
    供"转会密探"等栏目使用。与原 match_data 管线并行。

    Returns:
        list[dict]: 转会新闻列表，每条含 title, url, article_text, source, _content_type_hint
    """
    print(f"[数据] 采集转会新闻 ({date_str})...")
    from media_scraper import SportsScraper
    transfer_keywords = ("转会", "签约", "续约", "离队", "租借", "加盟",
                         "买断", "标价", "引进", "转会费", "解约", "免签",
                         "换帅", "下课", "新任主帅", "新帅")
    articles = []
    seen_titles = set()

    # 从直播吧采集新闻文章
    try:
        scraper = SportsScraper()
        news = scraper.scrape_football_news(date_str, max_articles=30)
        for art in news or []:
            title = (art.get("title", "") or "").strip()
            if not title or title in seen_titles:
                continue
            # 检查标题是否包含转会关键词
            if any(kw in title for kw in transfer_keywords):
                seen_titles.add(title)
                article_text = art.get("article_text", "") or art.get("_content", "") or ""
                articles.append({
                    "title": title,
                    "url": art.get("url", ""),
                    "article_text": article_text,
                    "source": "zhibo8",
                    "_content_type_hint": "转会资讯",
                })
    except Exception as e:
        print(f"   ⚠️ 直播吧转会新闻采集异常: {e}")

    # 从懂球帝采集
    try:
        scraper2 = SportsScraper()
        dq_news = scraper2.scrape_dongqiudi_headlines(max_articles=30)
        for art in dq_news or []:
            title = (art.get("title", "") or "").strip()
            if not title or title in seen_titles:
                continue
            if any(kw in title for kw in transfer_keywords):
                seen_titles.add(title)
                article_text = ""
                try:
                    result = scraper2.scrape_dongqiudi_article(art.get("url", ""))
                    if result:
                        article_text = result.get("content", "") or result.get("article_text", "") or ""
                except Exception:
                    pass
                articles.append({
                    "title": title,
                    "url": art.get("url", ""),
                    "article_text": article_text,
                    "source": "dongqiudi",
                    "_content_type_hint": "转会资讯",
                    "published_at": art.get("published_at", ""),
                })
    except Exception as e:
        print(f"   ⚠️ 懂球帝转会新闻采集异常: {e}")

    if articles:
        print(f"   🔍 转会新闻: {len(articles)} 篇")
        for a in articles[:5]:
            print(f"      - {a['title'][:45]}")
    else:
        print(f"   ℹ️ 暂未发现转会新闻")
    # 时效闸门（计划 4.1 / 7.2）：转会新闻同样受 72h 约束，防旧闻新发
    articles, _d_tr, _u_tr = filter_fresh_news(articles, NEWS_MAX_AGE_HOURS, label="转会新闻")
    if _d_tr:
        print(f"   ⏱️ 转会新闻时效闸门：剔除 {len(_d_tr)} 条超 {NEWS_MAX_AGE_HOURS}h 素材")
    return articles


def collect_future_matches(date_str, days_ahead=1):
    """采集未来比赛数据用于赛前预测。

    使用 media_scraper.SportsScraper 的 scrape_zhibo8_schedule() 方法
    从直播吧首页获取未来赛程，返回未开始的比赛列表。

    Args:
        date_str: 当前日期 YYYY-MM-DD
        days_ahead: 预测哪一天的比赛，默认1（明日）

    Returns:
        list[dict]: 未来比赛列表，每条含 home_team, away_team, league, status, utc_date
    """
    target_date = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days_ahead)
    target_str = target_date.strftime("%Y-%m-%d")

    from media_scraper import SportsScraper
    try:
        scraper = SportsScraper()
        if not scraper.check_available():
            print("   ⚠️ 直播吧不可达，无法获取赛程")
            return []

        future_matches = scraper.scrape_zhibo8_schedule(target_str)
        if future_matches:
            print(f"   ✅ 赛前预测素材: {len(future_matches)} 场未来比赛 ({target_str})")
        else:
            print(f"   ℹ️ 明日 ({target_str}) 无赛程，跳过赛前预测")
        return future_matches
    except Exception as e:
        print(f"   ⚠️ 获取未来赛程异常: {e}")
        return []


def _collect_from_media(scraper, date_str):
    """从媒体源（直播吧）采集比赛数据，并用战报内容丰富数据。

    返回格式与 collect_real_matches 兼容：
    {
        "date": str, "total_matches": int,
        "fixtures_by_league": dict,
        "all_fixtures": list[dict],
        "standings": dict,
        "data_source": str,
        "media_reports": dict,  # match_id → report dict
    }
    """
    matches = scraper.scrape_today_matches(date_str)
    if not matches:
        return {"date": date_str, "total_matches": 0, "fixtures_by_league": {},
                "all_fixtures": [], "standings": {}, "data_source": "media"}

    # 获取每场比赛的战报全文
    fixture_details = []
    media_reports = {}

    for m in matches:
        report_text = ""
        goals = []
        source_images = []
        match_url = m.get("match_url", "")

        # 获取战报
        if match_url:
            try:
                report = scraper.scrape_match_report(match_url)
                if report:
                    report_text = report.get("article_text", "")
                    goals = report.get("goals", [])
                    source_images = report.get("images", [])
                    # 从战报中提取联赛名
                    if not m.get("league") and report.get("league"):
                        m["league"] = report["league"]
                    # 从战报正文推断联赛名
                    if not m.get("league") and report_text:
                        league_keywords = {
                            "世界杯": "FIFA World Cup", "欧冠": "UEFA Champions League",
                            "英超": "Premier League", "西甲": "Primera Division",
                            "意甲": "Serie A", "德甲": "Bundesliga", "法甲": "Ligue 1",
                        }
                        for kw, league in league_keywords.items():
                            if kw in report_text:
                                m["league"] = league
                                break
            except Exception as e:
                print(f"   ⚠️ 战报获取失败 ({m['home_team']} vs {m['away_team']}): {e}")

        fixture = {
            "id": f"zhibo8_{m['home_team']}_{m['away_team']}",
            "source": "zhibo8",
            "source_url": match_url,
            "league": m.get("league", ""),
            "home_team": m["home_team"],
            "away_team": m["away_team"],
            "home_score": m.get("home_score"),
            "away_score": m.get("away_score"),
            "status": m.get("status", "FT"),
            "utc_date": date_str,
            "goals": goals,
            "article_text": report_text,
            "source_images": source_images,
            "data_confidence": "high",
        }
        fixture_details.append(fixture)

        if report_text:
            media_reports[fixture["id"]] = fixture

    # 按联赛分组
    by_league = defaultdict(list)
    for f in fixture_details:
        league = f.get("league", "未知赛事")
        by_league[league].append(f)

    print(f"   📄 直播吧战报: {len(media_reports)}/{len(fixture_details)} 场有详细战报")

    result = {
        "date": date_str,
        "total_matches": len(fixture_details),
        "fixtures_by_league": dict(by_league),
        "all_fixtures": fixture_details,
        "standings": {},
        "data_source": "zhibo8",
        "media_reports": media_reports,
        "news_articles": [],  # 新闻文章兜底
    }

    # 平行采集直播吧新闻文章（作为补充素材，不依赖战报数量）
    try:
        news_articles = scraper.scrape_football_news(date_str, max_articles=15)
        result["news_articles"] = news_articles
        if news_articles:
            print(f"   📰 直播吧新闻: {len(news_articles)} 篇")
    except Exception as e:
        print(f"   ⚠️ 新闻采集异常: {e}")

    return result


def _collect_from_dongqiudi(scraper, date_str):
    """从懂球帝采集新闻文章。

    与直播吧平行采集，返回格式与 collect_real_matches 兼容，
    data_source = "dongqiudi"。
    """
    headlines = scraper.scrape_dongqiudi_headlines(max_articles=15)
    # 时效闸门（计划 4.1 / 7.2）：懂球帝首页会挂出多日前文章，
    # 先按发布时间拦截旧闻，再取正文（顺带省下无谓的正文抓取）。
    headlines, _dropped_dq, _ = filter_fresh_news(headlines, NEWS_MAX_AGE_HOURS, label="dongqiudi")
    if _dropped_dq:
        print(f"   ⏱️ 懂球帝时效闸门：剔除 {len(_dropped_dq)} 条超 {NEWS_MAX_AGE_HOURS}h 旧闻")
    if not headlines:
        return {"date": date_str, "total_matches": 0, "fixtures_by_league": {},
                "all_fixtures": [], "standings": {}, "data_source": "media"}

    # 取每篇文章正文
    fixture_details = []
    for art in headlines:
        url = art.get("url", "")
        title = art.get("title", "")
        try:
            result = scraper.scrape_dongqiudi_article(url)
            article_text = result.get("article_text", "") if result else ""
        except Exception:
            article_text = ""

        fixture = {
            "id": f"dongqiudi_{hash(url) % 1000000:06d}",
            "source": "dongqiudi",
            "source_url": url,
            "league": "",
            "home_team": "", "away_team": "",
            "home_score": None, "away_score": None,
            "status": "NEWS",
            "utc_date": date_str,
            "goals": [],
            "article_text": article_text,
            "article_title": title,
            "source_images": [],
            "data_confidence": "high",
        }
        fixture_details.append(fixture)

    print(f"   ✅ 懂球帝文章: {len(fixture_details)} 篇, "
          f"{sum(1 for f in fixture_details if f.get('article_text'))} 篇有正文")

    return {
        "date": date_str,
        "total_matches": len(fixture_details),
        "fixtures_by_league": {},
        "all_fixtures": fixture_details,
        "standings": {},
        "data_source": "dongqiudi",
        "media_reports": {},
        "news_articles": [],
    }


_RE_CJK = re.compile(r"[\u4e00-\u9fff]")


def _normalize_zh_team_names(fixtures) -> int:
    """把残缺中文队名归一到实体表里的规范名，返回改动处数。

    2026-11-06 实证：直播吧把「布拉格斯拉维亚」写成「拉格斯拉维亚」，这个名字
    一路带进选题、标题、正文和配图 anchor。``data_chart.resolve_team`` 认不出，
    anchor 存下脏名，配图相关性验收复验时「实体不在正文」，硬项失败把整批发布
    挡掉——当天 0 篇。源头在这里，改源头才是根治。

    只动**含中文**的值：football-data 给的英文名（"SK Slavia Praha"）要参与
    Wikipedia 比分比对与 standings 匹配，换成中文会打断那条链。
    归一只在唯一命中时成立，否则保留原值（不猜）。
    """
    import data_chart            # 延迟导入：采集不依赖渲染层，且避免顶层循环

    n = 0
    for f in fixtures or []:
        if not isinstance(f, dict):
            continue
        for key in ("home_team", "away_team"):
            raw = str(f.get(key) or "").strip()
            if not raw or not _RE_CJK.search(raw):
                continue
            zh = data_chart.normalize_team_zh(raw)
            if zh and zh != raw:
                f.setdefault(f"raw_{key}", raw)     # 原值留痕，便于回溯数据源错字
                f[key] = zh
                n += 1
    return n


def _collect_from_football_data(date_str):
    """从 football-data.org 采集比赛数据（备用源）。"""
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    target_date = datetime.strptime(date_str, "%Y-%m-%d")
    weekday = target_date.weekday()
    if weekday == 6:
        from_date = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    elif weekday == 0:
        from_date = (target_date - timedelta(days=2)).strftime("%Y-%m-%d")
    else:
        from_date = date_str
    to_date = date_str
    print(f"   查询范围: {from_date} ~ {to_date}")

    all_matches = []
    for league_name, comp_id in COMPETITION_IDS.items():
        try:
            def _fetch():
                resp = requests.get(f"{FOOTBALL_DATA_BASE}/competitions/{comp_id}/matches",
                                   params={"dateFrom": from_date, "dateTo": to_date},
                                   headers=headers, timeout=15)
                resp.raise_for_status()
                return resp.json().get("matches", [])
            matches = retry(_fetch, max_retries=2, base_delay=1, desc=f"football-data({league_name})")
            if matches:
                print(f"   {league_name}: {len(matches)} 场")
            all_matches.extend(matches)
            time.sleep(0.6)
        except Exception as e:
            print(f"   {league_name}: error - {e}")

    seen_ids = set()
    unique = []
    for m in all_matches:
        if m.get("id") not in seen_ids:
            seen_ids.add(m.get("id"))
            unique.append(m)

    relevant = []
    fixture_details = []
    valid_comps = {"Premier League", "Primera Division", "Serie A", "Bundesliga",
                   "Ligue 1", "UEFA Champions League", "Campeonato Brasileiro Série A",
                   "FIFA World Cup"}
    for m in unique:
        comp = m.get("competition", {}).get("name", "")
        if comp in valid_comps:
            relevant.append(m)
            score = m.get("score", {}).get("fullTime", {})
            hg = score.get("home")
            ag = score.get("away")
            fixture = {
                "id": m.get("id"),
                "league": comp, "home_team": m.get("homeTeam", {}).get("name", ""),
                "away_team": m.get("awayTeam", {}).get("name", ""),
                "home_score": hg, "away_score": ag,
                "status": m.get("status"), "matchday": m.get("matchday"),
                "utc_date": m.get("utcDate", ""),
                "goals": [],  # will be filled below if available
            }
            fixture_details.append(fixture)

    # Step 1b: Nullify scores for non-finished matches
    # Football-data.org score.fullTime may contain placeholder or stale data
    # for IN_PLAY/PRE matches. Only FT/AET/PEN status scores are reliable.
    FINISHED_STATUSES = {"FT", "AET", "PEN"}
    non_finished = 0
    for f in fixture_details:
        if f.get("status") not in FINISHED_STATUSES:
            if f["home_score"] is not None or f["away_score"] is not None:
                non_finished += 1
            f["home_score"] = None
            f["away_score"] = None
    if non_finished:
        print(f"   ⚠️ 已清除 {non_finished} 场未结束比赛的比分（状态非FT/AET/PEN）")

    # Step 2: Enrich with goal scorers from match detail API (if result exists)
    finished_matches = [f for f in fixture_details
                        if f["home_score"] is not None or f["away_score"] is not None]
    if finished_matches:
        print(f"   补充进球数据 ({len(finished_matches)} 场有比分)...")
    for f in finished_matches:
        mid = f.get("id")
        if not mid:
            continue
        try:
            def _fetch_detail():
                resp = requests.get(f"{FOOTBALL_DATA_BASE}/matches/{mid}",
                                   headers=headers, timeout=15)
                resp.raise_for_status()
                return resp.json()
            detail = retry(_fetch_detail, max_retries=1, base_delay=1, desc=f"match-detail({mid})")
            raw_goals = detail.get("match", {}).get("goals", []) or detail.get("goals", [])
            goals = []
            for g in raw_goals:
                scorer = g.get("scorer", {}) or {}
                assist = g.get("assist", {}) or {}
                goals.append({
                    "minute": g.get("minute"),
                    "scorer_name": scorer.get("name", ""),
                    "scorer_team": "home" if g.get("team", {}).get("type", "") == "home" else "away",
                    "assist_name": assist.get("name", ""),
                    "type": g.get("type", "GOAL"),
                })
            if goals:
                f["goals"] = goals
                print(f"   ⚽ {f['home_team']} vs {f['away_team']}: {len(goals)} 粒进球")
            time.sleep(0.6)
        except Exception as e:
            print(f"   ⚠️ match-detail({mid}): {e}")
            time.sleep(0.6)

    # Step 3: Cross-validate World Cup scores against Wikipedia (free, reliable)
    wc_finished = [f for f in fixture_details
                   if f.get("league") == "FIFA World Cup"
                   and (f["home_score"] is not None or f["away_score"] is not None)]
    if wc_finished:
        print(f"   🌐 交叉验证世界比赛分 (Wikipedia)...")
        wiki_scores = _fetch_wikipedia_wc_scores()
        if wiki_scores:
            for f in wc_finished:
                key = (f["home_team"].lower(), f["away_team"].lower())
                wiki_score = wiki_scores.get(key) or wiki_scores.get((key[1], key[0]))
                if wiki_score:
                    wk_h, wk_a = wiki_score
                    if wk_h == f["home_score"] and wk_a == f["away_score"]:
                        f["data_confidence"] = "high"  # 双源一致
                        print(f"   ✅ {f['home_team']} vs {f['away_team']}: {f['home_score']}-{f['away_score']} (Wikipedia一致)")
                    elif wk_h == f["away_score"] and wk_a == f["home_score"]:
                        # 比分一致但主客队对调
                        f["data_confidence"] = "high"
                        print(f"   ✅ {f['home_team']} vs {f['away_team']}: {f['home_score']}-{f['away_score']} (Wikipedia一致, 主客对调)")
                    else:
                        f["data_confidence"] = "conflict"
                        print(f"   ⚠️ {f['home_team']} vs {f['away_team']}: football-data={f['home_score']}-{f['away_score']} vs Wikipedia={wk_h}-{wk_a}")
                else:
                    f["data_confidence"] = "low"  # 单一来源
        else:
            for f in wc_finished:
                f["data_confidence"] = "low"

    by_league = defaultdict(list)
    for f in fixture_details:
        by_league[f["league"]].append(f)
    print(f"   {len(relevant)} 场比赛 ({len(by_league)} 个联赛)")

    standings = fetch_recent_standings()

    result = {"date": date_str, "total_matches": len(relevant),
              "fixtures_by_league": dict(by_league), "all_fixtures": fixture_details, "standings": standings}

    # Log source count
    enriched = sum(1 for f in fixture_details if f.get("goals"))
    high_conf = sum(1 for f in fixture_details if f.get("data_confidence") == "high")
    conflicts = sum(1 for f in fixture_details if f.get("data_confidence") == "conflict")
    if enriched:
        print(f"   📊 数据源: football-data.org (比分{len(finished_matches)}场 + 进球{enriched}场)")
    if high_conf:
        print(f"   📊 双源验证通过: {high_conf}场")
    if conflicts:
        print(f"   ⚠️ 比分冲突(需人工核查): {conflicts}场")
    _n_fix = _normalize_zh_team_names(fixture_details)
    if _n_fix:
        print(f"   🔤 队名归一：{_n_fix} 处残缺中文名已按实体表修正（原值留 raw_*）")
    return result


def _fetch_wikipedia_wc_scores():
    """Fetch 2026 World Cup match results from Wikipedia as cross-validation source.

    Returns dict: {(home_team, away_team): (home_score, away_score)}
    Team names are lowercased for matching. Handles common name variants.
    """
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "titles": "2026_FIFA_World_Cup",
                "prop": "extracts",
                "explaintext": 1,
                "format": "json",
            },
            timeout=20,
        )
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        content = ""
        for pid, pdata in pages.items():
            if pid != "-1":
                content = pdata.get("extract", "")
        if not content:
            return None

        # Normalize team name variants for matching
        NAME_MAP = {
            "usa": "united states", "us": "united states",
            "korea republic": "south korea", "south korea": "korea republic",
            "iran": "iran", "côte d'ivoire": "ivory coast",
            "china": "china pr", "saint kitts": "st kitts",
            "saint lucia": "st lucia", "saint vincent": "st vincent",
        }

        def norm(name):
            n = name.lower().strip()
            return NAME_MAP.get(n, n)

        # Find score patterns in Wikipedia text: "Team A 1–2 Team B"
        # Wikipedia uses en-dash (–) for scores
        pattern = r"([A-Za-zÀ-ÿ' ]+?)\s*(\d+)[–-](\d+)\s*([A-Za-zÀ-ÿ' ,]+?)(?:\n|\.|;|\))"
        results = {}
        for m in re.finditer(pattern, content):
            t1_raw = m.group(1).strip()
            s1 = int(m.group(2))
            s2 = int(m.group(3))
            t2_raw = m.group(4).strip()
            t1 = norm(t1_raw)
            t2 = norm(t2_raw.rstrip(".,;)"))
            if s1 >= 0 and s2 >= 0:
                results[(t1, t2)] = (s1, s2)

        if results:
            print(f"   🌐 Wikipedia 解析到 {len(results)} 场比分")
            return results
        return None
    except Exception as e:
        print(f"   ⚠️ Wikipedia API error: {e}")
        return None


# ============================================================
# GZH Trending
# ============================================================

def _is_football_relevant(article):
    title = (article.get("title", "") or "") + (article.get("summary", "") or "")
    for pattern in GZH_NOISE_PATTERNS:
        if pattern in title:
            return False
    return True


def get_previously_used_sources(current_date, lookback_days=3):
    used = set()
    today = datetime.strptime(current_date, "%Y-%m-%d")
    for i in range(1, lookback_days + 1):
        dt = today - timedelta(days=i)
        meta_path = OUTPUT_DIR / dt.strftime("%Y-%m-%d") / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
            for a in meta.get("articles", []):
                for src in a.get("sources_used", []):
                    used.add(src[:40])
                if a.get("title"):
                    used.add(a["title"][:40])
        except Exception:
            pass
    if used:
        print(f"   跨天去重: 已加载 {len(used)} 条历史素材/标题")
    return used


def get_topic_history(current_date, lookback_days=7):
    """Track previously covered topics — teams, players, keywords — to avoid repetition."""
    history = {"titles": set(), "keywords": set(), "teams": set(), "players": set(),
               "content_types": [], "event_signatures": set(), "title_prefixes": set()}
    today = datetime.strptime(current_date, "%Y-%m-%d")
    for i in range(1, lookback_days + 1):
        dt = today - timedelta(days=i)
        meta_path = OUTPUT_DIR / dt.strftime("%Y-%m-%d") / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
            for a in meta.get("articles", []):
                title = a.get("title", "")
                if title:
                    history["titles"].add(title[:30])
                    # 标题前缀（前 6 字）用于「禁止连续/近期同前缀」的模板化去重
                    history["title_prefixes"].add(title[:6])
                    # 比赛事件指纹：同两队+同比分 = 同一场，近 7 天只发一次
                    sig = build_match_signature(title)
                    if sig:
                        history["event_signatures"].add(sig)
                # Also track Hupu source post titles for dedup
                source_post = a.get("source_post", "")
                if source_post:
                    history["titles"].add(source_post[:50])
                for kw in a.get("keywords", []):
                    history["keywords"].add(kw.lower())
                for tag in a.get("tags", []):
                    history["keywords"].add(tag.lower())
                # 队名：原先只查 constants.WIKI_TEAMS（15 支队），那不勒斯/拉齐奥/
                # 科莫/朗斯全在表外，"近 7 天写过这两队"这条判据对它们恒为假。
                # 标题走最长匹配（「米兰」不吃「国际米兰」），关键词走跨语言归一
                # （keywords 常写英文 Napoli，标题是中文「那不勒斯」）。
                _ents = set(_dc.alive_owners(title.lower())) if title else set()
                _ents |= team_names.teams_in_tokens(
                    list(a.get("keywords") or []) + list(a.get("tags") or []))
                history["teams"] |= _ents
                for player in WIKI_PLAYERS:
                    if player in title:
                        history["players"].add(player)
                ct = a.get("content_type", "")
                if ct:
                    history["content_types"].append(ct)
        except Exception:
            pass
    if history["titles"]:
        print(f"   历史去重: 近{lookback_days}天 {len(history['titles'])} 篇, "
              f"覆盖球队 {len(history['teams'])} 支, 球员 {len(history['players'])} 人"
              + (f", 比赛事件 {len(history['event_signatures'])} 场" if history['event_signatures'] else ""))
    return history


def fetch_gzh_football_trends(date_str, keyword_groups=None, fallback_match_data=None):
    print(f"[数据] 从公众号爆款库采集足球话题 ({date_str})...")
    target_date = datetime.strptime(date_str, "%Y-%m-%d")
    start_date = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    all_raw = []
    kw_groups = keyword_groups if keyword_groups is not None else GZH_KEYWORD_GROUPS

    gzh_cache = OUTPUT_DIR / "gzh_cache"
    gzh_cache.mkdir(parents=True, exist_ok=True)

    for kw in kw_groups:
        try:
            safe_name = re.sub(r'[^a-zA-Z0-9_一-鿿]', '_', kw)[:30]
            output_file = str(gzh_cache / f"gzh_{safe_name}.json")
            cmd = [sys.executable, GZH_SCRIPT, "--keyword", kw, "--start-date", start_date,
                   "--output-format", "json", "--output-file", output_file]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                if os.path.exists(output_file):
                    data = json.loads(Path(output_file).read_text())
                    for item in data.get("items", []):
                        if _is_football_relevant(item):
                            all_raw.append(item)
                    # Clean up temp file after reading
                    try:
                        Path(output_file).unlink()
                    except OSError:
                        pass
        except Exception as e:
            print(f"   搜索'{kw[:20]}'失败: {e}")

    # Clean up stale cache files (>1 day old)
    try:
        cutoff = time.time() - 86400
        for f in gzh_cache.glob("gzh_*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except Exception:
        pass

    if not all_raw:
        print("   公众号爆款库未找到足球相关文章")
        if fallback_match_data:
            print("   ⚠️ GZH爆款库不可用，尝试从比赛数据构造备选热点...")
            return fetch_fallback_trends(fallback_match_data)
        return []

    seen = set()
    unique = []
    for a in all_raw:
        t = a.get("title", "")[:40]
        if t and t not in seen:
            seen.add(t)
            unique.append(a)
    unique.sort(key=lambda x: x.get("dataScore", 0), reverse=True)

    used_sources = get_previously_used_sources(date_str)
    if used_sources:
        filtered = []
        for a in unique:
            title = a.get("title", "")[:40]
            if title in used_sources:
                continue
            is_dup = any(len(u) >= 10 and (u[:20] in title or title[:20] in u) for u in used_sources)
            if not is_dup:
                filtered.append(a)
        unique = filtered

    if not unique and fallback_match_data:
        print("   ⚠️ GZH爆款库文章全部去重，从比赛数据补充备选热点...")
        return fetch_fallback_trends(fallback_match_data)

    print(f"   采集到 {len(unique)} 篇真实足球爆款文章")
    for i, a in enumerate(unique[:10]):
        print(f"   {i+1}. [{a.get('clicksCount', '?')}阅读] {a.get('title', '')[:60]} — {a.get('accountName', '?')}")
    return unique


def fetch_fallback_trends(match_data, standings=None):
    """当 GZH 爆款库不可用时，从比赛数据和积分榜构造备选热点话题。

    Returns list of dicts in the same format as GZH articles (title, clicksCount, etc.)
    so the downstream LLM pipeline works identically.
    """
    print("   ⚠️ GZH 爆款库为空，使用比赛数据构造备选热点话题...")
    fallback = []
    idx = 0

    # 1. From match results: high-scoring games, upsets, close games
    for m in match_data.get("all_fixtures", []):
        hg = m.get("home_score")
        ag = m.get("away_score")
        home = m.get("home_team", "")
        away = m.get("away_team", "")
        league = m.get("league", "")
        if hg is None:
            continue

        total_goals = hg + ag
        goal_diff = abs(hg - ag)

        # High-scoring game
        if total_goals >= 5:
            fallback.append({
                "title": f"进球大战！{home} {hg}-{ag} {away}，{league}今日最刺激一战",
                "summary": f"{home}与{away}联手贡献{total_goals}球，堪称今日最佳比赛。",
                "clicksCount": 50000 + total_goals * 10000,
                "accountName": "足球热点",
                "dataScore": 95,
            })
            idx += 1

        # Upset / close game
        if goal_diff <= 1 and total_goals > 0:
            tag = "冷门" if goal_diff == 0 else "险胜"
            fallback.append({
                "title": f"{tag}！{home} {hg}-{ag} {away}，比赛悬念留到最后",
                "summary": f"{home}与{away}的较量直到最后时刻才分出胜负。",
                "clicksCount": 30000 + (3 - goal_diff) * 5000,
                "accountName": "足球热点",
                "dataScore": 88 - goal_diff * 5,
            })
            idx += 1

    # 2. From standings: top-of-table clashes, relegation battles
    if standings:
        for league_name, table in standings.items():
            if not table:
                continue
            # Championship race
            if len(table) >= 2:
                top = table[0]
                second = table[1]
                pts_diff = top.get("points", 0) - second.get("points", 0)
                if pts_diff <= 3:
                    fallback.append({
                        "title": f"{league_name}争冠白热化！{top['team']}仅领先{second['team']}{pts_diff}分",
                        "summary": f"本赛季{league_name}冠军悬念再起，{top['team']}和{second['team']}的差距仅{pts_diff}分。",
                        "clicksCount": 40000 + (3 - pts_diff) * 10000,
                        "accountName": "足球热点",
                        "dataScore": 90 + (3 - pts_diff) * 3,
                    })
                    idx += 1

            # Relegation battle (last 3)
            if len(table) >= 6:
                bottom = table[-1]
                bottom2 = table[-2] if len(table) >= 2 else None
                if bottom2 and bottom.get("points", 0) is not None and bottom2.get("points", 0) is not None:
                    pts_gap = bottom2["points"] - bottom["points"]
                    if pts_gap <= 2:
                        fallback.append({
                            "title": f"保级生死战！{bottom['team']}垫底，距安全区仅{pts_gap}分",
                            "summary": f"{bottom['team']}目前排名垫底，保级形势严峻。",
                            "clicksCount": 25000,
                            "accountName": "足球热点",
                            "dataScore": 82,
                        })
                        idx += 1

    # 3. World Cup special: if match data contains World Cup fixtures
    wc_matches = [m for m in match_data.get("all_fixtures", [])
                  if m.get("league") == "FIFA World Cup"]
    if wc_matches:
        groups = set()
        for m in wc_matches:
            groups.add(m.get("matchday", "?"))
        fallback.append({
            "title": f"🌍 世界杯战报：今日{len(wc_matches)}场激战，出线形势日渐明朗",
            "summary": f"世界杯小组赛继续进行，今日{len(wc_matches)}场比赛，各队为出线名额全力争胜。",
            "clicksCount": 80000 + len(wc_matches) * 5000,
            "accountName": "世界杯专区",
            "dataScore": 98,
        })
        idx += 1

        # Check for standout results
        for m in wc_matches[:3]:
            hg = m.get("home_score")
            ag = m.get("away_score")
            if hg is not None and ag is not None and abs(hg - ag) <= 1:
                fallback.append({
                    "title": f"世界杯悬念：{m['home_team']} {hg}-{ag} {m['away_team']}，小组格局再生变",
                    "summary": f"世界杯小组赛一场关键战，{m['home_team']}与{m['away_team']}的激烈对决。",
                    "clicksCount": 60000,
                    "accountName": "世界杯专区",
                    "dataScore": 92,
                })
                idx += 1

    # Deduplicate by title
    seen = set()
    unique = []
    for a in fallback:
        t = a.get("title", "")[:40]
        if t and t not in seen:
            seen.add(t)
            unique.append(a)

    if unique:
        print(f"   ✅ 已从比赛数据构造 {len(unique)} 个备选热点话题")
        for i, a in enumerate(unique[:5]):
            print(f"      {i+1}. [模拟{ a.get('clicksCount', 0)}阅读] {a.get('title', '')[:50]}")
    else:
        print("   ❌ 比赛数据也不足以构造备选话题")
    return unique


def fetch_recent_standings():
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    # Reuse COMPETITION_IDS for league→ID mapping (domestic leagues only)
    sid_map = {k: v for k, v in COMPETITION_IDS.items() if v != 2001}  # exclude UCL
    standings = {}
    for comp_name, comp_id in sid_map.items():
        try:
            resp = requests.get(f"{FOOTBALL_DATA_BASE}/competitions/{comp_id}/standings",
                              headers=headers, timeout=15)
            if resp.status_code == 200:
                for s in resp.json().get("standings", []):
                    if s.get("type") == "TOTAL":
                        standings[comp_name] = [{"position": r.get("position"),
                            "team": r.get("team", {}).get("name", ""), "points": r.get("points"),
                            "played": r.get("playedGames"), "goal_diff": r.get("goalDifference")}
                            for r in s.get("table", [])]
            time.sleep(0.6)
        except Exception as e:
            print(f"   积分榜({comp_name}): error - {e}")
    return standings


def fetch_scorers():
    """Fetch top scorers from major leagues for 排行榜 content type."""
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    # Reuse COMPETITION_IDS for league→ID mapping
    scorers = {}
    for league_name, comp_id in COMPETITION_IDS.items():
        try:
            resp = requests.get(f"{FOOTBALL_DATA_BASE}/competitions/{comp_id}/scorers",
                              headers=headers, params={"limit": 10}, timeout=15)
            if resp.status_code == 200:
                data = resp.json().get("scorers", [])
                scorers[league_name] = [
                    {"player": s.get("player", {}).get("name", ""),
                     "team": s.get("team", {}).get("name", ""),
                     "goals": s.get("goals"), "assists": s.get("assists"),
                     "played": s.get("playedMatches")}
                    for s in data[:15]
                ]
            time.sleep(0.6)
        except Exception as e:
            print(f"   射手榜({league_name}): error - {e}")
    return scorers


def fetch_rankings_data():
    """Aggregate standings + scorers for 排行榜 content generation."""
    print("[数据] 采集排行榜数据 (standings + scorers)...")
    standings = fetch_recent_standings()
    scorers = fetch_scorers()

    # Build combined rankings context
    rankings = {"standings": {}, "scorers": {}}
    for league, table in standings.items():
        rankings["standings"][league] = table[:10]  # top 10
    for league, top_scorers in scorers.items():
        rankings["scorers"][league] = top_scorers[:10]

    standings_count = len(rankings["standings"])
    scorers_count = len(rankings["scorers"])
    print(f"   积分榜: {standings_count} 联赛 | 射手榜: {scorers_count} 联赛")
    return rankings


# ============================================================
# Image Search
# ============================================================

def search_wikipedia(entity_name, lang="en"):
    images = []
    try:
        resp = requests.get(
            f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(entity_name)}",
            headers={"User-Agent": "WusongShuru/1.0"}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "originalimage" in data:
                images.append({"url": data["originalimage"]["source"], "source": "wikipedia",
                               "alt": data.get("title", entity_name)})
            elif "thumbnail" in data:
                images.append({"url": data["thumbnail"]["source"], "source": "wikipedia",
                               "alt": data.get("title", entity_name)})
    except Exception:
        pass
    return images


def search_footyrenders(keywords, count=5):
    """footyrenders 球员图（**已停用**，见 search_images 注释）。

    ▼ 2026-11-01 查实：`/cdn/players/<slug>.png` 返回的是 **50×50、约 2KB** 的占位缩略图，
    而保存侧 `ImageService.download_image` 要求 min 800×600 / 51200 字节 —— 候选进了池子
    也必然在下载环节被全部丢弃。历史上前端「有 3 张候选图，最后 0 张落盘」就是这么来的。

    同时修掉一处相关性 bug：原来抓 `?s=<slug>` 页面里的**全部** png，其中含站点侧栏的
    「热门球员」推荐位，代码直接 `[:count]` 取前几张 → 查 Messi 第一个返回 C 罗。
    现在按 slug 与检索词匹配后才收（万一以后换图源重启用，也不会再把无关球员当封面）。

    函数保留（多处 import 与单测引用），但 search_images 不再调用它。
    """
    images = []
    search_terms = set()
    for kw in keywords:
        kw_lower = kw.lower()
        for name_key, slug in FOOTYRENDERS_PLAYERS.items():
            if name_key in kw_lower:
                search_terms.add(slug)
    if not search_terms:
        return images
    for term in list(search_terms)[:2]:
        try:
            resp = requests.get(f"https://www.footyrenders.com/?s={requests.utils.quote(term)}",
                              headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if resp.status_code == 200:
                import re
                pngs = re.findall(r'src="(/cdn/players/[^"]+\.png)"', resp.text)
                for png in pngs:
                    if "1x1-pixel" in png:
                        continue
                    # 只收与检索词对得上的那张：页面里混着侧栏「热门球员」，不过滤就会
                    # 给「梅西」配上「C 罗」的图（实测发生过）。
                    if term not in png:
                        continue
                    url = f"https://www.footyrenders.com{png}"
                    if url not in [img["url"] for img in images]:
                        images.append({"url": url, "source": "footyrenders",
                                       "alt": term.replace("-", " ").title()})
        except Exception:
            pass
    return images[:count]


def _title_matches(name, title):
    """维基条目名与查询实体是否指同一件事（够用的粗判，不用消歧义库）。

    判据：去掉「足球俱乐部/俱乐部」这类通名后缀后，一方包含另一方即算匹配。
    这样 `国际米兰 → 国际米兰足球俱乐部`（重定向）能过，
    而 `国际米兰 → 2021年国际冠军杯`（搜索飘了）、`米兰 → 米兰`（城市条目）能拦住。
    """
    core = (name or "").strip().replace("足球俱乐部", "").replace("俱乐部", "")
    t = (title or "").strip()
    if not core or not t:
        return False
    return core in t or t in core


# 维基熔断：某些出口网络（如本地/部分 CI 区域）根本连不上维基，而 search_images
# 每篇要试 3 个实体 × 2 种语言 × 2 次请求 —— 每个请求 6s 超时，一篇就能白等一分多钟。
# 连续 2 次网络异常就整进程跳过维基，避免把批次拖到超时。
_WIKI_DOWN = {"fails": 0, "down": False}
WIKI_TRIP_AFTER = 2


def _wiki_note_fail():
    _WIKI_DOWN["fails"] += 1
    if _WIKI_DOWN["fails"] >= WIKI_TRIP_AFTER and not _WIKI_DOWN["down"]:
        _WIKI_DOWN["down"] = True
        print("   🖼️ 维基图源连续超时，本批次起跳过该图源（不影响其余链路）")


def _wiki_note_ok():
    _WIKI_DOWN["fails"] = 0


def _wiki_available():
    return not _WIKI_DOWN["down"]


def reset_wiki_breaker():
    """给测试与每批次开头重置用。"""
    _WIKI_DOWN["fails"] = 0
    _WIKI_DOWN["down"] = False


def search_wikipedia_entity(name, langs=("zh", "en"), timeout=6):
    """按任意实体名取维基配图（精确页 → 搜索兜底），返回至多 1 张。

    为什么需要它：原来只认 `WIKI_PLAYERS`(18 人) / `WIKI_TEAMS`(15 队) 两张手工表，
    实体不在表里就一路 0 图。而 `config/entity_map.json` 里有几百个中英文实体——
    拿它去搜维基，等于把图源覆盖率从「33 个实体」扩到「整个实体词典」。

    返回的 image dict 带 `source="wikipedia"` 与 `alt=<解析到的页名>`，供 cover_binding 判定；
    解析到的页名与查询名完全无关时（维基搜索会飘）直接丢弃，避免又造出「图文不符」。
    """
    if not name or not _wiki_available():
        return None
    for lang in langs:
        base = f"https://{lang}.wikipedia.org"
        headers = {"User-Agent": "WusongShuru/1.0 (football-auto-publish)"}
        title = None
        # 1) 先按精确页名试（中文实体多半能直接命中）
        try:
            resp = requests.get(f"{base}/api/rest_v1/page/summary/"
                                f"{requests.utils.quote(name)}",
                                headers=headers, timeout=timeout)
            if resp.status_code == 200:
                d = resp.json()
                if not d.get("disambiguation"):
                    title = d.get("title") or None
                    img = (d.get("originalimage") or d.get("thumbnail") or {}).get("source")
                    # 精确页也要校验页名相关性：维基里「米兰」可能是城市条目，
                    # 拿到的是大教堂照片而不是球队。重定向（国际米兰→国际米兰足球俱乐部）
                    # 因为核心名被包含，仍然放行。
                    if img and _title_matches(name, title or ""):
                        _wiki_note_ok()
                        return {"url": img, "source": "wikipedia",
                                "alt": title or name, "query": name}
        except Exception:
            _wiki_note_fail()
        # 2) 精确页取不到图 → 搜索 API 拿最相关条目再取摘要
        try:
            sr = requests.get(f"{base}/w/api.php", params={
                "action": "query", "list": "search", "srsearch": name,
                "format": "json", "srlimit": 1}, headers=headers, timeout=timeout)
            hits = ((sr.json().get("query") or {}).get("search") or []) if sr.status_code == 200 else []
            if not hits:
                continue
            cand = hits[0].get("title") or ""
            # 搜索会飘（拿队名搜到「某赛季」条目），页名与实体不沾边就不收
            if not _title_matches(name, cand):
                continue
            ss = requests.get(f"{base}/api/rest_v1/page/summary/"
                              f"{requests.utils.quote(cand)}",
                              headers=headers, timeout=timeout)
            if ss.status_code != 200:
                continue
            d = ss.json()
            img = (d.get("originalimage") or d.get("thumbnail") or {}).get("source")
            if img:
                return {"url": img, "source": "wikipedia",
                        "alt": d.get("title") or cand, "query": name}
        except Exception:
            continue
    return None


def extract_search_entities(topic):
    title = topic.get("title", "")
    keywords_cn = topic.get("keywords_cn", [])
    search_text = title + " " + " ".join(keywords_cn)
    players = []
    teams = []
    for cn_name, wiki_page in WIKI_PLAYERS.items():
        if cn_name in search_text:
            players.append({"cn": cn_name, "wiki": wiki_page})
            name_key = cn_name.lower()
            if name_key in FOOTYRENDERS_PLAYERS:
                players[-1]["fr_slug"] = FOOTYRENDERS_PLAYERS[name_key]
    for cn_name, wiki_page in WIKI_TEAMS.items():
        if cn_name in search_text:
            teams.append({"cn": cn_name, "wiki": wiki_page})
    filler = ["的", "了", "是", "在", "和", "也", "都", "就", "要", "会", "能", "不", "这", "那"]
    query_terms = [t.strip() for t in title.replace("？", " ").replace("！", " ").replace("：", " ").split()
                   if len(t.strip()) >= 2 and t.strip() not in filler]
    specific_query = " ".join(query_terms[:5]) if query_terms else title
    return players, teams, specific_query


def _gallery_pixabay(core: str, need: int, *, timeout: int = 12, accept=None) -> list:
    """Pixabay 图库检索（免费 5000 次/小时，注册即用**无需审核**）。

    只接受横向、且原始尺寸已过 800×600 的图——下载端有该闸门，尺寸不够的图
    取回来也会被拒，白白占用一次请求。``safesearch`` 固定 true：这是自媒体发布链路，
    不能出现任何需要人工复核的素材。

    ``accept`` 非空时还要过 alt 校验，但 **Pixabay 的元数据只有 tags**（"football,
    stadium, ball" 这类通用词，几乎不含球场名），所以带 accept 的检索通常一张都过不了。
    这不是 bug，是这条链路**校验不了**的诚实结论：Pixabay 只能当氛围图库，
    而计划 2.2 明确禁用随机图库配图 —— 所以它在实体图这条路上基本是死路，
    留给「Key 到手后实测 tags 到底装了什么」再定（当前 ⏸）。
    """
    if need <= 0:
        return []
    url = "https://pixabay.com/api/"
    if not PIXABAY_KEY:
        _diag.record("pixabay", url, kind="empty",
                     detail="未配置 PIXABAY_KEY（pixabay.com/account/api-key 注册即用，无需审核）")
        return []
    out = []
    rejected = 0
    try:
        resp = requests.get(url, params={
            "key": PIXABAY_KEY, "q": core, "image_type": "photo",
            "orientation": "horizontal", "per_page": max(3, need * 3),
            "safesearch": "true", "order": "popular"}, timeout=timeout)
    except Exception as e:
        _diag.record("pixabay", url, kind=_diag.classify_exception(e),
                     detail=f"{type(e).__name__}: {str(e)[:100]}")
        return []
    if resp.status_code != 200:
        _diag.record("pixabay", url, kind=_diag.classify_status(resp.status_code),
                     status=resp.status_code, detail=f"HTTP {resp.status_code}: {resp.text[:120]}")
        return []
    for h in (resp.json() or {}).get("hits", []):
        if len(out) >= need:
            break
        if int(h.get("imageWidth") or 0) < 800 or int(h.get("imageHeight") or 0) < 600:
            continue
        alt = (h.get("tags") or "")[:80]
        if accept and not _gq.is_accepted(alt, accept):
            rejected += 1
            continue
        u = h.get("largeImageURL") or h.get("webformatURL") or ""
        if u:
            out.append({"url": u, "source": "pixabay", "alt": alt, "query": core})
    _diag.record("pixabay", url, status=200, count=len(out),
                 detail=f"query={core[:50]}" + (f"；alt 未过校验 {rejected} 张" if rejected else ""))
    return out


def _gallery_pexels(core: str, need: int, *, timeout: int = 12, accept=None) -> list:
    """Pexels 图库检索（免费 200 次/小时，注册即用**无需审核**，2026-11-04 实测可用）。

    ``accept`` 是必须的：Pexels 对球队的召回只有一半能对上（16 支主流球队里
    alt 命中主场名的比率 0%~77%），而且失败方式是「看起来合理但是错的」——
    查国米返回诺坎普、查马竞返回阿根廷的球场。所以只放行 alt 里明确写了
    **该队主场名**的图（重音折叠后比对，``Bernabéu`` 要能被 ``bernabeu`` 命中）。
    """
    if need <= 0:
        return []
    url = "https://api.pexels.com/v1/search"
    if not PEXELS_KEY:
        _diag.record("pexels", url, kind="empty",
                     detail="未配置 PEXELS_KEY（pexels.com/api 注册即用，无需审核）")
        return []
    out = []
    rejected = 0
    try:
        resp = requests.get(url, params={"query": core, "per_page": max(5, need * 4),
                                         "orientation": "landscape", "size": "large"},
                            headers={"Authorization": PEXELS_KEY}, timeout=timeout)
    except Exception as e:
        _diag.record("pexels", url, kind=_diag.classify_exception(e),
                     detail=f"{type(e).__name__}: {str(e)[:100]}")
        return []
    if resp.status_code != 200:
        _diag.record("pexels", url, kind=_diag.classify_status(resp.status_code),
                     status=resp.status_code, detail=f"HTTP {resp.status_code}: {resp.text[:120]}")
        return []
    for p in (resp.json() or {}).get("photos", []):
        if len(out) >= need:
            break
        if int(p.get("width") or 0) < 800 or int(p.get("height") or 0) < 600:
            continue
        alt = (p.get("alt") or "")[:120]
        if accept and not _gq.is_accepted(alt, accept):
            rejected += 1
            continue
        src = (p.get("src") or {})
        u = src.get("large2x") or src.get("original") or src.get("large") or ""
        if u:
            out.append({"url": u, "source": "pexels", "alt": alt, "query": core})
    _diag.record("pexels", url, status=200, count=len(out),
                 detail=f"query={core[:50]}" + (f"；alt 未过校验 {rejected} 张" if rejected else ""))
    return out


def search_images(topic, count=5):
    """按正文实体找配图。顺序＝精确度从高到低，任一层凑够就停。

    2026-11-01 重构（体检结论：10 月已发布篇里 37% 是「无封面」发出的，无封面组聚合
    点击率 0.52%，有封面组 2.07%，差 4 倍）。原来图源只有三条，每条都断：
      ① WIKI_PLAYERS(18 人) / WIKI_TEAMS(15 队) 精确页 —— 覆盖面只有 33 个实体；
      ② footyrenders —— 实际返回 50×50 占位图，下载端 800×600 闸门必拒；
      ③ Unsplash / DuckDuckGo —— 前者无 Key、后者在 CI 出口 IP 常被挡。
    三者叠加 → 绝大多数文章「找不到图 → 不配图 → 无封面发布」。

    现在第 ① 层扩成「实体词典 → 维基搜索」，实体来源换成 cover_binding.article_entities
    （读 config/entity_map.json，几百个条目），footyrenders 停用。
    「宁可不配图也不放随机图库图」这条仍然守：只从实体出发，绝不用 football/stadium 这类通用词。

    ▼ 2026-11-04 再重构（「国内的免费图源」调研结论，见 docs/audit/2026-11-04-图源替代与审核分层.md）
    实测四条链路（本机直连 + 走代理各一次）：
      · zh.wikipedia / commons.wikimedia —— 超时（连不上，不是慢）
      · api.openverse.org —— 超时
      · api.unsplash.com —— 可达，但应用状态 In review，Access Key 401
      · api.pixabay.com 400（Key 无效＝域名通）/ api.pexels.com 401（同上）**两条都可达**
    结论：**国内没有对外开放 API 的免费正版图库**（图虫/沙沙野/百度图片都没有公开检索接口，
    爬取有版权与稳定性双重风险，不做）。真正可用的是下面这条，而且它原本就在手上：

    ⓿ **新闻源正文图**：media_scraper 抓直播吧战报时已经把正文 `<img>` 抽成了
      `fixture["source_images"]`，但只有 `_find_source_article` 那一条路径用过，
      其余 6 条生成路径全部丢弃 → 抓了等于没抓。这一层零网络、零 Key、图文天然一致
      （就是那场比赛的图），精度高于任何图库，所以排在最前。
      版权口径：引用新闻源配图＝转述性使用，与「随便找张足球图」性质不同；仍按
      `cover_binding.filter_bound_images` 做正文绑定复核。

    图库层顺序：Pixabay（5000/h 免审核）→ Pexels（200/h 免审核）→ Unsplash（等审核）。
    三家都要求 query 绑定正文实体，且都不在无 Key 时静默跳过——落 scraper_diagnostics。

    ▼ 2026-11-05 再改（用户要求「单篇文章至少 3 张相关配图」）
    上面这些层加起来，一篇稿子实测只能拿 0~1 张：Commons 本机不通、图库白名单只有
    10 支球队、球员零覆盖。所以新增一层**爬取**：
      ⓿' `news_images`：爬「标题命中正文实体」的新闻站报道，取正文容器里的图。
          实测 6 篇相关报道可稳定拿到 3 张 1280×853 的图，且图文天然同源。
    同时把 Commons 的实体遍历从 2 个放宽到 4 个（每实体只取还差的张数）。
    仍然守「宁可不配图也不放随机图」：没有实体就**不爬**，绝不会按标题词乱爬。
    """
    images = []
    keywords = list(topic.get("keywords", [])) if isinstance(topic, dict) else ["football"]
    en_keywords = [k for k in keywords if isinstance(k, str)
                   and not any("\u4e00" <= c <= "\u9fff" for c in k)]
    players, teams, _ = extract_search_entities(topic) if isinstance(topic, dict) else ([], [], "")
    # 实体在 ⓿'（新闻图爬取）就要用，提前到这里算；下面各层共用同一份
    ents = _cb.article_entities(topic) if isinstance(topic, dict) else []
    _title_terms = _cb.title_terms(topic.get("title", "")) if isinstance(topic, dict) else []

    def _push(img):
        if img and img.get("url") and img["url"] not in [i["url"] for i in images]:
            images.append(img)

    # ⓿ 新闻源正文图（零网络、零 Key、图文天然一致）
    src_imgs = topic.get("source_images") if isinstance(topic, dict) else None
    if src_imgs:
        for it in src_imgs:
            url = it if isinstance(it, str) else (it or {}).get("url", "")
            if not url or not str(url).startswith("http"):
                continue
            _push({"url": url, "source": "news_source",
                   "alt": (it or {}).get("alt", "") if isinstance(it, dict) else "",
                   "query": "source_article"})
            if len(images) >= count:
                break
        _diag.record("news_source", "zhibo8_report", status=200, count=len(images),
                     detail=f"复用战报正文图 {len(images)} 张")

    # ⓿' 相关新闻正文图（**爬取层**，2026-11-05 用户要求「单篇至少 3 张」）
    # 为什么排在图库前面：一篇报道本身就是「图 + 文」成套产出的，图与题材天然同源，
    # 精度高于任何通用图库；而免费图库在足球题材上根本没有球员肖像（行业结构问题）。
    # 边界收在 `news_images` 里：只爬「标题命中正文实体」的报道、只取正文容器里的图。
    if len(images) < count:
        try:
            import news_images as _nimg
            for img in _nimg.fetch(ents, need=count - len(images)):
                _push(img)
        except Exception as e:                 # 图源不是发布前置条件，绝不抛出
            _diag.record("news_body", "zhibo8", kind="parse",
                         detail=f"{type(e).__name__}: {str(e)[:100]}")

    # ① 手工表的精确页名（最高精度，命中即用）
    for p in players[:2]:
        for img in search_wikipedia(p["wiki"]):
            _push(img)
    for t in teams[:2]:
        for img in search_wikipedia(t["wiki"]):
            _push(img)

    # ② 实体词典 → 维基搜索（扩展面最大的那一层）
    # tried 只装「已经真的取到图」的实体：精确页查空的那个实体还要靠这一层补，
    # 否则「手工表里有名字但维基页没图」的实体就被白跳过了。
    if len(images) < count:
        got = [i for i in images]
        tried = set() if not got else {e for e in ents if any(e in (i.get("alt") or "") or e in (i.get("query") or "") for i in got)}
        for ent in ents:
            if len(images) >= count:
                break
            if ent in tried:
                continue
            _push(search_wikipedia_entity(ent))
            tried.add(ent)

    # ②' 维基共享（Commons）—— 与上面的 zh.wikipedia 是**两个不同的源**：
    #     wikipedia 取「条目页首图」（33 个手工实体 + 实体词典能命中才有），
    #     commons  按**文件**检索（球场、比赛现场、球员，量级大一个数量级）。
    #     它是足球领域唯一「免费 + 有真人照 + 可商用 + 有 API」的源（2026-11-04 实测：
    #     Pexels/Pixabay 是英文标签索引的通用图库，球员肖像根本不在库里，
    #     中文 query 还会返回酱油饺子）。详见 wiki_media.py 模块 docstring。
    #     合规由模块保证：只收 CC0/PD/CC BY/CC BY-SA、必须有作者（能署名）、
    #     ≥800×600、文件名/描述/分类命中实体；球员类还必须在 football 分类下。
    #     熔断：本机 *.wikimedia.org 全超时，连不上就停，别每篇白等 12 秒。
    if len(images) < count and _wiki_available():
        # 2026-11-05：原来只查前 2 个实体，一篇「队 + 球员 + 教练」的稿子只能拿到 1~2 张。
        # 改成最多 4 个实体，每个只取「还差的张数」——多查实体是为了覆盖率，
        # 不是为了让同一支球队堆三张一样的球场照（need 会自动收窄）。
        for ent in ents[:4]:
            if len(images) >= count or not _wiki_available():
                break
            try:
                got_c = _wm.search_for_entity(ent, count - len(images))
            except Exception as e:      # 模块内已 fail-closed，这里只兜未知异常
                _diag.record("wikimedia", "search_for_entity", kind="parse",
                             detail=f"{type(e).__name__}: {str(e)[:80]}")
                got_c = []
            if got_c:
                _wiki_note_ok()
            elif _wm.LAST.get("network"):
                # 区分「真没图」与「连不上」：连不上才熔断，取空不熔断
                _wiki_note_fail()
            for img in got_c:
                _push(img)

    # ③ 不做「标题词兜底」：没有实体时拿标题词去搜维基，几乎必然搜到无关条目
    #    （维基不是图库）。计划 2.2 的「宁可不配图也不放随机图」在这里优先于覆盖率。

    # 图库检索：Pixabay / Pexels / Unsplash 三家都是**英文标签索引**，中文 query 会
    # 返回完全无关的图（2026-11-04 实测：query="皇家马德里" 第一条是酱油饺子，
    # query="欧冠" 是欧元纸币，query="Haaland" 是奥地利的一个村庄）。
    # 所以这里不再用 build_bound_query 的中文结果，改走 gallery_query：
    #   · 只有登记在 config/gallery_entities.yaml、且实测确有该球场照片的球队才查；
    #   · 用英文队名/球场名检索，返回图还要 alt 命中该队主场名（重音折叠后比对）；
    #   · 不在白名单（球员 / 未登记球队 / 无实体）→ 不查，交给 cover_card 数据卡。
    # 「查了没结果」和「不该查」必须分开记，否则又变成没人能解释的黑箱。
    if len(images) < count:
        gal = _gq.resolve(ents)
        if not gal:
            _diag.record("gallery", "skipped", kind="empty", count=len(images),
                         detail=f"实体不在图库白名单 {ents[:3]}，不查英文图库 → 走数据卡")
        else:
            # Pixabay 停用（2026-11-04）：它的元数据只有 tags（"football, stadium, ball"），
            # 不含球场名/队名 → accept 校验必然全挂，等于每次都白花一个请求还永远取不到图。
            # 留着它只会让「配图率」看起来有一条链路，实际贡献 0。
            _diag.record("pixabay", "skipped", kind="empty",
                         detail="元数据只有 tags、无法做 alt 校验，2026-11-04 停用（函数保留）")
            for img in _gallery_pexels(gal["query"], count - len(images), accept=gal["accept"]):
                _push(img)

    if len(images) < count and UNSPLASH_KEY:
        # 同样只走白名单（Unsplash 也是英文索引）；校验字段是 description，
        # 两者都空 = 无法校验 = 不放行（与 Pexels 的口径一致）。
        gal = _gq.resolve(ents)
        if gal:
            for suffix in ("stadium", "football match"):
                if len(images) >= count:
                    break
                q = f"{gal['query']} {suffix}".strip()
                _un_url = "https://api.unsplash.com/search/photos"
                try:
                    resp = requests.get(_un_url, params={
                        "query": q, "per_page": max(5, (count - len(images)) * 4),
                        "orientation": "landscape", "client_id": UNSPLASH_KEY}, timeout=10)
                except Exception as e:
                    # 此前是 `except: pass` —— 图源静默死亡，没人知道。
                    # 2026-11-04 加 Key 时才发现 401 已经被吞了很久。
                    _diag.record("unsplash", _un_url, kind=_diag.classify_exception(e),
                                 detail=f"{type(e).__name__}: {str(e)[:100]}")
                    break
                if resp.status_code == 200:
                    _got = _rej = 0
                    for r in resp.json().get("results", []):
                        alt = f"{r.get('description') or ''} {r.get('alt_description') or ''}".strip()
                        if not _gq.is_accepted(alt, gal["accept"]):
                            _rej += 1
                            continue
                        images.append({"url": r["urls"]["regular"], "source": "unsplash",
                                       "alt": alt[:120], "query": q})
                        _got += 1
                        if len(images) >= count:
                            break
                    _diag.record("unsplash", _un_url, status=200, count=_got,
                                 detail=f"query={q[:50]}" + (f"；alt 未过校验 {_rej} 张" if _rej else ""))
                else:
                    _diag.record("unsplash", _un_url,
                                 kind=_diag.classify_status(resp.status_code),
                                 status=resp.status_code,
                                 detail=f"HTTP {resp.status_code}: {resp.text[:120]}")
                    if resp.status_code in _diag.BLOCKED_STATUS:
                        # 401/403 = Key 无效或无权限，换 suffix 只会再浪费一次配额
                        break

    # DuckDuckGo 图片搜索兜底。它和上面三家不同：**是中文可用的网页图片搜索**，
    # DuckDuckGo 图片层已删除（2026-11-04）：
    #   ① 本机走代理时 duckduckgo.com 直接超时（实测），CI 上也拿不到 vqd，
    #      等于每次白等 20 秒；
    #   ② 它返回的是任意中文站点的图，只有「标题命中实体」一道弱校验，
    #      既是版权风险也是配错图的主通道；
    #   ③ 同样是「拿不到正确图」的层，Commons 的精度和合规性都更好，保留它没有意义。
    #   若将来要恢复，必须先解决许可与校验两个问题，不能只把代码贴回来。

    # 绑定复核：剔除未与正文绑定的配图（随机图库配图在此被拦下）
    bound = _cb.filter_bound_images(images, ents, _title_terms + en_keywords)
    if len(bound) < len(images):
        print(f"   🖼️ 封面绑定：剔除 {len(images) - len(bound)} 张未绑定配图（计划 2.2 禁用随机图库）")
    if not bound:
        print("   🖼️ 封面绑定：未找到与正文绑定的配图，本篇不配随机图")
    return bound[:count]


# ============================================================
# Topic Selection & Article Generation
# ============================================================


# ============================================================
# Deterministic Fixture Supply (一级·确定性供给)
# ============================================================
# 赛程驱动的「冲突点选题」：提前 7 天可预生成，占内容供给 ~30%。
# 与 Pipeline A（scraper 改写）解耦——不依赖直播吧/懂球帝源文章，
# 因此可作为 scraper 失效时的可靠补充/兜底。
# 事实层（积分榜/射手榜）来自 football-data.org，表达层由 LLM 生成。
CST = timezone(timedelta(hours=8))

FIXTURE_POOL_PATH = PROJECT_ROOT / "data" / "fixtures" / "topic_pool.json"
ENTITY_MAP_PATH = PROJECT_ROOT / "config" / "entity_map.json"

# 本地缓存：en/别名(lower) → 中文名，供事实层 en→zh 映射
_ENTITY_ZH_CACHE = None


def load_entity_zh_map():
    """加载中文实体映射，返回 en/别名(lower) → name_zh 字典（带模块级缓存）。"""
    global _ENTITY_ZH_CACHE
    if _ENTITY_ZH_CACHE is not None:
        return _ENTITY_ZH_CACHE
    m = {}
    if ENTITY_MAP_PATH.exists():
        try:
            data = json.loads(ENTITY_MAP_PATH.read_text(encoding="utf-8"))
            for e in data.get("entities", {}).values():
                m[e["name_en"].lower()] = e.get("name_zh")
                for a in e.get("aliases", []):
                    m.setdefault(a.lower(), e.get("name_zh"))
        except Exception:
            pass
    _ENTITY_ZH_CACHE = m
    return m


def _zh(name_en):
    """英文队名/球员名 → 中文名（未知返回原值）。"""
    if not name_en:
        return name_en
    return load_entity_zh_map().get(name_en.strip().lower(), name_en)


def collect_fixture_topics(date_str, batch_mode="auto", max_topics=1,
                           lookahead_days=3, topic_history=None,
                           cross_batch_covered=None):
    """确定性供给：从 data/fixtures/topic_pool.json 读取赛程选题，筛选本批次可发布的。

    筛选规则：
      1. 时效闸门：publish_by（开赛时刻）必须晚于当前北京时间，过期=旧闻新发，打回。
      2. 临近窗口：开赛时刻必须在 [now, now+lookahead_days] 内，太远不急。
      3. 去重：与近 7 天已覆盖球队 / 今日已发 / 昨日已报 去重，避免重复同一场比赛。

    Returns:
        list[dict]: 赛程选题（含 home_zh/away_zh/competition/kickoff_cst/angle_type/
                    conflict/awareness_pair/suggested_column/fact_source/priority）。
    """
    if not FIXTURE_POOL_PATH.exists():
        print("   ℹ️ 赛程选题池不存在（未运行 fixture_library.py），跳过确定性供给")
        return []
    try:
        pool = json.loads(FIXTURE_POOL_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"   ⚠️ 赛程选题池解析失败: {e}")
        return []

    topics = pool.get("topics", [])
    if not topics:
        return []

    now = datetime.now(CST)
    horizon = now + timedelta(days=lookahead_days)

    # 去重基准：近 7 天已覆盖球队（topic_history["teams"]）+ 今日已发关键词
    hist_teams = set(topic_history.get("teams", set())) if topic_history else set()
    covered_kw = set(cross_batch_covered.get("keywords", set())) if cross_batch_covered else set()
    covered_titles = set(cross_batch_covered.get("titles", set())) if cross_batch_covered else set()
    covered_pairs = set(cross_batch_covered.get("fixture_pairs") or set()) if cross_batch_covered else set()

    candidates = []
    for t in topics:
        # 1. 时效闸门
        try:
            pub_by = datetime.strptime(t.get("publish_by", ""), "%Y-%m-%d %H:%M").replace(tzinfo=CST)
        except Exception:
            continue
        if pub_by <= now:
            continue
        if pub_by > horizon:
            continue

        home, away = t.get("home_zh", ""), t.get("away_zh", "")
        pair = frozenset([home, away])

        # 3. 去重：近 7 天已写过这两队之一 → 跳过（赛程稿主打冲突点，重发两队易撞车）
        if home in hist_teams or away in hist_teams:
            continue
        # 3a. 今日写过同一场（对阵级）→ 跳过。
        #     这是唯一与语言、措辞都无关的判据：2026-11-06 晨读写了
        #     「领跑，那不勒斯这一关」、午间又写「登顶遇上那不勒斯」，
        #     两篇 keywords 重叠 4/5 却没被拦住，因为已发侧存的是英文
        #     （lazio / napoli），候选侧是中文（拉齐奥 / 那不勒斯），
        #     `"napoli" in "那不勒斯"` 恒为 False。
        canon_pair = frozenset([team_names.canonical_team(home) or home,
                                team_names.canonical_team(away) or away])
        if len(canon_pair) == 2 and canon_pair in covered_pairs:
            continue
        # 3b. 今日已发同队关键词 → 跳过（跨语言：napoli 也要认出那不勒斯）
        forms = team_names.alias_forms(home) | team_names.alias_forms(away)
        if any(kw in forms for kw in covered_kw if kw):
            continue
        # 今日已发同标题（粗略：含两队名）→ 跳过
        if any((home in ti and away in ti) for ti in covered_titles if ti):
            continue

        candidates.append(t)

    candidates.sort(key=lambda x: -x.get("priority", 0))
    picked = candidates[:max_topics]
    if picked:
        print(f"   🗓️ 确定性供给候选（{len(picked)}/{len(candidates)} 通过筛选，窗口 {lookahead_days} 天）:")
        for t in picked:
            print(f"      [{t['priority']:5.1f}] {t['kickoff_cst'][:16]} {t['competition']:3s} "
                  f"{t['home_zh']} vs {t['away_zh']} <{t['angle_type']}>")
    else:
        print(f"   ℹ️ 确定性供给：本批次窗口内无可发布赛程选题（{len(topics)} 条中 "
              f"{sum(1 for t in topics if _within_horizon(t, now, horizon))} 条在窗口内但被去重/过期）")
    return picked


def _within_horizon(t, now, horizon):
    try:
        pub_by = datetime.strptime(t.get("publish_by", ""), "%Y-%m-%d %H:%M").replace(tzinfo=CST)
    except Exception:
        return False
    return now < pub_by <= horizon


def collect_recap_topics(date_str, max_topics=1, sent_match_ids=None,
                         covered_titles=None):
    """隔夜战报：从选题池 `recaps` 取「已完赛且赛果还新鲜」的场次（P0-1）。

    为什么单独一条通道：赛程池只收未开赛场次（旧闻新发红线），已完赛比分
    **没有任何入口** → 08:00 晨读批只能拿蓄水池软性内容补位（10-12~10-25：
    晨读 14 篇里 12 篇软性、0 篇战报），而「今晨刚踢完的比赛」本该是晨读最硬的一块。

    与 collect_fixture_topics 的去重口径**不同**，别混用：
      - 赛程稿按「近 7 天写过这两队就跳过」（同一场比赛不重复做前瞻）；
      - 战报按 **match_id** 去重（赛前分析 + 赛后复盘是两篇文章，不是重复）。
        用球队去重会把「昨天刚前瞻过的德比」这最值钱的战报直接枪毙。

    Returns:
        list[dict]: 战报选题（含 score/goals/winner_zh/goal_diff/late_goal 等事实层）。
    """
    if not FIXTURE_POOL_PATH.exists():
        print("   ℹ️ 赛程选题池不存在，跳过隔夜战报")
        return []
    try:
        pool = json.loads(FIXTURE_POOL_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"   ⚠️ 赛程选题池解析失败（隔夜战报跳过）: {e}")
        return []

    recaps = pool.get("recaps") or []
    if not recaps:
        print("   ℹ️ 选题池无已完赛场次（窗口内没有昨天的赛果），跳过隔夜战报")
        return []

    now = datetime.now(CST)
    sent_ids = set(str(x) for x in (sent_match_ids or ()))
    titles = [t for t in (covered_titles or ()) if t]

    picked_pool = []
    for r in recaps:
        mid = str(r.get("match_id") or "")
        if mid and mid in sent_ids:
            continue
        try:
            pub_by = datetime.strptime(r.get("publish_by", ""), "%Y-%m-%d %H:%M").replace(tzinfo=CST)
        except Exception:
            continue
        # 战报只能「赛后不久」发：过期即旧闻（计划 11.1 时效闸门）
        if pub_by <= now:
            continue
        # 同一对阵不重复做战报（同日多批次的兜底）。**别名感知**：
        # 标题常写「皇马2-1巴萨」，而队名全称是「皇家马德里／巴塞罗那」，
        # 纯子串匹配会漏掉，等于这道防线形同虚设。
        h_forms = [r.get("home_zh", "")] + list(r.get("home_aliases") or [])
        a_forms = [r.get("away_zh", "")] + list(r.get("away_aliases") or [])
        if any(any(h and h in ti for h in h_forms) and any(a and a in ti for a in a_forms)
               for ti in titles):
            continue
        picked_pool.append(r)

    picked_pool.sort(key=lambda x: -x.get("priority", 0))
    picked = picked_pool[:max_topics]
    if picked:
        print(f"   📰 隔夜战报候选（{len(picked)}/{len(picked_pool)} 通过时效/去重）:")
        for r in picked:
            sc = r.get("score") or {}
            print(f"      [{r['priority']:5.1f}] {r['competition']} "
                  f"{r['home_zh']} {sc.get('home')}-{sc.get('away')} {r['away_zh']}"
                  f"（{len(r.get('goals') or [])} 个进球，发布截止 {r['publish_by']}）")
    else:
        print(f"   ℹ️ 隔夜战报：{len(recaps)} 场赛果中无可用（已发/过期/同对阵去重）")
    return picked


def gather_fixture_facts(topic):
    """为赛程选题采集事实层（积分榜 + 射手榜），全部来自 football-data.org。

    失败则降级返回部分数据（非阻断）——确定性供给宁可少发也不编数据。
    队名/球员名做 en→zh 映射，便于 LLM 用中文表达。
    """
    comp_id = (topic.get("fact_source") or {}).get("fd_comp_id")
    comp_name = topic.get("competition", "")
    facts = {
        "competition": comp_name,
        "kickoff_cst": topic.get("kickoff_cst", ""),
        "home_zh": topic.get("home_zh", ""),
        "away_zh": topic.get("away_zh", ""),
        "conflict": topic.get("conflict", ""),
        "angle_type": topic.get("angle_type", ""),
        # 人视角叙事要求（R11 写人 > 写事）：随选题透传给生成侧提示词
        "angle_hint": topic.get("angle_hint", ""),
        "awareness_pair": topic.get("awareness_pair", ""),
        "standings": [],
        "scorers": [],
        "home_standing": None,
        "away_standing": None,
        "home_top_scorer": None,
        "away_top_scorer": None,
    }
    if not comp_id:
        return facts

    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    base = FOOTBALL_DATA_BASE

    # 积分榜
    try:
        r = requests.get(f"{base}/competitions/{comp_id}/standings", headers=headers, timeout=15)
        if r.status_code == 200:
            for s in r.json().get("standings", []):
                if s.get("type") == "TOTAL":
                    table = [{"position": x.get("position"),
                              "team_zh": _zh(x.get("team", {}).get("name", "")),
                              "team_en": x.get("team", {}).get("name", ""),
                              "points": x.get("points"),
                              "played": x.get("playedGames"),
                              "gd": x.get("goalDifference")}
                             for x in s.get("table", [])]
                    facts["standings"] = table
                    break
    except Exception as e:
        print(f"   ⚠️ 积分榜采集失败({comp_name}): {e}")

    # 射手榜
    try:
        r = requests.get(f"{base}/competitions/{comp_id}/scorers",
                         headers=headers, params={"limit": 15}, timeout=15)
        if r.status_code == 200:
            facts["scorers"] = [{"player_zh": _zh(x.get("player", {}).get("name", "")),
                                 "player_en": x.get("player", {}).get("name", ""),
                                 "team_zh": _zh(x.get("team", {}).get("name", "")),
                                 "goals": x.get("goals"),
                                 "assists": x.get("assists")}
                                for x in r.json().get("scorers", [])]
    except Exception as e:
        print(f"   ⚠️ 射手榜采集失败({comp_name}): {e}")

    # 抽取两队站位与队内射手王
    home_en = None
    away_en = None
    emap = load_entity_zh_map()
    for en, zh in emap.items():
        if zh == facts["home_zh"]:
            home_en = en
        if zh == facts["away_zh"]:
            away_en = en
    for row in facts["standings"]:
        if home_en and row.get("team_en", "").lower() == home_en:
            facts["home_standing"] = row
        if away_en and row.get("team_en", "").lower() == away_en:
            facts["away_standing"] = row
    for sc in facts["scorers"]:
        if home_en and sc.get("team_en", "").lower() == home_en and not facts["home_top_scorer"]:
            facts["home_top_scorer"] = sc
        if away_en and sc.get("team_en", "").lower() == away_en and not facts["away_top_scorer"]:
            facts["away_top_scorer"] = sc

    return facts

