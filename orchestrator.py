#!/usr/bin/env python3
"""足球自媒体 - 文章生成编排器 (独立版，无 Flask 依赖)

Usage: python orchestrator.py [YYYY-MM-DD]
"""

import os, json, sys, subprocess, requests, time, re, signal
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

from file_writer import FileWriter
from image_service import ImageService
from hupu_scraper import HupuScraper

# --- Config ---
PROJECT_ROOT = Path(__file__).parent
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "output"))
GZH_SCRIPT = str(PROJECT_ROOT / "skills" / "gzh-explosive-content-detector" / "scripts" / "fetch_gzh_trends.py")

# API keys from env (GitHub Secrets)
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DASHSCOPE_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
UNSPLASH_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"

# WxPusher
WXPUSHER_APPTOKEN = os.environ.get("WXPUSHER_APPTOKEN", "")
WXPUSHER_UID = os.environ.get("WXPUSHER_UID", "")


def send_wxpusher(title, content):
    if not WXPUSHER_APPTOKEN or not WXPUSHER_UID:
        return
    try:
        requests.post(
            "https://wxpusher.zjiecode.com/api/send/message",
            json={"appToken": WXPUSHER_APPTOKEN, "content": f"{title}\n\n{content}",
                  "contentType": 1, "uids": [WXPUSHER_UID]},
            timeout=10,
        )
    except Exception:
        pass


def retry(func, *args, max_retries=3, base_delay=2, desc="API", **kwargs):
    last_err = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            if isinstance(e, requests.exceptions.HTTPError):
                status = e.response.status_code if hasattr(e, 'response') and e.response is not None else None
                if status in (403, 404):
                    raise
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"   [{desc}] 重试 {attempt+1}/{max_retries} (等待{delay}s): {e}")
                time.sleep(delay)
    raise last_err


def call_llm(url, api_key, model, messages, temperature=0.7, max_tokens=4096, timeout=120):
    def _call():
        resp = requests.post(url, json={
            "model": model, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "stream": False
        }, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    return retry(_call, desc=f"LLM({model})")


def safe_json_loads(text):
    import re
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return json.loads(text, strict=False)
        except json.JSONDecodeError:
            fixed = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', lambda m: f'\\u{ord(m.group(0)):04x}', text)
            return json.loads(fixed)


# ============================================================
# Data Collection
# ============================================================

COMPETITION_IDS = {
    "英超": 2021, "西甲": 2014, "意甲": 2019, "德甲": 2002, "法甲": 2015, "欧冠": 2001,
}

GZH_KEYWORD_GROUPS = [
    "足球",
    "英超,欧冠,转会",
    "梅西,C罗,姆巴佩,哈兰德,内马尔,萨拉赫",
    "足球,冲突,争议,红牌,绝杀,逆转",
    "转会,签约,续约,离队,绯闻,花边,冲突,下课",
]

# Transfer/Gossip-focused keywords — used when user wants only transfer/rumor content
GZH_TRANSFER_KEYWORDS = [
    "足球转会,重磅签约,天价转会",
    "梅西,C罗,姆巴佩,哈兰德,内马尔,转会绯闻",
    "足球,下课,换帅,新任主帅",
    "续约,离队,解约金,免签",
    "球员花边,场外新闻,女友,冲突",
    "足球八卦,转会流言,传闻",
]

GZH_NOISE_PATTERNS = [
    "三角洲", "实况足球", "FIFA", "足球经理", "FM", "梦幻足球",
    "乒乓球", "樊振东", "孙颖莎", "王楚钦", "马龙", "国乒",
    "和平精英", "王者荣耀", "英雄联盟", "LPL",
]

WIKI_PLAYERS = {
    "姆巴佩": "Kylian_Mbappé", "梅西": "Lionel_Messi", "C罗": "Cristiano_Ronaldo",
    "c罗": "Cristiano_Ronaldo", "哈兰德": "Erling_Haaland", "内马尔": "Neymar",
    "萨拉赫": "Mohamed_Salah", "德布劳内": "Kevin_De_Bruyne",
    "贝林厄姆": "Jude_Bellingham", "维尼修斯": "Vinícius_Júnior",
    "孙兴慜": "Son_Heung-min", "凯恩": "Harry_Kane",
    "莱万": "Robert_Lewandowski", "莫德里奇": "Luka_Modrić",
    "帕尔默": "Cole_Palmer", "福登": "Phil_Foden", "亚马尔": "Lamine_Yamal", "穆夏拉": "Jamal_Musiala",
}
WIKI_TEAMS = {
    "阿森纳": "Arsenal_F.C.", "曼城": "Manchester_City_F.C.", "利物浦": "Liverpool_F.C.",
    "曼联": "Manchester_United_F.C.", "切尔西": "Chelsea_F.C.", "热刺": "Tottenham_Hotspur_F.C.",
    "巴萨": "FC_Barcelona", "皇马": "Real_Madrid_CF", "马竞": "Atlético_Madrid",
    "拜仁": "FC_Bayern_Munich", "多特": "Borussia_Dortmund", "国米": "Inter_Milan",
    "AC米兰": "AC_Milan", "尤文": "Juventus_FC", "巴黎": "Paris_Saint-Germain_F.C.",
}
FOOTYRENDERS_PLAYERS = {
    "messi": "lionel-messi", "ronaldo": "cristiano-ronaldo", "mbappe": "kylian-mbappe",
    "haaland": "erling-braut-haaland", "neymar": "neymar-jr", "salah": "mohamed-salah",
    "debruyne": "kevin-de-bruyne", "bellingham": "jude-bellingham",
    "vinicius": "vinicius-junior", "vini": "vinicius-junior",
}


def collect_real_matches(date_str):
    print(f"[1/5] 采集真实比赛数据 ({date_str})...")
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
            time.sleep(0.3)
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
                   "Ligue 1", "UEFA Champions League", "Campeonato Brasileiro Série A"}
    for m in unique:
        comp = m.get("competition", {}).get("name", "")
        if comp in valid_comps:
            relevant.append(m)
            score = m.get("score", {}).get("fullTime", {})
            fixture_details.append({
                "league": comp, "home_team": m.get("homeTeam", {}).get("name", ""),
                "away_team": m.get("awayTeam", {}).get("name", ""),
                "home_score": score.get("home"), "away_score": score.get("away"),
                "status": m.get("status"), "matchday": m.get("matchday"),
                "utc_date": m.get("utcDate", ""),
            })

    by_league = defaultdict(list)
    for f in fixture_details:
        by_league[f["league"]].append(f)
    print(f"   {len(relevant)} 场比赛 ({len(by_league)} 个联赛)")

    standings = {}
    sid_map = {"Premier League": 2021, "Primera Division": 2014, "Serie A": 2019,
               "Bundesliga": 2002, "Ligue 1": 2015, "Campeonato Brasileiro Série A": 2013}
    for comp_name, comp_id in sid_map.items():
        if comp_name in by_league:
            try:
                resp = requests.get(f"{FOOTBALL_DATA_BASE}/competitions/{comp_id}/standings",
                                   headers=headers, timeout=10)
                if resp.status_code == 200:
                    for s in resp.json().get("standings", []):
                        if s.get("type") == "TOTAL":
                            standings[comp_name] = [{"position": r.get("position"),
                                "team": r.get("team", {}).get("name", ""), "points": r.get("points"),
                                "played": r.get("playedGames"), "goal_diff": r.get("goalDifference")}
                                for r in s.get("table", [])]
            except Exception:
                pass

    return {"date": date_str, "total_matches": len(relevant),
            "fixtures_by_league": dict(by_league), "all_fixtures": fixture_details, "standings": standings}


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
    history = {"titles": set(), "keywords": set(), "teams": set(), "players": set(), "content_types": []}
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
                for kw in a.get("keywords", []):
                    history["keywords"].add(kw.lower())
                for tag in a.get("tags", []):
                    history["keywords"].add(tag.lower())
                for team in WIKI_TEAMS:
                    if team in title:
                        history["teams"].add(team)
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
              f"覆盖球队 {len(history['teams'])} 支, 球员 {len(history['players'])} 人")
    return history


