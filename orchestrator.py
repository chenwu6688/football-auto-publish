#!/usr/bin/env python3
"""足球自媒体 - 文章生成编排器 (独立版，无 Flask 依赖)

Usage: python orchestrator.py [YYYY-MM-DD]
"""

import os, json, sys, subprocess, requests, time, re, signal, yaml
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

from file_writer import FileWriter
from image_service import ImageService
from hupu_scraper import HupuScraper
from constants import (PROJECT_ROOT, OUTPUT_DIR, GZH_SCRIPT,
                       DEEPSEEK_KEY, DASHSCOPE_KEY, UNSPLASH_KEY, FOOTBALL_DATA_KEY,
                       DEEPSEEK_URL, DASHSCOPE_URL, FOOTBALL_DATA_BASE,
                       WXPUSHER_APPTOKEN, WXPUSHER_UID,
                       COMPETITION_IDS, GZH_KEYWORD_GROUPS, GZH_TRANSFER_KEYWORDS,
                       GZH_NOISE_PATTERNS, WIKI_PLAYERS, WIKI_TEAMS, FOOTYRENDERS_PLAYERS,
                       BATCH_TYPES, FALLBACK_MAP, ALL_CONTENT_TYPES)
from utils import retry, call_llm, safe_json_loads
from logger import log
from data_collector import (collect_real_matches, fetch_gzh_football_trends,
                             fetch_recent_standings, fetch_scorers, fetch_rankings_data,
                             search_images, search_wikipedia, search_footyrenders,
                             extract_search_entities, get_topic_history, get_previously_used_sources)


def print_daily_summary(date_str, batch_mode):
    """Print a daily summary of all batches completed so far."""
    meta_path = OUTPUT_DIR / date_str / "metadata.json"
    if not meta_path.exists():
        print(f"\n{'='*60}\n  今日摘要: {date_str} — 尚无批次完成\n{'='*60}")
        return

    try:
        meta = json.loads(meta_path.read_text())
        batches = meta.get("batches_completed", [])
        articles = meta.get("articles", [])

        print(f"\n{'='*60}")
        print(f"  今日摘要: {date_str}")
        print(f"  批次: {', '.join(batches) if batches else '无'}")
        print(f"  文章数: {len(articles)}")
        for a in articles:
            ct = a.get("content_type", "?")
            title = a.get("title", "?")[:45]
            perf = a.get("performance", {})
            reads = perf.get("reads", "?") if isinstance(perf, dict) else "?"
            print(f"    [{ct}] {title}")
            if reads and reads != "?":
                print(f"        阅读:{reads}")
        print(f"{'='*60}")
    except Exception as e:
        print(f"   ⚠️  摘要生成失败: {e}")


def load_season_weights(date_str=None):
    """Load season weights from config.yaml for the current month.
    Returns {content_type: weight} dict. Weight > 1.0 = preferred, < 1.0 = deprioritized."""
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    if not config_path.exists():
        return None

    try:
        cfg = yaml.safe_load(config_path.read_text())
        season_weights = cfg.get("season_weights", [])
        if not season_weights:
            return None

        dt = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now()
        month = dt.month

        for period in season_weights:
            if month in period.get("months", []):
                weights = period.get("weights", {})
                label = period.get("label", "未知")
                print(f"   📅 赛季节奏: {label} (月份{month}, 权重: {weights})")
                return weights

        # Default: balanced
        return {"热点球评": 1.0, "转会资讯": 1.0, "排行榜": 1.0, "八卦趣事": 1.0, "战术解析": 1.0}
    except Exception as e:
        print(f"   ⚠️  加载赛季权重失败: {e}")
        return None


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



def get_cross_batch_covered(date_str):
    """Check what earlier batches today have already published.

    Returns dict with covered content_types, teams, players, keywords, and titles
    so the current batch can avoid duplication.
    """
    covered = {"content_types": set(), "teams": set(), "players": set(),
               "keywords": set(), "titles": set(), "batch_count": 0}
    meta_path = OUTPUT_DIR / date_str / "metadata.json"
    if not meta_path.exists():
        return covered
    try:
        meta = json.loads(meta_path.read_text())
        for a in meta.get("articles", []):
            ct = a.get("content_type", "")
            if ct:
                covered["content_types"].add(ct)
            title = a.get("title", "")
            if title:
                covered["titles"].add(title[:30])
            for kw in a.get("keywords", []):
                covered["keywords"].add(kw.lower())
            for tag in a.get("tags", []):
                covered["keywords"].add(tag.lower())
            for team in WIKI_TEAMS:
                if team in title:
                    covered["teams"].add(team)
            for player in WIKI_PLAYERS:
                if player in title:
                    covered["players"].add(player)
        covered["batch_count"] = len(meta.get("batches_completed", []))
    except Exception:
        pass
    if covered["content_types"]:
        print(f"   跨批次去重: 今日已有 {len(meta.get('articles', []))} 篇, "
              f"覆盖品类: {', '.join(covered['content_types'])}")
    return covered


