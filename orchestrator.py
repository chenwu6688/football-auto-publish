#!/usr/bin/env python3
"""足球自媒体 - 文章生成编排器 (独立版，无 Flask 依赖)

Usage: python orchestrator.py [YYYY-MM-DD]
"""

import os, json, sys, subprocess, requests, time, re, signal, threading, yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
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
                       BATCH_CONFIG, LLM_JSON_CANDIDATES)
from utils import (retry, call_llm, safe_json_loads, load_prompt_template,
                   call_llm_json, QuotaExhaustedError)
from logger import log
import risk_gate
import degrade
import supply_mix
import rumor_wording
import title_antipattern
import title_positive
import interaction_bait
import topic_category
import content_reference
import template_gate
from data_collector import (collect_real_matches, collect_transfer_news, collect_future_matches,
                             search_images, search_wikipedia, search_footyrenders,
                             extract_search_entities, get_topic_history,
                             build_match_signature, is_event_duplicate,
                             collect_fixture_topics, gather_fixture_facts,
                             collect_recap_topics,
                             TEAM_ALIASES)


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


# ============================================================
# 品牌手册（单一事实源）加载与渲染
# 所有"球评人老六"的人设/口吻/立场/红线/受众/栏目/钩子/配比集中维护在
# config/brand_manual.yaml，本模块读取后渲染成文本，注入到选题与改写 prompt，
# 根治"AI 偷懒/无魂"——人设定义只改一处即可全局生效。
# ============================================================
_BRAND_MANUAL_DATA = None  # 缓存原始 dict，避免每次调用重复读盘


def _get_brand_manual_data():
    """读取 config/brand_manual.yaml，返回 dict（带模块级缓存）。文件缺失/解析失败返回 {}。"""
    global _BRAND_MANUAL_DATA
    if _BRAND_MANUAL_DATA is not None:
        return _BRAND_MANUAL_DATA
    path = PROJECT_ROOT / "config" / "brand_manual.yaml"
    data = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                data = {}
        except Exception as e:
            log.warning(f"品牌手册加载失败，跳过注入: {e}")
            data = {}
    _BRAND_MANUAL_DATA = data
    return data


def _render_brand_manual_node(node, indent):
    """递归渲染品牌手册节点为可读文本（LLM 上下文友好）。"""
    pad = "  " * indent
    lines = []
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}- {k}：")
                lines.extend(_render_brand_manual_node(v, indent + 1))
            else:
                lines.append(f"{pad}- {k}：{v}")
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                lines.extend(_render_brand_manual_node(item, indent))
            else:
                lines.append(f"{pad}- {item}")
    else:
        lines.append(f"{pad}{node}")
    return lines


def _render_brand_manual(data):
    """将品牌手册 dict 渲染为带分节标题的文本块。"""
    out = []
    for section, body in data.items():
        title = section.replace("_", " ").strip()
        out.append(f"### {title}")
        out.extend(_render_brand_manual_node(body, 1))
        out.append("")
    return "\n".join(out).strip()


def load_brand_manual():
    """返回渲染后的品牌手册文本块；无内容时返回空串（调用方据此决定是否注入）。"""
    data = _get_brand_manual_data()
    if not data:
        return ""
    return _render_brand_manual(data)


def get_brand_style_guide():
    """从品牌手册的 columns 派生 style_guide（content_type → 风格引导），
    保留三大核心类型的兜底文案，新增栏目自动纳入。"""
    defaults = {
        "热点球评": "像赛后和球友喝酒复盘——先讲最刺激的瞬间，再拆关键战术细节，最后给个不带套路的结论。",
        "转会资讯": "像球迷群里的八卦——重点是「为什么」和「影响」。有趣不编造，有逻辑不学术。",
        "八卦趣事": "聚焦一个侧面、一个瞬间、一个画面。用细节和情绪让读者有代入感。",
    }
    data = _get_brand_manual_data()
    columns = data.get("columns", {}) if isinstance(data, dict) else {}
    style_guide = dict(defaults)
    if isinstance(columns, dict):
        for ct, c in columns.items():
            if isinstance(c, dict) and c.get("style"):
                style_guide[ct] = c["style"]
    return style_guide


def get_brand_series():
    """从品牌手册的 series_playbook 派生 (id->series_dict, max_per_batch)。

    用于维度2 连载引擎：改写环节按 series_id 查系列名/主题注入追更提示，
    监控环节校验同批同系列不超过上限。
    """
    data = _get_brand_manual_data()
    sp = data.get("series_playbook", {}) if isinstance(data, dict) else {}
    if not isinstance(sp, dict):
        sp = {}
    series = sp.get("series", []) if isinstance(sp, dict) else []
    max_per = sp.get("max_per_batch", 1)
    by_id = {}
    if isinstance(series, list):
        for s in series:
            if isinstance(s, dict) and s.get("id"):
                by_id[s["id"]] = s
    return by_id, max_per


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


def _resolve_batch_quota(batch_mode, date_str):
    """解析本批篇数上限（策略手册第五章 / 计划 12.1）。

    常态每批 2 篇；仅周四或 S 级事件可加发（加发原因可用环境变量
    FOOTBALL_UPLIFT_REASON 显式指定，取值须落在白名单内）。
    非阻断（计划九）：解析异常一律回落到常态上限，绝不中断发布。
    """
    fallback = {"batch": batch_mode, "max_articles": 2, "uplifted": False,
                "reason": None, "requested_reason": None, "normal_max": 2,
                "uplift_max": 2, "weekly_limit": 0, "weekly_used": 0,
                "detail": "常态 2 篇（篇数纪律不可用，按保守值继续）", "source": ""}
    try:
        import batch_quota
        reason = (os.environ.get("FOOTBALL_UPLIFT_REASON") or "").strip() or None
        try:
            weekday = datetime.strptime(date_str, "%Y-%m-%d").weekday()
        except Exception:
            weekday = None
        return batch_quota.decide(
            batch_mode, weekday=weekday, uplift_reason=reason,
            weekly_uplifted=batch_quota.weekly_uplifted(date_str))
    except Exception as e:
        print(f"   ⚠️ 篇数纪律解析异常（按常态 {fallback['max_articles']} 篇继续）: {e}")
        return fallback


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
               "keywords": set(), "titles": set(), "batch_count": 0,
               "event_signatures": set(), "title_prefixes": set()}
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
                covered["title_prefixes"].add(title[:6])
                sig = build_match_signature(title)
                if sig:
                    covered["event_signatures"].add(sig)
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

    n_slots = len(slots)
    for i, topic in enumerate(topics):
        # 动态条数可能超过固定栏目数：循环复用栏目，避免越界导致后续话题缺失栏目元数据
        slot = slots[i % n_slots]
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
    print(f"\n[2/5] LLM 话题筛选 (TokenHub 多模型轮换, target={topic_count}篇)...")
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

    # Season-aware guidance: 不同赛季节奏给 LLM 不同的选题侧重
    season_guidance = ""
    if season_label == "休赛期过渡":
        season_guidance = """
## 📌 休赛期选题指引（新赛季未开始）
当前是赛程空窗期，注意：
1. ✅ 夏季/冬季转会窗是最大热点 — 优先选转会相关话题
2. ✅ 球员场外花边、经典回顾、赛季前瞻正当其时
3. ✅ 中超/中甲/中乙等中国联赛可选 — 前提是懂球帝/直播吧当日有相关源文章
4. ✅ **2034杯（全国青少年足球锦标赛）** — 近期热度高，如懂球帝/直播吧有相关文章可选
5. ❌ 不要从比赛比分中"创造"话题 — 所有话题必须有对应源文章
6. ⚠️ 2034杯等青少年赛事：只能写赛事氛围、感人故事、整体趋势，**绝对不能对具体小球员做"天赋""前途""技术"等断言**
"""
    elif season_label == "新赛季进行期":
        season_guidance = """
## 📌 新赛季选题指引（五大联赛 + 欧联/欧冠进行中）
当前新赛季已开打，内容应多维覆盖，避免单一维度堆砌：
1. ✅ 赛程赛况：当日/近期比赛结果、积分榜变化、争冠/保级/出线形势
2. ✅ 转会动态：关窗前后签约、租借、续约、解约（结合夏窗/冬窗节点）
3. ✅ 球员八卦：场外花边、伤病、更衣室、个人生活
4. ✅ 比赛分析：战术解读、数据复盘、关键球员表现与趋势
5. ⚠️ 同一场比赛不要既写"赛果"又写"分析"两篇雷同稿——换角度或换比赛
6. ❌ 不要从比赛比分中"创造"话题——所有话题必须有对应源文章
"""

    # Season-aware diversity hint（注入到"内容多样性铁律"中，替换原先写死的休赛期文案）
    if season_label == "休赛期过渡":
        season_focus_text = "- 非比赛话题（转会传闻、球员趣事、经典回顾）优先选择——休赛期读者更爱看这些"
    elif season_label == "新赛季进行期":
        season_focus_text = ("- 新赛季进行中：多维覆盖赛程赛况、转会动态、球员八卦、比赛分析（战术/数据），"
                             "避免只写单一维度；有真实比赛的优先写赛果与看点，无比赛的用转会/八卦/前瞻填充")
    else:
        season_focus_text = ("- 比赛话题与非比赛话题按当日素材质量自然配比，优先选最具话题性的事件；"
                             "转会/八卦类素材丰富时适当提高其占比")

    # P1-6 内容类型再平衡指引（赛季感知，动态注入，非阻断由 warn_type_balance 监控）
    type_balance_hint = ""
    if season_label == "新赛季进行期":
        type_balance_hint = """
## 📌 内容类型再平衡（P1-6）
新赛季进行期：热点球评设下限（≥40%）、转会资讯+八卦趣事设上限（合计 ≤50%），
避免场外话题霸屏；同一批 ≥3 篇至少覆盖 2 个不同品类。
"""
    elif season_label == "休赛期过渡":
        type_balance_hint = """
## 📌 内容类型再平衡（P1-6）
休赛期：转会资讯+八卦趣事为主力，热点球评为辅；不要硬凑比赛稿。
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
{type_balance_hint}

📌 **内容多样性铁律（最重要规则）**：
- {topic_count} 个话题必须是 {topic_count} 个不同的事件——不能都是同一场比赛或同一转会故事
{season_focus_text}
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
[{{"title": "标题(15-25字)", "angle": "切入角度+明确态度", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "content_type": "热点球评/转会资讯/排行榜/八卦趣事/战术解析", "score": 90, "controversy_level": "high/medium/low", "target_emotion": "愤怒/骄傲/怀旧/震惊/感动/好奇", "resonance_angle": "国足情结/老球迷身份认同/世界杯经典时刻/名帅名宿沉浮/无", "series_id": "guozu-chronicle/wc-classics/old-fan-night/无", "why_pick": "为什么选这个角度(20字)"}}]
只输出JSON。"""

    topic_selector_prompt = load_prompt_template("topic_selector.txt")
    if not topic_selector_prompt:
        topic_selector_prompt = "你是头条号足球博主'球评人老六'，有态度、有人味、不骑墙。严格按要求分配内容类型，避开历史话题。只输出JSON。"

    # 维度1：品牌手册单一事实源——注入人设/口吻/立场/红线/栏目/钩子/配比
    brand_manual_block = load_brand_manual()
    if brand_manual_block:
        topic_selector_prompt = (
            topic_selector_prompt
            + "\n\n## 品牌手册（球评人老六 · 单一事实源，选题须遵循）\n"
            + brand_manual_block
        )

    messages = [
        {"role": "system", "content": topic_selector_prompt},
        {"role": "user", "content": prompt}
    ]
    # Multi-model rotation on JSON parse failure / empty response.
    # Default order = constants.LLM_JSON_CANDIDATES（deepseek > kimi > hy/mimo/step > glm）
    try:
        topics, _model_used = call_llm_json(messages, LLM_JSON_CANDIDATES, temperature=0.7, max_tokens=2048)
    except ValueError as e:
        print(f"   ❌ 所有 LLM 候选均未能返回可用 JSON: {e}")
        print("   跳过LLM选题")
        return []
    if topics and isinstance(topics, dict) and "title" in topics:
        topics = [topics]  # LLM returned single object instead of array
    if not isinstance(topics, list):
        topics = []
    # R11 选题分类（类别优先级前置）：判定不了的一律「未分类」，绝不猜
    for _t in topics:
        if isinstance(_t, dict):
            _t["_category"] = topic_category.classify(_t)
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
        cross_sigs = cross_batch_covered.get("event_signatures", set())
        filtered = []
        for t in topics:
            t_title = t.get("title", "")[:30]
            t_kws = set(k.lower() for k in (t.get("keywords", []) or []) + (t.get("keywords_cn", []) or []))
            title_overlap = t_title in cross_titles
            kw_overlap = len(t_kws & cross_kw) / max(len(t_kws), 1) if t_kws else 0
            if title_overlap or kw_overlap >= 0.4:
                print(f"   🗑️ 跨批次去重: 丢弃「{t_title}」(关键词重叠率 {kw_overlap:.0%})")
            elif is_event_duplicate(t.get("title", ""), cross_sigs):
                print(f"   🗑️ 跨批次事件去重: 丢弃「{t_title}」(与今日已发比赛重复)")
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

    # ── P0-1: 语义去重（事件指纹硬去重）──
    # 同一场比赛（同两队 + 同比分）近 7 天只发一次，与措辞无关，
    # 彻底拦截「换说法重发」（如「切尔西4-3布莱顿：进球大战」与「…老六直呼过瘾」）。
    event_sigs = (topic_history or {}).get("event_signatures")
    if event_sigs and topics:
        filtered = []
        for t in topics:
            t_title = t.get("title", "")
            if is_event_duplicate(t_title, event_sigs):
                print(f"   🗑️ 事件指纹去重: 丢弃「{t_title[:30]}」(与近7天已发比赛重复)")
            else:
                filtered.append(t)
        if len(filtered) < len(topics):
            print(f"   事件指纹去重: {len(topics)} → {len(filtered)} 个话题")
        topics = filtered

    # ── P0-1: 跨天关键词去重（7 天窗口，>=50% 重叠即丢弃）──
    # 比仅看昨天更稳：覆盖「跨天同事件换说法」且标题未带比分的情形。
    hist_kw = (topic_history or {}).get("keywords")
    if hist_kw and topics:
        filtered = []
        for t in topics:
            t_title = t.get("title", "")[:30]
            t_kws = set(k.lower() for k in (t.get("keywords", []) or []) + (t.get("keywords_cn", []) or []))
            if not t_kws:
                filtered.append(t)
                continue
            overlap = len(t_kws & hist_kw) / max(len(t_kws), 1)
            if overlap >= 0.5:
                print(f"   🗑️ 跨天去重(7天): 丢弃「{t_title}」(关键词重叠 {overlap:.0%})")
            else:
                filtered.append(t)
        if len(filtered) < len(topics):
            print(f"   跨天去重(7天): {len(topics)} → {len(filtered)} 个话题")
        topics = filtered

    print(f"   筛选出 {len(topics)} 个话题:")
    for i, t in enumerate(topics):
        print(f"   {i+1}. [{t.get('content_type', 'N/A')}] {t['title'][:50]}")

    # Check topic material sufficiency — reject topics that match_data can't support
    topics = _check_topic_material_sufficiency(topics, match_data)

    # R11 结论落地：有效选题（转会官宣/冲突舆情/中国足球）优先，战报比分减产
    topics, _cat_dropped = topic_category.prioritize(topics)

    # ── P1-4 / P1-6 / 维度4 / 维度2: 非阻断监控（标题钩子 / 类型平衡 / 共鸣角度 / 系列连载）──
    warn_title_hook_distribution(topics)
    topic_category.warn_category_distribution(topics)
    title_positive.warn_adoption(topics)
    warn_type_balance(topics, season_label=season_label)
    warn_resonance_coverage(topics)
    warn_series_continuity(topics)

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


def rewrite_article(topic, match_context, index, temperature=0.5, retry_hint="", date_str="", source=None):
    """将已核实的源文章改写为老六风格。

    输入：来自直播吧/懂球帝的记者核实报道
    输出：老六风格文章（完全相同的事实，不同的文笔）
    """
    # 优先使用调用方已匹配/懒加载（含爬取全文）的 source；
    # 否则回退到从 match_context 重新查找。这样转会/八卦类话题在
    # generate_article_with_retry 里懒加载到完整正文后，不会被 rewrite_article
    # 重新用短文本查询而误判为「无源文章」导致整篇改写失败。
    source = source or _find_source_article(topic, match_context)
    if not source:
        return None

    source_text = source["article_text"]
    fixture = source["fixture"]

    content_type = topic.get("content_type", "热点球评")
    print(f"\n[3.{index}] [改写-{content_type}] {topic['title'][:40]}...")

    # 风格引导（维度1：由品牌手册 columns 派生，单一事实源）
    style_guide = get_brand_style_guide()
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

    # 计划 11.3 素材边界·物理隔离：生成器只能看到抽取器产出的「结构化事实块」，
    # 绝不接触正文原文。抽取器允许看原文，但其输出在离开抽取器前已过逐字比对闸门。
    from extractor import build_source_facts_block
    try:
        facts_block, _extracted = build_source_facts_block(source, call_llm=call_llm_json)
    except Exception as _e:
        facts_block, _extracted = "（抽取器不可用）", []
        print(f"   ⚠️ 抽取器失败，降级为无源文事实块: {type(_e).__name__}")
    print(f"   🧩 抽取器：{len(_extracted)} 条结构化事实（生成器对原文不可见）")

    prompt = base_prompt.format(
        source_text=facts_block,
        content_type=content_type,
        style=style,
        word_min=word_min,
        word_max=word_max,
        index=index,
        column_block=column_block,
        retry_block=retry_block,
    )

    # 维度4：共鸣角度闭环——选题锁定的共鸣角度，改写时自然融入
    prompt = prompt + _build_resonance_hint(topic)
    # 维度2：系列连载闭环——选题锁定的 series_id，改写时注入追更引导
    prompt = prompt + _build_series_hint(topic)

    # 维度1：品牌手册注入系统提示（单一事实源，避免无魂/偷懒）
    system_content = ("你是一个足球文章改写助手。你必须保留所有事实（比分、球员、事件），只改变文风和叙述角度。\n")
    brand_manual_block = load_brand_manual()
    if brand_manual_block:
        system_content += "\n## 品牌手册（球评人老六 · 单一事实源，本篇写作须遵循）\n" + brand_manual_block

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": prompt},
    ]

    # 多模型轮换 + 免费额度管理（空响应/解析失败自动切换，避免单模型空响应导致改写失败）
    try:
        article, _model_used = call_llm_json(messages, LLM_JSON_CANDIDATES,
                                             temperature=temperature, max_tokens=8192)
    except ValueError as e:
        print(f"   ❌ 改写：所有 LLM 候选均失败: {e}")
        return {}

    if article and isinstance(article, dict):
        article["content_type"] = content_type
        article["_source_fixture"] = fixture
        # Inject column metadata（P2-7：消除元数据空字段，保证栏目/风格/互动齐全）
        for _f in ("_column_id", "_column_name", "_writing_style", "_style_detail",
                   "_interaction_type", "_topic_domain", "_batch_name",
                   "_batch_time", "_reader_scenario", "_overall_tone"):
            v = topic.get(_f, "")
            if v:
                article[_f] = v
        # ⚠️ 注意：_batch_name 必须独立于 _column_name 写入。
        # 应急/预测文章没有 _column_name，但仍有批次归属，否则发布器按批次过滤会漏掉。
        print(f"   改写完成: {article.get('title','?')}, {len(article.get('content',''))}字")
    return article