def fetch_gzh_football_trends(date_str, keyword_groups=None):
    print(f"[数据] 从公众号爆款库采集足球话题 ({date_str})...")
    target_date = datetime.strptime(date_str, "%Y-%m-%d")
    start_date = (target_date - timedelta(days=2)).strftime("%Y-%m-%d")
    all_raw = []
    kw_groups = keyword_groups if keyword_groups is not None else GZH_KEYWORD_GROUPS

    for kw in kw_groups:
        try:
            safe_name = re.sub(r'[^a-zA-Z0-9_一-鿿]', '_', kw)[:30]
            cmd = [sys.executable, GZH_SCRIPT, "--keyword", kw, "--start-date", start_date,
                   "--output-format", "json", "--output-file", f"/tmp/gzh_{safe_name}.json"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                output_file = f"/tmp/gzh_{safe_name}.json"
                if os.path.exists(output_file):
                    for item in json.loads(Path(output_file).read_text()).get("items", []):
                        if _is_football_relevant(item):
                            all_raw.append(item)
        except Exception as e:
            print(f"   搜索'{kw[:20]}'失败: {e}")

    if not all_raw:
        print("   公众号爆款库未找到足球相关文章")
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

    print(f"   采集到 {len(unique)} 篇真实足球爆款文章")
    for i, a in enumerate(unique[:10]):
        print(f"   {i+1}. [{a.get('clicksCount', '?')}阅读] {a.get('title', '')[:60]} — {a.get('accountName', '?')}")
    return unique


def fetch_recent_standings():
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    sid_map = {"Premier League": 2021, "Primera Division": 2014, "Serie A": 2019,
               "Bundesliga": 2002, "Ligue 1": 2015}
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
            time.sleep(0.3)
        except Exception:
            pass
    return standings


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
                    url = f"https://www.footyrenders.com{png}"
                    if url not in [img["url"] for img in images]:
                        images.append({"url": url, "source": "footyrenders", "alt": term.replace("-", " ").title()})
        except Exception:
            pass
    return images[:count]


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


def search_images(topic, count=5):
    images = []
    keywords = list(topic.get("keywords", [])) if isinstance(topic, dict) else ["football"]
    en_keywords = [k for k in keywords if isinstance(k, str) and not any('一' <= c <= '鿿' for c in k)]
    players, teams, _ = extract_search_entities(topic) if isinstance(topic, dict) else ([], [], "")

    for p in players[:2]:
        for img in search_wikipedia(p["wiki"]):
            if img["url"] not in [i["url"] for i in images]:
                images.append(img)
    for t in teams[:2]:
        for img in search_wikipedia(t["wiki"]):
            if img["url"] not in [i["url"] for i in images]:
                images.append(img)
    if players:
        for img in search_footyrenders(keywords, count=3):
            if img["url"] not in [i["url"] for i in images]:
                images.append(img)

    if len(images) < count and UNSPLASH_KEY:
        core = " ".join(en_keywords[:3]) if en_keywords else "football"
        for q in [f"{core} football match action", f"{core} soccer", "football match stadium"]:
            if len(images) >= count:
                break
            try:
                resp = requests.get("https://api.unsplash.com/search/photos", params={
                    "query": q, "per_page": count - len(images), "orientation": "landscape",
                    "client_id": UNSPLASH_KEY}, timeout=10)
                if resp.status_code == 200:
                    for r in resp.json().get("results", []):
                        images.append({"url": r["urls"]["regular"], "source": "unsplash",
                                       "alt": r.get("description") or q})
            except Exception:
                pass

    if len(images) == 0 and UNSPLASH_KEY:
        try:
            resp = requests.get("https://api.unsplash.com/search/photos", params={
                "query": "football", "per_page": count, "orientation": "landscape",
                "client_id": UNSPLASH_KEY}, timeout=10)
            if resp.status_code == 200:
                for r in resp.json().get("results", []):
                    images.append({"url": r["urls"]["regular"], "source": "unsplash", "alt": "football"})
        except Exception:
            pass
    return images[:count]


# ============================================================
# Topic Selection & Article Generation
# ============================================================

def select_topics(match_data, gzh_articles=None, topic_history=None):
    print("\n[2/5] LLM 话题筛选 (DeepSeek)...")
    lines = []
    for league, matches in sorted(match_data.get("fixtures_by_league", {}).items()):
        lines.append(f"\n## {league}")
        for m in matches:
            hg, ag = m.get("home_score"), m.get("away_score")
            lines.append(f"  {m['home_team']} {hg}-{ag if hg is not None else 'vs'} {m['away_team']}")

    gzh_text = ""
    if gzh_articles:
        gzh_text = "\n## 当前公众号爆款文章（了解热点方向，不可照搬）\n"
        for a in gzh_articles[:8]:
            gzh_text += f"- [{a.get('clicksCount', '?')}阅读] {a.get('title', '')[:60]} — {a.get('accountName', '?')}\n"

    history_text = ""
    if topic_history and (topic_history.get("titles") or topic_history.get("teams") or topic_history.get("players")):
        history_text = "\n## ⚠️ 过去7天已报道（必须避开，不可重复）\n"
        if topic_history.get("titles"):
            sampled = list(topic_history["titles"])[:6]
            history_text += "已写标题: " + " | ".join(sampled) + "\n"
        if topic_history.get("teams"):
            history_text += "已覆盖球队: " + ", ".join(sorted(list(topic_history["teams"])[:10])) + "\n"
        if topic_history.get("players"):
            history_text += "已覆盖球员: " + ", ".join(sorted(list(topic_history["players"])[:10])) + "\n"

    prompt = f"""你是头条号足球博主"球评人老六"。以下是 {match_data['date']} 的真实比赛结果。请筛选 3 个有爆款潜力的话题。

比赛数据：
{"".join(lines)}
{gzh_text}
{history_text}

硬性要求 — 3 个话题必须覆盖不同内容类型：
1. 第1篇：比赛复盘型 — 从当日比赛中选最有话题性的一场
2. 第2篇：转会八卦型/争议观点型 — 转会传闻、球员花边、冲突争议、场外话题
3. 第3篇：人物故事型/趋势解读型 — 球员故事、战术趋势、数据洞察

如果当日有绝杀、逆转、红牌、VAR争议、教练冲突等事件，优先选择。

风格要求：像老球迷喝酒聊天一样自然，有明确立场和情绪，不骑墙、不套模板。
避免：任何过去7天已报道过的球队/球员/话题。

输出纯JSON数组：
[{{"title": "标题(15-25字)", "angle": "切入角度+明确态度", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "content_type": "比赛复盘型/转会八卦型/争议观点型/人物故事型/趋势解读型", "score": 90, "controversy_level": "high/medium/low", "target_emotion": "愤怒/骄傲/怀旧/震惊/感动/好奇", "why_pick": "为什么选这个角度(20字)"}}]
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是头条号足球博主'球评人老六'，有态度、有人味、不骑墙。严格按要求分配3种内容类型，避开历史话题。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-flash", messages, temperature=0.7, max_tokens=4096)
    topics = safe_json_loads(response)
    print(f"   筛选出 {len(topics)} 个话题:")
    for i, t in enumerate(topics):
        print(f"   {i+1}. [{t.get('content_type', 'N/A')}] {t['title'][:50]}")
    return topics


def collect_real_gzh_topics(date_str, topic_history=None, topic_preference="auto"):
    raw_articles = fetch_gzh_football_trends(
        date_str,
        keyword_groups=GZH_TRANSFER_KEYWORDS if topic_preference == "transfer" else None
    )
    if not raw_articles:
        print("   ERROR: 无真实数据源")
        return [], []
    standings = fetch_recent_standings()

    articles_text = [{"id": i+1, "title": a.get("title", "")[:80],
                       "summary": (a.get("summary", "") or "")[:120],
                       "account": a.get("accountName", "?"),
                       "reads": a.get("clicksCount", "?"), "likes": a.get("likeCount", 0),
                       "data_score": a.get("dataScore", 0),
                       "pub_time": (a.get("publicTime") or "")[:10]}
                      for i, a in enumerate(raw_articles[:15])]

    # Sort by recency: prefer articles from last 24h, then by data score
    today_str = date_str
    yesterday_str = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    for a in articles_text:
        pt = a.get("pub_time", "")
        if pt == today_str:
            a["_recency_score"] = 100
        elif pt == yesterday_str:
            a["_recency_score"] = 80
        elif pt and pt >= (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%d"):
            a["_recency_score"] = 40
        else:
            a["_recency_score"] = 0
    articles_text.sort(key=lambda x: (x["_recency_score"], x.get("data_score", 0)), reverse=True)

    history_text = ""
    if topic_history and (topic_history.get("titles") or topic_history.get("teams")):
        history_text = "\n⚠️ 过去7天已报道（必须避开）:\n"
        if topic_history.get("titles"):
            history_text += "已写: " + " | ".join(list(topic_history["titles"])[:5]) + "\n"

    # Build prompt based on preference
    if topic_preference == "transfer":
        type_requirement = "3个选题必须全部是转会/签约/续约/离队/绯闻/花边/下课类型。优先选择热度最高、最有话题性的。"
        system_msg = "你是头条号足球博主'球评人老六'，有态度有人味。绝不洗稿，跨源合成+新观点=全新原创。全部选题聚焦转会八卦/球员花边/场外话题。只输出JSON。"
    else:
        type_requirement = """硬性要求 — 3个选题必须覆盖不同内容类型：
1. 第1篇：比赛复盘/赛后争议型
2. 第2篇：转会八卦/球员花边/场外话题型
3. 第3篇：人物故事/战术趋势/数据洞察型"""
        system_msg = "你是头条号足球博主'球评人老六'，有态度有人味。绝不洗稿，跨源合成+新观点=全新原创。严格按3种内容类型分配。只输出JSON。"

    print(f"\n[2/5] 基于真实爆款数据筛选选题 (DeepSeek, mode={topic_preference})...")
    prompt = f"""你是头条号足球博主"球评人老六"。以下是公众号平台最近2天真实爆款足球文章数据，请从中选出3个最有二次创作价值的选题。

真实爆款文章数据（已按时效性+热度排序，pub_time为发布日期）：
{json.dumps(articles_text, ensure_ascii=False)}
{history_text}

⚠️ 时效性硬性要求：只能选择 pub_time 为 {yesterday_str} 或 {today_str} 的话题。超过2天的旧闻一律不用。

{type_requirement}

二次创作原则：借话题方向，不借标题和内容。用新角度、新观点、新表达重写。绝不可照搬原文标题或金句。

输出纯JSON：
[{{"title": "新标题(15-25字)", "source_article_ids": [引用文章id], "source_titles": ["原文标题"], "angle": "切入角度+新观点", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "content_type": "转会八卦型/争议观点型/人物故事型/趋势解读型", "controversy_level": "high/medium/low", "target_emotion": "愤怒/骄傲/怀旧/震惊/感动/好奇"}}]
只输出JSON。"""

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-flash", messages, temperature=0.6, max_tokens=4096)
    topics = safe_json_loads(response)
    print(f"   筛选出 {len(topics)} 个选题（全部来自真实爆款）:")
    for i, t in enumerate(topics):
        srcs = t.get("source_titles", ["?"])
        print(f"   {i+1}. [{t.get('content_type', 'N/A')}] {t['title'][:45]} [引用: {srcs[0][:25]}...]")

    raw_map = {a.get("id"): a for a in articles_text}
    for t in topics:
        ids = t.get("source_article_ids", [])
        t["_source_articles"] = [raw_map.get(sid, {}) for sid in ids]
        t["_all_articles"] = articles_text[:10]
        t["_standings"] = standings
    return topics, raw_articles


def generate_article(topic, match_context, index, gzh_articles=None, temperature=0.8, retry_hint=""):
    content_type = topic.get("content_type", "比赛复盘")
    print(f"\n[3.{index}] [{content_type}] {topic['title'][:40]}...")

    fixtures = match_context.get("fixtures_by_league", {})
    standings = match_context.get("standings", {})

    # Only provide standings data for match analysis types
    is_match_type = "比赛复盘" in content_type or "分析" in content_type
    if is_match_type:
        context_str = json.dumps({
            "date": match_context["date"],
            "matches": fixtures,
            "standings": {k: v[:6] for k, v in standings.items()},
        }, ensure_ascii=False)
    else:
        context_str = json.dumps({
            "date": match_context["date"],
            "matches": fixtures,
        }, ensure_ascii=False)

    gzh_text = ""
    if gzh_articles:
        gzh_text = "\n## 当前公众号爆款文章（了解热点语境，不可照搬）\n"
        for a in gzh_articles[:6]:
            gzh_text += f"- [{a.get('clicksCount', '?')}阅读] {a.get('title', '')[:60]}\n"

    # Style guidance by content type
    style_guide = {
        "比赛复盘型": "像赛后和球友喝酒复盘：先讲最刺激的瞬间，再拆关键战术细节，最后给个痛快结论。少列数据，多讲故事和感受。",
        "转会八卦型": "像球迷群里的八卦消息：分析转会的「为什么」和「影响」，结合球队需求和球员处境。有趣味但不编造，有逻辑但不学术。",
        "争议观点型": "像一个敢说真话的老球迷：开篇就亮态度，不怕得罪人，但每条观点都有事实支撑。可以情绪化但不能无理取闹。",
        "人物故事型": "像给朋友讲一个你佩服的球员：有细节、有情感、有画面感。不写流水账履历，聚焦一个侧面或瞬间。",
        "趋势解读型": "像老球皮分析联赛走势：从现象中提炼规律，用一两组关键数据说话，但不过度堆数据。让读者看完有「原来如此」的感觉。",
    }
    style = style_guide.get(content_type, "口语化+专业深度，短句为主，有明确立场。像朋友聊天一样自然。")

    # Retry hint: inject failure feedback to force improvement
    retry_block = ""
    if retry_hint:
        retry_block = f"""
⚠️ 上次生成失败！问题：{retry_hint}
这次必须修正上述所有问题。正文至少800字，至少3个##小标题，文末至少3个配图标记。"""

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。创作一篇完全原创的足球文章。

今日话题：{topic['title']}
切入角度：{topic['angle']}
内容类型：{content_type}
目标情绪：{topic.get('target_emotion', '好奇')}

真实数据（只能使用以下提供的，不可编造）：
{context_str[:3000]}
{gzh_text}
{retry_block}

写作要求：
{style}

结构：开篇钩子（制造悬念或情绪冲击）→ 2-3个小节展开 → 高潮观点/金句 → 收尾互动

硬性规范：
- 正文 800-1500 字（这是硬性要求，不是建议！低于800字视为不合格）
- 必须包含 ≥3 个 ## 二级标题
- 文末必须包含3张配图标记：![配图1](images/article-{index}-img-001.jpg) 等
- 真实性红线：只能使用提供的比赛数据和事实，禁止编造"内部消息""知情人士透露"
- 如果内容类型是转会八卦/花边，必须注明基于已有公开报道的推测

禁用词：震惊、吓尿、哭惨、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说
禁用模式：不要每段都以"老六认为"开头，不要像写论文一样列一二三四

输出JSON:
{{"title": "标题(15-25字，有话题性，不标题党)", "content": "Markdown正文(800-1500字，含≥3个##小标题，文末含3个配图标记)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_bait": "互动问题", "content_type": "{content_type}"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": f"你是头条号足球博主'球评人老六'，10万粉丝。风格：{style} 严格基于真实数据，不编造。用自然口语化中文写作，有态度有人味。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-pro", messages, temperature=temperature, max_tokens=8192)
    article = safe_json_loads(response)
    print(f"   标题: {article.get('title','?')}, 正文: {len(article.get('content',''))}字")
    return article


def generate_gossip_article(topic, index, temperature=0.8, retry_hint=""):
    content_type = topic.get("content_type", "趋势解读")
    print(f"\n[3.{index}] [跨源-{content_type}] {topic['title'][:40]}...")

    sources = topic.get("_source_articles", [])
    all_articles = topic.get("_all_articles", [])

    sources_text = ""
    for i, s in enumerate(sources):
        sources_text += f"\n来源{i+1}：{s.get('title', '')[:60]}\n  账号：{s.get('account', '?')} | 阅读：{s.get('reads', '?')}\n"

    bg_text = "".join(f"- [{a.get('reads', '?')}阅读] {a.get('title', '')[:60]}\n"
                      for a in all_articles[:8])

    # Style guidance by content type
    style_guide = {
        "转会八卦型": "像球迷群里的八卦：分析转会为什么发生、对各方的影响。有趣的推测但不编造事实，有逻辑但不写学术论文。如有多个信源可交叉印证。",
        "争议观点型": "像一个敢说真话的老球迷：开篇直接亮态度，有事实支撑。可以情绪化但不能无理取闹，可以从多角度呈现争议。",
        "人物故事型": "像讲述一个你佩服（或不爽）的球员：聚焦一个侧面、一段经历、一个瞬间。有细节、有情感、有画面。不写流水账。",
        "趋势解读型": "像老球皮分析足坛走向：从现象中提炼规律，用关键事实说话。让读者看完有「原来如此」的感觉。",
    }
    style = style_guide.get(content_type, "口语化+专业深度，短句为主，有明确立场。像朋友聊天一样自然。")

    # Retry hint: inject failure feedback
    retry_block = ""
    if retry_hint:
        retry_block = f"""
⚠️ 上次生成失败！问题：{retry_hint}
这次必须修正上述所有问题。正文至少800字，至少3个##小标题，文末至少3个配图标记。"""

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。基于真实爆款数据，二次创作一篇完全原创的足球文章。

话题方向（了解当前热点，不可照搬）：
{sources_text}

同期语境：
{bg_text}

内容类型：{content_type}
切入角度：{topic.get('angle', '独特角度')}
{retry_block}

二次创作约束：
- 借话题方向，不借标题和内容。新角度、新观点、新表达。
- 绝不可照搬参考文章的任何完整句子或金句
- 只使用提供的公开事实，不可虚构"内部消息"
- 时效性红线：只能写最近1-2天发生的事件。如果素材中有旧闻，必须找到最新的关联角度切入，不可写成"回顾历史"类文章

写作风格：
{style}

硬性规范：
- 正文 800-1500 字（这是硬性要求，不是建议！低于800字视为不合格）
- 必须包含 ≥3 个 ## 二级标题
- 文末必须包含3张配图标记：![配图1](images/article-{index}-img-001.jpg) 等

禁用词：震惊、吓尿、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说
禁用模式：不要列一二三四，不要太强的论文感

输出JSON:
{{"title": "标题(15-25字，有话题性，不标题党)", "content": "Markdown正文(800-1500字，含≥3个##小标题，文末含3个配图标记)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_bait": "互动问题", "content_type": "{content_type}", "sources_used": ["来源文章标题"], "originality_note": "如何区别于原文(20字)"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": f"你是头条号足球博主'球评人老六'，有态度有人味。跨源合成：多源事实+自己观点=全新原创，绝不洗稿。风格：{style} 用自然口语化中文写作。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-pro", messages, temperature=temperature, max_tokens=8192)
    article = safe_json_loads(response)
    print(f"   标题: {article.get('title','?')}, 正文: {len(article.get('content',''))}字")
    return article


# ============================================================
# Quality Validation & Retry
# ============================================================

def validate_article(article, index):
    """Validate article quality. Returns (is_valid, issues_list)."""
    issues = []
    content = article.get("content", "")
    title = article.get("title", "")

    if not title or len(title) < 10:
        issues.append(f"标题过短({len(title)}字,需≥10)")
    elif len(title) > 32:
        issues.append(f"标题过长({len(title)}字,需≤32)")

    if not content or len(content) < 500:
        issues.append(f"正文字数不足({len(content)}字,需≥500)")

    h2_count = len(re.findall(r'^## ', content, re.MULTILINE))
    if h2_count < 2:
        issues.append(f"缺少小标题(仅{h2_count}个##,需≥2)")

    img_count = len(re.findall(r'!\[.*?\]\(images/', content))
    if img_count < 3:
        issues.append(f"配图标记不足({img_count}个,需≥3)")

    if content.strip() == "":
        issues.append("正文为空")

    return len(issues) == 0, issues


def generate_article_with_retry(topic, match_context, index, gzh_articles=None,
                                is_gossip=False, is_tieba=False, tieba_context=None,
                                max_retries=2):
    """Generate article with validation and automatic retry on failure.

    On retry, progressively lowers temperature and strengthens the prompt
    to force longer, more structured output. Also detects short raw responses
    before JSON parsing to fail fast.
    """
    last_issues = ""
    for attempt in range(max_retries + 1):
        temp = max(0.3, 0.8 - attempt * 0.2)  # 0.8 → 0.6 → 0.4
        try:
            if is_tieba:
                art = generate_tieba_article(topic, index, tieba_context,
                                             temperature=temp, retry_hint=last_issues)
            elif is_gossip:
                art = generate_gossip_article(topic, index, temperature=temp,
                                              retry_hint=last_issues)
            else:
                art = generate_article(topic, match_context, index, gzh_articles,
                                       temperature=temp, retry_hint=last_issues)

            # Check raw content sanity before full validation
            content = art.get("content", "")
            if len(content) < 200 and attempt < max_retries:
                print(f"   ⚠️  正文过短({len(content)}字)，直接重试")
                last_issues = f"上次正文仅{len(content)}字，远低于800字最低要求。请大幅扩展内容。"
                continue

            is_valid, issues = validate_article(art, index)
            if is_valid:
                if attempt > 0:
                    print(f"   ✅ 第{attempt+1}次尝试通过验证")
                return art, None

            print(f"   ⚠️  第{attempt+1}次验证失败: {'; '.join(issues)}")
            last_issues = "; ".join(issues)
            if attempt < max_retries:
                print(f"   🔄 重试 (temperature={temp}, 加强约束)...")
            else:
                return art, f"验证失败({max_retries+1}次): {'; '.join(issues)}"

        except Exception as e:
            print(f"   ❌ 第{attempt+1}次生成异常: {e}")
            if attempt < max_retries:
                print(f"   🔄 重试...")
            else:
                return {}, str(e)

    return {}, "未知错误"


# ============================================================
# Hupu Data Collection & Article Generation
# ============================================================

def collect_tieba_data(date_str):
    print(f"\n[数据] 采集虎扑球迷讨论 ({date_str})...", flush=True)
    try:
        scraper = HupuScraper(headless=True)
        # Set 5-minute timeout via signal to prevent CI hang
        signal.signal(signal.SIGALRM, lambda s, f: (_ for _ in ()).throw(TimeoutError("Hupu scraping timed out after 5min")))
        signal.alarm(300)
        try:
            data = scraper.collect_all(date_str)
        finally:
            signal.alarm(0)
        if data and data.get("raw_posts"):
            print(f"   采集到 {len(data['raw_posts'])} 条有效讨论帖", flush=True)
            return data
        else:
            print("   虎扑未采集到有效讨论数据", flush=True)
            return None
    except TimeoutError as e:
        print(f"   虎扑采集超时: {e}", flush=True)
        return None
    except Exception as e:
        print(f"   虎扑采集异常: {e}", flush=True)
        return None


def select_tieba_topics(tieba_data, topic_history=None):
    print("\n[2.6] LLM 从虎扑讨论中筛选话题 (DeepSeek)...")

    posts_text = []
    for p in tieba_data.get("raw_posts", [])[:20]:
        posts_text.append({
            "team": p["team"],
            "title": p["title"],
            "reply_num": p["reply_num"],
            "main_post": (p.get("main_content") or "")[:200],
            "hot_replies": [r["content"][:100] for r in p.get("top_replies", [])[:3]],
        })

    history_text = ""
    if topic_history and (topic_history.get("titles") or topic_history.get("teams")):
        history_text = "\n⚠️ 过去7天已报道（必须避开）:\n"
        if topic_history.get("titles"):
            history_text += "已写: " + " | ".join(list(topic_history["titles"])[:5]) + "\n"

    prompt = f"""你是头条号足球博主"球评人老六"。以下是虎扑8大足球球队专区最近2天的真实球迷讨论。请从中筛选3个最有二次创作价值的话题。

虎扑热门讨论（按回复数排序）：
{json.dumps(posts_text, ensure_ascii=False)}
{history_text}

硬性要求 — 3个话题必须覆盖不同内容维度：
1. 第1篇：争议讨论型 — 球迷意见两极分化的话题，有明确的"站队"空间
2. 第2篇：情绪共鸣型 — 引发集体情感的话题（怀念、愤怒、感动、骄傲）
3. 第3篇：深度洞察型 — 球迷讨论中出现了有价值的战术/管理/行业分析

风格要求：标题和角度要"接地气，有人味"，就像是虎扑老哥在发帖。保留球迷语言的生动和直接。

二次创作原则：借讨论方向，不借原文。综合多个帖子/回复的观点，加上你自己的分析和态度。绝不可照搬任何原帖句子。

输出纯JSON数组：
[{{"title": "标题(15-25字，有网感，像虎扑标题)", "angle": "切入角度+你的态度", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "content_type": "争议讨论型/情绪共鸣型/深度洞察型", "controversy_level": "high/medium/low", "source_threads": ["引用的虎扑帖子标题"], "why_pick": "为什么选这个角度"}}]
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是头条号足球博主'球评人老六'，有态度有人味，能像虎扑老哥一样聊球。从球迷真实讨论中提炼话题，综合多源观点+自己态度=全新原创。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-flash", messages, temperature=0.7, max_tokens=4096)
    topics = safe_json_loads(response)
    print(f"   筛选出 {len(topics)} 个虎扑话题:")
    for i, t in enumerate(topics):
        print(f"   {i+1}. [{t.get('content_type', 'N/A')}] {t['title'][:50]}")
    return topics


def generate_tieba_article(topic, index, tieba_context, temperature=0.8, retry_hint=""):
    content_type = topic.get("content_type", "争议讨论")
    print(f"\n[Hupu-{index}] [{content_type}] {topic['title'][:40]}...")

    style_guide = {
        "争议讨论型": "像虎扑老哥发帖一样：开篇就抛出争议点，直接亮明你的态度（站某一方），然后有理有据地掰扯。引用球迷讨论中的典型观点，然后给出你自己的见解。接地气，有人味，不做和事佬。",
        "情绪共鸣型": "像在球场看台上和一个老朋友聊天：从球迷的真实情绪出发，讲述为什么大家会这样想/这样感受。有温度有细节，让读者觉得'对对对，就是这么回事'。引用几句球迷的原话，然后展开你的共鸣或不同视角。",
        "深度洞察型": "像一个懂球的老球迷从虎扑讨论中发现了有趣的东西：你看到球迷们在讨论某个现象，你从中总结出规律或趋势。视角要比普通球迷高一点，但语言要保持接地气。用球迷讨论作为引子，展开你的分析。",
    }
    style = style_guide.get(content_type, style_guide["争议讨论型"])

    posts_context = ""
    for p in tieba_context.get("raw_posts", [])[:15]:
        posts_context += f"\n【{p['team']}专区】{p['title']}（{p['reply_num']}回复）\n"
        if p.get("main_content"):
            posts_context += f"  主帖: {p['main_content'][:150]}\n"
        for j, r in enumerate(p.get("top_replies", [])[:2]):
            posts_context += f"  高赞回复{j+1}({r['agree_count']}赞): {r['content'][:120]}\n"

    retry_block = ""
    if retry_hint:
        retry_block = f"""
⚠️ 上次生成失败！问题：{retry_hint}
这次必须修正上述所有问题。正文至少800字，至少3个##小标题，文末至少3个配图标记。"""

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。今天的文章素材来自虎扑球迷的真实讨论。你需要综合这些讨论，写一篇完全原创的足球文章。

今日话题：{topic['title']}
切入角度：{topic['angle']}
内容类型：{content_type}

虎扑球迷真实讨论（综合多个帖子）：
{posts_context[:3500]}

写作要求：
{style}

结构：
- 开篇：直接抛出争议/情绪/发现（引用一句球迷讨论作为引子，但用自己的话说）
  → 如果是争议型：开篇就站队，别骑墙
  → 如果是情绪型：从具体的球迷感受切入，建立共鸣
  → 如果是洞察型：从球迷讨论中的某个有意思的点展开
- 中间2-3小节：展开你的分析/观点，每节融合球迷讨论中的典型声音
- 高潮：最犀利的观点
- 收尾：金句+互动

硬性规范：
- 正文 800-1500 字（硬性要求，低于800字视为不合格）
- 必须包含 ≥3 个 ## 二级标题
- 文末必须包含3张配图标记：![配图1](images/article-{index}-img-001.jpg) 等
- 真实性红线：不得编造球迷没说过的话，引用讨论要忠于原意
- 原创性红线：综合多个帖子/回复的观点后用自己的话写，不可照搬原文
- 风格红线：接地气，有人味，像真人在聊球。不用套话，不用模板

禁用词：震惊、吓尿、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说
禁用模式：不要列一二三四，不强用"首先其次最后"

输出JSON:
{{"title": "标题(15-25字，有网感有态度)", "content": "Markdown正文(800-1500字，含≥3个##小标题，文末含3个配图标记)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_bait": "互动问题", "content_type": "{content_type}", "sources_used": ["引用的虎扑帖子标题"]}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": f"你是头条号足球博主'球评人老六'，有态度有人味。今天的风格：{style} 从虎扑球迷真实讨论出发，综合多源观点+自己的分析=全新原创。用自然口语化中文写作。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-pro", messages, temperature=temperature, max_tokens=8192)
    article = safe_json_loads(response)
    print(f"   标题: {article.get('title','?')}, 正文: {len(article.get('content',''))}字")
    return article


# ============================================================
# Save Articles (Local, no Flask)
# ============================================================

def save_articles_local(date_str, articles, images_map, topics, match_data, extra=None):
    """Save articles directly to filesystem (no Flask dependency)."""
    print(f"\n[4/5] 保存文章...")
    image_service = ImageService(config={
        "images": {"min_width": 800, "min_height": 600, "max_size_bytes": 5242880,
                   "min_size_bytes": 51200, "max_per_article": 5, "required_per_article": 3}})
    file_writer = FileWriter(base_dir=str(OUTPUT_DIR))

    date_dir = OUTPUT_DIR / date_str
    date_dir.mkdir(parents=True, exist_ok=True)
    images_dir = date_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    all_hashes = set()

    for i, art in enumerate(articles):
        idx = i + 1
        prefix = f"article-{idx}-img"

        # Download images from URLs
        img_urls = [img["url"] for img in images_map.get(i, [])[:5]]
        downloaded = []
        for j, url in enumerate(img_urls):
            if len(downloaded) >= 3:
                break
            if not url or not url.startswith("http"):
                continue
            result = image_service.download_image(url=url, target_dir=images_dir,
                                                  prefix=prefix, index=len(downloaded)+1,
                                                  existing_hashes=all_hashes)
            if result:
                all_hashes.add(result["md5"])
                downloaded.append(result)

        content = art.get("content", "")
        if "![配图" not in content and "![" not in content:
            for j, img in enumerate(downloaded):
                img_ref = f"\n![{img.get('description', f'配图{j+1}')}](images/{img['filename']})\n"
                parts = content.split("\n## ", 1)
                if len(parts) == 2:
                    rest = "## " + parts[1]
                    insert_pos = rest.find("\n\n", len(rest) // (j + 2) + len(rest) // 3)
                    if insert_pos > 0:
                        content = parts[0] + "\n" + rest[:insert_pos] + img_ref + rest[insert_pos:]
                    else:
                        content = parts[0] + "\n" + rest + img_ref
                else:
                    content = content + img_ref
        art["content"] = content

        # Save article
        art_data = {**art, "downloaded_images": downloaded,
                     "tags": art.get("keywords", []), "category": "足球"}
        result = file_writer.save_article(date_str=date_str, index=idx, article_data=art_data)
        saved.append({"index": idx, "title": art.get("title", ""), "path": result["article_path"],
                       "slug": result["slug"], "tags": art.get("keywords", []),
                       "keywords": art.get("keywords", []), "images": result["image_paths"],
                       "sources_used": art.get("sources_used", []),
                       "originality_note": art.get("originality_note", ""),
                       "content_type": art.get("content_type", "")})

    meta = {"total_articles": len(saved), "articles": saved, "topics": topics, "data_sources": {}}
    if extra:
        meta.update(extra)
    file_writer.save_index(date_str, saved)
    file_writer.save_metadata(date_str, meta)

    output_path = OUTPUT_DIR / date_str
    print(f"   保存至: {output_path}")
    return {"success": True, "date": date_str, "total_articles": len(saved),
            "articles": saved, "output_dir": str(output_path)}


# ============================================================
# Main
# ============================================================

def main():
    # Parse args: python orchestrator.py [YYYY-MM-DD] [--topic=auto|transfer|match]
    date_str = None
    topic_preference = "auto"
    for arg in sys.argv[1:]:
        if arg.startswith("--topic="):
            topic_preference = arg.split("=", 1)[1]
        elif not arg.startswith("--"):
            date_str = arg
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    print(f"足球自媒体内容自动化 - {date_str} (topic={topic_preference})\n")

    start_time = time.time()
    success = False
    result_msg = ""
    stats = {"generated": 0, "valid": 0, "failed": 0, "issues": []}
    extra_meta = {}

    try:
        # Step 0: Load topic history for dedup
        topic_history = get_topic_history(date_str)

        # Step 1: Collect match data (always, for context)
        match_data = collect_real_matches(date_str)

        articles = []
        images_map = {}
        topics = []

        # ============================================================
        # Main Article Pipeline (articles 1-3)
        # ============================================================

        if topic_preference != "auto":
            print(f"   用户偏好: {topic_preference}，使用公众号爆款数据为主\n")
            topics_and_raw = collect_real_gzh_topics(
                date_str, topic_history, topic_preference=topic_preference)
            if not topics_and_raw or not topics_and_raw[0]:
                result_msg = f"无{topic_preference}相关真实爆款数据可用"
                print(f"ERROR: {result_msg}")
                send_wxpusher("足球自媒体 ⚠️", f"{date_str} 发文任务中止：{result_msg}")
                return

            topics, raw_articles = topics_and_raw
            extra_meta = {"type": f"gzh_{topic_preference}"}

            for i, topic in enumerate(topics[:3]):
                print(f"\n--- 第{i+1}/3篇 ---")
                imgs = search_images(topic, count=5)
                images_map[i] = imgs
                art, error = generate_article_with_retry(
                    topic, match_data, i + 1, is_gossip=True, max_retries=2)
                stats["generated"] += 1
                if error:
                    print(f"   ❌ 最终失败: {error}")
                    stats["failed"] += 1
                    stats["issues"].append(f"第{i+1}篇: {error}")
                else:
                    stats["valid"] += 1
                articles.append((i, art))

        elif match_data["total_matches"] == 0:
            print("   今日无比赛，切换为公众号爆款数据模式\n")
            topics_and_raw = collect_real_gzh_topics(date_str, topic_history)
            if not topics_and_raw or not topics_and_raw[0]:
                result_msg = "无比赛且无真实爆款数据可用"
                print(f"ERROR: {result_msg}")
                send_wxpusher("足球自媒体 ⚠️", f"{date_str} 发文任务中止：{result_msg}")
                return

            topics, raw_articles = topics_and_raw
            extra_meta = {"type": "gzh_real_data"}

            for i, topic in enumerate(topics[:3]):
                print(f"\n--- 第{i+1}/3篇 ---")
                imgs = search_images(topic, count=5)
                images_map[i] = imgs
                art, error = generate_article_with_retry(
                    topic, match_data, i + 1, is_gossip=True, max_retries=2)
                stats["generated"] += 1
                if error:
                    print(f"   ❌ 最终失败: {error}")
                    stats["failed"] += 1
                    stats["issues"].append(f"第{i+1}篇: {error}")
                else:
                    stats["valid"] += 1
                articles.append((i, art))

        else:
            print("\n   获取公众号爆款趋势作为跨源参考...")
            gzh_raw = fetch_gzh_football_trends(date_str)
            gzh_context = gzh_raw[:8] if gzh_raw else []

            topics = select_topics(match_data, gzh_context, topic_history)
            extra_meta = {"type": "match_analysis"}

            for i, topic in enumerate(topics[:3]):
                print(f"\n--- 第{i+1}/3篇 [{topic.get('content_type', 'N/A')}] ---")
                imgs = search_images(topic, count=5)
                images_map[i] = imgs
                art, error = generate_article_with_retry(
                    topic, match_data, i + 1, gzh_articles=gzh_context, max_retries=2)
                stats["generated"] += 1
                if error:
                    print(f"   ❌ 最终失败: {error}")
                    stats["failed"] += 1
                    stats["issues"].append(f"第{i+1}篇({topic.get('content_type','?')}): {error}")
                else:
                    stats["valid"] += 1
                articles.append((i, art))

        # ============================================================
        # Hupu Pipeline (articles 4-6, independent of main pipeline)
        # ============================================================
        try:
            print("\n--- 虎扑球迷讨论数据源 ---")
            tieba_data = collect_tieba_data(date_str)
            if tieba_data and tieba_data.get("raw_posts"):
                tieba_topics = select_tieba_topics(tieba_data, topic_history)

                for ti, t_topic in enumerate(tieba_topics[:3]):
                    t_idx = len(articles) + ti + 1
                    print(f"\n--- 第{t_idx}/6篇 [Hupu-{t_topic.get('content_type', 'N/A')}] ---")
                    imgs = search_images(t_topic, count=5)
                    images_map[len(articles) + ti] = imgs
                    art, error = generate_article_with_retry(
                        t_topic, match_data, t_idx,
                        is_tieba=True, tieba_context=tieba_data, max_retries=2)
                    stats["generated"] += 1
                    if error:
                        print(f"   ❌ 最终失败: {error}")
                        stats["failed"] += 1
                        stats["issues"].append(f"第{t_idx}篇(虎扑): {error}")
                    else:
                        stats["valid"] += 1
                    articles.append((len(articles), art))
                    topics.append(t_topic)

                extra_meta["hupu"] = True
            else:
                print("   虎扑无有效数据，跳过球迷讨论文章（不影响主文章）")
        except Exception as e:
            print(f"   ⚠️  虎扑数据采集/生成失败（不影响主文章）: {e}")

        # ============================================================
        # Save all articles
        # ============================================================
        if not articles:
            result_msg = "未能生成任何文章"
            print(f"ERROR: {result_msg}")
            send_wxpusher("足球自媒体 ⚠️", f"{date_str} 发文任务中止：{result_msg}")
            return

        articles_sorted = [a for _, a in sorted(articles, key=lambda x: x[0])]
        result = save_articles_local(date_str, articles_sorted, images_map, topics, match_data,
                                     extra=extra_meta)

        elapsed = int(time.time() - start_time)
        article_titles = []
        for a in result.get("articles", []):
            ct = a.get("content_type", "")
            title = a.get("title", "?")[:40]
            article_titles.append(f"[{ct}] {title}")

        result_msg = (
            f"生成 {stats['valid']}/{stats['generated']} 篇 ({elapsed}s)\n"
            + "\n".join(f"- {t}" for t in article_titles)
        )
        if stats["failed"] > 0:
            result_msg += f"\n\n⚠️ 失败 {stats['failed']} 篇:\n" + "\n".join(f"- {i}" for i in stats["issues"])

        print(f"\n完成! ({elapsed}s) | 成功 {stats['valid']}/{stats['generated']} 篇")
        print(f"   输出: {result.get('output_dir', 'N/A')}")
        for a in result.get("articles", []):
            print(f"   - [{a.get('content_type', 'N/A')}] {a.get('title', 'N/A')[:50]} ({len(a.get('images', []))}张图)")
        success = True

    except Exception as e:
        result_msg = f"异常: {e}"
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

    # Notify on generation result
    if success and stats["valid"] > 0:
        send_wxpusher("足球自媒体 📝", f"{date_str} 文章生成完成\n\n{result_msg}")
    elif not success or stats["valid"] == 0:
        send_wxpusher("足球自媒体 ❌", f"{date_str} 文章生成失败\n\n{result_msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
