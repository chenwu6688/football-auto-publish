#!/usr/bin/env python3
"""采集结果分类台账（计划九 · 降级纪律）。

## 为什么要有这个模块

`media_scraper` 各抓取方法失败时统一 `return []` / `return ""`，于是调用方看到
的「0 篇」有四种完全不同的成因，却长得一模一样：

| 真实情况 | 旧表现 |
|---|---|
| 今天确实没新闻 | 0 篇（正确） |
| 被 EdgeOne 挑战页拦了 | 0 篇（**静默失效**） |
| 网络超时 / DNS 失败 | 0 篇（**静默失效**） |
| 页面改版，选择器全不中 | 0 篇（**静默失效**） |

2026-10-26 实测现场就是这么翻车的：懂球帝过了腾讯 EdgeOne，`_http_get` 那套
请求头被判成爬虫，返回 **HTTP 567 挑战页**（7KB 空壳）。567 不在 403/429/404
分支里 → 走完重试循环 `return ""` → 备源静默归零，日志只有一行
「懂球帝文章: 0 篇」。查了半天以为是「懂球帝没内容」。

同一类事故在 MediaCrawler 上也有（`except DataFetchError: break` + 一行日志），
说明这不是某个项目的疏忽，是**「返回空」这种接口设计的通病**。

## 纪律

1. **四问必须可答**：真空数据 / 被风控 / 网络故障 / 解析失败，不许合并成「空」。
2. **缺失标缺失**：`empty` 与 `blocked` 绝不互相冒充，也绝不用 0 计数代替。
3. **台账写入失败不阻断主流程**：这是观测层，不是业务层。写不进去就打印告警。
4. **只记计数与状态，不记正文**：台账里不放页面内容，避免把采集物二次落盘。

## 用法

    import scraper_diagnostics as diag

    kind = diag.classify_status(resp.status_code)      # 状态码 → 类型
    diag.record("dongqiudi", url, kind, status=567)    # 记一条
    diag.flush()                                       # 落 data/audit/scraper_fetch_latest.json
    diag.summary()                                     # {"by_kind":…, "blocked_sources":[…]}
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
LEDGER_PATH = AUDIT_DIR / "scraper_fetch_latest.json"
LEDGER_MD_PATH = AUDIT_DIR / "scraper_fetch_latest.md"

# ---------------------------------------------------------------- 类型
KIND_OK = "ok"                    # 拿到正文
KIND_EMPTY = "empty"              # HTTP 200 且页面正常，但确实没有目标内容
KIND_BLOCKED = "blocked"          # 403 / 429 / 验证码 / 封禁词命中
KIND_CHALLENGE = "challenge"      # 反爬挑战页（EdgeOne / Cloudflare / JS 挑战）
KIND_NETWORK = "network"          # 超时 / 连接失败 / DNS
KIND_HTTP_ERROR = "http_error"    # 其它非 200（5xx、4xx）
KIND_PARSE = "parse"              # 拿到页面但解析抛异常

KINDS = (KIND_OK, KIND_EMPTY, KIND_BLOCKED, KIND_CHALLENGE,
         KIND_NETWORK, KIND_HTTP_ERROR, KIND_PARSE)

# 需要人工介入的（不是「今天没内容」）
ATTENTION_KINDS = (KIND_BLOCKED, KIND_CHALLENGE, KIND_NETWORK, KIND_HTTP_ERROR, KIND_PARSE)

# 反爬挑战页的状态码：567 是腾讯 EdgeOne 的专用码，521~524 是 Cloudflare 系
CHALLENGE_STATUS = frozenset({405, 412, 418, 429, 521, 522, 523, 524, 567})
BLOCKED_STATUS = frozenset({401, 403})
GOOD_STATUS = 200
MISSING_STATUS = 404          # 页面真没了 ≠ 被拦，二者必须分开

# 挑战页特征词。注意**都是机房/风控厂商的专有串**，不是「验证」这类通用词——
# 足球新闻里出现「验证」「关注」是常态，拿通用词判挑战页会天天误报。
CHALLENGE_MARKERS = (
    "EdgeOne", "eo-", "__jsl_clearance", "cf-chl", "Just a moment",
    "人机验证", "滑动验证", "安全验证", "访问验证", "captcha",
)

# 挑战页都是薄壳。带特征词的长页面多半是「文章里提到了验证两个字」，不是挑战页。
CHALLENGE_MAX_BYTES = 60_000

# ---------------------------------------------------------------- 内存缓冲
_RECORDS: list[dict] = []


def reset() -> None:
    """清空缓冲（测试与 verify 用）。"""
    _RECORDS.clear()


def records() -> list[dict]:
    """当前缓冲的副本（外部改不到内部状态）。"""
    return [dict(r) for r in _RECORDS]


# ---------------------------------------------------------------- 判定
def classify_status(code: int | None) -> str:
    """HTTP 状态码 → 类型。

    404 归 empty 而不是 http_error：页面真不存在，和「被拦」是两件事，
    以前混在一起导致「备源 0 篇」无法定位。
    """
    if code == GOOD_STATUS:
        return KIND_OK
    if code == MISSING_STATUS:
        return KIND_EMPTY
    if code in BLOCKED_STATUS:
        return KIND_BLOCKED
    if code in CHALLENGE_STATUS:
        # 429 既可能是限流也可能是挑战页前置，统一按 challenge 处理：
        # 处置动作（停手 + 告警）与 blocked 一致，但成因要能看见。
        return KIND_CHALLENGE
    return KIND_HTTP_ERROR


def looks_like_challenge(text: str | None) -> bool:
    """HTTP 200 但内容是挑战页壳子。

    两个条件同时满足才判：① 命中机房/风控厂商专有串；② 页面足够短。
    只判①会把「某文章里写了 captcha」误判成被拦。
    """
    if not text:
        return False
    if len(text.encode("utf-8", "ignore")) > CHALLENGE_MAX_BYTES:
        return False
    low = text.lower()
    return any(m.lower() in low for m in CHALLENGE_MARKERS)


def classify_exception(exc: BaseException) -> str:
    """异常 → 类型。只做类型判断，不解析 message（message 里的中文会变）。"""
    name = type(exc).__name__
    if "Challenge" in name:
        return KIND_CHALLENGE
    if "Blocked" in name:
        return KIND_BLOCKED
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return KIND_NETWORK
    if "Timeout" in name or "Connection" in name:
        return KIND_NETWORK
    if "Parse" in name:
        return KIND_PARSE
    return KIND_PARSE


# ---------------------------------------------------------------- 记账
def record(source: str, url: str = "", kind: str = KIND_OK, *,
           status: int | None = None, detail: str = "", count: int = 0) -> dict:
    """记一条采集结果。永不抛异常——观测层不许把主流程带崩。

    **同秒同源同 URL 同类型自动去重**：`_http_get` 在抛异常前已经记过一条，
    调用方的 `except` 往往会把同一个异常再记一次。重试间隔 ≥1s，所以「同一秒内
    完全相同的一条」必然是重复记账，不是两次真实请求。
    """
    rec = {
        "at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "source": source or "unknown",
        "url": (url or "")[:300],
        "kind": kind if kind in KINDS else KIND_PARSE,
        "status": status,
        "count": int(count or 0),
        "detail": (detail or "")[:200],
    }
    try:
        last = _RECORDS[-1] if _RECORDS else None
        if last and all(last.get(k) == rec[k] for k in ("at", "source", "url", "kind", "status")):
            return last
        _RECORDS.append(rec)
    except Exception:                                   # pragma: no cover
        return rec
    return rec


def summary(recs: list[dict] | None = None) -> dict:
    """按类型/来源汇总，并给出「需要人工介入」的来源清单。"""
    recs = _RECORDS if recs is None else recs
    by_kind: dict[str, int] = {k: 0 for k in KINDS}
    by_source: dict[str, dict[str, int]] = {}
    blocked: dict[str, str] = {}
    for r in recs:
        k = r.get("kind", KIND_PARSE)
        by_kind[k] = by_kind.get(k, 0) + 1
        s = r.get("source", "unknown")
        by_source.setdefault(s, {}).setdefault(k, 0)
        by_source[s][k] += 1
        if k in ATTENTION_KINDS:
            blocked.setdefault(s, k)
    return {
        "total": len(recs),
        "by_kind": by_kind,
        "by_source": by_source,
        "attention_sources": blocked,
        "ok": all(by_kind.get(k, 0) == 0 for k in ATTENTION_KINDS),
    }


def flush(path: Path | None = None) -> Path | None:
    """落盘（latest.json + latest.md）。失败只告警，返回 None。"""
    p = Path(path) if path else LEDGER_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        rep = {
            "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "summary": summary(),
            "records": records(),
        }
        p.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            p.with_suffix(".md").write_text(render_md(rep), encoding="utf-8")
        except Exception:
            pass
        return p
    except Exception as e:                              # pragma: no cover
        print(f"   ⚠️ 采集台账写入失败（不阻断主流程）: {e}")
        return None


def _kind_label(kind: str) -> str:
    return {
        KIND_OK: "正常",
        KIND_EMPTY: "真空数据",
        KIND_BLOCKED: "被风控拦截",
        KIND_CHALLENGE: "反爬挑战页",
        KIND_NETWORK: "网络故障",
        KIND_HTTP_ERROR: "HTTP 异常",
        KIND_PARSE: "解析失败",
    }.get(kind, kind)


def render_md(rep: dict) -> str:
    s = rep.get("summary") or {}
    L = ["# 采集结果分类台账", "",
         f"- 生成时间：{rep.get('generated_at', '')}",
         f"- 记录数：{s.get('total', 0)}",
         f"- 结论：**{'全部正常 ✅' if s.get('ok') else '存在需人工介入的失败 ❌'}**", "",
         "## 按类型", ""]
    for k in KINDS:
        n = (s.get("by_kind") or {}).get(k, 0)
        if n:
            L.append(f"- {_kind_label(k)}（`{k}`）：{n} 次")
    if s.get("attention_sources"):
        L += ["", "## 需要人工介入的来源", ""]
        for src, kind in s["attention_sources"].items():
            L.append(f"- **{src}** → {_kind_label(kind)}（`{kind}`）")
    L += ["", "## 明细", "",
          "| 时间 | 来源 | 类型 | 状态码 | 条数 | 说明 |",
          "|---|---|---|---|---|---|"]
    for r in rep.get("records", [])[:200]:
        L.append(f"| {r.get('at','')} | {r.get('source','')} | {_kind_label(r.get('kind',''))} "
                 f"| {r.get('status','') or '-'} | {r.get('count',0)} | {r.get('detail','')[:60]} |")
    L.append("")
    return "\n".join(L)