# ============================================================
# 事实校验辅助：队名/球员名脏数据识别 + 多渠道校准
# ============================================================
# 含这些结果/动作词的字符串不是纯队名（如 "客胜曼联"、"十人曼城"）
_TEAM_DIRTY_TOKENS = ("胜", "负", "平", "赢", "输", "绝杀", "逆转", "爆冷",
                      "晋级", "淘汰", "出局", "点球", "加时", "帽子戏法",
                      "梅开二度", "上演", "破门", "进球", "十人", "九人",
                      "八人", "七人", "十一人", "十二人",
                      # 阵容/轮换类词：赛程数据常把「首发阵容」粘连进队名
                      # （2026-10-13 真实熔断：away_team='大田市民首发'）
                      "首发", "替补", "阵容", "轮换", "出战", "名单",
                      "报名", "登场", "伤缺", "伤退", "停赛", "复出")
# 球员名尾部常见的动作/描述词，出现说明抽取到了片段而非纯名字
_PLAYER_DIRTY_TOKENS = ("上演", "破门", "进球", "帽子戏法", "梅开二度", "独中两元",
                        "绝杀", "助攻", "头球", "远射", "抽射", "推射", "点射",
                        "补射", "铲射", "垫射", "完成", "帮助", "传中", "传出",
                        "梅开", "独中", "独造", "分钟", "第", "连下", "双响",
                        "扳平", "反超", "扳回")


def _looks_like_team(name):
    """判断字符串是否像一个干净的球队名（用于过滤脏数据）。

    含结果/红牌描述词（如 '客胜曼联'、'十人曼城'）的不是纯队名；
    纯拉丁队名（如 'Manchester City FC'，不含中文描述词）也视为有效。
    """
    if not isinstance(name, str):
        return False
    s = name.strip()
    if not (2 <= len(s) <= 30):
        return False
    if any(tok in s for tok in _TEAM_DIRTY_TOKENS):
        return False
    return True


# ------------------------------------------------------------
# 计划 7.3 · 队名「别名感知」判定
# ------------------------------------------------------------
# 事实校验必须要求「同一支球队」，而不是「同一个字符串」。事实层队名来自
# entity_map（规范名，如「拜仁」），而来源/正文可能用全称或俗称（「拜仁慕尼黑」
# 已作为 entity_map 别名登记）。此前按字面包含判定，一旦两处用不同写法即误报
# 「缺少客队名」，累积 5 次还会触发第十章自动熔断（2026-10-12 的真实熔断即由
# 「缺少客队名: 拜仁慕尼黑」这一误报引起）。故这里按**实体等价**判定。
_ENTITY_TEAM_FORMS_CACHE = None


def _load_entity_team_forms():
    """config/entity_map.json（仅 club）→ {表面形式: frozenset(同实体全部表面形式)}。"""
    global _ENTITY_TEAM_FORMS_CACHE
    if _ENTITY_TEAM_FORMS_CACHE is not None:
        return _ENTITY_TEAM_FORMS_CACHE
    idx = {}
    try:
        data = json.loads((PROJECT_ROOT / "config" / "entity_map.json")
                          .read_text(encoding="utf-8"))
        for e in (data.get("entities") or {}).values():
            if e.get("category") != "club":
                continue
            raw = [e.get("name_zh"), e.get("name_en")] + list(e.get("aliases") or [])
            forms = frozenset(str(x).strip() for x in raw if isinstance(x, str) and len(str(x).strip()) >= 2)
            if not forms:
                continue
            for f in forms:
                idx.setdefault(f.lower(), set()).update(forms)
    except Exception:
        idx = {}
    _ENTITY_TEAM_FORMS_CACHE = {k: frozenset(v) for k, v in idx.items()}
    return _ENTITY_TEAM_FORMS_CACHE


def _team_forms(name):
    """返回同一支球队的**全部可接受写法**（规范名 + entity_map 别名 + TEAM_ALIASES 同组）。

    精确到实体：``AC米兰`` 的别名含「米兰」，而 ``国际米兰`` 的别名只有「国米 /
    国际米兰 / 蓝黑军团」——因此正文写「米兰」不会让「国际米兰」蒙混过关。
    """
    if not isinstance(name, str) or not name.strip():
        return frozenset()
    name = name.strip()
    forms = {name}
    ent = _load_entity_team_forms().get(name.lower())
    if ent:
        forms |= set(ent)
    canon = TEAM_ALIASES.get(name, name)
    group = {k for k, v in TEAM_ALIASES.items() if v == canon}
    group.add(canon)
    forms |= group
    return frozenset(f for f in forms if isinstance(f, str) and len(f) >= 2)


def _team_in_content(name, content):
    """正文是否提到该球队（任一可接受写法命中即算）。"""
    if not content:
        return False
    return any(f in content for f in _team_forms(name))


def _team_registered(name) -> bool:
    """队名是否登记了别名集合（entity_map club ∪ TEAM_ALIASES 同组）。

    未登记 → 我们**无法知道它的合法简称集合**，此时硬性「全称字面包含」判定必然误杀：
    2026-10-13 的真实熔断即由此引起（赛程 away_team='大田市民首发'，正文按中文习惯
    写简称「大田」，字面不含全称 → 判「缺少客队名」→ 同批打回 5 次 → 自动熔断）。
    """
    return len(_team_forms(name)) > 1


def _relaxed_team_mention(name, content, other: str = "") -> bool:
    """未登记球队的**简称命中**判定（放宽，不拦截）。

    中文队名通常是「地名 + 昵称」（大田市民 = 大田 + 市民），正文写地名简称是正常写法。
    取队名前 2 字作为地名核心，要求：
      ① 该核心出现在正文；
      ② 不与同场另一队的地名核心相同——否则「上海海港 / 上海申花」会互相放行，
         那就真成了漏检。冲突时返回 False，交回严格判定。
    """
    if not content or not isinstance(name, str):
        return False
    core = name.strip()[:2]
    if len(core) < 2 or core not in content:
        return False
    if other and isinstance(other, str) and other.strip()[:2] == core:
        return False
    return True


