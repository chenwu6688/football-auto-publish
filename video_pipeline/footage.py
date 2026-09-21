#!/usr/bin/env python3
"""素材检索与下载（Phase 3 · 自动 B-roll）—— 让口播视频"有画面"，看起来更自然。

设计要点：
- 素材来源优先走 **Pexels / Pixabay 免费视频 API**：免版权、明确可商用/转发，按关键词自动搜足球/球场/
  球迷素材。需各自免费 API key（官网申请），用标准库 urllib 调用，无额外依赖。
- 兜底来源：**本地素材库**（你自己的真人/授权片段）与 **图片当 B-roll**（Pexels 图片 API 或本地图），
  零版权风险，适合起步。
- 关键词抽取：默认规则法（分句+去停用词+取短语），可选注入 LLM 提取更准的实体。
- 下载带**缓存**（按 query 哈希落盘），重复运行不重复下载；任意失败（无 key/无网/空结果）优雅返回空池，
  由上层回退到纯主讲人，保证出片不中断。
- 本模块只负责"拿到一批本地素材片段路径"，真正的剪接在 edit.py。
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

# 中文常见停用词（粗略），用于规则抽取时过滤无意义词
_STOP = set(
    "的 了 是 在 和 与 及 也 都 就 而 但 并 被 把 让 给 对 从 向 到 这 那 你 我 他 她 它 "
    "们 有 没 不 会 能 要 该 还 很 更 最 多 少 上 下 中 内 外 前 后 里 出来 起来 一个 一种 "
    "我们 他们 这个 那个 什么 怎么 为什么 如何 如果 因为 所以 而且 但是 然后 其实 已经 "
    "今天 昨天 现在 目前 之后 之前 一下 一直 一直 这些 那些".split()
)

_VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv", ".avi")
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


class FootageUnavailable(Exception):
    """素材检索不可用（无 key/无网/空结果）。供上层回退纯主讲人。"""


# ----------------------------------------------------------- 关键词抽取
def extract_keywords(text, k=4, llm_fn=None, english=False):
    """从口播稿抽取检索关键词。

    Args:
        text: 口播稿。
        k: 最多返回关键词数。
        llm_fn: 可选，注入 LLM(messages)->(dict,model)；若提供则优先让 LLM 提取
               足球实体（球队/球星/赛事），失败回退规则法。
        english: True 时让 LLM 返回英文关键词（Pexels/Pixabay 是英文标签库）。
    Returns:
        list[str]: 关键词（去重，最多 k 个）。
    """
    if llm_fn:
        try:
            lang_tip = "用英文返回" if english else "用原语言返回"
            msgs = [{
                "role": "system",
                "content": "你是足球内容关键词提取器。从口播稿提取最多%d个最适合用来搜索"
                           "相关视频素材的足球关键词（球队名、球星名、赛事名、球场等具象词），"
                           "%s，只返回 JSON：{\"keywords\": [\"...\",\"...\"]}。" % (k, lang_tip),
            }, {"role": "user", "content": text}]
            resp, _ = llm_fn(msgs)
            kws = resp.get("keywords") or []
            if isinstance(kws, list) and kws:
                out, seen = [], set()
                for w in kws:
                    w = str(w).strip()
                    if w and w not in seen:
                        seen.add(w)
                        out.append(w)
                    if len(out) >= k:
                        break
                if out:
                    return out
        except Exception:
            pass  # 回退规则法
    return extract_keywords_rule(text, k=k)


def extract_keywords_rule(text, k=4):
    """规则法：分句 → 去停用词 → 保留 2–6 字短语 → 去重取前 k。"""
    text = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9\s]", " ", text or "")
    clauses = re.split(r"[\s，。！？!?；;、：:]", text)
    out, seen = [], set()
    for cl in clauses:
        cl = cl.strip()
        if len(cl) < 2 or len(cl) > 8:
            continue
        if cl in _STOP:
            continue
        if any(ch.isdigit() for ch in cl):
            continue
        if cl not in seen:
            seen.add(cl)
            out.append(cl)
        if len(out) >= k:
            break
    return out


# ----------------------------------------------------------- 检索客户端
def _http_get_json(url, headers=None, timeout=20):
    # 必须带 User-Agent：Pexels 的 WAF 会拦截 urllib 默认 UA(Python-urllib/*) 返回 403，
    # 导致「素材检索异常：HTTP Error 403」。统一用浏览器 UA 规避。
    h = {"User-Agent": "Mozilla/5.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_download(url, out_path, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(out_path, "wb") as f:
        shutil.copyfileobj(r, f)


def search_pexels_videos(query, api_key, per_page=5, timeout=20):
    """Pexels 视频搜索，返回候选列表 [{url, width, height, duration}]。"""
    if not api_key:
        raise FootageUnavailable("Pexels 需要 API key")
    url = "https://api.pexels.com/videos/search?" + urllib.parse.urlencode(
        {"query": query, "per_page": per_page, "size": "medium"})
    data = _http_get_json(url, headers={"Authorization": api_key}, timeout=timeout)
    cands = []
    for v in data.get("videos", []):
        dur = v.get("duration") or 0
        # 选一个分辨率适中的文件（宽度 <= 1920）
        files = [f for f in v.get("video_files", [])
                 if f.get("width", 0) <= 1920 and f.get("link")]
        files.sort(key=lambda f: f.get("width", 0), reverse=True)
        if files:
            f = files[0]
            cands.append({"url": f["link"], "width": f.get("width", 0),
                          "height": f.get("height", 0), "duration": dur})
    return cands


def search_pixabay_videos(query, api_key, per_page=5, timeout=20):
    """Pixabay 视频搜索，返回候选列表。"""
    if not api_key:
        raise FootageUnavailable("Pixabay 需要 API key")
    url = "https://pixabay.com/api/videos/?" + urllib.parse.urlencode(
        {"key": api_key, "q": query, "per_page": per_page})
    data = _http_get_json(url, timeout=timeout)
    cands = []
    for h in data.get("hits", []):
        vids = h.get("videos", {})
        link = vids.get("medium") or vids.get("large") or vids.get("small")
        if link:
            cands.append({"url": link, "width": 0, "height": 0,
                          "duration": h.get("duration", 0)})
    return cands


def search_pexels_images(query, api_key, per_page=5, timeout=20):
    """Pexels 图片搜索（当用图片当 B-roll 时），返回候选列表。"""
    if not api_key:
        raise FootageUnavailable("Pexels 需要 API key")
    url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode(
        {"query": query, "per_page": per_page})
    data = _http_get_json(url, headers={"Authorization": api_key}, timeout=timeout)
    cands = []
    for p in data.get("photos", []):
        link = (p.get("src", {}).get("large2x") or p.get("src", {}).get("large")
                or p.get("src", {}).get("original"))
        if link:
            cands.append({"url": link, "width": p.get("width", 0),
                          "height": p.get("height", 0), "duration": 0, "is_image": True})
    return cands


# ----------------------------------------------------------- 中文→英文 检索词映射
# Pexels / Pixabay 的素材几乎全是英文标签，中文关键词直接搜基本 0 结果；
# 这里把常见中文足球词映射到英文检索词，保证中文脚本也能拉到相关素材。
# 顺序有讲究：球队/球星/赛事等「具体实体」放前面，通用名词放后面——
# _to_english 按此顺序做子串匹配，优先命中具体实体（如 "皇马更衣室炸了" → Real Madrid，
# 而非更短的通用词 locker room），检索相关度更高。
_FOOTBALL_EN = {
    # —— 具体球队 / 球星 / 赛事（优先命中）——
    "梅西": "Messi", "C罗": "Cristiano Ronaldo", "C 罗": "Cristiano Ronaldo",
    "皇马": "Real Madrid", "巴萨": "Barcelona", "曼联": "Manchester United",
    "曼城": "Manchester City", "利物浦": "Liverpool", "切尔西": "Chelsea",
    "阿森纳": "Arsenal", "拜仁": "Bayern Munich", "多特": "Borussia Dortmund",
    "巴黎": "Paris Saint-Germain", "尤文": "Juventus", "国米": "Inter Milan",
    "米兰": "AC Milan", "热刺": "Tottenham", "英格兰": "England",
    "西班牙": "Spain", "德国": "Germany", "法国": "France", "巴西": "Brazil",
    "阿根廷": "Argentina", "葡萄牙": "Portugal", "贝林厄姆": "Bellingham",
    "哈兰德": "Haaland", "姆巴佩": "Mbappe", "维尼修斯": "Vinicius",
    "罗德里": "Rodri", "凯恩": "Kane", "孙兴慜": "Son Heung-min",
    "内马尔": "Neymar", "萨拉赫": "Salah",
    "欧冠": "champions league", "世界杯": "world cup", "德比": "football derby",
    # —— 通用名词（后命中，作为兜底）——
    "足球": "football", "球迷": "football fans", "球场": "stadium",
    "体育场": "stadium", "球队": "football team", "比赛": "football match",
    "赛事": "football match", "联赛": "football league", "进球": "goal",
    "射门": "football", "传球": "football", "防守": "football", "进攻": "football",
    "教练": "football coach", "主帅": "football coach", "球员": "football player",
    "球星": "football star", "转会": "football transfer", "更衣室": "locker room",
}


def _has_cjk(s):
    return any("一" <= ch <= "鿿" for ch in s)


def _to_english(query):
    """把检索词转成英文（Pexels/Pixabay 友好）。

    规则：纯 ASCII → 原样返回；含中文 → 命中映射表（含子串匹配）取英文，
    未命中 → 返回 None（该词跳过，避免拿中文去英文库搜 0 结果）。
    """
    q = (query or "").strip()
    if not q:
        return None
    if not _has_cjk(q):
        return q
    # 子串匹配：长短语里含已知词也能命中（如 "皇马更衣室炸了" → Real Madrid）
    for zh, en in _FOOTBALL_EN.items():
        if zh in q:
            return en
    return None


# ----------------------------------------------------------- 收集与下载
def _cache_key(query, source):
    h = hashlib.md5(f"{source}:{query}".encode("utf-8")).hexdigest()[:12]
    return h


def _duration_ok(dur, min_d, max_d):
    """按 API 给出的时长过滤；dur<=0 表示未知，默认保留。"""
    if dur <= 0:
        return True
    return min_d <= dur <= max_d


def collect_footage(script, *, cfg, cache_dir=None, llm_fn=None):
    """检索并下载 B-roll 素材池（按 video_config 的 footage 配置块）。

    Args:
        script: 口播稿。
        cfg: video_config 的 footage 配置块（sources/per_query/max_clips/key 等）。
        cache_dir: 下载缓存目录（默认 <repo>/output/footage_cache）。
        llm_fn: 可选 LLM（配合 llm_keywords 提取更准实体）。
    Returns:
        list[dict]: 本地素材池，元素 {path, is_image, duration}；失败/无结果返回 []。
    """
    fc = cfg or {}
    sources = fc.get("sources") or ["pexels_video"]
    # 联网源（Pexels/Pixabay）列表，供下方英文检索判断 & 检索循环复用
    online = [s for s in sources if s in
              ("pexels_video", "pixabay_video", "pexels_image")]
    use_image = (online[0] == "pexels_image") if online else False
    per_query = int(fc.get("per_query", 3))
    max_clips = int(fc.get("max_clips", 8))
    min_d = float(fc.get("min_clip_dur", 2.0))
    max_d = float(fc.get("max_clip_dur", 8.0))
    llm_kw = bool(fc.get("llm_keywords", False))
    pexels_key = fc.get("pexels_api_key", "") or ""
    pixabay_key = fc.get("pixabay_api_key", "") or ""
    local_dir = Path(fc.get("local_dir", "") or "")

    keywords = extract_keywords(
        script, k=int(fc.get("keywords", 4)),
        llm_fn=(llm_fn if llm_kw else None),
        english=bool(online))  # 联网源用英文检索词（Pexels/Pixabay 英文标签）

    cache_dir = Path(cache_dir or Path(__file__).resolve().parents[1]
                     / "output" / "footage_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    pool = []
    used = []  # 实际贡献素材的来源（按出现顺序），用于反馈与元数据

    # 中文关键词 → 英文（Pexels/Pixabay 英文标签库），未命中/纯中文无映射则跳过
    queries = keywords or [script[:8]]
    eqs = []
    for q in queries:
        eq = _to_english(q)
        if eq:
            eqs.append(eq)
    if not eqs:
        eqs = ["football"]  # 兜底：关键词都译不出英文时，用通用词保证有素材
    queries = eqs

    # 每个联网源最多贡献的条数：保证多源混合（如视频+图片都出现，而非某一种占满）
    per_source_cap = max(2, max_clips // max(1, len(online))) if online else 0

    try:
        # 1) 联网源优先（Pexels / Pixabay 视频 或 Pexels 图片）—— 设计上「素材来源优先走 Pexels」
        for src_name in online:
            if len(pool) >= max_clips:
                break
            use_image = src_name == "pexels_image"
            key = pexels_key if src_name.startswith("pexels") else pixabay_key
            contributed = 0
            for q in queries:
                if contributed >= per_source_cap or len(pool) >= max_clips:
                    break
                ck = _cache_key(q, src_name)
                # 先看缓存
                cached = sorted(cache_dir.glob(f"{ck}_*"))
                if cached:
                    for c in cached:
                        if contributed >= per_source_cap or len(pool) >= max_clips:
                            break
                        pool.append({"path": str(c),
                                     "is_image": c.suffix.lower() in _IMG_EXT,
                                     "duration": 0})
                        contributed += 1
                    if contributed and src_name not in used:
                        used.append(src_name)
                    continue
                # 联网检索
                try:
                    if src_name == "pexels_video":
                        cands = search_pexels_videos(q, key, per_query)
                    elif src_name == "pixabay_video":
                        cands = search_pixabay_videos(q, key, per_query)
                    else:
                        cands = search_pexels_images(q, key, per_query)
                except FootageUnavailable as e:
                    print(f"   ⚠️ {src_name} 不可用：{e}")
                    break
                for i, c in enumerate(cands[:per_query]):
                    if contributed >= per_source_cap or len(pool) >= max_clips:
                        break
                    if not _duration_ok(c.get("duration", 0), min_d, max_d):
                        continue
                    suf = ".jpg" if use_image else ".mp4"
                    dst = cache_dir / f"{ck}_{i}{suf}"
                    try:
                        _http_download(c["url"], str(dst), timeout=60)
                        pool.append({"path": str(dst),
                                     "is_image": use_image,
                                     "duration": c.get("duration", 0)})
                        contributed += 1
                    except Exception as e:
                        print(f"   ⚠️ 素材下载失败（{q}#{i}）：{e}")
            if contributed and src_name not in used:
                used.append(src_name)
            if contributed:
                print(f"   ✅ {src_name} 命中 {contributed} 条素材")

        # 联网全源都搜不到（网络/限流/key 问题）→ 通用兜底再试一次，保证至少有足球素材
        if not pool and online:
            fb_q = "football match" if (online[0] != "pexels_image") else "football stadium"
            try:
                if online[0] == "pexels_video":
                    cands = search_pexels_videos(fb_q, pexels_key, per_query)
                elif online[0] == "pixabay_video":
                    cands = search_pixabay_videos(fb_q, pixabay_key, per_query)
                else:
                    cands = search_pexels_images(fb_q, pexels_key, per_query)
                use_image = online[0] == "pexels_image"
                for i, c in enumerate(cands[:per_query]):
                    if len(pool) >= max_clips:
                        break
                    if not _duration_ok(c.get("duration", 0), min_d, max_d):
                        continue
                    suf = ".jpg" if use_image else ".mp4"
                    dst = cache_dir / f"_fb_{i}{suf}"
                    try:
                        _http_download(c["url"], str(dst), timeout=60)
                        pool.append({"path": str(dst),
                                     "is_image": use_image,
                                     "duration": c.get("duration", 0)})
                    except Exception:
                        pass
                if pool and online[0] not in used:
                    used.append(online[0])
            except Exception:
                pass

        # 2) 联网全失败/空 → 回退本地素材库（零版权兜底，仅当联网一无所获时）
        if not pool and "local" in sources and local_dir.exists():
            for ext in _VIDEO_EXT + _IMG_EXT:
                for p in sorted(local_dir.rglob(f"*{ext}")):
                    if len(pool) >= max_clips:
                        break
                    pool.append({"path": str(p),
                                 "is_image": ext.lower() in _IMG_EXT,
                                 "duration": 0})
            if pool:
                used.append("local")
                print(f"   ⚠️ 联网源无结果，回退本地素材库 {len(pool)} 条（{local_dir}）")
    except Exception as e:
        print(f"   ⚠️ 素材检索异常：{e}，回退纯主讲人")

    return pool, used
