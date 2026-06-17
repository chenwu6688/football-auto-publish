#!/usr/bin/env python3
"""赛事日历 — 赛程预取 + 焦点战标记 + 内容序列规划

从 football-data.org 预取未来 3 天比赛，标记焦点战，
为 orchestrator 提供"赛前预告→赛中快讯→赛后复盘"的内容序列。
"""

import os, json, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from constants import FOOTBALL_DATA_KEY, FOOTBALL_DATA_BASE, COMPETITION_IDS
from utils import retry
import requests


# Focus match criteria: teams with global fanbase or high viewership
BIG_TEAMS = {
    "Real Madrid", "FC Barcelona", "Manchester City", "Manchester United",
    "Liverpool", "Arsenal", "Chelsea", "Bayern Munich", "Borussia Dortmund",
    "Juventus", "AC Milan", "Inter Milan", "Paris Saint-Germain",
    "Argentina", "Brazil", "Germany", "France", "Spain", "England",
    "Netherlands", "Portugal", "Italy",
}

HIGH_INTEREST_LEAGUES = {
    "FIFA World Cup", "UEFA Champions League", "Premier League",
    "Primera Division", "Serie A", "Bundesliga", "Ligue 1",
}


def fetch_upcoming_matches(days_ahead=3):
    """Fetch matches for today + next N days from football-data.org.

    Returns list of match dicts sorted by date, with focus_match flag.
    """
    cst = ZoneInfo("Asia/Shanghai")
    today = datetime.now(cst)
    from_date = today.strftime("%Y-%m-%d")
    to_date = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    print(f"📅 赛事日历: 查询 {from_date} ~ {to_date} 的比赛...")

    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    all_matches = []
    seen_ids = set()

    for league_name, comp_id in COMPETITION_IDS.items():
        try:
            resp = requests.get(
                f"{FOOTBALL_DATA_BASE}/competitions/{comp_id}/matches",
                params={"dateFrom": from_date, "dateTo": to_date},
                headers=headers, timeout=15,
            )
            if resp.status_code == 200:
                matches = resp.json().get("matches", [])
                for m in matches:
                    mid = m.get("id")
                    if mid and mid not in seen_ids:
                        seen_ids.add(mid)
                        home = m.get("homeTeam", {}).get("name", "")
                        away = m.get("awayTeam", {}).get("name", "")
                        comp = m.get("competition", {}).get("name", "")
                        utc_date = m.get("utcDate", "")
                        status = m.get("status", "")

                        # Convert UTC to CST
                        cst_time = ""
                        if utc_date:
                            try:
                                dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
                                dt_cst = dt + timedelta(hours=8)
                                cst_time = dt_cst.strftime("%Y-%m-%d %H:%M")
                            except Exception:
                                pass

                        is_focus = (
                            home in BIG_TEAMS or away in BIG_TEAMS
                        ) and comp in HIGH_INTEREST_LEAGUES

                        all_matches.append({
                            "id": mid,
                            "home_team": home,
                            "away_team": away,
                            "league": comp,
                            "utc_date": utc_date,
                            "cst_time": cst_time,
                            "cst_date": cst_time[:10] if cst_time else "",
                            "status": status,
                            "focus_match": is_focus,
                            "competition_id": comp_id,
                        })
            time.sleep(0.6)
        except Exception as e:
            print(f"   ⚠️ {league_name}: {e}")

    # Sort by date
    all_matches.sort(key=lambda m: m.get("cst_time", ""))

    # Group by date
    by_date = {}
    for m in all_matches:
        d = m["cst_date"]
        if d:
            by_date.setdefault(d, []).append(m)

    focus_count = sum(1 for m in all_matches if m["focus_match"])
    print(f"   📊 共 {len(all_matches)} 场比赛, {focus_count} 场焦点战, {len(by_date)} 天")
    for date in sorted(by_date.keys()):
        day_matches = by_date[date]
        day_focus = sum(1 for m in day_matches if m["focus_match"])
        print(f"      {date}: {len(day_matches)} 场 ({day_focus} 焦点)")

    return all_matches


def plan_content_sequence(matches):
    """从比赛列表生成内容序列建议。

    Returns list of dicts:
    [{date, match, content_type, slot, rationale}, ...]
    """
    plans = []
    cst = ZoneInfo("Asia/Shanghai")
    now = datetime.now(cst)

    for m in matches:
        if not m["cst_time"]:
            continue

        try:
            match_dt = datetime.strptime(m["cst_time"], "%Y-%m-%d %H:%M")
            match_dt = match_dt.replace(tzinfo=cst)
        except Exception:
            continue

        match_date = m["cst_date"]
        home = m["home_team"]
        away = m["away_team"]

        # Days until match
        days_until = (match_dt - now).days

        if not m["focus_match"]:
            continue  # Only plan sequences for focus matches

        # Preview: day before match → morning batch
        if days_until >= 0:
            plans.append({
                "date": match_date,
                "match": f"{home} vs {away}",
                "league": m["league"],
                "content_type": "preview",
                "suggested_batch": "morning",
                "label": f"🔮 赛前前瞻: {home} vs {away} ({m['league']})",
                "rationale": f"焦点战前瞻，分析双方状态、历史交锋、看点",
            })

        # Review: day after match → evening batch
        from datetime import timedelta as td
        review_date = (match_dt + td(days=1)).strftime("%Y-%m-%d")
        plans.append({
            "date": review_date,
            "match": f"{home} vs {away}",
            "league": m["league"],
            "content_type": "review",
            "suggested_batch": "evening",
            "label": f"📺 赛后复盘: {home} vs {away} ({m['league']})",
            "rationale": f"焦点战复盘，战术分析、关键球员、比赛转折点",
        })

        # Deep dive: 2 days after match → noon batch
        deep_date = (match_dt + td(days=2)).strftime("%Y-%m-%d")
        plans.append({
            "date": deep_date,
            "match": f"{home} vs {away}",
            "league": m["league"],
            "content_type": "deep_dive",
            "suggested_batch": "noon",
            "label": f"📊 数据深读: {home} vs {away} — 从数据看胜负手",
            "rationale": f"赛后数据深度解读，从射门/控球/跑动等数据维度还原比赛",
        })

    if plans:
        print(f"\n📋 内容序列规划 ({len(plans)} 条建议):")
        for p in plans[:6]:
            print(f"   {p['label'][:60]}")
        if len(plans) > 6:
            print(f"   ... 还有 {len(plans)-6} 条")
    else:
        print("   ℹ️ 无焦点战，跳过内容序列规划")

    return plans


def save_schedule(matches, plans, output_dir=None):
    """保存赛事日历到 JSON 文件，供 orchestrator 读取。"""
    if output_dir is None:
        output_dir = PROJECT_ROOT / "output" / "schedule"
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M"),
        "matches": matches,
        "content_plans": plans,
    }

    filepath = output_dir / "match_schedule.json"
    filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"   💾 赛事日历已保存: {filepath}")
    return filepath


if __name__ == "__main__":
    """CLI usage: python3 match_scheduler.py [days_ahead=3]"""
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    matches = fetch_upcoming_matches(days_ahead=days)
    if matches:
        plans = plan_content_sequence(matches)
        save_schedule(matches, plans)
