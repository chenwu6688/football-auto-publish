#!/usr/bin/env python3
"""TLS 指纹 A/B 探针：challenge 到底是「指纹被识别」还是「IP 被风控」。

## 为什么需要这个探针

2026-11-04 的采集台账显示 CI（GitHub runner）75 次请求里 19 次 challenge（25%），
但**同一个 URL、同一套请求头、同一个库（requests）**，本机（国内 IP）连续 6 次
全部 200 / 615KB / 无 EdgeOne 特征。也就是说：

    头部组合、UA、URL 都不是变量，变的是 IP 与网络路径。

这直接决定 curl_cffi（TLS 指纹伪装）值不值得引入：如果海外机房段的风控是按
**IP 归属地/ASN** 判的，改 TLS 指纹一点用都没有，正确动作是降频/换源/换出口。
但**不能凭直觉断言**——必须同一个 runner、交替发请求、拿计数说话。

## 实验设计（交错，不是先后）

先跑 50 次 A 再跑 50 次 B 是错的：后一组承受更高的累计请求量，challenge 率高
可能只是「打得更久」。所以按 A→B→A→B 交替，两组在时间上均匀分布。

判据**预先写死**在下方的 VERDICT_* 常量里，看到数据后不许改：
  - fingerprint_effective：A 组比 B 组高 ≥20 个百分点、B ≤10%、Fisher p<0.05
  - not_fingerprint_*：两组都高（≥30%）→ 环境/IP 维度，换库无用
  - not_reproducible：两组都接近 0 → **该环境不复现**，不是「修好了」
  - inconclusive：差值和显著性都不够 → 加样本，别下结论

## 退出码

  0 = 测量有效完成，结论在 verdict 里
  1 = 参数/运行错误
  2 = 未判定（环境不复现 / 缺 curl_cffi / 样本不足）

用法：
    /usr/bin/python3 scripts/ab_fingerprint_probe.py --n 50
    /usr/bin/python3 scripts/ab_fingerprint_probe.py --n 10 --json data/audit/fp_probe.json

注意：本脚本**不写 scraper_diagnostics 的生产台账**（只调用它的纯判据函数）。
100 次探针请求混进生产采集统计，会把「采集成功率」这个指标污染掉。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper_diagnostics as diag  # noqa: E402  只复用判据，不写它的台账

# ---------------------------------------------------------------- 判据（预先写死）
DELTA_MIN = 0.20      # A 组 challenge 率至少比 B 组高这么多，才算「指纹有效」
B_MAX = 0.10          # B 组（curl_cffi）challenge 率上限
HIGH = 0.30           # 两组都 ≥ 这个值 → 不是指纹问题
ALPHA = 0.05          # 统计显著性

# 通道成本序：数字小的优先（同分时先试零成本的）
COST_RANK = {"urllib": 0, "curl_cffi": 1}
LOW = 0.05            # 两组都 ≤ 这个值 → 该环境不复现

DEFAULT_URL = "https://www.zhibo8.cc/"
DEFAULT_GAP = 1.2     # 两次请求之间的间隔（秒），两组共用同一节奏
BREAKER_N = 5         # 连续 N 次 403 → 熔断（保护站点，也保护自己不被封）

# 与 media_scraper._http_get 完全一致的头部组合：这是「生产真实条件」，
# 探针里改头就等于换了被测对象。
HEADERS_FULL = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}
# `_http_get_plain` 的最小头（只 UA）。懂球帝过 EdgeOne 就是靠它从 567 变回 200 的，
# 所以「全头 vs 最小头」本身就是第三个变量，值得和 TLS 指纹一起在同一台机器上对照。
HEADERS_PLAIN: dict[str, str] = {}

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


def build_headers(ua: str, mode: str = "full") -> dict:
    base = HEADERS_FULL if mode == "full" else HEADERS_PLAIN
    return dict(base, **{"User-Agent": ua})


# ---------------------------------------------------------------- 统计
def fisher_p(a_succ: int, a_n: int, b_succ: int, b_n: int) -> float:
    """2x2 Fisher 精确检验（双侧）。不引 scipy —— 一个探针不该拖依赖。

    a_succ/b_succ 是「被 challenge 的次数」，a_n/b_n 是各组样本数。
    """
    if a_n <= 0 or b_n <= 0:
        return 1.0
    total = a_n + b_n
    row1 = a_succ + b_succ
    col1 = a_n

    def prob(k: int) -> float:
        # 超几何分布：从 row1 个「挑战」中取出 k 个落进 A 组的概率
        return (math.comb(col1, k) * math.comb(total - col1, row1 - k)
                / math.comb(total, row1))

    lo, hi = max(0, row1 - (total - col1)), min(row1, col1)
    obs = prob(a_succ)
    # 浮点容差：极端分布下 prob 会小到 0，直接比会漏
    return min(1.0, sum(p for k in range(lo, hi + 1)
                        if (p := prob(k)) <= obs * (1 + 1e-9)))


def verdict(a: dict, others: dict | None) -> tuple[str, str, int]:
    """按预先写死的判据给结论。``a`` 是基线（requests，即生产现状）。

    返回 (verdict, 说明, 退出码)。
    """
    if not others or a["n"] == 0:
        return ("未判定", "只有基线通道有数据（缺 curl_cffi 或未启用），无法比较", 2)
    # 同分必须优先零成本通道：两条都能过时，「不装新依赖」本身就是优势。
    # 只看 challenge_rate 会按字典顺序挑到 curl_cffi，等于白引一个依赖。
    best_name, bg = min(others.items(),
                        key=lambda kv: (kv[1]["challenge_rate"], COST_RANK.get(kv[0], 9)))
    ar, br = a["challenge_rate"], bg["challenge_rate"]
    p = fisher_p(a["challenge"], a["n"], bg["challenge"], bg["n"])

    if ar <= LOW and all(g["challenge_rate"] <= LOW for g in others.values()):
        return ("该环境不复现",
                f"所有通道 challenge 率都 ≤{LOW:.0%}（基线 {ar:.0%}）——"
                f"这个网络位置本来就打不进去，**不能据此宣称「修好了」**。"
                f"要在真正出问题的那台机器（CI runner）上再测", 2)
    if ar - br >= DELTA_MIN and br <= B_MAX and p < ALPHA:
        cost = "零成本（换库即可，不装新依赖）" if best_name == "urllib" else \
               "需装一个依赖"
        return (f"最优通道 {best_name}",
                f"基线 requests {ar:.0%} → {best_name} {br:.0%}，"
                f"降 {ar-br:.0%}，Fisher p={p:.4f}；{cost}", 0)
    if ar >= HIGH and br >= HIGH:
        return ("不是请求栈问题",
                f"所有通道都 ≥{HIGH:.0%}（基线 {ar:.0%}，最优 {best_name} {br:.0%}，"
                f"p={p:.4f}）：换库无用，是 IP/ASN 或频率维度 → 转降频、换源、换出口", 0)
    if ar - br >= DELTA_MIN and p >= ALPHA:
        return ("证据不足",
                f"差值 {ar-br:+.0%} 看似不小，但 p={p:.4f} 未达显著（样本 "
                f"{a['n']}+{bg['n']}）→ 加样本，别先改代码", 2)
    return ("未判定",
            f"基线 {ar:.0%} / 最优 {best_name} {br:.0%}（差 {ar-br:+.0%}，p={p:.4f}）："
            f"既不显著也不够高，证据不足", 2)


# ---------------------------------------------------------------- 两条通道
def _classify(status: int, text: str) -> str:
    """与生产同一套判据：状态码优先，200 再看是不是挑战壳。"""
    kind = diag.classify_status(status)
    if kind == diag.KIND_OK and diag.looks_like_challenge(text):
        return diag.KIND_CHALLENGE
    return kind


def _resp_fingerprint(headers) -> dict:
    """记录响应侧的「谁在拦」线索。

    光有 challenge 计数只能知道「被拦了」，不能知道「被谁拦、是不是同一个节点」。
    如果 ok 与 challenge 的 Server/厂商头不同，就说明是边缘节点分流而不是指纹判定
    ——这两种成因的处置方式完全相反，所以必须留证。
    """
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    vendor = ""
    for key, tag in (("eo-log-uuid", "EdgeOne"), ("cf-ray", "Cloudflare"),
                     ("x-tt-logid", "ByteDance")):
        if key in h:
            vendor = tag
            break
    return {"srv": h.get("server", "")[:40], "vendor": vendor}


def probe_requests(url: str, ua: str, timeout: int, mode: str = "full") -> dict:
    import requests
    h = build_headers(ua, mode)
    try:
        r = requests.get(url, headers=h, timeout=timeout)
        return {"status": r.status_code, "bytes": len(r.content),
                "kind": _classify(r.status_code, r.text), "err": "",
                **_resp_fingerprint(r.headers)}
    except Exception as e:                       # noqa: BLE001
        return {"status": 0, "bytes": 0, "kind": diag.KIND_NETWORK,
                "err": type(e).__name__, "srv": "", "vendor": ""}


def probe_curl_cffi(url: str, ua: str, timeout: int, impersonate: str,
                    mode: str = "full") -> dict:
    from curl_cffi import requests as creq
    h = build_headers(ua, mode)
    try:
        r = creq.get(url, headers=h, timeout=timeout, impersonate=impersonate)
        body = r.content or b""
        text = body.decode("utf-8", "ignore")
        return {"status": r.status_code, "bytes": len(body),
                "kind": _classify(r.status_code, text), "err": "",
                **_resp_fingerprint(getattr(r, "headers", {}))}
    except Exception as e:                       # noqa: BLE001
        return {"status": 0, "bytes": 0, "kind": diag.KIND_NETWORK,
                "err": type(e).__name__, "srv": "", "vendor": ""}


def probe_urllib(url: str, ua: str, timeout: int, mode: str = "full") -> dict:
    """生产 ``_http_get_plain`` 用的就是 urllib —— 这是**零成本备选通道**。

    2026-11-04 本机实测（同一 IP、同一 URL、同一套最小头，只换库）：
    懂球帝 requests 4/4 被 567 拦，urllib 4/4 拿到 310KB 正文。
    既然换库就能改结果，它必须进同一次对照，否则会漏掉「不加依赖也能修好」这条路。
    """
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers=build_headers(ua, mode))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            text = raw.decode("utf-8", "ignore")
            return {"status": 200, "bytes": len(raw),
                    "kind": _classify(200, text), "err": "",
                    **_resp_fingerprint(r.headers)}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "bytes": 0,
                "kind": _classify(e.code, ""), "err": "",
                "srv": "", "vendor": ""}
    except Exception as e:                       # noqa: BLE001
        return {"status": 0, "bytes": 0, "kind": diag.KIND_NETWORK,
                "err": type(e).__name__, "srv": "", "vendor": ""}


# ---------------------------------------------------------------- 主流程
def run(url: str, n: int, gap: float, timeout: int,
        impersonate: str, channels: list[str], mode: str = "full") -> dict:
    """三通道交错发请求。

    交错而不是先后，是为了让各通道的「累计请求量/时间窗」一致——
    否则后跑的那条通道会被前面的请求打高 challenge 率，看起来像它更差。
    """
    groups: dict[str, list[dict]] = {c: [] for c in channels}
    streak_403 = 0
    t0 = time.time()
    for i in range(n):
        ua = UAS[i % len(UAS)]
        for name in channels:
            if name == "requests":
                rec = probe_requests(url, ua, timeout, mode)
            elif name == "curl_cffi":
                rec = probe_curl_cffi(url, ua, timeout, impersonate, mode)
            else:
                rec = probe_urllib(url, ua, timeout, mode)
            rec["i"] = i
            rec["at"] = datetime.now().strftime("%H:%M:%S")
            groups[name].append(rec)
            streak_403 = streak_403 + 1 if rec["status"] == 403 else 0
            if streak_403 >= BREAKER_N:
                print(f"   ⛔ 连续 {BREAKER_N} 次 403，熔断（保护站点）", flush=True)
                return _summarize(groups, url, n, impersonate, aborted=True, mode=mode)
        if i < n - 1:
            time.sleep(gap)
    out = _summarize(groups, url, n, impersonate, aborted=False, mode=mode)
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out


def _summarize(groups, url, n, impersonate, aborted: bool, mode: str = "full") -> dict:
    out = {"url": url, "planned_n": n, "impersonate": impersonate,
           "headers_mode": mode, "aborted": aborted, "groups": {},
           "at": datetime.now().isoformat(timespec="seconds")}
    for name, recs in groups.items():
        if not recs:
            continue
        kinds: dict[str, int] = {}
        for r in recs:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        ch = kinds.get(diag.KIND_CHALLENGE, 0)

        def _dist(sel) -> dict:
            """按「Server/厂商头」聚合同一类结果的来源节点。"""
            c: dict[str, int] = {}
            for r in recs:
                if sel(r):
                    key = (r.get("srv") or "-") + (
                        "/" + r["vendor"] if r.get("vendor") else "")
                    c[key] = c.get(key, 0) + 1
            return c

        out["groups"][name] = {
            "n": len(recs), "kinds": kinds, "challenge": ch,
            "challenge_rate": round(ch / len(recs), 4),
            "ok_rate": round(kinds.get(diag.KIND_OK, 0) / len(recs), 4),
            "bytes_median": _median([r["bytes"] for r in recs if r["bytes"]]),
            # 时间序列：challenge 是否集中在后段（频率风控的特征）
            "challenge_at_index": [r["i"] for r in recs if r["kind"] == diag.KIND_CHALLENGE],
            "ok_server": _dist(lambda r: r["kind"] == diag.KIND_OK),
            "challenge_server": _dist(lambda r: r["kind"] == diag.KIND_CHALLENGE),
            "errors": sorted({r["err"] for r in recs if r["err"]}),
        }
    a = out["groups"].get("requests", {"n": 0, "challenge": 0, "challenge_rate": 0.0})
    others = {k: v for k, v in out["groups"].items()
              if k != "requests" and v.get("n", 0) > 0}
    v, why, rc = verdict(a, others)
    out["verdict"], out["explain"], out["exit_code"] = v, why, rc
    return out


def _median(xs: list[int]) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    return s[len(s) // 2]


def _has_cffi() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:            # noqa: BLE001
        return False


def render(out: dict) -> str:
    lines = [f"TLS 指纹 A/B 探针 — {out['url']}",
             f"  头部 {out.get('headers_mode', 'full')}，计划每组 {out['planned_n']} 次"
             + ("（熔断中止）" if out.get("aborted") else "")
             + (f"，耗时 {out['elapsed_s']}s" if out.get("elapsed_s") else "")]
    for name, g in out["groups"].items():
        label = {"requests": "A·requests（现状）",
                 "curl_cffi": f"B·curl_cffi({out['impersonate']})",
                 "urllib": "C·urllib（零成本）"}.get(name, name)
        lines.append(f"  {label:<22} n={g['n']:<3} challenge {g['challenge']}/{g['n']}"
                     f" = {g['challenge_rate']:.0%}   ok {g['ok_rate']:.0%}"
                     f"   中位字节 {g['bytes_median']:,}")
        if g["challenge_at_index"]:
            lines.append(f"      被挑战的序号: {g['challenge_at_index'][:12]}"
                         + ("…" if len(g["challenge_at_index"]) > 12 else ""))
        ok_s, ch_s = g.get("ok_server") or {}, g.get("challenge_server") or {}
        if ch_s:
            lines.append(f"      ok 来自节点: {ok_s or '—'}")
            lines.append(f"      被拦来自节点: {ch_s}"
                         + ("   ← 同一节点内也被拦 ⇒ 更像频率/指纹"
                            if not ok_s or set(ok_s) & set(ch_s)
                            else "   ← 节点不同 ⇒ 边缘分流，不是指纹"))
        if g["errors"]:
            lines.append(f"      异常类型: {g['errors']}")
    lines.append(f"  ⇒ 结论：{out['verdict']}")
    lines.append(f"     {out['explain']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--n", type=int, default=50, help="每组请求次数")
    ap.add_argument("--gap", type=float, default=DEFAULT_GAP, help="两次请求间隔（秒）")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--impersonate", default="chrome131")
    ap.add_argument("--headers", dest="mode", choices=["full", "plain"], default="full",
                    help="full=_http_get 那套（直播吧）／plain=最小头（懂球帝 EdgeOne 通道）")
    ap.add_argument("--no-cffi", action="store_true",
                    help="跳过 curl_cffi 通道（仍对照 requests vs urllib）")
    ap.add_argument("--json", default="", help="结果落盘路径")
    args = ap.parse_args()

    if args.n < 5:
        print("❌ n < 5，样本太小无法判断")
        return 1
    with_cffi = (not args.no_cffi) and _has_cffi()
    channels = ["requests"] + (["curl_cffi"] if with_cffi else []) + ["urllib"]
    if not args.no_cffi and not with_cffi:
        print("⚠️ 未安装 curl_cffi → 跳过该通道（requests vs urllib 仍然有意义）")

    print(f"开始探测 {args.url}（每组 {args.n} 次，间隔 {args.gap}s，"
          f"头部 {args.mode}，通道: {' + '.join(channels)}）", flush=True)
    out = run(args.url, args.n, args.gap, args.timeout, args.impersonate,
              channels, args.mode)
    print(render(out), flush=True)

    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {p}")
    return out["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
