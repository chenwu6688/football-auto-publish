"""信源前移（计划 13.4）。

平台媒体的核心资产是编辑团队跟踪的原始信源。本模块把「原始信源清单」转成
ranker 可用的热度信号（同题报道条数 / 来源媒体权重），从而在选题择优时
体现「谁在谈」。

纪律（计划 13.4）：原始信源只作信号，不进正文。本模块只产出计数与权重，
不导出任何可供生成器引用的原文文本。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
WATCHLIST_PATH = PROJECT_ROOT / "config" / "source_watchlist.yaml"

_CACHE: dict | None = None


def load_watchlist(path: Path | None = None) -> dict:
    """加载信源清单（带缓存）。"""
    global _CACHE
    if _CACHE is not None and path is None:
        return _CACHE
    p = Path(path) if path else WATCHLIST_PATH
    data = {"sources": [], "signal": {"weight_cap": 1.0, "same_topic_cap": 10}}
    try:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        data.update(loaded)
    except Exception:
        pass
    if path is None:
        _CACHE = data
    return data


def _source_index(data: dict) -> list[tuple[str, list[str], float]]:
    idx = []
    for s in data.get("sources", []) or []:
        name = str(s.get("name", "")).strip()
        aliases = [str(a) for a in (s.get("aliases") or []) if a]
        weight = float(s.get("weight", 0.5))
        if name:
            idx.append((name, aliases + [name], weight))
    return idx


def match_sources(text: str) -> list[str]:
    """返回文本中命中的原始信源名列表（去重）。"""
    if not text:
        return []
    data = load_watchlist()
    hits = []
    for name, aliases, _w in _source_index(data):
        for a in aliases:
            if a and a in text:
                hits.append(name)
                break
    return hits


def source_weight(names, data: dict | None = None) -> float:
    """命中来源的权重之和，按 weight_cap 封顶。"""
    data = data or load_watchlist()
    cap = float((data.get("signal") or {}).get("weight_cap", 1.0))
    idx = {name: w for name, _a, w in _source_index(data)}
    total = sum(idx.get(n, 0.0) for n in (names or []))
    return min(cap, round(total, 3))


def _entity_terms() -> set[str]:
    try:
        from extractor import _entity_names
        return _entity_names()
    except Exception:
        return set()


def signals_from_articles(articles, entities=None) -> dict:
    """从新闻条目计算「可计算」热度信号（同题报道条数 / 来源媒体权重）。

    articles: [{"title":..., "source":..., "url":...}, ...]
    返回 {实体: {"同题报道条数": n, "来源媒体权重": w}}

    社媒讨论量 / 搜索指数不在本模块产出：由 signal_sources 接入公开源（微博/百度热搜、
    百度联想）后补齐；未命中即为缺失，由 ranker 标记 _signal_missing，绝不用 0 冒充。
    """
    data = load_watchlist()
    cap = int((data.get("signal") or {}).get("same_topic_cap", 10))
    ents = set(entities) if entities else _entity_terms()
    out: dict[str, dict] = {}
    for art in articles or []:
        if not isinstance(art, dict):
            continue
        text = f"{art.get('title', '')} {art.get('summary', '')}"
        srcs = match_sources(text)
        w = source_weight(srcs, data)
        for e in ents:
            if e and e in text:
                rec = out.setdefault(e, {"同题报道条数": 0, "来源媒体权重": 0.0})
                rec["同题报道条数"] = min(cap, rec["同题报道条数"] + 1)
                rec["来源媒体权重"] = max(rec["来源媒体权重"], w)
    return out


def signals_from_match_data(match_data: dict, entities=None) -> dict:
    """从 match_data 的新闻流合成信号（供 ranker L2 使用）。"""
    if not isinstance(match_data, dict):
        return {}
    arts = list(match_data.get("news_articles") or []) + list(match_data.get("transfer_news") or [])
    return signals_from_articles(arts, entities)


def merge_with_forwarded(signals: dict, forwarded: dict | None) -> dict:
    """把已有（转发/外部）信号并入，取逐字段较大值。"""
    if not forwarded:
        return signals or {}
    out = {k: dict(v) for k, v in (signals or {}).items()}
    for k, v in forwarded.items():
        rec = out.setdefault(k, {"同题报道条数": 0, "来源媒体权重": 0.0,
                                 "社媒讨论量": 0, "搜索指数": 0})
        for f in ("同题报道条数", "来源媒体权重", "社媒讨论量", "搜索指数"):
            rec[f] = max(rec.get(f, 0), (v or {}).get(f, 0))
    return out
