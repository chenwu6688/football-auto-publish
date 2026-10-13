#!/usr/bin/env python3
"""信号饱和度体检（防「往无区分度信号上写规则」重演）。

背景（2026-10-13）：「搜索指数」的教训——`_search_from_suggest` = min(100, 百度联想命中数×10)，
而知名球队的百度联想恒 10 条且全含实体名，导致 72 个实体里 70 个恒为 100（97% 饱和）。
任何基于它的阈值都等于「给所有候选均匀加分」，不是信号而是噪声，却没有告警物能发现它。
本脚本把「某字段是否还有区分度」变成可执行验收项。

判据：
  - 零覆盖：n == 0                    —— 该字段在本信号表中从未有实体取到值（可能由其它模块
                                        运行时供给，如 source_watch 产 同题报道条数/来源媒体权重，
                                        故仅告警，不阻断）。
  - 饱和：n >= MIN_SAMPLE 且 众数占比 >= SATURATION_RATIO —— 有供给但无区分度。

硬项（不达标退出码 1）
  A. 每个**饱和**字段都不得影响规则打分——把该字段从信号里去掉后，`ranker.rule_score`
     的五维分必须逐维不变（行为级断言，不扫描源码，避免注释/docstring 误判）。
     —— 即：一旦某字段饱和，禁止把它接回维度加分；今天不饱和的字段不受约束。

告警项
  B. 列出所有饱和字段（含众数与占比）
  C. 列出所有零覆盖字段

用法：
  python3 scripts/verify_signal_saturation.py [--quiet]
产出：data/audit/signal_saturation_<date>.md + signal_saturation_latest.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ranker            # noqa: E402
import signal_sources    # noqa: E402

CST = timezone(timedelta(hours=8))
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
NOW = datetime(2026, 10, 13, 20, 0, tzinfo=CST)

# 判定参数（集中定义，便于调参）
MIN_SAMPLE = 10          # 少于 10 个有值实体不判饱和，避免小样本误报
SATURATION_RATIO = 0.9   # 众数占比 >= 90% 即视为无区分度


def _install_isolated_env():
    """rank 侧所有会落 data/ 的路径指到 tempfile，真实缓存零污染。"""
    ranker._awareness_cache = {}
    ranker.CACHE_PATH = Path(tempfile.mktemp(suffix=".json"))


def _distribution(table: dict) -> dict:
    """对每个已知字段统计 (有值实体数, 众数, 众数占比, 取值种类数)。"""
    out = {}
    for f in signal_sources.FIELDS:
        vals = [v[f] for v in table.values() if isinstance(v, dict) and f in v]
        if not vals:
            out[f] = {"n": 0, "mode": None, "ratio": 0.0, "distinct": 0}
            continue
        mode, cnt = Counter(vals).most_common(1)[0]
        out[f] = {"n": len(vals), "mode": mode, "ratio": cnt / len(vals),
                  "distinct": len(set(vals))}
    return out


def check_saturated_fields_inert(table: dict, saturated: list[str]) -> dict:
    """硬项 A：饱和字段不影响规则五维分（逐个字段行为断言）。"""
    if not saturated:
        return {"item": "饱和字段不影响规则打分", "ok": True, "advisory": False,
                "detail": ["当前无饱和字段，无事可验（判据 n>=%d 且 众数占比>=%.0f%%）✅"
                           % (MIN_SAMPLE, SATURATION_RATIO * 100)]}
    details, ok = [], True
    # 取任一命中实体做被试；同表同字段取值相同，选谁不影响结论
    for f in saturated:
        host = next((e for e, v in table.items()
                     if isinstance(v, dict) and f in v), None)
        if host is None:
            continue
        satval = table[host][f]
        card = {"id": "sat", "主体": host, "动作": "转会", "数值": None,
                "发生时间": "2026-10-13",
                "来源列表": [{"来源名": "zhibo8", "地址": "http://z"}],
                "可信度": "单源", "可用角度": ["角度1", "角度2", "角度3"],
                "生命周期": "进行中"}
        with_f = {"card": card}
        without = {"card": card}
        # 先 l2_enrich 补全基线（它会整体覆盖 _signals），再注入/保持被试字段
        ranker.l2_enrich([with_f, without], {})
        with_f["_signals"][f] = satval
        s1 = ranker.rule_score(card, with_f, NOW)
        s2 = ranker.rule_score(card, without, NOW)
        same = (s1 == s2)
        ok = ok and same
        details.append(
            f"{f}（饱和值 {satval}，主体 {host}）：带={s1} 不带={s2} "
            f"{'逐维一致 ✅' if same else '被该字段影响 ❌（饱和字段不得参与加分）'}")
    return {"item": "饱和字段不影响规则打分", "ok": bool(ok), "advisory": False,
            "detail": details}


def verify() -> dict:
    _install_isolated_env()
    table = signal_sources.load_external()
    if not isinstance(table, dict):
        table = {}
    dist = _distribution(table)
    saturated = [f for f, d in dist.items()
                 if d["n"] >= MIN_SAMPLE and d["ratio"] >= SATURATION_RATIO]
    zero = [f for f, d in dist.items() if d["n"] == 0]

    hard = [check_saturated_fields_inert(table, saturated)]

    advisory = []
    if saturated:
        advisory.append({"item": "饱和字段（无区分度）", "ok": False, "advisory": True,
                         "detail": [f"{f}：众数 {dist[f]['mode']} 占比 "
                                    f"{dist[f]['ratio']:.1%}（{dist[f]['n']} 个有值实体，"
                                    f"{dist[f]['distinct']} 种取值）" for f in saturated]})
    else:
        advisory.append({"item": "饱和字段（无区分度）", "ok": True, "advisory": True,
                         "detail": ["无✅"]})
    advisory.append({"item": "零覆盖字段（本表无供给）", "ok": not zero, "advisory": True,
                     "detail": ([f"{f}：信号表中 0 个实体有值（若由运行时模块供给则属正常，"
                                 f"如 source_watch 产 同题报道条数/来源媒体权重）" for f in zero]
                                or ["无✅"])})
    advisory.append({"item": "信号表规模", "ok": True, "advisory": True,
                     "detail": [f"实体 {len(table)} 个；字段分布 " +
                                "、".join(f"{f}={dist[f]['n']}" for f in signal_sources.FIELDS)]})
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": hard + advisory,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "saturated": saturated, "zero_coverage": zero,
            "distribution": dist}


def render_md(rep: dict) -> str:
    L = ["# 信号饱和度体检报告（防无效信号规则）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**",
         f"- 判定参数：有值实体数 ≥ {MIN_SAMPLE} 且众数占比 ≥ {SATURATION_RATIO:.0%} 视为饱和", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "（告警项）" if c.get("advisory") else "（硬项）"
        L.append(f"## {c['item']}{tag}：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="信号饱和度体检")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"signal_saturation_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "signal_saturation_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
