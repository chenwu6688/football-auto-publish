#!/usr/bin/env python3
"""新闻源正文图爬取 —— 「单篇至少 3 张相关配图」的第一层（2026-11-05）。

## 为什么这一层必须是爬取，而不是又一个图库

2026-11-05 noon 批次三篇里有二篇走了「数据卡兜底」，剩下那篇也只有 1 张图。用户看到
的实际效果是「光有文字，没有配图」。把图源挨个过一遍就明白为什么：

    Wikimedia Commons  本机（国内网络）全超时；CI 可通，但一篇只能取到 1~2 张
    Pexels / Pixabay   英文标签索引，白名单里只有 10 支球队有球场照，球员零覆盖
    Unsplash           应用仍在 review，Key 401
    Openverse          本机超时

也就是说：**任何「免费图库」都不可能在足球这个题材上稳定给到 3 张相关图** ——
球员肖像权在俱乐部/经纪公司/联赛手里，免费图库根本没有这些素材（这是行业结构，
不是搜索技巧）。真正的存量在新闻站：一篇报道本身就是「图 + 文」成套产出的。

所以这一层去**爬新闻站正文图**，但把口子收得很紧：

1. **只爬与这篇稿子同实体的报道** —— 文章标题必须命中正文实体（别名感知）。
   不做「按标题词随便搜」：那等于自建一个随机图库，还背上版权风险。
2. **只取正文容器里的 `<img>`** —— 站内 logo、政策图标、推荐位、gif 一律过滤，
   靠域名与文件名双重黑名单，不靠「看起来像不像」。
3. **单篇最多取 2 张** —— 同一场比赛的十张图放一篇里是重复不是丰富。
4. **转述性使用** —— 与 `media_scraper` 抓战报正文图同一口径（引用新闻源配图），
   落台账时 `source="news_body"` 并带上原报道 URL，可回溯。

## 图床尺寸这一关（不修就会「爬到了但下不下来」）

直播吧正文图走 `tu.duoduocdn.com`，URL 形如 `xxx_720.jpg`，实测 **720×480**——
`image_service` 的 800×600 闸门会整批拒掉，表现就是「爬取层每篇都返回图，
但一篇都没落盘」，又是那种查不到原因的黑箱。实测同一张图的尺寸变体：

    _720.jpg   720×480   178 KB
    _1280.jpg  1280×853  471 KB     ← 用它
    （去掉后缀）5194×3464  3.0 MB   超 5MB 上限，不用

所以 `upgrade_url()` 把已知图床的 `_<数字>.jpg` 改写成 `_1280.jpg`，**取不到再回退原
URL**（`fetch` 里靠 `fallback` 字段带着走）。这不是「猜一个更大的尺寸」，是实测过
该图床的尺寸变体命名规则；别的域名一律不动。

## 纪律

- 网络失败/解析失败一律返回 `[]`，**绝不抛异常**阻塞发布（图源不是发布的前置条件）；
- 「没爬到」和「爬到了但不合格」分开记（`scraper_diagnostics` 的 `detail`），
  否则下次又是「配图率低但没人知道为什么」；
- 进程内缓存索引页 10 分钟：一批 3~8 篇稿子不该把同一个列表页拉 8 次。

命令行：
    python3 news_images.py --selftest
    python3 news_images.py --entities 皇马 姆巴佩 --show
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

try:                                     # 测试环境可能没装 requests
    import requests
except ImportError:                      # pragma: no cover
    requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:                      # pragma: no cover
    BeautifulSoup = None                 # type: ignore[assignment]

import scraper_diagnostics as _diag

ROOT = Path(__file__).resolve().parent
ENTITY_MAP = ROOT / "config" / "entity_map.json"

INDEX_URL = "https://news.zhibo8.com/zuqiu/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

MAX_PER_PAGE = 2            # 单篇报道最多取几张（同场比赛多图=重复）
MAX_ARTICLES = 6            # 最多抓几篇相关报道
INDEX_TTL = 600             # 索引页缓存秒数
MIN_EDGE_PX = 200           # img 标签上写死的宽/高小于它就当图标

# 最近一次 fetch 的状态（给验收脚本/日志用，不参与判定）
LAST: dict = {"index": 0, "picked": 0, "pages": 0, "images": 0, "error": ""}

_ARTICLE_URL = re.compile(
    r"^(?://|https?://)news\.zhibo8\.com/zuqiu/(\d{4}-\d{2}-\d{2})/([0-9a-z]+)\.htm", re.I)

# 正文图黑名单：站内静态资源、图标、占位图、广告位。
# 双条件（域名 + 文件名）是有原因的：2026-11-04 那次「eo-」子串误报就是只看了文件名，
# 把正常稿全判成反爬拦截。这里宁可多列几条，也不要一个「看起来像图」的 logo。
_BAD_URL = re.compile(
    r"(?:"
    r"static\d*\w*\.duoduocdn\.com"           # 站内静态资源域名
    r"|/static/|/common/img/|/images?/logo"
    r"|logo|icon|avatar|emoji|qrcode|placeholder|loading|spinner|blank\.|policy"
    r"|wenhuajingying|\.gif(?:$|\?)"
    r")", re.I)

# 图床尺寸变体：只对确认支持 `_<数字>.<ext>` 命名的域名做改写
_UPGRADABLE_HOST = re.compile(r"^https?://tu\.duoduocdn\.com/", re.I)
_SIZE_SUFFIX = re.compile(r"_(?P<n>\d{2,4})\.(?P<ext>jpe?g|png|webp)$", re.I)
WANT_WIDTH = 1280
_FALLBACK_WIDTH = 720

_CONTENT_SEL = re.compile(
    r"(?:^|[-_ ])(?:content|article|artical|detail|news[-_]?content|post[-_]?body)(?:$|[-_ ])", re.I)

_CACHE: dict = {"index": None, "at": 0.0, "aliases": None, "reverse": None}


# ——————————————————————————————————————————————
# 纯函数（全部可单测，不打网络）
# ——————————————————————————————————————————————

def parse_index(html: str) -> list[dict]:
    """解析新闻列表页，返回 ``[{title, url, date}]``（按页面顺序，已去重）。

    列表页里重复的 <a> 很多（同一个链接在标题区和「更多」区各一份），
    去重键用 URL 路径而不是整条 URL——查询串不同但指向同一篇文章。
    """
    if not html or BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        if len(title) < 10:                       # 导航/栏目标签，不是标题
            continue
        href = (a.get("href") or "").strip()
        m = _ARTICLE_URL.match(href)
        if not m:
            continue
        if href.startswith("//"):
            href = "https:" + href
        key = m.group(2)
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title[:80], "url": href, "date": m.group(1)})
    return out


def upgrade_url(url: str) -> str:
    """把图床的 `_720.jpg` 改写成 `_1280.jpg`（实测存在且过 800×600 闸门）。

    - 只对白名单域名动手（`tu.duoduocdn.com`），别家命名规则未知，改了就是赌；
    - 原后缀已经 ≥1280 的不动；
    - 改写结果**不保证存在**，调用方必须保留原 URL 作回退。
    """
    if not url or not _UPGRADABLE_HOST.match(url):
        return url
    m = _SIZE_SUFFIX.search(url)
    if not m:
        return url
    try:
        cur = int(m.group("n"))
    except ValueError:
        return url
    if cur >= WANT_WIDTH:
        return url
    return url[:m.start()] + f"_{WANT_WIDTH}.{m.group('ext')}"


def original_url(url: str) -> str:
    """把升级过的 `_1280.jpg` 还原成图床原始尺寸 `_720.jpg`（实测存在、720×480）。

    只用作**下载失败时的回退**：1280 版本万一是空的，退回 720 至少能落盘；
    下载端对正文图可以用比封面更宽的尺寸闸门，所以 720 宽也能过。
    """
    if not url or not _UPGRADABLE_HOST.match(url):
        return url
    m = _SIZE_SUFFIX.search(url)
    if not m:
        return url
    try:
        if int(m.group("n")) == _FALLBACK_WIDTH:
            return url
    except ValueError:
        return url
    return url[:m.start()] + f"_{_FALLBACK_WIDTH}.{m.group('ext')}"


def parse_article_images(html: str, *, limit: int = MAX_PER_PAGE) -> list[str]:
    """从报道正文里抽图（绝对 URL，已过滤图标/占位/gif，已升级尺寸）。

    正文容器找不到时**不退回整个 <body>**：列表页那种「一屏几十张小图」会全部混进来，
    等于又回到随机图库。取不到就返回空，由下一层图源接手。
    """
    if not html or BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    box = None
    for cand in soup.find_all(True, class_=_CONTENT_SEL):
        if cand.find("img"):
            box = cand
            break
    if box is None:
        return []

    out: list[str] = []
    for img in box.find_all("img"):
        src = (img.get("data-src") or img.get("data-original")
               or img.get("src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        for attr in ("width", "height"):
            v = str(img.get(attr) or "")
            if v.isdigit() and int(v) < MIN_EDGE_PX:
                src = ""
                break
        if not src or _BAD_URL.search(src):
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            continue                              # 相对路径在新闻页多半是站内小图
        if not src.startswith("http"):
            continue
        src = upgrade_url(src)
        if src not in out:
            out.append(src)
        if len(out) >= limit:
            break
    return out


def _load_aliases() -> tuple[dict[str, set[str]], dict[str, str]]:
    """返回（标准中文名 → 别名集合，别名 → 标准名）。

    第二个表是必须的：正文实体可能是「皇家马德里」，而新闻标题写的是「皇马」。
    只按标准名查表就会漏掉全部别名写法，表现为「明明有报道，一条都没挑中」。
    """
    if _CACHE["aliases"] is not None:
        return _CACHE["aliases"], _CACHE["reverse"]
    table: dict[str, set[str]] = {}
    reverse: dict[str, str] = {}
    try:
        data = json.loads(ENTITY_MAP.read_text(encoding="utf-8")) or {}
        for item in (data.get("entities") or {}).values():
            if not isinstance(item, dict):
                continue
            canon = str(item.get("name_zh") or "").strip()
            if not canon:
                continue
            terms = {canon}
            for a in item.get("aliases") or []:
                a = str(a or "").strip()
                if len(a) >= 2:
                    terms.add(a)
                    reverse.setdefault(a, canon)
            table[canon] = terms
    except Exception:
        table, reverse = {}, {}
    _CACHE["aliases"], _CACHE["reverse"] = table, reverse
    return table, reverse


def entity_terms(entity: str) -> set[str]:
    """一个实体在新闻标题里可能出现的写法（自身 + 别名 + 去掉「足球俱乐部」后缀）。"""
    e = (entity or "").strip()
    if not e:
        return set()
    table, reverse = _load_aliases()
    canon = reverse.get(e, e)
    terms = {e} | table.get(canon, set())
    for suffix in ("足球俱乐部", "俱乐部", "足球"):
        if e.endswith(suffix) and len(e) - len(suffix) >= 2:
            terms.add(e[: -len(suffix)])
    return {t for t in terms if len(t) >= 2}


def pick_articles(items, entities, limit: int = MAX_ARTICLES) -> list[dict]:
    """挑出「标题命中实体」的报道，按命中实体数降序（命中的实体越多越相关）。

    一条都没命中就返回空 —— 不拿「同样带足球两个字」的新闻凑数。
    """
    ents = [e for e in (entities or []) if isinstance(e, str) and e.strip()]
    if not items or not ents:
        return []
    terms = {e: entity_terms(e) for e in ents}
    scored = []
    for it in items:
        title = str(it.get("title") or "")
        if not title:
            continue
        hits = [e for e, ts in terms.items() if any(t in title for t in ts)]
        if not hits:
            continue
        scored.append((len(hits), it, hits))
    scored.sort(key=lambda x: -x[0])
    out = []
    for n_hit, it, hits in scored[:limit]:
        row = dict(it)
        row["matched"] = hits
        row["score"] = n_hit
        out.append(row)
    return out


# ——————————————————————————————————————————————
# 网络层
# ——————————————————————————————————————————————

def _get(url: str, *, timeout: float = 6.0, referer: str = "https://news.zhibo8.com/"):
    if requests is None:
        return None
    headers = {"User-Agent": UA, "Referer": referer,
               "Accept-Language": "zh-CN,zh;q=0.9"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        _diag.record("news_body", url, kind=_diag.classify_exception(e),
                     detail=f"{type(e).__name__}: {str(e)[:100]}")
        return None
    if resp.status_code != 200:
        _diag.record("news_body", url, kind=_diag.classify_status(resp.status_code),
                     status=resp.status_code,
                     detail=f"HTTP {resp.status_code}: {resp.text[:100]}")
        return None
    # 直播吧是 UTF-8，但 requests 常把它猜成 ISO-8859-1；猜错后标题全是乱码，
    # 实体匹配必然全挂——这类「能连上但没结果」最难查，所以直接指定。
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
        resp.encoding = "utf-8"
    return resp


def load_index(*, timeout: float = 6.0, force: bool = False) -> list[dict]:
    """当日新闻列表（进程内缓存 10 分钟）。"""
    now = time.time()
    if not force and _CACHE["index"] is not None and now - _CACHE["at"] < INDEX_TTL:
        return _CACHE["index"]                     # type: ignore[return-value]
    resp = _get(INDEX_URL, timeout=timeout)
    items = parse_index(resp.text) if resp is not None else []
    if items:
        _CACHE["index"], _CACHE["at"] = items, now
        _diag.record("news_body", INDEX_URL, status=200, count=len(items),
                     detail=f"新闻列表 {len(items)} 条（缓存 {INDEX_TTL}s）")
    else:
        _diag.record("news_body", INDEX_URL, kind="empty", count=0,
                     detail="列表页没解析出文章链接（改版？）")
    return items


def fetch(entities, need: int = 3, *, timeout: float = 6.0,
          max_articles: int | None = None, budget_s: float = 18.0,
          session_index: list[dict] | None = None) -> list[dict]:
    """按实体爬相关新闻的正文图，返回至多 ``need`` 张候选（未下载、未校验尺寸）。

    返回项结构：``{"url", "source": "news_body", "alt", "query",
    "article_url", "fallback"}``。
    ``fallback`` 是「尺寸升级失败时回退用的原 URL」，下载端决定用哪个。
    """
    LAST.update({"index": 0, "picked": 0, "pages": 0, "images": 0, "error": ""})
    need = max(0, int(need))
    if need <= 0 or requests is None:
        return []

    ents = [e for e in (entities or []) if isinstance(e, str) and e.strip()]
    if not ents:
        # 没有实体就不爬：拿标题里的普通词去爬新闻，必然爬到不相干的报道
        _diag.record("news_body", INDEX_URL, kind="empty",
                     detail="无正文实体，不爬新闻图（避免不限主题的随机配图）")
        return []

    items = session_index if session_index is not None else load_index(timeout=timeout)
    LAST["index"] = len(items)
    picked = pick_articles(items, ents, limit=max_articles or max(need * 2, 4))
    LAST["picked"] = len(picked)
    if not picked:
        _diag.record("news_body", INDEX_URL, kind="empty", count=0,
                     detail=f"当日 {len(items)} 条新闻里没有命中实体 {ents[:3]} 的报道")
        return []

    deadline = time.time() + max(1.0, budget_s)
    pool: list[tuple[dict, list[str]]] = []      # (报道, 正文图 URL 列表)
    for art in picked:
        if len(pool) >= need or time.time() > deadline:
            break
        resp = _get(art["url"], timeout=timeout)
        if resp is None:
            continue
        LAST["pages"] += 1
        urls = parse_article_images(resp.text, limit=MAX_PER_PAGE)
        if urls:
            pool.append((art, urls))
        _diag.record("news_body", art["url"], status=200, count=len(urls),
                     detail=f"《{art['title'][:24]}》命中 {art['matched'][:2]}")

    # 取图顺序：**先每篇各取 1 张**，不够再回头取第 2 张。
    # 实测踩过：一篇「欧冠夺冠概率」的报道正文有 3 张图，如果按篇取满，整篇稿子
    # 的 3 张配图会全来自这一篇（同一张榜单换三个角度），看着像复制粘贴。
    # 3 篇不同报道各 1 张，观感明显更像「这一事件的多方报道」。
    out: list[dict] = []
    for idx in range(MAX_PER_PAGE):
        for art, urls in pool:
            if len(out) >= need:
                break
            if idx >= len(urls):
                continue
            url = urls[idx]
            cand = {"url": url, "source": "news_body",
                    "alt": art["title"][:80], "query": "、".join(art["matched"][:2]),
                    "article_url": art["url"]}
            # 回退用**原始尺寸**（_720），不是「去掉尺寸后缀」——去后缀是 3 MB 原图，
            # 会撞上下载端 5 MB 上限，回退反而必失败。
            orig = original_url(url)
            if orig != url:
                cand["fallback"] = orig
            out.append(cand)
        if len(out) >= need:
            break

    LAST["images"] = len(out)
    if not out:
        LAST["error"] = "抓到了相关报道但正文里没有合格图"
        _diag.record("news_body", INDEX_URL, kind="empty", count=0,
                     detail=f"抓到 {LAST['pages']} 篇相关报道，正文图为 0（图都在图标/摘要外？）")
    return out


# ——————————————————————————————————————————————
# 自检
# ——————————————————————————————————————————————

_INDEX_HTML = """
<ul><li><a href="//news.zhibo8.com/zuqiu/2026-11-05/aaa111native.htm">皇马客战瓦伦西亚大名单出炉，姆巴佩领衔锋线</a></li>
<li><a href="//news.zhibo8.com/zuqiu/2026-11-05/bbb222native.htm">中超最后两轮赛程调整，海港国安争冠悬念留到最后</a></li>
<li><a href="/zuqiu/2026-11-05/ccc333native.htm">短标题</a></li>
<li><a href="//news.zhibo8.com/zuqiu/2026-11-05/aaa111native.htm?from=list">皇马客战瓦伦西亚大名单出炉，姆巴佩领衔锋线</a></li>
</ul>"""

_ARTICLE_HTML = """
<div class="content">
  <p>直播吧11月5日讯</p>
  <img src="//static4style.duoduocdn.com/web/logo_o3x_03.png">
  <img src="//tu.duoduocdn.com/uploads/day_261104/abc_720.jpg">
  <img src="//tu.duoduocdn.com/uploads/day_261104/def_720.jpg" width="60">
  <img src="//tu.duoduocdn.com/uploads/day_261104/ghi_720.gif">
  <img data-src="//tu.duoduocdn.com/uploads/day_261104/jkl_720.jpg">
  <img src="//tu.duoduocdn.com/uploads/day_261104/mno_720.jpg">