def _same_team(a, b):
    """两个队名是否指同一支球队（写法差异不算不同队）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    return bool(_team_forms(a) & _team_forms(b))


def _teams_in_content(content, teams):
    """正文中出现的球队（**取最长匹配**，消除子串串味）。

    反向补一个真实误报源：``_KNOWN_TEAMS`` 含「米兰」，而正文写「国际米兰」时
    ``"米兰" in content`` 为真，会被误判成「出现事实卡外的球队实体: 米兰」。
    这里按跨度取最长匹配，命中「国际米兰」后即不再把其中的「米兰」算作另一队。
    """
    spans = []
    for t in teams:
        for f in _team_forms(t):
            start = content.find(f)
            while start != -1:
                spans.append((start, start + len(f), t))
                start = content.find(f, start + 1)
    spans.sort(key=lambda s: (-(s[1] - s[0]), s[0]))
    kept, covered = set(), []
    for a, b, t in spans:
        if any(a >= x and b <= y for x, y in covered):
            continue
        kept.add(t)
        covered.append((a, b))
    return kept


# ── 比分口径归一化（2026-10-24 晚间熔断根因）────────────────────────
# 正文写比分的写法不止「0-1」：0比1 / 0：1 / 0—1 / ０－１ 说的都是同一件事。
# 旧实现三个判定点各写各的正则（check_rewrite_fidelity /
# validate_article_vs_match_data / check_consistency_plan），且都只认 ASCII
# 的「-」「:」，于是「蓉城0比1英博」被判成「文中未出现该比分」，单篇改写反复
# 打回 5 次触发自动熔断。这里统一成一个抽取器：先剔除日期 / 时间 / 轮次（避免
# 「2026-10-1」「15:30」被误认成比分），再做全角→半角归一，最后抽数字对。
_DATE_LIKE = re.compile(r"\d{2,4}\s*[-–—－/.]\s*\d{1,2}\s*[-–—－/.]\s*\d{1,2}")
_TIME_LIKE = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}")
_ROUND_LIKE = re.compile(r"第\s*\d+\s*轮")
_SCORE_RE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:-|–|—|－|~|～|:|：|比|vs\.?)\s*(\d{1,2})(?![\d.])")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _normalize_score_text(text):
    """归一成适合抽比分的形态：去日期/时间/轮次，全角数字转半角。"""
    if not isinstance(text, str):
        return ""
    s = text.translate(_FULLWIDTH_DIGITS)
    s = _DATE_LIKE.sub(" ", s)
    s = _TIME_LIKE.sub(" ", s)
    s = _ROUND_LIKE.sub(" ", s)
    return s


def _extract_scores(text):
    """抽出文中所有比分形态的数字对（去重保序）。"""
    out = []
    for a, b in _SCORE_RE.findall(_normalize_score_text(text)):
        pair = (int(a), int(b))
        if pair not in out:
            out.append(pair)
    return out


def _score_mentioned(content, hg, ag):
    """正文是否写出了该比分（主客顺序可颠倒，写法不限）。"""
    try:
        hg, ag = int(hg), int(ag)
    except (TypeError, ValueError):
        return True
    return any(pair in ((hg, ag), (ag, hg)) for pair in _extract_scores(content))


def _clean_player_name(raw):
    """清理被动作片段污染的球员名（如 '内利上演' -> '内利'）。

    返回 (cleaned, was_dirty)：was_dirty 表示原始串含动作词、可信度低。
    """
    if not isinstance(raw, str):
        return "", False
    s = raw.strip()
    was_dirty = any(tok in s for tok in _PLAYER_DIRTY_TOKENS)
    changed = True
    while changed:
        changed = False
        for tok in _PLAYER_DIRTY_TOKENS:
            if s.endswith(tok) and len(s) > len(tok):
                s = s[: -len(tok)]
                changed = True
                break
    if not s or len(s) < 2:
        return "", was_dirty
    if any(tok in s for tok in _PLAYER_DIRTY_TOKENS):
        return "", was_dirty
    return s, was_dirty


def _build_match_reference(fixture, match_context):
    """从多渠道 fixtures 中为该场比赛中提取校准后的队名与球员名。

    返回 {'home_team','away_team','scorers'}：优先采用同场不同源的干净值，
    作为事实校验的"共识基准"，从而用多个渠道交叉验证而非依赖单一脏数据。
    """
    ref = {"home_team": "", "away_team": "", "scorers": set()}
    if not fixture or not match_context:
        return ref
    hg, ag = fixture.get("home_score"), fixture.get("away_score")
    ht0, at0 = fixture.get("home_team", ""), fixture.get("away_team", "")
    siblings = []
    for f in match_context.get("all_fixtures", []) or []:
        if f is fixture:
            continue
        if f.get("home_score") is None or f.get("away_score") is None:
            continue
        if (f.get("home_score"), f.get("away_score")) != (hg, ag):
            continue
        fh, fa = f.get("home_team", ""), f.get("away_team", "")
        if not fh or not fa:
            continue
        if any(x and x in (fh + fa) for x in (ht0, at0) if x) or \
           any(x and x in (ht0 + at0) for x in (fh, fa) if x):
            siblings.append(f)
    home_cands = [t for t in [ht0] + [s.get("home_team", "") for s in siblings] if _looks_like_team(t)]
    away_cands = [t for t in [at0] + [s.get("away_team", "") for s in siblings] if _looks_like_team(t)]
    ref["home_team"] = ht0 if _looks_like_team(ht0) else (home_cands[0] if home_cands else "")
    ref["away_team"] = at0 if _looks_like_team(at0) else (away_cands[0] if away_cands else "")
    scorers = set()
    all_goals = list(fixture.get("goals", []) or [])
    for s in siblings:
        all_goals.extend(s.get("goals", []) or [])
    for g in all_goals:
        if not isinstance(g, dict):
            continue
        cleaned, _ = _clean_player_name(g.get("scorer_name", g.get("scorer", "")))
        if cleaned:
            scorers.add(cleaned)
    ref["scorers"] = scorers
    return ref


def check_rewrite_fidelity(source_fixture, rewritten_article, match_context=None):
    """检查改写文是否忠实于来源文章。

    对比关键事实（比分、球员名、球队名）是否被改动。
    返回 (passed, issues)。
    """
    issues = []
    content = rewritten_article.get("content", "") + rewritten_article.get("title", "")
    source_text = source_fixture.get("article_text", "")

    # 检查1：比分一致
    # 注意：正文可能含日期「2026-07-03」、时间「15:30」，抽取器已先剔除这些形态。
    # 只在「文中出现了比分但都不对」时判不一致——前瞻类内容可以完全不写比分，
    # 不出现不等于失真（是否必须写由 validate_article_vs_match_data 按文种要求）。
    expected_hg = source_fixture.get("home_score")
    expected_ag = source_fixture.get("away_score")
    if expected_hg is not None and expected_ag is not None:
        found_scores = _extract_scores(content)
        if found_scores and not _score_mentioned(content, expected_hg, expected_ag):
            issues.append(f"比分不一致: 来源 {expected_hg}-{expected_ag}")

    # 检查2：球员名出现在改写文中（多渠道校准：自身清理 + 兄弟源干净名）
    ref = _build_match_reference(source_fixture, match_context) if match_context else None
    cand = []  # (name, confident)
    for g in source_fixture.get("goals", []) or []:
        raw = g.get("scorer_name", g.get("scorer", ""))
        if not raw:
            continue
        cleaned, was_dirty = _clean_player_name(raw)
        if cleaned:
            cand.append((cleaned, not was_dirty))
    if ref:
        for s in ref["scorers"]:
            cand.append((s, True))
    # 去重保序
    seen, unique = set(), []
    for name, conf in cand:
        if name in seen:
            continue
        seen.add(name)
        unique.append((name, conf))
    for name, confident in unique:
        if name in content:
            continue  # 改写文已包含该球员，通过
        if name in source_text:
            if confident:
                issues.append(f"缺少球员: {name}")
            else:
                print(f"   ⚠️ 球员名 '{name}' 源自脏数据片段且改写文未出现，放宽校验（不拦截）")
        # 源文章中也没有该名字（多为清理产物），跳过

    # 检查3：禁止新增断言表达，但需对照结构化进球数据做语义判断
    # 先用 goals[] 数据统计每个球员的进球数，用于验证"梅开二度""帽子戏法"
    from collections import defaultdict
    source_goals = source_fixture.get("goals", []) or []
    goals_by_scorer = defaultdict(int)
    for g in source_goals:
        name = g.get("scorer_name", g.get("scorer", ""))
        if name:
            goals_by_scorer[name] += 1
    has_double = any(c == 2 for c in goals_by_scorer.values())
    has_hattrick = any(c >= 3 for c in goals_by_scorer.values())

    # 强事实断言（帽子戏法/梅开二度/独造X球）：与结构化进球数据语义校验，硬拦截
    banned_patterns = [
        (r'帽子戏法', '新增编造: 帽子戏法'),
        (r'梅开二度', '新增编造: 梅开二度'),
        (r'独造\d+球', '新增编造: 独造X球'),
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

    # 具体时间（第N分钟）：足球叙述常用，且单一源文未必含；多渠道核验而非一刀切拦截。
    # 仅当多源文章与结构化进球数据都核不到该分钟时，降级为告警（不拦截），
    # 避免琐碎时间细节误杀整篇——这也是"从多个渠道去验证"的体现。
    minute_pattern = re.compile(r'第(\d+)分钟')
    content_minutes = set(int(m) for m in minute_pattern.findall(content))
    if content_minutes:
        # 结构化进球数据的分钟
        struct_minutes = set()
        all_goals = list(source_fixture.get("goals", []) or [])
        if match_context:
            for f in match_context.get("all_fixtures", []) or []:
                all_goals.extend(f.get("goals", []) or [])
        for g in all_goals:
            if isinstance(g, dict) and isinstance(g.get("minute"), int):
                struct_minutes.add(g["minute"])
        # 多源文章正文（含源文章自身）
        source_texts = [t for t in [source_text] if t] or []
        if match_context:
            for f in match_context.get("all_fixtures", []) or []:
                t = f.get("article_text", "")
                if t:
                    source_texts.append(t)
        corroborated = set()
        for n in content_minutes:
            if any(f"第{n}分钟" in t or f"{n}分钟" in t or f"{n}'" in t or f"{n}’" in t
                   for t in source_texts):
                corroborated.add(n)
            elif n in struct_minutes:
                corroborated.add(n)
        uncorroborated = content_minutes - corroborated
        if uncorroborated:
            names = "、".join(f"第{n}分钟" for n in sorted(uncorroborated))
            print(f"   ⚠️ 具体时间 {names} 未在多渠道源文中核到，放宽校验（不拦截）")

    return len(issues) == 0, issues


def validate_article_vs_match_data(source_fixture, rewritten_article, match_context=None):
    """验证改写文中的关键比赛信息是否与结构化比赛数据一致。

    与 check_rewrite_fidelity 不同，此函数直接对比比赛数据（而非源文章），
    可检测 LLM 编造的、源文章中也不存在的虚假细节。
    返回 (passed, issues)。
    """
    issues = []
    content = rewritten_article.get("content", "") + rewritten_article.get("title", "")

    # 多渠道校准：优先用同场不同源的干净队名，避免单一脏数据（如 '客胜曼联'）误拦截
    ref = _build_match_reference(source_fixture, match_context) if match_context else None
    ht = (ref or {}).get("home_team") or source_fixture.get("home_team", "")
    at = (ref or {}).get("away_team") or source_fixture.get("away_team", "")
    if not _looks_like_team(ht):
        if ht:
            print(f"   ⚠️ 主队名 '{ht}' 疑似脏数据，跳过主队名校验")
        ht = ""
    if not _looks_like_team(at):
        if at:
            print(f"   ⚠️ 客队名 '{at}' 疑似脏数据，跳过客队名校验")
        at = ""
    hg = source_fixture.get("home_score")
    ag = source_fixture.get("away_score")

    # 检查1：主客队名出现在文中（别名感知：全称/简称/俗称视为同一队）
    # 未登记别名的球队走「简称放宽」：中文正文写地名简称是正常写法，硬性全称包含
    # 必然误杀（2026-10-13 熔断根因）。放宽仅豁免「没提到该地名」，不豁免张冠李戴
    # ——后者由事实卡外球队校验（_teams_in_content）负责。
    if ht and not _team_in_content(ht, content):
        if not _team_registered(ht) and _relaxed_team_mention(ht, content, at):
            print(f"   ⚠️ 主队名 '{ht}' 未登记别名表，正文以简称出现，放宽校验（不拦截）")
        else:
            issues.append(f"缺少主队名: {ht}")
    if at and not _team_in_content(at, content):
        if not _team_registered(at) and _relaxed_team_mention(at, content, ht):
            print(f"   ⚠️ 客队名 '{at}' 未登记别名表，正文以简称出现，放宽校验（不拦截）")
        else:
            issues.append(f"缺少客队名: {at}")

    # 检查2：比分一致性
    # 口径统一到 _score_mentioned：0-1 / 0比1 / 0：1 / 0—1 / ０－１ 等价，
    # 主客顺序可颠倒；日期、时间、轮次不会被误认成比分（2026-10-24 熔断根因）。
    if hg is not None and ag is not None:
        if not _score_mentioned(content, hg, ag):
            issues.append(f"比分不一致: 比赛数据 {ht} {hg}-{ag} {at}，但文中未出现该比分"
                          f"（正文需写出比分，0-1／0比1／0：1 均可）")

    # 检查3：结构化进球数据的球员断言验证
    from collections import defaultdict
    goals = source_fixture.get("goals", [])
    goals_by_scorer = defaultdict(int)
    for g in goals:
        raw = g.get("scorer_name", g.get("scorer", ""))
        name = _clean_player_name(raw)[0] if raw else ""
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


def _downgrade_topic(topic):
    """计划九·一致性校验打回：把选题降级为低风险板块（无硬事实/时效依赖）。"""
    try:
        section = degrade.pick_low_risk_section()
    except Exception:
        section = "人物故事"
    topic["content_type"] = section
    topic["_column_name"] = section
    topic["_degraded"] = True
    return topic


# ── 计划第十章 · 自动熔断钩子 ───────────────────────────────────
# 同批次一致性校验打回次数是「事实层是否整体失真」的早期信号。
# 超过阈值即主动熔断（宁停勿错），避免在事实层失真的情况下继续批量产出。
_CONSISTENCY_REJECTS = {"count": 0, "suspended": False, "evidence": []}
_CONSISTENCY_REJECT_LOCK = threading.Lock()
#: 熔断证据保留条数（供人工复盘是哪几篇触发）
_CONSISTENCY_EVIDENCE_MAX = 8


def _note_consistency_reject(hint: str) -> None:
    """登记一次一致性打回；超阈值触发合规自动熔断（计划第十章）。

    同时累积**打回证据**（选题 + 具体问题）写进熔断 history，避免事后只看到
    最后一条提示、无法定位触发批次（2026-10-12 熔断即缺此证据）。
    """
    try:
        import compliance_guard
    except Exception:
        return
    with _CONSISTENCY_REJECT_LOCK:
        if _CONSISTENCY_REJECTS["suspended"]:
            return
        _CONSISTENCY_REJECTS["count"] += 1
        n = _CONSISTENCY_REJECTS["count"]
        _CONSISTENCY_REJECTS["evidence"].append(str(hint or "")[:120])
        evidence = list(_CONSISTENCY_REJECTS["evidence"][-_CONSISTENCY_EVIDENCE_MAX:])
    if compliance_guard.auto_suspend_if_systemic(
            n, detail=f"同批次一致性校验打回 {n} 次，疑事实层失真：{hint[:80]}",
            evidence="\n".join(f"{i + 1}. {e}" for i, e in enumerate(evidence))):
        _CONSISTENCY_REJECTS["suspended"] = True
        try:
            send_wxpusher("足球自媒体 ⛔ 自动熔断",
                          f"一致性校验打回 {n} 次，疑似事实层失真，已暂停自动发布")
        except Exception:
            pass


def _breaker_active() -> bool:
    """当前批次是否已处于合规熔断状态（进程内已触发 or 状态文件已熔断）。

    计划第十章「回到人工审核模式直至定位原因」。2026-10-13 事故谱系：
    熔断在生成阶段触发，但生成与发布都未停下，当晚照样发文，故障延后一整批。
    生成侧发现熔断立即中止后续产出——宁停勿错，也省下后续话题的 LLM 额度。
    """
    if _CONSISTENCY_REJECTS.get("suspended"):
        return True
    try:
        import compliance_guard
        return bool(compliance_guard.is_suspended())
    except Exception:
        # 状态不可读时不臆断——由发布边界（publisher）与 CI 复核步骤兜底
        return False


def check_interaction_bait(art: dict) -> dict:
    """互动引导生成后校验（R11 2026-10-13 建议5 / 计划 13.5 评论区运营）。

    在提示词里「粉丝沉淀钩子·必须满足」已存在，但此前**没有生成后校验**，写了等于没写。
    本闸门把「关注引导 + 互动引导」变成可打回的门禁项：
      - 生成侧（_rewrite_with_retry / generate_fixture_article）：未过 → 打回重写（最多 2 次）；
      - 保存侧（save_articles_local）：仍缺 → 模板兜底注入并复核，**绝不因此丢弃整篇成稿**。
    配置不可用 → 放行（degraded），不阻塞发布（计划九）。
    """
    return interaction_bait.check((art or {}).get("content", ""),
                                  bait_field=(art or {}).get("interaction_bait", ""))


def _bait_retry_hint(res: dict) -> str:
    return (f"互动引导未达标（缺 {'、'.join(res.get('missing') or [])}）：" +
            str(res.get("hint") or "") +
            " 请在正文或文末补足，且不得编造事实。")


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
                                  retry_hint=last_hint, date_str=date_str or "",
                                  source=source)
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
            passed, issues = check_rewrite_fidelity(fixture, art, match_context)
            if not passed:
                last_hint = "; ".join(issues)
                if attempt < max_retries:
                    continue
                return {}, f"改写不忠实: {last_hint}"

            # 第二层验证：防止改写文编造比赛数据中不存在的事件
            match_passed, match_issues = validate_article_vs_match_data(fixture, art, match_context)
            if not match_passed:
                last_hint = "; ".join(match_issues)
                _note_consistency_reject(
                    f"[{(topic.get('title') or topic.get('label') or topic.get('话题') or '未命名选题')[:40]}] {last_hint}")
                if attempt < max_retries:
                    continue
                # 计划九·一致性校验打回：降级为低风险板块后重生成一次；仍不过则丢弃
                if not topic.get("_degraded"):
                    _downgrade_topic(topic)
                    degrade.note("一致性校验", f"事实验证连续不过: {last_hint[:60]}",
                                 action=f"降级为低风险板块（{topic.get('content_type')}）后重生成一次")
                    return _rewrite_with_retry(topic, match_context, index, source, 1, date_str)
                degrade.note("一致性校验", f"降级后仍不过: {last_hint[:60]}",
                             action="丢弃该选题，交由候选池补位")
                return {}, f"事实验证失败: {last_hint}"

            # 计划 11.3 / 12.3 反洗稿双保险：相似度闸门 + 信息增量强制
            _src_texts = extract_source_texts(source)
            if _src_texts:
                _sim_ok, _sim_issues, _sim_metrics = check_similarity_gate(art, _src_texts)
                _inc_ok, _inc_issues, _inc_metrics = check_information_increment(art, _src_texts)
                art["_similarity"] = _sim_metrics
                art["_increment"] = _inc_metrics
                if not (_sim_ok and _inc_ok):
                    last_hint = ("；".join(_sim_issues + _inc_issues) +
                                 "。请用全新表达重写、切勿复述源文句子，并补充源文没有的事实"
                                 "（数据补充 / 历史对比 / 双源交叉）。")
                    if attempt < max_retries:
                        print(f"   🔁 反洗稿检查未过(attempt {attempt+1}/{max_retries+1}): "
                              f"{'；'.join(_sim_issues + _inc_issues)[:80]}")
                        continue
                    return {}, f"反洗稿检查未过: {last_hint}"

            # 计划合规·标题评分（及格线 60，未过打回重生成，最多 2 次）
            _ct = topic.get("content_type", "热点球评")
            _sc, _sc_pass, _sc_reasons = score_title_plan(topic, art.get("title", ""), _ct, date_str or "")
            if not _sc_pass:
                last_hint = (f"标题未达及格线({_sc}分): " + "; ".join(_sc_reasons) +
                             "。请按公式重拟：具体球队/球员+冲突反差动作+疑问收尾，字数18-30。")
                if attempt < max_retries:
                    continue
                degrade.note("评分连续不过", f"标题评分未过({_sc}分)",
                             action="丢弃该选题并由候选池补位")
                return {}, f"标题评分未过: {last_hint}"

            # 互动引导硬校验（R11 建议5 / 计划 13.5）：缺关注/互动引导 → 打回重写；
            # 最后一轮仍缺则放行，交由保存侧模板兜底注入（不丢整篇成稿）
            _ib = check_interaction_bait(art)
            if not _ib["ok"]:
                last_hint = _bait_retry_hint(_ib)
                if attempt < max_retries:
                    print(f"   🔁 互动引导未达标(attempt {attempt+1}/{max_retries+1})："
                          f"缺 {'、'.join(_ib['missing'])}")
                    continue
                degrade.note("互动引导未达标", f"缺 {'、'.join(_ib['missing'])}",
                             action="保存侧模板兜底注入（不丢弃成稿）")

            # 计划合规·AI 生成标注（7.2，发布前强制；改写路径的一致性校验由上方 fidelity 承担）
            append_ai_annotation(art, art.get("ai_perspective"))
            return art, None

        except Exception as e:
            last_hint = f"异常: {e}"
            if attempt >= max_retries:
                degrade.note("标题评分", f"改写异常: {str(e)[:60]}",
                             action="按上一版配比直接生成，不阻塞发布")
                return {}, f"改写异常: {e}"

    degrade.note("评分连续不过", "重生成次数耗尽", action="丢弃该选题并由候选池补位")
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

# ── 溯源标注（计划第十章：提高溯源覆盖率）─────────────────────────────
# LLM 改写路径此前不写 sources_used/source_post，导致溯源覆盖率长期偏低
# （实测 793 篇仅 38 篇有值）。而真实来源其实一直挂在
# article["_source_fixture"] 上（直播吧/懂球帝源文），只是没有落成标注。
# 这里把它显式写进 metadata（**只写元数据，不改正文**，符合「信号/事实不入正文」）。
_SOURCE_DISPLAY = {
    "zhibo8": "直播吧 zhibo8.cc",
    "dongqiudi": "懂球帝 dongqiudi.com",
}


def _source_display_name(key) -> str:
    k = str(key or "").strip()
    return _SOURCE_DISPLAY.get(k.lower(), k)


def resolve_provenance(art: dict) -> tuple:
    """从成稿解析可信来源标注，返回 (sources_used, source_post)。

    - sources_used：确定性供给已填则原样保留；否则从 `_source_fixture` 的媒体来源派生。
    - source_post：源文标题（供 data_collector 跨日同题去重）；取不到就留空，
      **绝不编造**（诚实原则：没有的来源不冒充）。
    """
    used = [str(s).strip() for s in (art.get("sources_used") or [])
            if s and str(s).strip()]
    fx = art.get("_source_fixture") or {}
    if not isinstance(fx, dict):
        fx = {}
    media = _source_display_name(fx.get("source"))
    if media and media not in used:
        used.append(media)
    post = str(art.get("source_post") or "").strip()
    if not post:
        post = str(fx.get("article_title") or fx.get("title") or "").strip()
    return used, post


def ensure_batch_attribution(articles, batch_name, batch_time=""):
    """给漏写批次归属的成稿补齐 batch_name / batch_time（就地修改）。

    为什么必须补：publisher.py 用 ``--batch`` 过滤文章，而过滤依据是 frontmatter
    的 batch_name。任何追加路径（突发球评/确定性供给/节点事件/赛前预测）只要漏写
    这一项，文章就会被当成「其他批次」静默跳过——生成了、入库了，却永远发不出去
    （2026-10-25 晚间预测稿实证：batch_name 为空、is_prediction 也没落，纯靠
    「空值被过滤」意外挡住）。

    ⚠️ 预测稿同样补归属，但保留 ``_is_prediction``：publisher 会显式跳过预测稿
    （R13 决策④），所以补 batch_name 不会让它被发出去；反而消除了「批次过滤 0
    命中 → 降级发布全部 → 预测稿漏发」这条隐患。

    返回补齐的篇数（供调用方打日志/验收）。
    """
    filled = 0
    for a in articles or []:
        if not isinstance(a, dict):
            continue
        if not a.get("_batch_name") and batch_name:
            a["_batch_name"] = batch_name
            filled += 1
        if not a.get("_batch_time") and batch_time:
            a["_batch_time"] = batch_time
    return filled


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
    pending_review = []
    all_hashes = set()
    pre_downloaded = pre_downloaded_images or {}

    for i, art in enumerate(articles):
        idx = i + 1
        # P2-7: 补全栏目/风格/互动字段，避免 metadata 出现空字段（垂直锚点缺失）
        fill_article_defaults(art)

        # 计划 1.2 / 4.1 高风险内容人工确认：涉及伤病/合同金额/争议指控的成稿
        # 转入人工确认队列（data/pending_review.json），确认前不进入发布集合。
        _risk = risk_gate.assess(art)
        if _risk.get("high_risk"):
            _rid = risk_gate.enqueue(art, date_str, idx, _risk)
            pending_review.append({"id": _rid, "index": idx,
                                   "title": art.get("title", ""),
                                   "reasons": _risk.get("reasons", [])})
            print(f"   🛡️ 高风险内容转人工确认队列 [{_rid}]："
                  f"{'；'.join(_risk.get('reasons', []))} → 本次不发布")
            continue

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

        # 4. 互动引导硬落地复核（R11 建议5 / 计划 13.5）：LLM 没给足关注/互动引导时，
        #    用模板兜底注入并复核——绝不因此丢弃整篇成稿（正文事实才是主体）。
        _ib = check_interaction_bait(art)
        if not _ib["ok"]:
            _inj = interaction_bait.inject(art)
            if _inj["changed"]:
                print(f"   🎣 互动引导兜底注入：缺 {'、'.join(_inj['injected'])}"
                      f" → {'已达标' if _inj['result']['ok'] else '仍未达标'}")
                degrade.note("互动引导兜底", f"缺 {'、'.join(_inj['injected'])}",
                             action="保存侧模板兜底注入")
            else:
                degrade.note("互动引导未达标", f"缺 {'、'.join(_ib['missing'])}",
                             action="模板兜底未生效，放行并留痕（不阻塞发布）")

        # Save article
        art_data = {**art, "downloaded_images": downloaded,
                     "tags": art.get("keywords", []), "category": "足球",
                     "column_id": art.get("_column_id", ""),
                     "column_name": art.get("_column_name", ""),
                     "batch_name": art.get("_batch_name", ""),
                     "batch_time": art.get("_batch_time", "")}
        result = file_writer.save_article(date_str=date_str, index=idx, article_data=art_data)
        # 溯源标注（计划第十章）：从 _source_fixture 派生真实来源，只写元数据、不动正文
        _prov_sources, _prov_post = resolve_provenance(art)
        _rec = {"index": idx, "title": art.get("title", ""), "path": result["article_path"],
                "slug": result["slug"], "tags": art.get("keywords", []),
                "keywords": art.get("keywords", []), "images": result["image_paths"],
                "sources_used": _prov_sources,
                "source_post": _prov_post,
                "originality_note": art.get("originality_note", ""),
                "content_type": art.get("content_type", ""),
                # 选题类型（计划 5.1 / R11）：落盘到批次 metadata，供看板按「选题类型」
                # 分组统计效果、供 ratio_tuner 以选题类型为调参轴（而非栏目）。
                # 形态优先于主题（见 topic_category 模块），判定不了即「未分类」，不猜。
                "topic_category": topic_category.classify(
                    {"title": art.get("title", ""),
                     "content_type": art.get("content_type", ""),
                     "keywords_cn": art.get("keywords", [])}),
                "column_id": art.get("_column_id", ""),
                "column_name": art.get("_column_name", ""),
                "batch_name": art.get("_batch_name", ""),
                # 晚间赛前预测稿：不发布到头条，看板/配额验收按此标剔除（R13 决策④）
                "is_prediction": bool(art.get("_is_prediction")),
                # 隔夜战报（P0-1）：跨批 match_id 去重的唯一依据，也是
                # 14 天后定向观测「这条通道值不值得留」的标记。
                # 必须落进 metadata —— 只在文章对象上留着下划线标记，
                # 一旦保存层挑字段就丢，导致同一场比赛被写两遍（同 10-25 预测稿漏标）。
                "is_recap": bool(art.get("_is_recap")),
                # 计划 12.1：三层供给打标（一级·确定性 / 二级·突发 / 三级·软性）
                "supply_layer": supply_mix.tag(art)}
        if art.get("_recap_match_id") not in (None, ""):
            _rec["recap_match_id"] = str(art["_recap_match_id"])
        # 互动引导兜底留痕（R11 建议5）：仅当命中模板兜底时才落盘，便于事后统计覆盖率
        if art.get("interaction_bait_source"):
            _rec["interaction_bait_source"] = art["interaction_bait_source"]
        saved.append(_rec)

    meta = {"total_articles": len(saved), "articles": saved, "topics": topics, "data_sources": {}}
    # 计划 12.1：记录本批三层供给构成（供 scripts/verify_supply_mix.py 汇总验收）。
    # 预测稿不发布到头条，不进配比口径（与看板/配额验收同口径，R13 决策④）。
    _supply = supply_mix.summarize(
        [a for a in articles if not a.get("_is_prediction")])
    meta["supply_layers"] = _supply
    if _supply["unclassified"]:
        print(f"   ⚠️ 三层供给存在未分层成稿 {_supply['unclassified']} 篇"
              f"（上游缺来源标志，验收将判失败）")
    _degrade_events = degrade.drain()
    if _degrade_events:
        meta["degrade_events"] = _degrade_events
        print(f"   🧯 本次降级事件 {len(_degrade_events)} 条（详见 metadata.degrade_events）")
    if pending_review:
        meta["pending_review"] = pending_review
        print(f"   🛡️ 本次 {len(pending_review)} 篇高风险成稿已转人工确认队列"
              f"（risk_gate.py --list 查看 / --approve <id> 确认）")
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
    """Generate a focused emergency article for a major event.

    计划合规：标题评分(60及格, 未过打回最多2次) + 一致性校验(卡外实体/比分即打回)
    + AI 生成标注（7.2）。第 3 次兜底发布并标注告警。
    """
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

    style = "突发新闻快评风格：开篇直接冲事件核心，节奏快，短句多，像第一条推送。300-400字即可，有冲击力，有明确态度。"

    base_prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝。刚刚发生了一件大事，需要你立刻写一篇快评！

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
{{"title": "标题(15-25字，有冲击力)", "backup_title": "备选标题", "content": "Markdown正文(300-500字，含≥2个##小标题，文末配图)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "站队式/投票式/预测式/共鸣式/挑战式/调侃式", "interaction_bait": "互动问题", "content_type": "紧急球评", "event_type": "{event_type}", "ai_perspective": "基于上述事实的一句独立判断（老六视角，≤40字，犀利有态度不骑墙）"}}
只输出JSON。"""

    # 事实卡：事件实际比分 + 事件涉及球队（用于一致性校验）
    # 口径与正文判定共用 _extract_scores：事件文本可能是「蓉城0比1英博」或全角写法，
    # 旧正则只认 ASCII「-」「:」，会把 allowed_scores 抽成空 → 事实卡失去比分约束。
    _det_scores = _extract_scores(detail or "")
    _fw_tier = rumor_wording.tier_of(event)   # 计划 4.1：事件若带分级则据此措辞（未知不判定）
    facts = {
        "allowed_scores": [f"{_det_scores[0][0]}-{_det_scores[0][1]}"] if _det_scores else [],
        "allowed_teams": [t for t in _KNOWN_TEAMS if t in (detail or "") or t in (title_hint or "")],
    }
    if _fw_tier:
        facts["可信度"] = _fw_tier
        base_prompt += (
            f"\n\n⚠️ 该事件分级为「{_fw_tier}」："
            + ("标题与正文都不得使用肯定语气，须写「传闻」「有消息称」「或将」「据悉」等模糊措辞；"
               "正文须明确标注消息来源。"
               if _fw_tier == rumor_wording.CONF_RUMOR else
               "标题与正文须写明来源（如「据某媒体报道」）。")
            + "\n"
        )

    last_hint = ""
    max_retries = 2
    for attempt in range(max_retries + 1):
        prompt = base_prompt
        if last_hint:
            prompt += f"\n⚠️ 上次未达标：{last_hint}\n这次必须修正。\n"
        messages = [
            {"role": "system", "content": f"你是头条号足球博主'球评人老六'，擅长突发事件快评。{style} 只输出JSON。"},
            {"role": "user", "content": prompt}
        ]
        try:
            article, _model_used = call_llm_json(messages, LLM_JSON_CANDIDATES,
                                                 temperature=temperature, max_tokens=4096)
        except ValueError as e:
            print(f"   ❌ 紧急球评：所有 LLM 候选均失败: {e}")
            return {}

        if not isinstance(article, dict) or not article.get("title"):
            if attempt < max_retries:
                last_hint = "模型未返回有效JSON/标题"
                continue
            print(f"   ❌ 紧急球评解析失败 (3次)")
            return {}

        content = article.get("content", "")
        if len(content) < 200:
            if attempt < max_retries:
                last_hint = f"正文仅{len(content)}字，需≥300字"
                continue

        # 计划合规·一致性校验（卡外实体/比分即打回）
        cons_ok, cons_issues = check_consistency_plan(article, facts, blocking=True)
        # 计划合规·标题评分（及格线 60；传闻级断言式标题直接 0 分，计划 4.1）
        sc, sc_pass, sc_reasons = score_title_plan(
            {"content_type": "紧急球评"}, article.get("title", ""), "紧急球评",
            fact_tier=_fw_tier)
        if not (cons_ok and sc_pass):
            if not cons_ok:
                _note_consistency_reject(
                    f"[{(article.get('title') or title_hint or '紧急球评')[:40]}] "
                    + "; ".join(cons_issues))
            if attempt < max_retries:
                parts = []
                if cons_issues:
                    parts.append("一致性:" + "; ".join(cons_issues))
                if not sc_pass:
                    parts.append(f"标题{sc}分:" + "; ".join(sc_reasons))
                last_hint = "；".join(parts)
                print(f"   🔁 紧急球评合规未过(attempt {attempt+1}/{max_retries+1}): {last_hint}")
                continue
            print(f"   ⚠️ 紧急球评合规末次仍未过(标题{sc}分/一致性{'OK' if cons_ok else 'FAIL'})，兜底发布并标注告警")

        # 计划合规·AI 生成标注（7.2）
        append_ai_annotation(article, article.get("ai_perspective"))
        print(f"   紧急球评标题: {article.get('title','?')}, 正文: {len(article.get('content',''))}字")
        return article

    return {}


