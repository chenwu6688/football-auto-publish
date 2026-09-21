#!/usr/bin/env python3
"""球队标识（开场去真人）—— 根据口播稿自动识别球队，拉取队标/球场图。

设计：
- 识别：复用中文→英文球队映射（TEAMS 表，覆盖主流俱乐部+国家队），按出现顺序抽取被提及的球队。
- 取图（双源，按序降级）：
    1) Wikimedia（Wikipedia pageimages 取队标、Wikidata P115 取主场球场图）——自由版权，首选；
    2) api-sports 队标 CDN（media.api-sports.io/football/teams/<id>.png）——**无需 key、直链稳定**，
       本沙箱实测可直连且队标正确；作为 Wikimedia 不可达/冷门条目无图时的兜底。
  两源都拿不到 → 优雅跳过，绝不臆造错误标识。
- 缓存：下载到 assets/teams/ 按球队 slug 落盘，重复运行不重复请求。
- 合规：Wikimedia 为自由版权；api-sports CDN 仅取队标图作节目内标识展示（二创用途），
  用户也可在 assets/teams/ 放本地核实图覆盖自动拉取结果。

补充说明（关于 tzuqiu.cc）：tzuqiu.cc（T足球）确有中文球队资料，但整站经 Cloudflare
托管式 JS 挑战，脚本化抓取会被拦（curl/无头浏览器均 403），且其页面/接口对外不稳定，
不适合做自动管线数据源，故未接入；如需中文球队资料，建议人工整理成表放本地。
"""

import json
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

# 中文球队名 → 英文 + Wikipedia 条目标题 + api-sports 队标 ID（api_id）
#   · wiki：国家队指向"国家足球队"条目，避免主图变成国旗
#   · api_id：api-sports 队标 CDN 的球队 ID（已逐个肉眼核对队标正确）
# 仅收录俱乐部/国家队「球队」实体；球员/赛事不在此表（由 detect_teams 自然排除）。
TEAMS = {
    "皇马":   {"en": "Real Madrid",        "wiki": "Real Madrid CF",              "api_id": 541},
    "巴萨":   {"en": "Barcelona",          "wiki": "FC Barcelona",                "api_id": 529},
    "曼联":   {"en": "Manchester United",  "wiki": "Manchester United F.C.",      "api_id": 33},
    "曼城":   {"en": "Manchester City",    "wiki": "Manchester City F.C.",        "api_id": 50},
    "利物浦": {"en": "Liverpool",          "wiki": "Liverpool F.C.",              "api_id": 40},
    "切尔西": {"en": "Chelsea",            "wiki": "Chelsea F.C.",                "api_id": 49},
    "阿森纳": {"en": "Arsenal",            "wiki": "Arsenal F.C.",                "api_id": 42},
    "拜仁":   {"en": "Bayern Munich",      "wiki": "FC Bayern Munich",            "api_id": 157},
    "多特":   {"en": "Borussia Dortmund",  "wiki": "Borussia Dortmund",           "api_id": 165},
    "巴黎":   {"en": "Paris Saint-Germain","wiki": "Paris Saint-Germain F.C.",    "api_id": 85},
    "尤文":   {"en": "Juventus",           "wiki": "Juventus F.C.",               "api_id": 496},
    "国米":   {"en": "Inter Milan",        "wiki": "Inter Milan",                 "api_id": 505},
    "米兰":   {"en": "AC Milan",           "wiki": "AC Milan",                    "api_id": 489},
    "热刺":   {"en": "Tottenham",          "wiki": "Tottenham Hotspur F.C.",      "api_id": 47},
    # 国家队：api-sports 返回的是国旗（非队徽），故不设 api_id，队标统一走 Wikimedia 的
    # "X national football team" 条目（主图即队徽），避免"国旗冒充队标"。
    "英格兰": {"en": "England",            "wiki": "England national football team"},
    "西班牙": {"en": "Spain",              "wiki": "Spain national football team"},
    "德国":   {"en": "Germany",            "wiki": "Germany national football team"},
    "法国":   {"en": "France",             "wiki": "France national football team"},
    "巴西":   {"en": "Brazil",             "wiki": "Brazil national football team"},
    "阿根廷": {"en": "Argentina",          "wiki": "Argentina national football team"},
    "葡萄牙": {"en": "Portugal",           "wiki": "Portugal national football team"},
}

# api-sports 队标 CDN 模板（无需 key，直链）
_API_SPORTS_BADGE = "https://media.api-sports.io/football/teams/{id}.png"
# 注意：api-sports 对「不存在的 ID」会返回通用占位图；且国家队 ID 返回的是国旗而非队徽。
# 因此 api_id 只用于**已肉眼核对过队标正确**的俱乐部；国家队不设 api_id（走 Wikimedia 队徽条目）。
# 需要补冷门/中超球队时，先确证其 api-sports ID（核对队标图正确）再填，切勿凭猜。


