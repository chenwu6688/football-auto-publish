#!/usr/bin/env python3
"""RedFox 热点数据客户端 —— Phase 1 只读试点（用户 2026-10-13 授权，Key 走环境变量）。

定位：为选题择优（ranker L2「社媒讨论量」维度）提供**真实**热度信号——
抖音足球热榜作品 / 公众号足球热文的互动量归一化。信号只作输入，绝不写入正文。

架构纪律（与 signal_sources 完全一致，另加付费 API 的额度纪律）：
  1. **只读试点**：只调搜索类端点，不写、不改 RedFox 侧任何数据；
  2. **主流程不直连**：由 scripts/refresh_redfox_signals.py 预热落
     data/signals/redfox_signals.json，主流程经 signal_sources.load_external 只读；
  3. **绝不假装有信号**：无 Key / 网络失败 / code≠2000 / 超日调用上限 → 一律返回空，
     由调用方按「缺失」处理，绝不用 0 或编造数据冒充（计划九降级不阻塞）；
  4. **额度台账**：每次真实调用落 data/redfox_budget.json（{日期: 次数}），
     日上限 daily_call_cap 是硬闸，测试/verify 前必须把 *_PATH 隔离到 tempfile；
  5. **缓存优先**：TTL 内直接读 data/redfox_cache.json，不重复烧积分。

归一化口径：
  - 互动量 engagement：抖音 = 点赞 + 评论 + 分享；公众号 = 阅读 + 点赞 + 在看；
  - 原始计数全部保留在 raw 字段，绝不折算、绝不用 -1 之类占位符冒充（平台缺就是 None）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "redfox.yaml"
CACHE_PATH = PROJECT_ROOT / "data" / "redfox_cache.json"
BUDGET_PATH = PROJECT_ROOT / "data" / "redfox_budget.json"
SIGNALS_PATH = PROJECT_ROOT / "data" / "signals" / "redfox_signals.json"

ENV_KEY = "REDFOX_API_KEY"

# 配置缺失时的最小可用集（仅字段级兜底；配置整体不可用则调用方降级放行）
_FALLBACK = {
    "base_url": "https://redfox.hk",
    "timeout": 15,
    "endpoints": {"douyin_works": "/story/api/dyData/searchArticle"},
    "endpoint_enabled": {"douyin_works": True},
    "sort": "_4",
    "per_keyword_limit": 20,
    "keywords": ["足球"],
    "cache_ttl_hours": 12,
    "daily_call_cap": 30,
}

_CONFIG_CACHE: dict = {}


# ------------------------------------------------------------
# 配置 / 路径
# ------------------------------------------------------------
def load_config(path: Path | str | None = None) -> dict | None:
    """读取配置；不可用返回 None（调用方据此降级，不阻塞）。"""
    p = Path(path or CONFIG_PATH)
    key = str(p)
    if key in _CONFIG_CACHE:
        return _CONFIG_CACHE[key]
    cfg = None
    try:
        import yaml
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                cfg = data
    except Exception:
        cfg = None
    _CONFIG_CACHE[key] = cfg
    return cfg


def api_key() -> str:
    """从环境取 Key（本地 scripts/local_env.sh / CI Secrets 注入）。"""
    return (os.environ.get(ENV_KEY) or "").strip()


def _cfg_value(cfg: dict | None, key: str):
    return (cfg or {}).get(key, _FALLBACK.get(key))


def _today() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _load_json(p: Path) -> dict:
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(p: Path, obj: dict) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------
# 额度台账（硬闸：超日上限一律不再真实调用）
# ------------------------------------------------------------
def calls_today(budget_path: Path | None = None) -> int:
    return int((_load_json(Path(budget_path or BUDGET_PATH)) or {}).get(_today(), 0) or 0)


def _record_call(budget_path: Path | None = None) -> int:
    p = Path(budget_path or BUDGET_PATH)
    budget = _load_json(p)
    n = int(budget.get(_today(), 0) or 0) + 1
    budget[_today()] = n
    # 只保留近 7 天，防止台账无限增长
    cutoff = (datetime.now(CST) - timedelta(days=7)).strftime("%Y-%m-%d")
    for d in [d for d in budget if d < cutoff]:
        del budget[d]
    _save_json(p, budget)
    return n


# ------------------------------------------------------------
# 缓存
# ------------------------------------------------------------
def _cache_fresh(entry: dict, ttl_hours: int) -> bool:
    try:
        at = datetime.strptime(str(entry.get("fetched_at", "")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
    except Exception:
        return False
    return datetime.now(CST) - at < timedelta(hours=ttl_hours)


# ------------------------------------------------------------
# HTTP（唯一出口；所有异常都在这里吞掉并返回 None）
# ------------------------------------------------------------
def _post(endpoint: str, body: dict, cfg: dict | None = None,
          budget_path: Path | None = None) -> dict | None:
    """POST 到 RedFox；成功（code==2000）返回 data dict，其余一切情况返回 None。

    失败原因一律不进返回值——调用方只关心「有没有数据」，原因由日志可见。
    """
    key = api_key()
    if not key:
        return None
    cap = int(_cfg_value(cfg, "daily_call_cap") or 0)
    if cap and calls_today(budget_path) >= cap:
        return None
    base = str(_cfg_value(cfg, "base_url") or "https://redfox.hk").rstrip("/")
    url = base + endpoint
    try:
        import requests
        r = requests.post(url, headers={"REDFOX_API_KEY": key,
                                        "Content-Type": "application/json"},
                          json=body, timeout=int(_cfg_value(cfg, "timeout") or 15))
        data = r.json()
    except Exception:
        return None
    _record_call(budget_path)          # 只要真发出了请求就记账（含被服务端拒绝的）
    if not isinstance(data, dict) or data.get("code") != 2000:
        return None
    payload = data.get("data")
    return payload if isinstance(payload, dict) else None


# ------------------------------------------------------------
# 归一化
# ------------------------------------------------------------
def _int_or_none(v):
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return int(v)
    except Exception:
        return None


def normalize_work(item: dict, platform: str) -> dict | None:
    """把平台原始条目归一化为统一信号记录；无法识别返回 None。

    platform: "douyin" | "gzh"。engagement = 归一化互动量（抖音=赞+评+转；
    公众号=阅读+赞+在看）；raw 原样保留全部计数，绝不折算。
    """
    if not isinstance(item, dict):
        return None
    item = {k: v for k, v in item.items() if v is not None}
    title = str(item.get("title") or "").strip()
    if not title:
        return None

    if platform == "douyin":
        like, comment, share = (_int_or_none(item.get("likeCount")),
                                _int_or_none(item.get("commentCount")),
                                _int_or_none(item.get("shareCount")))
        eng = sum(x for x in (like, comment, share) if x is not None)
        return {
            "platform": "douyin",
            "work_id": str(item.get("workId") or ""),
            "title": title,
            "url": str(item.get("workUrl") or ""),
            "publish_time": str(item.get("publishTime") or ""),
            "author": str(item.get("accountName") or ""),
            "follower_count": _int_or_none(item.get("followerCount")),
            "engagement": eng,
            "comment_top_keywords": [str(x) for x in (item.get("commentTopKeywords") or [])],
            "crawl_time": str(item.get("crawlTime") or ""),
            "raw": {"like": like, "comment": comment, "share": share,
                    "collect": _int_or_none(item.get("collectCount")),
                    "repost": _int_or_none(item.get("repostCount"))},
        }
    if platform == "gzh":
        read, like, watch = (_int_or_none(item.get("readCount")),
                             _int_or_none(item.get("likeCount")),
                             _int_or_none(item.get("watchCount")))
        eng = sum(x for x in (read, like, watch) if x is not None)
        return {
            "platform": "gzh",
            "work_id": str(item.get("workUuid") or ""),
            "title": title,
            "url": str(item.get("workUrl") or ""),
            "publish_time": str(item.get("publishTime") or ""),
            "author": str(item.get("author") or ""),
            "follower_count": None,      # 公众号作品条目无粉丝数字段，缺就是缺
            "engagement": eng,
            "comment_top_keywords": [],
            "crawl_time": str(item.get("syncTime") or ""),
            "raw": {"read": read, "like": like, "watch": watch,
                    "comment": _int_or_none(item.get("commentCount")),
                    "share": _int_or_none(item.get("shareCount"))},
        }
    return None


# ------------------------------------------------------------
# 搜索（缓存 + 额度 + 降级）
# ------------------------------------------------------------
def search_works(keyword: str, platform: str = "douyin", *,
                 cfg: dict | None = None,
                 cache_path: Path | None = None,
                 budget_path: Path | None = None,
                 use_cache: bool = True) -> list[dict]:
    """按关键词搜作品，返回归一化记录列表；任何失败/超限返回 []。

    缓存键 = f"{platform}:{keyword}"，TTTL 内直接返回缓存（不调用、不计费）。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    cfg = cfg if cfg is not None else load_config()
    if cfg is None:
        return []
    enabled = ((cfg.get("endpoint_enabled") or {}).get(
        "douyin_works" if platform == "douyin" else "gzh_articles"))
    endpoint = (cfg.get("endpoints") or {}).get(
        "douyin_works" if platform == "douyin" else "gzh_articles")
    if not enabled or not endpoint:
        return []

    cpath = Path(cache_path or CACHE_PATH)
    cache = _load_json(cpath) if use_cache else {}
    ckey = f"{platform}:{keyword}"
    ttl = int(_cfg_value(cfg, "cache_ttl_hours") or 12)
    if use_cache and _cache_fresh(cache.get(ckey) or {}, ttl):
        return list((cache.get(ckey) or {}).get("items") or [])

    body = {"keyword": keyword, "sort": str(_cfg_value(cfg, "sort") or "_4"), "offset": 0}
    data = _post(endpoint, body, cfg, budget_path)
    items = []
    if data:
        limit = int(_cfg_value(cfg, "per_keyword_limit") or 20)
        for it in (data.get("list") or [])[:limit]:
            rec = normalize_work(it, platform)
            if rec:
                items.append(rec)
        if use_cache:
            cache[ckey] = {"fetched_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
                           "items": items}
            _save_json(cpath, cache)
    return items