# ============================================================
# Main
# ============================================================

def _has_source_for_topic(topic, match_data):
    """快速预检：该话题是否能在源文章/转会/新闻中找到可改写素材。

    逻辑与 generate_article_with_retry 的匹配保持一致（fixture 匹配 或 新闻标题关键词匹配），
    用于生成前提前跳过注定失败的话题，避免浪费 LLM 调用（优化②：减少无谓失败与耗时）。
    """
    if _find_source_article(topic, match_data):
        return True
    # 退路：新闻/转会标题与话题关键词匹配（generate_article_with_retry 会懒加载正文）
    news = list(match_data.get("news_articles", [])) + list(match_data.get("transfer_news", []))
    if not news:
        return False
    topic_kw = set(k.lower() for k in (topic.get("keywords", []) or []) + (topic.get("keywords_cn", []) or []))
    topic_text = (topic.get("title", "") + " " + topic.get("angle", "")).lower()
    for art in news:
        art_title = art.get("title", "").lower()
        if not art_title:
            continue
        if topic_kw and any(kw in art_title for kw in topic_kw):
            return True
        if any(word in topic_text for word in art_title.split()):
            return True
    # 话题无关键词时，generate_article_with_retry 会按"匹配任意"处理，视为可尝试
    return len(topic_kw) == 0


def _ranker_llm_call(messages):
    """ranker L3 模型打分的调用适配器。

    复用全局 LLM 轮换与额度管理（call_llm_json）；温度压低以保证量表打分的稳定性。
    """
    return call_llm_json(messages, LLM_JSON_CANDIDATES,
                         temperature=0.3, max_tokens=1024, timeout=60)


def _topic_card(t, cid, date_str):
    """把选题适配为事实卡形式，供 ranker 择优（L1 预筛 / L3 五维打分）使用。"""
    kw = t.get("keywords_cn") or t.get("keywords") or []
    return {
        "id": cid,
        "主体": (kw[0] if kw else ""),
        "动作": t.get("content_type", ""),
        "发生时间": date_str,
        "可用角度": [t.get("angle")] if t.get("angle") else [],
        "生命周期": "进行中",
    }


def _rank_topics(topics, match_data, date_str, *, use_llm=True):
    """计划 11.2 择优 L1→L2→L3→L4：信号增强 + 模型五维打分重排（非阻断）。

    纪律：
      - 仅重排、不删题——L1 剔除或未进入排名的候选按原序追加到末尾，发布集合不变；
      - 模型不可用 / 无信号 / 单日 token 超限 → 由 ranker 自动降级为规则排序（计划九）；
      - 全部异常都不影响发布（调用方 try/except 包裹）。
    """
    import ranker
    import source_watch

    # 计划 13.4 信源前移：原始信源只作信号（谁在谈），不进正文
    _signals = source_watch.signals_from_match_data(match_data)
    # 计划 11.2 L2：并入社媒/搜索等可得信号（缺失时保持缺省，绝不假装有信号）
    try:
        import signal_sources
        _signals = signal_sources.merge_with_forwarded(_signals, signal_sources.load_external())
    except Exception:
        pass

    cands = []
    for i, t in enumerate(topics):
        cid = f"topic-{i}"
        t["_rank_id"] = cid
        cands.append({
            "id": cid,
            "card": _topic_card(t, cid, date_str),
            "板块": t.get("column_name") or t.get("content_type", ""),
            "keywords_cn": t.get("keywords_cn"),
            "title": t.get("title"),
            "_topic": t,
        })

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    res = ranker.rank_candidates(
        cands, ctx={"now": now, "max_age_hours": 72}, signals=_signals,
        call_llm=_ranker_llm_call if use_llm else None,
        now=now, target_max=max(len(cands), ranker.DEFAULT_TARGET_MAX))

    order = {r["id"]: i for i, r in enumerate(res["ranking"])}
    topics.sort(key=lambda t: order.get(t.get("_rank_id"), 10_000))
    print(f"   🎯 择优（{res['mode']} 模式）：{len(res['ranking'])} 条进入排名；"
          f"{'；'.join(res['notes'][:2])}")
    return res


def _generate_articles_from_topics(topics, count, match_data, images_map, stats,
                                    articles_out, date_str=None, max_workers=3,
                                    index_offset=0):
    """Pipeline A：对每个话题从直播吧/懂球帝源文章改写为老六风格（并行生成以缩短耗时）。

    配图优先级：① 源文章战报图片 → ② Unsplash/Wikipedia 搜索。
    生成前先做源文章存在性预检，跳过注定失败的话题（优化②）；
    各话题并行改写，wall-clock 时间随并发数下降（优化③）。

    index_offset：本批文章在最终 articles 列表中的起始位置。P0 修法 A 后
    确定性供给先于主稿生成并占用配额，主稿必须从 offset 之后编号，
    否则 images_map / articles 元组索引会与确定性文章相撞（配图错位）。
    """
    selected = topics[:count]
    if not selected:
        return

    def _gen_one(i, topic):
        ct = topic.get("content_type", "N/A")
        print(f"\n--- 第{index_offset + i + 1}/{index_offset + count}篇 [{ct}] ---")

        # 计划第十章：本批已熔断（一致性打回超阈值）→ 不再继续生成（宁停勿错）
        if _breaker_active():
            print(f"   ⛔ 合规熔断中，本篇不再生成（待人工定位原因）")
            return i, None, "合规熔断中，本篇不再生成（计划第十章·宁停勿错）", []

        # 优化②：源文章存在性预检，提前跳过注定失败的话题
        if not _has_source_for_topic(topic, match_data):
            return i, None, f"未找到与话题「{topic.get('title','')[:20]}」匹配的源文章（预检跳过）", []

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

        art, error = generate_article_with_retry(topic, match_data, index_offset + i + 1,
                                                  max_retries=2, date_str=date_str)
        return i, (art if not error else None), error, imgs

    results = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(selected))) as ex:
        futures = [ex.submit(_gen_one, i, topic) for i, topic in enumerate(selected)]
        for fut in as_completed(futures):
            i, art, error, imgs = fut.result()
            results[i] = (art, error, imgs)

    # 按原顺序汇总，保证文章/配图索引一致（main 末尾会按 index 排序，这里顺序不影响最终输出）
    for i in sorted(results.keys()):
        art, error, imgs = results[i]
        images_map[index_offset + i] = imgs
        ct = selected[i].get("content_type", "N/A")
        stats["generated"] += 1
        if error:
            print(f"   ❌ 第{index_offset + i + 1}篇失败: {error}")
            stats["failed"] += 1
            stats["issues"].append(f"第{index_offset + i + 1}篇({ct}): {error}")
        else:
            stats["valid"] += 1
            # P0-3: 信息增量非阻断告警
            warn_if_low_info_increment(art)
            articles_out.append((index_offset + i, art))


# ============================================================
# Info-increment check (P0-3: 信息增量 · 非阻断告警)
# ============================================================
import re as _re

_INFO_NUM_RE = _re.compile(r"(\d{1,3}(?:\.\d+)?\s*(?:%|％|万|亿|岁|分|球|场|次|连|名|位|米|kg|KG|磅|′|’|\'|’))")
_INFO_DATE_RE = _re.compile(r"(\d{4}年|\d{1,2}月\d{1,2}日|第\d+[届次轮])")
_INFO_PROPER_RE = _re.compile(r"([一-鿿]{2,5}(?:杯|联赛|纪录|冠军|赛季|转会|德比|德比战|战|大战))")


def count_info_points(content):
    """粗略统计正文中的「信息点」数量（非阻断，仅用于告警）。

    信息点 = 具体数值/单位 + 时间锚点 + 专有赛事/纪录名词。
    低于阈值说明文章可能只是复述核心事实、缺乏增量，有被限流风险。
    """
    if not content:
        return 0
    # 去重计数：同一数字/名词只算一次
    nums = set(_INFO_NUM_RE.findall(content))
    dates = set(_INFO_DATE_RE.findall(content))
    props = set(_INFO_PROPER_RE.findall(content))
    # 球员/球队专有名词（来自别名词典）也算信息点
    teams = set()
    try:
        from data_collector import extract_match_teams
        teams = set(extract_match_teams(content))
    except Exception:
        teams = set()
    return len(nums) + len(dates) + len(props) + len(teams)


def warn_if_low_info_increment(article):
    """对单篇文章做信息增量告警（非阻断）。"""
    content = (article or {}).get("content", "")
    pts = count_info_points(content)
    title = (article or {}).get("title", "?")[:30]
    if pts < 4:
        print(f"   ⚠️ 信息增量偏低: 「{title}」仅检出 {pts} 个信息点"
              f"（数值/时间/专有名词偏少，限流风险↑，建议补充数据或背景）")
    else:
        print(f"   ✅ 信息增量 OK: 「{title}」检出 {pts} 个信息点")
    return pts


# ============================================================
# P1-4 / P1-6 非阻断监控：标题钩子分布 / 内容类型再平衡
# ============================================================
_TITLE_Q_RE = re.compile(r"[?？]|(吗|呢|凭什么|凭啥|为啥|为什么|怎么|究竟|到底)[\s，。！!]")
# 注意：本文件唯一的 _TITLE_CONFLICT_KW 定义在「计划合规层」(score_title_plan 使用的 29 词版)，
# 此处为早期 P1-4 监控遗留的 25 词副本，运行期已被下方同名下定义遮蔽，删除为行为无副作用。
_TITLE_NUM_RE = re.compile(r"\d+")


def _classify_title_hook(title):
    """把标题归类为 (是否疑问钩子, 是否冲突钩子, 是否数据钩子)。三类可重叠。"""
    t = title or ""
    is_q = bool(_TITLE_Q_RE.search(t))
    has_conflict = any(k in t for k in _TITLE_CONFLICT_KW)
    has_num = bool(_TITLE_NUM_RE.search(t))
    return is_q, has_conflict, has_num


def warn_title_hook_distribution(topics):
    """非阻断：检查选题标题的钩子分布（P1-4 提点击率，目标 疑问≥60% / 冲突≥30%）。"""
    if not topics:
        return
    n = len(topics)
    q = c = num = 0
    for t in topics:
        is_q, has_conflict, has_num = _classify_title_hook(t.get("title", ""))
        if is_q:
            q += 1
        if has_conflict:
            c += 1
        if has_num:
            num += 1
    qp, cp, np_ = q / n, c / n, num / n
    print(f"   📊 标题钩子分布: 疑问 {q}/{n} ({qp:.0%}) | 冲突 {c}/{n} ({cp:.0%}) | 数据 {num}/{n} ({np_:.0%})")
    if qp < 0.6:
        print(f"   ⚠️ 疑问钩子偏低（{qp:.0%} < 60%）——点击率风险↑，建议增加「？/吗/凭什么」类标题")
    if cp < 0.3:
        print(f"   ⚠️ 冲突钩子偏低（{cp:.0%} < 30%）——缺乏戏剧张力，建议增加反差/爆冷/绝杀类标题")
    if qp >= 0.6 and cp >= 0.3:
        print(f"   ✅ 标题钩子分布达标")


def warn_type_balance(topics, season_label=""):
    """非阻断：检查内容类型分布（P1-6 破同质化）。"""
    if not topics:
        return
    cnt = {}
    for t in topics:
        ct = t.get("content_type", "未知")
        cnt[ct] = cnt.get(ct, 0) + 1
    n = len(topics)
    print(f"   📊 内容类型分布: " + ", ".join(f"{k} {v}/{n}" for k, v in sorted(cnt.items(), key=lambda x: -x[1])))
    ball = cnt.get("热点球评", 0)
    tf = cnt.get("转会资讯", 0) + cnt.get("八卦趣事", 0)
    if season_label == "新赛季进行期":
        if n >= 3 and ball / n < 0.4:
            print(f"   ⚠️ 热点球评占比偏低（{ball / n:.0%} < 40%）——新赛季进行期应保球评下限")
        if tf / n > 0.5:
            print(f"   ⚠️ 转会+八卦占比偏高（{tf / n:.0%} > 50%）——应设上限，避免场外霸屏")
    if n >= 3 and len(cnt) < 2:
        print(f"   ⚠️ 品类单一（仅 {len(cnt)} 类）——建议覆盖 ≥2 个品类破同质化")


# 维度4（情绪共鸣升级）：选题须标注共鸣角度，以下为"无共鸣"判定值
_RESONANCE_NONE = ("无", "无（确实难共鸣时）", "无（确实难共鸣）", "")


def _build_resonance_hint(topic):
    """根据选题锁定的共鸣角度，生成改写环节的自然融入提示（维度4 闭环）。

    返回追加到改写 prompt 的文本；若无可共鸣角度则返回空串。
    """
    ra = (topic or {}).get("resonance_angle", "")
    if ra and ra not in _RESONANCE_NONE:
        return (
            f"\n\n## 本篇共鸣角度提示（来自选题）\n"
            f"本篇选题已锁定共鸣角度：{ra}。改写时自然融入，用老球迷聊球的口吻带出具体画面或回忆，"
            f"不强行煽情、不脱离事实（详见品牌手册「共鸣剧本」的切入与 framing 示例）。"
        )
    return ""


def warn_resonance_coverage(topics):
    """非阻断：检查选题共鸣角度覆盖率（维度4 情绪共鸣升级，目标 ≥60% 带真实共鸣角度）。"""
    if not topics:
        return
    n = len(topics)
    hit = 0
    for t in topics:
        ra = (t or {}).get("resonance_angle", "")
        if ra and ra not in _RESONANCE_NONE:
            hit += 1
    cov = hit / n
    print(f"   📊 共鸣角度覆盖: {hit}/{n} ({cov:.0%}) 带真实共鸣角度")
    if cov < 0.6:
        print(f"   ⚠️ 共鸣角度覆盖偏低（{cov:.0%} < 60%）——建议选题优先挑能用『国足情结/老球迷身份/世界杯经典/名宿沉浮』切入的事件")
    else:
        print(f"   ✅ 共鸣角度覆盖达标")


# 维度2（连载栏目引擎）：选题可标注 series_id，以下为"无系列"判定
_SERIES_NONE = ("无", "无(不适配系列时)", "无（不适配系列时）", "")


def _build_series_hint(topic):
    """根据选题锁定的 series_id，生成改写环节的追更引导提示（维度2 闭环）。

    返回追加到改写 prompt 的文本；若 series_id 为空或不在品牌手册系列库中则返回空串。
    不臆造『第N期』具体数字，避免与已发内容事实冲突。
    """
    sid = (topic or {}).get("series_id", "")
    if not sid or sid in _SERIES_NONE:
        return ""
    by_id, _ = get_brand_series()
    s = by_id.get(sid)
    if not s:
        return ""
    name = s.get("name", sid)
    theme = s.get("theme", "")
    return (
        f"\n\n## 本篇系列归属提示（来自选题）\n"
        f"本篇归属系列：《{name}》（主题：{theme}）。改写时可在文末自然加追更引导"
        f"（如『这个系列我会持续更，关注老六不迷路』）；若确系承接前文，可加『上集我们聊了…』，"
        f"但必须确保与已发内容事实一致，不编造未发生的『上一集』。"
    )


def warn_series_continuity(topics):
    """非阻断：检查系列连载分布（维度2 连载引擎）。

    同一批同系列不超过 max_per_batch（默认1），避免系列扎堆导致同质化；
    并汇报本批归属系列的篇数（系列并非每批都要开，0 篇属正常）。
    """
    if not topics:
        return
    n = len(topics)
    by_id, max_per = get_brand_series()
    counts = {}
    for t in topics:
        sid = (t or {}).get("series_id", "")
        if sid and sid not in _SERIES_NONE:
            counts[sid] = counts.get(sid, 0) + 1
    total = sum(counts.values())
    if counts:
        parts = ", ".join(f"{by_id.get(s, {}).get('name', s)}×{c}" for s, c in sorted(counts.items()))
        print(f"   📊 系列连载分布: {total}/{n} 篇归属系列（{parts}）")
    else:
        print(f"   📊 系列连载分布: 0/{n} 篇归属系列（本批无系列选题，正常）")
    for sid, c in counts.items():
        if c > max_per:
            print(f"   ⚠️ 系列『{by_id.get(sid, {}).get('name', sid)}』同批 {c} 篇 > 上限 {max_per}——同质化风险，建议同批只续 1 篇")


# ============================================================
# Article field fill (P2-7: 消除元数据空字段)
# ============================================================
# 按 content_type 给文章补齐默认栏目/风格/互动字段，保证发布器与算法
# 拿到完整的结构化信号（栏目=行为锚点，互动=涨粉钩子）。
_COLUMN_DEFAULTS_BY_TYPE = {
    "热点球评": ("hot-take", "老六辣评", "脱口秀吐槽体",
                "像足球吐槽大会单人版：开篇直接开火，用事实当子弹，有情绪更要有依据；结尾让人想截图转发。"),
    "转会资讯": ("transfer-radar", "转会雷达", "内幕分析体",
                "像球队经理评估交易：消息来源→球员分析→球队需求→转会可能性→影响评估，不确定的就说不知道。"),
    "八卦趣事": ("fan-life", "球迷众生相", "人间观察体",
                "像在球场边观察人间百态，用细节和画面说话，少评论多展示，让读者有共鸣。"),
    "战术解析": ("tactics-board", "战术黑板", "教书体",
                "先抛一个反常识的战术发现，用生活类比解释，最后给一个能记住的结论。"),
    "排行榜": ("data-rank", "数据盘点", "排名体",
                "每个条目3-5句话，毒舌但不刻薄，用对比制造笑点，最后一句是让人截图转发的吐槽。"),
    "紧急球评": ("breaking", "突发直击", "快讯体",
                "第一时间犀利点评，直击最刺激的瞬间，观点锋利不留余地。"),
    # —— 计划 6.2 固定四板块（赛程确定性供给落位到此）——
    "战术榜单": ("tactics-board", "战术黑板", "教书体",
                "先抛一个反常识的战术发现，用生活类比解释，最后给一个能记住的结论。"),
    "人物故事": ("fan-life", "球迷众生相", "人间观察体",
                "像在球场边观察人间百态，用细节和画面说话，少评论多展示，让读者有共鸣。"),
    "中国足球": ("china-football", "中国足球", "本土视角体",
                "深耕本土赛事，做全国媒体覆盖不到的本土视角。"),
}


def fill_article_defaults(art):
    """为单篇文章补齐缺失的栏目/风格/互动字段（非阻断，原地修改 art）。

    主稿经 _assign_columns_to_topics + rewrite_article 透传后通常已齐全；
    预测/Hupu/应急稿原本没有栏目，这里按 content_type 给默认，
    确保 metadata 不再出现『栏目/风格/互动为空』，强化算法垂直锚点。
    """
    if not isinstance(art, dict):
        return art
    ct = art.get("content_type", "") or "八卦趣事"
    cid, cname, style, detail = _COLUMN_DEFAULTS_BY_TYPE.get(
        ct, _COLUMN_DEFAULTS_BY_TYPE["八卦趣事"])
    if not art.get("_column_id"):
        art["_column_id"] = cid
    if not art.get("_column_name"):
        art["_column_name"] = cname
    if not art.get("_writing_style"):
        art["_writing_style"] = style
    if not art.get("_style_detail"):
        art["_style_detail"] = detail
    if not art.get("_interaction_type"):
        art["_interaction_type"] = "共鸣式"
    return art


