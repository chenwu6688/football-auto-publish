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
from constants import (PROJECT_ROOT, OUTPUT_DIR,
                       HY3_API_KEY, HY3_BASE_URL, HY3_MODEL_FLASH, HY3_MODEL_PRO,
                       DASHSCOPE_KEY, UNSPLASH_KEY, FOOTBALL_DATA_KEY,
                       DASHSCOPE_URL, FOOTBALL_DATA_BASE,
                       WXPUSHER_APPTOKEN, WXPUSHER_UID,
                       WIKI_PLAYERS, WIKI_TEAMS, FOOTYRENDERS_PLAYERS,
                       BATCH_CONFIG)
from utils import retry, call_llm, safe_json_loads, load_prompt_template
from logger import log
from data_collector import (collect_real_matches, collect_transfer_news, collect_future_matches,
                             search_images, search_wikipedia, search_footyrenders,
                             extract_search_entities, get_topic_history)


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
                # 叠加历史效果反馈：如果 performance_log.json 有数据，自动调整权重
                perf_adjusted = _apply_performance_boost(weights.copy())
                if perf_adjusted != weights:
                    diffs = {k: f"{v:.1f}→{perf_adjusted.get(k, v):.1f}" for k, v in weights.items()
                             if perf_adjusted.get(k, v) != v}
                    if diffs:
                        print(f"   📊 效果反馈调权: {diffs}")
                    weights = perf_adjusted
                print(f"   📅 赛季节奏: {label} (月份{month}, 权重: {weights})")
                return weights, label

        # Default: balanced
        return {"热点球评": 1.0, "转会资讯": 1.0, "排行榜": 1.0, "八卦趣事": 1.0, "战术解析": 1.0}, "常规赛季"
    except Exception as e:
        print(f"   ⚠️  加载赛季权重失败: {e}")
        return None, ""


def _apply_performance_boost(weights):
    """根据 performance_log.json 的历史阅读数据，自动微调选题权重。

    规则：
    - 读取最近7天的效果数据
    - 计算每篇 content_type 的平均阅读量
    - 如果某 content_type 平均阅读 > 全局均值 20%，权重 +0.3
    - 如果某 content_type 平均阅读 < 全局均值 20%，权重 -0.2
    - 权重范围限制在 [0.3, 3.0] 之间
    """
    perf_path = OUTPUT_DIR / "performance_log.json"
    if not perf_path.exists():
        return weights

    try:
        perf = json.loads(perf_path.read_text())
        articles_data = perf.get("articles", {})
        if not articles_data:
            return weights

        # Group by content_type: need to read metadata to map article index -> content_type
        type_stats = {}  # content_type -> [reads]
        for key, p in articles_data.items():
            reads = p.get("reads", 0)
            if reads <= 0:
                continue
            date_str = p.get("date", "")
            idx = p.get("index", 0)
            # Read metadata to get content_type
            meta_path = OUTPUT_DIR / date_str / "metadata.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                    for a in meta.get("articles", []):
                        if a.get("index") == idx:
                            ct = a.get("content_type", "")
                            if ct:
                                type_stats.setdefault(ct, []).append(reads)
                            break
                except Exception:
                    pass

        if not type_stats:
            return weights

        # Calculate per-type average
        type_avg = {ct: sum(vs)/len(vs) for ct, vs in type_stats.items()}
        global_avg = sum(type_avg.values()) / len(type_avg)

        # Apply boost/reduction
        for ct, avg_reads in type_avg.items():
            if ct in weights:
                ratio = avg_reads / global_avg if global_avg > 0 else 1.0
                if ratio > 1.2:
                    weights[ct] = min(3.0, weights[ct] + 0.3)
                elif ratio < 0.8:
                    weights[ct] = max(0.3, weights[ct] - 0.2)

        return weights
    except Exception:
        return weights


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


def get_yesterday_keywords(date_str):
    """Get yesterday's article keywords for cross-day dedup.

    Returns set of lowercase keywords/tags from the previous day's metadata.
    Used by select_topics() as a hard filter to prevent same-match repeat across days.
    """
    yesterday_kw = set()
    try:
        from datetime import timedelta
        dt = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)
        meta_path = OUTPUT_DIR / dt.strftime("%Y-%m-%d") / "metadata.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            for a in meta.get("articles", []):
                for kw in a.get("keywords", []):
                    yesterday_kw.add(kw.lower())
                for tag in a.get("tags", []):
                    yesterday_kw.add(tag.lower())
            if yesterday_kw:
                print(f"   跨天去重: 昨日的 {len(meta.get('articles', []))} 篇覆盖 "
                      f"{len(yesterday_kw)} 个关键词，将过滤今日同类选题")
    except Exception:
        pass
    return yesterday_kw


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
        topic["content_type"] = topic.get("content_type", "八卦趣事")

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


