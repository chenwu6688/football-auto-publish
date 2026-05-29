#!/usr/bin/env python3
"""足球自媒体 - 文章生成编排器 (独立版，无 Flask 依赖)

Usage: python orchestrator.py [YYYY-MM-DD]
"""

import os, json, sys, subprocess, requests, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

from file_writer import FileWriter
from image_service import ImageService

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


def fetch_gzh_football_trends(date_str):
    print(f"[1/5] 无比赛日，从公众号爆款库采集真实足球话题 ({date_str})...")
    target_date = datetime.strptime(date_str, "%Y-%m-%d")
    start_date = (target_date - timedelta(days=7)).strftime("%Y-%m-%d")
    keywords_list = ["足球", "英超,欧冠,转会,梅西,C罗"]
    all_raw = []

    for kw in keywords_list:
        try:
            cmd = [sys.executable, GZH_SCRIPT, "--keyword", kw, "--start-date", start_date,
                   "--output-format", "json", "--output-file", f"/tmp/gzh_{kw.replace(',', '_')[:30]}.json"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                output_file = f"/tmp/gzh_{kw.replace(',', '_')[:30]}.json"
                if os.path.exists(output_file):
                    for item in json.loads(Path(output_file).read_text()).get("items", []):
                        if _is_football_relevant(item):
                            all_raw.append(item)
        except Exception as e:
            print(f"   搜索'{kw}'失败: {e}")

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

def select_topics(match_data, gzh_articles=None):
    print("\n[2/5] LLM 话题筛选 (DeepSeek)...")
    lines = []
    for league, matches in sorted(match_data.get("fixtures_by_league", {}).items()):
        lines.append(f"\n## {league}")
        for m in matches:
            hg, ag = m.get("home_score"), m.get("away_score")
            lines.append(f"  {m['home_team']} {hg}-{ag if hg is not None else 'vs'} {m['away_team']}")
    for league, table in match_data.get("standings", {}).items():
        lines.append(f"\n## {league} 积分榜前6:")
        for row in table[:6]:
            lines.append(f"  {row['position']}. {row['team']} {row['points']}分 (净胜球:{row['goal_diff']})")

    gzh_text = ""
    if gzh_articles:
        gzh_text = "\n## 当前公众号爆款文章（了解热点方向，不可照搬）\n"
        for a in gzh_articles[:6]:
            gzh_text += f"- [{a.get('clicksCount', '?')}阅读] {a.get('title', '')[:60]} — {a.get('accountName', '?')}\n"

    prompt = f"""你是资深中文足球媒体主编。以下是 {match_data['date']} 的真实比赛结果和积分榜数据。请筛选3个最有爆款基因的话题。

{"".join(lines)}
{gzh_text}

爆款评分体系（120分制，低于70分淘汰）：
- 争议性（25分）、故事性（15分）、情绪共鸣（12分）、讨论价值（8分）、差异化（10分）

输出纯JSON数组：
[{{"title": "标题(15-25字)", "formula": "A/B/C/D/E/F", "angle": "切入角度+明确观点立场", "keywords": ["soccer", "premier league"], "keywords_cn": ["中文关键词"], "content_type": "比赛复盘型/人物故事型/争议观点型/趋势解读型", "score": 95, "controversy_level": "high/medium/low", "target_emotion": "愤怒/骄傲/怀旧/震惊/感动/好奇"}}]
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是资深足球主编，严格按120分评分体系筛选。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-pro", messages, temperature=0.7, max_tokens=4096)
    topics = safe_json_loads(response)
    print(f"   筛选出 {len(topics)} 个话题:")
    for i, t in enumerate(topics):
        print(f"   {i+1}. {t['title']} [{t.get('content_type', 'N/A')}] (评分:{t.get('score', 'N/A')})")
    return topics


def collect_real_gzh_topics(date_str):
    raw_articles = fetch_gzh_football_trends(date_str)
    if not raw_articles:
        print("   ERROR: 无真实数据源")
        return [], []
    standings = fetch_recent_standings()

    articles_text = [{"id": i+1, "title": a.get("title", "")[:80],
                       "summary": (a.get("summary", "") or "")[:120],
                       "account": a.get("accountName", "?"),
                       "reads": a.get("clicksCount", "?"), "likes": a.get("likeCount", 0),
                       "data_score": a.get("dataScore", 0)}
                      for i, a in enumerate(raw_articles[:15])]

    print("\n[2/5] 基于真实爆款数据筛选选题 (DeepSeek)...")
    prompt = f"""你是资深中文足球媒体主编。以下是公众号平台最近7天真实爆款足球文章数据。

真实爆款文章数据：
{json.dumps(articles_text, ensure_ascii=False)}

请从这些真实爆款中选出3个最有二次创作价值的选题，套用爆款标题公式重新包装。

输出纯JSON：
[{{"title": "新标题(15-25字)", "source_article_ids": [引用文章id], "source_titles": ["原文标题"], "formula": "A/B/C/D/E/F", "angle": "切入角度+明确观点", "keywords": ["soccer", "football"], "keywords_cn": ["中文关键词"], "content_type": "比赛复盘型/人物故事型/争议观点型/趋势解读型/转会八卦型", "controversy_level": "high/medium/low", "target_emotion": "愤怒/骄傲/怀旧/震惊/感动/好奇"}}]
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是足球主编。严格只基于提供的真实文章数据选题，绝不编造。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-pro", messages, temperature=0.6, max_tokens=4096)
    topics = safe_json_loads(response)
    print(f"   筛选出 {len(topics)} 个选题（全部来自真实爆款）:")
    for i, t in enumerate(topics):
        srcs = t.get("source_titles", ["?"])
        print(f"   {i+1}. {t['title']} [引用{len(srcs)}篇: {srcs[0][:30]}...]")

    raw_map = {a.get("id"): a for a in articles_text}
    for t in topics:
        ids = t.get("source_article_ids", [])
        t["_source_articles"] = [raw_map.get(sid, {}) for sid in ids]
        t["_all_articles"] = articles_text[:10]
        t["_standings"] = standings
    return topics, raw_articles


def generate_article(topic, match_context, index, gzh_articles=None):
    print(f"\n[3.{index}] 生成文章: {topic['title'][:40]}...")
    fixtures = match_context.get("fixtures_by_league", {})
    standings = match_context.get("standings", {})

    context_str = json.dumps({
        "date": match_context["date"],
        "matches": fixtures,
        "standings": {k: v[:6] for k, v in standings.items()},
    }, ensure_ascii=False)

    gzh_text = ""
    if gzh_articles:
        gzh_text = "\n## 当前公众号爆款文章（跨源参考，不可改写）\n"
        for a in gzh_articles[:6]:
            gzh_text += f"- [{a.get('clicksCount', '?')}阅读] {a.get('title', '')[:60]} — {a.get('accountName', '?')}\n"

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。创作一篇完全原创的足球爆款文章。

今日话题：{topic['title']}
切入角度：{topic['angle']}
内容类型：{topic.get('content_type', '比赛分析')}

真实比赛数据：{context_str[:4000]}
{gzh_text}

真实性红线：
✅ 比分、积分、球队名只能用提供的真实数据
❌ 禁止编造"内部报告""内部调研""知情人士透露"等虚假信源
❌ 禁止虚构任何比赛数据、球员数据

文章结构：
1. 开头钩子（A争议/B场景/C反常识，3选1）
2. 事实铺陈 3. 老六观点展开 4. 高潮金句(≥2句，≤30字) 5. 互动引导

风格：口语化+专业深度，短句为主，有明确立场。禁用词：震惊、吓尿、哭惨、看傻了

输出JSON:
{{"title": "爆款标题(15-25字)", "content": "Markdown正文(1000-1800字，含##小标题，文末必须包含 ![配图1](images/article-{index}-img-001.jpg) 等3张配图标记)", "summary": "50字摘要", "keywords": ["英文关键词"], "golden_lines": ["金句1", "金句2"], "hook_type": "A/B/C", "interaction_bait": "互动问题", "sources_used": ["来源文章标题"], "originality_note": "差异化说明(30字)"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是头条号足球博主'球评人老六'，10万粉丝。严格基于真实数据，不编造。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DASHSCOPE_URL, DASHSCOPE_KEY, "qwen3-max", messages, temperature=0.8, max_tokens=8192)
    article = safe_json_loads(response)
    print(f"   标题: {article.get('title','?')}, 正文: {len(article.get('content',''))}字")
    return article


def generate_gossip_article(topic, index):
    print(f"\n[3.{index}] 生成文章（跨源合成）: {topic['title'][:40]}...")
    sources = topic.get("_source_articles", [])
    all_articles = topic.get("_all_articles", [])
    standings = topic.get("_standings", {})

    sources_text = ""
    for i, s in enumerate(sources):
        sources_text += f"\n来源{i+1}：{s.get('title', '')[:60]}\n  账号：{s.get('account', '?')} | 阅读：{s.get('reads', '?')}\n"

    bg_text = "".join(f"- [{a.get('reads', '?')}阅读] {a.get('title', '')[:60]} — {a.get('account', '?')}\n"
                      for a in all_articles[:8])

    standings_text = ""
    if standings:
        for league, table in list(standings.items())[:4]:
            standings_text += f"\n{league} 积分榜前5:"
            for row in table[:5]:
                standings_text += f"\n  {row['position']}. {row['team']} {row['points']}分 (净胜球{row['goal_diff']})"

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。创作一篇完全原创的足球爆款文章。

参考爆款文章（话题方向，不可改写）：
{sources_text}

积分榜真实数据（事实锚点）：
{standings_text if standings_text else "（无积分榜数据）"}

同期其他热门话题（了解语境）：
{bg_text}

创作约束：
- 真实性红线：只使用提供的真实数据，不得虚构
- 原创性：不可照抄参考文章的标题/段落/金句，必须有不同切入角度
- 文章结构：开头钩子 → 事实铺陈 → 老六观点展开 → 高潮金句 → 互动引导

风格：口语化+专业深度，短句为主，有明确立场。

输出JSON:
{{"title": "爆款标题(15-25字)", "content": "Markdown正文(800-1500字，含##小标题，文末必须包含 ![配图1](images/article-{index}-img-001.jpg) 等3张配图标记)", "summary": "50字摘要", "keywords": ["英文关键词"], "golden_lines": ["金句1", "金句2"], "hook_type": "A/B/C", "interaction_bait": "互动问题", "sources_used": ["来源文章标题"], "originality_note": "如何区别于原文的说明(30字)"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是'球评人老六'，头条号足球博主。绝不洗稿，跨源合成：多源事实+积分榜数据+自己观点=全新原创。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DASHSCOPE_URL, DASHSCOPE_KEY, "qwen3-max", messages, temperature=0.8, max_tokens=8192)
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
                       "originality_note": art.get("originality_note", "")})

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
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    print(f"足球自媒体内容自动化 - {date_str}\n")

    start_time = time.time()
    success = False
    result_msg = ""

    try:
        # Step 1: Collect match data
        match_data = collect_real_matches(date_str)

        if match_data["total_matches"] == 0:
            # No matches — GZH gossip mode
            print("   今日无比赛，切换为公众号爆款数据模式\n")
            topics_and_raw = collect_real_gzh_topics(date_str)
            if not topics_and_raw or not topics_and_raw[0]:
                result_msg = "无比赛且无真实爆款数据可用"
                print(f"ERROR: {result_msg}")
                send_wxpusher("足球自媒体 ⚠️", f"{date_str} 发文任务中止：{result_msg}")
                return

            topics, raw_articles = topics_and_raw
            articles = []
            images_map = {}

            def _worker_gossip(args):
                i, topic = args
                imgs = search_images(topic, count=5)
                art = generate_gossip_article(topic, i + 1)
                return i, imgs, art

            with ThreadPoolExecutor(max_workers=3) as ex:
                futures = [ex.submit(_worker_gossip, (i, t)) for i, t in enumerate(topics)]
                for f in as_completed(futures):
                    i, imgs, art = f.result()
                    images_map[i] = imgs
                    articles.append((i, art))
            articles = [a for _, a in sorted(articles, key=lambda x: x[0])]

            result = save_articles_local(date_str, articles, images_map, topics, match_data,
                                         extra={"type": "gzh_real_data"})
        else:
            # Match mode
            print("\n   获取公众号爆款趋势作为跨源参考...")
            gzh_raw = fetch_gzh_football_trends(date_str)
            gzh_context = gzh_raw[:8] if gzh_raw else []

            topics = select_topics(match_data, gzh_context)
            articles = []
            images_map = {}

            def _worker_match(args):
                i, topic = args
                imgs = search_images(topic, count=5)
                art = generate_article(topic, match_data, i + 1, gzh_context)
                return i, imgs, art

            with ThreadPoolExecutor(max_workers=3) as ex:
                futures = [ex.submit(_worker_match, (i, t)) for i, t in enumerate(topics)]
                for f in as_completed(futures):
                    i, imgs, art = f.result()
                    images_map[i] = imgs
                    articles.append((i, art))
            articles = [a for _, a in sorted(articles, key=lambda x: x[0])]

            result = save_articles_local(date_str, articles, images_map, topics, match_data,
                                         extra={"type": "match_analysis"})

        elapsed = int(time.time() - start_time)
        article_titles = [a.get("title", "?")[:40] for a in result.get("articles", [])]
        result_msg = f"生成 {len(articles)} 篇文章 ({elapsed}s)\n" + "\n".join(f"- {t}" for t in article_titles)
        print(f"\n[5/5] 完成! ({elapsed}s)")
        print(f"   输出: {result.get('output_dir', 'N/A')}")
        for a in result.get("articles", []):
            print(f"   - {a.get('title', 'N/A')[:50]} ({len(a.get('images', []))}张图片)")
        success = True

    except Exception as e:
        result_msg = f"异常: {e}"
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

    # Send WxPusher notification
    if success:
        send_wxpusher("足球自媒体 ✅", f"{date_str} 文章生成完毕\n\n{result_msg}")
    else:
        send_wxpusher("足球自媒体 ❌", f"{date_str} 文章生成失败\n\n{result_msg}")

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
