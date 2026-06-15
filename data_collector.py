#!/usr/bin/env python3
"""足球自媒体 — 数据采集模块

负责: 比赛数据采集、公众号爆款趋势、积分榜/射手榜、图片搜索。
"""

import os, json, sys, subprocess, requests, time, re
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

from constants import (PROJECT_ROOT, OUTPUT_DIR, GZH_SCRIPT,
                       FOOTBALL_DATA_KEY, FOOTBALL_DATA_BASE,
                       COMPETITION_IDS, GZH_KEYWORD_GROUPS, GZH_TRANSFER_KEYWORDS,
                       GZH_NOISE_PATTERNS, WIKI_PLAYERS, WIKI_TEAMS, FOOTYRENDERS_PLAYERS,
                       UNSPLASH_KEY)

from utils import retry


# ============================================================
# Data Collection
# ============================================================


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

    standings = fetch_recent_standings()

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
                # Also track Hupu source post titles for dedup
                source_post = a.get("source_post", "")
                if source_post:
                    history["titles"].add(source_post[:50])
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