def select_topics(match_data, topic_history=None, preferred_types=None, season_weights=None, cross_batch_covered=None, season_label="", topic_count=3, yesterday_keywords=None):
    print(f"\n[2/5] LLM 话题筛选 (hy3/Hunyuan, target={topic_count}篇)...")
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

    # 新闻/转会/花边文章标题：始终展示，作为非比赛话题的选题素材
    news_lines = []
    news_articles = match_data.get("news_articles", [])
    if news_articles:
        news_lines.append("\n## 📰 今日足球新闻（含转会/花边/战报，选材重要来源）")
        for art in news_articles[:20]:
            title = art.get("title", "")
            if title:
                news_lines.append(f"  - {title}")
    elif match_data.get("data_source") == "dongqiudi":
        # 懂球帝降级路径：文章在 all_fixtures 中
        news_lines.append("\n## 📰 今日懂球帝文章（选材来源）")
        for f in match_data.get("all_fixtures", [])[:20]:
            title = f.get("article_title", "")
            if title:
                news_lines.append(f"  - {title}")

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

    # Cross-day dedup: emphasize yesterday's content so LLM avoids suggesting the same match
    if yesterday_keywords:
        yesterday_sample = list(yesterday_keywords)[:20]
        history_text += "\n## 🚫 昨日已报道的赛事（严禁今日再次选择相同比赛）\n"
        history_text += "昨日关键词: " + ", ".join(sorted(yesterday_sample)) + "\n"
        history_text += "如果今日的比赛数据中包含昨日已报道的同一场比赛，必须选择其他比赛。同一场比赛连续两天报道是绝对禁止的。\n"

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

    # Post-World Cup guidance: focus on transfer news, avoid Chinese leagues
    season_guidance = ""
    if season_label == "休赛期过渡":
        season_guidance = """
## 📌 休赛期选题指引（2026世界杯已结束，新赛季未开始）
当前是欧洲杯赛休赛期，注意：
1. ✅ 夏季转会窗是最大热点 — 优先选转会相关话题
2. ✅ 球员场外花边、经典回顾、赛季前瞻正当其时
3. ✅ 中超/中甲/中乙等中国联赛可选 — 前提是懂球帝/直播吧当日有相关源文章
4. ✅ **2034杯（全国青少年足球锦标赛）** — 近期热度高，如懂球帝/直播吧有相关文章可选
5. ❌ 不要从比赛比分中"创造"话题 — 所有话题必须有对应源文章
6. ⚠️ 2034杯等青少年赛事：只能写赛事氛围、感人故事、整体趋势，**绝对不能对具体小球员做"天赋""前途""技术"等断言**
"""

    prompt = f"""你是头条号足球博主"球评人老六"。以下是 {match_data['date']} 的选题素材。

## 📰 今日懂球帝/直播吧文章（主要选题来源）
❗ 核心规则：必须从下方文章列表中选话题，不能从比赛比分中自创话题。{"".join(news_lines)}

## 📊 今日比赛结果（仅作背景参考，不是选题来源）
{"".join(lines)}

⚠️ 注意：只显示了已结束的比赛(FT/AET/PEN)。进行中的比赛显示为"vs"，不要选作选题。

{history_text}
{cross_batch_text}
{weight_hint}
{season_guidance}

📌 **内容多样性铁律（最重要规则）**：
- {topic_count} 个话题必须是 {topic_count} 个不同的事件——不能都是同一场比赛或同一转会故事
- 非比赛话题（转会传闻、球员趣事、经典回顾）优先选择——休赛期读者更爱看这些
- 如果当日素材中有转会新闻或场外话题，优先选择非比赛内容

⚠️ 去重铁律：
- 禁止2个话题围绕同一核心事件/同一核心球员/同一转会故事展开
- 举例：如果第1篇写"姆巴佩去皇马"，第2篇就不能再写"姆巴佩的薪资谈判"
- {topic_count}个话题的核心关键词集合交集必须为空
- 如果当日素材不够{topic_count}个完全不同的主题，宁可减少话题数也不要凑近似话题

风格要求：像老球迷喝酒聊天一样自然，有明确立场和情绪，不骑墙、不套模板。
避免：任何过去7天已报道过的球队/球员/话题。

⚠️ 选题时效性（必读）：
当前日期：{match_data['date']}
- 如果选题是关于"转会传闻""某球员可能离开某队""某球队有意收购"等，必须判断该事件是否已经过时。
- 例如：如果某球员2-3年前就被传转会且很可能已经完成了转会，不应再炒冷饭写"传闻该球员将转会"——这是过时信息。
- ✅ 可以从"事后复盘"角度写已完成的转会（如"XX当初加盟XX后改变了什么"），但不能写"即将转会/有望加盟"等正在进行时。
- ✅ 如果不确定该事件是否最新，写"此前有报道称"并用过去时表述。

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
    # Retry on JSON parse failure: hy3/Hunyuan occasionally returns malformed JSON
    topics = None
    for attempt in range(3):
        response = call_llm(HY3_BASE_URL, HY3_API_KEY, HY3_MODEL_FLASH, messages, temperature=0.7, max_tokens=4096,
                           fallback_url=DASHSCOPE_URL, fallback_key=DASHSCOPE_KEY, fallback_model="qwen-turbo")
        try:
            topics = safe_json_loads(response)
            break  # success
        except ValueError:
            if attempt < 2:
                print(f"   ⚠️ JSON 解析失败 (attempt {attempt+1}/3)，重试...")
                continue
            print(f"   ❌ JSON 解析失败 (3次均失败)，跳过LLM选题")
            topics = None
    if topics is None:
        return [], []
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

    # Cross-day dedup: hard filter against yesterday's keywords (overlap >= 40% → drop)
    if yesterday_keywords and topics:
        filtered = []
        for t in topics:
            t_title = t.get("title", "")[:30]
            t_kws = set(k.lower() for k in (t.get("keywords", []) or []) + (t.get("keywords_cn", []) or []))
            kw_overlap = len(t_kws & yesterday_keywords) / max(len(t_kws), 1) if t_kws else 0
            if kw_overlap >= 0.4:
                print(f"   🗑️ 跨天去重: 丢弃「{t_title}」(昨日关键词重叠率 {kw_overlap:.0%})")
            else:
                filtered.append(t)
        if len(filtered) < len(topics):
            print(f"   跨天去重: {len(topics)} → {len(filtered)} 个话题")
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


def _extract_chinese_words(text):
    """从文本中提取有意义的2+字中文词序列用于匹配。"""
    return re.findall(r'[一-鿿]{2,}', text)


def _calculate_topic_article_match(topic, art_title, article_text=""):
    """计算 topic 与 article 的匹配度。

    从 topic title/angle 中提取每个中文词，与 article title 做部分匹配。
    返回 > 0 表示有匹配，值越大匹配越强；返回 0 表示不匹配。
    """
    topic_title = topic.get("title", "") or ""
    topic_angle = topic.get("angle", "") or ""
    topic_kw = set(k.lower() for k in (topic.get("keywords", []) or []) + (topic.get("keywords_cn", []) or []))
    cn_words = set(_extract_chinese_words(topic_title + " " + topic_angle))
    all_match_words = topic_kw | cn_words
    if not all_match_words:
        return 0

    art_title_lower = art_title.lower()
    title_matches = sum(1 for w in all_match_words if len(w) >= 2 and w.lower() in art_title_lower)

    if title_matches >= 2:
        return title_matches
    if title_matches >= 1 and any(len(w) >= 4 and w.lower() in art_title_lower for w in all_match_words):
        return 1

    if article_text:
        text_lower = article_text.lower()[:500]
        text_matches = sum(1 for w in all_match_words if len(w) >= 2 and w.lower() in text_lower)
        if text_matches >= 2:
            return text_matches
    return 0


def _find_source_article(topic, match_context):
    """从 match_context 中找到与话题关联的源文章。

    按 content_type 路由：
    - "转会资讯"/"八卦趣事"：优先在 news_articles/transfer_news 中搜索，跳过比赛战报
    - 其他：先匹配比赛战报（按球队名），再匹配新闻文章

    关键词匹配使用 _calculate_topic_article_match() 提取中文词做部分匹配。
    """
    if not match_context:
        return None
    if match_context.get("data_source") not in ("zhibo8", "dongqiudi"):
        return None

    content_type = topic.get("content_type", "")
    is_news_content = content_type in ("转会资讯", "八卦趣事")

    # ── 转会资讯/八卦趣事 → 优先在新闻文章中搜索（跳过比赛战报） ──
    if is_news_content:
        # 1. 在 transfer_news 中搜索（已标记的转会文章）
        for art in match_context.get("transfer_news", []):
            article_text = art.get("article_text", "") or art.get("_content", "")
            if not article_text or len(article_text) < 100:
                continue
            art_title = art.get("title", "").lower()
            if _calculate_topic_article_match(topic, art_title, article_text) > 0:
                return {"article_text": article_text, "fixture": {
                    "source": "zhibo8", "home_team": "", "away_team": "",
                    "league": content_type, "article_text": article_text,
                    "source_images": art.get("source_images", [])}}

        # 2. 在 news_articles 中搜索
        for art in match_context.get("news_articles", []):
            article_text = art.get("article_text", "") or art.get("_content", "")
            if not article_text or len(article_text) < 100:
                continue
            art_title = art.get("title", "").lower()
            if _calculate_topic_article_match(topic, art_title, article_text) > 0:
                return {"article_text": article_text, "fixture": {
                    "source": "zhibo8", "home_team": "", "away_team": "",
                    "league": content_type, "article_text": article_text,
                    "source_images": art.get("source_images", [])}}

    # ── 先匹配比赛战报（按球队名） ──
    topic_text = (topic.get("title", "") + " " + topic.get("angle", "")).lower()
    for f in match_context.get("all_fixtures", []):
        article_text = f.get("article_text", "")
        if not article_text or len(article_text) < 100:
            continue
        home = f.get("home_team", "").lower()
        away = f.get("away_team", "").lower()
        if home and home in topic_text:
            return {"article_text": article_text, "fixture": f}
        if away and away in topic_text:
            return {"article_text": article_text, "fixture": f}

    # ── 再匹配新闻文章（按关键词，适用于无比赛日/转会/八卦类无战报匹配时） ──
    for art in match_context.get("news_articles", []):
        article_text = art.get("article_text", "") or art.get("_content", "")
        if not article_text or len(article_text) < 100:
            continue
        art_title = art.get("title", "").lower()
        if _calculate_topic_article_match(topic, art_title, article_text) > 0:
            return {"article_text": article_text, "fixture": {"source": "zhibo8",
                "home_team": "", "away_team": "", "league": topic.get("content_type", ""),
                "article_text": article_text}}

    # 再匹配懂球帝 NEWS 状态文章（按标题关键词，懂球帝降级路径）
    for f in match_context.get("all_fixtures", []):
        if f.get("status") != "NEWS":
            continue
        article_text = f.get("article_text", "")
        if not article_text or len(article_text) < 100:
            continue
        art_title = f.get("article_title", "").lower()
        topic_kw = set(k.lower() for k in (topic.get("keywords", []) or []) + (topic.get("keywords_cn", []) or []))
        if not topic_kw:
            continue
        kw_match = any(kw in art_title for kw in topic_kw if len(kw) >= 2)
        topic_title_words = set(topic.get("title", "").lower().split())
        art_title_words = set(art_title.split())
        if kw_match or len(topic_title_words & art_title_words) >= 1:
            return {"article_text": article_text, "fixture": f}

    return None


def rewrite_article(topic, match_context, index, temperature=0.5, retry_hint="", date_str=""):
    """将已核实的源文章改写为老六风格。

    输入：来自直播吧/懂球帝的记者核实报道
    输出：老六风格文章（完全相同的事实，不同的文笔）
    """
    source = _find_source_article(topic, match_context)
    if not source:
        return None

    source_text = source["article_text"]
    fixture = source["fixture"]

    content_type = topic.get("content_type", "热点球评")
    print(f"\n[3.{index}] [改写-{content_type}] {topic['title'][:40]}...")

    # 风格引导
    style_guide = {
        "热点球评": "像赛后和球友喝酒复盘——先讲最刺激的瞬间，再拆关键战术细节，最后给个不带套路的结论。",
        "转会资讯": "像球迷群里的八卦——重点是「为什么」和「影响」。有趣不编造，有逻辑不学术。",
        "八卦趣事": "聚焦一个侧面、一个瞬间、一个画面。用细节和情绪让读者有代入感。",
    }
    style = style_guide.get(content_type, "自然口语化中文写作")

    # 字数
    word_range = topic.get("_word_count_range", [500, 800])
    word_min = word_range[0]
    word_max = word_range[1] if len(word_range) > 1 else word_min + 200

    # 列信息
    column_name = topic.get("_column_name", "")
    column_block = f"\n栏目：{column_name}\n" if column_name else ""

    retry_block = ""
    if retry_hint:
        retry_block = f"\n⚠️ 上次改写失败！问题：{retry_hint}\n这次必须修正。\n"

    # 加载 prompt 模板
    prompt_template_path = os.path.join(os.path.dirname(__file__), "prompts", "rewrite_article.txt")
    base_prompt = ""
    if os.path.exists(prompt_template_path):
        with open(prompt_template_path, "r", encoding="utf-8") as f:
            base_prompt = f.read()

    if not base_prompt:
        base_prompt = f"""你是头条号足球博主"球评人老六"。今天的任务是将一篇真实的体育新闻报道改写成你的个人风格。

