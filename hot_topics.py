# -*- coding: utf-8 -*-
"""今日热点选题通道（零级·热点）：把「全网正在谈的足球事件」变成可选题的候选。

## 为什么需要这个模块（2026-11-06）

用户反馈：「昨晚 C 罗国家队退役，我文章里没有任何体现，重新梳理选题逻辑，
要紧跟热点，根据每日热点排行，不要重复发过期内容。」

查证结果：

1. 微博热搜 rank 20 就是「C罗退出国家队」，榜上是有的；
2. 但生产链路的选题素材只有两处 —— ``data_collector`` 抓的 **15 条** 直播吧
   新闻 + ``data/fixtures/topic_pool.json`` 的赛程池；
3. ``signal_sources`` 虽然能抓热搜（``fetch_hot`` 实测 100 条），可它只被
   ``ranker`` 用来给**已有候选**加分 —— 热搜词和赛程池里的比赛没有交集，
   加分永远是 0，也就永远**产生不了新候选**；
4. 更直接的是：``batch.yml`` 从来没运行过 ``scripts/refresh_signals.py``，
   所以连「给已有候选加分」这条路在 CI 上也是断的。

一句话：**信号只做排序，不做选题；素材池里没有「热搜事件」这一路供给。**

## 本模块的边界（三条纪律）

- 热搜词只是**线索**，不是事实。正文仍必须从源报道改写，本模块只负责
  「今天该写什么」和「拿哪篇报道作依据」，绝不把热词写进正文。
- **没有报道支撑的热词不产出候选**。热搜只有一个短语，没有细节；硬写就是
  编造。宁可少一篇，也不写「据热搜」。
- 已发过的事件不再产出。``event_key`` = 实体 + 事件词，与措辞无关，
  所以「C罗退出国家队」与「C罗宣布结束国家队生涯」算同一件事。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")

ROOT = Path(__file__).resolve().parent
ENTITY_MAP_PATH = ROOT / "config" / "entity_map.json"
PLAYER_NAMES_PATH = ROOT / "config" / "player_names.json"
LEDGER_DIR = ROOT / "data" / "audit"

#: 足球**专有**词 —— 只收「出现即基本可确定是足球」的词。
#:
#: 为什么不放「球」「比赛」「队」「球员」这类泛词：2026-11-06 实测榜单里
#: 「郑钦文神仙球获评月最佳」（网球）、「克莱封盖库里」（篮球）、
#: 「温瑞博3比0王楚钦」（乒乓球）都会命中泛词。用泛词过滤等于不过滤，
#: 会让热点通道把别的项目顶到足球稿的位置上。
#:
#: 也不放「官宣」「宣布」：它们跨领域通用（手机官宣、明星官宣），
#: 实测「荣耀700系列外观官宣」正是靠「官宣」混进足球切片的。
FOOTBALL_TERMS: tuple[str, ...] = (
    # 赛事 / 联赛
    "欧冠", "欧联", "欧协", "英超", "西甲", "意甲", "德甲", "法甲", "中超", "中甲",
    "中乙", "足协杯", "亚冠", "世界杯", "欧洲杯", "亚洲杯", "世预赛", "世青赛",
    "世俱杯", "联合会杯", "美洲杯", "非洲杯", "金球奖", "FIFA", "欧国联",
    # 组织 / 角色
    "国足", "国家队", "主帅", "主教练", "门将", "后卫", "中场", "前锋", "边锋",
    "边卫", "队长", "替补席", "更衣室", "青训", "转会窗", "冬窗", "夏窗",
    # 事件 / 术语
    "转会", "租借", "免签", "续约", "解约", "买断", "违约金", "退役",
    "退出国家队", "帽子戏法", "梅开二度", "点球", "任意球", "越位", "红牌",
    "黄牌", "绝杀", "逆转", "保级", "争冠", "升降级", "下课", "解雇", "伤退",
    "十字韧带", "腹股沟", "半月板", "越位判罚", "VAR",
)

#: 事件词：用于把「同一实体的不同事件」区分开，避免 event_key 过粗。
#: 过粗会把「C罗打进第 900 球」和「C罗退出国家队」当成同一件事而漏发。
#:
#: ⚠️ 不放「宣布」「官宣」：它们不区分事件（宣布退役 / 宣布转会 / 宣布伤停都用），
#: 放进来只会让同一件事因为措辞多了一个词而算出两个 key。
EVENT_TERMS: tuple[str, ...] = (
    "退役", "退出国家队", "告别国家队", "转会", "租借", "免签", "续约", "解约",
    "买断", "下课", "解雇", "上任", "执教", "受伤", "伤退", "手术", "停赛",
    "禁赛", "罚单", "夺冠", "出局", "晋级", "降级", "破门", "绝杀", "逆转",
    "帽子戏法", "梅开二度", "金球奖", "回归", "加盟", "离队",
)

#: 事件族：同一件事的不同措辞必须落到同一个族，否则「C罗退役」与
#: 「C罗退出国家队」会被判成两个事件、双双发出。
#:
#: 第一版直接拿命中词集合拼 event_key，实测「C罗宣布退出葡萄牙国家队」得到
#: ``C罗#宣布,退出国家队``、「C罗退出国家队后首度发声」得到 ``C罗#退出国家队``，
#: 于是同一件事分裂成两条、每条都不够「同题 2 篇」的门槛，降级通道直接空转。
EVENT_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("retire", ("退役", "退出国家队", "告别国家队", "退出葡萄牙", "结束国脚生涯")),
    ("transfer", ("转会", "加盟", "租借", "免签", "买断", "离队", "违约金")),
    ("contract", ("续约", "解约")),
    ("coach", ("下课", "解雇", "上任", "执教", "接任")),
    ("injury", ("受伤", "伤退", "手术", "十字韧带", "腹股沟", "半月板")),
    ("ban", ("停赛", "禁赛", "罚单")),
    ("award", ("金球奖",)),
)

#: 热搜只看前 N 名。再往后基本是长尾，与「今天大家都在谈什么」无关。
DEFAULT_MAX_RANK = 60

#: 中文分词太贵也不准，这里只做「连续 2 字重叠」的粗配对。
_RE_CJK = re.compile(r"[\u4e00-\u9fff]{2,}")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _alias_index() -> dict[str, str]:
    """别名（小写）→ 规范名。球队取 ``name_zh``，球员取中文名。

    只登记**长度 ≥2** 的别名：单字别名（「罗」「马」）在热搜词里几乎必然误命中。
    """
    idx: dict[str, str] = {}
    data = _load_json(ENTITY_MAP_PATH)
    ents = data.get("entities") or {}
    if isinstance(ents, dict):
        for item in ents.values():
            if not isinstance(item, dict):
                continue
            canon = str(item.get("name_zh") or "").strip()
            if not canon:
                continue
            forms = {canon}
            for a in (item.get("aliases") or []):
                if isinstance(a, str) and a.strip():
                    forms.add(a.strip())
            for f in forms:
                if len(f) >= 2:
                    idx.setdefault(f.lower(), canon)

    players = _load_json(PLAYER_NAMES_PATH).get("players") or {}
    if isinstance(players, dict):
        for en, zh in players.items():
            zh = str(zh or "").strip()
            if len(zh) >= 2:
                idx.setdefault(zh.lower(), zh)
            en = str(en or "").strip()
            if len(en) >= 4:
                idx.setdefault(en.lower(), zh or en)
    return idx


_ALIAS_CACHE: dict[str, object] = {"table": None}


def alias_table() -> dict[str, str]:
    if _ALIAS_CACHE["table"] is None:
        _ALIAS_CACHE["table"] = _alias_index()
    return _ALIAS_CACHE["table"]  # type: ignore[return-value]


def entities_in(text: str) -> list[str]:
    """文本里出现的登记实体（规范名），**最长匹配优先**。

    「国际米兰」里躺着「米兰」，不消解就会把国米的新闻算到 AC 米兰头上 ——
    同一类串味已经在配图相关性上炸过一次（2026-11-06 米兰/国米用例）。
    """
    low = (text or "").lower()
    if not low:
        return []
    table = alias_table()
    spans: list[tuple[int, int, str]] = []
    for form, canon in table.items():
        pos = low.find(form)
        while pos != -1:
            spans.append((pos, pos + len(form), canon))
            pos = low.find(form, pos + 1)
    if not spans:
        return []
    spans.sort(key=lambda x: (-(x[1] - x[0]), x[0]))
    kept: list[tuple[int, int]] = []
    out: list[str] = []
    for s, t, canon in spans:
        if any(s >= cs and t <= ce and (t - s) < (ce - cs) for cs, ce in kept):
            continue
        kept.append((s, t))
        if canon not in out:
            out.append(canon)
    return out


def is_football(text: str, ents: list[str] | None = None) -> bool:
    """这条热搜词是不是**足球事件**。

    两档判据，缺一不可地分层：

    - **强档**：命中足球专有词（赛事/联赛/组织/角色/术语）→ 直接算。
      「国足新帅浮出水面」没有登记实体，但「国足」够硬。
    - **弱档**：只命中登记实体 → **必须同时带事件词**才算。
      实测反例：「三岁半男孩给电视里的红军递饼干」命中了利物浦别名「红军」，
      若只看实体就会把一条萌娃视频塞进足球选题；它没有任何事件词，弱档挡掉。
      「C罗退出国家队」有实体 + 「退出国家队」，弱档放行。

    只靠「实体」或只靠「词表」都会漏一边：实体管不住「国足」这种集合概念，
    词表管不住「某球星妻子」这种只有人名的热搜。
    """
    text = text or ""
    if any(t in text for t in FOOTBALL_TERMS):
        return True
    return bool(ents) and bool(_event_terms(text))


def _event_terms(text: str) -> list[str]:
    return sorted({t for t in EVENT_TERMS if t in (text or "")})


def event_family(text: str) -> str:
    """命中的事件族名；一个都没命中返回空串。

    多族同时命中时取**最先声明**的那个 —— ``EVENT_FAMILIES`` 的顺序就是
    优先级（退役 > 转会 > 续约 > 换帅 > 伤病 > 停赛 > 奖项）。
    """
    t = text or ""
    for name, words in EVENT_FAMILIES:
        if any(w in t for w in words):
            return name
    return ""


def event_key(text: str, ents: list[str] | None = None) -> str:
    """事件键 = 实体（排序）+ 事件族。与措辞无关，用于跨批次去重。

    「C罗退役」「C罗宣布退出国家队」「C罗告别国家队」都得到
    ``C罗#retire``，同一个键。

    没有实体时返回空串 —— 宁可这条不参与去重（会重复），也不能把
    「今天所有没实体的稿」都判成同一件事（会漏发）。
    """
    ents = ents if ents is not None else entities_in(text)
    if not ents:
        return ""
    return "|".join(sorted(ents)) + "#" + event_family(text)


def fetch_board(timeout: int = 8) -> tuple[list[dict], dict]:
    """抓热搜榜，返回 (条目, 诊断信息)。

    单独调百度/微博两个接口而不是 ``signal_sources.fetch_hot``：合并版会把
    两榜的 rank 重排成 1..n，**丢掉「它原本在第几名」这个信息**，而本模块
    恰恰要用排名判断「这事到底有多热」。同时保留 source，便于事后核对
    「这条是微博榜第 20 还是百度榜第 3」。

    2026-11-06 起抓取统一交给 ``hot_sources``（多源 + 时效闸门），本函数只保留
    签名与 ``diag`` 形状，避免调用方跟着改。**不要再往这里加源**：
    加源改 ``hot_sources.PROVIDERS``，一处就够。
    """
    try:
        import hot_sources
    except Exception as exc:                     # pragma: no cover - 依赖缺失
        return [], {"errors": [f"hot_sources import 失败: {exc}"]}
    pool = hot_sources.collect(timeout=timeout)
    diag = dict(pool.get("diag") or {})
    diag.setdefault("errors", [])
    return pool.get("board") or [], diag


def football_hits(board: list[dict], max_rank: int = DEFAULT_MAX_RANK) -> list[dict]:
    """从榜单里筛出足球相关条目，附命中的实体。"""
    out = []
    for row in board or []:
        if int(row.get("rank") or 0) > max_rank:
            continue
        word = row.get("word") or ""
        ents = entities_in(word)
        if not is_football(word, ents):
            continue
        out.append({**row, "entities": ents})
    out.sort(key=lambda x: (int(x.get("rank") or 999), -int(x.get("heat") or 0)))
    return out


def _cjk_overlap(a: str, b: str) -> int:
    """两段中文的最长连续公共子串长度（只算 ≥2 的）。粗配对够用。"""
    best = 0
    for seg in _RE_CJK.findall(b or ""):
        for n in range(len(seg), 1, -1):
            for i in range(0, len(seg) - n + 1):
                if seg[i:i + n] in (a or "") and n > best:
                    best = n
    return best


def pair_news(ents: list[str], word: str, news: list[dict]) -> list[dict]:
    """找出这条热搜的支撑报道。

    判据：标题命中同一实体；无实体时退化为「与热词有 ≥3 字连续重叠」。
    三条都要标题 —— 不拿正文片段当标题，避免「正文里顺嘴提到」被当成专题报道。
    """
    out = []
    for n in news or []:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title") or "").strip()
        if not title:
            continue
        if ents:
            if set(ents) & set(entities_in(title)):
                out.append(n)
        elif _cjk_overlap(title, word) >= 3:
            out.append(n)
    return out


def build_candidates(board: list[dict], news: list[dict], *,
                     covered_keys: set[str] | None = None,
                     max_rank: int = DEFAULT_MAX_RANK,
                     limit: int = 3) -> list[dict]:
    """热搜 + 新闻 → 可选题候选（按热度排序，已发事件剔除）。

    返回项字段：
      ``word`` 热搜原词（线索，不是标题）、``rank``/``heat``/``source``、
      ``entities`` 命中实体、``support`` 支撑报道（≤3 条）、
      ``event_key`` 去重键。
    """
    covered = set(covered_keys or set())
    out: list[dict] = []
    for hit in football_hits(board, max_rank=max_rank):
        support = pair_news(hit.get("entities") or [], hit.get("word") or "", news)
        if not support:
            # 没有报道 = 只有一句热搜词。硬写就是编，直接不产出。
            continue
        key = event_key(hit.get("word") or "", hit.get("entities") or [])
        # 没有事件族的词（「C罗妻子晒照」）不是「事件」，不能占热点位。
        # 它进不了候选，也就不会被当成一条「今日热点」写出去。
        if not key or key.endswith("#"):
            continue
        if key in covered:
            continue
        out.append({
            "word": hit.get("word"),
            "rank": hit.get("rank"),
            "heat": hit.get("heat"),
            "source": hit.get("source"),
            "entities": hit.get("entities") or [],
            "support": support[:3],
            "event_key": key,
        })
        if len(out) >= limit:
            break
    return out


def write_ledger(date_str: str, *, board: list[dict], hits: list[dict],
                 candidates: list[dict], diag: dict,
                 path: Path | None = None) -> Path | None:
    """落「今日热点榜（足球切片）+ 选中了谁 + 为什么」的台账。

    台账不是闸门，写失败不影响发布。但没有它，「C 罗退役为什么没发」
    就只能靠猜 —— 2026-11-06 这次正是因为整条链路没有一处记录「今天榜上有什么」。
    """
    p = path or (LEDGER_DIR / f"hot_board_{date_str}.json")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "date": date_str,
            "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            "board_total": len(board),
            "diag": diag,
            "football_hits": hits,
            "candidates": candidates,
            "note": ("热搜词只作选题线索；候选必须带支撑报道才会被产出。"
                     "football_hits 是榜单的足球切片，用于事后核对「今天到底热了什么」。"),
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return p
    except Exception:
        return None


def build_from_news(news: list[dict], *, covered_keys: set[str] | None = None,
                    limit: int = 1) -> list[dict]:
    """榜单不可用时的降级通道：用「同题报道条数」当热度。

    为什么需要：热搜接口都在国内（weibo.com / top.baidu.com），生产跑在
    GitHub 的 runner 上，境外访问不一定通。若把「紧跟热点」整个押在榜单上，
    一旦被墙就退回今天这种「写赛程、不跟热点」的状态，而且**看不出来**。

    判据换成「同一天里有多篇报道在讲同一件事」——这本身就是热度的一种度量，
    也正好是 ``source_watch.signals_from_articles`` 已有的口径（同题报道条数）。
    只有一个实体、且事件词能对上，才算一条候选。
    """
    covered = set(covered_keys or set())
    buckets: dict[str, dict] = {}
    for n in news or []:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title") or "").strip()
        if not title:
            continue
        ents = entities_in(title)
        if not ents:
            continue
        key = event_key(title, ents)
        if not key or key.endswith("#"):
            continue
        rec = buckets.setdefault(key, {"entities": ents, "titles": [], "key": key})
        rec["titles"].append(n)

    cands = []
    for key, rec in buckets.items():
        if key in covered:
            continue
        # 只有一条报道不算「热」：那只是普通新闻，交给 LLM 选题走原路即可。
        if len(rec["titles"]) < 2:
            continue
        cands.append({
            "word": rec["titles"][0].get("title"),
            "rank": 0,
            "heat": len(rec["titles"]),
            "source": "news_cluster",
            "entities": rec["entities"],
            "support": rec["titles"][:3],
            "event_key": key,
        })
    cands.sort(key=lambda x: -int(x.get("heat") or 0))
    return cands[:limit]


def build_from_media(media: list[dict], *, covered_keys: set[str] | None = None,
                     limit: int = 1) -> list[dict]:
    """媒体头条直接成候选（懂球帝等编辑部稿件）。

    为什么不需要「同题 ≥2 篇」：那一条门槛是给**降级通道**用的 —— 榜单不可达时
    只能拿「多少人同时在写」当热度。而媒体头条是编辑部选题，**本身就有正文**，
    标题 + 正文就是事实底本，一条就够改写。11-06 实测「C罗自宣退出葡萄牙国家队」
    就是懂球帝首条：按降级通道要同题 2 篇才放行，它永远进不来。

    仍然要求：命中登记实体 + 事件族（``event_key`` 非空且不以 ``#`` 结尾）。
    只要求实体的结果是「某某生日快乐」这类非事件稿占掉热点位。
    """
    covered = set(covered_keys or set())
    out: list[dict] = []
    for m in media or []:
        title = str((m or {}).get("word") or "").strip()
        if not title:
            continue
        ents = entities_in(title)
        if not is_football(title, ents):
            continue
        key = event_key(title, ents)
        if not key or key.endswith("#") or key in covered:
            continue
        covered.add(key)
        out.append({
            "word": title,
            "rank": int((m or {}).get("rank") or 0),
            "heat": int((m or {}).get("heat") or 0),
            "source": (m or {}).get("source") or "media",
            "entities": ents,
            "support": [{"title": title, "url": (m or {}).get("url") or "",
                         "source": (m or {}).get("source") or "media"}],
            "event_key": key,
            "providers": [(m or {}).get("source") or "media"],
        })
        if len(out) >= limit:
            break
    return out


def pick_candidates(board: list[dict], media: list[dict], news: list[dict], *,
                    covered_keys: set[str] | None = None,
                    limit: int = 1,
                    max_rank: int = DEFAULT_MAX_RANK) -> tuple[list[dict], str]:
    """三条通道合一，返回 (候选, 用了哪条通道)。

    优先级就是「证据强弱」：

      1. ``board``   榜单上有名次 —— 说明**大家真的在谈**（最强热度证据）；
      2. ``media``   编辑部头条 —— 说明**有事实底本**（最强事实证据）；
      3. ``news_cluster`` 同题 ≥2 篇 —— 榜单不可达时的降级。

    为什么不是「先 media 后 board」：热点通道的价值是「跟上今天在谈什么」，
    没有热度的媒体稿交给 LLM 选题走原路即可，不必占这个稀缺槽位。
    但榜单常被墙（境外 runner），所以 media 必须能独立成候选 —— 否则通道
    会长期空转，而且看不出来。

    同一条事件在多源出现时**只出一条候选**，``providers`` 记下它上过哪些源：
    「微博 + 贴吧 + 懂球帝 都在说」本身是最强的热度证据，也方便事后复盘。
    """
    # base = 今日已发的事件键（要跳过）；seen = 本次已产出的候选（要合并而不是跳过）。
    # 两者必须分开：若把「本轮已产出」也塞进 base 传给各通道，媒体稿在合并之前
    # 就被自己的 board 兄弟挡住了，providers 永远只有一个源。
    base = set(covered_keys or set())
    covered = set(base)
    picked: list[dict] = []
    by_key: dict[str, dict] = {}

    def _absorb(items: list[dict], channel: str) -> None:
        for c in items:
            key = str(c.get("event_key") or "")
            if not key:
                continue
            srcs = list(c.get("providers") or [c.get("source") or ""])
            if key in by_key:
                # 同一事件已在池里：合并来源，保留最先（=最强热度）那条的名次
                prev = by_key[key]
                prev["providers"] = sorted({*prev.get("providers", []), *srcs})
                # 支撑报道取并集，正文底本越多越不容易抓不到
                seen = {str((s or {}).get("url") or "") for s in prev.get("support", [])}
                for s in c.get("support") or []:
                    if str((s or {}).get("url") or "") not in seen:
                        prev.setdefault("support", []).append(s)
                continue
            if key in covered:
                continue
            rec = dict(c)
            rec["providers"] = sorted({*srcs})
            rec["channel"] = channel
            by_key[key] = rec
            picked.append(rec)
            covered.add(key)

    _absorb(build_candidates(board, news, covered_keys=base,
                             max_rank=max_rank, limit=max(limit * 4, 8)),
            "board")
    _absorb(build_from_media(media, covered_keys=base,
                             limit=max(limit * 4, 8)), "media")
    if not picked:
        _absorb(build_from_news(news, covered_keys=base, limit=limit),
                "news_cluster")

    # 榜单候选已在 build_candidates 里按 rank 排好；媒体候选按 rank 排。
    # 这里稳定排序：有真实名次（rank>0 且来自 board）的优先，其余保持原序。
    picked.sort(key=lambda c: (0 if c.get("channel") == "board" else 1,
                               int(c.get("rank") or 999)))
    channel = picked[0]["channel"] if picked else "none"
    return picked[:limit], channel
