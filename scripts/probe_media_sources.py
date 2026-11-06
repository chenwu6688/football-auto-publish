#!/usr/bin/env python3
"""主流足球媒体源可用性探测（2026-11-06 建）。

## 为什么要有这个脚本

「接入 N 个数据源」这种任务，最大的浪费是**按名单逐个写解析器**：
写完才发现某站是 Cloudflare 挑战、某站 RSS 是七年前的死档、某站是 JS 壳。
本脚本只做一件事：**在写任何解析器之前，先量一遍每个源到底给不给数据**。

三条硬纪律（与全仓一致）：

1. **不猜域名**。名单里的站必须显式登记 URL；查不到正规站的条目直接标
   ``skipped`` 并写明原因，绝不「大概应该是这个地址」。
2. **区分「没数据」与「被拦」**。403 / Cloudflare 挑战 / 200 但零标题，
   是三种不同的失败，台账里必须分得清 —— 否则下次还会有人重试同一个死路。
3. **探测结果不参与发布**。本脚本只写 ``data/audit/media_sources_<date>.json``，
   不阻断任何流程，也不改任何线上配置。

## 输出怎么读

``verdict`` 五档：

- ``ok``        拿到 ≥1 条可用标题（附 ``titles`` 条数与样例）
- ``empty``     200 但解析不出标题（多半是 JS 壳，需要 API 或 headless）
- ``blocked``   401/403/451（反爬或地区限制）
- ``stale``     能拿到数据但**时效不达标**（RSS 死档的典型表现）
- ``skipped``   源本身不成立（域名查不到 / 不该接），``reason`` 写明

用法::

    python3 scripts/probe_media_sources.py
    python3 scripts/probe_media_sources.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"

CST = timezone(timedelta(hours=8))

#: 移动端 UA —— 贴吧/新浪等站对 PC 无头 UA 的拦截明显更严。
_UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")
_UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

#: 新闻时效窗口（小时）。超过这个时间的条目一律不算「今日热点」。
#: 2026-11-06 实测：新浪体育 RSS（rss.sina.com.cn/sports/global/focus.xml）
#: 的 pubDate 全是 2018-09，若按「RSS 能通就接」的思路直接接入，
#: 会把七年前的稿子当成当日热点 —— 正是「旧闻新发」红线。
DEFAULT_MAX_AGE_HOURS = 48

#: 源登记表。``kind`` 决定用哪种探测方式，不写解析器，只判断「给不给数据」。
SOURCES: tuple[dict, ...] = (
    {
        "name": "懂球帝",
        "kind": "dongqiudi",
        "url": "https://www.dongqiudi.com/",
        "note": "仓库已有 SportsScraper.scrape_dongqiudi_headlines，直接复用",
        "tier": "P0",
    },
    {
        "name": "百度贴吧",
        "kind": "tieba_hot",
        "url": "https://tieba.baidu.com/hottopic/browse/topicList",
        "note": "话题榜 JSON（移动 UA + Referer 可过），全站榜需做足球过滤",
        "tier": "P0",
    },
    {
        "name": "新浪体育",
        "kind": "html_links",
        "url": "https://sports.sina.com.cn/",
        "note": "首页/栏目页 HTML 可解析；RSS 已死，禁止走 RSS",
        "tier": "P1",
    },
    {
        "name": "腾讯体育",
        "kind": "html_links",
        "url": "https://new.qq.com/ch/sports/",
        "note": "2026-11-06 实测仅 19KB 且无内嵌数据（JS 壳），需逆向 API",
        "tier": "P2",
    },
    {
        "name": "虎扑",
        "kind": "html_links",
        "url": "https://bbs.hupu.com/soccer",
        "note": "2026-11-06 实测 226KB 但全 JS 渲染，需 API",
        "tier": "P2",
    },
    {
        "name": "Goal 中国",
        "kind": "html_links",
        "url": "https://www.goal.com/zh-cn",
        "note": "有年龄门与博彩广告同页，标题为英文，需合规评估",
        "tier": "P3",
    },
    {
        "name": "球天下",
        "kind": "html_links",
        "url": "https://www.qiutianxia.com/",
        "note": "2026-11-06 实测 Cloudflare 挑战页（Just a moment）",
        "tier": "P3",
    },
    {
        "name": "k7体育",
        "kind": "unsupported",
        "url": "",
        "note": ("检索不到正规新闻站；命中的结果均为博彩平台 SEO 页面。"
                 "在拿到确切新闻域名之前不接入"),
        "tier": "剔除",
    },
)


def _now() -> datetime:
    return datetime.now(CST)


def _get(url: str, *, mobile: bool = False, referer: str = "", timeout: int = 12):
    """返回 (status, text, err)。任何异常都收敛成 (None, "", msg)。"""
    try:
        import requests
    except Exception as exc:                        # pragma: no cover - 依赖缺失
        return None, "", f"requests 不可用: {exc}"
    headers = {"User-Agent": _UA_MOBILE if mobile else _UA_DESKTOP}
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        # 新浪首页声明 ISO-8859-1 但实际是 utf-8，不显式指定会得到乱码标题
        if r.encoding and r.encoding.lower() in ("iso-8859-1", "ascii"):
            r.encoding = r.apparent_encoding or "utf-8"
        return r.status_code, r.text, ""
    except Exception as exc:
        return None, "", f"{type(exc).__name__}: {exc}"


def _verdict_of(status, titles: list[str], err: str) -> str:
    """统一判词。**先判「被拦」，再判「空」**——两者原因不同，不能混。"""
    if err or status is None:
        return "blocked"
    if status in (401, 403, 451) or status >= 400:
        return "blocked"
    return "ok" if titles else "empty"


_RE_CJK = re.compile(r"[\u4e00-\u9fff]")
_NAV_WORDS = re.compile(r"^(首页|登录|注册|更多|下载|客户端|专题|视频|图片|评论|分享|"
                        r"上一页|下一页|返回|顶部|English)$")


def _looks_like_title(t: str) -> bool:
    """这条文本像不像**新闻标题**。

    2026-11-06 首版只数 ``<a>`` 个数，结果新浪首页报 813 条、虎扑报 26 条，
    打开一看分别是「HarmonyOS」和「saodi_monk」（用户名）—— 把导航栏和 ID
    当成了标题。**「数得到」不等于「拿到了」**，所以按中文标题特征再过一道：
    至少 6 个汉字，且不是纯导航词。
    """
    t = (t or "").strip()
    if len(t) < 8 or len(t) > 40 or _NAV_WORDS.match(t):
        return False
    return len(_RE_CJK.findall(t)) >= 6


def _football_like(titles: list[str]) -> int:
    """榜上有几条能被判成足球（复用 hot_topics 的两档准入，不另写一套词表）。"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        import hot_topics
    except Exception:
        return -1
    n = 0
    for t in titles:
        try:
            if hot_topics.is_football(t, hot_topics.entities_in(t)):
                n += 1
        except Exception:
            continue
    return n


