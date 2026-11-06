# -*- coding: utf-8 -*-
"""热点源接入层：把「榜单 / 媒体头条 / UGC 话题」统一成同一种候选行。

## 为什么要单独一层（2026-11-06）

`hot_topics` 原来只认百度 + 微博两个热搜接口。用户要求加入主流足球媒体源，
但如果每接一个源就在 `hot_topics` 里写一段 `if 源 == ...`，很快会变成第二个
「8 个供给入口各写各的」——**先定协议，再加源**。

三个字段决定一个源能干什么，这是本模块唯一的知识：

    字段          取值                     决定
    ------------  -----------------------  ------------------------------------
    kind="board"  热搜/话题榜              只能当「今天大家在谈什么」
    kind="media"  编辑部稿件列表           可当事实底本（有正文可抓）
    kind="ugc"    贴吧等用户话题           只能当热度信号，**绝不能当事实底本**
    tier          "P0"/"P1"/"P2"          接不接、先接谁

## 三条纪律

1. **不新增时间解析**。发布时间统一交给 `utils.filter_fresh_news` —— 仓里已经
   有一份（`parse_news_time` 处理相对时间/绝对时间/URL 日期），再写一份就是
   11-05「同一量两处定义」的老毛病。本模块只声明阈值。
2. **拿不到时间 ≠ 新鲜**。媒体报道必须过时效闸门，`stale` 直接剔除并计数。
   2026-11-06 实测新浪体育 RSS 的 pubDate 全是 2018-09：接进去就是旧闻新发。
3. **一个源挂了不影响别的源**。每个 provider 独立 try/except + 独立超时，
   失败只进 `diag`，永不抛出。境外 runner 连不上国内源是常态。
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")

#: 热点素材时效（小时）。比 `utils.NEWS_MAX_AGE_HOURS`（72）严得多。
#:
#: 为什么不能共用：72 小时是「新闻素材」的闸门（够写一篇迟到的深度稿），
#: 而热点要的是「今天大家在谈什么」——三天前的热搜不是热点，是旧闻。
#: 两条阈值量的不是同一个东西，所以各自声明，但**解析器共用**。
HOT_MEDIA_MAX_AGE_HOURS = 36

#: 媒体区取多少条。够配对就行，多了反而让「同题 ≥2 篇」降级通道失真。
MEDIA_MAX_ITEMS = 30

#: 源清单。`enabled=False` 的不是「坏了」，是「实测不可用/不合规」，留档备查。
#:
#: 状态依据 `scripts/probe_media_sources.py` 的 2026-11-06 实测：
#:   腾讯体育 19KB 纯 JS 壳 / 虎扑 226KB 全 JS 渲染（朴素抓取 0 条中文标题）
#:   / 球天下 Cloudflare 挑战页 / k7 体育查不到正规新闻站（博彩 SEO 软文）
PROVIDERS: tuple[dict, ...] = (
    {"name": "baidu", "kind": "board", "tier": "P0", "enabled": True,
     "note": "百度热搜，rank 保留原始名次"},
    {"name": "weibo", "kind": "board", "tier": "P0", "enabled": True,
     "note": "微博热搜"},
    {"name": "dongqiudi", "kind": "media", "tier": "P0", "enabled": True,
     "note": "懂球帝头条；正文可抓，可作事实底本"},
    {"name": "tieba", "kind": "ugc", "tier": "P0", "enabled": True,
     "note": "贴吧话题榜；只作热度信号，不当事实底本"},
    # ---- 以下为实测不可用，默认关闭（不要因为「加了源」就打开）----
    {"name": "sina_sports", "kind": "media", "tier": "P1", "enabled": False,
     "note": "首页可解析；RSS pubDate 全为 2018-09（死档），接入前必须先修时效"},
    {"name": "tencent_sports", "kind": "media", "tier": "P2", "enabled": False,
     "note": "纯 JS 壳，需 API"},
    {"name": "hupu", "kind": "media", "tier": "P2", "enabled": False,
     "note": "全 JS 渲染，朴素抓取 0 条"},
    {"name": "goal_cn", "kind": "media", "tier": "P3", "enabled": False,
     "note": "年龄门 + 博彩广告同页，需合规评审"},
    {"name": "qiutianxia", "kind": "media", "tier": "P3", "enabled": False,
     "note": "Cloudflare 挑战页"},
    {"name": "k7", "kind": "media", "tier": "P3", "enabled": False,
     "note": "查不到正规新闻站，命中结果均为博彩平台 SEO（用户未提供域名前不接）"},
)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
              "AppleWebKit/605.1.15 Mobile/15E148")

#: 贴吧话题榜接口（移动端 JSON，需 Referer；网页版是 JS 渲染，抓不到）
TIEBA_HOT_URL = "https://tieba.baidu.com/hottopic/browse/topicList"


def _row(word, *, rank, heat, source, kind, url="", published_at="") -> dict:
    return {"word": str(word or "").strip(), "rank": int(rank or 0),
            "heat": int(heat or 0), "source": source, "kind": kind,
            "url": str(url or ""), "published_at": str(published_at or "")}


# ------------------------------------------------------------
# providers
# ------------------------------------------------------------
def fetch_baidu(timeout: int = 8) -> list[dict]:
    import signal_sources
    items = signal_sources.fetch_baidu_hot(timeout=timeout) or []
    return [_row(it.get("word"), rank=it.get("rank") or i + 1,
                 heat=it.get("num") or 0, source="baidu", kind="board")
            for i, it in enumerate(items) if str(it.get("word") or "").strip()]


def fetch_weibo(timeout: int = 8) -> list[dict]:
    import signal_sources
    items = signal_sources.fetch_weibo_hot(timeout=timeout) or []
    return [_row(it.get("word"), rank=it.get("rank") or i + 1,
                 heat=it.get("num") or 0, source="weibo", kind="board")
            for i, it in enumerate(items) if str(it.get("word") or "").strip()]


def fetch_tieba(timeout: int = 8) -> list[dict]:
    """贴吧话题榜（全站）。**UGC，只能当热度**。

    实测（11-06）：全站榜 30 条里足球只占 3 条。所以它不能当选题来源，
    只能回答「这条足球新闻在贴吧有没有人聊」——用于给已有候选加一档热度，
    或让某条媒体稿确认「确实在发酵」。
    """
    import requests
    r = requests.get(TIEBA_HOT_URL, timeout=timeout,
                     headers={"User-Agent": _UA_MOBILE,
                              "Referer": "https://tieba.baidu.com/"})
    data = r.json()
    topics = ((data.get("data") or {}).get("bang_topic") or {}).get("topic_list") or []
    out: list[dict] = []
    for i, t in enumerate(topics):
        name = str((t or {}).get("topic_name") or "").strip()
        if not name:
            continue
        out.append(_row(name, rank=i + 1, heat=(t or {}).get("discuss_num") or 0,
                        source="tieba", kind="ugc",
                        url=str((t or {}).get("topic_url") or "")))
    return out


def fetch_dongqiudi(timeout: int = 8, max_items: int = MEDIA_MAX_ITEMS) -> list[dict]:
    """懂球帝头条。**media** 类：有正文链接，可作事实底本。

    这是本轮最实质的一条供给：11-06 实测「C罗自宣退出葡萄牙国家队」就在
    懂球帝首条 —— 源早就有，只是从没接进热点池。
    `published_at` 由采集器从链接文本里抽出（「07-09 11:15」/「3小时前」），
    交给 `utils.filter_fresh_news` 判定时效。
    """
    from media_scraper import SportsScraper
    heads = SportsScraper().scrape_dongqiudi_headlines(max_articles=max_items) or []
    out: list[dict] = []
    for i, a in enumerate(heads):
        title = str(a.get("title") or "").strip()
        if not title:
            continue
        out.append(_row(title, rank=i + 1, heat=0, source="dongqiudi", kind="media",
                        url=str(a.get("url") or ""),
                        published_at=str(a.get("published_at") or "")))
    return out


_FETCHERS = {"baidu": fetch_baidu, "weibo": fetch_weibo,
             "tieba": fetch_tieba, "dongqiudi": fetch_dongqiudi}


# ------------------------------------------------------------
# 时效
# ------------------------------------------------------------
def apply_freshness(rows: list[dict], *, max_hours: int = HOT_MEDIA_MAX_AGE_HOURS,
                    now: datetime | None = None) -> tuple[list[dict], list[dict], int]:
    """按时效闸门筛媒体行，返回 (kept, dropped, unknown_count)。

    只对 `kind="media"` 生效：榜单本身没有发布时间，它「此刻在榜上」就是新鲜的；
    拿榜单套时间会把它全判成 unknown，等于白做。

    解析器复用 `utils.filter_fresh_news`（单一口径），这里不做第二套。
    """
    media = [r for r in rows if r.get("kind") == "media"]
    others = [r for r in rows if r.get("kind") != "media"]
    if not media:
        return others, [], 0
    import utils
    kept, dropped, unknown = utils.filter_fresh_news(
        media, max_hours=max_hours, now=now, label="热点媒体")
    return others + kept, list(dropped), unknown


# ------------------------------------------------------------
# 汇总
# ------------------------------------------------------------
def collect(*, timeout: int = 8, now: datetime | None = None) -> dict:
    """抓所有启用的源，返回 {"board": [...], "media": [...], "ugc": [...], "diag": {...}}。

    `diag` 逐源记录「拿到几条 / 报了什么错 / 时效剔了几条」——台账里必须能看出
    「今天没热点」和「源挂了」的区别，这两种情况的处置完全不同。
    """
    diag: dict = {"providers": {}, "errors": [], "stale_dropped": 0, "unknown_time": 0}
    rows: list[dict] = []
    for p in PROVIDERS:
        name = p["name"]
        if not p.get("enabled"):
            diag["providers"][name] = {"status": "disabled", "count": 0,
                                       "note": p.get("note", "")}
            continue
        fn = _FETCHERS.get(name)
        if fn is None:
            diag["providers"][name] = {"status": "no_fetcher", "count": 0}
            continue
        t0 = datetime.now(CST)
        try:
            got = fn(timeout=timeout) or []
            diag["providers"][name] = {
                "status": "ok" if got else "empty", "count": len(got),
                "kind": p.get("kind"), "tier": p.get("tier"),
                "elapsed_s": round((datetime.now(CST) - t0).total_seconds(), 2),
            }
            rows.extend(got)
        except Exception as exc:                       # pragma: no cover - 网络
            diag["providers"][name] = {"status": "error", "count": 0,
                                       "error": f"{type(exc).__name__}: {exc}"[:160]}
            diag["errors"].append(f"{name}: {type(exc).__name__}")

    rows, dropped, unknown = apply_freshness(rows, now=now)
    diag["stale_dropped"] = len(dropped)
    diag["unknown_time"] = unknown
    if dropped:
        # 只记「哪几条被时效拦了」，不记正文 —— 这是 11-06 新浪死档的教训：
        # 不记就无法在事后解释「为什么今天一条媒体热点都没有」。
        diag["stale_samples"] = [
            {"title": str((it or {}).get("word") or "")[:40],
             "age_h": round(age, 1) if isinstance(age, (int, float)) else None}
            for it, age in dropped[:5]
        ]

    board = [r for r in rows if r.get("kind") in ("board", "ugc")]
    media = [r for r in rows if r.get("kind") == "media"]
    ugc = [r for r in rows if r.get("kind") == "ugc"]
    board.sort(key=lambda x: (int(x.get("rank") or 999), -int(x.get("heat") or 0)))
    return {"board": board, "media": media, "ugc": ugc, "diag": diag}


def article_text_of(url: str) -> str:
    """按域名抓正文（热点稿的事实底本）。

    两个源走两套解析器，所以要有这个派发点；调用方只管「给我正文」，
    不该关心是直播吧还是懂球帝。
    """
    url = str(url or "")
    if not url:
        return ""
    from media_scraper import SportsScraper
    s = SportsScraper()
    try:
        if "dongqiudi.com" in url:
            art = s.scrape_dongqiudi_article(url)
            return str((art or {}).get("article_text") or "")
        return str(s.scrape_zhibo8_article_content(url) or "")
    except Exception:
        return ""


def selftest() -> list[tuple[str, bool]]:
    """不联网的自检：只验时效闸门与行结构（联网那部分交给 probe 脚本）。"""
    cases: list[tuple[str, bool]] = []
    now = datetime(2026, 11, 6, 14, 0, tzinfo=CST)

    # 媒体行：4 个月前 → stale，必须被剔（这就是新浪 RSS 2018 死档的形态）
    old = [_row("旧闻", rank=1, heat=0, source="dongqiudi", kind="media",
                published_at="07-09 11:15")]
    kept, dropped, _ = apply_freshness(old, now=now)
    cases.append(("4 个月前的头条被时效剔除", not kept and len(dropped) == 1))

    # 媒体行：3 小时前 → 保留
    fresh = [_row("C罗退出国家队", rank=1, heat=0, source="dongqiudi", kind="media",
                  published_at="3小时前")]
    kept2, _d, _ = apply_freshness(fresh, now=now)
    cases.append(("3 小时前的头条保留", len(kept2) == 1))

    # 榜单行不套时效（榜上没有发布时间，此刻在榜即新鲜）
    board = [_row("C罗退出国家队", rank=2, heat=900, source="weibo", kind="board")]
    kept3, _d3, _ = apply_freshness(board, now=now)
    cases.append(("榜单行不受时效闸门影响", len(kept3) == 1))

    # kind 必须被保留：ubg 不能当事实底本
    cases.append(("ugc 行保留 kind", kept3[0]["kind"] == "board"))
    return cases


def main() -> int:
    import sys
    if "--selftest" in sys.argv:
        bad = 0
        for name, ok in selftest():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            bad += 0 if ok else 1
        print(f"\n自检 {len(selftest())} 项，失败 {bad} 项")
        return 1 if bad else 0
    if "--collect" in sys.argv:
        import json
        out = collect()
        print(json.dumps({k: v for k, v in out["diag"].items()}, ensure_ascii=False, indent=2))
        print(f"board {len(out['board'])} / media {len(out['media'])} / ugc {len(out['ugc'])}")
        for r in out["board"][:8]:
            print(f"  [{r['kind']}] {r['word'][:36]}  ({r['source']} #{r['rank']})")
        for r in out["media"][:5]:
            print(f"  [media] {r['word'][:36]}  ({r['published_at']})")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
