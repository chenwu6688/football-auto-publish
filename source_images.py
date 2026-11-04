#!/usr/bin/env python3
"""新闻源正文图复用（2026-11-04 用户指令：直接复用 source_images，不走图层方案）。

## 问题

`media_scraper.scrape_match_report` 抓直播吧战报时，会把正文 `<img>` 抽成
``report["images"]``，`data_collector._collect_from_media` 再落到
``fixture["source_images"]``。但 2026-11-04 之前全项目只有
``orchestrator._find_source_article``（Pipeline A 改写路径）一条路径消费过它，
其余 6 处配图调用点都是从 LLM 产出里**重建**一个 img_topic，根本走不到那个 fixture
——抓了等于没抓。

## 为什么这条比图库优

| 维度 | 新闻源正文图 | 图库（Pixabay/Pexels/Unsplash） |
| 网络 | 0 次请求（采集时已拿） | 每次都要查 |
| Key | 不需要 | 需要，且 Unsplash 应用还在人工审核 |
| 图文一致 | 100%（就是那场比赛的图） | 靠 query 猜，实测常配错人 |

所以它是**第一优先级**，不是「图源不通时的备选」。

## 三件事（本模块只做这三件，不做别的）

1. ``images_of(obj)`` —— 从一个对象里把正文图找出来。字段名有四个历史写法：
   ``source_images``（fixture）、``fixture.source_images``（source 结构）、
   ``images``（media_scraper 原始返回）、``facts.source_images``。**一处收口**，
   调用方不用再猜字段名——2026-11-04 之前 `_img_topic` 漏了 ``fixture.`` 这一层，
   而那恰好是上游唯一真实存在的那一层，于是「透传」写成了空转。
2. ``build_index(match_data)`` —— 把本次采集到的 fixture 提成
   ``[{home, away, images}]``，挂在 ``match_data`` 上（不建全局态）。
3. ``lookup(index, text, forms_fn)`` —— 按**队名二元组**把一篇稿子关联到它那场
   比赛的战报图。判据是「两支球队的可接受写法都出现在稿件文本里」，缺一不可：
   只命中一支就会把「上海海港」的图配给「上海申花」——这比不配图更糟
   （配错图是事实错误，无图只是缺图，后者有 cover_card 兜底）。

## 纪律

- 只取 ``http(s)`` 开头的绝对地址。直播吧正文里还有相对路径与 ``data:`` 占位图，
  原样传下去 ``search_images`` 会静默跳过 → 又是「抓了等于没抓」。
- 不改 ``media_scraper``、不改 ``data_collector`` 的采集口径：本模块只消费已有字段。
- 队名是否同队的判定**不在本模块**：由调用方注入 ``forms_fn``（唯一口径是
  ``orchestrator._team_forms``，别名感知 + entity_map）。本模块不另写一套别名表
  ——10-24 比分口径事故就是「三处各写各的」造成的。

命令行：
    python3 source_images.py --selftest
"""

from __future__ import annotations

import argparse

# 单篇最多取几张：与 publish 侧「首图 + 2 张内文图」对齐；取多了既拖慢下载，
# 也会让不同稿件用同一场的同一组图，读者一眼看出是同一篇。
MAX_IMAGES = 3

# 允许的取图字段（按可信度排序：越靠前越是「这条内容的原始配图」）
_FIELDS = ("source_images", "images")


def _norm_item(it) -> dict | None:
    """把 ``"url"`` / ``{"url": ...}`` 统一成 dict，非 http 一律丢弃。"""
    if isinstance(it, str):
        url, alt, src = it, "", ""
    elif isinstance(it, dict):
        url = it.get("url") or it.get("src") or ""
        alt = it.get("alt") or it.get("title") or ""
        src = it.get("source") or ""
    else:
        return None
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    # 直播吧正文里混着「加载中」占位图与头像缩略图，靠文件名特征挡掉
    low = url.lower()
    if any(k in low for k in ("loading.", "placeholder", "/icon", "avatar", "logo_", "spacer")):
        return None
    return {"url": url, "alt": alt, "source": src or "news_source"}


def images_of(obj, *, limit: int = MAX_IMAGES) -> list[dict]:
    """从一个对象里取正文图（四个历史字段名 + 一层 fixture 嵌套）。

    ``obj`` 可以是 fixture、source 结构、media_scraper 的 report、或 topic。
    取不到返回 ``[]``——**不返回占位图**，无图由 cover_card 兜底。
    """
    if not isinstance(obj, dict):
        return []
    buckets: list = []
    for f in _FIELDS:
        v = obj.get(f)
        if isinstance(v, list):
            buckets.append(v)
    fx = obj.get("fixture")
    if isinstance(fx, dict):
        for f in _FIELDS:
            v = fx.get(f)
            if isinstance(v, list):
                buckets.append(v)
    facts = obj.get("facts")
    if isinstance(facts, dict):
        for f in _FIELDS:
            v = facts.get(f)
            if isinstance(v, list):
                buckets.append(v)

    out: list[dict] = []
    seen: set[str] = set()
    for b in buckets:
        for it in b:
            n = _norm_item(it)
            if n and n["url"] not in seen:
                seen.add(n["url"])
                out.append(n)
                if len(out) >= limit:
                    return out
    return out