def save_batch_state(date_str, batch_name, articles_saved):
    """Update daily metadata with batch completion info for cross-batch dedup."""
    meta_path = OUTPUT_DIR / date_str / "metadata.json"
    existing = {}
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text())
        except Exception:
            pass
    batches = existing.get("batches_completed", [])
    if batch_name not in batches:
        batches.append(batch_name)
    existing["batches_completed"] = batches
    existing["last_batch"] = batch_name
    existing["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        print(f"   批次状态已更新: {', '.join(batches)}")
    except Exception as e:
        print(f"   ⚠️  批次状态保存失败: {e}")


def analyze_content_performance(date_str=None, lookback_days=30):
    """Analyze past article performance from metadata to guide topic selection.

    Scans metadata.json files from past N days, aggregates content_type frequency
    and available performance signals. Returns {content_type: performance_score} dict.
    Higher score = better performing content type.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    today = datetime.strptime(date_str, "%Y-%m-%d")

    type_stats = {}
    keyword_freq = {}
    team_freq = {}
    player_freq = {}

    for i in range(1, lookback_days + 1):
        dt = today - timedelta(days=i)
        meta_path = OUTPUT_DIR / dt.strftime("%Y-%m-%d") / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
            for a in meta.get("articles", []):
                ct = a.get("content_type", "")
                if not ct:
                    continue
                if ct not in type_stats:
                    type_stats[ct] = {"count": 0, "total_score": 0}
                perf = a.get("performance", {})
                reads = perf.get("reads", 0) if isinstance(perf, dict) else 0
                comments = perf.get("comments", 0) if isinstance(perf, dict) else 0
                article_score = 1.0 + (reads / 1000.0) + (comments / 10.0)
                type_stats[ct]["count"] += 1
                type_stats[ct]["total_score"] += article_score

                for kw in a.get("keywords", []):
                    kw_lower = kw.lower()
                    keyword_freq[kw_lower] = keyword_freq.get(kw_lower, 0) + 1
                for tag in a.get("tags", []):
                    tag_lower = tag.lower()
                    keyword_freq[tag_lower] = keyword_freq.get(tag_lower, 0) + 1
                title = a.get("title", "")
                for team in WIKI_TEAMS:
                    if team in title:
                        team_freq[team] = team_freq.get(team, 0) + 1
                for player in WIKI_PLAYERS:
                    if player in title:
                        player_freq[player] = player_freq.get(player, 0) + 1
        except Exception:
            pass

    performance = {}
    for ct, stats in type_stats.items():
        if stats["count"] > 0:
            performance[ct] = round(stats["total_score"] / stats["count"], 2)

    if performance:
        ranked = sorted(performance.items(), key=lambda x: -x[1])
        print(f"   内容表现分析(近{lookback_days}天):")
        for ct, score in ranked:
            count = type_stats[ct]["count"]
            print(f"     {ct}: {score}分 ({count}篇)")

    return {
        "performance": performance,
        "type_stats": type_stats,
        "top_keywords": sorted(keyword_freq.items(), key=lambda x: -x[1])[:15],
        "top_teams": sorted(team_freq.items(), key=lambda x: -x[1])[:10],
        "top_players": sorted(player_freq.items(), key=lambda x: -x[1])[:10],
    }


def get_performance_boost(performance_data):
    """Convert performance analysis into content type boost multipliers.

    Returns {content_type: boost_multiplier} where >1.0 means prefer, <1.0 means avoid.
    Combines with season weights for balanced optimization.
    """
    perf = performance_data.get("performance", {})
    if not perf:
        return {}

    scores = list(perf.values())
    avg_score = sum(scores) / len(scores) if scores else 1.0
    if avg_score == 0:
        return {}

    boosts = {}
    for ct, score in perf.items():
        ratio = score / avg_score
        boosts[ct] = round(max(0.5, min(1.5, ratio)), 2)

    if boosts:
        ranked = sorted(boosts.items(), key=lambda x: -x[1])
        boost_str = ", ".join(f"{ct}:{b:.1f}x" for ct, b in ranked if abs(b - 1.0) > 0.05)
        if boost_str:
            print(f"   反馈调整: {boost_str}")

    return boosts

def select_topics(match_data, gzh_articles=None, topic_history=None, preferred_types=None, season_weights=None):
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

    # Season weights hint
    weight_hint = ""
    if season_weights:
        high_types = [f"{ct}({w:.1f})" for ct, w in sorted(season_weights.items(), key=lambda x: -x[1]) if w >= 1.2]
        low_types = [f"{ct}({w:.1f})" for ct, w in sorted(season_weights.items(), key=lambda x: x[1]) if w < 0.8]
        if high_types or low_types:
            weight_hint = "\n## 赛季权重指引\n"
            if high_types:
                weight_hint += f"优先选择: {', '.join(high_types)}\n"
            if low_types:
                weight_hint += f"降低频率: {', '.join(low_types)}\n"

    prompt = f"""你是头条号足球博主"球评人老六"。以下是 {match_data['date']} 的真实比赛结果。请筛选 3 个有爆款潜力的话题。

比赛数据：
{"".join(lines)}
{gzh_text}
{history_text}
{weight_hint}

硬性要求 — 3 个话题必须覆盖不同内容类型：
1. 第1篇：热点球评 — 从当日比赛中选最有话题性的一场
2. 第2篇：转会资讯/八卦趣事 — 转会传闻、球员花边、冲突争议、场外话题
3. 第3篇：排行榜/战术解析/八卦趣事 — 数据榜单或战术趋势

如果当日有绝杀、逆转、红牌、VAR争议、教练冲突等事件，优先选择。

风格要求：像老球迷喝酒聊天一样自然，有明确立场和情绪，不骑墙、不套模板。
避免：任何过去7天已报道过的球队/球员/话题。

输出纯JSON数组：
[{{"title": "标题(15-25字)", "angle": "切入角度+明确态度", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "content_type": "热点球评/转会资讯/排行榜/八卦趣事/战术解析", "score": 90, "controversy_level": "high/medium/low", "target_emotion": "愤怒/骄傲/怀旧/震惊/感动/好奇", "why_pick": "为什么选这个角度(20字)"}}]
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是头条号足球博主'球评人老六'，有态度、有人味、不骑墙。严格按要求分配内容类型，避开历史话题。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-flash", messages, temperature=0.7, max_tokens=4096)
    topics = safe_json_loads(response)
    print(f"   筛选出 {len(topics)} 个话题:")
    for i, t in enumerate(topics):
        print(f"   {i+1}. [{t.get('content_type', 'N/A')}] {t['title'][:50]}")
    return topics


def collect_real_gzh_topics(date_str, topic_history=None, topic_preference="auto", preferred_types=None, season_weights=None):
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
    if preferred_types:
        n = len(preferred_types)
        type_requirement = f"{n}个选题全部按指定类型：{' 和 '.join(preferred_types)}。优先选择热度最高、最有话题性的。"
        system_msg = f"你是头条号足球博主'球评人老六'，有态度有人味。绝不洗稿，跨源合成+新观点=全新原创。本次仅出{preferred_types}类选题。只输出JSON。"
    elif topic_preference == "transfer":
        type_requirement = "3个选题必须全部是转会/签约/续约/离队/绯闻/花边/下课类型。优先选择热度最高、最有话题性的。"
        system_msg = "你是头条号足球博主'球评人老六'，有态度有人味。绝不洗稿，跨源合成+新观点=全新原创。全部选题聚焦转会资讯/八卦趣事/场外话题。只输出JSON。"
    else:
        type_requirement = """硬性要求 — 3个选题必须覆盖不同内容类型：
1. 第1篇：热点球评 — 从当日比赛中选最有话题性的一场
2. 第2篇：转会资讯/八卦趣事 — 转会传闻、球员花边、场外话题
3. 第3篇：排行榜/战术解析/八卦趣事 — 数据榜单或战术趋势"""
        system_msg = "你是头条号足球博主'球评人老六'，有态度有人味。绝不洗稿，跨源合成+新观点=全新原创。严格按对应内容类型分配。只输出JSON。"

    # Season weights hint for topic selection
    weight_hint = ""
    if season_weights:
        high_types = [f"{ct}({w:.1f})" for ct, w in sorted(season_weights.items(), key=lambda x: -x[1]) if w >= 1.2]
        low_types = [f"{ct}({w:.1f})" for ct, w in sorted(season_weights.items(), key=lambda x: x[1]) if w < 0.8]
        if high_types or low_types:
            weight_hint = "\n## 赛季权重指引\n"
            if high_types:
                weight_hint += f"优先选择: {', '.join(high_types)}\n"
            if low_types:
                weight_hint += f"降低频率: {', '.join(low_types)}\n"

    print(f"\n[2/5] 基于真实爆款数据筛选选题 (DeepSeek, mode={topic_preference})...")
    prompt = f"""你是头条号足球博主"球评人老六"。以下是公众号平台最近2天真实爆款足球文章数据，请从中选出3个最有二次创作价值的选题。

{weight_hint}

真实爆款文章数据（已按时效性+热度排序，pub_time为发布日期）：
{json.dumps(articles_text, ensure_ascii=False)}
{history_text}

⚠️ 时效性硬性要求：只能选择 pub_time 为 {yesterday_str} 或 {today_str} 的话题。超过2天的旧闻一律不用。

{type_requirement}

二次创作原则：借话题方向，不借标题和内容。用新角度、新观点、新表达重写。绝不可照搬原文标题或金句。

输出纯JSON：
[{{"title": "新标题(15-25字)", "source_article_ids": [引用文章id], "source_titles": ["原文标题"], "angle": "切入角度+新观点", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "content_type": "热点球评/转会资讯/排行榜/八卦趣事/战术解析", "controversy_level": "high/medium/low", "target_emotion": "愤怒/骄傲/怀旧/震惊/感动/好奇"}}]
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

    # Style guidance by content type (5 categories)
    style_guide = {
        "热点球评": "像赛后和球友喝酒复盘：先讲最刺激的瞬间，再拆关键战术细节，最后给个痛快结论。少列数据，多讲故事和感受。",
        "转会资讯": "像球迷群里的八卦消息：分析转会的「为什么」和「影响」，结合球队需求和球员处境。有趣味但不编造，有逻辑但不学术。",
        "排行榜": "用对比制造冲突，把数据融入叙事而非堆表格。每个上榜人物都要有槽点或亮点，不能让读者觉得是干巴巴的列表。",
        "八卦趣事": "像给朋友讲一个你佩服（或不爽）的球员：聚焦一个侧面、一个瞬间，有画面感。带点吃瓜的调侃味，不写流水账。",
        "战术解析": "把复杂的战术概念用大白话讲清楚，让普通球迷也能看懂。数据辅助观点，不反客为主。让读者看完有「原来如此」的感觉。",
        # Legacy compatibility
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
这次必须修正上述所有问题。正文至少500字，至少2个##小标题，文末至少2个配图标记。"""

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。今天的任务是基于真实数据写一篇有观点的足球文章——不是编造，是用数据说话。

今日话题：{topic['title']}
切入角度：{topic['angle']}
内容类型：{content_type}
目标情绪：{topic.get('target_emotion', '好奇')}

你的素材（只能使用以下数据中的事实）：
{context_str[:3000]}
{gzh_text}
{retry_block}

写作规则：
1. **事实来自素材**：文章中的数据、比分、排名、球队名称必须来自上面的数据。素材里没有的球员名字、比赛细节、转会金额，不要写。
2. **观点来自你**：在事实基础上，你可以分析、质疑、对比、预测。但要区分"数据说X"和"老六认为Y"。
3. **有多少写多少**：如果数据只够写500字，就写500字紧凑的内容，不要注水。

写作要求：
{style}

结构：开篇钩子（用数据中一个有意思的点切入）→ 2个小节展开分析 → 收尾观点+互动

硬性规范：
- 正文 500-800 字（硬性要求，紧凑有力，有多少事实写多少字，不要水字数）
- 必须包含 ≥2 个 ## 二级标题
- 文末必须包含2张配图标记：![配图1](images/article-{index}-img-001.jpg) 等
- 事实红线：素材里没有的数据/事件/引语，一律不写。有几分数据说几分话

禁用词：震惊、吓尿、哭惨、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说
禁用模式：不要每段都以"老六认为"开头，不要像写论文一样列一二三四

输出JSON:
{{"title": "优选标题(15-25字)", "backup_title": "备选标题(不同角度，15-25字)", "content": "Markdown正文(500-800字，含≥2个##小标题，文末含2个配图标记)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "站队式/投票式/预测式/共鸣式/挑战式/调侃式", "interaction_bait": "互动问题", "content_type": "{content_type}"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": f"你是头条号足球博主'球评人老六'，10万粉丝。核心原则：事实来自素材，观点来自你。素材里没有的绝不编造。风格：{style} 用自然口语化中文写作，有态度有人味。只输出JSON。"},
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
        summary = (s.get("summary", "") or "")[:150]
        sources_text += f"\n来源{i+1}：{s.get('title', '')[:80]}\n  账号：{s.get('account', '?')} | 阅读：{s.get('reads', '?')}\n  摘要：{summary}\n"

    bg_text = "".join(f"- [{a.get('reads', '?')}阅读] {a.get('title', '')[:80]} | {a.get('account', '?')}\n"
                      for a in all_articles[:8])

    # Style guidance by content type
    style_guide = {
        "热点球评": "像赛后和球友喝酒复盘：先讲最刺激的瞬间，再拆关键战术细节，最后给个痛快结论。少列数据，多讲故事和感受。",
        "转会资讯": "像球迷群里的八卦：分析转会为什么发生、对各方的影响。有趣的推测但不编造事实，有逻辑但不写学术论文。如有多个信源可交叉印证。",
        "排行榜": "用对比制造冲突，把数据融入叙事而非堆表格。每个上榜人物都要有槽点或亮点，不能让读者觉得是干巴巴的列表。",
        "八卦趣事": "像讲述一个你佩服（或不爽）的球员：聚焦一个侧面、一段经历、一个瞬间。有细节、有情感、有画面。不写流水账。",
        "战术解析": "像老球皮分析足坛走向：从现象中提炼规律，用关键事实说话。让读者看完有「原来如此」的感觉。",
        # Legacy compatibility
        "转会八卦型": "像球迷群里的八卦：分析转会为什么发生、对各方的影响。有趣的推测但不编造事实，有逻辑但不写学术论文。",
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
这次必须修正上述所有问题。正文至少500字，至少2个##小标题，文末至少2个配图标记。"""

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。今天的任务不是凭空创作，而是**基于真实热点文章转写改编**。

你的素材 — 公众号平台真实爆款文章（这些是真实存在的文章，写的是真实发生的事件）：
{sources_text}

同期其他热点（了解语境）：
{bg_text}

内容类型：{content_type}
切入角度：{topic.get('angle', '独特角度')}
{retry_block}

转写改编规则（非常重要）：
1. **事实继承**：源文章里写了什么事件、什么数据，你才能写什么。源文章没提到的人物、比分、细节，一律不写。
2. **角度变换**：用不同的切入角度和叙事顺序重新组织，但不能改事实。比如源文章写"A转会B队"，你可以从B队战术需求、A的职业生涯选择、转会费是否合理等不同角度切入。
3. **语言重写**：用你自己的话、自己的节奏、自己的金句。绝不可照搬源文章的任何完整句子。
4. **观点升级**：在源文章事实基础上，加上你作为老球迷的分析和态度。但分析要标注清楚是"推测"还是"事实"。
5. **时效性**：只写最近1-2天的事件。如有旧闻，必须找最新关联角度。

写作风格：
{style}

硬性规范：
- 正文 500-800 字（硬性要求，紧凑有力，不要水字数）
- 必须包含 ≥2 个 ## 二级标题
- 文末必须包含2张配图标记：![配图1](images/article-{index}-img-001.jpg) 等
- 事实红线：文章主体必须基于上面提供的来源摘要，有几分事实说几分话，不可凭空编造细节

禁用词：震惊、吓尿、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说
禁用模式：不要列一二三四，不要太强的论文感

输出JSON:
{{"title": "优选标题(15-25字)", "backup_title": "备选标题(不同角度，15-25字)", "content": "Markdown正文(500-800字，含≥2个##小标题，文末含2个配图标记)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "站队式/投票式/预测式/共鸣式/挑战式/调侃式", "interaction_bait": "互动问题", "content_type": "{content_type}", "sources_used": ["来源文章标题"], "originality_note": "如何区别于原文(20字)"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": f"你是头条号足球博主'球评人老六'，有态度有人味。你的工作是转写改编真实热点文章：用新角度新语言重新组织事实，加自己的分析态度。事实来自素材，观点来自你。绝不编造素材里没有的事实。风格：{style} 用自然口语化中文写作。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-pro", messages, temperature=temperature, max_tokens=8192)
    article = safe_json_loads(response)
    print(f"   标题: {article.get('title','?')}, 正文: {len(article.get('content',''))}字")
    return article


# ============================================================
# Quality Validation & Retry
# ============================================================

def validate_article(article, index, is_tieba=False):
    """Validate article quality. Returns (is_valid, issues_list, originality_score)."""
    issues = []
    score = 100  # Start from 100, deduct for each issue
    content = article.get("content", "")
    title = article.get("title", "")
    min_words = 300 if is_tieba else 500
    min_images = 3 if is_tieba else 2
    min_h2 = 3 if is_tieba else 2

    if not title or len(title) < 10:
        issues.append(f"标题过短({len(title)}字,需≥10)")
        score -= 15
    elif len(title) > 32:
        issues.append(f"标题过长({len(title)}字,需≤32)")
        score -= 10

    if not content or len(content) < min_words:
        issues.append(f"正文字数不足({len(content)}字,需≥{min_words})")
        score -= 20

    h2_count = len(re.findall(r'^## ', content, re.MULTILINE))
    if h2_count < min_h2:
        issues.append(f"缺少小标题(仅{h2_count}个##,需≥{min_h2})")
        score -= 10

    img_count = len(re.findall(r'!\[.*?\]\(images/', content))
    if img_count < min_images:
        issues.append(f"配图标记不足({img_count}个,需≥{min_images})")
        score -= 10

    if content.strip() == "":
        issues.append("正文为空")
        score = 0
        return False, issues, 0

    # Originality check: banned words
    banned_words = ["震惊", "吓尿", "哭惨", "看傻了"]
    for bw in banned_words:
        if bw in content:
            issues.append(f"禁用词: {bw}")
            score -= 20

    # Originality check: AI cliche patterns
    ai_cliches = ["众所周知", "值得一提的是", "从某种意义上说", "不得不说", "不可否认",
                  "总而言之", "首先其次最后", "让我们来看看", "接下来我们分析"]
    for cliche in ai_cliches:
        if cliche in content:
            issues.append(f"AI套话: {cliche}")
            score -= 10

    # Originality check: source article overlap (if available)
    sources = article.get("sources_used", [])
    if sources:
        for src in sources:
            src_short = src[:30] if len(src) > 30 else src
            if len(src_short) >= 8 and src_short in content:
                issues.append(f"疑似照搬来源: {src[:30]}")
                score -= 30

    return len(issues) == 0, issues, max(score, 0)


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
                last_issues = f"上次正文仅{len(content)}字，远低于500字最低要求。请基于提供的事实数据充实内容。"
                continue

            is_valid, issues, score = validate_article(art, index, is_tieba=is_tieba)
            if is_valid and score >= 85:
                if attempt > 0:
                    print(f"   ✅ 第{attempt+1}次尝试通过 (原创度: {score})")
                return art, None

            if not is_valid:
                print(f"   ⚠️  验证失败: {'; '.join(issues)}")
            elif score < 85:
                print(f"   ⚠️  原创度不足 ({score}/100，需≥85): {'; '.join(issues)}")
            last_issues = "; ".join(issues)
            if attempt < max_retries:
                print(f"   🔄 重试 (temperature={temp}, 加强约束)...")
            else:
                return art, f"验证失败({max_retries+1}次, 原创度{score}): {'; '.join(issues)}"

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


def generate_tieba_article(topic, index, post_data, temperature=0.8, retry_hint=""):
    """Generate article from a single real Hupu post + its replies. No fabrication."""
    team = post_data.get("team", "")
    post_title = post_data.get("title", "")
    main_content = post_data.get("main_content", "")
    replies = post_data.get("top_replies", [])
    reply_num = post_data.get("reply_num", 0)

    print(f"\n[Hupu-{index}] {team}: {post_title[:40]} ({reply_num}回复)...")

    # Build the single-post context
    context = f"【{team}专区】原帖标题：{post_title}\n"
    context += f"回复数：{reply_num}\n\n"
    if main_content:
        context += f"原帖内容：\n{main_content[:500]}\n\n"

    if replies:
        context += "网友热门回复（按点赞数排序）：\n"
        for j, r in enumerate(replies):
            author = r.get("author", "匿名")
            agree = r.get("agree_count", 0)
            content = r.get("content", "")
            context += f"\n--- 回复{j+1}：{author}（{agree}赞）---\n"
            context += f"{content[:300]}\n"
    else:
        context += "（暂无高赞回复）\n"

    retry_block = ""
    if retry_hint:
        retry_block = f"""
⚠️ 上次生成失败！问题：{retry_hint}
这次必须修正。正文300-500字，3个##小标题，每个小标题后紧跟一张配图标记。"""

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。下面是虎扑上一个真实帖子和网友回复。你的任务不是凭空创作，而是**基于这个帖子的内容进行二次创作**。

=== 真实帖子数据（唯一素材来源） ===
{context[:6000]}

=== 二次创作规则（必须遵守） ===
1. **事实来自帖子**：文章中出现的球迷观点、言论、情绪，必须能从上面找到出处。帖子里没说的，不要写。
2. **引用真实回复**：直接引用网友回复中的原话（用引号标注），然后展开你的分析。这是文章的灵魂。
3. **分析可以延伸**：在球迷讨论的基础上，你可以分析为什么会有这些观点、背后反映了什么。但要标注"老六分析""推测"等，和球迷原话区分开。
4. **不编造不注水**：有多少素材写多少字。如果素材只够300字，就写300字干货。不要为了凑字数添加虚假细节。
{retry_block}

结构要求（3段+3图，紧凑编排）：
- 第1段（开篇引子）：直接引用帖子里最精彩的回复作为引子 → 紧跟配图1
- 第2段（展开分析）：围绕球迷讨论展开1-2层分析，引用真实回复为论据 → 紧跟配图2
- 第3段（收尾观点）：总结你的观点 + 抛出一个问题让读者互动 → 紧跟配图3

硬性规范：
- 正文 300-500 字（精炼有力，不要水字数）
- 必须包含 3 个 ## 二级标题（每段一个）
- 每个 ## 小标题段落后紧跟一张配图标记，共3张：
  ![配图1](images/article-{index}-img-001.jpg)
  ![配图2](images/article-{index}-img-002.jpg)
  ![配图3](images/article-{index}-img-003.jpg)
- **事实底线**：每条球迷观点必须有出处，找不到出处的不要写

禁用词：震惊、吓尿、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说

输出JSON:
{{"title": "优选标题(15-25字，有网感有态度)", "backup_title": "备选标题(不同角度，15-25字)", "content": "Markdown正文(300-500字，含3个##小标题+3张配图)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "站队式/投票式/预测式/共鸣式/挑战式/调侃式", "interaction_bait": "互动问题", "content_type": "球迷讨论", "source_post": "{post_title[:50]}"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是头条号足球博主'球评人老六'，有态度有人味。核心原则：素材即边界。文章所有球迷观点必须来自提供的帖子数据，分析可以延伸但需标注。不编造，不注水。用自然口语化中文写作。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-pro", messages, temperature=temperature, max_tokens=8192)
    article = safe_json_loads(response)
    print(f"   标题: {article.get('title','?')}, 正文: {len(article.get('content',''))}字")
    return article


# ============================================================
# Save Articles (Local, no Flask)
# ============================================================

def save_articles_local(date_str, articles, images_map, topics, match_data, extra=None,
                        pre_downloaded_images=None):
    """Save articles directly to filesystem (no Flask dependency).

    pre_downloaded_images: dict mapping article index (0-based in articles list)
                           to list of already-downloaded image info dicts.
                           When present, skips URL download for that article.
    """
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
    pre_downloaded = pre_downloaded_images or {}

    for i, art in enumerate(articles):
        idx = i + 1
        prefix = f"article-{idx}-img"

        downloaded = []
        if i in pre_downloaded:
            # Use pre-downloaded (already cropped) images
            for img_info in pre_downloaded[i]:
                if len(downloaded) >= 3:
                    break
                if img_info.get("md5"):
                    all_hashes.add(img_info["md5"])
                downloaded.append(img_info)
        else:
            # Download images from URLs
            img_urls = [img["url"] for img in images_map.get(i, [])[:5]]
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
        # Strip auto-generated markers first to sync with actual downloaded count
        content = re.sub(r'!\[配图\d+\]\(images/article-\d+-img-\d+\.jpg\)\n?', '', content)
        if downloaded:
            # Inject downloaded images into content as fallback
            # For articles with ## sections, place one image after each section
            sections = content.split("\n## ")
            if len(sections) > 1 and len(downloaded) >= 2:
                new_parts = [sections[0]]
                for si, sec in enumerate(sections[1:]):
                    sec_text = ("## " + sec) if si == 0 else ("## " + sec)
                    new_parts.append(sec_text)
                    if si < len(downloaded):
                        img = downloaded[si]
                        new_parts.append(
                            f"\n![{img.get('description', f'配图{si+1}')}](images/{img['filename']})\n")
                content = "\n".join(new_parts)
            else:
                # Old logic: proportional insertion
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
                       "source_post": art.get("source_post", ""),
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
# Major Event Detection & Emergency Article Trigger
# ============================================================

def detect_major_events(match_data, gzh_articles=None):
    """Detect significant football events that warrant immediate coverage.

    Scans match data for comebacks, red cards, high-scoring games, upsets,
    and checks GZH trends for breaking news with viral potential.

    Returns list of events sorted by urgency (highest first).
    """
    events = []

    # 1. Scan match data for significant events
    for league, fixtures in match_data.get("fixtures_by_league", {}).items():
        for m in fixtures:
            home = m.get("home_team", "")
            away = m.get("away_team", "")
            hg = m.get("home_score")
            ag = m.get("away_score")

            if hg is None or ag is None:
                continue

            total_goals = hg + ag

            # High-scoring thriller (5+ goals)
            if total_goals >= 5:
                events.append({
                    "type": "进球大战",
                    "title_hint": f"{home} {hg}-{ag} {away}，{total_goals}球对攻大战",
                    "urgency": min(90, 60 + total_goals * 5),
                    "league": league,
                    "detail": f"{league}: {home} {hg}-{ag} {away} (共{total_goals}球)",
                })

            # One-sided blowout (4+ goal difference)
            if abs(hg - ag) >= 4:
                winner = home if hg > ag else away
                events.append({
                    "type": "惨案",
                    "title_hint": f"{winner}血洗对手，{abs(hg-ag)}球大胜震惊{league}",
                    "urgency": min(85, 55 + abs(hg - ag) * 7),
                    "league": league,
                    "detail": f"{league}: {home} {hg}-{ag} {away} ({abs(hg-ag)}球差距)",
                })

            # Major upset: use standings to detect (low-ranked beating high-ranked)
            # Simplified: flag any match where both teams scored 3+
            if hg >= 3 and ag >= 3:
                events.append({
                    "type": "神仙打架",
                    "title_hint": f"{home}和{away}互捅刀子，{total_goals}球神仙打架",
                    "urgency": 75,
                    "league": league,
                    "detail": f"{league}: {home} {hg}-{ag} {away}",
                })

            # Clean sheet blowout by underdog (simplified heuristic)
            if (hg >= 3 and ag == 0) or (ag >= 3 and hg == 0):
                big_team = home if hg >= 3 else away
                shutout_team = away if hg >= 3 else home
                events.append({
                    "type": "碾压局",
                    "title_hint": f"{big_team}{'主场' if hg >= 3 else '客场'}碾压{shutout_team}",
                    "urgency": 65,
                    "league": league,
                    "detail": f"{league}: {home} {hg}-{ag} {away}",
                })

    # 2. Scan GZH trends for breaking news
    if gzh_articles:
        breaking_keywords = ["重磅", "官宣", "下课", "突发", "绝杀", "逆转", "冲突", "红牌",
                            "解雇", "签约", "宣布", "确诊", "重伤", "退役", "告别"]
        for a in gzh_articles:
            title = a.get("title", "")
            summary = a.get("summary", "") or ""
            text = title + summary
            matched_kws = [kw for kw in breaking_keywords if kw in text]
            if matched_kws:
                reads = a.get("clicksCount", 0)
                # Viral potential: high reads + breaking keywords
                viral_score = min(95, 60 + len(matched_kws) * 5 + (reads // 10000) * 2)
                events.append({
                    "type": "突发新闻",
                    "title_hint": title[:60],
                    "urgency": min(95, viral_score),
                    "source": "GZH trending",
                    "detail": f"公众号爆款: {title[:60]} (阅读:{reads})",
                    "gzh_article": a,
                })

    # Deduplicate by title_hint
    seen = set()
    unique = []
    for e in sorted(events, key=lambda x: -x["urgency"]):
        hint = e.get("title_hint", "")[:40]
        if hint not in seen:
            seen.add(hint)
            unique.append(e)

    if unique:
        top = unique[:3]
        print(f"   ⚡ 检测到 {len(unique)} 个重大事件，前{len(top)}个:")
        for i, e in enumerate(top):
            print(f"   {i+1}. [{e['type']}][urg={e['urgency']}] {e['detail'][:60]}")

    return unique


def generate_emergency_article(event, match_data, index, temperature=0.8):
    """Generate a focused emergency article for a major event."""
    event_type = event.get("type", "突发新闻")
    title_hint = event.get("title_hint", "")
    detail = event.get("detail", "")

    print(f"\n[紧急] [{event_type}] 快速生成突发球评: {title_hint[:40]}...")

    fixtures = match_data.get("fixtures_by_league", {})
    context_str = json.dumps({
        "event_type": event_type,
        "event_detail": detail,
        "matches": fixtures,
        "urgency_level": event.get("urgency", 70),
    }, ensure_ascii=False)

    # Style for emergency articles: urgent, punchy
    style = "突发新闻快评风格：开篇直接冲事件核心，节奏快，短句多，像第一条推送。300-400字即可，有冲击力，有明确态度。"

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。刚刚发生了一件大事，需要你立刻写一篇快评！

⚠️ 重大事件：{title_hint}
事件详情：{detail}
事件类型：{event_type}

背景数据：
{context_str[:2000]}

写作要求：
{style}

结构：开篇事件核心（一句话出态度）→ 快速分析为什么重要 → 收尾观点（抛给读者讨论）

硬性规范：
- 正文 300-500 字（快评，不要求长文，但要够犀利）
- 必须包含 ≥2 个 ## 二级标题
- 文末至少1张配图标记：![配图1](images/article-{index}-img-001.jpg)
- 态度要鲜明，不要骑墙

禁用词：震惊、吓尿、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说

输出JSON:
{{"title": "标题(15-25字，有冲击力)", "backup_title": "备选标题", "content": "Markdown正文(300-500字，含≥2个##小标题，文末配图)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "站队式/投票式/预测式/共鸣式/挑战式/调侃式", "interaction_bait": "互动问题", "content_type": "紧急球评", "event_type": "{event_type}"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": f"你是头条号足球博主'球评人老六'，擅长突发事件快评。{style} 只输出JSON。"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-pro", messages, temperature=temperature, max_tokens=4096)
    article = safe_json_loads(response)
    print(f"   紧急球评标题: {article.get('title','?')}, 正文: {len(article.get('content',''))}字")
    return article


# ============================================================
# Main
# ============================================================

def _generate_articles_from_topics(topics, count, match_data, images_map, stats,
                                    articles_out, is_gossip=False, gzh_articles=None):
    """Shared article generation loop — used by all three data-source branches."""
    for i, topic in enumerate(topics[:count]):
        ct = topic.get("content_type", "N/A")
        print(f"\n--- 第{i+1}/{count}篇 [{ct}] ---")
        imgs = search_images(topic, count=5)
        images_map[i] = imgs
        kwargs = {"max_retries": 2}
        if is_gossip:
            kwargs["is_gossip"] = True
        if gzh_articles is not None:
            kwargs["gzh_articles"] = gzh_articles
        art, error = generate_article_with_retry(topic, match_data, i + 1, **kwargs)
        stats["generated"] += 1
        if error:
            print(f"   ❌ 最终失败: {error}")
            stats["failed"] += 1
            stats["issues"].append(f"第{i+1}篇({ct}): {error}")
        else:
            stats["valid"] += 1
        articles_out.append((i, art))


def _run_hupu_pipeline(date_str, batch_mode, topic_history, match_data,
                        articles, topics, images_map, stats, extra_meta):
    """Hupu fan discussion pipeline — generates articles 4-6 from top 3 hot posts."""
    pre_downloaded = {}
    try:
        if batch_mode != "auto":
            print("   [批次模式] 跳过虎扑数据采集")
            return pre_downloaded

        print("\n--- 虎扑球迷讨论数据源 ---")
        tieba_data = collect_tieba_data(date_str)
        if not tieba_data or not tieba_data.get("raw_posts"):
            print("   虎扑无有效数据，跳过球迷讨论文章（不影响主文章）")
            return pre_downloaded

        raw_posts = tieba_data["raw_posts"]
        hupu_history_titles = topic_history.get("titles", set())
        filtered_posts = []
        for p in raw_posts:
            title = p.get("title", "")
            if any(len(ht) >= 8 and ht[:15] in title for ht in hupu_history_titles):
                print(f"   ⏭️  跳过(历史重复): {title[:40]}")
                continue
            filtered_posts.append(p)
        if len(filtered_posts) < 3:
            print(f"   去重后仅剩 {len(filtered_posts)} 帖，保留原始排序补充")
            existing_titles = {p['title'] for p in filtered_posts}
            for p in raw_posts:
                if len(filtered_posts) >= max(3, len(raw_posts[:10])):
                    break
                if p['title'] not in existing_titles:
                    filtered_posts.append(p)
                    existing_titles.add(p['title'])

        top3_posts = filtered_posts[:3]
        extra_meta["hupu"] = True
        extra_meta["hupu_posts"] = [
            {"team": p["team"], "title": p["title"], "reply_num": p["reply_num"]}
            for p in top3_posts
        ]

        hupu_images_dir = OUTPUT_DIR / date_str / "images"
        hupu_images_dir.mkdir(parents=True, exist_ok=True)
        img_service = ImageService(config={
            "images": {"min_width": 600, "min_height": 400, "max_size_bytes": 5242880,
                       "min_size_bytes": 20480, "max_per_article": 5, "required_per_article": 2}})

        for ti, post in enumerate(top3_posts):
            t_idx = len(articles) + ti + 1
            print(f"\n--- 第{t_idx}/6篇 [虎扑热帖 #{ti+1}] {post['team']}: {post['title'][:40]} ({post['reply_num']}回复) ---")

            post_images = post.get("images", [])
            hupu_imgs = []
            if post_images:
                print(f"   帖子含 {len(post_images)} 张图片，下载并裁剪水印...")
                for j, img_url in enumerate(post_images[:3]):
                    result = img_service.download_and_crop_image(
                        url=img_url, target_dir=hupu_images_dir,
                        prefix=f"article-{t_idx}-img", index=j + 1)
                    if result:
                        hupu_imgs.append(result)
                        print(f"   ✅ 图片{j+1}: {result['filename']} ({result['width']}x{result['height']})")
                    else:
                        print(f"   ⚠️  图片{j+1}下载/裁剪失败")
                    time.sleep(0.5)

            if hupu_imgs:
                if len(hupu_imgs) < 3:
                    print(f"   仅 {len(hupu_imgs)} 张帖子图片，搜索补充...")
                    fallback = search_images(
                        {"title": post['title'], "keywords_cn": [post['team']]},
                        count=3 - len(hupu_imgs))
                    for fb in fallback:
                        if len(hupu_imgs) >= 3:
                            break
                        result = img_service.download_image(
                            url=fb["url"], target_dir=hupu_images_dir,
                            prefix=f"article-{t_idx}-img",
                            index=len(hupu_imgs) + 1,
                            existing_hashes=set())
                        if result:
                            result["source"] = fb.get("source", "search")
                            hupu_imgs.append(result)
                            print(f"   ✅ 补充图片{len(hupu_imgs)}: {result['filename']}")
                    time.sleep(0.5)

                pre_downloaded[len(articles) + ti] = hupu_imgs
                images_map[len(articles) + ti] = [
                    {"url": img["url"], "source": img.get("source", "hupu")}
                    for img in hupu_imgs
                ]
            else:
                print(f"   无可用帖子图片，使用通用图片搜索")
                imgs = search_images({"title": post['title'], "keywords_cn": [post['team']]}, count=5)
                images_map[len(articles) + ti] = imgs

            art, error = generate_article_with_retry(
                {"title": post['title'], "team": post['team']},
                match_data, t_idx,
                is_tieba=True, tieba_context=post, max_retries=2)
            stats["generated"] += 1
            if error:
                print(f"   ❌ 最终失败: {error}")
                stats["failed"] += 1
                stats["issues"].append(f"第{t_idx}篇(虎扑): {error}")
            else:
                stats["valid"] += 1
            articles.append((len(articles), art))
            topics.append({"title": post['title'], "content_type": f"球迷讨论-{post['team']}"})
    except Exception as e:
        print(f"   ⚠️  虎扑数据采集/生成失败（不影响主文章）: {e}")
    return pre_downloaded


def main():
    # Parse args: python orchestrator.py [YYYY-MM-DD] [--topic=auto|transfer|match] [--batch=auto|morning|noon|evening]
    date_str = None
    topic_preference = "auto"
    batch_mode = "auto"
    for arg in sys.argv[1:]:
        if arg.startswith("--topic="):
            topic_preference = arg.split("=", 1)[1]
        elif arg.startswith("--batch="):
            batch_mode = arg.split("=", 1)[1]
        elif not arg.startswith("--"):
            date_str = arg
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # Load season weights for content type optimization
    season_weights = load_season_weights(date_str)
    performance_boost = {}

    if batch_mode in BATCH_TYPES:
        target_types = list(BATCH_TYPES[batch_mode])
        article_count = 2

        # Apply season weights + performance boost: swap low-weight types for high-weight alternatives
        if season_weights:
            # Merge performance boost into effective weights
            effective_weights = dict(season_weights)
            if performance_boost:
                for ct, boost in performance_boost.items():
                    if ct in effective_weights:
                        effective_weights[ct] = round(effective_weights[ct] * boost, 2)
            for i, ct in enumerate(target_types):
                w = effective_weights.get(ct, 1.0)
                if w < 0.7:
                    candidates = sorted(effective_weights.items(), key=lambda x: -x[1])
                    for alt_type, alt_w in candidates:
                        if alt_w > 1.3 and alt_type not in target_types:
                            print(f"   🔄 赛季权重调整: {ct}(权重{w}) → {alt_type}(权重{alt_w})")
                            target_types[i] = alt_type
                            break

        print(f"足球自媒体内容自动化 - {date_str} (batch={batch_mode}, types={target_types})\n")
    else:
        target_types = None
        article_count = 3
        print(f"足球自媒体内容自动化 - {date_str} (topic={topic_preference})\n")

    start_time = time.time()
    log.info(f"开始执行 — 日期:{date_str} 批次:{batch_mode} 偏好:{topic_preference}")
    success = False
    result_msg = ""
    stats = {"generated": 0, "valid": 0, "failed": 0, "issues": []}
    extra_meta = {}

    try:
        # Step 0: Load topic history for dedup
        topic_history = get_topic_history(date_str)
        # Cross-batch dedup: check what earlier batches already published today
        cross_batch_covered = get_cross_batch_covered(date_str)
        # Performance feedback: analyze past content performance for topic optimization
        performance_data = analyze_content_performance(date_str, lookback_days=30)
        performance_boost = get_performance_boost(performance_data)

        # Step 1: Collect match data (always, for context)
        match_data = collect_real_matches(date_str)

        # Data availability check: adjust target types if no supporting data
        if target_types:
            original_types = list(target_types)
            # Check 热点球评 feasibility
            if "热点球评" in target_types and match_data["total_matches"] == 0:
                fb = FALLBACK_MAP["热点球评"]
                target_types = [fb if t == "热点球评" else t for t in target_types]
                print(f"   ⚠️  无比赛数据，热点球评 → {fb}")

            # Check 排行榜 feasibility
            if "排行榜" in target_types:
                rankings_data = fetch_rankings_data()
                has_rankings = bool(rankings_data.get("scorers") or rankings_data.get("standings"))
                if not has_rankings:
                    fb = FALLBACK_MAP["排行榜"]
                    target_types = [fb if t == "排行榜" else t for t in target_types]
                    print(f"   ⚠️  无排行榜数据，排行榜 → {fb}")
                else:
                    print(f"   ✅ 排行榜数据可用：{len(rankings_data.get('scorers', {}))}个联赛射手榜, {len(rankings_data.get('standings', {}))}个积分榜")

            if target_types != original_types:
                print(f"   调整后品类: {target_types}")
                # Deduplicate if fallback created duplicates
                seen = set()
                deduped = []
                for t in target_types:
                    if t not in seen:
                        seen.add(t)
                        deduped.append(t)
                if len(deduped) < len(target_types):
                    # Fill in missing slots with types not already selected
                    all_types = ["八卦趣事", "转会资讯", "战术解析", "热点球评", "排行榜"]
                    for at in all_types:
                        if len(deduped) >= article_count:
                            break
                        if at not in seen:
                            deduped.append(at)
                            seen.add(at)
                    target_types = deduped[:article_count]
                    print(f"   去重后品类: {target_types}")

        articles = []
        images_map = {}
        topics = []

        # Cross-batch content type dedup: avoid repeating types already covered today
        if batch_mode in BATCH_TYPES and cross_batch_covered.get("content_types"):
            already_covered_types = cross_batch_covered["content_types"]
            for i, ct in enumerate(target_types):
                if ct in already_covered_types:
                    # Find unused alternative with highest season weight
                    candidates = sorted(season_weights.items(), key=lambda x: -x[1]) if season_weights else []
                    all_types = ["八卦趣事", "转会资讯", "战术解析", "热点球评", "排行榜"]
                    for alt_type, _ in candidates:
                        if alt_type not in already_covered_types and alt_type not in target_types:
                            print(f"   🔄 跨批次去重: {ct}(已在今日覆盖) → {alt_type}")
                            target_types[i] = alt_type
                            already_covered_types.add(alt_type)
                            break
                    else:
                        # Fallback: try any unused type
                        for alt_type in all_types:
                            if alt_type not in already_covered_types and alt_type not in target_types:
                                print(f"   🔄 跨批次去重(fallback): {ct}(已覆盖) → {alt_type}")
                                target_types[i] = alt_type
                                already_covered_types.add(alt_type)
                                break

        # ============================================================
        # Main Article Pipeline (articles 1-3)
        # ============================================================

        if topic_preference != "auto":
            print(f"   用户偏好: {topic_preference}，使用公众号爆款数据为主\n")
            topics_and_raw = collect_real_gzh_topics(
                date_str, topic_history, topic_preference=topic_preference, preferred_types=target_types, season_weights=season_weights)
            if not topics_and_raw or not topics_and_raw[0]:
                result_msg = f"无{topic_preference}相关真实爆款数据可用"
                print(f"ERROR: {result_msg}")
                send_wxpusher("足球自媒体 ⚠️", f"{date_str} 发文任务中止：{result_msg}")
                return

            topics, raw_articles = topics_and_raw
            extra_meta = {"type": f"gzh_{topic_preference}"}

            _generate_articles_from_topics(topics, article_count, match_data, images_map, stats, articles, is_gossip=True)

        elif match_data["total_matches"] == 0:
            print("   今日无比赛，切换为公众号爆款数据模式\n")
            topics_and_raw = collect_real_gzh_topics(date_str, topic_history, preferred_types=target_types, season_weights=season_weights)
            if not topics_and_raw or not topics_and_raw[0]:
                result_msg = "无比赛且无真实爆款数据可用"
                print(f"ERROR: {result_msg}")
                send_wxpusher("足球自媒体 ⚠️", f"{date_str} 发文任务中止：{result_msg}")
                return

            topics, raw_articles = topics_and_raw
            extra_meta = {"type": "gzh_real_data"}

            _generate_articles_from_topics(topics, article_count, match_data, images_map, stats, articles, is_gossip=True)

        else:
            print("\n   获取公众号爆款趋势作为跨源参考...")
            gzh_raw = fetch_gzh_football_trends(date_str)
            gzh_context = gzh_raw[:8] if gzh_raw else []

            topics = select_topics(match_data, gzh_context, topic_history, preferred_types=target_types, season_weights=season_weights)
            extra_meta = {"type": "match_analysis"}

            _generate_articles_from_topics(topics, 3, match_data, images_map, stats, articles, gzh_articles=gzh_context)

        # ============================================================
        # Hupu Pipeline (articles 4-6, top 3 hottest posts)
        # ============================================================
        pre_downloaded = _run_hupu_pipeline(
            date_str, batch_mode, topic_history, match_data,
            articles, topics, images_map, stats, extra_meta)

        # ============================================================
        # Major Event Detection: generate emergency article if high-urgency event found
        # ============================================================
        if match_data["total_matches"] > 0:
            major_events = detect_major_events(match_data)
            urgent_events = [e for e in major_events if e["urgency"] >= 70]
            # Only trigger emergency in non-batch mode or morning batch (avoid duplicates)
            if urgent_events and batch_mode in ("auto", "morning"):
                top_event = urgent_events[0]
                e_idx = len(articles) + 1
                e_imgs = search_images({"title": top_event.get("title_hint", ""),
                                        "keywords_cn": [top_event.get("league", "足球")]}, count=3)
                images_map[len(articles)] = e_imgs
                e_art, e_err = generate_article_with_retry(
                    {"title": top_event.get("title_hint", ""),
                     "angle": top_event.get("detail", ""),
                     "content_type": "紧急球评",
                     "target_emotion": "震惊"},
                    match_data, e_idx, max_retries=1)
                stats["generated"] += 1
                if e_err:
                    print(f"   ⚠️  紧急球评生成失败: {e_err}")
                    stats["failed"] += 1
                else:
                    stats["valid"] += 1
                    articles.append((len(articles), e_art))
                    topics.append({"title": top_event.get("title_hint", ""),
                                   "content_type": f"紧急球评-{top_event.get('type', '')}"})
                    print(f"   🚨 紧急球评已生成: [{top_event['type']}] urgency={top_event['urgency']}")

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
                                     extra=extra_meta, pre_downloaded_images=pre_downloaded)

        # Save batch state for cross-batch dedup
        save_batch_state(date_str, batch_mode if batch_mode != "auto" else "full", result.get("articles", []))

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
        log.info(f"执行完成 — {stats['valid']}/{stats['generated']}篇成功, 耗时{elapsed}s")
        print_daily_summary(date_str, batch_mode)
        print(f"   输出: {result.get('output_dir', 'N/A')}")
        for a in result.get("articles", []):
            print(f"   - [{a.get('content_type', 'N/A')}] {a.get('title', 'N/A')[:50]} ({len(a.get('images', []))}张图)")
        success = True

    except Exception as e:
        result_msg = f"异常: {e}"
        print(f"ERROR: {e}")
        log.error(f"执行异常: {e}", exc_info=True)
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