</div>"""


def selftest() -> list[tuple[str, bool]]:
    cases: list[tuple[str, bool]] = []

    items = parse_index(_INDEX_HTML)
    cases.append(("列表页解析出去重后的文章", len(items) == 2))
    cases.append(("相对路径的文章链接被丢弃", all(i["url"].startswith("https://") for i in items)))
    cases.append(("解析出发布日期", items[0]["date"] == "2026-11-05"))

    picked = pick_articles(items, ["皇马"], limit=5)
    cases.append(("按实体命中挑文章", len(picked) == 1 and "皇马" in picked[0]["matched"]))
    cases.append(("无实体不挑文章", pick_articles(items, []) == []))
    cases.append(("实体不在标题里不挑", pick_articles(items, ["拜仁"]) == []))
    cases.append(("别名也能命中", len(pick_articles(items, ["皇家马德里"])) == 1))

    imgs = parse_article_images(_ARTICLE_HTML, limit=3)
    cases.append(("过滤 logo / gif / 小图", len(imgs) == 3))
    cases.append(("尺寸升级到 _1280", imgs and imgs[0].endswith("_1280.jpg")))
    cases.append(("data-src 也认", any("jkl" in u for u in imgs)))
    cases.append(("单篇限流", len(parse_article_images(_ARTICLE_HTML, limit=1)) == 1))
    cases.append(("无正文容器返回空",
                  parse_article_images("<div class='list'><img src='//a/b.jpg'></div>") == []))

    cases.append(("非白名单域名不改写",
                  upgrade_url("https://x.example/a_720.jpg") == "https://x.example/a_720.jpg"))
    cases.append(("已是 1280 不改写",
                  upgrade_url("https://tu.duoduocdn.com/a_1280.jpg")
                  == "https://tu.duoduocdn.com/a_1280.jpg"))
    cases.append(("空实体不触发网络", fetch([], need=3) == []))
    return cases


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="新闻源正文图爬取")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--entities", nargs="*", default=None)
    ap.add_argument("--need", type=int, default=3)
    ap.add_argument("--show", action="store_true", help="联网跑一次并打印结果")
    a = ap.parse_args()
    if a.selftest:
        fails = 0
        for name, ok in selftest():
            print(("  ✅ " if ok else "  ❌ ") + name)
            fails += 0 if ok else 1
        print(f"\n失败 {fails} / {len(selftest())} 项")
        return 1 if fails else 0
    if a.entities is not None:
        got = fetch(a.entities, need=a.need)
        print(json.dumps(got, ensure_ascii=False, indent=1))
        print("LAST:", LAST)
        return 0 if got else 2
    print(json.dumps(LAST, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
