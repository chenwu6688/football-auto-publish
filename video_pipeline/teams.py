#!/usr/bin/env python3
"""球队标识（开场去真人）—— 根据口播稿自动识别球队，从 Wikimedia 拉取队标/球场。

设计：
- 识别：复用中文→英文球队映射（TEAMS 表，覆盖主流俱乐部+国家队），按出现顺序抽取被提及的球队。
- 拉取：Wikipedia pageimages 取俱乐部条目主图（基本即队标，较稳）；Wikidata P115 取主场球场图（best-effort）。
- 缓存：下载到 assets/teams/ 按球队 slug 落盘，重复运行不重复请求；任意失败优雅跳过，绝不臆造错误标识。
- 合规：Wikimedia 为自由版权，可用于足球二创；用户也可在 assets/teams/ 放本地核实图覆盖自动拉取结果。
"""

import json
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

# 中文球队名 → 英文 + Wikipedia 条目标题（国家队指向"国家足球队"条目，避免主图变成国旗）
# 仅收录俱乐部/国家队「球队」实体；球员/赛事不在此表（由 detect_teams 自然排除）。
TEAMS = {
    "皇马":   {"en": "Real Madrid",        "wiki": "Real Madrid CF"},
    "巴萨":   {"en": "Barcelona",          "wiki": "FC Barcelona"},
    "曼联":   {"en": "Manchester United",  "wiki": "Manchester United F.C."},
    "曼城":   {"en": "Manchester City",    "wiki": "Manchester City F.C."},
    "利物浦": {"en": "Liverpool",          "wiki": "Liverpool F.C."},
    "切尔西": {"en": "Chelsea",            "wiki": "Chelsea F.C."},
    "阿森纳": {"en": "Arsenal",            "wiki": "Arsenal F.C."},
    "拜仁":   {"en": "Bayern Munich",      "wiki": "FC Bayern Munich"},
    "多特":   {"en": "Borussia Dortmund",  "wiki": "Borussia Dortmund"},
    "巴黎":   {"en": "Paris Saint-Germain","wiki": "Paris Saint-Germain F.C."},
    "尤文":   {"en": "Juventus",           "wiki": "Juventus F.C."},
    "国米":   {"en": "Inter Milan",        "wiki": "Inter Milan"},
    "米兰":   {"en": "AC Milan",           "wiki": "AC Milan"},
    "热刺":   {"en": "Tottenham",          "wiki": "Tottenham Hotspur F.C."},
    "英格兰": {"en": "England",            "wiki": "England national football team"},
    "西班牙": {"en": "Spain",              "wiki": "Spain national football team"},
    "德国":   {"en": "Germany",            "wiki": "Germany national football team"},
    "法国":   {"en": "France",             "wiki": "France national football team"},
    "巴西":   {"en": "Brazil",             "wiki": "Brazil national football team"},
    "阿根廷": {"en": "Argentina",          "wiki": "Argentina national football team"},
    "葡萄牙": {"en": "Portugal",           "wiki": "Portugal national football team"},
}


def _http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_download(url, out_path, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(out_path, "wb") as f:
        shutil.copyfileobj(r, f)


def _slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_") or "team"


def detect_teams(script):
    """从口播稿识别被提及的球队（按出现顺序去重）。

    Returns:
        list[dict]: [{zh, en, wiki}, ...]，未识别到返回 []。
    """
    text = script or ""
    found = []
    seen = set()
    # 长词优先匹配（如"巴黎圣日耳曼"之类；本表无更长重叠，但保留顺序稳健性）
    for zh in sorted(TEAMS.keys(), key=len, reverse=True):
        idx = text.find(zh)
        if idx >= 0 and zh not in seen:
            seen.add(zh)
            found.append({"zh": zh, **TEAMS[zh]})
    # 按出现顺序排序
    found.sort(key=lambda t: text.find(t["zh"]))
    return found


def _fetch_crest(wiki_title, timeout=20):
    """Wikipedia pageimages 取条目主图（俱乐部条目主图即队标）。失败返回 None。"""
    try:
        url = ("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "format": "json", "prop": "pageimages",
            "piprop": "original", "titles": wiki_title, "redirects": "1",
        }))
        data = _http_get_json(url, timeout=timeout)
        for page in data.get("query", {}).get("pages", {}).values():
            src = (page.get("original") or {}).get("source")
            if src:
                return src
    except Exception:
        pass
    return None


