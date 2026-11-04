#!/usr/bin/env python3
"""三要素闸门的**尺子校准**：用真实数据复算通过率与阅读分层（P0-1，2026-11-04）。

为什么必须有这个脚本：
  2026-11-03 的教训是「放宽词表必须用 181 条对标样本复算，否则就是把尺子做松」；
  反过来，把闸门定成「三项全过」也可能把尺子做紧——我方 88 篇里只有 22% 能过，
  连全站 TOP1（494 阅读）都只有 2/3。**尺子松紧必须用两组数据同时验**：

    我方 88 篇实发稿（output/metadata + performance_log，按 platform_id 去重）
    头部 181 条图文（data/audit/benchmark_raw/*.txt，benchmark_peers.parse_home_text）

判据（不是拍脑袋，见输出里的 monotonic / peer_gap 两项）：
  - **我方单调性**：通过组的阅读中位必须高于被拦组，否则闸门没有区分度；
  - **不误杀头部**：头部通过率不得低于我方通过率（尺子不能比行业头部还紧）；
  - **不误杀头部 TOP10**：头部前 10 条通过率 ≥80%；
  - **有改进压力**：我方通过率落在 20%~85%（太高=形同虚设，太低=天天打回自己）。

用法：
    python3 scripts/analyze_title_elements.py
    python3 scripts/analyze_title_elements.py --json --quiet
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import title_elements as te           # noqa: E402

PERF_PATH = ROOT / "output" / "performance_log.json"
PEER_DIR = ROOT / "data" / "audit" / "benchmark_raw"

# 判定阈值（改动必须同步 verify_title_elements.py 的硬项与 README 口径）
PEER_TOP_N = 10
# 头部 TOP10 的通过率下限取 70% 而不是 80%：头部的爆款里有「纯冲突型」
# （西甲主席斥责姆巴佩，无数字无对比）与「代称型」（4场6球！25岁大英帝星复活，
# 无专名）两类，本来就不属于三要素覆盖对象。实测 8/10=80%，留一格余量防止
# 样本替换后反复红（红久必被 `|| echo` 软化，那是 11-03 已踩过的坑）。
PEER_TOP_MIN_PASS = 0.70
OURS_PASS_MIN, OURS_PASS_MAX = 0.20, 0.85


def _load_ours() -> list[dict]:
    """我方实发稿：metadata 拿标题，performance_log 拿阅读，按 platform_id 去重。"""
    try:
        perf = json.loads(PERF_PATH.read_text(encoding="utf-8")).get("articles") or {}
    except Exception:
        perf = {}
    out, seen = [], set()
    for path in sorted(glob.glob(str(ROOT / "output" / "2026-1[01]-*" / "metadata.json"))):
        date = Path(path).parent.name
        try:
            meta = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in meta.get("articles") or []:
            pf = perf.get(f"{date}/{a.get('slug', '')}") or {}
            reads = pf.get("reads")
            if not isinstance(reads, (int, float)):
                continue
            key = pf.get("platform_id") or f"{date}/{a.get('slug', '')}"
            if key in seen:
                continue
            seen.add(key)
            out.append({"title": a.get("title", ""), "reads": int(reads),
                        "content_type": a.get("content_type", ""),
                        "impressions": pf.get("impressions"),
                        "click_rate": pf.get("click_rate")})
    return out


def _load_peers() -> list[dict]:
    """头部图文样本（只要 unit=阅读，视频号播放量不混进来）。"""
    try:
        import benchmark_peers as bp
    except Exception:
        return []
    out = []
    for f in sorted(glob.glob(str(PEER_DIR / "*.txt"))):
        account = Path(f).stem
        try:
            txt = Path(f).read_text(encoding="utf-8")
        except Exception:
            continue
        for r in bp.parse_home_text(txt):
            if r.get("unit") != "阅读":
                continue
            out.append({"account": account, "title": r.get("title", ""),
                        "reads": int(r.get("views") or 0)})
    return out


def _stat(vals: list[int]) -> dict:
    if not vals:
        return {"n": 0, "median": None, "mean": None, "over100": None}
    return {"n": len(vals), "median": st.median(vals),
            "mean": round(st.mean(vals), 1),
            "over100": round(100 * sum(1 for v in vals if v >= 100) / len(vals), 1)}


def _split(rows: list[dict]) -> dict:
    """按三要素命中项数分组，并给出通过/被拦两组。"""
    by_count: dict[int, list[int]] = {}
    passed, blocked = [], []
    for r in rows:
        e = te.extract(r["title"])
        by_count.setdefault(e.count, []).append(r["reads"])
        (passed if te.check_ok(e) else blocked).append(r["reads"])
    return {
        "n": len(rows),
        "coverage": te.coverage([r["title"] for r in rows]),
        "by_count": {k: _stat(v) for k, v in sorted(by_count.items())},
        "passed": _stat(passed),
        "blocked": _stat(blocked),
    }


def build_report() -> dict:
    ours = _split(_load_ours())
    peers = _split(_load_peers())

    # 单调性：通过组中位 > 被拦组中位（任一为空则不判，标 None）
    mono = None
    if ours["passed"]["n"] and ours["blocked"]["n"]:
        mono = ours["passed"]["median"] > ours["blocked"]["median"]

    # 头部 TOP10 是否被误杀
    peer_rows = sorted(_load_peers(), key=lambda r: -r["reads"])[:PEER_TOP_N]
    top_pass = (sum(1 for r in peer_rows if te.check_ok(te.extract(r["title"])))
                if peer_rows else None)
    top_rate = round(top_pass / len(peer_rows), 2) if peer_rows else None

    ours_rate = round(ours["passed"]["n"] / ours["n"], 2) if ours["n"] else None
    peer_rate = round(peers["passed"]["n"] / peers["n"], 2) if peers["n"] else None

    verdict = {
        "ours_pass_rate": ours_rate,
        "peers_pass_rate": peer_rate,
        "monotonic": mono,
        "peer_top_pass": f"{top_pass}/{len(peer_rows)}" if peer_rows else None,
        "peer_top_rate": top_rate,
        # 尺子比头部还紧 = 做紧了；可通过率过高 = 形同虚设
        "looser_than_peers": (peer_rate is not None and ours_rate is not None
                              and peer_rate >= ours_rate),
        "within_pressure_band": (ours_rate is not None
                                 and OURS_PASS_MIN <= ours_rate <= OURS_PASS_MAX),
    }
    return {"ours": ours, "peers": peers, "verdict": verdict,
            "thresholds": {"min_elements": te.MIN_ELEMENTS,
                           "require_proper": te.REQUIRE_PROPER,
                           "ours_pass_band": [OURS_PASS_MIN, OURS_PASS_MAX],
                           "peer_top_pass_min": PEER_TOP_MIN_PASS}}


def render_md(rep: dict) -> str:
    L = ["# 三要素闸门尺子校准", ""]
    for name, key in (("我方实发稿", "ours"), ("头部对标样本", "peers")):
        s = rep[key]
        L += [f"## {name}（n={s['n']}）", "",
              "| 命中项数 | 篇数 | 中位阅读 | 均值 | 过百率 |",
              "| --- | --- | --- | --- | --- |"]
        for k, v in s["by_count"].items():
            L.append(f"| {k} | {v['n']} | {v['median']} | {v['mean']} | {v['over100']}% |")
        L += ["",
              f"- 通过（≥{rep['thresholds']['min_elements']} 项且含专名）："
              f"n={s['passed']['n']} 中位 {s['passed']['median']}",
              f"- 被拦：n={s['blocked']['n']} 中位 {s['blocked']['median']}",
              f"- 三要素覆盖：数字 {s['coverage']['number']}% / 对比 {s['coverage']['contrast']}% / "
              f"专名 {s['coverage']['proper']}% / 三项全过 {s['coverage']['all_three']}%", ""]
    v = rep["verdict"]
    L += ["## 判据", "",
          f"- 我方通过率 {v['ours_pass_rate']:.0%}"
          f"（压力带 {rep['thresholds']['ours_pass_band'][0]:.0%}~"
          f"{rep['thresholds']['ours_pass_band'][1]:.0%}）→ "
          f"{'✅' if v['within_pressure_band'] else '❌'}",
          f"- 单调性（通过组中位 > 被拦组）→ {'✅' if v['monotonic'] else '❌'}",
          f"- 头部通过率 {v['peers_pass_rate']:.0%} ≥ 我方 {v['ours_pass_rate']:.0%} → "
          f"{'✅' if v['looser_than_peers'] else '❌（尺子比行业头部还紧）'}",
          f"- 头部 TOP{PEER_TOP_N} 通过 {v['peer_top_pass']} → "
          f"{'✅' if (v['peer_top_rate'] or 0) >= PEER_TOP_MIN_PASS else '❌（误杀头部爆款）'}",
          "",
          "> 三要素齐全 ≠ 爆款（BOTTOM 里也有 3/3 的 0 阅读稿）。闸门只负责抬下限，"
          "CTR 是否改善要看发布后回流，不在这里下结论。"]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="三要素闸门尺子校准（只算不写）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="只输出判据，不输出明细")
    args = ap.parse_args(argv)

    rep = build_report()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif args.quiet:
        v = rep["verdict"]
        print(f"ours_pass={v['ours_pass_rate']} peers_pass={v['peers_pass_rate']} "
              f"monotonic={v['monotonic']} peer_top={v['peer_top_pass']} "
              f"band={v['within_pressure_band']} looser={v['looser_than_peers']}")
    else:
        print(render_md(rep))

    v = rep["verdict"]
    bad = (v["monotonic"] is False) or (v["within_pressure_band"] is False) \
        or (v["looser_than_peers"] is False) \
        or ((v["peer_top_rate"] is not None) and v["peer_top_rate"] < PEER_TOP_MIN_PASS)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