def _load_local_teams(cfg):
    """从本地覆盖文件加载球队映射（补冷门/中超球队），与内置 TEAMS 合并。

    配置：teams.local_map（默认 assets/teams/teams_local.json），格式：
        { "上海海港": {"en": "Shanghai Port", "wiki": "Shanghai Port F.C.", "api_id": 1234}, ... }
    覆盖优先级高于内置表；文件不存在则忽略（不报错）。
    """
    path = (cfg or {}).get("local_map") or "assets/teams/teams_local.json"
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[1] / p
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("en")} \
            if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_teams(cfg=None):
    """返回「内置表 + 本地覆盖表」合并后的球队映射。"""
    merged = dict(TEAMS)
    merged.update(_load_local_teams(cfg))
    return merged



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


def detect_teams(script, cfg=None):
    """从口播稿识别被提及的球队（按出现顺序去重）。

    Args:
        script: 口播稿。
        cfg: teams 配置块（可选）；提供时把 assets/teams/teams_local.json 的本地球队
             一并纳入识别（用于补冷门/中超球队）。
    Returns:
        list[dict]: [{zh, en, wiki, api_id?, pos}, ...]，未识别到返回 []。
            pos = 该队名在口播稿中「首次出现」的字符下标（用于把队标对齐到提到它的那句）。
    """
    text = script or ""
    table = get_teams(cfg) if cfg is not None else TEAMS
    found = []
    seen = set()
    # 长词优先匹配（如"巴黎圣日耳曼"之类；本表无更长重叠，但保留顺序稳健性）
    for zh in sorted(table.keys(), key=len, reverse=True):
        idx = text.find(zh)
        if idx >= 0 and zh not in seen:
            seen.add(zh)
            found.append({"zh": zh, "pos": idx, **table[zh]})
    # 按出现顺序排序
    found.sort(key=lambda t: t["pos"])
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


def _fetch_crest_api_sports(api_id, timeout=20):
    """api-sports 队标 CDN 直链（无需 key）。返回图片 URL；无 api_id 返回 None。"""
    if not api_id:
        return None
    return _API_SPORTS_BADGE.format(id=int(api_id))


def fetch_team_assets(team, cache_dir, *, stadium=True, timeout=20):
    """拉取单支球队的队标 + 球场图，缓存到 cache_dir。

    队标双源降级：Wikimedia（自由版权首选）→ api-sports CDN（无 key、直链稳）。
    球场图仅走 Wikidata（best-effort），失败跳过。

    Args:
        team: detect_teams 返回的单个球队 dict（含 wiki，可能含 api_id）。
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
        # 源1：Wikimedia（自由版权首选）
        src = _fetch_crest(team["wiki"], timeout=timeout)
        # 源2：Wikimedia 拿不到 → api-sports CDN 兜底（本沙箱实测可直连）
        if not src:
            src = _fetch_crest_api_sports(team.get("api_id"), timeout=timeout)
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


def align_teams_to_segments(teams, segments, script):
    """把「球队」对齐到「提到它的那一句」，供开场队标按位置插播。

    做法：把各句文本按顺序拼成全文（与 script 同源），累计每句在全文中的
    起止字符区间；再用 detect_teams 给出的 pos 找该队名落在哪一句。
    找不到（拼接与 script 有出入）则退化为「按顺序铺在前 N 段」。

    Args:
        teams: detect_teams 返回列表（含 pos）。
        segments: 句时间轴 list[{start,end,text}]。
        script: 口播稿全文。
    Returns:
        dict[int, str]: {段下标: 队名 zh}，按段号升序唯一（同一段只保留第一支）。
    """
    if not teams or not segments:
        return {}
    # 逐句累计区间（用 strip 后长度累计，容忍 SRT 与全文的空格差异）
    spans = []          # [(seg_i, start, end)]
    cursor = 0
    for i, s in enumerate(segments):
        t = (s.get("text") or "").strip()
        if not t:
            continue
        # 在 script 中从 cursor 处向后找该句，定位真实区间
        at = script.find(t, cursor) if script else -1
        if at >= 0:
            spans.append((i, at, at + len(t)))
            cursor = at + len(t)
        else:
            spans.append((i, cursor, cursor + len(t)))
            cursor += len(t)

    out = {}
    used_seg = set()
    for t in teams:
        pos = t.get("pos")
        seg_i = None
        if isinstance(pos, int):
            for si, st, en in spans:
                if st <= pos < en:
                    seg_i = si
                    break
            if seg_i is None and spans:
                # pos 落在句间空隙：归给最近的下一句
                for si, st, en in spans:
                    if st >= pos:
                        seg_i = si
                        break
                if seg_i is None:
                    seg_i = spans[-1][0]
        if seg_i is None:
            continue
        if seg_i in used_seg:      # 同段只放第一支（避免叠加）
            continue
        used_seg.add(seg_i)
        out[seg_i] = t["zh"]
    # 若对齐结果为空（异常），退化：前 N 段按顺序铺
    if not out:
        for k, t in enumerate(teams):
            if k < len(segments):
                out[k] = t["zh"]
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