def fetch_hot_works(*, keywords: list[str] | None = None, platform: str = "douyin",
                    cfg: dict | None = None,
                    cache_path: Path | None = None,
                    budget_path: Path | None = None) -> list[dict]:
    """批量取足球关键词热榜作品（缓存/额度/降级全生效）。失败词静默跳过。"""
    cfg = cfg if cfg is not None else load_config()
    kws = keywords or list((cfg or {}).get("keywords") or [])
    out: list[dict] = []
    for kw in kws:
        out.extend(search_works(kw, platform, cfg=cfg,
                                cache_path=cache_path, budget_path=budget_path))
    return out


# ------------------------------------------------------------
# 信号表构造
# ------------------------------------------------------------
def entity_heat(records: list[dict], keywords: list[str]) -> dict:
    """{关键词: {"社媒讨论量": 互动量之和, "作品数": n}}（只含真实命中）。

    互动量口径见 normalize_work；无命中的词不出现（绝不假装有信号）。
    """
    out: dict[str, dict] = {}
    for kw in (keywords or []):
        kw = (kw or "").strip()
        if not kw:
            continue
        hits = [r for r in records if kw in (r.get("title") or "")]
        if not hits:
            continue
        out[kw] = {"社媒讨论量": int(sum(r.get("engagement") or 0 for r in hits)),
                   "作品数": len(hits)}
    return out