# ============================================================
# 计划合规层（评分器 / AI 标注 / 一致性校验）
# 来源：《足球自媒体自动化选题与内容生产完善计划》v1.0
#   - 5.1 评分器是「及格线」(60分)：低于打回重生成，高于放行，不做择优漏斗
#   - 3.3 / 462 标题公式：具体人名或球队 + 冲突/反差动作 + 疑问收尾 + 字数 26-30
#   - 7.2 发布前强制加入 AI 生成标注
#   - 7.3 一致性校验：成稿专有名词/数字比对事实卡，卡外实体即打回
# 覆盖：主稿 Pipeline A（改写）、突发层（紧急球评）、预测层（赛前预测）
# ============================================================

# 标题字数合理区间（按内容类型；计划 3.3 公式化标题 26-30，快评/预测放宽）
_TITLE_LEN_BAND = {
    "紧急球评": (12, 28),
}
_TITLE_LEN_DEFAULT = (18, 30)

# 冲突/反差动作词（计划 3.3：冲突或反差动作）
_TITLE_CONFLICT_KW = ["却", "反而", "逆袭", "惨败", "绝杀", "爆冷", "反转", "下课", "翻盘",
                       "打脸", "暴跌", "血洗", "横扫", "复仇", "意外", "离谱", "掀翻",
                       "不过", "然而", "反超", "绝平", "苦涩", "尴尬", "崩盘", "硬仗",
                       "生死战", "点球", "红牌", "德比", "逆转", "翻车"]

# 知名球队/国家队（一致性校验「卡外实体」候选识别，仅球队；误漏不误杀）
_KNOWN_TEAMS = {
    "曼城", "曼联", "利物浦", "切尔西", "阿森纳", "热刺", "纽卡", "埃弗顿", "西汉姆", "维拉",
    "皇马", "巴萨", "马竞", "塞维利亚", "瓦伦西亚", "毕尔巴鄂", "国米", "米兰", "尤文", "那不勒斯",
    "罗马", "拉齐奥", "拜仁", "多特", "勒沃库森", "莱比锡", "法兰克福", "巴黎", "马赛", "里昂",
    "葡萄牙", "西班牙", "英格兰", "法国", "德国", "巴西", "阿根廷", "荷兰", "意大利", "比利时",
    "克罗地亚", "日本", "韩国", "中超", "国足", "广州", "申花", "海港", "国安", "泰山", "蓉城",
    "浙江", "三镇", "亚泰", "玉昆",
}
# 知名球员（仅用于标题「具体人物」加分，不参与一致性实体拦截，避免误杀）
_KNOWN_PLAYERS = {"梅西", "C罗", "姆巴佩", "哈兰德", "内马尔", "贝林厄姆", "凯恩", "萨拉赫"}


def score_title_plan(topic, title, content_type, batch_mode=None, *, fact_tier=None):
    """计划合规·标题评分器（及格线 60）。

    维度（计划 5.1/3.3/462）：字数、疑问钩子、具体对阵/球队/人物、冲突反差动作、
    板块对齐、自指惩罚、R11 标题正向规则（悬念词 +87% / 21-30 字最优 / 赛事名词垫底 /
    情绪词 -15%，见 title_positive.evaluate，只加分减分不阻塞）。返回
    (score:int 0-100, passed:bool, reasons:list[str])。
    reasons 用于在打回时回灌给 LLM 作为 retry_hint。

    传闻硬校验（计划 4.1：严重标题党扣 30 分 →「传闻类标题禁用肯定语气」）：
    ``fact_tier`` 未显式给出时从 ``topic`` 自动解析（可信度／fact_tier 字段）。
    命中传闻级断言式标题 → 直接 0 分并打回，不参与其余维度加分。

    赛果播报式硬校验（计划 3.3 标题公式 / 第十章内容质量，见 title_antipattern.py）：
    头条 869 条真实回测显示「以比分/赛果开场 + 无具体人物 + 无悬念钩子」是最典型的
    亏损形态（该组 0 爆款、最高阅读仅 67）。命中 → 直接 0 分并打回重写。

    纯赛事名词式硬校验（R11 内容诊断 2026-10-13 建议4，同 title_antipattern.check）：
    同一模块的规则二——「以世界杯/决赛/小组赛等赛事泛指名词开场 + 无具体人物/球队 + 无钩子」
    此前只在 title_positive 减 12 分（及格线 60 → 不扣也及格、采用率 0%），现升级为同级
    硬打回（真实回测命中组零百阅读）。两条规则任一命中即 0 分打回。
    """
    title = (title or "").strip()
    reasons = []
    if not title:
        return 0, False, ["标题为空"]

    # 计划 4.1：传闻类标题硬校验（先判，命中即 0 分打回）
    _tier = fact_tier if fact_tier is not None else rumor_wording.tier_of(topic)
    _rw = rumor_wording.check(_tier, title, "")
    if not _rw["ok"]:
        return 0, False, _rw["hard"] + [_rw["hint"]]

    # 计划 3.3/第十章：赛果播报式硬校验（同前，命中即 0 分打回）
    _ta = title_antipattern.check(title)
    _ta_penalty = 0
    if not _ta["ok"]:
        _ta_penalty = int(_ta.get("penalty_score") or 0)
        if _ta_penalty <= 0:
            return 0, False, list(_ta["reasons"]) + [_ta["hint"]]

    L = len(title)
    lo, hi = _TITLE_LEN_BAND.get(content_type, _TITLE_LEN_DEFAULT)
    if lo <= L <= hi:
        score = 25
    elif L < lo:
        score = max(0, 25 - (lo - L) * 3)
        reasons.append(f"标题偏短({L}字<{lo})")
    else:
        score = max(0, 25 - (L - hi) * 2)
        reasons.append(f"标题偏长({L}字>{hi})")

    # 疑问钩子（计划公式：疑问收尾）
    has_q = bool(re.search(r"[?？]$", title)) or any(
        w in title for w in ("吗", "呢", "凭什么", "凭啥", "为啥", "为什么", "怎么", "究竟", "到底"))
    if has_q:
        score += 20
    else:
        score += 5
        reasons.append("缺疑问钩子(建议以？/吗收尾)")

    # 具体对阵/球队/人物
    has_specific = bool(re.search(r"(vs|VS|对阵|大战|德比|\b:\b|：)", title)) or any(
        t in title for t in (_KNOWN_TEAMS | _KNOWN_PLAYERS))
    if has_specific:
        score += 20
    else:
        score += 3
        reasons.append("缺具体对阵/球队/人物")

    # 冲突/反差动作
    if any(k in title for k in _TITLE_CONFLICT_KW):
        score += 20
    else:
        score += 6
        reasons.append("缺冲突/反差动作")

    # 板块对齐（计划 5.1：板块）
    board_kw = {
        "转会资讯": ("转会", "签约", "官宣", "加盟", "离队", "续约", "转会费"),
        "战术解析": ("战术", "阵型", "数据", "打法", "复盘"),
        "排行榜": ("榜", "排名", "排行", "数据", "盘点"),
    }.get(content_type)
    if board_kw:
        if any(k in title for k in board_kw):
            score += 10
        else:
            score += 2
            reasons.append(f"与板块[{content_type}]关键词不匹配")
    else:
        score += 8  # 通用板块（热点球评/紧急球评/八卦）给基础分

    # R11 数据结论：标题正向规则（悬念词 +87% / 21-30 字最优 / 赛事名词垫底 / 情绪词 -15%）
    # 放在「板块对齐」之后、「自指惩罚/模板前缀」之前：模板前缀规则后续会把 score 归零，
    # 故本块绝不能把已被模板前缀归零的标题「复活」成通过分。
    _tp = title_positive.evaluate(title, known_teams=_KNOWN_TEAMS, has_specific=has_specific)
    if not _tp["degraded"]:
        score += _tp["bonus"]
        reasons.extend(_tp["reasons"])

    # 自指惩罚
    if any(w in title for w in ("老六", "小编", "我们")):
        score -= 10
        reasons.append("标题自指(老六/小编/我们)扣10")

    # 模板前缀惩罚（计划 P0-2：预测禁用固定前缀）
    for bad in ("老六精准预测：", "老六精准预测:", "老六预测：", "老六预测:"):
        if title.startswith(bad):
            score = 0
            reasons.append("命中禁用模板前缀(老六精准预测)")
            break

    # 赛果播报式（配置判罚分 >0 时按扣分处理；=0 时已在上方硬返回）
    if _ta_penalty:
        score -= _ta_penalty
        reasons.append(f"赛果播报式标题扣{_ta_penalty}：" + str(_ta.get("hint", "")))

    score = max(0, min(100, int(score)))
    passed = score >= 60
    if not passed and not reasons:
        reasons.append("综合得分未达60及格线")
    return score, passed, reasons


def append_ai_annotation(article, perspective=None):
    """计划合规·AI 生成标注（7.2）。

    文末强制加入「本文由 AI 生成」显式标识 + 「老六视角」独立判断段落
    （计划 4.1/4.2：主动标注并让判断力可见，满足实质人工介入判定）。
    幂等：已注入则跳过，避免重试时重复追加。
    """
    if not isinstance(article, dict):
        return article
    content = article.get("content", "") or ""
    if "本文由 AI 生成" in content:
        return article
    persp = (perspective or article.get("ai_perspective") or "").strip()
    block = (
        "\n\n---\n"
        "> 🤖 **本文由 AI 生成**。内容由「球评人老六」人设创作模型基于公开赛事数据自动撰写，"
        "观点仅为个人看法，**不构成任何投资、投注或决策建议**。\n"
    )
    if persp:
        block += f"\n**老六视角**（基于上述事实的独立判断）：{persp}\n"
    else:
        block += ("\n**老六视角**（基于上述事实的独立判断）：以上纯属老六一家之言，"
                  "欢迎评论区拍砖，赛后见真章。\n")
    article["content"] = content + block
    article["ai_generated"] = True
    article["ai_annotation_added"] = True
    return article


def check_consistency_plan(article, facts=None, *, blocking=True):
    """计划合规·一致性校验（7.3）。

    抽取成稿中的数字（比分）与知名实体（球队/球星），比对事实卡：
    - 比分：若 facts 提供 allowed_scores，出现卡外比分即打回（高 precision，针对不实内容）。
    - 球队实体：若 facts 提供 allowed_teams，正文中出现的知名球队不在允许集即打回（卡外实体）。
      blocking=False 时仅记录 issues、不阻断（用于前瞻类内容，避免误杀）。
    返回 (passed:bool, issues:list[str])。issues 用于 retry_hint / 告警日志。
    """
    facts = facts or {}
    content = (article or {}).get("content", "") or ""
    issues = []

    allowed_scores = set(facts.get("allowed_scores", []) or [])
    if allowed_scores:
        # 与事实层校验共用同一比分口径（0-1 / 0比1 / 0：1 等价，日期时间不算比分）
        norm = {f"{a}-{b}" for a, b in _extract_scores(content)}
        stray = norm - allowed_scores
        if stray:
            issues.append(f"出现事实卡外的比分: {', '.join(sorted(stray))}")

    allowed_teams = [t for t in (facts.get("allowed_teams", []) or []) if t]
    if allowed_teams:
        hit = any(_team_in_content(t, content) for t in allowed_teams)
        if not hit:
            issues.append("未提及任何事实卡内球队（疑似编造对阵/事件）")
        # strict 实体拦截：仅在 blocking 且来源为中文事实卡时启用，避免跨语言误杀。
        # 别名感知 + 最长匹配：写法差异（拜仁/拜仁慕尼黑）与子串串味（国际米兰/米兰）
        # 都不再误杀（计划 7.3 误杀率<5%）。
        if blocking:
            for t in sorted(_teams_in_content(content, _KNOWN_TEAMS)):
                if not any(_same_team(t, at) for at in allowed_teams):
                    issues.append(f"出现事实卡外的球队实体: {t}")
                    break

    # 计划 4.1：传闻分级措辞系统（失实表述扣 10 分）
    # 分级由 facts 携带（fact_tier／可信度，或嵌套事实卡），未携带则不判定（不误杀）。
    _tier = rumor_wording.tier_of(facts)
    if _tier:
        _rw = rumor_wording.check(_tier, (article or {}).get("title", ""), content)
        issues.extend(_rw["hard"])

    if not blocking:
        # warn-only：永远放行，但 issues 仍回传供日志
        _res = True
    else:
        _res = (len(issues) == 0)
    # 计划 7.3：一致性校验台账（拦截率可统计 / 误杀率留待人工标注）。失败不阻断。
    try:
        import consistency_stats
        consistency_stats.record(
            blocking=blocking, passed=_res, issues=issues, tier=_tier,
            title=(article or {}).get("title", ""),
            subject=facts.get("主体") or "、".join(list(allowed_teams)[:3]) or facts.get("subject", ""))
    except Exception:
        pass
    return _res, issues


# ------------------------------------------------------------
# 计划 11.3 / 12.3：相似度闸门 + 信息增量强制（反洗稿双保险）
#   第二道防线：成稿与源文 n-gram 重叠检测
#   第三道防线：成稿事实点数须不少于最强源文，且至少新增 2 条源文没有的事实
# ------------------------------------------------------------
_SIM_MAX_RUN = 12            # 连续重合字数上限（超过即判洗稿）
_SIM_GRAM_N = 8              # n-gram 长度
_SIM_MAX_GRAM_RATIO = 0.15   # 8-gram 重合率上限

# 信息增量强制（计划 11.3 / 12.3）
_INCREMENT_MIN_NEW = 2                  # 至少新增的「源文没有」的事实条数
_INCREMENT_REQUIRE_COUNT_PARITY = True  # 成稿事实点数须不少于最强源文


def _normalize_for_sim(text):
    """相似度比对前的归一化：去图片标记/markdown 符号/空白，只留实义字符。"""
    t = text or ""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)          # 配图标记
    t = re.sub(r"\]\([^)]*\)", "", t)                   # 残留链接
    t = re.sub(r"[#>*`\-—|\[\]（）()【】「」《》！？。，、；：“”‘’…\s]+", "", t)
    return t


def _longest_shared_run(a, b, n_gram=None):
    """a、b 的最长公共子串长度（13-gram 命中后左右扩展，O(n) 级别）。"""
    n = n_gram or (_SIM_MAX_RUN + 1)
    if not a or not b or len(a) < n or len(b) < n:
        return 0
    idx = {}
    for j in range(len(b) - n + 1):
        idx.setdefault(b[j:j + n], j)
    best = 0
    for i in range(len(a) - n + 1):
        j = idx.get(a[i:i + n])
        if j is None:
            continue
        x, y = i, j
        while x > 0 and y > 0 and a[x - 1] == b[y - 1]:
            x -= 1
            y -= 1
        e1, e2 = i + n, j + n
        while e1 < len(a) and e2 < len(b) and a[e1] == b[e2]:
            e1 += 1
            e2 += 1
        run = e1 - x
        if run > best:
            best = run
    return best


def _gram_overlap_ratio(article_text, source_text, n=_SIM_GRAM_N):
    """成稿的 n-gram 有多少比例出现在源文中（重合率）。"""
    if not article_text or not source_text or len(article_text) < n:
        return 0.0
    A = {article_text[i:i + n] for i in range(len(article_text) - n + 1)}
    S = {source_text[i:i + n] for i in range(len(source_text) - n + 1)}
    if not A:
        return 0.0
    return len(A & S) / len(A)


def extract_source_texts(source):
    """从改写路径的 source 结构里收集全部源文文本（去重）。"""
    texts = []

    def _grab(d):
        if not isinstance(d, dict):
            return
        for k in ("article_text", "content", "text", "raw_text"):
            v = d.get(k)
            if isinstance(v, str) and len(v) >= 50:
                texts.append(v)

    if isinstance(source, dict):
        _grab(source)
        _grab(source.get("fixture"))
    out, seen = [], set()
    for t in texts:
        key = t[:80]
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def check_similarity_gate(article, source_texts, *, max_run=_SIM_MAX_RUN,
                          gram_n=_SIM_GRAM_N, max_gram_ratio=_SIM_MAX_GRAM_RATIO):
    """计划 11.3 第二道防线：n-gram 相似度闸门。

    连续重合 > max_run 字，或 gram_n 元组重合率 > max_gram_ratio → 判洗稿。
    返回 (passed:bool, issues:list[str], metrics:dict)。source_texts 为空时放行。
    """
    content = _normalize_for_sim((article or {}).get("content", ""))
    if not content or not source_texts:
        return True, [], {"max_run": 0, "gram_ratio": 0.0}
    worst_run, worst_ratio = 0, 0.0
    for st in source_texts:
        s = _normalize_for_sim(st)
        if not s:
            continue
        run = _longest_shared_run(content, s)
        if run > worst_run:
            worst_run = run
        ratio = _gram_overlap_ratio(content, s, gram_n)
        if ratio > worst_ratio:
            worst_ratio = ratio
    issues = []
    if worst_run > max_run:
        issues.append(f"与源文连续重合 {worst_run} 字（>{max_run}，疑似洗稿）")
    if worst_ratio > max_gram_ratio:
        issues.append(f"{gram_n}-gram 重合率 {worst_ratio:.1%}（>{max_gram_ratio:.0%}，疑似洗稿）")
    return (len(issues) == 0), issues, {"max_run": worst_run, "gram_ratio": round(worst_ratio, 4)}


def _extract_fact_points(text):
    """抽取事实点：数字（带常见单位）+ 知名球队/球员实体。"""
    t = text or ""
    nums = set(re.findall(
        r"\d+(?:\.\d+)?(?:亿|万)?(?:欧元|英镑|镑|万|岁|年|%|球|分|场|次|连胜|连败|名|位|人)?", t))
    nums = {x for x in nums if x}
    ents = {e for e in _KNOWN_TEAMS if e in t}
    ents |= {e for e in _KNOWN_PLAYERS if e in t}
    return nums | ents


def check_information_increment(article, source_texts, *, min_new=_INCREMENT_MIN_NEW,
                                require_count_parity=_INCREMENT_REQUIRE_COUNT_PARITY):
    """计划 11.3 / 12.3 第三道防线：信息增量强制。

    成稿事实点数须不少于最强源文，且至少新增 min_new 条源文没有的事实。
    返回 (passed:bool, issues:list[str], metrics:dict)。
    """
    content = (article or {}).get("content", "") or ""
    if not source_texts:
        return True, [], {"article_facts": 0, "source_max": 0, "new_facts": 0}
    a_facts = _extract_fact_points(content)
    source_union, source_max = set(), 0
    for st in source_texts:
        f = _extract_fact_points(st)
        source_union |= f
        source_max = max(source_max, len(f))
    new_facts = a_facts - source_union
    issues = []
    if require_count_parity and len(a_facts) < source_max:
        issues.append(f"事实点数少于最强源文（{len(a_facts)} < {source_max}）")
    if len(new_facts) < min_new:
        issues.append(f"信息增量不足：仅新增 {len(new_facts)} 条源文没有的事实（需≥{min_new}）")
    return (len(issues) == 0), issues, {
        "article_facts": len(a_facts), "source_max": source_max, "new_facts": len(new_facts)}


# ============================================================
# Prediction Article — 赛前预测
# ============================================================