## 核心原则
1. 事实零改动：来源文章中的所有比分、球队名、球员名、关键事件必须完全保留。
2. 风格全换：把原文的客观新闻报道语气 → 老六的个人风格。
3. 结构重组：用自己的叙事重新组织。

输出JSON: {{{{ "title": "标题(15-25字)", "content": "Markdown正文({word_min}-{word_max}字，含≥2个##小标题)", "summary": "摘要", "keywords": [], "keywords_cn": [], "golden_lines": [], "interaction_type": "共鸣式", "interaction_bait": "互动问题", "content_type": "{content_type}" }}}}"""

    prompt = base_prompt.format(
        source_text=source_text[:3000],
        content_type=content_type,
        style=style,
        word_min=word_min,
        word_max=word_max,
        index=index,
        column_block=column_block,
        retry_block=retry_block,
    )

    messages = [
        {"role": "system", "content": "你是一个足球文章改写助手。你必须保留所有事实（比分、球员、事件），只改变文风和叙述角度。"},
        {"role": "user", "content": prompt},
    ]

    response = call_llm(HY3_BASE_URL, HY3_API_KEY, HY3_MODEL_FLASH, messages,
                        temperature=temperature, max_tokens=8192,
                        fallback_url=DASHSCOPE_URL, fallback_key=DASHSCOPE_KEY, fallback_model="qwen-turbo")
    article = safe_json_loads(response)

    if article and isinstance(article, dict):
        article["content_type"] = content_type
        article["_source_fixture"] = fixture
        # Inject column metadata
        column_name = topic.get("_column_name", "")
        if column_name:
            article["_column_name"] = column_name
            article["_batch_name"] = topic.get("_batch_name", "")
        print(f"   改写完成: {article.get('title','?')}, {len(article.get('content',''))}字")
    return article


def check_rewrite_fidelity(source_fixture, rewritten_article):
    """检查改写文是否忠实于来源文章。

    对比关键事实（比分、球员名、球队名）是否被改动。
    返回 (passed, issues)。
    """
    issues = []
    content = rewritten_article.get("content", "") + rewritten_article.get("title", "")
    source_text = source_fixture.get("article_text", "")

    # 检查1：比分一致
    # 注意：用 any() 而非 all()，因为内容中可能包含日期"2026-07-03"等
    # 会被 (\d+)[:-](\d+) 误匹配为假比分。只要正确比分出现一次即通过。
    expected_hg = source_fixture.get("home_score")
    expected_ag = source_fixture.get("away_score")
    if expected_hg is not None and expected_ag is not None:
        found_scores = re.findall(r'(\d+)[:-](\d+)', content)
        if found_scores:
            score_ok = any(
                (int(a) == expected_hg and int(b) == expected_ag) or
                (int(a) == expected_ag and int(b) == expected_hg)
                for a, b in found_scores
            )
            if not score_ok:
                issues.append(f"比分不一致: 来源 {expected_hg}-{expected_ag}")

    # 检查2：源文章中的球员名出现在改写文中
    source_goals = source_fixture.get("goals", [])
    for g in source_goals:
        scorer = g.get("scorer_name", g.get("scorer", ""))
        if scorer and scorer not in content:
            if scorer in source_text:
                issues.append(f"缺少球员: {scorer}")

    # 检查3：禁止新增断言表达，但需对照结构化进球数据做语义判断
    # 先用 goals[] 数据统计每个球员的进球数，用于验证"梅开二度""帽子戏法"
    from collections import defaultdict
    goals_by_scorer = defaultdict(int)
    for g in source_goals:
        name = g.get("scorer_name", g.get("scorer", ""))
        if name:
            goals_by_scorer[name] += 1
    has_double = any(c == 2 for c in goals_by_scorer.values())
    has_hattrick = any(c >= 3 for c in goals_by_scorer.values())

    banned_patterns = [
        (r'帽子戏法', '新增编造: 帽子戏法'),
        (r'梅开二度', '新增编造: 梅开二度'),
        (r'独造\d+球', '新增编造: 独造X球'),
        (r'第\d+分钟', '新增编造: 具体时间'),
    ]
    for pattern, desc in banned_patterns:
        if not re.search(pattern, content):
            continue
        # 通过结构化进球数据验证语义正确性，而非死板比对文章字面
        if pattern == r'梅开二度' and has_double:
            continue  # 球员确实进了2球，语义正确
        if pattern == r'帽子戏法' and has_hattrick:
            continue  # 球员确实进了3+球，语义正确
        # 退回到字面比对：仅当 source_text 也没有时才判定为编造
        if re.search(pattern, source_text):
            continue  # 源文章本身用了这个词，没问题
        issues.append(desc)

    return len(issues) == 0, issues


def validate_article_vs_match_data(source_fixture, rewritten_article):
    """验证改写文中的关键比赛信息是否与结构化比赛数据一致。

    与 check_rewrite_fidelity 不同，此函数直接对比比赛数据（而非源文章），
    可检测 LLM 编造的、源文章中也不存在的虚假细节。
    返回 (passed, issues)。
    """
    issues = []
    content = rewritten_article.get("content", "") + rewritten_article.get("title", "")

    ht = source_fixture.get("home_team", "")
    at = source_fixture.get("away_team", "")
    hg = source_fixture.get("home_score")
    ag = source_fixture.get("away_score")

    # 检查1：主客队名出现在文中
    if ht and ht not in content:
        issues.append(f"缺少主队名: {ht}")
    if at and at not in content:
        issues.append(f"缺少客队名: {at}")

    # 检查2：比分一致性（加强版 regex，排除假匹配）
    if hg is not None and ag is not None:
        score_found = False
        for m in re.finditer(r'(\d+)\s*[-–:]\s*(\d+)', content):
            a, b = int(m.group(1)), int(m.group(2))
            if (a == hg and b == ag) or (a == ag and b == hg):
                score_found = True
                break
        if not score_found:
            # 宽松检查：检查是否有单数字比分表示
            if f"{hg}-{ag}" not in content.replace(" ", "").replace(" ", ""):
                issues.append(f"比分不一致: 比赛数据 {ht} {hg}-{ag} {at}，但文中未出现该比分")

    # 检查3：结构化进球数据的球员断言验证
    from collections import defaultdict
    goals = source_fixture.get("goals", [])
    goals_by_scorer = defaultdict(int)
    for g in goals:
        name = g.get("scorer_name", g.get("scorer", ""))
        if name:
            goals_by_scorer[name] += 1

    # 3a: 如果进球数据明确，检查禁区断言
    if goals_by_scorer:
        # LLM 常用但易编造的球员表现断言模式
        # 验证策略：断言级别短语必须在 goals 数据中有对应依据
        for player, goal_count in goals_by_scorer.items():
            # 检查"梅开二度"：需要进2球
            if re.search(rf'{re.escape(player)}.*?梅开二度', content):
                if goal_count < 2:
                    issues.append(f"编造数据: {player} 实际进{goal_count}球，但文中称'梅开二度'")
            # 检查"帽子戏法"：需要进3+球
            if re.search(rf'{re.escape(player)}.*?帽子戏法', content):
                if goal_count < 3:
                    issues.append(f"编造数据: {player} 实际进{goal_count}球，但文中称'帽子戏法'")
            # 检查"独造X球"：通常指进球+助攻≥X
            dm = re.search(rf'{re.escape(player)}.*?独造(\d+)球', content)
            if dm:
                claimed = int(dm.group(1))
                # 保守估计：如果没有助攻数据，只算进球
                if goal_count < claimed:
                    issues.append(f"编造数据: {player} 实际进{goal_count}球，但文中称'独造{claimed}球'")

    return len(issues) == 0, issues


# ============================================================
# Quality Validation & Retry
# ============================================================

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


def _rewrite_with_retry(topic, match_context, index, source, max_retries, date_str):
    """Rewrite a verified source article with retry on fidelity failure.

    The rewrite path is simpler than standard generation because:
    - No need for hallucination detection (facts come from verified source)
    - No need for LLM fact-checking (fidelity check is regex-based)
    - Requires fewer retries (the task is easier)
    """
    fixture = source["fixture"]
    last_hint = ""

    for attempt in range(max_retries + 1):
        temp = max(0.3, 0.5 - attempt * 0.1)
        try:
            art = rewrite_article(topic, match_context, index, temperature=temp,
                                  retry_hint=last_hint, date_str=date_str or "")
            if not art or not isinstance(art, dict):
                last_hint = "改写返回空结果，请确保输出完整的JSON"
                continue

            # Basic content check
            content = art.get("content", "")
            word_range = topic.get("_word_count_range", [500, 800])
            min_words = word_range[0] if isinstance(word_range, (list, tuple)) else 500
            if len(content) < max(200, int(min_words * 0.6)):
                last_hint = f"正文仅{len(content)}字，需要至少{min_words}字"
                continue

            # Fidelity check: verify facts preserved
            passed, issues = check_rewrite_fidelity(fixture, art)
            if not passed:
                last_hint = "; ".join(issues)
                if attempt < max_retries:
                    continue
                return {}, f"改写不忠实: {last_hint}"

            # 第二层验证：防止改写文编造比赛数据中不存在的事件
            match_passed, match_issues = validate_article_vs_match_data(fixture, art)
            if not match_passed:
                last_hint = "; ".join(match_issues)
                if attempt < max_retries:
                    continue
                return {}, f"事实验证失败: {last_hint}"

            return art, None

        except Exception as e:
            last_hint = f"异常: {e}"
            if attempt >= max_retries:
                return {}, f"改写异常: {e}"

    return {}, "改写失败"


def generate_article_with_retry(topic, match_context, index, max_retries=2, date_str=None):
    """改写路径：从直播吧/懂球帝源文章改写为老六风格（Pipeline A）。"""
    if not match_context or match_context.get("data_source") not in ("zhibo8", "dongqiudi"):
        return {}, "Pipeline A 不可用：数据源非直播吧/懂球帝"

    source = _find_source_article(topic, match_context)
    if not source:
        # 如果没匹配到战报，尝试去 news_articles / transfer_news 中懒加载正文
        all_news = list(match_context.get("news_articles", [])) + \
                   list(match_context.get("transfer_news", []))
        if all_news:
            topic_text = (topic.get("title", "") + " " + topic.get("angle", "")).lower()
            topic_kw = set(k.lower() for k in (topic.get("keywords", []) or []) + (topic.get("keywords_cn", []) or []))
            for art in all_news:
                art_title = art.get("title", "").lower()
                # 检查标题是否包含话题关键词
                kw_match = any(kw in art_title for kw in topic_kw) if topic_kw else False
                title_word_match = any(word in topic_text for word in art_title.split())
                if kw_match or title_word_match or len(topic_kw) == 0:
                    # 懒加载正文
                    article_text = art.get("article_text", "")
                    if not article_text or len(article_text) < 100:
                        from media_scraper import SportsScraper
                        try:
                            scraper = SportsScraper()
                            article_text = scraper.scrape_zhibo8_article_content(art.get("url", ""))
                        except Exception:
                            article_text = ""
                    if article_text and len(article_text) >= 100:
                        source = {"article_text": article_text, "fixture": {
                            "source": "zhibo8", "home_team": "", "away_team": "",
                            "league": topic.get("content_type", ""),
                            "article_text": article_text, "source_images": []}}
                        break

    if not source:
        return {}, f"Pipeline A：未找到与话题「{topic.get('title','')[:20]}」匹配的源文章"

    return _rewrite_with_retry(topic, match_context, index, source,
                               max_retries, date_str)


# ============================================================
# Hupu Data Collection & Article Generation
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

        # ————————————————————————————————————————————
        # 内容互动增强（不改动事实，只优化表达）
        # ————————————————————————————————————————————

        # 1. 标题优选：从 title + backup_title 中选点击率更高的
        title = art.get("title", "")
        backup = art.get("backup_title", "")
        if title and backup and backup != title:
            def _title_score(t):
                s = 0
                if re.search(r'\d', t): s += 3           # 含数字 → 具体
                for team in ("巴西", "阿根廷", "葡萄牙", "西班牙", "英格兰", "法国", "德国",
                             "荷兰", "意大利", "比利时", "梅西", "C罗", "姆巴佩", "哈兰德",
                             "内马尔", "贝林厄姆", "凯恩", "萨拉赫"):
                    if team in t: s += 2                  # 含巨星/豪门
                if any(w in t for w in ("?", "！", "…")): s += 2   # 情绪符号
                if ":" in t or "：" in t: s += 1           # 解释结构
                if len(t) < 12: s -= 2                    # 太短 → 信息不足
                if any(w in t for w in ("老六", "小编", "我们")): s -= 1  # 自指 → 弱
                return s
            score_t, score_b = _title_score(title), _title_score(backup)
            if score_b > score_t:
                art["title"] = backup
                art["original_title"] = title
                print(f"   📝 标题优选: 「{title}」({score_t}分) → 「{backup}」({score_b}分)")
            elif title != backup:
                print(f"   📝 标题优选: 「{title}」({score_t}分) 保持 (备选「{backup}」{score_b}分)")

        # 2. 金句高亮：在正文中标记 golden_lines
        golden = art.get("golden_lines", [])
        if golden and isinstance(golden, list):
            content = art.get("content", "")
            # 只在正文中确实出现了的金句才做高亮
            for g in golden:
                g_clean = g.strip().strip('"').strip('"').strip("'")
                if g_clean and len(g_clean) > 8 and g_clean in content:
                    # 加粗+引号包裹，让它更显眼
                    content = content.replace(g_clean, f"**「{g_clean}」**", 1)
            # 如果有2+金句，文末加一个金句回顾框（类似"🎙️ 老六金句"）
            valid_golden = [g for g in golden if len(g.strip().strip('"')) > 8]
            if len(valid_golden) >= 2 and "老六金句" not in content:
                golden_block = "\n\n---\n🎙️ **老六金句**\n"
                for g in valid_golden[:3]:
                    gd = g.strip().strip('"')
                    golden_block += f"> *{gd}*\n\n"
                content += golden_block
            art["content"] = content

        # 3. 互动钩子注入：在文末追加 interaction_bait
        bait = art.get("interaction_bait", "")
        if bait and len(bait) > 5 and bait not in art.get("content", ""):
            bait_clean = bait.strip().strip('"').strip('"')
            # 根据 interaction_type 添加不同的前缀表情
            i_type = art.get("interaction_type", "")
            prefix_map = {
                "站队式": "🗣️ 说说你的看法",
                "投票式": "📊 来投个票",
                "预测式": "🔮 你的预测是",
                "共鸣式": "💬 有没有同感的",
                "挑战式": "🤔 不服来辩",
                "调侃式": "😏 你们说呢",
                "": "💬 各位老铁",
            }
            prefix = prefix_map.get(i_type, "💬")
            art["content"] = f"{art.get('content', '')}\n\n---\n**{prefix}：{bait_clean}**\n👇 评论区见分晓！"
            print(f"   🎣 互动钩子: [{i_type}] {bait_clean[:40]}")

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
    response = call_llm(HY3_BASE_URL, HY3_API_KEY, HY3_MODEL_FLASH, messages, temperature=temperature, max_tokens=4096,
                        fallback_url=DASHSCOPE_URL, fallback_key=DASHSCOPE_KEY, fallback_model="qwen-turbo")
    article = safe_json_loads(response)
    print(f"   紧急球评标题: {article.get('title','?')}, 正文: {len(article.get('content',''))}字")
    return article


# ============================================================
# Main
# ============================================================

def _generate_articles_from_topics(topics, count, match_data, images_map, stats,
                                    articles_out, date_str=None):
    """Pipeline A：对每个话题从直播吧/懂球帝源文章改写为老六风格。

    配图优先级：① 源文章战报图片 → ② Unsplash/Wikipedia 搜索。
    """
    for i, topic in enumerate(topics[:count]):
        ct = topic.get("content_type", "N/A")
        print(f"\n--- 第{i+1}/{count}篇 [{ct}] ---")

        # 优先用源文章的配图（比赛相关，不重复）
        source_imgs = []
        if match_data and match_data.get("data_source") in ("zhibo8", "dongqiudi"):
            source = _find_source_article(topic, match_data)
            if source and source.get("fixture", {}).get("source_images"):
                source_imgs = source["fixture"]["source_images"][:3]
                print(f"   📷 使用源文章配图: {len(source_imgs)} 张")

        if source_imgs:
            imgs = source_imgs
        else:
            imgs = search_images(topic, count=5)

        images_map[i] = imgs
        art, error = generate_article_with_retry(topic, match_data, i + 1,
                                                  max_retries=2, date_str=date_str)
        stats["generated"] += 1
        if error:
            print(f"   ❌ 最终失败: {error}")
            stats["failed"] += 1
            stats["issues"].append(f"第{i+1}篇({ct}): {error}")
        else:
            stats["valid"] += 1
            articles_out.append((i, art))


# ============================================================
# Prediction Article — 赛前预测
# ============================================================

def generate_prediction_article(future_matches, date_str=None):
    """根据未来比赛数据生成一篇赛前预测文章。

    用 LLM 对每场明日比赛做 2-3 句分析 + 预测结果，
    文末带互动引导："评论区下注，明天赛后回来打我脸！"

    Args:
        future_matches: list[dict]，由 collect_future_matches 返回
        date_str: 当前日期 YYYY-MM-DD（用于配图搜索）

    Returns:
        dict or None: 文章 dict（含 title, content, content_type 等），
                     或 None（失败时）
    """
    if not future_matches:
        return None

    print(f"\n[预测] 生成赛前预测文章 ({len(future_matches)} 场)...")

    match_lines = []
    for i, m in enumerate(future_matches, 1):
        league = m.get("league", "未知赛事")
        home = m.get("home_team", "?")
        away = m.get("away_team", "?")
        utc = m.get("utc_date", "")
        match_lines.append(f"{i}. [{league}] {home} vs {away} {'(' + utc + ')' if utc else ''}")

    matches_text = "\n".join(match_lines)
    max_matches = min(len(future_matches), 8)

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝，以犀利预测和毒舌分析著称。

你的任务是写一篇"明日赛程预测"——分析明天的足球比赛，给出你的预测结果。
今天是 {date_str or '今日'}。

以下是明天的赛程（{len(future_matches)} 场），请选择最有话题性的 {max_matches} 场进行分析：

{matches_text}

写作要求：
1. 标题如"老六精准预测：明天XX对XX，我看好..."
2. 为每场选中的比赛写 2-3 句话分析，给出明确预测结果（XX胜/平局/谁赢面大）
3. 语气要自信但不狂妄，像老球迷在群里吹水
4. 文末带互动引导：🔥 评论区下注，明天赛后回来打我脸！

风格：自信、犀利、有数据感但不堆砌。不编造球员具体数据。
没有确切数据就说"老六觉得""从近期表现来看"。

输出纯JSON:
{{"title": "标题(18-30字)", "content": "Markdown正文(含##小标题，600-900字)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "预测式", "interaction_bait": "互动问题，如'明天最看好哪场？评论区下注！'", "content_type": "热点球评"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是头条号足球博主'球评人老六'，以犀利预测和毒舌分析著称。风格自信、有数据感。不编造球员级别数据。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]

    for attempt in range(3):
        try:
            response = call_llm(HY3_BASE_URL, HY3_API_KEY, HY3_MODEL_FLASH,
                                messages, temperature=0.7, max_tokens=4096,
                                fallback_url=DASHSCOPE_URL, fallback_key=DASHSCOPE_KEY, fallback_model="qwen-turbo")
            article = safe_json_loads(response)
            if not isinstance(article, dict) or not article.get("title"):
                if attempt < 2:
                    print(f"   ⚠️ 预测文章解析失败 (attempt {attempt+1}/3)，重试...")
                    continue
                print(f"   ❌ 预测文章解析失败 (3次均失败)")
                return None

            content = article.get("content", "")
            if len(content) < 200:
                if attempt < 2:
                    print(f"   ⚠️ 预测文章正文仅{len(content)}字 (attempt {attempt+1}/3)")
                    continue
                return None

            article["content_type"] = "热点球评"
            article["interaction_type"] = article.get("interaction_type", "预测式")
            article["_is_prediction"] = True
            print(f"   ✅ 预测文章生成成功: {article['title'][:50]} ({len(content)}字)")
            return article

        except Exception as e:
            if attempt < 2:
                print(f"   ⚠️ 预测文章生成异常 (attempt {attempt+1}/3): {e}")
                continue
            print(f"   ❌ 预测文章生成失败: {e}")
            return None


def main():
    # Parse args: python orchestrator.py [YYYY-MM-DD] [--batch=morning|noon|evening]
    date_str = None
    batch_mode = "auto"
    for arg in sys.argv[1:]:
        if arg.startswith("--batch="):
            batch_mode = arg.split("=", 1)[1]
        elif not arg.startswith("--"):
            date_str = arg
    if date_str is None:
        date_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

    # Load season weights for content type optimization
    season_weights, season_label = load_season_weights(date_str)

    if batch_mode in BATCH_CONFIG:
        batch_cfg = BATCH_CONFIG[batch_mode]
        slots = batch_cfg["slots"]
        article_count = len(slots)
        # Columns are fixed per batch — no season weight type swapping
        # Season weights only affect topic selection framing, not column identity
        column_names = [s["column_name"] for s in slots]
        print(f"足球自媒体内容自动化 - {date_str} (batch={batch_mode}, 栏目={', '.join(column_names)}, {batch_cfg['name']}·{batch_cfg['time']})\n")
        target_types = None  # Column-driven, not type-driven
    start_time = time.time()
    log.info(f"开始执行 — 日期:{date_str} 批次:{batch_mode}")
    success = False
    result_msg = ""
    stats = {"generated": 0, "valid": 0, "failed": 0, "issues": []}
    extra_meta = {}

    try:
        # Step 0: Load topic history for dedup
        topic_history = get_topic_history(date_str)
        # Cross-batch dedup: check what earlier batches already published today
        cross_batch_covered = get_cross_batch_covered(date_str)
        # Cross-day dedup: get yesterday's keywords for hard filter
        yesterday_keywords = get_yesterday_keywords(date_str)

        # Step 1: Collect match data (always, for context)
        match_data = collect_real_matches(date_str)

        # Step 1b: Collect transfer/gossip news for content diversity
        try:
            transfer_news = collect_transfer_news(date_str)
            if transfer_news:
                existing_news = match_data.get("news_articles", [])
                # Merge, dedup by title
                existing_titles = {a.get("title", "") for a in existing_news}
                for tn in transfer_news:
                    if tn.get("title", "") not in existing_titles:
                        existing_news.append(tn)
                        existing_titles.add(tn.get("title", ""))
                if "news_articles" not in match_data:
                    match_data["news_articles"] = existing_news
        except Exception as e:
            print(f"   ⚠️ 转会新闻采集异常 (不影响主流程): {e}")

        articles = []
        images_map = {}
        topics = []

        # ============================================================
        # Main Article Pipeline — Pipeline A only (source article rewrite)
        # ============================================================

        # Validate we have media source articles for rewriting
        has_matches = match_data.get("total_matches", 0) > 0
        has_news_articles = bool(match_data.get("news_articles"))
        if match_data.get("data_source") not in ("zhibo8", "dongqiudi"):
            result_msg = f"无直播吧/懂球帝源文章，Pipeline B已禁用 (data_source={match_data.get('data_source')})"
            print(f"   ❌ {result_msg}")
            send_wxpusher("足球自媒体 ⚠️", f"{date_str} 发文任务中止：{result_msg}")
            return

        # Topic selection via LLM (based on match data + column domain guidance)
        topics = select_topics(match_data, topic_history=topic_history,
                               preferred_types=target_types,
                               season_weights=season_weights,
                               cross_batch_covered=cross_batch_covered,
                               season_label=season_label,
                               topic_count=article_count,
                               yesterday_keywords=yesterday_keywords)
        extra_meta = {"type": "match_analysis"}
        _assign_columns_to_topics(topics, batch_mode)

        # Generate articles: find source article for each topic, rewrite to 老六 style
        _generate_articles_from_topics(topics, article_count, match_data,
                                       images_map, stats, articles, date_str=date_str)

        # ============================================================
        # Prediction Article — 晚间批次生成明日赛前预测
        # ============================================================
        if batch_mode == "evening":
            print("\n[预测] 晚间批次：采集明日赛程，生成赛前预测...")
            future_matches = collect_future_matches(date_str, days_ahead=1)
            if future_matches:
                pred_art = generate_prediction_article(future_matches, date_str=date_str)
                if pred_art:
                    p_idx = len(articles) + 1
                    # 为预测文章搜索配图
                    league_names = list(set(m.get("league", "足球") for m in future_matches if m.get("league")))
                    team_names = []
                    for m in future_matches[:6]:
                        team_names.append(m.get("home_team", ""))
                        team_names.append(m.get("away_team", ""))
                    img_topic = {"title": pred_art.get("title", "明日足球预测"),
                                 "keywords_cn": league_names[:3] + team_names[:4],
                                 "keywords": ["football", "prediction"] + [t for t in team_names[:4] if t]}
                    p_imgs = search_images(img_topic, count=3)
                    images_map[len(articles)] = p_imgs
                    stats["generated"] += 1
                    stats["valid"] += 1
                    articles.append((len(articles), pred_art))
                    topics.append({"title": pred_art.get("title", ""),
                                   "content_type": "热点球评",
                                   "_is_prediction": True})
                    print(f"   🎯 赛前预测已追加: {pred_art['title'][:50]}")
                else:
                    print("   ℹ️ 赛前预测生成跳过（无有效素材或生成失败）")
            else:
                print("   ℹ️ 明日无赛程，跳过赛前预测")

        # ============================================================
        # Hupu Pipeline (articles 4-6, top 3 hottest posts)
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
            result_msg = "未能生成任何文章（所有话题改写失败）"
            print(f"ERROR: {result_msg}")
            # 保存空批次元数据 — 避免同批次其他 cron 触发点重复重试
            save_batch_state(date_str, batch_mode if batch_mode != "auto" else "full", [])
            send_wxpusher("足球自媒体 ⚠️", f"{date_str} 发文任务中止：{result_msg}")
            return

        articles_sorted = [a for _, a in sorted(articles, key=lambda x: x[0])]
        result = save_articles_local(date_str, articles_sorted, images_map, topics, match_data,
                                     extra=extra_meta)

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
