"""维基共享（Wikimedia Commons）取图 —— 足球领域唯一可用的免费真人图源。

## 为什么是它（2026-11-04 实测，不是推测）

Pexels / Pixabay 是**英文标签索引的通用图库**，对足球这个领域系统性失效：

| 查什么 | 实际返回 | 结论 |
|---|---|---|
| `皇家马德里` | 酱油饺子特写 | 中文 query 全废 |
| `欧冠` | 欧元纸币 | 同上 |
| `Haaland` | 奥地利一个村庄 | 球员名不可用 |
| 球队名（16 队实测） | 10 队有主场照，尤文/热刺/马竞/纽卡 0 张 | 覆盖率一半 |
| `FC Internazionale Milano` | 诺坎普（巴萨球场） | **错得很像真的** |
| `Paris Saint-Germain` | 巴黎街景 | 同上 |

根因不是「图库不好」，是**球员肖像权不在图库手里**：肖像权归俱乐部/经纪公司/联赛，
新闻图由 Getty / Reuters / IC Photo 垄断。所以「免费 + 有球员真人照 + 可商用 + 有 API」
的通用图库在足球领域**根本不存在**——这是行业结构决定的，不是搜索技巧问题。

Commons 是唯一例外：社区上传、逐张标注自由许可、1.5 亿文件、无需 Key、无速率门槛
（带合规 UA 200 req/min）。它的弱项是审美参差、以及**消歧**（见下）。

## 三条合规条件，缺一条就不许用

1. **许可白名单，fail-closed**：只收 CC0 / Public Domain / CC BY / CC BY-SA。
   任何不认识的许可（Fair use、Non-free、GFDL、空值）**一律拒绝**——宁可这张不用，
   也不要拿一张"大概是自由的"图去发布。
2. **必须能署名**：CC BY / BY-SA 要求署名作者 + 许可名 + 链接。
   **没有 Artist 字段的图直接拒绝**（署不了名就不能用）。
   ⚠️ 2026-11-04 之前本项目的维基配图**完全没有署名**（旧 `search_wikipedia` 直接取
   `pageimages` 的 `originalimage`），那是实际的许可违规。本模块把 attribution 随图返回，
   由 `orchestrator` 落到正文末尾。
3. **尺寸与消歧**：≥800×600；文件名/描述/分类必须命中实体 token；
   球员类还必须落在 football/soccer 分类下（挡「苏格拉底→希腊哲学家」「Hulk→绿巨人」
   「Carlos→罗伯托·卡洛斯」这类同名误配）。

## 网络现实

本机（国内网络）访问 `*.wikimedia.org` **全部超时**，走代理与直连一样；`upload.wikimedia.org`
CDN 也不通。GitHub Actions 可以通。因此：
- 调用方必须用 `data_collector._wiki_available()` 熔断，不要每篇稿子都白等 12 秒；
- 拿不到时返回 `[]` 并落 `scraper_diagnostics`，**不能抛异常、不能拖慢主链路**；
- 验收脚本必须做三态：网络不通判「未验证」，不能算通过。
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

try:                                     # 测试环境可能没装 requests
    import requests
except ImportError:                      # pragma: no cover
    requests = None

import scraper_diagnostics as _diag

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
ENTITY_MAP = Path(__file__).resolve().parent / "config" / "entity_map.json"

# Commons 要求带可识别的 UA（不带会被限到 10 req/min 甚至 403）
UA = ("football-auto-publish/1.0 "
      "(https://github.com/chenwu6688/football-auto-publish; issue tracker) "
      "python-requests")

MIN_W, MIN_H = 800, 600

# 许可白名单（fail-closed 的第一半）：只认这些形态
_OK_LICENSE = re.compile(
    r"^(?:"
    r"cc0(?:\s*1\.0)?"
    r"|cc[-\s]by(?:[-\s]sa)?(?:\s*\d(?:\.\d)?)?"
    r"|public\s*domain(?:\s*[-\u2013]?\s*(?:self|mark|expired|d\.?c\.?|\d\.\d))?"
    r"|pd(?:[-\s].*)?"
    r")$",
    re.IGNORECASE,
)
# 许可黑名单（fail-closed 的第二半）：即便上面正则手滑放过也拦住
_BAD_LICENSE = re.compile(
    r"fair\s*use|non[-\s]?free|copyright(?!ed\s*free)|all\s*rights|"
    r"gfdl|no\s*licen[cs]e|unknown|unclear|\bnc\b|-nc\b|\bnd\b",
    re.IGNORECASE,
)

# 球员类必须落在这些分类下，否则视为同名误配
_SPORT_CATEGORY = re.compile(r"football|soccer|footballer|fifa", re.IGNORECASE)

_CACHE: dict = {}

# 最近一次检索的结果摘要，供调用方熔断用：
#   network=True 表示连不上（超时/DNS 等），调用方应停止后续请求；
#   count 是本次有效图数。两者必须分开——「真没图」不该触发熔断。
LAST: dict = {"network": False, "count": 0}


def _fold(s: str) -> str:
    """去重音 + 小写。Bernabéu 必须能被 bernabeu 命中。"""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _has_cjk(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


def _strip_html(s: str) -> str:
    """extmetadata 的 Artist / ImageDescription 里带 HTML。"""
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
         .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", s).strip()


# ——————————————————————————————————————————————
# 许可与署名
# ——————————————————————————————————————————————

def license_ok(name: str) -> bool:
    """这个许可名能不能用。不认识的一律 False（fail-closed）。"""
    s = _strip_html(name)
    if not s:
        return False
    if _BAD_LICENSE.search(s):
        return False
    return bool(_OK_LICENSE.match(s))


def attribution_of(meta: dict, *, page: str = "") -> dict | None:
    """从 extmetadata 提取署名信息；不满足条件返回 None。

    返回 None 的三种情况都必须拒绝使用这张图：
    - 许可不在白名单（含 Fair use / GFDL / 空值）；
    - 有许可但没有作者（CC BY 要求署名，署不了名就不能用）；
    - 许可名本身缺失。
    """
    def val(key: str) -> str:
        v = (meta or {}).get(key)
        if isinstance(v, dict):
            v = v.get("value")
        return _strip_html(v or "")

    lic = val("LicenseShortName")
    if not license_ok(lic):
        return None
    artist = val("Artist")
    if not artist:
        return None
    return {
        "artist": artist[:80],
        "license": lic[:40],
        "license_url": val("LicenseUrl")[:200],
        "page": page,
    }


def credit_line(attrs, *, source_name: str = "Wikimedia Commons") -> str:
    """一张图的正文署名行（紧凑版，头条正文不吃 HTML）。"""
    a = attrs or {}
    artist = (a.get("artist") or "").strip()
    lic = (a.get("license") or "").strip()
    if not artist or not lic:
        return ""
    return f"图片来源：{source_name}／{artist}（{lic}）"


def append_credit(content: str, lines) -> str:
    """把署名行追加到正文末尾。已存在同样内容则不重复追加。"""
    body = content or ""
    uniq = []
    for ln in (lines or []):
        ln = (ln or "").strip()
        if ln and ln not in body and ln not in uniq:
            uniq.append(ln)
    if not uniq:
        return body
    return body.rstrip() + "\n\n" + "\n".join(uniq)


# ——————————————————————————————————————————————
# 中文实体 → 英文检索名
# ——————————————————————————————————————————————

def _entity_index() -> dict:
    """{中文名/别名/英文小写: (英文名, category)}。字典缺失 → 空。"""
    if "ent" in _CACHE:
        return _CACHE["ent"]
    out: dict = {}
    try:
        data = json.loads(ENTITY_MAP.read_text(encoding="utf-8"))
        for ent in (data.get("entities") or {}).values():
            en = (ent.get("name_en") or "").strip()
            if not en:
                continue
            cat = ent.get("category") or ""
            for zh in [ent.get("name_zh")] + list(ent.get("aliases") or []):
                if zh:
                    out.setdefault(str(zh).strip(), (en, cat))
            out.setdefault(en.lower(), (en, cat))
    except Exception:
        out = {}
    _CACHE["ent"] = out
    return out


def resolve(name: str) -> tuple[str, str]:
    """中文名 → (英文检索名, category)。非词典实体原样返回、category 为空。"""
    n = (name or "").strip()
    if not n:
        return "", ""
    hit = _entity_index().get(n) or _entity_index().get(n.lower())
    if hit:
        return hit
    return (n, "") if not _has_cjk(n) else ("", "")


# ——————————————————————————————————————————————
# 检索
# ——————————————————————————————————————————————

def _pages(payload: dict) -> list:
    """兼容 formatversion=1（dict）与 2（list）。"""
    q = (payload or {}).get("query") or {}
    pages = q.get("pages")
    if isinstance(pages, dict):
        return list(pages.values())
    return list(pages or [])


def _cats(page: dict) -> list:
    out = []
    for c in page.get("categories") or []:
        t = c.get("title") if isinstance(c, dict) else str(c)
        if t:
            out.append(t)
    return out


def _tokens(query: str) -> list:
    """检索词里长度 ≥5 的 token（短词太容易撞，不用）。"""
    return [t for t in re.split(r"[\s\-_.]+", _fold(query)) if len(t) >= 5]


def search(query: str, need: int = 2, *, timeout: int = 12,
           require_sport_category: bool = False, session=None) -> list:
    """在 Commons 搜图片，返回最多 ``need`` 张**已过许可+署名+尺寸+相关性**的图。

    每张图带 ``attribution``（作者/许可/许可链接/文件页），调用方必须把它写进正文。
    """
    LAST.update({"network": False, "count": 0})
    if not query or need <= 0 or requests is None:
        return []
    if _has_cjk(query):
        # Commons 的检索以英文/原文名为主，中文 query 召回质量差且容易飘
        _diag.record("wikimedia", COMMONS_API, kind="empty",
                     detail=f"拒绝中文 query（应先经 resolve() 转英文）：{query[:30]}")
        return []
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": "6",
        "gsrlimit": str(max(need * 3, 6)),
        "prop": "imageinfo|categories",
        "iiprop": "url|size|extmetadata",
        "iiurlwidth": "1200",
        "cllimit": "max", "clshow": "!hidden",
        "maxlag": "5",
    }
    sess = session or requests
    try:
        resp = sess.get(COMMONS_API, params=params, headers={"User-Agent": UA},
                        timeout=timeout)
    except Exception as e:
        LAST["network"] = True
        _diag.record("wikimedia", COMMONS_API, kind=_diag.classify_exception(e),
                     detail=f"{type(e).__name__}: {str(e)[:100]}")
        return []
    if resp.status_code != 200:
        _diag.record("wikimedia", COMMONS_API,
                     kind=_diag.classify_status(resp.status_code),
                     status=resp.status_code,
                     detail=f"HTTP {resp.status_code}: {resp.text[:120]}")
        return []

    toks = _tokens(query)
    out, rejected = [], {"license": 0, "no_artist": 0, "size": 0, "irrelevant": 0,
                         "not_sport": 0}
    for page in _pages(resp.json() if resp.content else {}):
        if len(out) >= need:
            break
        title = page.get("title") or ""
        ii = page.get("imageinfo") or []
        if isinstance(ii, dict):
            ii = [ii]
        info = ii[0] if ii else {}
        if not info:
            continue

        cats = _cats(page)
        if require_sport_category and not any(_SPORT_CATEGORY.search(c) for c in cats):
            rejected["not_sport"] += 1
            continue

        meta = info.get("extmetadata") or {}
        page_url = info.get("descriptionurl") or ""
        attrs = attribution_of(meta, page=page_url)
        if attrs is None:
            lic = _strip_html(((meta.get("LicenseShortName") or {}) or {}).get("value"))
            rejected["no_artist" if license_ok(lic) else "license"] += 1
            continue

        w, h = int(info.get("width") or 0), int(info.get("height") or 0)
        if w < MIN_W or h < MIN_H:
            rejected["size"] += 1
            continue

        # 相关性：文件名 + 描述 + 分类里必须出现检索词的某个 token
        hay = _fold(title + " " + _strip_html(
            ((meta.get("ImageDescription") or {}) or {}).get("value")) + " " + " ".join(cats))
        if toks and not any(t in hay for t in toks):
            rejected["irrelevant"] += 1
            continue

        url = info.get("thumburl") or info.get("url") or ""
        if not url.startswith("http"):
            continue
        out.append({
            "url": url,
            "source": "wikimedia",
            "alt": _strip_html(((meta.get("ImageDescription") or {}) or {}).get("value"))[:80]
                   or title[:80],
            "query": query,
            "width": w, "height": h,
            "attribution": attrs,
        })

    _diag.record("wikimedia", COMMONS_API, status=200, count=len(out),
                 detail=f"query={query[:40]} 拒={{{', '.join(f'{k}:{v}' for k, v in rejected.items() if v)}}}")
    LAST["count"] = len(out)
    return out


def search_for_entity(name: str, need: int = 2, *, timeout: int = 12,
                      session=None) -> list:
    """中文实体名 → 英文检索 → Commons 取图。词典外的中文名直接放弃。"""
    en, cat = resolve(name)
    if not en:
        return []
    return search(en, need, timeout=timeout,
                  require_sport_category=(cat == "player"), session=session)


# ——————————————————————————————————————————————
# 自检
# ——————————————————————————————————————————————

def _selftest() -> list:
    cases = []
    # 1) 许可白名单
    for lic, want in [("CC0", True), ("CC0 1.0", True), ("Public domain", True),
                      ("CC BY 4.0", True), ("CC BY-SA 4.0", True), ("CC BY-SA 3.0", True),
                      ("CC BY-NC 4.0", False), ("CC BY-ND 4.0", False),
                      ("Fair use", False), ("Non-free", False), ("GFDL", False),
                      ("", False), ("All rights reserved", False),
                      # Commons 上有 {{Copyrighted free use}} 模板，但它既无标准许可名
                      # 也无署名要求，无法在正文里合规标注 → 按 fail-closed 一并拒绝
                      ("Copyrighted free use", False)]:
        cases.append((f"许可 {lic!r} → {want}", license_ok(lic) is want))
    # 2) 署名 fail-closed
    meta_ok = {"LicenseShortName": {"value": "CC BY-SA 4.0"},
               "Artist": {"value": "<a href='#'>张三</a>"},
               "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"}}
    a = attribution_of(meta_ok, page="https://commons.wikimedia.org/wiki/File:X.jpg")
    cases.append(("能取到作者并去 HTML", a is not None and a["artist"] == "张三"))
    cases.append(("许可被原样保留", a is not None and a["license"] == "CC BY-SA 4.0"))
    cases.append(("无作者 → 拒绝（署不了名）",
                  attribution_of({"LicenseShortName": {"value": "CC BY-SA 4.0"}}) is None))
    cases.append(("Fair use → 拒绝",
                  attribution_of({"LicenseShortName": {"value": "Fair use"},
                                  "Artist": {"value": "x"}}) is None))
    # 3) 署名行
    cases.append(("署名行含作者与许可",
                  "张三" in credit_line(a) and "CC BY-SA 4.0" in credit_line(a)))
    cases.append(("空署名 → 空串", credit_line(None) == "" and credit_line({}) == ""))
    body = append_credit("正文", [credit_line(a)])
    cases.append(("追加到正文末尾", body.startswith("正文") and "图片来源" in body))
    cases.append(("不重复追加", append_credit(body, [credit_line(a)]) == body))
    # 4) 消歧与 token
    cases.append(("token 去掉短词", _tokens("Real Madrid CF") == ["madrid"]))
    cases.append(("去重音：Bernabéu→bernabeu", _fold("Bernabéu") == "bernabeu"))
    cases.append(("中文 query 被拒",
                  search("皇家马德里", 2, session=object()) == []))
    # 5) 中文实体解析
    en, cat = resolve("皇家马德里")
    cases.append(("中文队名→英文名", en.lower().startswith("real madrid")))
    cases.append(("球队 category", cat == "club"))
    cases.append(("球员 category", resolve("哈兰德")[1] == "player"))
    cases.append(("词典外中文名放弃", resolve("某个不存在的球员") == ("", "")))
    # 6) 响应解析（两种 formatversion）
    fake = {"query": {"pages": [
        {"title": "File:Zhang San 2024.jpg",
         "imageinfo": [{"width": 1600, "height": 1200, "thumburl": "https://u/a.jpg",
                        "descriptionurl": "https://commons.wikimedia.org/wiki/File:A.jpg",
                        "extmetadata": meta_ok}],
         "categories": [{"title": "Category:Association football"}]}]}}

    class _R:
        status_code = 200
        content = b"{}"

        def json(self_inner):
            return fake

    class _S:
        def get(self_inner, *a, **k):
            return _R()

    got = search("Zhang San", 1, session=_S())
    cases.append(("解析 formatversion=2 列表", len(got) == 1))
    cases.append(("解析出的图带 attribution", bool(got and got[0].get("attribution"))))
    # 尺寸闸门
    fake2 = json.loads(json.dumps(fake))
    fake2["query"]["pages"][0]["imageinfo"][0]["width"] = 100
    class _S2(_S):
        def get(self_inner, *a, **k):
            r = _R()
            r.json = lambda: fake2
            return r
    cases.append(("小图被拒", search("Zhang San", 1, session=_S2()) == []))
    # 球员类缺 football 分类 → 拒
    fake3 = json.loads(json.dumps(fake))
    fake3["query"]["pages"][0]["title"] = "File:Socrates bust.jpg"
    fake3["query"]["pages"][0]["categories"] = [{"title": "Category:Philosophers"}]
    class _S3(_S):
        def get(self_inner, *a, **k):
            r = _R()
            r.json = lambda: fake3
            return r
    cases.append(("球员无足球分类被拒（挡哲学家）",
                  search("Socrates", 1, require_sport_category=True, session=_S3()) == []))
    return cases


if __name__ == "__main__":
    import sys
    res = _selftest()
    bad = [n for n, ok in res if not ok]
    for n, ok in res:
        print(("  ✅ " if ok else "  ❌ ") + n)
    print(f"\n{len(res) - len(bad)}/{len(res)} 通过")
    sys.exit(1 if bad else 0)