def generate_prediction_article(future_matches, date_str=None, recent_prefixes=None):
    """根据未来比赛数据生成一篇赛前预测文章。

    用 LLM 对每场明日比赛做 2-3 句分析 + 预测结果，
    文末带互动引导："评论区下注，明天赛后回来打我脸！"

    ⚠️ P0-2 改造：赛前预测**禁止**使用固定栏目前缀（如"老六精准预测："），
    并要求标题每日各不相同、不与近 7 天已发标题同前缀，破除模板化复读。

    Args:
        future_matches: list[dict]，由 collect_future_matches 返回
        date_str: 当前日期 YYYY-MM-DD（用于配图搜索）
        recent_prefixes: set[str]，近 7 天已发布标题的前 6 字集合（用于防重）

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

    # P0-2：把近 7 天已用标题前缀作为禁忌，明确禁止复读
    prefix_hint = ""
    if recent_prefixes:
        sample = list(recent_prefixes)[:12]
        prefix_hint = (
            "\n⚠️ 标题防重铁律（最重要）：\n"
            f"- 禁止以任何固定栏目前缀开头（尤其严禁『老六精准预测：』这类每天重复的模板）。\n"
            f"- 近 7 天已用过的标题开头（前 6 字）不可再用：{sample}\n"
            "- 今天这篇预测标题必须全新、独立，和上面任何一条都不重样；"
            "用具体的比赛/看点做开头，而不是固定口号。\n"
        )

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝，以犀利预测和毒舌分析著称。

你的任务是写一篇"明日赛程预测"——分析明天的足球比赛，给出你的预测结果。
今天是 {date_str or '今日'}。

以下是明天的赛程（{len(future_matches)} 场），请选择最有话题性的 {max_matches} 场进行分析：

{matches_text}
{prefix_hint}
写作要求：
1. 标题必须自然、有信息量（例如用具体对阵/看点开头，如"曼城主场能否啃下铁桶阵？""欧冠夜这三场最值得熬夜"），严禁任何固定栏目前缀模板
2. 为每场选中的比赛写 2-3 句话分析，给出明确预测结果（XX胜/平局/谁赢面大）
3. 语气要自信但不狂妄，像老球迷在群里吹水
4. 文末带互动引导：🔥 评论区下注，明天赛后回来打我脸！

风格：自信、犀利、有数据感但不堆砌。不编造球员具体数据。
没有确切数据就说"老六觉得""从近期表现来看"。

输出纯JSON:
{{"title": "标题(18-30字，全新且非模板化)", "content": "Markdown正文(含##小标题，600-900字)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "预测式", "interaction_bait": "互动问题，如'明天最看好哪场？评论区下注！'", "content_type": "热点球评", "ai_perspective": "基于上述事实的一句独立判断（老六视角，≤40字，犀利有态度不骑墙）"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是头条号足球博主'球评人老六'，以犀利预测和毒舌分析著称。风格自信、有数据感。不编造球员级别数据。标题必须自然不套模板。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]

    for attempt in range(3):
        try:
            # 多模型轮换 + 免费额度管理（空响应/解析失败自动切换，避免单模型空响应导致预测生成失败）
            article, _model_used = call_llm_json(messages, LLM_JSON_CANDIDATES,
                                                 temperature=0.7, max_tokens=4096)
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

            title = article.get("title", "")

            # ── P0-2：标题前缀防重拦截 ──
            # 若生成标题开头(前6字)与近7天已发标题重复，先重试；
            # 末次仍冲突则剥离违规前缀兜底，避免模板化复读上线。
            if recent_prefixes and title[:6] in recent_prefixes:
                if attempt < 2:
                    print(f"   ⚠️ 预测标题前缀复读「{title[:6]}」，重试换标题 (attempt {attempt+1}/3)...")
                    continue
                # 兜底：剥离固定前缀
                stripped = title
                for bad in ("老六精准预测：", "老六精准预测:", "老六预测：", "老六预测:"):
                    if stripped.startswith(bad):
                        stripped = stripped[len(bad):]
                        break
                else:
                    stripped = title[6:].lstrip("：:，, -—")
                stripped = stripped.strip("：:，, -— ")
                if stripped:
                    title = stripped
                    print(f"   🛠️ 预测标题前缀剥离兜底: →「{title[:30]}」")
                else:
                    print(f"   ⚠️ 预测标题剥离后为空，保留原样:「{article.get('title','')[:30]}」")

            article["title"] = title

            # 计划合规·一致性校验（前瞻内容：以赛程团队范围为软校验，warn-only 不阻断）
            pred_facts = {"allowed_teams": [m.get("home_team", "") for m in future_matches]
                          + [m.get("away_team", "") for m in future_matches]}
            _cons_ok, _cons_issues = check_consistency_plan(article, pred_facts, blocking=False)
            if _cons_issues:
                print(f"   ⚠️ 预测一致性软告警: {'; '.join(_cons_issues)}")
            # 计划合规·标题评分（及格线 60，未过打回重生成，最多 2 次）
            _p_sc, _p_sc_pass, _p_sc_reasons = score_title_plan(
                {"content_type": "热点球评"}, article.get("title", ""), "热点球评")
            if not _p_sc_pass:
                if attempt < 2:
                    _hint = (f"标题未过及格线({_p_sc}分): " + "; ".join(_p_sc_reasons) +
                             "。按公式重拟：具体对阵+冲突反差动作+疑问收尾。")
                    messages[1]["content"] += f"\n⚠️ {_hint}\n"
                    print(f"   🔁 预测标题未过及格线({_p_sc}分): {'; '.join(_p_sc_reasons)}，重试换标题...")
                    continue
                print(f"   ⚠️ 预测标题末次未过({_p_sc}分)，兜底发布并标注告警")

            # 计划合规·AI 生成标注（7.2，发布前强制）
            append_ai_annotation(article, article.get("ai_perspective"))

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


# ============================================================
# Fixture Article — 确定性供给（赛程冲突点选题）
# ============================================================
FIXTURE_PER_BATCH = 1  # 每批最多生成的确定性供给篇数（计划：确定性供给占内容~30%）
# P0 修法 A 配套（R13）：确定性供给改为**占用批次配额**后，若每批都发 1 篇，
# 日 6 篇下确定性将占 50%，偏离计划 12.1 的 ~30% 配比。故设每日预算：
# 日配额 6~8 篇 × 30% ≈ 2 篇/日，先到先得（晨/午优先，晚间预测稿日本身有预测稿）。
DETERMINISTIC_DAILY_BUDGET = 2

# ---- 隔夜战报（P0-1，二级·突发）----
# 每批最多 1 篇；只允许晨读与午间批产出（晚间批已有「明日赛前预测」占位，
# 再塞一篇战报会把晚报做成「早上那场球的复盘」，选题更闷）。
# 晨读批必发：赛果是晨读最硬的供给，赛事少就照常降级为「无战报」，
# 但**不允许**再拿蓄水池软性内容顶替（P0-2，见 supply_gap 处批次守卫）。
RECAP_PER_BATCH = 1
RECAP_BATCHES = ("auto", "morning", "noon")

# ---- 软性内容（三级）日上限（P0-2）----
# 计划 12.1 目标 20%，日 6 篇下约 1.2 篇。给 2 篇上限是留余量，
# 但晨读批硬性 0 篇（早晨必须吃隔夜赛果/硬新闻）。
SOFT_DAILY_LIMIT = 2


def _deterministic_used_today(date_str) -> int:
    """今日已保存批次中「一级·确定性」供给的篇数（每日预算计数，P0 修法 A 配套）。

    计数口径必须**跨批累计**：CI 的 batch.yml 回写步骤会把当日各批 articles
    合并进同一份 metadata（批次级 supply_layers 却只留当批），故按**成稿级
    `supply_layer`** 计数（合并后 articles 即当日累计，可靠）；
    成稿级标签缺失（改造前数据）时回落批次级汇总。读不到按 0 计——
    缺数据时放行而非冒充已用满（与「缺失标待接入」同思路）。
    """
    try:
        meta_path = OUTPUT_DIR / date_str / "metadata.json"
        if not meta_path.exists():
            return 0
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        n = sum(1 for a in (meta.get("articles") or [])
                if str(a.get("supply_layer") or "").startswith("一级·确定性"))
        if n:
            return n
        counts = (meta.get("supply_layers") or {}).get("counts") or {}
        return int(counts.get("一级·确定性", 0) or 0)
    except Exception:
        return 0


def _soft_used_today(date_str) -> int:
    """今日已发「三级·软性」成稿篇数（跨批累计，P0-2 单日上限判定）。

    口径同 `_deterministic_used_today`：读当日 metadata 的**成稿级** supply_layer，
    而不是批次级汇总（批次汇总只留当批，跨批会重复或漏算）。
    """
    try:
        meta_path = OUTPUT_DIR / date_str / "metadata.json"
        if not meta_path.exists():
            return 0
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return sum(1 for a in (meta.get("articles") or [])
                   if str(a.get("supply_layer") or "").startswith("三级·软性"))
    except Exception:
        return 0


def _fixture_content_type(topic):
    """赛程分型 → 板块 content_type（计划 6.2 固定四板块：转会动态／人物故事／中国足球／战术榜单）。"""
    return topic.get("板块", "战术榜单")


def _recap_sent_today(date_str) -> set:
    """今日已发隔夜战报的 match_id 集合（P0-1 跨批去重）。

    战报的发布窗口是「完赛 + 14h」，凌晨结束的比赛会横跨晨读与午间两批；
    若不用 match_id 去重，同一场国家德比会被写两遍。
    赛程稿按球队去重、战报按 match_id 去重 —— 两套口径**不能混用**
    （赛前分析 + 赛后复盘是两篇文章，不是重复）。
    """
    try:
        meta_path = OUTPUT_DIR / date_str / "metadata.json"
        if not meta_path.exists():
            return set()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return {str(a.get("recap_match_id")) for a in (meta.get("articles") or [])
                if a.get("recap_match_id")}
    except Exception:
        return set()


def _build_fixture_fact_card(facts):
    """事实层：把积分榜/射手榜/站位组装成 LLM 唯一可引用的事实卡（中文）。"""
    lines = [
        f"赛事：{facts.get('competition', '')}",
        f"开赛（北京时间）：{facts.get('kickoff_cst', '')}",
        f"对阵：{facts.get('home_zh', '')} vs {facts.get('away_zh', '')}",
        f"认知度组合：{facts.get('awareness_pair', '')}",
        f"核心冲突点：{facts.get('conflict', '')}",
    ]
    standings = facts.get("standings") or []
    if standings:
        lines.append("\n积分榜（前5）：")
        for r in standings[:5]:
            lines.append(f"  {r['position']}. {r['team_zh']} {r['points']}分（净胜球{r['gd']}）")
    hs, aw = facts.get("home_standing"), facts.get("away_standing")
    if hs or aw:
        lines.append("\n两队站位：")
        if hs:
            lines.append(f"  {facts['home_zh']}：第{hs['position']}位 {hs['points']}分 净胜球{hs['gd']}")
        if aw:
            lines.append(f"  {facts['away_zh']}：第{aw['position']}位 {aw['points']}分 净胜球{aw['gd']}")
    scorers = facts.get("scorers") or []
    if scorers:
        lines.append("\n射手榜（前5）：")
        for s in scorers[:5]:
            lines.append(f"  {s['player_zh']}（{s['team_zh']}）{s['goals']}球")
    hts, ats = facts.get("home_top_scorer"), facts.get("away_top_scorer")
    if hts or ats:
        lines.append("\n两队射手王：")
        if hts:
            lines.append(f"  {facts['home_zh']}：{hts['player_zh']} {hts['goals']}球")
        if ats:
            lines.append(f"  {facts['away_zh']}：{ats['player_zh']} {ats['goals']}球")
    if not standings and not scorers:
        lines.append("\n（注：积分榜/射手榜接口暂不可用，仅基于赛程与认知度冲突点创作，禁止编造数据）")
    return "\n".join(lines)


def _build_recap_fact_card(topic):
    """隔夜战报事实卡：比分 / 半场 / 进球明细，全部来自 football-data 原值。

    **比分口径唯一**：卡里写「主队 2 - 1 客队」这一种顺序（football-data 的
    score.fullTime.home/away），下游校验（_extract_scores / _score_mentioned）
    已统一支持「2-1」「2比1」「2：1」等写法，但方向必须与卡一致。
    """
    sc = topic.get("score") or {}
    hg, ag = sc.get("home"), sc.get("away")
    lines = [
        f"赛事：{topic.get('competition', '')}",
        f"比赛（北京时间）：{topic.get('kickoff_cst', '')} 开球",
        f"对阵：{topic.get('home_zh', '')}（主队） vs {topic.get('away_zh', '')}（客队）",
        "",
        f"★ 最终比分：{topic.get('home_zh', '')} {hg} - {ag} {topic.get('away_zh', '')}",
    ]
    ht_h, ht_a = sc.get("ht_home"), sc.get("ht_away")
    if ht_h is not None and ht_a is not None:
        lines.append(f"半场比分：{topic.get('home_zh', '')} {ht_h} - {ht_a} {topic.get('away_zh', '')}")
    else:
        lines.append("半场比分：接口未提供（不得自行编造）")
    if topic.get("winner_zh"):
        lines.append(f"结果：{topic['winner_zh']}取胜，净胜 {topic.get('goal_diff')} 球")
    else:
        lines.append("结果：双方打平")

    goals = topic.get("goals") or []
    if goals:
        lines.append("")
        lines.append("进球明细（按发生顺序，全部为接口原值）：")
        for g in goals:
            minute = g.get("minute")
            inj = g.get("injury_time")
            mtxt = f"第 {minute} 分钟" if minute is not None else "补时阶段"
            if inj:
                mtxt = f"第 {minute}+{inj} 分钟"
            kind = {"OWN": "乌龙球", "PENALTY": "点球"}.get(g.get("type"), "")
            name = g.get("scorer_zh") or ""
            if not g.get("has_zh_name"):
                # 无中文译名：原样给出，并在共享提示里要求正文照抄，禁止自行音译
                lines.append(f"  {mtxt} {name}（{g.get('team_zh', '')}）{kind}"
                             f"  ← 该球员无中文译名，正文请照抄此原文写法")
                continue
            lines.append(f"  {mtxt} {name}（{g.get('team_zh', '')}）{kind}")
    else:
        lines.append("")
        lines.append("进球明细：接口未提供（只能写比分与走势，不得虚构进球者）")
    lines.append("")
    lines.append("数据来源：football-data.org v4（赛果接口）")
    return "\n".join(lines)


def score_fixture_title(topic, title, batch_mode="auto"):
    """管道内规则评分器（计划 5.1/7.2）：及格线 60 分，低于阈值打回重生成。

    评分维度（计划 5.1：字数、问号、板块、时段 —— 只判格式合规，不作择优漏斗）：
      - 字数 30 分：26~30 字达标；22~34 字给 20；其余 0（计划 3.3：字数落在 26 至 30 字）。
      - 疑问收尾 30 分：以『？』收尾满分；含疑问词（吗/呢/为何/凭什么/怎么/谁更）给 18；否则 0
        （计划 3.3：以疑问收尾）。
      - 具体对阵 25 分：标题含主客队双方各 25；含一方 15；否则 0（计划 3.3：具体人名或球队+冲突反差）。
      - 板块保底 15 分：赛程确定性选题默认给到（避免误杀）。
    返回 (score 0~100, reasons)。
    """
    t = (title or "").strip()
    n = len(t)
    reasons = []
    score = 0
    if 26 <= n <= 30:
        score += 30; reasons.append("字数达标")
    elif 22 <= n <= 34:
        score += 20; reasons.append(f"字数略偏({n})")
    else:
        reasons.append(f"字数偏差({n})")
    if t.endswith("？") or t.endswith("?"):
        score += 30; reasons.append("疑问收尾")
    elif any(k in t for k in ("吗", "呢", "为何", "凭什么", "怎么", "为啥", "谁更", "究竟")):
        score += 18; reasons.append("含疑问词")
    else:
        reasons.append("无疑问收尾")
    home, away = topic.get("home_zh", ""), topic.get("away_zh", "")
    if home and home in t and away and away in t:
        score += 25; reasons.append("含主客队")
    elif (home and home in t) or (away and away in t):
        score += 15; reasons.append("含一方队名")
    else:
        reasons.append("未含对阵队名")
    score += 15; reasons.append("板块保底")

    # R11 数据结论：标题正向规则（确定性供给同口径；主客队名已知即视为「具体」）
    _tp = title_positive.evaluate(
        t,
        known_teams={x for x in (topic.get("home_zh", ""), topic.get("away_zh", "")) if x},
        has_specific=bool(topic.get("home_zh", "") and topic.get("home_zh", "") in t)
                     or bool(topic.get("away_zh", "") and topic.get("away_zh", "") in t),
    )
    if not _tp["degraded"]:
        score += _tp["bonus"]
        reasons.extend(_tp["reasons"])

    return min(100, score), reasons


def generate_fixture_article(topic, facts, date_str=None, recent_prefixes=None, topic_history=None, batch_mode="auto", avoid_signatures=None):
    """确定性供给文章：基于赛程冲突点 + 积分榜/射手榜事实层，由 LLM 生成表达层。

    事实层/表达层隔离：LLM 只能基于下方事实卡表达，禁止编造任何具体数据
    （比分、转会、伤病等卡外信息）。失败返回 None（非阻断）。
    """
    if not topic:
        return None
    content_type = _fixture_content_type(topic)
    fact_card = _build_fixture_fact_card(facts)

    prefix_hint = ""
    if recent_prefixes:
        sample = list(recent_prefixes)[:12]
        prefix_hint = (
            "\n⚠️ 标题防重铁律：\n"
            f"- 禁止任何固定栏目前缀/模板（尤其『老六前瞻：』『赛前分析：』这类每天重复的）。\n"
            f"- 近7天已用过的标题开头（前6字）不可再用：{sample}\n"
            "- 标题必须全新、独立，用具体对阵/看点开头；赛程类选题严禁写『XX前瞻』做标题"
            "（可用战术/数据/人物角度）。\n"
        )

    angle_type = topic.get("angle_type", "")
    # 角度指引按**建议栏目**取。此前按 angle_type 取，而 angle_type 取值是「强强对话/强弱反差」
    # 等分型名、与「战术榜单/数据对照/人物故事」这套键对不上 → 指引恒走兜底分支，
    # R11「写人 > 写事」的人视角要求实际从未生效（建议3 顺带修）。
    column_key = topic.get("suggested_column") or ""
    angle_guidance = {
        "战术榜单": "从战术层面拆解这场较量的胜负手，但**落点在人的选择**"
                    "（主帅怎么排、谁先变阵、球员怎么执行），用生活类比解释一个反常识发现。",
        "数据对照": "用数据对比呈现两队的境遇落差，每个数据点配一句毒舌点评，"
                    "最后把落差归结到**具体的人**身上。",
        "人物故事": "聚焦弱势一方（或弱侧）的主帅/关键球员故事，写他的处境与变数，"
                    "有人味、有细节、少评论多展示。",
    }.get(column_key or angle_type, "围绕核心冲突点做有态度的分析，落点在人的处境。")
    # 人视角叙事要求（R11「写人 > 写事」）——由 fixture_library.ANGLE_RULES 随选题下发
    person_hint = topic.get("angle_hint", "") or "落点在人的处境（主帅/核心球员），不写成纯对阵分析。"

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝，犀利、有数据感、不骑墙。

今天要写一篇「赛程确定性选题」——不是赛果复盘，而是基于即将到来的比赛，从冲突点出发做战术/数据/人物分析。

## 事实卡（唯一事实来源，严禁编造卡外数据）
{fact_card}

## 选题角度
类型：{angle_type}（建议栏目：{topic.get('suggested_column', '')}）
核心冲突：{topic.get('conflict', '')}
写法指引：{angle_guidance}
人视角要求（R11：写人 > 写事，同线索点击差 3-10 倍）：{person_hint}

## 内容参照层（计划 13.3：只抄方法论，不抄选题）
{content_reference.exclusion_guidance()}
- 可参照的方法论（借方法、不搬选题）：标题内携带信源（如「罗马诺：」）、
  信源+动作+数字/人名的信息结构、话题时效判断、固定栏目形态。

今天是 {date_str or '今日'}，比赛在北京时间 {topic.get('kickoff_cst', '')} 开球。
{prefix_hint}
写作硬规则（严格按计划 v1.0 第三章/第五章/第七章）：
1. 标题公式（R11 回测口径）：以**具体的人或球队** + **悬念词**（没想到／原来／竟然／居然／反转，
   回测点击率 +87%）开头，以疑问『？』收尾；字数严格落在 26~30 字（含标点）。
   **严禁写成「A队 vs B队」的对阵前瞻或纯战术罗列**——同一条赛程线索「写人」比「写事」
   高 3-10 倍点击；把镜头对准主帅/核心球员的处境与选择，而不是两队的强弱对比。
   严禁『XX前瞻』『赛前分析』等模板化标题（赛事前瞻已停发，计划 7.1 验收：前瞻体占比为零）。
2. 所有数据只能来自上方事实卡；卡里没有的（具体比分、转会、伤病、历史交锋、合同金额）一律不写，宁可不提也不编造。
3. 语气像老球迷喝酒聊天，有明确立场和情绪，不套模板。
4. 依板块落栏：{content_type}（计划 6.2 板块：转会动态／人物故事／中国足球／战术榜单）。
5. 文末必须包含两类固定内容：
   (a) 互动钩子，引评论区讨论；
   (b) 一段「老六视角」——基于上述事实卡给出的独立判断（只做判断、不编造任何卡外数据），
       既是计划 4.2 要求的「基于事实的独立判断」（满足实质人工介入判定、形成账号人设），
       也体现计划 13.5「观点增量」差异化方向。
6. 文末另须显式标注（计划 4.1 失实表述对策、4.2 反向利好）：
   「⚠️ 本文由 AI 生成，数据来源：football-data.org / 聚合数据，赛程与数据以官方为准；文中观点为基于公开数据的独立分析，仅供参考。」

输出纯JSON:
{{"title": "标题(26-30字，具体对阵/人名+冲突反差开头，必须以疑问？收尾)", "content": "Markdown正文(含##小标题，600-900字，结尾含老六视角与AI生成标注)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "站队式/投票式/挑战式/共鸣式", "interaction_bait": "互动问题", "content_type": "{content_type}"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是头条号足球博主'球评人老六'，犀利、有数据感。事实层/表达层隔离：只基于给定事实卡表达，不编造任何卡外数据。标题必须自然不套模板。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]

    for attempt in range(3):
        try:
            article, _model = call_llm_json(messages, LLM_JSON_CANDIDATES,
                                            temperature=0.7, max_tokens=4096)
            if not isinstance(article, dict) or not article.get("title"):
                if attempt < 2:
                    print(f"   ⚠️ 确定性供给解析失败 (attempt {attempt + 1}/3)，重试...")
                    continue
                return None
            content = article.get("content", "")
            if len(content) < 200:
                if attempt < 2:
                    print(f"   ⚠️ 确定性供给正文仅{len(content)}字 (attempt {attempt + 1}/3)")
                    continue
                return None
            title = (article.get("title", "") or "").strip()
            # 句式闸门（P0-3）：近 14 天某句式已满额 → 不许再写同款。
            # 源头已改成「每型 3 条模板按日轮换」，这里再兜一层出口检查：
            # 模型仍可能自己收敛回「主帅压力体」，那种标题读者一眼就腻。
            _sig = template_gate.signature_of(title)
            if _sig and avoid_signatures and _sig in avoid_signatures:
                if attempt < 2:
                    print(f"   ⚠️ 确定性供给标题命中已满句式「{_sig}」（14天≥"
                          f"{template_gate.MAX_PER_SIGNATURE}篇），重生成 (attempt {attempt + 1}/3)...")
                    continue
                degrade.note("标题句式满额", f"{_sig} 已用满 {template_gate.MAX_PER_SIGNATURE} 篇",
                             action="兜底放行，记入待复盘（不阻塞发布）")
            # 评分器及格线（计划 5.1/7.2）：低于 60 分打回重生成，最多两次（attempt 0,1 重试，第 3 次兜底放行）
            s_score, s_reasons = score_fixture_title(topic, title, batch_mode)
            if s_score < 60:
                if attempt < 2:
                    print(f"   ⚠️ 确定性供给标题评分 {s_score} 未过及格线(60) [{','.join(s_reasons)}] (attempt {attempt + 1}/3)，重生成...")
                    continue
                print(f"   ⚠️ 确定性供给标题评分 {s_score} 仍不过，兜底放行（不阻塞发布）")
            if recent_prefixes and title[:6] in recent_prefixes:
                if attempt < 2:
                    print(f"   ⚠️ 确定性供给标题前缀复读「{title[:6]}」，重试 (attempt {attempt + 1}/3)...")
                    continue
                stripped = title[6:].lstrip("：:，, -—").strip("：:，, -— ")
                title = stripped or title
            # 互动引导硬校验（R11 建议5 / 计划 13.5）：缺关注/互动引导 → 打回重写；
            # 最后一轮仍缺则放行，交由保存侧模板兜底注入
            _ib = check_interaction_bait(article)
            if not _ib["ok"]:
                if attempt < 2:
                    print(f"   ⚠️ 确定性供给互动引导未达标（缺 {'、'.join(_ib['missing'])}）"
                          f"(attempt {attempt + 1}/3)，重生成...")
                    continue
                degrade.note("互动引导未达标", f"缺 {'、'.join(_ib['missing'])}",
                             action="保存侧模板兜底注入（不丢弃成稿）")
            article["title"] = title
            article["content_type"] = content_type
            article["板块"] = topic.get("板块", content_type)
            article["_is_fixture"] = True
            src = ("聚合数据 juhe.cn (中超赛程)" if topic.get("competition") == "中超"
                   else "football-data.org/v4 (赛程/积分榜/射手榜)")
            article["sources_used"] = [src]
            article["topic_id"] = topic.get("topic_id", "")
            print(f"   ✅ 确定性供给生成成功(score={s_score}): {article['title'][:50]} ({len(content)}字)")
            return article
        except Exception as e:
            if attempt < 2:
                print(f"   ⚠️ 确定性供给生成异常 (attempt {attempt + 1}/3): {e}")
                continue
            print(f"   ❌ 确定性供给生成失败: {e}")
            return None


def generate_recap_article(topic, date_str=None, recent_prefixes=None, batch_mode="auto",
                           avoid_signatures=None):
    """隔夜战报：已完赛赛果 → 赛后复盘稿（二级·突发，P0-1）。

    与 generate_fixture_article 的根本区别：
      - fixture 是**赛前**（未开赛，讲「会发生什么」），事实层只有积分榜/射手榜；
      - recap 是**赛后**（已完赛，讲「为什么是这个结果」），事实层有确切比分与进球。
    两者共用配额与评分器，但提示词、事实卡、板块都不同，不能合并。

    事实层/表达层隔离：比分、半场、进球者、进球时间只能来自事实卡；
    卡里没有的（控球率、射门数、伤停、红黄牌、教练发言）一律不写。
    """
    if not topic:
        return None
    sc = topic.get("score") or {}
    if sc.get("home") is None or sc.get("away") is None:
        print("   ⚠️ 战报选题缺比分，跳过（不用 0 冒充）")
        return None
    content_type = topic.get("板块") or "战术榜单"
    fact_card = _build_recap_fact_card(topic)

    prefix_hint = ""
    if recent_prefixes:
        sample = list(recent_prefixes)[:12]
        prefix_hint = (
            "\n⚠️ 标题防重铁律：\n"
            f"- 近7天已用过的标题开头（前6字）不可再用：{sample}\n"
            "- 特别注意：本报近期赛前稿大量使用「XX主帅这场压力/输不起/没想到XX主帅」句式，"
            "战报**一律不得**再用这类句式开头（同一句式 14 天内最多 2 篇）。\n"
        )

    prompt = f"""你是头条号足球博主"球评人老六"，10万粉丝，犀利、有数据感、不骑墙。

