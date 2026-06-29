#!/usr/bin/env python3
"""足球自媒体 - 文章生成编排器 (独立版，无 Flask 依赖)

Usage: python orchestrator.py [YYYY-MM-DD]
"""

import os, json, sys, subprocess, requests, time, re, signal, yaml
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import defaultdict

from file_writer import FileWriter
from image_service import ImageService
from hupu_scraper import HupuScraper
from micro_headline import generate_micro_headlines
import micro_headline as mh
from constants import (PROJECT_ROOT, OUTPUT_DIR, GZH_SCRIPT,
                       DEEPSEEK_KEY, DASHSCOPE_KEY, UNSPLASH_KEY, FOOTBALL_DATA_KEY,
                       DEEPSEEK_URL, DASHSCOPE_URL, FOOTBALL_DATA_BASE,
                       WXPUSHER_APPTOKEN, WXPUSHER_UID,
                       COMPETITION_IDS, GZH_KEYWORD_GROUPS, GZH_TRANSFER_KEYWORDS,
                       GZH_NOISE_PATTERNS, WIKI_PLAYERS, WIKI_TEAMS, FOOTYRENDERS_PLAYERS,
                       BATCH_TYPES, BATCH_CONFIG, CONTENT_TYPE_TO_COLUMN,
                       FALLBACK_MAP, ALL_CONTENT_TYPES, WEEKLY_COLUMNS,
                       EVENING_COLUMN_POOL)
from utils import retry, call_llm, safe_json_loads, load_prompt_template
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
    Returns (weights_dict, label) tuple. Weight > 1.0 = preferred, < 1.0 = deprioritized."""
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    if not config_path.exists():
        return None, ""

    try:
        cfg = yaml.safe_load(config_path.read_text())
        season_weights = cfg.get("season_weights", [])
        if not season_weights:
            return None, ""

        dt = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now(ZoneInfo("Asia/Shanghai"))
        month = dt.month

        for period in season_weights:
            if month in period.get("months", []):
                weights = period.get("weights", {})
                label = period.get("label", "未知")
                print(f"   📅 赛季节奏: {label} (月份{month}, 权重: {weights})")
                return weights, label

        # Default: balanced
        return {"热点球评": 1.0, "转会资讯": 1.0, "排行榜": 1.0, "八卦趣事": 1.0, "战术解析": 1.0}, "常规赛季"
    except Exception as e:
        print(f"   ⚠️  加载赛季权重失败: {e}")
        return None, ""


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


def get_batch_columns(batch_mode):
    """Get column configs for a given batch from BATCH_CONFIG.

    Returns list of column dicts (one per article slot), each containing
    full column metadata: column_id, column_name, writing_style, word_count, etc.
    Returns None if batch_mode is not a valid batch name.
    """
    if batch_mode not in BATCH_CONFIG:
        return None
    return BATCH_CONFIG[batch_mode]



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
    existing["last_updated"] = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
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
        date_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
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

def get_column_for_date(date_str, content_type=None):
    """Get the weekly column for a given date.

    Returns (column_dict, is_match) where column_dict is the column config
    and is_match indicates whether the column suits the given content_type.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    weekday = dt.weekday()  # 0=Mon, 6=Sun
    column = WEEKLY_COLUMNS.get(weekday)
    if not column:
        return None, False
    if content_type:
        is_match = content_type in column.get("best_with", [])
        return column, is_match
    return column, True


def _assign_columns_to_topics(topics, batch_mode):
    """Assign each topic its corresponding column based on slot position.

    Each topic gets its column metadata (column_id, column_name, writing_style,
    style_detail, word_count, interaction_type, etc.) injected directly into
    the topic dict. This replaces the old single-column assignment — now ALL
    topics get their batch-specific column.

    When batch_mode is 'auto' or not in BATCH_CONFIG, this is a no-op.
    """
    if not topics or batch_mode not in BATCH_CONFIG:
        return

    batch_cfg = BATCH_CONFIG[batch_mode]
    slots = batch_cfg["slots"]

    for i, topic in enumerate(topics):
        if i >= len(slots):
            break
        slot = slots[i]
        topic["_column_id"] = slot["column_id"]
        topic["_column_name"] = slot["column_name"]
        topic["_column_icon"] = slot["icon"]
        topic["_writing_style"] = slot["writing_style"]
        topic["_style_detail"] = slot["style_detail"]
        topic["_word_count_range"] = slot["word_count"]
        topic["_interaction_type"] = slot["interaction_type"]
        topic["_interaction_guidance"] = slot["interaction_guidance"]
        topic["_topic_domain"] = slot["topic_domain"]
        topic["_topic_guidance"] = slot["topic_guidance"]
        topic["_data_source_hint"] = slot["data_source_hint"]
        topic["_batch_name"] = batch_cfg["name"]
        topic["_batch_time"] = batch_cfg["time"]
        topic["_reader_scenario"] = batch_cfg["reader_scenario"]
        topic["_overall_tone"] = batch_cfg["overall_tone"]
        # Map column to legacy content_type for metadata compatibility
        topic["content_type"] = CONTENT_TYPE_TO_COLUMN.get(slot["column_name"], topic.get("content_type", "八卦趣事"))

    column_names = [t.get("_column_name", "?") for t in topics[:len(slots)]]
    print(f"   📰 栏目分配: {', '.join(column_names)} ({batch_cfg['name']}·{batch_cfg['time']})")


def _check_intra_batch_dedup(topics):
    """Check that no two topics share core subjects (teams/players/keywords).

    Returns (clean_topics, warnings). If two topics share >40% of their
    keyword sets, the lower-scored one is flagged as potentially duplicate.
    """
    if len(topics) <= 1:
        return topics, []

    warnings = []
    for i in range(len(topics)):
        for j in range(i + 1, len(topics)):
            ki = set(k.lower() for k in (topics[i].get("keywords", []) or []))
            kj = set(k.lower() for k in (topics[j].get("keywords", []) or []))
            if not ki or not kj:
                continue
            overlap = ki & kj
            if len(overlap) == 0:
                continue
            overlap_ratio = len(overlap) / min(len(ki), len(kj))
            if overlap_ratio > 0.4:
                # Also check Chinese keyword overlap
                kci = set(k for k in (topics[i].get("keywords_cn", []) or []))
                kcj = set(k for k in (topics[j].get("keywords_cn", []) or []))
                cn_overlap = kci & kcj
                ti = topics[i].get("title", "")[:30]
                tj = topics[j].get("title", "")[:30]
                msg = (f"⚠️ 批内重复: #{i+1}「{ti}」与 #{j+1}「{tj}」"
                       f" 共享关键词 {overlap}{' + CN:' + str(cn_overlap) if cn_overlap else ''}")
                warnings.append(msg)

    if warnings:
        for w in warnings:
            print(f"   {w}")
    return topics, warnings


