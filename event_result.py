"""节点事件「结果稿」的多源交叉验证（计划 11.1 时效闸门 / 13.4 原始信源纪律）。

## 为什么需要这个模块

揭晓型事件（金球奖 / 抽签 / 官宣 / 颁奖）过了 reveal_at 之后，赛前稿立刻变成旧闻，
正确动作是改发**结果稿**。但「谁获奖」这个事实只在揭晓之后才存在，预生成拿不到，
只能开奖后自己去抓、自己去对。

抓来的东西不核对就写，等于把外媒预测当既成事实。2026-10-26 10:28 懂球帝上就摆着一条
《【CBS Sports】姆巴佩夺金球》——而真正的开奖要到 10-27 03:00。只凭这一条写稿，
就是把一天的悬念写成结论。

## 判定链（五道，全过才算数）

1. **时间闸门**：条目发布时间必须**晚于 reveal_at**。这一刀挡掉全部赛前预热、
   预测、提名分析——它们全都发在揭晓之前，无论措辞多像结论。
2. **关键词命中**：标题/正文含事件关键词（金球奖 / Ballon d'Or …）。
3. **结果句式**：含完成态动词（获得/荣获/当选/摘得/夺得/加冕/折桂…），
   且**不含**预测标记（预计/有望/或将/领跑/热门/预测/悬念/明晨/今晚…）。
4. **姓名抽取**：按固定句式抽获奖者，归一化后去重（只做规则抽取，
   不让模型自由发挥——名字抽错一个就是编造）。
5. **多源一致**：同一姓名必须出现在 **≥2 个不同站点**。
   出现 ≥2 个不同姓名 → 判「冲突」不发；一个都抽不出 → 判「未证实」不发。

任何一步不过都返回 ok=False 并写清原因，落到 ``data/audit/event_result_latest.json``。
「为什么没发」必须可复盘：是没抓到、抓到但冲突、还是句式不认识。三者修法完全不同。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
AUDIT_PATH = AUDIT_DIR / "event_result_latest.json"

# 揭晓后至少等这么久再判：媒体落地需要时间，过早抓只有零散快讯
MIN_LAG_MINUTES = 20

# 完成态动词：必须真的「已经发生」。
# 单字「夺」也算——结果出炉后媒体大量用「XX夺金球」这种简写，
# 不认就会漏掉一整类源；「争夺/争夺战」由 RUMOR_MARKERS 先一步挡掉。
DONE_VERBS = (
    "获得", "荣获", "荣膺", "摘得", "摘走", "夺得", "夺走", "拿下", "捧起", "夺",
    "当选", "加冕", "折桂", "问鼎", "首夺", "再夺", "获奖", "获评", "中选",
)

# 预测/悬念标记：出现即判为「还没揭晓」，一律不作结果
RUMOR_MARKERS = (
    "预计", "有望", "或将", "将会", "可能", "也许", "恐怕", "领跑", "热门",
    "预测", "悬念", "前瞻", "展望", "候选", "提名", "呼声", "争夺", "谁会",
    "谁能", "谁该", "明晨", "明晚", "今晚", "今夜", "即将", "倒计时", "赔率",
    "？", "?",
)

# 姓名候选的停用词：这些「2~5 字中文」不是人名
NAME_STOPWORDS = {
    "官方", "记者", "媒体", "球迷", "主帅", "教练", "队长", "队友", "球队",
    "俱乐部", "联赛", "法国", "欧洲", "世界", "国际", "年度", "本届", "今年",
    "首位", "史上", "金球", "金球奖", "法国足球", "队报", "颁奖", "典礼",
    "得主", "获奖者", "名单", "投票", "评委", "他", "她", "该奖", "此奖",
    "球员", "前锋", "中场", "后卫", "门将", "巨星", "球星", "新科", "冠军",
    "新王", "王者", "颁奖礼", "盛典", "揭晓",
}

# 源权重：官方/权威优先，但**每个源只算一票**，权重只用于证据排序
SOURCE_LABELS = {
    "zhibo8": "直播吧",
    "dongqiudi": "懂球帝",
    "sina": "新浪体育",
    "netease": "网易体育",
}


# ----------------------------------------------------------------------
#  时间处理
# ----------------------------------------------------------------------

def parse_published_at(raw: str, now: datetime | None = None) -> datetime | None:
    """把各源五花八门的发布时间解析成 CST datetime，解析不出返回 None。

    见过的三种形态（都是真实现场）：
      - 懂球帝 payload：``2026-10-26 10:28:14``
      - 懂球帝 HTML：  ``10-25 06:01``（缺年份！）
      - 直播吧：       URL 里只有日期 ``/zuqiu/2026-10-27/``（无时刻）

    缺年份时按当前年补；补出来比 now 还晚一天以上，说明是跨年的去年条目，减一年。
    **解析不出就是 None，绝不用「今天」顶替**——那会把旧稿当新稿放行。
    """
    now = now or datetime.now(CST)
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=CST)
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        hh, mi = int(m.group(3) or 0), int(m.group(4) or 0)
        try:
            dt = datetime(now.year, mo, d, hh, mi, tzinfo=CST)
        except ValueError:
            return None
        if dt > now + timedelta(days=1):
            try:
                dt = dt.replace(year=now.year - 1)
            except ValueError:
                return None
        return dt
    return None


def date_from_url(url: str, now: datetime | None = None) -> datetime | None:
    """从 URL 路径里抽日期（直播吧 ``/zuqiu/2026-10-27/xxx.htm``），只有日精度。

    只有日期时按当天 00:00 记——对「晚于 reveal_at」的判定是**保守**的
    （当天的条目未必在揭晓后发），所以调用方还要靠句式与多源兜底。
    """
    now = now or datetime.now(CST)
    m = re.search(r"/(\d{4})-(\d{2})-(\d{2})/", url or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        0, 0, tzinfo=CST)
    except ValueError:
        return None


# ----------------------------------------------------------------------
#  姓名抽取
# ----------------------------------------------------------------------

_CN_NAME = r"[\u4e00-\u9fa5·]{2,6}"

# 动词按长度倒序拼进正则：长词优先，否则「首夺」会被单字「夺」抢走，
# 结果是「登贝莱首夺金球奖」抽出「登贝莱首」（实测踩过）。
_DONE_ALT = "|".join(sorted(DONE_VERBS, key=len, reverse=True))

# 姓名左边界：名字前面必须是开头/标点/括号/空白。
# 没有这个断言，「2026金球奖：凯恩力压登贝莱折桂」会从「压」后面起抽，
# 抽出「恩力压登贝莱」——中文字符串没有天然词边界，只能靠上下文卡。
_LEFT_EDGE = r"(?:^|[：:，,、；;。\s（()【】\[\]「」『』""“”]|据|官方|确认)"

# 句式 → 姓名所在分组。中文体育媒体写「谁拿了」就这么几种，穷举比模型猜可靠。
# 顺序即优先级：越 specific 的越前。「金球奖得主X」必须排在「金球奖…：Y」前面，
# 否则「金球奖得主登贝莱：最好的一年」会被后者的冒号句式抢答成「最好的一年」。
# 裸姓名（无动词紧邻）必须带右边界，否则会把冒号后整句当名字。
_NAME_PATTERNS = [
    # 金球奖得主登贝莱 / 金球奖得主登贝莱：……
    re.compile(rf"金球奖得主\s*(?P<n>[\u4e00-\u9fa5·]{{2,5}})(?=[：:，,。；;、\s]|$|{_DONE_ALT})"),
    # 官方：登贝莱荣膺2026金球奖
    re.compile(rf"{_LEFT_EDGE}\s*(?P<n>{_CN_NAME}?)\s*(?:{_DONE_ALT})[^，。；]{{0,12}}?金球"),
    # 登贝莱获得2026年金球奖 / 登贝莱首夺金球奖
    re.compile(rf"{_LEFT_EDGE}\s*(?P<n>{_CN_NAME}?)\s*(?:{_DONE_ALT})[^，。；]{{0,8}}"),
    # 2026金球奖揭晓：登贝莱当选
    re.compile(rf"金球奖[^，。；]{{0,10}}?[:：]\s*(?P<n>[\u4e00-\u9fa5·]{{2,5}})"
               rf"(?=[：:，,。；;、\s]|$|{_DONE_ALT})"),
]

# 名字前后常见的粘连词，抽出来要剥掉（「队报：登贝莱」→「登贝莱」）
_LEAD_NOISE = ("官方", "确认", "快讯", "突发", "最新", "刚刚", "据", "记者", "队报", "法国足球")
# 末尾的序数修饰：「登贝莱首」「亚马尔第2次」→ 剥掉
_TAIL_NOISE = re.compile(r"(?:第?\d+次?|首|再|首次|第二次|二度|三度)+$")
# 名字里出现这些连接/比较词，说明抽到的是一串并列语或比较句，不是一个人
_NAME_JOINERS = ("与", "和", "、", "及", "或", "vs", "VS", "对",
                 "力压", "击败", "战胜", "压过", "超越", "领先", "落后",
                 "不敌", "憾负", "胜过", "淘汰", "力克", "险胜")


def _clean_name(raw: str) -> str:
    s = (raw or "").strip().strip("：:，,、 ")
    changed = True
    while changed:
        changed = False
        for p in _LEAD_NOISE:
            if s.startswith(p) and len(s) > len(p) + 1:
                s = s[len(p):].lstrip("：: ")
                changed = True
    s2 = _TAIL_NOISE.sub("", s)
    if len(s2) >= 2:
        s = s2
    return s.strip()


def _is_name_like(s: str) -> bool:
    if not s or not (2 <= len(s) <= 6):
        return False
    if s in NAME_STOPWORDS:
        return False
    # 含「与/和/、/及」这类并列词 → 抽到的是一串人，不是一个人
    if any(j in s for j in _NAME_JOINERS):
        return False
    # 代词/指示词开头：那不是名字，是「他将夺得金球奖」这类句式抽出来的残渣
    if s[0] in "他她它这该此其谁您我你":
        return False
    # 候选里带上「明晨/今晚/预计」这类时间/预测词 → 是句子碎片，不是人名
    if any(m in s for m in RUMOR_MARKERS):
        return False
    if any(w in s for w in ("主帅", "教练", "记者", "官方", "球队", "球迷", "金球")):
        return False
    # 必须含中文，且不能是纯功能词
    return bool(re.search(r"[\u4e00-\u9fa5]", s))


def normalize_name(name: str) -> str:
    """归一姓名用于跨源比对：去中点/空格/标点。

    不做「取后两字」这类激进归并——「登贝莱」和「穆萨·登贝莱」若真是两个人，
    错误归并的代价是写错人。宁可判成两个名字→判冲突→不发。
    """
    return re.sub(r"[·\s\.\-_'’]", "", name or "")


def extract_winner(text: str) -> str | None:
    """从一条标题/摘要里抽「被指认为获奖者」的姓名，抽不出返回 None。"""
    s = (text or "").strip()
    if not s or "金球" not in s:
        return None
    for pat in _NAME_PATTERNS:
        for m in pat.finditer(s):
            name = _clean_name(m.group("n"))
            if _is_name_like(name):
                return name
    return None


# ----------------------------------------------------------------------
#  判定
# ----------------------------------------------------------------------

def is_result_sentence(text: str) -> tuple[bool, str]:
    """这条文本是不是「已经揭晓」的表述。返回 (是否结果句, 原因)。"""
    s = text or ""
    if not s:
        return False, "空文本"
    hits_rumor = [m for m in RUMOR_MARKERS if m in s]
    if hits_rumor:
        return False, f"含预测/悬念标记 {hits_rumor[:3]}"
    # 「金球奖得主 X」本身就是完成态（没有动词），不认会漏掉最常见的一种写法；
    # 但预测类已在上一步被 RUMOR_MARKERS 挡掉（「预测金球奖得主」含「预测」）。
    if "得主" in s or any(v in s for v in DONE_VERBS):
        return True, ""
    return False, "无完成态动词"


def verify(mentions: list[dict], *, keywords: list[str], reveal_at: datetime,
           min_sources: int = 2, published_unknown_ok: bool = False) -> dict:
    """对已抓取的条目做五道判定，返回判定结果。

    Args:
        mentions: [{"source","title","published_at","url","text"}]，
                  ``published_at`` 为原始字符串，解析失败视为时间未知。
        keywords: 事件关键词（任一命中即可）。
        reveal_at: 揭晓时刻；发布时间早于它的一律丢弃。
        min_sources: 至少几个**不同站点**指认同一姓名。
        published_unknown_ok: 时间未知的条目是否仍可参与（默认否，宁缺勿错）。
    """
    now = datetime.now(CST)
    kept, dropped = [], []
    keyword_hits = 0

    for m in mentions:
        title = (m.get("title") or "").strip()
        text = (m.get("text") or "").strip()
        blob = f"{title}\n{text}" if text else title
        src = m.get("source") or "?"
        if not any(k and k in blob for k in keywords):
            continue
        keyword_hits += 1

        pub = parse_published_at(m.get("published_at") or "", now=now)
        time_src = "explicit"
        if pub is None:
            pub = date_from_url(m.get("url") or "", now=now)
            time_src = "url_date" if pub else ""
        if pub is None:
            if not published_unknown_ok:
                dropped.append({"source": src, "title": title[:80],
                                "reason": "时间未知（不猜发布时间）"})
                continue
            time_src = "unknown"

        # 时间闸门：早于揭晓时刻就是赛前稿，措辞再像结论也不能用
        if pub is not None and pub < reveal_at:
            dropped.append({"source": src, "title": title[:80],
                            "reason": f"发布时间 {pub.strftime('%m-%d %H:%M')} 早于揭晓 "
                                      f"{reveal_at.strftime('%m-%d %H:%M')}"})
            continue

        # 标题优先、正文兜底。注意不能写 `a() or b()`——两个都返回二元组，
        # 前一个恒为真值，后一个永远不执行（这个坑踩过一次）。
        ok_sentence, why = is_result_sentence(title)
        if not ok_sentence:
            ok_sentence, why = is_result_sentence(blob)
        if not ok_sentence:
            dropped.append({"source": src, "title": title[:80], "reason": why})
            continue

        name = extract_winner(title) or extract_winner(blob)
        if not name:
            dropped.append({"source": src, "title": title[:80],
                            "reason": "结果句但抽不出姓名"})
            continue

        kept.append({"source": src, "title": title[:80], "url": m.get("url") or "",
                     "published_at": pub.strftime("%Y-%m-%d %H:%M") if pub else "",
                     "time_source": time_src, "name": name,
                     "name_norm": normalize_name(name)})

    if not kept:
        reason = ("未抓到含关键词的条目" if keyword_hits == 0
                  else f"命中 {keyword_hits} 条但均未通过时间/句式/姓名判定")
        return {"ok": False, "winner": "", "sources": [], "kept": [],
                "dropped": dropped[:20], "keyword_hits": keyword_hits,
                "reason": reason}

    # 按归一姓名聚合各源
    by_name: dict[str, dict] = {}
    for k in kept:
        rec = by_name.setdefault(k["name_norm"], {"names": set(), "rows": []})
        rec["names"].add(k["name"])
        rec["rows"].append(k)

    best = None
    for norm, rec in by_name.items():
        srcs = sorted({r["source"] for r in rec["rows"]})
        if len(srcs) < min_sources:
            continue
        cand = {"name_norm": norm, "sources": srcs, "rows": rec["rows"]}
        if best is None or len(srcs) > len(best["sources"]):
            best = cand

    if best is None:
        # 分两种，修法完全不同：
        #   ① 多个不同姓名 → 「说法打架」，可能是抽取规则不认识某句式，也可能是真冲突；
        #   ② 只有一个姓名但源不够 → 「没证实」，多半只是一家抄另一家。
        # 一律报「源不足」会掩盖 ①，事后复盘会白跑一圈。
        names_seen = sorted({n for rec in by_name.values() for n in rec["names"]})
        if len(by_name) > 1 and len(names_seen) > 1:
            detail = "、".join(
                f"{'/'.join(sorted({r['source'] for r in rec['rows']}))}:{sorted(rec['names'])[0]}"
                for rec in by_name.values())
            return {"ok": False, "winner": "", "sources": [], "kept": kept,
                    "dropped": dropped[:20], "keyword_hits": keyword_hits,
                    "reason": f"多源指认不一致（{detail}），且无任一姓名达到 "
                              f"{min_sources} 源，事实冲突不发"}
        detail = "、".join(
            f"{'/'.join(sorted({r['source'] for r in rec['rows']}))}:{sorted(rec['names'])[0]}"
            for rec in by_name.values())
        return {"ok": False, "winner": "", "sources": [], "kept": kept,
                "dropped": dropped[:20], "keyword_hits": keyword_hits,
                "reason": f"独立源不足 {min_sources} 个（{detail}）"}

    # 冲突只认「同样达标的两个不同姓名」：比如两家说登贝莱、另两家说亚马尔。
    # 不能拿「1 个源说登贝莱 + 1 个源说亚马尔」来判冲突——那本来就还在「源不足」档，
    # 而 1 源的杂音多半是抽取噪声，把它算成冲突会把正常稿件误杀。
    qualified = {norm: rec for norm, rec in by_name.items()
                 if len({r["source"] for r in rec["rows"]}) >= min_sources}
    if len(qualified) > 1:
        return {"ok": False, "winner": "", "sources": [], "kept": kept,
                "dropped": dropped[:20], "keyword_hits": keyword_hits,
                "reason": "多源指认不一致："
                          + "、".join(sorted({n for rec in qualified.values() for n in rec["names"]}))}

    return {
        "ok": True,
        "winner": best["rows"][0].get("name") or "",
        "sources": best["sources"],
        "kept": best["rows"],
        "dropped": dropped[:20],
        "keyword_hits": keyword_hits,
        "reason": "",
    }


# ----------------------------------------------------------------------
#  抓取 + 一键解析
# ----------------------------------------------------------------------

def collect_mentions(scraper=None, *, max_per_source: int = 40) -> tuple[list[dict], list[str]]:
    """从多个站点抓今日条目。返回 (mentions, 失败的源名)。

    单个源失败不阻断——但会记进 errors，因为「只剩一个源」等于交叉验证失效，
    必须能从审计里看出是「两家都说 A」还是「只抓到一家」。
    """
    if scraper is None:
        from media_scraper import SportsScraper
        scraper = SportsScraper()

    mentions, errors = [], []
    tasks = (
        ("zhibo8", lambda: scraper.scrape_football_news(max_articles=max_per_source)),
        ("dongqiudi", lambda: scraper.scrape_dongqiudi_headlines(max_articles=max_per_source)),
    )
    for name, fn in tasks:
        try:
            rows = fn() or []
        except Exception as e:                     # 抓取失败绝不拖垮发布主流程
            errors.append(f"{name}: {type(e).__name__} {str(e)[:80]}")
            continue
        if not rows:
            errors.append(f"{name}: 返回 0 条")
        for r in rows:
            mentions.append({
                "source": name,
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "published_at": r.get("published_at") or "",
                "text": r.get("article_text") or "",
            })
    return mentions, errors


def audit(result: dict, *, node_id: str = "", keywords: list[str] | None = None,
          reveal_at: datetime | None = None, errors: list[str] | None = None) -> Path:
    """把判定过程落审计。**不发也要落**——「为什么没发」比「发了什么」更需要复盘。"""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "checked_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "node_id": node_id,
        "keywords": keywords or [],
        "reveal_at": reveal_at.strftime("%Y-%m-%d %H:%M") if reveal_at else "",
        "ok": bool(result.get("ok")),
        "winner": result.get("winner", ""),
        "sources": result.get("sources", []),
        "reason": result.get("reason", ""),
        "keyword_hits": result.get("keyword_hits", 0),
        "kept": (result.get("kept") or [])[:20],
        "dropped": (result.get("dropped") or [])[:20],
        "source_errors": errors or [],
    }
    AUDIT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    return AUDIT_PATH


def resolve(topic: dict, *, scraper=None, min_sources: int = 2,
            now: datetime | None = None) -> dict:
    """一站式：读节点配置 → 抓源 → 判定 → 落审计。返回判定 dict（含 ok/winner/sources）。

    ``topic`` 为 deterministic_supply 产出的结果期选题，需带
    ``result_keywords`` / ``reveal_at`` / ``node_id``。
    """
    now = now or datetime.now(CST)
    keywords = topic.get("result_keywords") or []
    reveal_dt = parse_published_at(topic.get("reveal_at") or "", now=now)
    if not keywords or reveal_dt is None:
        res = {"ok": False, "winner": "", "sources": [], "kept": [], "dropped": [],
               "keyword_hits": 0, "reason": "节点未配置 result_keywords 或 reveal_at"}
        audit(res, node_id=topic.get("node_id", ""), keywords=keywords)
        return res

    # 揭晓后太早：媒体还没落地，抓了也是零散快讯
    if now < reveal_dt + timedelta(minutes=MIN_LAG_MINUTES):
        res = {"ok": False, "winner": "", "sources": [], "kept": [], "dropped": [],
               "keyword_hits": 0,
               "reason": f"揭晓未满 {MIN_LAG_MINUTES} 分钟，等媒体落地"}
        audit(res, node_id=topic.get("node_id", ""), keywords=keywords,
              reveal_at=reveal_dt)
        return res

    mentions, errors = collect_mentions(scraper)
    res = verify(mentions, keywords=keywords, reveal_at=reveal_dt,
                 min_sources=min_sources)
    audit(res, node_id=topic.get("node_id", ""), keywords=keywords,
          reveal_at=reveal_dt, errors=errors)
    return res