今天要写一篇**赛后复盘**——比赛已经踢完了，读者要的是「为什么会是这个结果」，
而不是赛前预测、也不是流水账式复述进球过程。

## 事实卡（唯一事实来源，严禁编造卡外数据）
{fact_card}

## 选题角度
核心：{topic.get('conflict', '')}
角度要求：{topic.get('angle_hint', '')}

## 内容参照层（计划 13.3：只抄方法论，不抄选题）
{content_reference.exclusion_guidance()}

比赛在北京时间 {topic.get('kickoff_cst', '')} 开球，今晨已经结束。
{prefix_hint}
写作硬规则（严格按计划 v1.0 第三章/第五章/第七章）：
1. 标题：**一律不带比分、不写「A队2-1B队」这类赛果式开头**。
   R11 回测（869 条真实导出）里，比分/赛果式标题产量占 23.9%、CTR 仅 0.49%、0 爆款，
   是全部形态里最差的一类——标题可以写「谁绝杀」，但不能写「2-1」。
   正确做法：从**人的处境或选择**切入（谁在第 87 分钟那一下做了错的选择、谁被换下场后
   比赛才转向、谁扛下了那个点球），26~30 字（含标点），以疑问『？』收尾，
   必须出现至少一支球队名或一名当事人。比分的交代放到正文第一段。
   禁止「XX前瞻」「赛后分析」「复盘：」这类模板前缀，禁止与近期赛前稿同句式。
2. 比分唯一口径：全文出现比分时必须与事实卡「最终比分」完全一致（主队在前、客队在后）。
   可写「2-1」，也可写「2比1」，但**方向和数字不得改动**，不得出现第二个互相矛盾的比分。
3. 进球信息只写事实卡列出的（分钟、球员、所属球队、点球/乌龙属性）；卡里没有的
   控球率、射门数、传球成功率、红黄牌、伤停、教练发言一律不写。
   球员名严格照抄事实卡写法（含标注为「无中文译名」的拉丁字母原名，禁止自行音译）。
4. 复盘要落到**人的选择**：谁换上了谁、谁在第几个球前后改变了踢法、谁承担了后果。
   不要逐分钟复述进球，要解释「这个比分是怎么被做出来的」。
5. 语气像老球迷喝酒聊天，有明确立场和情绪，不套模板；可对结果下判断、可批评。
6. 依板块落栏：{content_type}（计划 6.2 板块：转会动态／人物故事／中国足球／战术榜单）。
   **战报归「战术榜单」，不要写成人物故事**——本篇的价值是「结果为什么这样」，不是某人的生平。
7. 文末必须包含两类固定内容：
   (a) 互动钩子，引评论区讨论；
   (b) 一段「老六视角」——基于上述事实卡给出的独立判断（只做判断、不编造任何卡外数据）。
8. 文末另须显式标注（计划 4.1 失实表述对策、4.2 反向利好）：
   「⚠️ 本文由 AI 生成，数据来源：football-data.org，赛果与数据以官方为准；文中观点为基于公开数据的独立分析，仅供参考。」

