#!/usr/bin/env python3
"""图源探活诊断（2026-11-04）。

背景：封面零配图率长期在 33% 上下，但**没人知道是哪条图源断的**——
``data_collector`` 里 Unsplash 的失败是 ``except: pass``，维基超时只在日志里闪一句，
DuckDuckGo 被挡连日志都没有。于是"配图率低"变成一句无法行动的话。

这个脚本把四条链路逐条探活，每条给出**可执行的下一步**，而不是只报红绿：

  ① 维基百科 API      —— 实体→条目→图片，主力源
  ② Unsplash API       —— 需要 UNSPLASH_ACCESS_KEY；200/401/403/429 含义完全不同
  ③ images.unsplash.com —— API 通了不代表图能下载，CDN 单独测
  ④ DuckDuckGo         —— 无 Key 兜底源，常被机房 IP 挡

判据：
  · 401 "access token is invalid" 与随机串的报错**一字不差** ⇒ Key 未生效，
    不是抄错；去 unsplash.com/oauth/applications 看应用状态 / 是否同意了 API 条款。
  · CDN 返回 404 说明域名与 TLS 都通（只是那张图不存在），**这条链路是好的**。

用法：
  python3 scripts/probe_image_sources.py
  python3 scripts/probe_image_sources.py --json        # 机读
  python3 scripts/probe_image_sources.py --strict      # 无任何可用图源时退出码 1
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 12
# 任意一张公开的 Unsplash 图片 ID 都行；这里用官方示例图，不存在时返回 404 也算「链路通」
CDN_PROBE = "https://images.unsplash.com/photo-1508098682722-e99c43a406b2?w=100"


def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)


def probe_pixabay() -> dict:
    """Pixabay：免费 5000 次/小时，注册即用**无需审核**（2026-11-04 实测域名可达）。"""
    api = "https://pixabay.com/api/"
    key = os.environ.get("PIXABAY_KEY", "")
    t0 = time.time()
    if not key:
        return {"name": "Pixabay API", "ok": False, "ms": 0, "status": None,
                "detail": "未配置 PIXABAY_KEY",
                "action": "pixabay.com/account/api-key 注册即拿 Key（无需审核，5 分钟）"}
    try:
        r = requests.get(api, params={"key": key, "q": "football", "per_page": 3,
                                      "image_type": "photo", "orientation": "horizontal"},
                         timeout=TIMEOUT)
    except Exception as e:
        return {"name": "Pixabay API", "ok": False, "ms": _ms(t0), "status": None,
                "detail": f"{type(e).__name__}: {str(e)[:90]}",
                "action": "域名不通：本机/CI 出口被挡，改用新闻源正文图 + 数据卡兜底"}
    if r.status_code == 200:
        hits = (r.json() or {}).get("hits", [])
        return {"name": "Pixabay API", "ok": bool(hits), "ms": _ms(t0), "status": 200,
                "detail": f"命中 {len(hits)} 张（含原图尺寸信息）",
                "action": "" if hits else "200 但无结果：换检索词；图库层不是实体精确源"}
    return {"name": "Pixabay API", "ok": False, "ms": _ms(t0), "status": r.status_code,
            "detail": f"HTTP {r.status_code}: {r.text[:100]}",
            "action": ("400 = Key 格式不对或未生效；429 = 超 5000 次/小时配额。"
                       "注意：pixabay.com **网页**从本机 403，但 api 域名是通的，别据此判断不通")}


def probe_pexels() -> dict:
    """Pexels：免费 200 次/小时，注册即用**无需审核**。"""
    api = "https://api.pexels.com/v1/search"
    key = os.environ.get("PEXELS_KEY", "")
    t0 = time.time()
    if not key:
        return {"name": "Pexels API", "ok": False, "ms": 0, "status": None,
                "detail": "未配置 PEXELS_KEY",
                "action": "pexels.com/api 注册即拿 Key（无需审核）；额度比 Pixabay 小，作备选"}
    try:
        r = requests.get(api, params={"query": "football", "per_page": 3},
                         headers={"Authorization": key}, timeout=TIMEOUT)
    except Exception as e:
        return {"name": "Pexels API", "ok": False, "ms": _ms(t0), "status": None,
                "detail": f"{type(e).__name__}: {str(e)[:90]}", "action": "域名不通，跳过"}
    if r.status_code == 200:
        photos = (r.json() or {}).get("photos", [])
        return {"name": "Pexels API", "ok": bool(photos), "ms": _ms(t0), "status": 200,
                "detail": f"命中 {len(photos)} 张", "action": ""}
    return {"name": "Pexels API", "ok": False, "ms": _ms(t0), "status": r.status_code,
            "detail": f"HTTP {r.status_code}: {r.text[:100]}",
            "action": "401 = Key 未生效；psxels 网页 403 但 api 域名可达，别混淆"}


def probe_news_source() -> dict:
    """新闻源正文图：零网络、零 Key，精度最高的一层（就是那场比赛的图）。

    两层验证：① 代码链路——`search_images` 是否真的消费 ``topic["source_images"]``；
    ② 实绩——最近 7 天成稿里有没有真用上（没战报的批次为 0 属正常，不算故障）。
    """
    t0 = time.time()
    try:
        import data_collector as dc
        got = dc.search_images(
            {"title": "测试", "source_images": [{"url": "https://example.invalid/a.jpg"}]},
            count=1)
        linked = bool(got) and got[0].get("source") == "news_source"
    except Exception as e:
        return {"name": "新闻源正文图", "ok": False, "ms": _ms(t0), "status": None,
                "detail": f"{type(e).__name__}: {str(e)[:90]}",
                "action": "链路调用异常，检查 data_collector.search_images 的 ⓿ 层"}
    if not linked:
        return {"name": "新闻源正文图", "ok": False, "ms": _ms(t0), "status": None,
                "detail": "search_images 没有消费 topic['source_images']（链路断了）",
                "action": "检查 data_collector.search_images 的 ⓿ 层与 cover_binding 白名单"}

    used = 0
    out = PROJECT_ROOT / "output"
    if out.exists():
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone(timedelta(hours=8))).date()
        for i in range(7):
            d = out / (today - timedelta(days=i)).strftime("%Y-%m-%d")
            mp = d / "metadata.json"
            if not mp.exists():
                continue
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                continue
            for a in meta.get("articles") or []:
                used += sum(1 for im in (a.get("images") or [])
                            if isinstance(im, dict) and im.get("source") == "news_source")
    return {"name": "新闻源正文图", "ok": True, "ms": _ms(t0), "status": None,
            "detail": (f"链路已接上；近 7 天成稿里用到 {used} 张"
                       + ("" if used else "（没有战报的批次为 0 属正常）")),
            "action": "" if used else "若连续多日仍为 0，查 media_scraper 的战报正文 img 抽取"}


def probe_wikipedia() -> dict:
    """维基：用「皇家马德里」查条目 → 再查该条目有没有图。"""
    api = "https://zh.wikipedia.org/w/api.php"
    t0 = time.time()
    try:
        r = requests.get(api, params={
            "action": "query", "format": "json", "prop": "pageimages",
            "piprop": "original", "titles": "皇家马德里", "redirects": 1,
        }, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200:
            return {"name": "维基百科 API", "ok": False, "ms": _ms(t0),
                    "status": r.status_code,
                    "detail": f"HTTP {r.status_code}",
                    "action": "状态码非 200：先看是否为 5xx（维基侧故障）或 403（UA 被拒）"}
        pages = ((r.json().get("query") or {}).get("pages") or {})
        has_img = any((p.get("original") or {}).get("source") for p in pages.values())
        return {"name": "维基百科 API", "ok": True, "ms": _ms(t0), "status": 200,
                "detail": f"条目可达，{'有原图' if has_img else '该条目无原图（属正常，换实体即可）'}",
                "action": "" if has_img else "换一个实体词再试；无图条目不是故障"}
    except Exception as e:
        return {"name": "维基百科 API", "ok": False, "ms": _ms(t0), "status": None,
                "detail": f"{type(e).__name__}: {str(e)[:90]}",
                "action": ("超时/连接失败：本机或 CI 出口到 zh.wikipedia.org 不通。"
                           "封面链路会熔断跳过该源，改用数据卡兜底，不用改代码")}


def probe_unsplash_api() -> dict:
    key = os.environ.get("UNSPLASH_ACCESS_KEY", "")
    if not key:
        return {"name": "Unsplash API", "ok": False, "ms": 0, "status": None,
                "detail": "未设置 UNSPLASH_ACCESS_KEY",
                "action": ("本地：source scripts/local_env.sh；"
                           "CI：gh secret set UNSPLASH_ACCESS_KEY")}
    t0 = time.time()
    try:
        r = requests.get("https://api.unsplash.com/search/photos", params={
            "query": "football", "per_page": 1, "client_id": key}, timeout=TIMEOUT)
        ms = _ms(t0)
        if r.status_code == 200:
            n = len((r.json() or {}).get("results") or [])
            return {"name": "Unsplash API", "ok": n >= 0, "ms": ms, "status": 200,
                    "detail": f"认证通过，返回 {n} 条", "action": ""}
        body = r.text[:160].replace("\n", " ")
        if r.status_code in (401, 403):
            action = (
                "401/403 = 认证不通过。先用『对照法』定性：拿一串随机 43 位字符请求同一端点，"
                "若报错与此完全相同，说明不是字符抄错，是 Key 未生效 → "
                "去 https://unsplash.com/oauth/applications 检查该应用是否已完成设置/同意了 API 条款。")
        elif r.status_code == 429:
            action = "429 = 配额用尽（demo 应用 50 次/小时）。降低调用频率或申请生产额度。"
        else:
            action = f"非预期状态 {r.status_code}，看 body 定位"
        return {"name": "Unsplash API", "ok": False, "ms": ms, "status": r.status_code,
                "detail": body, "action": action}
    except Exception as e:
        return {"name": "Unsplash API", "ok": False, "ms": _ms(t0), "status": None,
                "detail": f"{type(e).__name__}: {str(e)[:90]}",
                "action": "网络不通：与 Key 无关，先解决出口"}


def probe_unsplash_cdn() -> dict:
    """CDN 单独测：API 拿到 URL 但下载失败是另一类故障。"""
    t0 = time.time()
    try:
        r = requests.get(CDN_PROBE, headers={"User-Agent": UA}, timeout=TIMEOUT, stream=True)
        ms = _ms(t0)
        # 200 = 图拿到；404 = 域名与 TLS 通，只是这张图不存在 → 链路仍然可用
        ok = r.status_code in (200, 404)
        detail = (f"HTTP {r.status_code}"
                  + ("（图可下载）" if r.status_code == 200
                     else "（该图不存在，但域名可达 → 链路可用）"))
        return {"name": "images.unsplash.com", "ok": ok, "ms": ms,
                "status": r.status_code, "detail": detail,
                "action": "" if ok else "CDN 不可达：图片下载会失败，图源等于没有"}
    except Exception as e:
        return {"name": "images.unsplash.com", "ok": False, "ms": _ms(t0), "status": None,
                "detail": f"{type(e).__name__}: {str(e)[:90]}",
                "action": "CDN 不可达：即使 API 通了也下不到图"}


def probe_duckduckgo() -> dict:
    t0 = time.time()
    try:
        r = requests.get("https://duckduckgo.com/", params={"q": "football stadium"},
                         headers={"User-Agent": UA}, timeout=TIMEOUT)
        ms = _ms(t0)
        ok = r.status_code == 200 and "vqd=" in r.text
        return {"name": "DuckDuckGo", "ok": ok, "ms": ms, "status": r.status_code,
                "detail": ("拿到 vqd token，可用" if ok
                           else f"HTTP {r.status_code}，未拿到 vqd（被挡或改版）"),
                "action": "" if ok else ("机房 IP 常被挡；这是无 Key 兜底源，"
                                        "不可用时不建议硬修，靠数据卡兜底")}
    except Exception as e:
        return {"name": "DuckDuckGo", "ok": False, "ms": _ms(t0), "status": None,
                "detail": f"{type(e).__name__}: {str(e)[:90]}",
                "action": "不可达，同机房 IP 受限，不影响主链路判断"}


def _proxy_hint() -> str:
    """本机代理会制造假红：维基/DDG 走代理报 ProxyError，绕过代理则直连超时。"""
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(k):
            return (f"检测到代理 {os.environ[k]}：本机维基/DDG 不可达可能只是本地出口限制，"
                    "CI 是直连环境，**以 CI 的 verify 结果为准**，别按本机结论去改图源代码")
    return ""


def run() -> dict:
    wiki = probe_wikipedia()
    uns = probe_unsplash_api()
    cdn = probe_unsplash_cdn()
    ddg = probe_duckduckgo()
    pix = probe_pixabay()
    pex = probe_pexels()
    news = probe_news_source()
    probes = [news, wiki, pix, pex, uns, cdn, ddg]
    usable = [p for p in probes if p["ok"]]
    # 「有图源」不是「随便哪条通」：图库必须 API 与 CDN 同时通才能真拿到图。
    has_source = (news["ok"] or wiki["ok"] or pix["ok"] or pex["ok"]
                  or (uns["ok"] and cdn["ok"]) or ddg["ok"])
    return {"probes": probes, "usable": len(usable),
            "total": len(probes), "has_source": has_source,
            "proxy_hint": _proxy_hint()}


def render(rep: dict) -> str:
    L = ["# 图源探活诊断", ""]
    if rep.get("proxy_hint"):
        L += [f"> ⚠️ {rep['proxy_hint']}", ""]
    for p in rep["probes"]:
        L.append(f"## {'✅' if p['ok'] else '❌'} {p['name']}"
                 + (f"（{p['ms']}ms）" if p["ms"] else ""))
        L.append(f"- {p['detail']}")
        if p.get("action"):
            L.append(f"- → {p['action']}")
        L.append("")
    L.append(f"**可用源 {rep['usable']}/{rep['total']}**")
    if not rep["has_source"]:
        L.append("")
        L.append("> 所有图源都不可用 → 封面只能走 `cover_card.py` 原创数据卡兜底。"
                 "此时不要继续加图源，先把 Key 或出口修好。")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="图源探活诊断")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true", help="无任何可用图源时退出码 1")
    args = ap.parse_args()

    rep = run()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    if args.strict and not rep["has_source"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
