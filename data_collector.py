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

    # Final fallback: DuckDuckGo (free, no key)
    if len(images) < count:
        try:
            q = " ".join(en_keywords[:3]) if en_keywords else "football match"
            from urllib.parse import quote_plus as qp
            import re
            ddg = requests.get("https://duckduckgo.com/", params={"q": f"{q} football"}, timeout=10)
            vqd_match = re.search(r'vqd=([\d-]+)', ddg.text)
            if vqd_match:
                vqd = vqd_match.group(1)
                resp = requests.get(
                    f"https://duckduckgo.com/i.js?q={qp(q)}+football&vqd={vqd}&o=json",
                    timeout=10)
                if resp.status_code == 200:
                    for item in resp.json().get("results", [])[:count]:
                        url = item.get("image", "")
                        if url and url not in [i["url"] for i in images]:
                            images.append({"url": url, "source": "duckduckgo", "alt": item.get("title", "")})
        except Exception:
            pass
    return images[:count]


# ============================================================
# Topic Selection & Article Generation
# ============================================================