输出纯JSON:
{{"title": "标题(26-30字，赛果反差+具体的人，必须以疑问？收尾)", "content": "Markdown正文(含##小标题，600-900字，结尾含老六视角与AI生成标注)", "summary": "50字摘要", "keywords": ["英文关键词"], "keywords_cn": ["中文关键词"], "golden_lines": ["金句1", "金句2"], "interaction_type": "站队式/投票式/挑战式/共鸣式", "interaction_bait": "互动问题", "content_type": "{content_type}"}}
只输出JSON。"""

    messages = [
        {"role": "system", "content": "你是头条号足球博主'球评人老六'，犀利、有数据感。事实层/表达层隔离：只基于给定事实卡表达，比分与进球不得编造或改动。标题必须自然不套模板。只输出JSON。"},
        {"role": "user", "content": prompt}
    ]

    for attempt in range(3):
        try:
            article, _model = call_llm_json(messages, LLM_JSON_CANDIDATES,
                                            temperature=0.7, max_tokens=4096)
            if not isinstance(article, dict) or not article.get("title"):
                if attempt < 2:
                    print(f"   ⚠️ 战报解析失败 (attempt {attempt + 1}/3)，重试...")
                    continue
                return None
            content = article.get("content", "")
            if len(content) < 200:
                if attempt < 2:
                    print(f"   ⚠️ 战报正文仅{len(content)}字 (attempt {attempt + 1}/3)")
                    continue
                return None
            title = (article.get("title", "") or "").strip()

            # 句式闸门（P0-3）：赛前稿已经把「主帅压力体」写满了，战报绝不能接着写
            _sig = template_gate.signature_of(title)
            if _sig and avoid_signatures and _sig in avoid_signatures:
                if attempt < 2:
                    print(f"   ⚠️ 战报标题命中已满句式「{_sig}」，重生成 (attempt {attempt + 1}/3)...")
                    continue
                degrade.note("战报标题句式满额", f"{_sig} 已用满",
                             action="兜底放行，记入待复盘")

            # R11 硬数据：比分/赛果式标题产量占 23.9%、CTR 仅 0.49%、0 爆款。
            # 战报是最容易滑回这种形态的体裁（「皇马2-1巴萨」最顺手），
            # 故标题出现比分一律打回——比分放正文第一段，标题写人。
            if _extract_scores(title):
                if attempt < 2:
                    print(f"   ⚠️ 战报标题含比分（R11：比分式标题 0 爆款），重生成 "
                          f"(attempt {attempt + 1}/3)...")
                    continue
                degrade.note("战报标题含比分", f"「{title[:20]}」",
                             action="兜底放行，但记入待复盘（R11 口径：比分式标题 CTR 0.49%）")

            # 比分一致性硬校验：正文里出现的比分只能是「本场全场比分」或「本场半场比分」
            # （各自允许主客颠倒写法）。挡住三类事故：① 把 2-1 写成 3-1（编造）；
            # ② 编造历史交锋比分；③ 编造单场进球数。宁缺勿错——战报的核心事实就是比分。
            # 半场比分必须在白名单里：事实卡给了它，正文必然会写「半场 1-0」，
            # 只允许全场比分会把它当卡外数据打回，等于自己给自己制造熔断。
            hg, ag = int(sc["home"]), int(sc["away"])
            _allowed_scores = {(hg, ag), (ag, hg)}
            if sc.get("ht_home") is not None and sc.get("ht_away") is not None:
                _th, _ta = int(sc["ht_home"]), int(sc["ht_away"])
                _allowed_scores |= {(_th, _ta), (_ta, _th)}
            bad_scores = [s for s in _extract_scores(title + "\n" + content)
                          if s not in _allowed_scores]
            if bad_scores:
                if attempt < 2:
                    print(f"   ⚠️ 战报出现卡外比分 {bad_scores}（本场 {hg}-{ag}），重生成 (attempt {attempt + 1}/3)")
                    continue
                print(f"   ❌ 战报仍出现卡外比分 {bad_scores}，丢弃（宁缺勿错）")
                return None

            s_score, s_reasons = score_fixture_title(topic, title, batch_mode)
            if s_score < 60:
                if attempt < 2:
                    print(f"   ⚠️ 战报标题评分 {s_score} 未过及格线(60) [{','.join(s_reasons)}]，重生成...")
                    continue
                print(f"   ⚠️ 战报标题评分 {s_score} 仍不过，兜底放行")
            if recent_prefixes and title[:6] in recent_prefixes:
                if attempt < 2:
                    print(f"   ⚠️ 战报标题前缀复读「{title[:6]}」，重试...")
                    continue
            _ib = check_interaction_bait(article)
            if not _ib["ok"]:
                if attempt < 2:
                    print(f"   ⚠️ 战报互动引导未达标（缺 {'、'.join(_ib['missing'])}），重生成...")
                    continue
                degrade.note("战报互动引导未达标", f"缺 {'、'.join(_ib['missing'])}",
                             action="保存侧模板兜底注入（不丢弃成稿）")

            article["title"] = title
            article["content_type"] = content_type
            article["板块"] = content_type
            # 层级标签必须显式：供 supply_mix.classify 判定（显式标签优先）。
            # 战报是二级·突发（硬新闻），**不是**一级·确定性——若漏填会被
            # `_is_fixture` 推断成一级，一级每日预算与配比就全乱了。
            article["_supply_layer"] = "二级·突发"
            article["_is_recap"] = True
            article["_recap_match_id"] = topic.get("match_id")
            article["sources_used"] = ["football-data.org/v4 (赛果)"]
            article["topic_id"] = topic.get("topic_id", "")
            print(f"   ✅ 战报生成成功(score={s_score}): {article['title'][:50]} ({len(content)}字)")
            return article
        except Exception as e:
            if attempt < 2:
                print(f"   ⚠️ 战报生成异常 (attempt {attempt + 1}/3): {e}")
                continue
            print(f"   ❌ 战报生成失败: {e}")
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

    # 计划第十章 · 合规红线熔断：合规指标是硬门槛，不实违规出现一次即暂停自动发布
    try:
        import compliance_guard
        _cg = compliance_guard.status()
        if _cg["suspended"]:
            _msg = (f"合规熔断中，自动发布已暂停：{_cg['reason']}（自 {_cg['since']}）。"
                    f"人工定位原因后运行 `python3 compliance_guard.py --clear` 解除。")
            print(f"   ⛔ {_msg}")
            log.error(_msg)
            try:
                send_wxpusher("足球自媒体 ⛔ 合规熔断",
                              f"{date_str} 发文已暂停：{_cg['reason']}")
            except Exception:
                pass
            return
        # 新批次开始，重置自动熔断计数器
        _CONSISTENCY_REJECTS["count"] = 0
        _CONSISTENCY_REJECTS["suspended"] = False
    except Exception as e:
        print(f"   ⚠️ 合规熔断检查异常（按不熔断继续）: {e}")

    # Load season weights for content type optimization
    season_weights, season_label = load_season_weights(date_str)

    if batch_mode in BATCH_CONFIG:
        batch_cfg = BATCH_CONFIG[batch_mode]
        slots = batch_cfg["slots"]
        # 单批篇数纪律（策略手册第五章 / 计划 12.1）：常态 2 篇，
        # 仅周四或 S 级事件可显式加发；上限由 batch_quota.decide 统一裁定。
        quota = _resolve_batch_quota(batch_mode, date_str)
        max_articles = quota["max_articles"]
        article_count = max_articles  # 初始值用于打印；select_topics 返回后按实际话题数动态裁剪
        # Columns are fixed per batch — no season weight type swapping
        # Season weights only affect topic selection framing, not column identity
        column_names = [s["column_name"] for s in slots]
        print(f"足球自媒体内容自动化 - {date_str} (batch={batch_mode}, 栏目={', '.join(column_names)}, {batch_cfg['name']}·{batch_cfg['time']}, 单批上限={max_articles}篇 · {quota['detail']})\n")
        target_types = None  # Column-driven, not type-driven
    else:
        # auto / 未配置批次：BATCH_CONFIG 里没有 "auto"，
        # 但下游「紧急球评」分支(batch_mode in ("auto","morning")) 会读 batch_cfg["name"]，
        # 不在此绑定会导致 UnboundLocalError（2026-09-27 生成文章 失败根因）。
        quota = _resolve_batch_quota(batch_mode, date_str)
        max_articles = quota["max_articles"]
        batch_cfg = {"name": "自动", "time": "", "max_articles": max_articles}
        article_count = max_articles
        target_types = None
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

        # 事实卡编译与落库（计划 6.1 / 12.2）：素材 → 结构化事实卡，可溯源、带生命周期。
        # 非阻断：失败仅告警，绝不阻塞发布（计划九）。
        try:
            from fact_card import compile_and_persist
            _fc_store, _fc_stats = compile_and_persist(match_data, date_str=date_str)
            print(f"   🗂️ 事实卡入库: 共 {_fc_stats['total']} 张"
                  f"（新增 {_fc_stats['added']} / 更新 {_fc_stats['updated']}，"
                  f"可信度 {_fc_stats['可信度']}，生命周期 {_fc_stats['生命周期']}）")
        except Exception as e:
            print(f"   ⚠️ 事实卡编译异常（不影响主流程）: {e}")
            degrade.note("事实层", f"事实卡编译失败: {str(e)[:60]}",
                         action="跳过该条事实，用剩余事实装配，不足则减少当日篇数")

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
        # 请求上限为 max_articles，让 LLM 在素材丰富时多选、素材匮乏时少选
        topics = select_topics(match_data, topic_history=topic_history,
                               preferred_types=target_types,
                               season_weights=season_weights,
                               cross_batch_covered=cross_batch_covered,
                               season_label=season_label,
                               topic_count=max_articles,
                               yesterday_keywords=yesterday_keywords)
        extra_meta = {"type": "match_analysis", "quota": quota}
        _assign_columns_to_topics(topics, batch_mode)

        # 计划 11.2 择优模型：L1（时效/配比/冷门度）→ L2 信号增强（信源前移 + 社媒/搜索）
        # → L3 五维模型打分重排 → L4 缓存与限额。仅重排、不删题；模型/信号不可用时
        # 自动降级为规则排序（计划九，非阻断）。
        try:
            _rank_res = _rank_topics(topics, match_data, date_str)
            extra_meta["ranker_mode"] = _rank_res.get("mode", "rule")
        except Exception as e:
            print(f"   ⚠️ 择优排序异常（跳过，不影响发布）: {e}")

        # ============================================================
        # 标题句式闸门（P0-3）：算出「近 14 天已用满」的句式，供生成侧打回
        # ============================================================
        # 供给侧已改成「每型 3 条模板 + 按日轮换」，但模型仍会自己收敛回同一句式，
        # 所以在出口再拦一次：命中已满句式的标题打回重写（最多 3 轮，兜底放行并留痕）。
        _avoid_sigs: set = set()
        try:
            _avoid_sigs = template_gate.saturated_signatures(
                template_gate.recent_titles(OUTPUT_DIR, date_str))
            if _avoid_sigs:
                print(f"   🚧 标题句式闸门：近 {template_gate.DEFAULT_WINDOW_DAYS} 天已用满 "
                      f"{sorted(_avoid_sigs)}（阈值 {template_gate.MAX_PER_SIGNATURE} 篇）")
        except Exception as e:
            print(f"   ⚠️ 句式闸门计算异常（跳过，不影响发布）: {e}")

        # ============================================================
        # 隔夜战报（P0-1，二级·突发）——先于赛程选题取用
        # ============================================================
        # 病根：赛程池只收未开赛场次（旧闻新发红线），已完赛比分**没有入口**，
        # 08:00 晨读批只能拿蓄水池软性内容补位（10-12~10-25 晨读 14 篇里
        # 12 篇软性、0 篇战报）。而「今晨刚踢完的比赛」是晨读最硬的供给。
        # 战报不占一级·确定性预算（det_cap），它是突发层；批内仍受 max_articles 约束。
        recap_added = 0
        if batch_mode in RECAP_BATCHES and len(articles) < max_articles:
            try:
                _sent_ids = _recap_sent_today(date_str)
                _recaps = collect_recap_topics(
                    date_str, max_topics=RECAP_PER_BATCH,
                    sent_match_ids=_sent_ids,
                    covered_titles=(cross_batch_covered or {}).get("titles"))
                for rc in _recaps:
                    r_art = generate_recap_article(
                        rc, date_str=date_str,
                        recent_prefixes=topic_history.get("title_prefixes"),
                        batch_mode=batch_mode,
                        avoid_signatures=_avoid_sigs)
                    if not r_art:
                        degrade.note("隔夜战报", f"{rc.get('home_zh')} vs {rc.get('away_zh')} 生成失败",
                                     action="本批不补战报，不拿软性内容顶替")
                        continue
                    img_topic = {
                        "title": r_art.get("title", ""),
                        "keywords_cn": [rc.get("home_zh", ""), rc.get("away_zh", "")],
                        "keywords": ["football", (rc.get("competition") or "match")],
                    }
                    images_map[len(articles)] = search_images(img_topic, count=3)
                    r_art["_batch_name"] = batch_cfg["name"]
                    r_art["_batch_time"] = batch_cfg["time"]
                    stats["generated"] += 1
                    stats["valid"] += 1
                    warn_if_low_info_increment(r_art)
                    articles.append((len(articles), r_art))
                    topics.append({
                        "title": r_art.get("title", ""),
                        "content_type": r_art.get("content_type", ""),
                        "_supply_layer": "二级·突发",
                        "_is_recap": True,
                    })
                    print(f"   📰 隔夜战报已追加: {r_art['title'][:50]}")
                    recap_added += 1
            except Exception as e:
                print(f"   ⚠️ 隔夜战报生成异常 (不影响主流程): {e}")

        # ============================================================
        # 一级·确定性供给（赛程冲突点选题 + 节点事件）——占用批次配额（P0 修法 A）
        # ============================================================
        # 计划 12.1 / 手册第五章：单批上限由 batch_quota 裁定。此前确定性供给
        # **追加在 articles 末尾、不占 max_articles**，实际日更冲到 10.5 篇、
        # 远超手册日 6 篇（R13 评估定为 P0）。现改为**先于主稿生成并占用配额**：
        # 主稿只填剩余槽位，单批成稿总数（不含未发布的晚间预测稿）永不超过
        # max_articles。每日预算 DETERMINISTIC_DAILY_BUDGET 篇保计划 ~30% 配比。
        # 与 Pipeline A 解耦：不依赖直播吧/懂球帝源文章，scraper 失效时也能产出
        # 真实、时效、有冲突点的内容。
        _main_topic_count = len(topics)
        fixture_added = 0
        det_cap = min(FIXTURE_PER_BATCH,
                      max(0, DETERMINISTIC_DAILY_BUDGET - _deterministic_used_today(date_str)),
                      # 为「主稿」保留至少 1 个槽位：战报占位后（晨读批 1 战报 + 1 主稿）
                      # 确定性供给必须让位，否则晨读会变成「1 战报 + 1 赛程稿」，
                      # 把本批唯一的主稿位挤掉，热点球评反而更少（正是要治的病）。
                      max(0, max_articles - len(articles) - 1))
        if FIXTURE_PER_BATCH > 0 and det_cap <= 0:
            _why = ("今日已用满每日预算 %d 篇（计划 12.1 一级·确定性 ~30%%）" % DETERMINISTIC_DAILY_BUDGET
                    if _deterministic_used_today(date_str) >= DETERMINISTIC_DAILY_BUDGET
                    else "本批槽位需留给主稿与战报，确定性供给让位")
            print(f"   🗓️ 确定性供给本批让位：{_why}")
        if FIXTURE_PER_BATCH > 0 and det_cap > 0:
            try:
                fixture_topics = collect_fixture_topics(
                    date_str, batch_mode=batch_mode, max_topics=det_cap,
                    lookahead_days=5, topic_history=topic_history,
                    cross_batch_covered=cross_batch_covered)
                if not fixture_topics:
                    # 计划九·赛程源不可用：退化为纯新闻流驱动，选题池仅由事实卡装配
                    degrade.note("赛程源", "本批无可用赛程选题（源不可用或无匹配）",
                                 action="退化为纯新闻流驱动，选题池仅由事实卡装配")
                for ft in fixture_topics:
                    facts = gather_fixture_facts(ft)
                    f_art = generate_fixture_article(
                        ft, facts, date_str=date_str,
                        recent_prefixes=topic_history.get("title_prefixes"),
                        topic_history=topic_history, batch_mode=batch_mode,
                        avoid_signatures=_avoid_sigs)
                    if f_art:
                        img_topic = {
                            "title": f_art.get("title", ""),
                            "keywords_cn": [ft.get("home_zh", ""), ft.get("away_zh", "")],
                            "keywords": ["football", (ft.get("competition") or "match")],
                        }
                        f_imgs = search_images(img_topic, count=3)
                        images_map[len(articles)] = f_imgs
                        stats["generated"] += 1
                        stats["valid"] += 1
                        warn_if_low_info_increment(f_art)
                        articles.append((len(articles), f_art))
                        topics.append({
                            "title": f_art.get("title", ""),
                            "content_type": f_art.get("content_type", ""),
                            "_is_fixture": True,
                        })
                        print(f"   🗓️ 确定性供给已追加: {f_art['title'][:50]}")
                        fixture_added += 1
                    else:
                        print("   ℹ️ 确定性供给选题生成跳过（无有效素材或生成失败）")
            except Exception as e:
                print(f"   ⚠️ 确定性供给生成异常 (不影响主流程): {e}")

        # 计划 12.1 一级·确定性第二类：节点事件（转会窗截止日/赛季节点/开赛倒计时）。
        # 由 scripts/pregen_deterministic.py 提前 7 天量产入库；此处直接取用，不再调用 LLM。
        # 与赛程类共用一级配额与每日预算（det_cap），库存为空即静默跳过（非阻断）。
        if det_cap > 0 and fixture_added < det_cap:
            try:
                import deterministic_supply
                slots = max(0, det_cap - fixture_added)
                node_arts = deterministic_supply.fill_gap(slots, date_str=date_str)
                for na in node_arts:
                    if not na.get("title") or len(na.get("content", "")) < 200:
                        continue
                    n_art = {
                        "title": na.get("title", ""),
                        "content": na.get("content", ""),
                        "summary": na.get("summary", ""),
                        "keywords": na.get("keywords", []),
                        "keywords_cn": na.get("keywords_cn", []),
                        "content_type": na.get("content_type", ""),
                        "_column_name": na.get("section", ""),
                        "_title_score": na.get("_title_score"),
                        "_is_fixture": True,
                        "_supply_layer": "一级·确定性·节点",
                    }
                    img_topic = {
                        "title": n_art.get("title", ""),
                        "keywords_cn": n_art.get("keywords_cn", []),
                        "keywords": n_art.get("keywords", ["football"]),
                    }
                    images_map[len(articles)] = search_images(img_topic, count=3)
                    stats["generated"] += 1
                    stats["valid"] += 1
                    articles.append((len(articles), n_art))
                    topics.append({
                        "title": n_art.get("title", ""),
                        "content_type": n_art.get("content_type", ""),
                        "_is_fixture": True,
                        "_supply_layer": "一级·确定性·节点",
                    })
                    print(f"   🗓️ 一级·确定性节点供给已取用: {n_art['title'][:50]}"
                          f"（{na.get('node_type', '')}）")
            except Exception as e:
                print(f"   ⚠️ 节点确定性供给取用异常 (不影响主流程): {e}")

        # 动态条数：确定性供给已占用配额（P0 修法 A），主稿只填剩余槽位
        # （常态 2 篇；周四或 S 级事件按 batch_quota 加发时上限提高，见本函数开头 quota）
        _main_slots = max(0, max_articles - fixture_added - recap_added)
        article_count = min(_main_topic_count, _main_slots)
        if fixture_added:
            print(f"   🧮 配额分配：单批上限 {max_articles} 篇 = 确定性 {fixture_added} + 主稿槽位 {_main_slots}")
        if _main_slots > 0 and article_count >= _main_slots:
            print(f"   📈 动态条数：本批主稿生成 {article_count} 篇（剩余槽位 {_main_slots}）")
        elif article_count > 0:
            print(f"   📉 动态条数：本批实际高质量话题 {_main_topic_count} 个，主稿生成 {article_count} 篇（剩余槽位 {_main_slots}）")
        else:
            print(f"   ⚠️ 动态条数：LLM 未返回可用话题，本批主稿 0 篇（将走应急/空批次兜底）")

        # Generate articles: find source article for each topic, rewrite to 老六 style
        # index_offset：确定性文章已占前 fixture_added 个位置，主稿从其后编号
        _generate_articles_from_topics(topics, article_count, match_data,
                                       images_map, stats, articles, date_str=date_str,
                                       index_offset=len(articles))

        # 加发留痕（策略手册第五章 / 计划 12.1）：只有真正加发才记账，
        # 供验收核查「周内加发 ≤ 2 次、原因可回溯」。失败仅告警，不阻塞发布。
        # 记账篇数 = 本批实际发布总数（主稿 + 确定性供给；预测稿不发布不计）。
        if quota.get("uplifted"):
            try:
                import batch_quota
                batch_quota.record(date_str, batch_mode, quota.get("reason") or "",
                                   articles=article_count + fixture_added,
                                   note=os.environ.get("FOOTBALL_UPLIFT_NOTE", ""))
                print(f"   📌 加发留痕：本批 {article_count + fixture_added} 篇"
                      f"（主稿 {article_count} + 确定性 {fixture_added}；原因 {quota.get('reason')}，"
                      f"周内已用 {quota.get('weekly_used')}/{quota.get('weekly_limit')}）")
            except Exception as e:
                print(f"   ⚠️ 加发留痕失败（不影响发布）: {e}")

        # ============================================================
        # 三级·软性蓄水池补位（计划 12.1）
        # ============================================================
        # 主稿供给不足（LLM 少选/源文缺失）时，由备货池中的软性内容补位，
        # 保证突发日不断供；突发密集时池中内容自然顺延（当日用不到即留库）。
        #
        # P0-2（2026-10-26）：**晨读批禁止软性补位**。
        # 实测 10-12~10-25 晨读 14 篇里 12 篇是蓄水池软性内容、0 篇战报——
        # 早晨本该吃「隔夜赛果 + 通勤时段硬新闻」，结果天天端人物故事/历史回顾，
        # 这是「推文全是人物故事」最直接的来源之一。软性只进午间/晚间，
        # 且单日不超过 SOFT_DAILY_LIMIT 篇（晚间最多，午间 1 篇）。
        _supply_gap = max_articles - len(articles)
        _soft_reason = ""
        if batch_cfg["name"] == "晨读":
            _soft_reason = "晨读批禁用软性补位（P0-2：晨读吃隔夜赛果+硬新闻）"
        elif _soft_used_today(date_str) >= SOFT_DAILY_LIMIT:
            _soft_reason = f"今日软性已用满 {SOFT_DAILY_LIMIT} 篇（P0-2 单日上限）"
        if _supply_gap > 0 and _soft_reason:
            print(f"   🗄️ 蓄水池补位跳过：{_soft_reason}；本批缺口 {_supply_gap} 篇不补")
            degrade.note("软性补位跳过", _soft_reason,
                         action="本批篇数不足，按实际成稿发布（不拿软性顶替）")
        elif _supply_gap > 0:
            try:
                import reservoir
                reservoir.expire()
                _picked = reservoir.fill_gap(_supply_gap)
                for _it in _picked:
                    _art = _it.get("article") or {}
                    if not _art:
                        continue
                    _art["_from_reservoir"] = True
                    _art["_batch_name"] = batch_cfg["name"]
                    _art["_batch_time"] = batch_cfg["time"]
                    _art["_column_name"] = _art.get("_column_name") or _it.get("section", "")
                    articles.append((len(articles), _art))
                    images_map[len(articles) - 1] = []
                    topics.append({"title": _art.get("title", ""),
                                   "content_type": _art.get("content_type", "人物故事"),
                                   "_column_name": _art["_column_name"],
                                   "_from_reservoir": True})
                    stats["generated"] += 1
                    stats["valid"] += 1
                if _picked:
                    print(f"   🗄️ 蓄水池补位：{len(_picked)} 篇软性备货转正（主稿缺口 {_supply_gap} 篇）")
            except Exception as e:
                print(f"   ⚠️ 蓄水池补位异常（跳过，不影响发布）: {e}")

        # ============================================================
        # Prediction Article — 晚间批次生成明日赛前预测
        # ============================================================
        if batch_mode == "evening":
            print("\n[预测] 晚间批次：采集明日赛程，生成赛前预测...")
            future_matches = collect_future_matches(date_str, days_ahead=1)
            if future_matches:
                pred_art = generate_prediction_article(
                    future_matches, date_str=date_str,
                    recent_prefixes=topic_history.get("title_prefixes"))
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
                    # P0-3: 信息增量非阻断告警（预测稿同样检查）
                    warn_if_low_info_increment(pred_art)
                    # 预测稿必须自带「不发布」标记与批次归属：publisher 靠 frontmatter
                    # 的 is_prediction 显式跳过（R13 决策④）。此前标记只写进 topics、
                    # 没落到文章对象，靠「batch_name 为空被批次过滤」意外挡住——一旦
                    # 该批过滤 0 命中，publisher 会降级发布全部文章，预测稿就被当真稿
                    # 发出去（2026-10-25 晚间实证：metadata 里 is_prediction=false、
                    # batch_name 为空，只差这一步就漏发）。
                    pred_art["_is_prediction"] = True
                    pred_art["_batch_name"] = batch_cfg["name"]
                    pred_art["_batch_time"] = batch_cfg["time"]
                    articles.append((len(articles), pred_art))
                    topics.append({"title": pred_art.get("title", ""),
                                   "content_type": "热点球评",
                                   "_batch_name": batch_cfg["name"],
                                   "_is_prediction": True})
                    print(f"   🎯 赛前预测已追加: {pred_art['title'][:50]}")
                else:
                    print("   ℹ️ 赛前预测生成跳过（无有效素材或生成失败）")
            else:
                print("   ℹ️ 明日无赛程，跳过赛前预测")

        # ============================================================
        # （一级·确定性供给已移至主稿生成之前，占用批次配额 —— P0 修法 A，见上）
        # ============================================================

        # ============================================================
        # Hupu Pipeline (articles 4-6, top 3 hottest posts)
        # ============================================================
        # Major Event Detection: generate emergency article if high-urgency event found
        # ============================================================
        if match_data["total_matches"] > 0:
            major_events = detect_major_events(match_data)
            urgent_events = [e for e in major_events if e["urgency"] >= 70]
            # Only trigger emergency in non-batch mode or morning batch (avoid duplicates)
            # 配额守卫（P0 修法 A）：本批已满则紧急球评让位，不突破单批上限
            # 熔断守卫（R18）：本批已触发合规熔断则紧急球评同样不生成（宁停勿错）
            if urgent_events and batch_mode in ("auto", "morning") and len(articles) < max_articles \
                    and not _breaker_active():
                top_event = urgent_events[0]
                e_idx = len(articles) + 1
                e_imgs = search_images({"title": top_event.get("title_hint", ""),
                                        "keywords_cn": [top_event.get("league", "足球")]}, count=3)
                images_map[len(articles)] = e_imgs
                e_art, e_err = generate_article_with_retry(
                    {"title": top_event.get("title_hint", ""),
                     "angle": top_event.get("detail", ""),
                     "content_type": "紧急球评",
                     "target_emotion": "震惊",
                     "_batch_name": batch_cfg["name"]},
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
            elif urgent_events and batch_mode in ("auto", "morning"):
                print(f"   🚨 紧急球评让位配额：本批已满 {len(articles)}/{max_articles} 篇"
                      f"（计划 12.1 单批上限，事件 [{urgent_events[0]['type']}] 顺延下批）")

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
        # 批次归属兜底：任何追加路径（突发/确定性/节点/预测）漏写 _batch_name 时，
        # publisher 的 --batch 过滤会把它当「其他批次」静默跳过——文章生成了却永远
        # 发不出去（2026-10-25 预测稿实证）。这里统一补齐，预测稿仍由 is_prediction
        # 显式跳过，不会因补归属而被发布。
        _filled = ensure_batch_attribution(articles_sorted, batch_cfg.get("name", ""),
                                           batch_cfg.get("time", ""))
        if _filled:
            print(f"   🏷️ 批次归属兜底：{_filled} 篇漏写 batch_name，"
                  f"已按「{batch_cfg.get('name', '')}」补齐（否则发布器会静默跳过）")
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

    except QuotaExhaustedError as e:
        result_msg = f"所有 LLM 免费额度已耗尽: {e}"
        print(f"ERROR: {result_msg}")
        log.error(f"额度耗尽: {e}")
        send_wxpusher("足球自媒体 🪫 额度耗尽", f"{date_str} 发文任务中止：{result_msg}\n请检查是否需要充值、降频或补充新的 API key。")
        sys.exit(1)

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