def probe_dongqiudi(src: dict) -> dict:
    """走仓库已有采集器，而不是另写一份解析器。"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from media_scraper import SportsScraper
        arts = SportsScraper().scrape_dongqiudi_headlines(max_articles=8) or []
        titles = [str(a.get("title") or "") for a in arts if a.get("title")]
        return {"verdict": "ok" if titles else "empty", "count": len(titles),
                "titles": titles[:3], "via": "SportsScraper.scrape_dongqiudi_headlines"}
    except Exception as exc:
        return {"verdict": "blocked", "count": 0, "titles": [],
                "error": f"{type(exc).__name__}: {exc}"[:160]}


def probe_tieba_hot(src: dict) -> dict:
    """贴吧话题榜：``data.bang_topic.topic_list``（30 条 + discuss_num）。

    注意这是**全站**榜，实测前 8 条全是电竞/校园话题，足球密度很低。
    它的价值是「热度」，不是「足球素材」——所以下面单独给 ``football_like``。
    """
    status, text, err = _get(src["url"], mobile=True,
                             referer="https://tieba.baidu.com/")
    titles: list[str] = []
    if text and not err:
        try:
            data = json.loads(text)
            rows = ((data.get("data") or {}).get("bang_topic") or {}).get("topic_list") or []
            for item in rows:
                name = str((item or {}).get("topic_name") or "").strip()
                if name:
                    titles.append(name)
        except Exception:
            titles = re.findall(r'"topic_name"\s*:\s*"([^"]{2,40})"', text)
    return {"verdict": _verdict_of(status, titles, err), "status": status,
            "count": len(titles), "football_like": _football_like(titles),
            "titles": titles[:5], "error": err[:160]}


def probe_html_links(src: dict) -> dict:
    """通用 HTML 探测：只数「像标题的中文链接」，不追求准确解析。

    ``count`` 是过滤后的标题数，``raw_links`` 是原始链接数 —— 两个数差得越大，
    说明页面里导航/用户名越多，后续写解析器的成本越高。
    """
    status, text, err = _get(src["url"], mobile=False)
    raw, titles = 0, []
    if text and not err:
        for _url, t in re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>([^<]{8,60})</a>', text):
            raw += 1
            t = t.strip()
            if _looks_like_title(t) and t not in titles:
                titles.append(t)
    return {"verdict": _verdict_of(status, titles, err), "status": status,
            "count": len(titles), "raw_links": raw, "football_like": _football_like(titles),
            "titles": titles[:5], "error": err[:160],
            "html_bytes": len(text or "")}


def probe_rss_freshness(url: str, *, max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> dict:
    """RSS 专用：**先看 pubDate 再看条数**。

    2026-11-06 的教训：``rss.sina.com.cn/sports/global/focus.xml`` 返回
    200 且有 53 条，看着完全可用 —— 但 pubDate 全部是 2018-09。
    「能拿到数据」和「数据是今天的」是两件事。
    """
    status, text, err = _get(url)
    if err or not text:
        return {"verdict": _verdict_of(status, [], err), "status": status, "error": err[:160]}
    items = re.findall(r"<item>(.*?)</item>", text, re.S)
    dates = []
    for raw in re.findall(r"<pubDate>(.*?)</pubDate>", text):
        try:
            from email.utils import parsedate_to_datetime
            dates.append(parsedate_to_datetime(raw.strip()))
        except Exception:
            continue
    if not dates:
        return {"verdict": "empty", "count": len(items),
                "note": "有 item 但解析不出 pubDate，无法判时效"}
    newest = max(dates)
    age_h = (_now() - newest.astimezone(CST)).total_seconds() / 3600.0
    return {"verdict": "ok" if age_h <= max_age_hours else "stale",
            "count": len(items), "newest": newest.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
            "age_hours": round(age_h, 1)}


def run(*, max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> dict:
    rows = []
    for src in SOURCES:
        kind = src["kind"]
        t0 = time.time()
        if kind == "unsupported":
            rec = {"verdict": "skipped", "reason": src.get("note", "")}
        elif kind == "dongqiudi":
            rec = probe_dongqiudi(src)
        elif kind == "tieba_hot":
            rec = probe_tieba_hot(src)
        elif kind == "rss":
            rec = probe_rss_freshness(src["url"], max_age_hours=max_age_hours)
        else:
            rec = probe_html_links(src)
        rec = {**rec, "name": src["name"], "tier": src.get("tier", ""),
               "url": src["url"], "kind": kind, "note": src.get("note", ""),
               "elapsed_s": round(time.time() - t0, 2)}
        rows.append(rec)
        fb = rec.get("football_like")
        fb_txt = ("足{}".format(fb) if isinstance(fb, int) and fb >= 0 else "—")
        print("  [{:<7}] {:<8} {:<8} {:>4} 条 {:>4}  {:.1f}s  {}".format(
            rec["verdict"], rec["tier"], rec["name"], rec.get("count", 0), fb_txt,
            rec["elapsed_s"], rec.get("titles", [""])[0][:26] if rec.get("titles") else ""))
    return {"version": 1, "generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "max_age_hours": max_age_hours, "sources": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="主流足球媒体源可用性探测")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS)
    ap.add_argument("--out", help="台账输出路径（默认 data/audit/media_sources_<date>.json）")
    args = ap.parse_args()

    print(f"探测 {len(SOURCES)} 个源（时效窗口 {args.max_age_hours}h）...")
    rep = run(max_age_hours=args.max_age_hours)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        ok = [r for r in rep["sources"] if r["verdict"] == "ok"]
        print(f"\n可用 {len(ok)}/{len(SOURCES)}："
              + "、".join(f"{r['name']}({r.get('count', 0)})" for r in ok))
        bad = [r for r in rep["sources"] if r["verdict"] != "ok"]
        if bad:
            print("不可用：" + "；".join(f"{r['name']}={r['verdict']}" for r in bad))

    try:
        out = Path(args.out) if args.out else (
            AUDIT_DIR / f"media_sources_{_now().strftime('%Y-%m-%d')}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"台账已写：{out}")
    except Exception as exc:
        print(f"台账写入失败（不影响探测）：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