def hot_comment_words(records: list[dict], top: int = 20) -> list[dict]:
    """评论热词聚合（[{"word","count"}]，降序）——观察新兴实体，不直接当信号。"""
    from collections import Counter
    c: Counter = Counter()
    for r in records:
        for w in (r.get("comment_top_keywords") or []):
            w = str(w).strip()
            if w:
                c[w] += 1
    return [{"word": w, "count": n} for w, n in c.most_common(top)]


def build_signals_payload(records: list[dict], keywords: list[str]) -> dict:
    """组装 redfox_signals.json 内容（供 signal_sources 合并 + 看板只读）。"""
    works = sorted(records, key=lambda r: -(r.get("engagement") or 0))
    return {
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "keywords": list(keywords or []),
        "signals": entity_heat(records, keywords),
        "hot_words": hot_comment_words(records),
        "works_top": [
            {"platform": r["platform"], "title": r["title"], "url": r["url"],
             "engagement": r["engagement"], "publish_time": r["publish_time"],
             "author": r["author"]}
            for r in works[:10]
        ],
        "record_count": len(records),
    }


def write_signals(payload: dict, path: Path | None = None) -> Path:
    p = Path(path or SIGNALS_PATH)
    _save_json(p, payload)
    return p


# ------------------------------------------------------------
# 自检 / CLI
# ------------------------------------------------------------
def selftest() -> list[tuple[str, bool]]:
    """冒烟自检：归一化口径 / 缓存命中不重复调用 / 限额硬闸 / 降级路径 / 信号表语义。"""
    cases: list[tuple[str, bool]] = []

    dy_item = {"workId": "1", "title": "梅西倒钩", "workUrl": "http://x/1",
               "publishTime": "2026-10-13 10:00:00", "accountName": "足球哥",
               "followerCount": "6668", "likeCount": 100, "commentCount": 20,
               "shareCount": 5, "collectCount": 7, "crawlTime": "2026-10-13 12:00:00",
               "commentTopKeywords": ["梅西", "倒钩"]}
    rec = normalize_work(dy_item, "douyin")
    cases.append(("抖音归一化 engagement=125", rec and rec["engagement"] == 125))
    cases.append(("抖音 follower 转 int", rec and rec["follower_count"] == 6668))
    cases.append(("抖音 raw 保留原值", rec and rec["raw"]["collect"] == 7))

    gzh_item = {"workUuid": "U1", "title": "国足大名单", "workUrl": "http://x/2",
                "publishTime": "2026-10-13 09:00:00", "author": "体坛",
                "readCount": 50000, "likeCount": 300, "watchCount": 100,
                "syncTime": "2026-10-13 12:00:00"}
    rec2 = normalize_work(gzh_item, "gzh")
    cases.append(("公众号归一化 engagement=50400", rec2 and rec2["engagement"] == 50400))
    cases.append(("公众号 follower_count 为 None（缺就是缺）",
                  rec2 and rec2["follower_count"] is None))

    cases.append(("无 title 返回 None", normalize_work({"workId": "2"}, "douyin") is None))
    cases.append(("未知平台返回 None", normalize_work(dy_item, "weibo") is None))

    # 缓存/额度：patch requests.post 走**真实**记账与限额路径（不在 _post 层打桩，
    # 否则限额闸与台账都会被绕过，测了等于没测）
    import tempfile
    from unittest import mock
    tmp = Path(tempfile.mkdtemp())
    cpath, bpath = tmp / "cache.json", tmp / "budget.json"

    class _Resp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    calls = {"n": 0}

    def _fake_requests_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _Resp({"code": 2000, "data": {"list": [dy_item]}})

    orig_env = os.environ.get(ENV_KEY)
    os.environ[ENV_KEY] = "selftest_fake_key"
    try:
        with mock.patch("requests.post", side_effect=_fake_requests_post):
            cfg = {"base_url": "https://x", "timeout": 5, "sort": "_4",
                   "per_keyword_limit": 5, "cache_ttl_hours": 12, "daily_call_cap": 30,
                   "endpoints": {"douyin_works": "/e"},
                   "endpoint_enabled": {"douyin_works": True},
                   "keywords": ["梅西"]}
            r1 = search_works("梅西", "douyin", cfg=cfg, cache_path=cpath, budget_path=bpath)
            r2 = search_works("梅西", "douyin", cfg=cfg, cache_path=cpath, budget_path=bpath)
            cases.append(("首次搜索 1 条", len(r1) == 1))
            cases.append(("TTL 内第二次走缓存（0 次新调用）",
                          len(r2) == 1 and calls["n"] == 1))
            cases.append(("缓存命中不计费（台账 1 次）", calls_today(bpath) == 1))

            # 超日上限 → 硬闸生效，不再发请求
            from datetime import datetime as _dt
            bpath.write_text(json.dumps({_dt.now(CST).strftime("%Y-%m-%d"): 999}),
                             encoding="utf-8")
            r3 = search_works("C罗", "douyin", cfg=cfg, cache_path=cpath, budget_path=bpath)
            cases.append(("超日上限降级返回 []", r3 == [] and calls["n"] == 1))
    finally:
        if orig_env is None:
            os.environ.pop(ENV_KEY, None)
        else:
            os.environ[ENV_KEY] = orig_env

    # 信号表语义
    recs = [rec, rec2]
    eh = entity_heat(recs, ["梅西", "国足", "热狗"])
    cases.append(("entity_heat 只含命中词", set(eh) == {"梅西", "国足"}))
    cases.append(("entity_heat 求和正确", eh["梅西"]["社媒讨论量"] == 125))
    payload = build_signals_payload(recs, ["梅西", "国足"])
    cases.append(("payload works_top 按互动降序",
                  payload["works_top"][0]["title"] == "国足大名单"))
    cases.append(("payload hot_words 聚合评论词",
                  any(w["word"] == "梅西" for w in payload["hot_words"])))

    # 网络异常 / 非 2000 → 降级返回 []（不假装有信号）
    # 注意：这两个用例也必须传 budget_path 到 tempfile——「非 2000」路径在 r.json()
    # 成功后仍会真实记账（请求已发出），不隔离就会污染 data/redfox_budget.json。
    _before = calls_today(BUDGET_PATH)     # 真实台账可能有历史计数（如当日已 refresh），
    orig_env2 = os.environ.get(ENV_KEY)    # 判据是「自检前后不变」而非「文件不存在」
    os.environ[ENV_KEY] = "selftest_fake_key"
    try:
        with mock.patch("requests.post", side_effect=RuntimeError("网络断了")):
            cases.append(("网络异常降级返回 []",
                          search_works("梅西", "douyin", cfg={
                              "endpoints": {"douyin_works": "/e"},
                              "endpoint_enabled": {"douyin_works": True},
                              "cache_ttl_hours": 12, "daily_call_cap": 30},
                              cache_path=tmp / "c2.json",
                              budget_path=tmp / "b2.json") == []))
        with mock.patch("requests.post", return_value=_Resp({"code": 3201, "msg": "积分不足"})):
            cases.append(("非 2000 降级返回 []",
                          search_works("梅西", "douyin", cfg={
                              "endpoints": {"douyin_works": "/e"},
                              "endpoint_enabled": {"douyin_works": True},
                              "cache_ttl_hours": 12, "daily_call_cap": 30},
                              cache_path=tmp / "c3.json",
                              budget_path=tmp / "b3.json") == []))
        cases.append(("降级路径未污染真实台账", calls_today(BUDGET_PATH) == _before))
    finally:
        if orig_env2 is None:
            os.environ.pop(ENV_KEY, None)
        else:
            os.environ[ENV_KEY] = orig_env2

    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="RedFox 热点数据客户端（Phase 1 只读试点）")
    ap.add_argument("--probe", metavar="KEYWORD", help="单关键词实网探测（真实计费 1 次）")
    ap.add_argument("--refresh", action="store_true", help="刷新全部关键词并写信号表")
    ap.add_argument("--status", action="store_true", help="查看缓存/台账/信号表状态")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        bad = 0
        for name, ok in selftest():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            bad += 0 if ok else 1
        print(f"\n自检结果：{'全部通过 ✅' if not bad else f'{bad} 项未通过 ❌'}")
        return 1 if bad else 0

    if args.probe:
        if not api_key():
            print("未配置 REDFOX_API_KEY（本地 source scripts/local_env.sh）")
            return 2
        recs = search_works(args.probe, "douyin")
        print(f"关键词「{args.probe}」返回 {len(recs)} 条（已计费 1 次）")
        for r in recs[:5]:
            print(f"  [{r['engagement']:>8}] {r['title'][:40]} | {r['author']} | {r['url'][:60]}")
        return 0 if recs else 1

    if args.refresh:
        cfg = load_config()
        if cfg is None:
            print("配置不可用（config/redfox.yaml），降级退出")
            return 2
        if not api_key():
            print("未配置 REDFOX_API_KEY，本次不刷新（不假装有信号）")
            return 2
        kws = list(cfg.get("keywords") or [])
        recs = fetch_hot_works(cfg=cfg)
        payload = build_signals_payload(recs, kws)
        p = write_signals(payload)
        print(f"刷新完成：{len(recs)} 条作品 / {len(payload['signals'])} 个命中词 → {p}")
        print(f"  今日已调用 {calls_today()} 次（上限 {cfg.get('daily_call_cap')}）")
        return 0

    if args.status:
        cache = _load_json(CACHE_PATH)
        budget = _load_json(BUDGET_PATH)
        sig = _load_json(SIGNALS_PATH)
        print(f"Key：{'已配置' if api_key() else '未配置'}")
        print(f"缓存：{len(cache)} 个关键词条目")
        print(f"台账：今日 {calls_today()} 次；近 7 天 {budget}")
        print(f"信号表：{sig.get('generated_at', '（无）')}，"
              f"{len(sig.get('signals') or {})} 个命中词，{sig.get('record_count', 0)} 条作品")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