def build_index(match_data) -> list[dict]:
    """把本次采集的 fixture 提成 ``[{home, away, images, url}]``（去重合并）。

    认两个位置：``all_fixtures``（主采集结果）与 ``media_reports``（有正文的战报）。
    两者可能是同一批对象，按 ``(home, away)`` 合并取并集。
    """
    if not isinstance(match_data, dict):
        return []
    recs: dict[tuple[str, str], dict] = {}
    pools = []
    af = match_data.get("all_fixtures")
    if isinstance(af, list):
        pools.append(af)
    mr = match_data.get("media_reports")
    if isinstance(mr, dict):
        pools.append(list(mr.values()))

    for pool in pools:
        for f in pool:
            if not isinstance(f, dict):
                continue
            home = (f.get("home_team") or f.get("home_zh") or "").strip()
            away = (f.get("away_team") or f.get("away_zh") or "").strip()
            if not home or not away:
                continue
            imgs = images_of(f)
            if not imgs:
                continue
            key = (home, away)
            rec = recs.get(key)
            if rec is None:
                recs[key] = {"home": home, "away": away, "images": imgs,
                             "url": f.get("source_url") or ""}
            else:
                seen = {i["url"] for i in rec["images"]}
                for i in imgs:
                    if i["url"] not in seen and len(rec["images"]) < MAX_IMAGES:
                        rec["images"].append(i)
                        seen.add(i["url"])
    return [r for r in recs.values() if r["images"]]


def get_index(match_data) -> list[dict]:
    """``build_index`` 的缓存版：结果挂回 ``match_data``，一次批次只算一次。

    挂在入参对象上而不是模块级全局：单测里换一个 ``match_data`` 天然隔离，
    也不会像全局缓存那样在两次批次之间串味（`_HIST_CACHE` 同样的处理）。
    """
    if not isinstance(match_data, dict):
        return []
    cached = match_data.get("_source_img_index")
    if isinstance(cached, list):
        return cached
    idx = build_index(match_data)
    try:
        match_data["_source_img_index"] = idx
    except Exception:      # 只读代理 / 冻结 dict：退化为每次重算，不因此中断生成
        pass
    return idx


def lookup(index, text, forms_fn, *, limit: int = MAX_IMAGES) -> list[dict]:
    """按队名二元组找回那场比赛的正文图；两支队缺一不可。

    ``forms_fn(name)`` 返回该队名全部可接受写法（注入，避免本模块自带一份别名表）。
    额外要求两队的写法集合**不相交**：上海海港 / 上海申花这类同城球队，
    若别名集合有重叠就无法区分是谁的图，此时宁可不配（配错图是事实错误）。
    """
    if not index or not text or not callable(forms_fn):
        return []
    for rec in index:
        home, away = rec.get("home", ""), rec.get("away", "")
        if not home or not away:
            continue
        hf, af = forms_fn(home), forms_fn(away)
        if not hf or not af:
            continue
        if hf & af:                       # 同城/同名写法重叠 → 无法区分，跳过
            continue
        if any(f in text for f in hf) and any(f in text for f in af):
            return list(rec.get("images") or [])[:limit]
    return []


def selftest() -> list[tuple[str, bool]]:
    cases: list[tuple[str, bool]] = []

    fixture = {"home_team": "皇马", "away_team": "巴萨",
               "source_images": [{"url": "https://a.example/1.jpg"}]}
    cases.append(("fixture.source_images 能取出", len(images_of(fixture)) == 1))
    cases.append(("一参 fixture 嵌套能取出",
                  len(images_of({"fixture": fixture})) == 1))
    cases.append(("media_scraper 的 images 字段能取出",
                  len(images_of({"images": [{"url": "https://b.example/2.jpg"}]})) == 1))
    cases.append(("facts 里的能取出",
                  len(images_of({"facts": {"source_images": ["https://c.example/3.jpg"]}})) == 1))

    # 负向：相对路径 / data: / 占位图必须丢掉，否则下游静默跳过 = 抓了等于没抓
    cases.append(("相对路径丢弃", images_of({"source_images": ["/img/a.jpg"]}) == []))
    cases.append(("data: 丢弃", images_of({"source_images": ["data:image/png;base64,x"]}) == []))
    cases.append(("loading 占位图丢弃",
                  images_of({"source_images": ["https://x.example/loading.gif"]}) == []))
    cases.append(("非 dict 不抛", images_of("nonsense") == []))
    cases.append(("去重", len(images_of({"source_images": [
        "https://d.example/1.jpg", "https://d.example/1.jpg"]})) == 1))
    cases.append(("上限 3 张", len(images_of({"source_images": [
        f"https://e.example/{i}.jpg" for i in range(6)]})) == MAX_IMAGES))

    md = {"all_fixtures": [fixture, {"home_team": "拜仁", "away_team": "多特",
                                     "source_images": []}]}
    idx = get_index(md)
    cases.append(("索引只收有图的 fixture", len(idx) == 1))
    cases.append(("索引被缓存回写", md.get("_source_img_index") is not None))

    forms = lambda n: frozenset({n, n[:2]})     # noqa: E731  简易别名：全名 + 前两字
    hit = lookup(idx, "皇马3-1巴萨，第70分钟换人后连丢2球", forms)
    cases.append(("两支队都在正文 → 命中", len(hit) == 1))
    miss = lookup(idx, "皇马又被绝杀了", forms)          # 只提到一支
    cases.append(("只命中一支队 → 不配图（配错图比无图更糟）", miss == []))
    cases.append(("文本为空 → 不配图", lookup(idx, "", forms) == []))
    cases.append(("index 为空 → 不配图", lookup([], "皇马打巴萨", forms) == []))

    same_city = [{"home": "上海海港", "away": "上海申花",
                  "images": [{"url": "https://f.example/1.jpg"}]}]
    cases.append(("同城两队写法重叠 → 跳过",
                  lookup(same_city, "上海海港对阵上海申花", lambda n: frozenset({n, "上海"})) == []))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="新闻源正文图复用")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if not args.selftest:
        ap.print_help()
        return 0
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检 {len(selftest())} 项，失败 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