def select_topics(match_data, gzh_articles=None, topic_history=None, preferred_types=None, season_weights=None, cross_batch_covered=None, season_label="", topic_count=3):
    print(f"\n[2/5] LLM 话题筛选 (DeepSeek, target={topic_count}篇)...")
    lines = []
    for league, matches in sorted(match_data.get("fixtures_by_league", {}).items()):
        lines.append(f"\n## {league}")
        for m in matches:
            hg, ag = m.get("home_score"), m.get("away_score")
            # Convert UTC match time to Beijing time for the prompt
            utc_date = m.get("utc_date", "")
            cst_time = ""
            if utc_date:
                try:
                    from datetime import datetime, timezone, timedelta
                    dt_utc = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
                    dt_cst = dt_utc + timedelta(hours=8)
                    cst_time = dt_cst.strftime("(%m-%d %H:%M 开球)")
                except Exception:
                    pass
            lines.append(f"  {m['home_team']} {hg}-{ag if hg is not None else 'vs'} {m['away_team']} {cst_time}")

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

    # Cross-batch dedup: tell LLM what today's earlier batches already published
    cross_batch_text = ""
    if cross_batch_covered and (cross_batch_covered.get("titles") or cross_batch_covered.get("keywords")):
        cross_batch_text = "\n## 🚫 今日已发布（严禁任何重复或变体）\n"
        if cross_batch_covered.get("titles"):
            today_titles = list(cross_batch_covered["titles"])[:5]
            cross_batch_text += "今日已发标题: " + " | ".join(today_titles) + "\n"
        if cross_batch_covered.get("keywords"):
            today_kw = list(cross_batch_covered["keywords"])[:15]
            cross_batch_text += "今日覆盖关键词: " + ", ".join(today_kw) + "\n"
        cross_batch_text += "禁止选择与上述标题或关键词重叠的新选题。\n"

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

    # World Cup priority: during World Cup month, strongly push tournament content
    world_cup_priority = ""
    if season_label == "世界杯月":
        world_cup_matches = sum(1 for m in match_data.get("all_fixtures", [])
                                if m.get("league") == "FIFA World Cup")
        if world_cup_matches > 0:
            world_cup_priority = f"""
🌍 世界杯优先模式 — 今天是2026世界杯比赛日（{world_cup_matches}场比赛）：
⚠️ 铁律：必须围绕世界杯比赛选材！这是读者最关心的事。
- 第1篇(热点球评)：必须从今日世界杯比赛中选一场最有话题性的。分析场上表现、战术、关键球员。
- 第2篇(转会资讯/八卦趣事)：优先选择世界杯球员的转会动态、场外花边、世界杯相关的争议冲突。
- 第3篇(排行榜/战术解析/八卦趣事)：优先从世界杯比赛中提炼数据榜单或战术趋势。
- 只有当今日无世界杯比赛或世界杯比赛确实无话题性时，才允许选择其他赛事/非赛事话题。
"""

    prompt = f"""你是头条号足球博主"球评人老六"。以下是 {match_data['date']} 的真实比赛结果。请筛选 {topic_count} 个有爆款潜力的话题。

比赛数据（只显示已结束的比赛FT/AET/PEN，进行中的比赛显示为"vs"）：
{"".join(lines)}

⚠️ 注意：只显示了已结束的比赛(FT/AET/PEN)。进行中的比赛显示为"vs"，不要选作选题。

{gzh_text}
{history_text}
{cross_batch_text}
{weight_hint}
{world_cup_priority}
硬性要求 — {topic_count} 个话题必须覆盖不同内容类型 + 不同核心主题：
1. 第1篇：热点球评 — 从当日比赛中选最有话题性的一场
2. 第2篇：转会资讯/八卦趣事 — 转会传闻、球员花边、冲突争议、场外话题
3. 第3篇及以后：排行榜/战术解析/八卦趣事 — 数据榜单或战术趋势

⚠️ 绝对禁止编造时间：每场比赛括号里的时间是真实开球时间(北京时间)，写文章时只能用这个时间，不许编造"凌晨X点""深夜X点"等虚构场景。如果比赛是上午9点开的就写"上午"或"早场"，不确定时间的就说"这场比赛"。

⚠️ 去重铁律：
- 禁止2个话题围绕同一核心事件/同一核心球员/同一转会故事展开（即便内容类型不同也不行）
- 举例：如果第1篇写"姆巴佩离队后巴黎夺冠"，第3篇就 不能 再写"姆巴佩的欧冠诅咒"
- {topic_count}个话题的核心关键词集合交集必须为空（如都含Mbappe/PSG/Champions League即违规）
- 如果当日素材不够{topic_count}个完全不同的主题，宁可减少话题数也不要凑近似话题

如果当日有绝杀、逆转、红牌、VAR争议、教练冲突等事件，优先选择。

风格要求：像老球迷喝酒聊天一样自然，有明确立场和情绪，不骑墙、不套模板。
避免：任何过去7天已报道过的球队/球员/话题。

输出纯JSON数组：
[{{"title": "标题(15-25字)", "angle": "切入角度+明确态度", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "content_type": "热点球评/转会资讯/排行榜/八卦趣事/战术解析", "score": 90, "controversy_level": "high/medium/low", "target_emotion": "愤怒/骄傲/怀旧/震惊/感动/好奇", "why_pick": "为什么选这个角度(20字)"}}]
只输出JSON。"""

    topic_selector_prompt = load_prompt_template("topic_selector.txt")
    if not topic_selector_prompt:
        topic_selector_prompt = "你是头条号足球博主'球评人老六'，有态度、有人味、不骑墙。严格按要求分配内容类型，避开历史话题。只输出JSON。"

    messages = [
        {"role": "system", "content": topic_selector_prompt},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-flash", messages, temperature=0.7, max_tokens=4096)
    topics = safe_json_loads(response)
    if topics and isinstance(topics, dict) and "title" in topics:
        topics = [topics]  # LLM returned single object instead of array
    if not isinstance(topics, list):
        topics = []
    topics, dup_warnings = _check_intra_batch_dedup(topics)
    # Drop topics with >60% keyword overlap (keep higher-scored one)
    if dup_warnings:
        to_drop = set()
        for i in range(len(topics)):
            for j in range(i + 1, len(topics)):
                ki = set(k.lower() for k in (topics[i].get("keywords", []) or []))
                kj = set(k.lower() for k in (topics[j].get("keywords", []) or []))
                if not ki or not kj:
                    continue
                overlap_ratio = len(ki & kj) / min(len(ki), len(kj))
                if overlap_ratio >= 0.5:
                    # Drop the lower-scored one
                    drop = i if topics[i].get("score", 0) < topics[j].get("score", 0) else j
                    to_drop.add(drop)
        if to_drop:
            topics = [t for idx, t in enumerate(topics) if idx not in to_drop]
            print(f"   🗑️ 自动去重: 移除 {len(to_drop)} 个重复话题，保留 {len(topics)} 个")

    # Cross-batch keyword overlap check
    if cross_batch_covered and topics:
        cross_kw = cross_batch_covered.get("keywords", set())
        cross_titles = cross_batch_covered.get("titles", set())
        filtered = []
        for t in topics:
            t_title = t.get("title", "")[:30]
            t_kws = set(k.lower() for k in (t.get("keywords", []) or []) + (t.get("keywords_cn", []) or []))
            title_overlap = t_title in cross_titles
            kw_overlap = len(t_kws & cross_kw) / max(len(t_kws), 1) if t_kws else 0
            if title_overlap or kw_overlap >= 0.4:
                print(f"   🗑️ 跨批次去重: 丢弃「{t_title}」(关键词重叠率 {kw_overlap:.0%})")
            else:
                filtered.append(t)
        if len(filtered) < len(topics):
            print(f"   跨批次去重: {len(topics)} → {len(filtered)} 个话题")
        topics = filtered

    print(f"   筛选出 {len(topics)} 个话题:")
    for i, t in enumerate(topics):
        print(f"   {i+1}. [{t.get('content_type', 'N/A')}] {t['title'][:50]}")

    # Check topic material sufficiency — reject topics that match_data can't support
    topics = _check_topic_material_sufficiency(topics, match_data)

    return topics


def _check_topic_material_sufficiency(topics, match_data):
    """Filter out topics that match_data cannot support with enough facts.

    match_data only contains: team names, scores, league name, status, utc_date.
    If a topic requires details beyond these (e.g., goalscorer names, possession stats),
    it will inevitably lead to hallucination.

    Strategy: extract team names from topic title/keywords, check if those teams
    appear in match_data with a FINISHED score. If a topic references teams not
    in match_data, or references match_data teams but the topic angle requires
    details beyond basic scores, mark it for review.

    Returns filtered list of topics.
    """
    if not topics:
        return topics

    # Collect all teams in today's match_data that have FINISHED scores
    finished_teams = set()
    finished_matches = {}  # (home, away) -> fixture dict
    all_fixtures = match_data.get("all_fixtures", [])
    for m in all_fixtures:
        status = m.get("status", "")
        if status in ("FT", "AET", "PEN"):
            home = m.get("home_team", "").lower()
            away = m.get("away_team", "").lower()
            if home:
                finished_teams.add(home)
            if away:
                finished_teams.add(away)
            finished_matches[(m.get("home_team", "").lower(), m.get("away_team", "").lower())] = m
            finished_matches[(away, home)] = m  # reverse lookup

    # Collect all known team names (CN + EN) from constants
    from constants import WIKI_TEAMS
    all_known_teams = set()
    for team in WIKI_TEAMS:
        all_known_teams.add(team.lower())
        # Also add common English names
        eng_names = {
            "阿森纳": "arsenal", "曼城": "manchester city", "利物浦": "liverpool",
            "曼联": "manchester united", "切尔西": "chelsea", "热刺": "tottenham",
            "巴萨": "barcelona", "皇马": "real madrid", "马竞": "atletico madrid",
            "拜仁": "bayern munich", "多特": "borussia dortmund", "国米": "inter milan",
            "AC米兰": "ac milan", "尤文": "juventus", "巴黎": "psg",
        }
        if team in eng_names:
            all_known_teams.add(eng_names[team])

    filtered = []
    dropped = []
    for t in topics:
        title = t.get("title", "")
        angle = t.get("angle", "")
        text = (title + " " + angle).lower()

        # Check: does this topic reference teams we have finished data for?
        has_finished_team = any(team in text for team in finished_teams)
        has_known_team = any(team in text for team in all_known_teams)

        if has_finished_team:
            # Good — this topic has finished match data to support it
            filtered.append(t)
        elif has_known_team:
            # Has a known team but no finished match data for it
            # This is risky — the LLM will have to hallucinate match details
            # Check if the topic is about transfer/gossip (no match data needed)
            non_match_types = ["转会资讯", "八卦趣事"]
            ct = t.get("content_type", "")
            if ct in non_match_types:
                # OK — transfer/gossip doesn't need match data
                filtered.append(t)
            else:
                # Match analysis topic without match data → drop
                dropped.append(t)
                print(f"   🗑️ 素材不足: 丢弃「{title[:40]}」— 素材中无该球队已结束比赛数据")
        else:
            # No known teams referenced — could be a general topic
            # Check if it mentions specific match details (scores, goalscorers, etc.)
            has_match_details = any(kw in text for kw in ["点球", "绝杀", "帽子戏法", "进球", "射门", "控球", "红牌", "黄牌"])
            if has_match_details:
                # Topic mentions match details but no teams in data → likely hallucination
                dropped.append(t)
                print(f"   🗑️ 素材不足: 丢弃「{title[:40]}」— 提及比赛细节但无对应数据")
            else:
                # General topic without match details — can keep
                filtered.append(t)

    if dropped:
        print(f"   📉 素材充足性检查: {len(topics)} → {len(filtered)} 个话题 (丢弃 {len(dropped)} 个)")
    else:
        print(f"   ✅ 素材充足性检查: 全部 {len(topics)} 个话题素材充足")

    return filtered


def select_evening_columns(gzh_articles, match_data, season_label=""):
    """从 EVENING_COLUMN_POOL 中根据当日热点选择 2 个最适合的晚间栏目。

    Uses LLM to score each column against today's GZH trending topics and
    match context, then returns the top 2. Falls back to random selection
    if LLM call fails.
    """
    if not gzh_articles:
        import random
        selected = random.sample(EVENING_COLUMN_POOL, 2)
        print(f"   🌙 无GZH数据，随机选择晚间栏目: {selected[0]['column_name']} + {selected[1]['column_name']}")
        return selected

    # Build trending summary
    trending_lines = []
    for a in gzh_articles[:15]:
        title = a.get("title", "")[:80]
        reads = a.get("clicksCount", "?")
        trending_lines.append(f"- [{reads}阅读] {title}")
    trending_text = "\n".join(trending_lines)

    # Build match summary
    match_lines = []
    if match_data and match_data.get("all_fixtures"):
        for m in match_data["all_fixtures"][:12]:
            hg = m.get("home_score")
            ag = m.get("away_score")
            score = f"{hg}-{ag}" if hg is not None else "vs"
            match_lines.append(f"- {m['home_team']} {score} {m['away_team']} ({m.get('league', '?')})")
    match_text = "\n".join(match_lines) if match_lines else "（今日无比赛数据）"

    world_cup_hint = ""
    if season_label == "世界杯月":
        wc_count = sum(1 for m in (match_data.get("all_fixtures") or [])
                      if m.get("league") == "FIFA World Cup")
        if wc_count > 0:
            world_cup_hint = f"\n🌍 世界杯期间（今日{wc_count}场世界杯比赛），优先选择与世界杯直接相关的栏目。\n"

    # Build column options
    columns_desc = ""
    for i, col in enumerate(EVENING_COLUMN_POOL):
        columns_desc += (
            f"\n{i}. {col['icon']} **{col['column_name']}** — {col['topic_domain']}\n"
            f"   适合场景: {col['topic_guidance'][:120]}\n"
        )

    prompt = f"""你是头条号足球博主"球评人老六"。今晚需要从以下5个栏目中选出2个最适合今天发布的栏目。

{world_cup_hint}
今日公众号足球热点：
{trending_text[:3000]}

今日比赛：
{match_text[:1000]}

可选栏目：
{columns_desc}

选择标准（按优先级）：
1. 今天有充足素材的栏目优先（热点话题多、讨论度高）
2. 两个栏目内容要有明显差异化（不要两个都讲类似的领域）
3. 世界杯期间优先选与世界杯比赛、球员、转会直接相关的栏目
4. 避免两个栏目都是"分析型"或都是"八卦型"，最好一硬一软搭配

输出纯JSON：
{{"selected": [0, 3], "reason": "简要说明为什么选这两个栏目(40字内)"}}

selected 是栏目序号(0-4)，必须选恰好2个。只输出JSON。"""

    try:
        messages = [
            {"role": "system", "content": "你是头条号足球博主'球评人老六'。根据当天热点选择最合适的晚间栏目。只输出JSON。"},
            {"role": "user", "content": prompt}
        ]
        response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-flash", messages, temperature=0.5, max_tokens=512)
        result = safe_json_loads(response)

        if result and isinstance(result, dict) and "selected" in result:
            indices = result["selected"][:2]
            selected = []
            for i in indices:
                if isinstance(i, int) and 0 <= i < len(EVENING_COLUMN_POOL):
                    selected.append(dict(EVENING_COLUMN_POOL[i]))  # copy to avoid mutating pool
            if len(selected) >= 2:
                print(f"   🌙 晚间栏目(LLM选择): {selected[0]['column_name']} + {selected[1]['column_name']}")
                print(f"      理由: {result.get('reason', 'N/A')[:100]}")
                return selected
    except Exception as e:
        print(f"   ⚠️  晚间栏目LLM选择失败: {e}")

    # Fallback: weighted random — prefer columns matching trending keywords
    import random
    trending_all = " ".join(a.get("title", "") for a in gzh_articles[:20])
    column_scores = []
    for col in EVENING_COLUMN_POOL:
        score = 1.0
        domain = col.get("topic_domain", "")
        # Boost if domain-related keywords appear in trending
        boost_keywords = {
            "世界杯": ["世界杯", "World Cup", "小组赛", "淘汰赛", "出线"],
            "辣评": ["争议", "冲突", "红牌", "绝杀", "逆转", "下课"],
            "裁判": ["VAR", "裁判", "点球", "红牌", "黄牌", "判罚"],
            "转会": ["转会", "签约", "续约", "离队", "身价", "绯闻", "官宣"],
            "球迷": ["球迷", "看台", "花边", "场外", "女友", "趣事"],
        }
        for key, kws in boost_keywords.items():
            if key in domain or key in col.get("column_name", ""):
                matches = sum(1 for kw in kws if kw.lower() in trending_all.lower())
                score += matches * 0.4
        column_scores.append((col, score))

    column_scores.sort(key=lambda x: -x[1])
    # Weighted random: pick from top 3, avoid always same pair
    top3 = column_scores[:3]
    weights = [cs[1] for cs in top3]
    total_w = sum(weights)
    if total_w > 0:
        picked = random.choices(top3, weights=weights, k=2)
        # If same column picked twice, replace second with next best
        if picked[0][0]["column_id"] == picked[1][0]["column_id"]:
            for cs in column_scores:
                if cs[0]["column_id"] not in [p[0]["column_id"] for p in picked]:
                    picked[1] = cs
                    break
        selected = [dict(p[0]) for p in picked[:2]]

    print(f"   🌙 晚间栏目(加权随机): {selected[0]['column_name']} + {selected[1]['column_name']}")
    return selected


def collect_real_gzh_topics(date_str, topic_history=None, topic_preference="auto", preferred_types=None, season_weights=None, cross_batch_covered=None, column_type_hint=None, season_label="", match_data=None):
    raw_articles = fetch_gzh_football_trends(
        date_str,
        keyword_groups=GZH_TRANSFER_KEYWORDS if topic_preference == "transfer" else None,
        fallback_match_data=match_data,
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

    # Cross-batch dedup: tell LLM what today's earlier batches already published
    cross_batch_text = ""
    if cross_batch_covered and (cross_batch_covered.get("titles") or cross_batch_covered.get("keywords")):
        cross_batch_text = "\n🚫 今日已发布（严禁任何重复或变体）:\n"
        if cross_batch_covered.get("titles"):
            today_titles = list(cross_batch_covered["titles"])[:5]
            cross_batch_text += "今日已发标题: " + " | ".join(today_titles) + "\n"
        if cross_batch_covered.get("keywords"):
            today_kw = list(cross_batch_covered["keywords"])[:15]
            cross_batch_text += "今日覆盖关键词: " + ", ".join(today_kw) + "\n"
        cross_batch_text += "禁止选择与上述标题或关键词重叠的新选题。\n"

    # Build prompt based on preference
    if column_type_hint:
        # Column-driven topic selection (e.g., evening batch with dynamic column pool)
        n = len(column_type_hint)
        domain_requirements = []
        for i, hint in enumerate(column_type_hint):
            domain_requirements.append(f"第{i+1}篇：{hint} — 从GZH爆款库中选择适合{hint}领域的话题")
        type_requirement = f"{n}个选题分别对应以下栏目领域：\n" + "\n".join(domain_requirements)
        system_msg = "你是头条号足球博主'球评人老六'，有态度有人味。绝不洗稿，跨源合成+新观点=全新原创。严格按栏目领域选择对应话题。只输出JSON。"
    elif preferred_types:
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

    # World Cup priority for GZH-based topic selection
    world_cup_hint = ""
    if season_label == "世界杯月":
        world_cup_hint = """
🌍 世界杯优先模式 — 2026世界杯正在进行中：
⚠️ 选题铁律：优先从世界杯相关话题中选材！
- 场上：世界杯比赛结果、球员表现、战术分析、冷门黑马
- 场下：世界杯球员转会传闻、场外花边、球迷故事
- 趋势：小组出线形势、金靴竞争、历史对比
- 只有当GZH数据中确实无世界杯相关话题时，才允许选择其他足球话题。
"""

    print(f"\n[2/5] 基于真实爆款数据筛选选题 (DeepSeek, mode={topic_preference})...")
    prompt = f"""你是头条号足球博主"球评人老六"。以下是公众号平台最近2天真实爆款足球文章数据，请从中选出3个最有二次创作价值的选题。

{weight_hint}
{world_cup_hint}
真实爆款文章数据（已按时效性+热度排序，pub_time为发布日期）：
{json.dumps(articles_text, ensure_ascii=False)}
{history_text}
{cross_batch_text}

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

    # Cross-batch keyword overlap check: drop topics that overlap with today's earlier batches
    if cross_batch_covered and topics:
        cross_kw = cross_batch_covered.get("keywords", set())
        cross_titles = cross_batch_covered.get("titles", set())
        filtered = []
        for t in topics:
            t_title = t.get("title", "")[:30]
            t_kws = set(k.lower() for k in (t.get("keywords", []) or []) + (t.get("keywords_cn", []) or []))
            title_overlap = t_title in cross_titles
            kw_overlap = len(t_kws & cross_kw) / max(len(t_kws), 1) if t_kws else 0
            if title_overlap or kw_overlap >= 0.4:
                print(f"   🗑️ 跨批次去重: 丢弃「{t_title}」(关键词重叠率 {kw_overlap:.0%})")
            else:
                filtered.append(t)
        if len(filtered) < len(topics):
            print(f"   跨批次去重: {len(topics)} → {len(filtered)} 个选题")
        topics = filtered

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



def _build_data_confidence_block(match_context):
    """Build a prompt block about data reliability for matches.

    Scans fixture data for data_confidence fields set by Wikipedia cross-validation.
    Returns a string that tells the LLM which match scores are reliable and which aren't.
    Returns empty string if no match_context or no confidence issues.
    """
    if not match_context:
        return ""

    all_fixtures = match_context.get("all_fixtures", [])
    if not all_fixtures:
        return ""

    conflicts = []
    mediums = []
    for f in all_fixtures:
        conf = f.get("data_confidence", "")
        home = f.get("home_team", "")
        away = f.get("away_team", "")
        if conf == "conflict":
            conflicts.append(f"{home} vs {away}")
        elif conf == "medium":
            mediums.append(f"{home} vs {away}")

    blocks = []
    if conflicts:
        conflicts_str = "、".join(conflicts[:5])
        blocks.append(f"""⚠️ ⚠️ ⚠️ 数据可信度警告（必读）：
以下比赛的数据来源存在比分冲突：{conflicts_str}
这些比赛的比分通过Wikipedia交叉验证后发现与API数据不符。
🔴 严禁在文章中使用这些比赛的具体比分。如果必须提及这些比赛，只能写「XX队与XX队进行了比赛」这样的笼统描述，不能说「X-X战胜/击败」。
🔴 严禁将API中的比分当作事实写入文章——这些比分已被证明不准确。""")

    if mediums and not conflicts:
        blocks.append("""📊 数据来源说明：部分比赛比分未经第三方验证，使用时建议避免过度强调具体比分数字的精确性。""")

    if blocks:
        return "\n".join(blocks) + "\n\n"
    return ""


def generate_article(topic, match_context, index, gzh_articles=None, temperature=0.5, retry_hint=""):
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

    # Style guidance by content type (5 categories) — fallback for non-column mode
    style_guide = {
        "热点球评": "像赛后和球友喝酒复盘——先讲最刺激的瞬间，再拆关键战术细节，最后给个不带套路的结论。用「说白了」「仔细想想」这类自然口语推进，不要「老六分析」标签。",
        "转会资讯": "像球迷群里的八卦——重点是「为什么」和「影响」。有趣味但不编造，有逻辑但不学术。不确定的地方就说「据说」「按这个趋势」，不要假装什么都知道。",
        "排行榜": "数字是药引子，对比是主菜。每个上榜人物都要有槽点或亮点，每个关键数字后面必须跟一句「这意味着...」。让读者感觉在翻一本有态度的排名，不是在看Excel。",
        "八卦趣事": "聚焦一个侧面、一个瞬间、一个画面。用细节和情绪让读者有代入感。可以调侃但不能刻薄。节奏轻快，不要写成流水账履历。",
        "战术解析": "你的任务是翻译——把专业术语翻译成让普通球迷听了能跟朋友吹牛的大白话。数据必须配人话解读。每篇至少1处历史/行业跨界类比。",
        "比赛复盘型": "像赛后和球友喝酒复盘——先讲最刺激的瞬间，再拆关键战术细节，最后给个不带套路的结论。",
        "转会八卦型": "像球迷群里的八卦——重点是「为什么」和「影响」。有趣不编造，有逻辑不学术。",
        "争议观点型": "像一个敢说真话的老球迷——开篇就亮态度，不怕得罪人，但每条观点都有事实支撑。",
        "人物故事型": "聚焦一个侧面、一个瞬间、一个画面。用细节和情绪让读者有代入感。不写流水账。",
        "趋势解读型": "从现象中提炼规律，用一两组关键数据说话，每个数字配人话解读。让读者看完有「原来如此」的感觉。",
    }

    # Column-driven style (v2): use column metadata when available
    column_name = topic.get("_column_name", "")
    if column_name:
        style = topic.get("_style_detail", "用自然口语化中文写作，有态度有人味。")
        word_min, word_max = topic.get("_word_count_range", [500, 800])
        column_block = f"""
📰 今日专栏：{column_name}
专栏领域：{topic.get('_topic_domain', '')}
选题指引：{topic.get('_topic_guidance', '')}
读者场景：{topic.get('_reader_scenario', '')}
写作体例：{topic.get('_writing_style', '')}
体例说明：{style}

结构要求：按照{column_name}栏目的固定结构写作。
字数要求：{word_min}-{word_max}字。
互动类型：{topic.get('_interaction_type', '')}
互动指引：{topic.get('_interaction_guidance', '')}
"""
        column_meta = {"column_id": topic.get("_column_id", ""),
                       "column_name": column_name,
                       "batch_name": topic.get("_batch_name", ""),
                       "batch_time": topic.get("_batch_time", "")}
        word_count_rule = f"正文 {word_min}-{word_max} 字（严格控制，{column_name}栏目规范）"
    else:
        style = style_guide.get(content_type, "口语化+专业深度，短句为主，有明确立场。像朋友聊天一样自然。")
        word_min, word_max = 500, 800
        column_block = ""
        column_meta = {}
        word_count_rule = "正文 500-800 字（紧凑有力，有多少事实写多少字，不要水字数）"

    # Retry hint: inject failure feedback to force improvement
    retry_block = ""
    if retry_hint:
        retry_block = f"""
⚠️ 上次生成失败！问题：{retry_hint}
这次必须修正上述所有问题。正文至少{word_min}字，至少2个##小标题，文末至少2个配图标记。"""

    # Data confidence block: warn LLM about unreliable match scores
    confidence_block = _build_data_confidence_block(match_context)

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。今天的任务是基于真实数据写一篇有观点的足球文章。
{column_block}
今日话题：{topic['title']}
切入角度：{topic['angle']}
内容类型：{content_type}
目标情绪：{topic.get('target_emotion', '好奇')}

⚠️ 数据局限性声明（必读，违反即作废）：
今天的 match_data 只包含以下字段：
- 比赛日期(date)
- 联赛名(league)
- 主队名(home_team) / 客队名(away_team)
- 最终比分(home_score / away_score) — 仅当比赛结束时才有值
- 比赛状态(status): FT=已结束, AET=加时赛结束, PEN=点球大战结束, IN_PLAY=进行中, HT=半场, PRE=未开始
- 开球时间(utc_date)

❌ match_data 不包含以下数据（严禁编造，违者作废）：
- 射门数、射正数、控球率、传球成功率、角球数、犯规数等任何技术统计
- 进球球员、进球时间、助攻球员、红黄牌
- 球员上场/没上场信息、球员个人表现
- 任何不在上述字段中的细节

✅ 你可以写的内容：
- 素材中明确给出的比分、球队名、联赛名、比赛状态
- 你的观点、分析、类比、预测（必须标注为推测，如"从战术逻辑上看"）

🚫 如果素材中没有某个数据，不要写。宁可写得平淡，不能胡编。

{confidence_block}你的素材（只能使用以下数据中的事实）：
{context_str[:3000]}
{gzh_text}
{retry_block}

写作规则：
1. **事实来自素材**：数据、比分、排名、球队名称必须来自上面的数据。素材里没有的不要写。
2. **观点来自你**：在事实基础上分析、质疑、对比、预测。区分"数据说X"和"我觉得Y"。
3. **每个关键数字配人话翻译**：不能裸奔数据。71球→"每3个球就有1个是定位球砸进去的"。69球→"差2球，差了17分"。
4. **开篇必须反套路**：禁止"昨晚XX队以X-X战胜XX队""近日XX传闻引发热议"。用一个具体场景、一句狠话、一个数据反差、或者坦诚表态作为开头。
5. **跨界视角**：至少1处历史类比/行业类比/生活类比。
6. **评论区引擎**：正文中预埋≥2个评论触发点（留白挑衅/回忆召唤/身份站队/梗的钩子），文末互动钩子必须具体、低门槛、自己先答。
7. **⚠️ 绝对禁止编造时间/场景**：比赛开球时间已在素材中标注，只能写"上午""下午""晚场"等笼统描述，或直接用"这场比赛"。禁止写"凌晨X点""深夜X点""半夜爬起来看"等虚构的时间场景。素材里没有的细节一律不写。

写作要求：
{style}

结构：反套路开篇 → 2个小节展开分析 → 收尾观点+互动

硬性规范：
- {word_count_rule}
- 必须包含 ≥2 个 ## 二级标题
- 文末必须包含2张配图标记：![配图1](images/article-{index}-img-001.jpg) 等
- 事实红线：素材里没有的数据/事件/引语，一律不写。有几分数据说几分话

禁用词：震惊、吓尿、哭惨、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说
禁用模式：不要每段以"老六"开头，不要列一二三四，不要"一部分球迷认为...另一部分球迷认为..."

输出JSON:
{{"title": "优选标题(15-25字)", "backup_title": "备选标题(不同角度，15-25字)", "content": "Markdown正文({word_min}-{word_max}字，含≥2个##小标题，文末含2个配图标记)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "站队式/投票式/预测式/共鸣式/挑战式/调侃式", "interaction_bait": "互动问题", "content_type": "{content_type}"}}
只输出JSON。"""

    base_prompt = load_prompt_template("article_generator.txt")
    if not base_prompt:
        base_prompt = f"你是头条号足球博主'球评人老六'，10万粉丝。核心原则：事实来自素材，观点来自你。素材里没有的绝不编造。风格：{style} 用自然口语化中文写作，有态度有人味。"

    messages = [
        {"role": "system", "content": f"{base_prompt}\n\n本次写作风格要求：{style}"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-pro", messages, temperature=temperature, max_tokens=8192)
    article = safe_json_loads(response)
    # Inject column metadata if present (flatten into article)
    if column_meta:
        article["_column_id"] = column_meta.get("column_id", "")
        article["_column_name"] = column_meta.get("column_name", "")
        article["_batch_name"] = column_meta.get("batch_name", "")
        article["_batch_time"] = column_meta.get("batch_time", "")
    print(f"   标题: {article.get('title','?')}, 正文: {len(article.get('content',''))}字")
    return article


def generate_gossip_article(topic, index, temperature=0.5, retry_hint=""):
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

    # Style guidance by content type — fallback for non-column mode
    style_guide = {
        "热点球评": "像赛后和球友喝酒复盘——先讲最刺激的瞬间，再拆关键战术细节，最后给个不带套路的结论。",
        "转会资讯": "像球迷群里的八卦——重点是「为什么」和「影响」。有趣不编造，有逻辑不学术。不确定就说「据说」。",
        "排行榜": "数字是药引子，对比是主菜。每个关键数字后面跟一句「这意味着...」。让读者感觉在看有态度的排名，不是Excel。",
        "八卦趣事": "聚焦一个侧面、一个瞬间、一个画面。用细节和情绪让读者有代入感。可以调侃但不刻薄。",
        "战术解析": "把专业术语翻译成让普通球迷能跟朋友吹牛的大白话。数据配人话解读，至少1处跨界类比。",
        "转会八卦型": "像球迷群里的八卦——重点是「为什么」和「影响」。有趣不编造，有逻辑不学术。",
        "争议观点型": "开篇就亮态度，不怕得罪人，但每条观点都有事实支撑。",
        "人物故事型": "聚焦一个侧面、一个瞬间、一个画面。用细节和情绪让读者有代入感。",
        "趋势解读型": "从现象中提炼规律，每个数字配人话解读。让读者看完有「原来如此」的感觉。",
    }

    # Column-driven style (v2): use column metadata when available
    column_name = topic.get("_column_name", "")
    if column_name:
        style = topic.get("_style_detail", "用自然口语化中文写作，有态度有人味。")
        word_min, word_max = topic.get("_word_count_range", [500, 800])
        column_block = f"""
📰 今日专栏：{column_name}
专栏领域：{topic.get('_topic_domain', '')}
选题指引：{topic.get('_topic_guidance', '')}
读者场景：{topic.get('_reader_scenario', '')}
写作体例：{topic.get('_writing_style', '')}
体例说明：{style}

结构要求：按照{column_name}栏目的固定结构写作。
字数要求：{word_min}-{word_max}字。
互动类型：{topic.get('_interaction_type', '')}
互动指引：{topic.get('_interaction_guidance', '')}
"""
        word_count_rule = f"正文 {word_min}-{word_max} 字（严格控制，{column_name}栏目规范）"
        column_meta = {"column_id": topic.get("_column_id", ""),
                       "column_name": column_name,
                       "batch_name": topic.get("_batch_name", ""),
                       "batch_time": topic.get("_batch_time", "")}
    else:
        style = style_guide.get(content_type, "口语化+专业深度，短句为主，有明确立场。像朋友聊天一样自然。")
        word_min, word_max = 500, 800
        column_block = ""
        column_meta = {}
        word_count_rule = "正文 500-800 字（紧凑有力，不要水字数）"

    # Retry hint: inject failure feedback
    retry_block = ""
    if retry_hint:
        retry_block = f"""
⚠️ 上次生成失败！问题：{retry_hint}
这次必须修正上述所有问题。正文至少{word_min}字，至少2个##小标题，文末至少2个配图标记。"""

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。今天的任务是基于真实热点文章转写改编。
{column_block}
你的素材 — 公众号平台真实爆款文章：
{sources_text}

同期其他热点（了解语境）：
{bg_text}

⚠️ 数据局限性声明（必读）：
match_data 只包含比赛的基本信息（球队名、比分、联赛名、状态）。
❌ 不包含射门数、射正数、控球率、传球数、进球球员、进球时间等任何技术统计。
✅ 你可以写的内容：素材中明确给出的比分、球队名、联赛名，以及你的观点和分析。
🚫 如果素材中没有某个数据，不要写。宁可写得平淡，不能胡编。

内容类型：{content_type}
切入角度：{topic.get('angle', '独特角度')}
{retry_block}

转写改编规则：
1. **事实继承**：源文章写了什么事件、数据，你才能写。没提到的不要编。
2. **角度变换**：用不同切入角度和叙事顺序重组——比如源文章写"A转会B队"，你从B队战术需求、A的生涯选择、转会费是否合理切入。
3. **语言100%重写**：用自己的话、自己的节奏。绝不可照搬源文章完整句子。
4. **观点升级**：在事实基础上加你的分析和态度。推测就说是推测，不要包装成事实。
5. **每个关键数字配人话翻译**：不能裸奔数据。转会费→"比市场价溢价了30%，这是恐慌性引援"。阅读量→"说明球迷对这事有多饥渴"。
6. **开篇必须反套路**：禁止"近日XX引发热议""据XX报道"。用一个场景、一个反问、一个数据反差开头。
7. **至少1处跨界类比**：行业类比、历史类比、生活类比。

⚠️ ⚠️ ⚠️ 来源标注铁律（违反即违规）：
所有非比赛数据类的信息，必须标注可靠程度：

🔴 球员具体表现（"独造三球""帽子戏法"等）：
- 如果来自公众号报道，必须写"据XX报道""据文章称"
- 禁止直接写"姆巴佩独造三球"这种确信断言
- 正确示例："据媒体报道，姆巴佩本场比赛表现极为出色"

🔴 转会传闻、合同金额、球员纠纷：
- 必须标注来源："据XX记者报道""据外媒透露""传闻称"
- 禁止写成确认事实："据ESPN报道，姆巴佩转会费或达1.8亿"

🔴 比分和比赛结果（非官方数据源）：
- 必须写"据媒体报道""据公众号文章引用"
- 不能直接写"法国0-1塞内加尔"这种确信表述

🔴 球员关系、更衣室矛盾、情绪解读：
- 必须写"有球迷解读""据传闻"或标注"老六推测"
- 禁止包装成确凿事实

📌 简单规则：数据来源的事实→直接写。公众号来源→标注"据媒体报道"。推测→标注"老六觉得"。

写作风格：
{style}

硬性规范：
- {word_count_rule}
- 必须包含 ≥2 个 ## 二级标题
- 文末必须包含2张配图标记：![配图1](images/article-{index}-img-001.jpg) 等
- 事实红线：主体基于来源摘要，有几分事实说几分话

禁用词：震惊、吓尿、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说
禁用模式：不要列一二三四，不要学术论文腔，不要"老六认为""老六分析"标签

输出JSON:
{{"title": "优选标题(15-25字)", "backup_title": "备选标题(不同角度，15-25字)", "content": "Markdown正文({word_min}-{word_max}字，含≥2个##小标题，文末含2个配图标记)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "站队式/投票式/预测式/共鸣式/挑战式/调侃式", "interaction_bait": "互动问题", "content_type": "{content_type}", "sources_used": ["来源文章标题"], "originality_note": "如何区别于原文(20字)"}}
只输出JSON。"""

    base_prompt = load_prompt_template("article_generator.txt")
    if not base_prompt:
        base_prompt = f"你是头条号足球博主'球评人老六'，有态度有人味。你的工作是转写改编真实热点文章：用新角度新语言重新组织事实，加自己的分析态度。事实来自素材，观点来自你。绝不编造素材里没有的事实。风格：{style} 用自然口语化中文写作。"

    messages = [
        {"role": "system", "content": f"{base_prompt}\n\n本次写作风格要求：{style}"},
        {"role": "user", "content": prompt}
    ]
    response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-pro", messages, temperature=temperature, max_tokens=8192)
    article = safe_json_loads(response)
    # Inject column metadata if present (flatten into article)
    if column_meta:
        article["_column_id"] = column_meta.get("column_id", "")
        article["_column_name"] = column_meta.get("column_name", "")
        article["_batch_name"] = column_meta.get("batch_name", "")
        article["_batch_time"] = column_meta.get("batch_time", "")
    print(f"   标题: {article.get('title','?')}, 正文: {len(article.get('content',''))}字")
    return article


# ============================================================
# Quality Validation & Retry
# ============================================================

def validate_article(article, index, is_tieba=False, min_words=500):
    """Validate article quality. Returns (is_valid, issues_list, originality_score)."""
    issues = []
    score = 100  # Start from 100, deduct for each issue
    content = article.get("content", "")
    title = article.get("title", "")
    min_images = 2
    min_h2 = 2

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


def _fact_check_article(article, match_data, gzh_articles=None):
    """Use a lightweight LLM call to fact-check the article against match_data.

    Sends the article content + match_data to a fast LLM model and asks it to
    verify each factual claim. Returns {"passed": bool, "issue": str}.

    This is a second line of defense after the regex-based check_data_hallucination().
    The regex check catches obvious hallucinations (射门数, 控球率, etc.) but the
    LLM can catch subtler issues like wrong scores, fictional events, etc.
    """
    try:
        content = article.get("content", "")
        title = article.get("title", "")
        match_text = json.dumps(match_data, ensure_ascii=False, indent=2)[:2000]

        # Scan for data_confidence conflicts
        conflict_matches = []
        all_fixtures = match_data.get("all_fixtures", []) if isinstance(match_data, dict) else []
        for f in all_fixtures:
            if f.get("data_confidence") == "conflict":
                home = f.get("home_team", "")
                away = f.get("away_team", "")
                conflict_matches.append(f"{home} vs {away}")

        confidence_warning = ""
        if conflict_matches:
            confidence_warning = f"""
⚠️ 重要：以下比赛的数据来源存在比分冲突（API与Wikipedia数据不一致），这些比赛的比分不可靠：
{', '.join(conflict_matches[:5])}
如文章使用了上述比赛的比分，必须标记为"比分错误": 使用了不可靠的数据来源。"""

        prompt = f"""你是一个严格的事实核查员。请检查下面这篇文章中的每个事实陈述是否能在比赛数据中找到依据。

比赛数据：
{match_text}
{confidence_warning}
文章内容：
标题：{title}
正文：{content[:2000]}

核查规则（逐条执行）：
1. 比赛数据只包含：球队名、比分、联赛名、比赛状态、开球时间
2. 如果文章提到了比赛数据中不存在的信息（如射门数、射正数、控球率、进球球员、进球时间、红黄牌等），标记为"数据幻觉"
3. 如果文章的比分与比赛数据不一致，标记为"比分错误"
4. 如果文章提到了不存在的比赛（数据中没有的球队对），标记为"虚构比赛"
5. ⚠️ 如果文章使用了 data_confidence=conflict 的比赛的具体比分（例如写了"A队X-B队Y战胜"），标记为"使用了不可靠数据"
6. ⚠️ 如果文章出现了"独造X球""帽子戏法""梅开二度""绝杀"等球员表现断言，且未标注来源（"据媒体报道""据公众号称"等），标记为"球员断言未标注来源"
7. ⚠️ 如果文章将推测/传闻/解读性内容包装成事实（如"眼神骗不了人""这事没得洗"等），标记为"推测包装成事实"
8. 如果文章的所有事实都能在数据中找到依据，且推测性内容已标注，标记为"通过"

只输出JSON：
{{"passed": true/false, "issue": "具体问题描述，如果没有问题则为空字符串"}}"""

        messages = [
            {"role": "system", "content": "你是一个严格的事实核查员。只输出JSON。"},
            {"role": "user", "content": prompt}
        ]

        response = call_llm(DEEPSEEK_URL, DEEPSEEK_KEY, "deepseek-v4-flash",
                           messages, temperature=0.1, max_tokens=512)
        result = safe_json_loads(response)

        if result and isinstance(result, dict):
            passed = result.get("passed", True)
            issue = result.get("issue", "")
            if not passed and issue:
                print(f"   🔍 事实核查: {issue}")
            return {"passed": passed, "issue": issue}
    except Exception as e:
        print(f"   ⚠️  事实核查异常: {e}")

    # 🔴 CRITICAL: fact-check failure or error → fail-closed (assume not passed)
    return {"passed": False, "issue": "事实核查异常"}


def check_content_references_data(article, match_data, gzh_articles=None):
    """Sanity check: verify article content references at least one real data source.

    Extracts known team and player names from the article body, then checks
    if any appear in today's match data or GZH source articles.
    Returns (passes_check, matched_names, warning).
    """
    content = article.get("content", "") + article.get("title", "")
    if not content:
        return False, [], "内容为空"

    # Collect all known entities from today's data
    today_entities = set()

    # From match data: teams
    if match_data and match_data.get("all_fixtures"):
        for m in match_data["all_fixtures"]:
            home = m.get("home_team", "")
            away = m.get("away_team", "")
            if home:
                today_entities.add(home.lower())
            if away:
                today_entities.add(away.lower())

    # From match data: leagues
    if match_data and match_data.get("fixtures_by_league"):
        for league in match_data["fixtures_by_league"]:
            today_entities.add(league.lower())

    # From GZH source articles: title entities
    if gzh_articles:
        for a in gzh_articles[:8]:
            title = a.get("title", "")
            if title:
                today_entities.add(title.lower()[:60])

    # From topic source_article_ids (for GZH-based articles)
    sources = article.get("sources_used", [])
    for s in sources:
        if s:
            today_entities.add(s.lower()[:60])

    # Check content against WIKI_TEAMS (known real teams)
    from constants import WIKI_TEAMS, WIKI_PLAYERS
    matched = []
    content_lower = content.lower()
    for team_cn in WIKI_TEAMS:
        if team_cn.lower() in content_lower:
            matched.append(team_cn)
    for player_cn in WIKI_PLAYERS:
        if player_cn.lower() in content_lower:
            matched.append(player_cn)

    if matched:
        return True, matched, ""

    # No known entities found — could still be valid (e.g., fun gossip)
    # Check if content references any non-empty entity from matches
    for entity in today_entities:
        if len(entity) >= 3 and entity in content_lower:
            return True, [entity[:30]], ""

    return False, [], "文章中未检测到比赛球队/球员或今日数据源实体"


def check_data_hallucination(article, match_data):
    """Detect if article contains data NOT present in match_data.

    match_data only contains: team names, scores, league name, status, utc_date.
    Any other numbers (shots, possession, passes, goalscorers, etc.) are hallucinations.

    Returns (passes_check, hallucinated_items, warning).
    """
    content = article.get("content", "") + article.get("title", "")
    if not content:
        return True, [], ""

    # Collect all known numeric data from match_data
    known_numbers = set()
    if match_data and match_data.get("all_fixtures"):
        for m in match_data["all_fixtures"]:
            hg = m.get("home_score")
            ag = m.get("away_score")
            if hg is not None:
                known_numbers.add(str(hg))
            if ag is not None:
                known_numbers.add(str(ag))

    # Known non-numeric entities from match_data
    known_entities = set()
    if match_data and match_data.get("all_fixtures"):
        for m in match_data["all_fixtures"]:
            home = m.get("home_team", "").lower()
            away = m.get("away_team", "").lower()
            if home:
                known_entities.add(home)
            if away:
                known_entities.add(away)

    # Patterns that indicate hallucinated data (NOT in match_data)
    # match_data ONLY contains: team names, scores (X-Y format), league name, status, utc_date
    # ALL of the following patterns indicate fabricated data because match_data never has these.
    hallucination_patterns = [
        # Technical stats — NEVER in match_data
        (r'(\d+)脚射门', '射门数'),
        (r'(\d+)次射门', '射门次数'),
        (r'(\d+)射正', '射正数'),
        (r'(\d+)次射正', '射正次数'),
        (r'(\d+)%控球', '控球率'),
        (r'控球率达(\d+)', '控球率'),
        (r'(\d+)%的控球', '控球率'),
        (r'(\d+)传球', '传球数'),
        (r'(\d+)次传球', '传球次数'),
        (r'(\d+)%传球成功率', '传球成功率'),
        (r'(\d+)角球', '角球数'),
        (r'(\d+)个角球', '角球个数'),
        (r'(\d+)犯规', '犯规数'),
        (r'(\d+)次犯规', '犯规次数'),
        (r'(\d+)张[黄红]牌', '牌数'),
        (r'(\d+)次扑救', '扑救数'),
        (r'(\d+)越位', '越位数'),
        (r'(\d+)次越位', '越位次数'),
        (r'(\d+)次抢断', '抢断数'),
        (r'(\d+)次拦截', '拦截数'),
        (r'(\d+)次解围', '解围数'),
        (r'(\d+)次威胁传球', '威胁传球'),
        (r'(\d+)次关键传球', '关键传球'),
        (r'(\d+)次射门', '射门次数2'),
        (r'(\d+)次进攻', '进攻次数'),
        (r'(\d+)次危险进攻', '危险进攻'),
        # Player performance claims — NEVER in match_data
        (r'帽子戏法', '帽子戏法'),
        (r'梅开二度', '梅开二度'),
        (r'独造(\d+)球', '独造进球'),
        (r'独中(\d+)元', '独中多元'),
        (r'上演帽子戏法', '上演帽子戏法'),
        (r'大四喜', '大四喜'),
        (r'助攻\w+(?:破门|得分)', '助攻描述'),
        (r'连过(\d+)人', '连过人数'),
        # Time and event fabrications — NEVER in match_data
        (r'第(\d+)分钟', '进球时间'),
        (r'\d+分钟[时时]', '比赛时间描述'),
        (r'补时第(\d+)分钟', '补时时间'),
        (r'上半场补时', '上半场补时'),
        (r'下半场补时', '下半场补时'),
        (r'加时赛第(\d+)分钟', '加时时间'),
        # Player-specific claims — NEVER in match_data
        (r'(?:被)?[一-鿿]{2,4}(?:换下|替下)', '球员替换描述'),
        (r'(?:被)?[一-鿿]{2,4}(?:受伤|倒地|痛苦)', '球员受伤描述'),
        # Event claims that need match_data (which doesn't have them)
        (r'点球[破得打罚命中进]', '点球事件'),
        (r'红牌', '红牌事件'),
        (r'绝杀', '绝杀'),
    ]

    hallucinated = []
    for pattern, desc in hallucination_patterns:
        try:
            if re.search(pattern, content):
                hallucinated.append(desc)
        except re.error:
            continue  # Skip invalid patterns silently

    if hallucinated:
        return False, hallucinated, f"检测到数据幻觉: {', '.join(hallucinated)} — match_data不包含这些数据"

    return True, [], ""


def check_cross_day_duplicate(title, content, date_str):
    """Check if the generated article is too similar to any article in the past 7 days.

    Returns (is_duplicate, matched_title, similarity_score).
    Uses title substring overlap and longest-common-subsequence ratio.
    """
    from difflib import SequenceMatcher

    today = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(1, 8):
        dt = today - timedelta(days=i)
        meta_path = OUTPUT_DIR / dt.strftime("%Y-%m-%d") / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
            for a in meta.get("articles", []):
                hist_title = a.get("title", "")
                if not hist_title or len(hist_title) < 8:
                    continue

                # Check 1: long common substring (15+ chars) = likely duplicate
                shorter = title if len(title) <= len(hist_title) else hist_title
                longer = hist_title if len(title) <= len(hist_title) else title
                for start in range(len(shorter) - 14):
                    sub = shorter[start:start + 15]
                    if sub in longer:
                        return True, hist_title, 100

                # Check 2: title similarity via SequenceMatcher
                title_ratio = SequenceMatcher(None, title[:40], hist_title[:40]).ratio()
                if title_ratio > 0.65:
                    return True, hist_title, round(title_ratio * 100)

                # Check 3: content overlap — first 100 chars of new vs old content
                hist_content = a.get("content", "")
                if hist_content and len(content) > 50 and len(hist_content) > 50:
                    content_ratio = SequenceMatcher(
                        None, content[:100], hist_content[:100]).ratio()
                    if content_ratio > 0.7:
                        return True, hist_title, round(content_ratio * 100)

        except Exception:
            pass

    return False, "", 0


def generate_article_with_retry(topic, match_context, index, gzh_articles=None,
                                is_gossip=False, is_tieba=False, tieba_context=None,
                                max_retries=2, date_str=None):
    """Generate article with validation and automatic retry on failure.

    On retry, progressively lowers temperature and strengthens the prompt
    to force longer, more structured output. Also detects short raw responses
    before JSON parsing to fail fast.
    """
    last_issues = ""
    # Column-aware word count: use topic's _word_count_range if available
    word_range = topic.get("_word_count_range", [500, 800])
    topic_min_words = word_range[0] if isinstance(word_range, (list, tuple)) and len(word_range) >= 1 else 500
    for attempt in range(max_retries + 1):
        temp = max(0.3, 0.8 - attempt * 0.2)  # 0.8 → 0.6 → 0.4
        try:
            if is_tieba:
                art = generate_tieba_article(topic, index, tieba_context,
                                             match_context=match_context,
                                             temperature=temp, retry_hint=last_issues)
            elif is_gossip:
                art = generate_gossip_article(topic, index, temperature=temp,
                                              retry_hint=last_issues)
            else:
                art = generate_article(topic, match_context, index, gzh_articles,
                                       temperature=temp, retry_hint=last_issues)

            # Check raw content sanity before full validation
            content = art.get("content", "")
            content_min = max(200, int(topic_min_words * 0.6))
            if len(content) < content_min and attempt < max_retries:
                print(f"   ⚠️  正文过短({len(content)}字)，直接重试")
                last_issues = f"上次正文仅{len(content)}字，远低于{topic_min_words}字最低要求。请基于提供的事实数据充实内容。"
                continue

            is_valid, issues, score = validate_article(art, index, is_tieba=is_tieba, min_words=topic_min_words)
            if is_valid and score >= 85:
                # Data source reference check: verify article references real data
                if match_context:
                    passes, matched, ref_warning = check_content_references_data(art, match_context, gzh_articles)
                    if not passes and matched is not None:
                        print(f"   ⚠️  数据源引用警告: {ref_warning}")
                        issues.append(ref_warning)
                        score -= 15
                        if score < 85:
                            last_issues = "; ".join(issues)
                            if attempt < max_retries:
                                print(f"   🔄 重试 (需引用真实数据源)...")
                                continue
                            else:
                                pass  # Allow through with low score rather than total failure

                # Data hallucination check: verify article doesn't contain fabricated data
                if match_context:
                    hallucination_pass, hallucinated, hallucination_warning = check_data_hallucination(art, match_context)
                    if not hallucination_pass:
                        print(f"   ❌ 数据幻觉检测失败: {hallucination_warning}")
                        issues.append(hallucination_warning)
                        score -= 30  # Heavy penalty for hallucination
                        if score < 85:
                            last_issues = "; ".join(issues)
                            if attempt < max_retries:
                                print(f"   🔄 重试 (检测到数据幻觉，禁止编造)...")
                                continue
                            else:
                                # 🔴 CRITICAL: hallucinated data → must NOT publish
                                return {}, f"数据幻觉: {hallucination_warning}"

                # LLM fact-check: use a second LLM call to verify article facts against match_data
                if match_context and score >= 85:
                    fact_check_result = _fact_check_article(art, match_context, gzh_articles)
                    if not fact_check_result.get("passed", True):
                        fact_issue = f"事实核查失败: {fact_check_result.get('issue', '')}"
                        print(f"   ❌ {fact_issue}")
                        issues.append(fact_issue)
                        score -= 25
                        if score < 85:
                            last_issues = "; ".join(issues)
                            if attempt < max_retries:
                                print(f"   🔄 重试 (事实核查未通过)...")
                                continue
                            else:
                                # 🔴 CRITICAL: fact-check failed → must NOT publish
                                return {}, f"事实核查: {fact_check_result.get('issue', 'unknown')}"

                # Cross-day dedup check
                if date_str:
                    title = art.get("title", "")
                    content = art.get("content", "")
                    is_dup, dup_title, dup_score = check_cross_day_duplicate(title, content, date_str)
                    if is_dup:
                        dup_issue = f"与历史文章雷同(相似度{dup_score}%，匹配: {dup_title[:30]})"
                        print(f"   ⚠️  跨天去重失败: {dup_issue}")
                        issues.append(dup_issue)
                        score -= 40
                        last_issues = "; ".join(issues)
                        if attempt < max_retries:
                            print(f"   🔄 重试 (加强去重约束)...")
                            continue
                        else:
                            return art, f"跨天去重失败: {dup_issue}"

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


def generate_tieba_article(topic, index, post_data, match_context=None, temperature=0.5, retry_hint=""):
    """Generate a real football article inspired by a Hupu hot post.

    The Hupu post provides the angle and fan sentiment — it's a starting point,
    not the article itself. The article should be a proper football piece that
    happens to use fan discussion as its hook, not a forum-thread summary.
    """
    team = post_data.get("team", "")
    post_title = post_data.get("title", "")
    main_content = post_data.get("main_content", "")
    replies = post_data.get("top_replies", [])
    reply_num = post_data.get("reply_num", 0)

    print(f"\n[Hupu-{index}] {team}: {post_title[:40]} ({reply_num}回复)...")

    # Build Hupu context — the "hook", not the "script"
    context = f"【{team}专区】原帖标题：{post_title}\n回复数：{reply_num}\n"
    if main_content:
        context += f"\n楼主观点：{main_content[:400]}\n"

    if replies:
        context += "\n热门评论（反映球迷情绪和观点，⚠️ 注意：评论中的数字是球迷个人说法，非官方统计数据，引用时需标注来源）：\n"
        for j, r in enumerate(replies[:6]):
            author = r.get("author", "匿名")
            agree = r.get("agree_count", 0)
            content = r.get("content", "")
            context += f"\n球迷{j+1}（{agree}赞）：{content[:250]}\n"

    # Build match context for football knowledge
    match_text = ""
    if match_context:
        fixtures = match_context.get("fixtures_by_league", {})
        standings = match_context.get("standings", {})
        if fixtures:
            match_text = "\n近期比赛数据（可作为文章背景和论据）：\n"
            for league, matches in sorted(fixtures.items()):
                match_text += f"\n## {league}\n"
                for m in matches[:4]:
                    hg, ag = m.get("home_score"), m.get("away_score")
                    score = f"{hg}-{ag}" if hg is not None else "vs"
                    match_text += f"  {m['home_team']} {score} {m['away_team']}\n"
        if standings:
            match_text += "\n联赛积分榜前列：\n"
            for league, table in list(standings.items())[:3]:
                match_text += f"\n{league}:\n"
                for r in table[:5]:
                    match_text += f"  {r.get('position','?')}. {r.get('team','?')} {r.get('points','?')}分\n"

    retry_block = ""
    if retry_hint:
        retry_block = f"\n⚠️ 上次问题：{retry_hint}\n这次必须修正。正文至少500字，至少2个##小标题，文末至少2张配图。\n"

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。下面有一个虎扑热帖，它反映了球迷圈当下最真实的情绪和关注点。你的任务：**把这个帖子当成选题线索，写一篇真正有干货的足球文章。**

=== 选题线索：虎扑热帖 ===
{context[:5000]}
{match_text}

=== 写作核心原则 ===

1. **帖子是钩子，不是正文**
   - 帖子里球迷在吵什么 → 这是选题方向，不是你文章的全部内容
   - 开篇必须反套路——禁止"虎扑上有个帖子""近日球迷热议"。用场景、反差、或者直接亮观点开头
   - 整篇文章中，引用/转述虎扑网友观点的比例不要超过40%

2. **你有60%的内容要靠自己的足球知识来写**
   - 从帖子情绪中提炼一个具体的足球命题，正面回答它
   - 可以用历史类比、战术逻辑推演、联赛横向对比
   - **至少1处跨界视角**：行业类比、生活类比、文化类比
   - **每个关键数字配人话翻译**：不能裸奔数据，必须解读"这意味着什么"

3. **不要写"论坛吵架实录"**
   - 不要：张三说X，李四回Y，老六觉得都有道理
   - 要：提炼球迷争论的核心命题，你自己正面回答，球迷观点一句话带过即可

4. **写作风格**
   - 赛后和朋友聊球的语气：直接、有观点、不骑墙
   - 用"说白了就是""仔细想想""如果是我看"这类自然口语，不要"老六分析""老六认为"标签
   - 暴露思考过程：可以说"说实话我也没想到""我查了数据才发现"
   - 不要列一二三四，不要学术论文腔
   {retry_block}

=== ⚠️ 数据真实性铁律（违反即失败） ===

你只能使用下面两类数据作为「事实」来写作：

✅ 可信事实来源（只有这两类）：
  A. 上面「比赛数据」板块里的比分、联赛名、球队名、积分榜排名
  B. 虎扑帖子中「楼主观点」陈述的客观事件（如"XX转会费8000万"）
  C. 虎扑帖子中「热门评论」里的观点和情绪（标明是"有球迷觉得""虎扑上有人提到"）

🚫 禁止编造（常见错误）：
  - 积分榜只有前5名排名和积分，禁止写"只输了X场""净胜球XX"等不在素材里的统计
  - 比赛数据只有比分和球队名，禁止编造进球球员、进球时间、红黄牌等细节
  - 禁止编造不在素材里的球员姓名（如"加纳乔如何如何""萨卡怎么怎么样"——除非素材里明确提到了他们）
  - 禁止编造百分比、转化率等精确数字（素材里没有的统计，用"似乎""看起来"软化表达）
  - 禁止把训练知识当事实：你知道某队的球员名单，但今天素材里没提到的球员就不能写

🟡 允许的推测（必须标注为推测）：
  - "从战术逻辑上看，这可能是..."
  - "结合积分榜位置推测，球队的策略应该是..."
  - "虽然没看到具体数据，但这个趋势似乎说明..."

结构（自由组织）：
- 开篇：从帖子里最抓人的一个点切入，快速抛出核心问题
- 主体：用「可信事实」支撑你的分析，允许推测但要标注
- 收尾：给出你的明确判断 + 互动钩子

硬性规范：
- 正文 500-800 字
- ≥2 个 ## 二级标题（标题也必须来自素材中的真实话题，不能凭空拟题）
- ≥2 张配图标记：![配图1](images/article-{index}-img-001.jpg)
- 球迷观点归纳即可，一句引用不超过15字

禁用词：震惊、吓尿、看傻了、众所周知、值得一提的是、从某种意义上说、不得不说
禁用模式：不要每段以"老六"开头，不要"一部分球迷认为...另一部分球迷认为..."来回拉锯

输出JSON:
{{"title": "优选标题(15-25字，有网感有态度)", "backup_title": "备选标题(不同角度，15-25字)", "content": "Markdown正文(500-800字)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "站队式/投票式/预测式/共鸣式/挑战式/调侃式", "interaction_bait": "互动问题", "content_type": "球迷讨论", "source_post": "{post_title[:50]}"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是头条号足球博主'球评人老六'，10万粉丝。你有丰富的足球知识、战术分析能力和鲜明的个人观点。虎扑帖子只是选题线索——真正的文章内容来自你的足球知识储备和对比赛的理解。写作风格：口语化、有态度、像赛后和球友聊天。只输出JSON。"},
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
                     "tags": art.get("keywords", []), "category": "足球",
                     "column_id": art.get("_column_id", ""),
                     "column_name": art.get("_column_name", ""),
                     "batch_name": art.get("_batch_name", ""),
                     "batch_time": art.get("_batch_time", "")}
        result = file_writer.save_article(date_str=date_str, index=idx, article_data=art_data)
        saved.append({"index": idx, "title": art.get("title", ""), "path": result["article_path"],
                       "slug": result["slug"], "tags": art.get("keywords", []),
                       "keywords": art.get("keywords", []), "images": result["image_paths"],
                       "sources_used": art.get("sources_used", []),
                       "source_post": art.get("source_post", ""),
                       "originality_note": art.get("originality_note", ""),
                       "content_type": art.get("content_type", ""),
                       "column_id": art.get("_column_id", ""),
                       "column_name": art.get("_column_name", ""),
                       "batch_name": art.get("_batch_name", "")})

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
            status = m.get("status", "")

            # Skip unfinished matches — don't treat in-progress data as final results
            if status not in ("FT", "AET", "PEN"):
                continue

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
                                    articles_out, is_gossip=False, gzh_articles=None,
                                    date_str=None):
    """Shared article generation loop — used by all three data-source branches."""
    for i, topic in enumerate(topics[:count]):
        ct = topic.get("content_type", "N/A")
        print(f"\n--- 第{i+1}/{count}篇 [{ct}] ---")
        imgs = search_images(topic, count=5)
        images_map[i] = imgs
        kwargs = {"max_retries": 2, "date_str": date_str}
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
            # Don't append failed articles — they contain fabricated data
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
                is_tieba=True, tieba_context=post, max_retries=2, date_str=date_str)
            stats["generated"] += 1
            if error:
                print(f"   ❌ 最终失败: {error}")
                stats["failed"] += 1
                stats["issues"].append(f"第{t_idx}篇(虎扑): {error}")
                # Don't append failed articles — skip this slot
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
        date_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

    # Load season weights for content type optimization
    season_weights, season_label = load_season_weights(date_str)
    performance_boost = {}

    if batch_mode in BATCH_CONFIG:
        batch_cfg = BATCH_CONFIG[batch_mode]
        slots = batch_cfg["slots"]
        article_count = len(slots)
        # Columns are fixed per batch — no season weight type swapping
        # Season weights only affect topic selection framing, not column identity
        column_names = [s["column_name"] for s in slots]
        print(f"足球自媒体内容自动化 - {date_str} (batch={batch_mode}, 栏目={', '.join(column_names)}, {batch_cfg['name']}·{batch_cfg['time']})\n")
        target_types = None  # Column-driven, not type-driven
    elif batch_mode in BATCH_TYPES:
        # Legacy: old content-type-based batch mode (kept for backward compat)
        target_types = list(BATCH_TYPES[batch_mode])
        article_count = 2
        print(f"足球自媒体内容自动化 - {date_str} (batch={batch_mode}, types={target_types}, legacy)\n")
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

        # 🌙 Evening batch: dynamically select 2 columns from EVENING_COLUMN_POOL
        # based on today's GZH trending data, then override the evening slots.
        if batch_mode == "evening":
            print("\n🌙 晚间栏目动态选择...")
            gzh_trends = fetch_gzh_football_trends(date_str, fallback_match_data=match_data)
            selected_cols = select_evening_columns(gzh_trends, match_data, season_label)
            # Assign slot indices and update BATCH_CONFIG in-place
            for i, col in enumerate(selected_cols):
                col["slot"] = i
            BATCH_CONFIG["evening"]["slots"] = selected_cols
            # Recompute dependent variables
            batch_cfg = BATCH_CONFIG[batch_mode]
            slots = batch_cfg["slots"]
            article_count = len(slots)
            column_names = [s["column_name"] for s in slots]
            print(f"   晚间栏目: {', '.join(column_names)} ({batch_cfg['name']}·{batch_cfg['time']})\n")

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

        # Cross-batch dedup: for legacy BATCH_TYPES mode, avoid repeating content types.
        # In BATCH_CONFIG mode, columns are unique by design — skip type-based dedup.
        if batch_mode in BATCH_TYPES and batch_mode not in BATCH_CONFIG and cross_batch_covered.get("content_types"):
            already_covered_types = cross_batch_covered["content_types"]
            for i, ct in enumerate(target_types or []):
                if ct in already_covered_types:
                    candidates = sorted(season_weights.items(), key=lambda x: -x[1]) if season_weights else []
                    all_types = ["八卦趣事", "转会资讯", "战术解析", "热点球评", "排行榜"]
                    for alt_type, _ in candidates:
                        if alt_type not in already_covered_types and alt_type not in (target_types or []):
                            print(f"   🔄 跨批次去重: {ct}(已在今日覆盖) → {alt_type}")
                            target_types[i] = alt_type
                            already_covered_types.add(alt_type)
                            break
                    else:
                        for alt_type in all_types:
                            if alt_type not in already_covered_types and alt_type not in (target_types or []):
                                print(f"   🔄 跨批次去重(fallback): {ct}(已覆盖) → {alt_type}")
                                target_types[i] = alt_type
                                already_covered_types.add(alt_type)
                                break

        # ============================================================
        # Main Article Pipeline
        # ============================================================

        # Build column-aware type guidance for topic selection
        # In BATCH_CONFIG mode, inject column domain guidance into the prompt
        column_type_hint = None
        all_gzh_only = False
        if batch_mode in BATCH_CONFIG and not target_types:
            slots = BATCH_CONFIG[batch_mode]["slots"]
            column_type_hint = [s["topic_domain"] for s in slots]
            # Evening batch columns use gzh_only data source — route to GZH path.
            # Always route to GZH path so column domain matches topic selection.
            all_gzh_only = all(s.get("data_source_hint") == "gzh_only" for s in slots)
            if all_gzh_only:
                print(f"   栏目全部为 gzh_only，切换为公众号爆款数据模式\n")

        if all_gzh_only:
            topics_and_raw = collect_real_gzh_topics(date_str, topic_history, preferred_types=target_types, season_weights=season_weights, cross_batch_covered=cross_batch_covered, column_type_hint=column_type_hint, season_label=season_label, match_data=match_data)
            if not topics_and_raw or not topics_and_raw[0]:
                result_msg = "无真实爆款数据可用(gzh_only batch)"
                print(f"ERROR: {result_msg}")
                send_wxpusher("足球自媒体 ⚠️", f"{date_str} 发文任务中止：{result_msg}")
                return

            topics, raw_articles = topics_and_raw
            extra_meta = {"type": "gzh_only_batch"}
            _assign_columns_to_topics(topics, batch_mode)

            _generate_articles_from_topics(topics, article_count, match_data, images_map, stats, articles, is_gossip=True, date_str=date_str)

        elif topic_preference != "auto":
            print(f"   用户偏好: {topic_preference}，使用公众号爆款数据为主\n")
            topics_and_raw = collect_real_gzh_topics(
                date_str, topic_history, topic_preference=topic_preference, preferred_types=target_types, season_weights=season_weights, cross_batch_covered=cross_batch_covered, season_label=season_label, match_data=match_data)
            if not topics_and_raw or not topics_and_raw[0]:
                result_msg = f"无{topic_preference}相关真实爆款数据可用"
                print(f"ERROR: {result_msg}")
                send_wxpusher("足球自媒体 ⚠️", f"{date_str} 发文任务中止：{result_msg}")
                return

            topics, raw_articles = topics_and_raw
            extra_meta = {"type": f"gzh_{topic_preference}"}
            _assign_columns_to_topics(topics, batch_mode)

            _generate_articles_from_topics(topics, article_count, match_data, images_map, stats, articles, is_gossip=True, date_str=date_str)

        elif match_data["total_matches"] == 0:
            print("   今日无比赛，切换为公众号爆款数据模式\n")
            topics_and_raw = collect_real_gzh_topics(date_str, topic_history, preferred_types=target_types, season_weights=season_weights, cross_batch_covered=cross_batch_covered, season_label=season_label, match_data=match_data)
            if not topics_and_raw or not topics_and_raw[0]:
                result_msg = "无比赛且无真实爆款数据可用"
                print(f"ERROR: {result_msg}")
                send_wxpusher("足球自媒体 ⚠️", f"{date_str} 发文任务中止：{result_msg}")
                return

            topics, raw_articles = topics_and_raw
            extra_meta = {"type": "gzh_real_data"}
            _assign_columns_to_topics(topics, batch_mode)

            _generate_articles_from_topics(topics, article_count, match_data, images_map, stats, articles, is_gossip=True, date_str=date_str)

        else:
            print("\n   获取公众号爆款趋势作为跨源参考...")
            gzh_raw = fetch_gzh_football_trends(date_str, fallback_match_data=match_data)
            gzh_context = gzh_raw[:8] if gzh_raw else []

            topics = select_topics(match_data, gzh_context, topic_history, preferred_types=target_types, season_weights=season_weights, cross_batch_covered=cross_batch_covered, season_label=season_label, topic_count=article_count)
            extra_meta = {"type": "match_analysis"}
            _assign_columns_to_topics(topics, batch_mode)

            _generate_articles_from_topics(topics, article_count, match_data, images_map, stats, articles, gzh_articles=gzh_context, date_str=date_str)

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
                    match_data, e_idx, max_retries=1, date_str=date_str)
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

        # ============================================================
        # Micro-headline: generate short-form content from match data
        # ============================================================
        if match_data and match_data.get("all_fixtures"):
            try:
                headlines = generate_micro_headlines(match_data, count=2)
                if headlines:
                    result["micro_headlines"] = headlines
                    # Also persist to metadata.json so publisher can find them
                    try:
                        meta_path = OUTPUT_DIR / date_str / "metadata.json"
                        if meta_path.exists():
                            meta = json.loads(meta_path.read_text())
                            meta["micro_headlines"] = headlines
                            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                    except Exception:
                        pass
                    print(f"\n   📢 已生成 {len(headlines)} 条微头条")
            except Exception as e:
                print(f"   ⚠️ 微头条生成失败: {e}")

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