def _fetch_stadium(wiki_title, timeout=20):
    """Wikidata P115(home venue) → 场馆图。best-effort，失败返回 None。"""
    try:
        # 1) 条目 → Wikidata QID
        qurl = ("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "format": "json", "prop": "pageprops",
            "ppprop": "wikibase_item", "titles": wiki_title, "redirects": "1",
        }))
        qdata = _http_get_json(qurl, timeout=timeout)
        qid = None
        for page in qdata.get("query", {}).get("pages", {}).values():
            qid = (page.get("pageprops") or {}).get("wikibase_item")
            if qid:
                break
        if not qid:
            return None
        # 2) QID → 主场场馆 QID (P115)
        edata = _http_get_json(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json",
                               timeout=timeout)
        claims = edata.get("entities", {}).get(qid, {}).get("claims", {})
        venue_qid = None
        for cl in claims.get("P115", []):
            v = cl.get("mainsnak", {}).get("datavalue", {}).get("value", {})
            if isinstance(v, dict) and v.get("id"):
                venue_qid = v["id"]
                break
        if not venue_qid:
            return None
        # 3) 场馆 QID → 图片 (P18)
        vdata = _http_get_json(f"https://www.wikidata.org/wiki/Special:EntityData/{venue_qid}.json",
                               timeout=timeout)
        vclaims = vdata.get("entities", {}).get(venue_qid, {}).get("claims", {})
        fname = None
        for cl in vclaims.get("P18", []):
            v = cl.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(v, str):
                fname = v
                break
        if not fname:
            return None
        # 4) File → 实际图片 URL（Special:FilePath 会 302 到真实文件）
        return "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(fname)
    except Exception:
        pass
    return None


def fetch_team_assets(team, cache_dir, *, stadium=True, timeout=20):
    """拉取单支球队的队标 + 球场图，缓存到 cache_dir。

    Args:
        team: detect_teams 返回的单个球队 dict（含 wiki）。
        cache_dir: 缓存目录。
        stadium: 是否尝试拉球场图。
    Returns:
        dict: {crest: path|None, stadium: path|None}
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug(team["wiki"])
    out = {"crest": None, "stadium": None}

    crest_path = cache_dir / f"{slug}_crest.png"
    if crest_path.exists():
        out["crest"] = str(crest_path)
    else:
        src = _fetch_crest(team["wiki"], timeout=timeout)
        if src:
            try:
                _http_download(src, str(crest_path), timeout=timeout)
                out["crest"] = str(crest_path)
            except Exception:
                pass

    if stadium:
        st_path = cache_dir / f"{slug}_stadium.png"
        if st_path.exists():
            out["stadium"] = str(st_path)
        else:
            src = _fetch_stadium(team["wiki"], timeout=timeout)
            if src:
                try:
                    _http_download(src, str(st_path), timeout=timeout)
                    out["stadium"] = str(st_path)
                except Exception:
                    pass
    return out


def collect_team_images(teams, cfg, cache_dir=None):
    """批量拉取被提及球队的标识图，返回素材池条目（队标优先，其次球场）。

    Args:
        teams: detect_teams 返回的列表。
        cfg: teams 配置块（cache_dir / stadium）。
        cache_dir: 覆盖缓存目录（默认从 cfg 解析，相对仓库根）。
    Returns:
        list[dict]: [{path, is_image:True, duration:0}, ...]（可直接 prepend 进 footage pool）。
    """
    if not teams:
        return []
    if cache_dir is None:
        root = Path(__file__).resolve().parents[1]
        cache_dir = root / cfg.get("cache_dir", "assets/teams")
    want_stadium = bool(cfg.get("stadium", True))

    pool = []
    for t in teams:
        assets = fetch_team_assets(t, cache_dir, stadium=want_stadium)
        for kind in ("crest", "stadium"):
            p = assets.get(kind)
            if p:
                pool.append({"path": p, "is_image": True, "duration": 0})
    return pool
