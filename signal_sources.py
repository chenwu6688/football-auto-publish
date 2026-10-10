"""社媒 / 搜索热度信号源（计划 11.2 L2 信号增强）。

计划 11.2 要求 L2 为候选补齐四类热度信号：
    同题报道条数、来源媒体权重、社媒讨论量、可获得的搜索指数

其中「同题报道条数 / 来源媒体权重」由 source_watch 从新闻流计算；
本模块补齐后两类：
    - 社媒讨论量：微博热搜榜命中（公开可得，无需授权）
    - 搜索指数：百度搜索联想词命中度（公开可得，无需授权）

两条纪律：
  1. **绝不假装有信号**——只有真实命中才写入字段；未命中一律不写，
     由 ranker.l2_enrich 标记为「缺失信号」，而非用 0 冒充。
  2. **预热后只读**——主流程只读 data/signals/external_signals.json（预热结果），
     不直连外网；抓取由 scripts/refresh_signals.py 或定时 workflow 完成，
     与「赛程落库缓存、业务侧只读」同一工程纪律（计划 11.4）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
SIGNALS_DIR = PROJECT_ROOT / "data" / "signals"
EXTERNAL_PATH = SIGNALS_DIR / "external_signals.json"

FIELDS = ("同题报道条数", "来源媒体权重", "社媒讨论量", "搜索指数")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


# ------------------------------------------------------------
# 读取（主流程只读这里）
# ------------------------------------------------------------
def _empty_record() -> dict:
    return {"同题报道条数": 0, "来源媒体权重": 0.0, "社媒讨论量": 0, "搜索指数": 0}


def load_external(path: Path | None = None) -> dict:
    """读取预热的社媒/搜索信号；无文件返回空表。

    返回 {实体: {"社媒讨论量": n, "搜索指数": m}}（仅含真实命中项）。
    """
    p = Path(path) if path else EXTERNAL_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    sig = data.get("signals") if isinstance(data, dict) else None
    if isinstance(sig, dict):
        return sig
    return data if isinstance(data, dict) else {}


def merge_with_forwarded(signals: dict, forwarded: dict | None) -> dict:
    """把预热信号并入，逐字段取较大值（与 source_watch.merge_with_forwarded 同语义）。"""
    out = {k: dict(v) for k, (v) in (signals or {}).items()}
    for k, v in (forwarded or {}).items():
        rec = out.setdefault(k, _empty_record())
        for f in FIELDS:
            if f in (v or {}):
                rec[f] = max(rec.get(f, 0), (v or {}).get(f, 0))
    return out


# ------------------------------------------------------------
# 抓取（供预热任务调用，主流程不调用）
# ------------------------------------------------------------
def fetch_weibo_hot(timeout: int = 8) -> list[dict]:
    """微博热搜榜：返回 [{"word":..., "num":..., "rank":...}]；失败返回 []。"""
    url = "https://weibo.com/ajax/side/hotSearch"
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        r.raise_for_status()
        data = r.json().get("data", {}) or {}
        items = data.get("realtime", []) or []
        out = []
        for i, it in enumerate(items):
            w = it.get("word") or it.get("note") or ""
            if w:
                out.append({"word": w, "num": int(it.get("num", 0) or 0), "rank": i + 1})
        return out
    except Exception:
        return []


def fetch_baidu_suggest(term: str, timeout: int = 6) -> list[str]:
    """百度搜索联想词：返回联想词列表；失败返回 []。

    注意：该接口返回的是非标准 JSON（键无引号，形如 ``cb({q:"x",s:["a","b"]});``），
    不能用 json.loads 解析，改用正则抽取 s 数组内的字符串。
    """
    try:
        r = requests.get("https://suggestion.baidu.com/su",
                         params={"wd": term, "cb": "cb"},
                         headers={"User-Agent": _UA}, timeout=timeout)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
            r.encoding = r.apparent_encoding or "utf-8"
        text = r.text
        m = (re.search(r'"s"\s*:\s*\[(.*?)\]', text, re.S)
             or re.search(r'\bs\s*:\s*\[(.*?)\]', text, re.S)
             or re.search(r'\[(.*?)\]', text, re.S))
        if not m:
            return []
        return re.findall(r'"([^"]*)"', m.group(1))
    except Exception:
        return []


def _walk_hot_words(obj):
    """递归收集含 word 字段的条目（兼容各热搜接口的层级差异）。"""
    if isinstance(obj, dict):
        if isinstance(obj.get("word"), str) and obj["word"]:
            yield obj
        for v in obj.values():
            yield from _walk_hot_words(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_hot_words(v)


def fetch_baidu_hot(timeout: int = 8) -> list[dict]:
    """百度热搜榜（公开，无需授权）：返回 [{"word","num","rank"}]；失败返回 []。"""
    try:
        r = requests.get("https://top.baidu.com/api/board",
                         params={"platform": "wise", "tab": "realtime"},
                         headers={"User-Agent": _UA}, timeout=timeout)
        r.raise_for_status()
        items = list(_walk_hot_words(r.json()))
        out = []
        for i, it in enumerate(items):
            if it.get("isTop"):
                continue
            num = int(it.get("hotScore") or it.get("heat") or 0)
            out.append({"word": it["word"], "num": num, "rank": i + 1})
        return out
    except Exception:
        return []


def fetch_hot(timeout: int = 8) -> list[dict]:
    """取得可用热搜榜：优先百度热搜，失败再退回微博热搜。"""
    hot = fetch_baidu_hot(timeout=timeout)
    if hot:
        return hot
    return fetch_weibo_hot(timeout=timeout)


def _social_from_hot(entities, hot: list[dict]) -> dict:
    """热搜命中 → 社媒讨论量（用讨论数，缺失则用榜单热度分数）。"""
    out = {}
    for e in entities or []:
        if not e:
            continue
        for it in hot or []:
            if e and e in (it.get("word") or ""):
                # 讨论数可能为 0，则用排名换算一个保守分数
                val = int(it.get("num") or 0) or max(1, 60 - int(it.get("rank", 60)))
                out.setdefault(e, {})["社媒讨论量"] = val
                break
    return out


def _search_from_suggest(entities, per_term=None) -> dict:
    """百度联想命中 → 搜索指数（命中度：联想词条数，带上限）。"""
    out = {}
    for e in entities or []:
        if not e:
            continue
        words = per_term.get(e) if per_term is not None else fetch_baidu_suggest(e)
        hit = [w for w in (words or []) if e in w]
        if hit:
            out.setdefault(e, {})["搜索指数"] = min(100, len(hit) * 10)
    return out


def build_signals(entities, *, hot=None, per_term=None, timeout: int = 8) -> dict:
    """抓取并用真实命中构造信号表（只含命中实体）。

    hot: 预取的热搜列表（不传则联网抓）；per_term: {实体: 联想词列表}（测试注入）。
    """
    ents = [e for e in (entities or []) if e]
    if not ents:
        return {}
    if hot is None:
        hot = fetch_hot(timeout=timeout)
    social = _social_from_hot(ents, hot)
    search = _search_from_suggest(ents, per_term=per_term)
    out: dict[str, dict] = {}
    for e in ents:
        rec = {}
        if e in social:
            rec.update(social[e])
        if e in search:
            rec.update(search[e])
        if rec:
            out[e] = rec
    return out


def write_external(signals: dict, *, entities=None, path: Path | None = None) -> Path:
    """把信号写入 external_signals.json（供主流程只读）。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    p = Path(path) if path else EXTERNAL_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "entities": list(entities or signals.keys()),
        "signals": signals,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
