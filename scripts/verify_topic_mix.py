#!/usr/bin/env python3
"""选题类型配比验收（2026-11-16 改动点 3）。

## 背景

`config/ratio_config.yaml` 从 2026-10-13 起就写着「战报比分 10%（R11：CTR 0.49%、
0 爆款）」，`ratio_tuner.py` 也一直按它出建议 —— 但 14 天实测：

    战报比分 30%（目标 10%）｜未分类 45%（目标 15%）｜转会官宣 8%（目标 30%）

**目标表有、调参器有、运行期没有**。本脚本把「配比」变成可验收项，
并验一遍 `mix_guard` 真的接在出口上（不是只写了个模块没人调）。

硬项（不达标退出码 1；只在 ``MIX_SINCE`` 之后的样本上判）：

  A. 战报比分 14 天滚动占比 ≤15%（目标 10%，留 5pt 缓冲区避免天天红）；
  B. 转会官宣 14 天滚动占比 ≥20%（目标 30%，同样留缓冲）；
  C. 接线（AST）—— `save_articles_local` 函数体内必须出现 `mix_guard.check`，
     且 metadata 组装处必须写 `mix_overshoot`。模块写好了但没人调，
     与「参数没有生效路径」是同一个病。
  D. 负向自检 —— 合成 20 篇战报台账，`snapshot` 必须报战报超配；换成 20 篇官宣，
     战报必须**不**在超配列表里（证明指标真的在动，不是恒真）。

告警项：生效日前的窗口只做基线留证（不判红），以及「冲突舆情 0 篇」这类供给缺口。

生效日 ``MIX_SINCE``：改动点 3 于 2026-11-16 落地。之前的配比是**没人管的**存量，
单列留证、不计判据 —— 不靠删样本让指标变绿，也不让历史样本把新链路永远判红。

用法：python3 scripts/verify_topic_mix.py [--days 14] [--quiet]
产出：data/audit/topic_mix_<date>.md + topic_mix_latest.json
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import mix_guard  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

MIX_SINCE = "2026-11-16"

# 硬项阈值（目标来自 ratio_config：战报 10 / 转会 30；这里读的是「验收线」，
# 留缓冲是为了不让日常波动把 CI 刷成常红——常红的判据等于没有判据）
SCORELINE_MAX = 15.0
TRANSFER_MIN = 20.0


# ---------------------------------------------------------------- 窗口内逐日台账
def daily_counts(days: int, end: str | None = None) -> dict:
    """{日期: {类别: 篇数}}，只读日级聚合台账（`publish_ledger_<date>.json`）。"""
    try:
        e = datetime.strptime(end, "%Y-%m-%d").date() if end else datetime.now(CST).date()
    except Exception:
        e = datetime.now(CST).date()
    lo = (e - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    hi = e.strftime("%Y-%m-%d")
    out: dict = {}
    if not AUDIT_DIR.exists():
        return out
    import topic_category as tc
    for p in sorted(AUDIT_DIR.glob("publish_ledger_*.json")):
        day = p.stem.replace("publish_ledger_", "")
        if len(day) != 10 or not (lo <= day <= hi):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        bucket = out.setdefault(day, {})
        for r in (data.get("records") or []):
            title = str((r or {}).get("title") or "").strip()
            if not title:
                continue
            c = tc.classify({"title": title})
            bucket[c] = bucket.get(c, 0) + 1
    return out


def _ratio(counts: dict) -> tuple[int, float | None]:
    n = sum(counts.values())
    if not n:
        return 0, None
    return n, 100.0 * counts.get("战报比分", 0) / n


# ---------------------------------------------------------------- 接线（AST）
def check_wiring() -> dict:
    path = PROJECT_ROOT / "orchestrator.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": None, "detail": [f"orchestrator.py 解析失败（{type(e).__name__}），未判定"]}
    found_call, found_write = False, False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "save_articles_local":
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "check" and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "mix_guard"):
                found_call = True
            # metadata 里写 mix_overshoot：查 Subscript 赋值 / dict 键
            if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant) \
                    and sub.slice.value == "mix_overshoot":
                found_write = True
            if isinstance(sub, ast.Constant) and sub.value == "mix_overshoot":
                found_write = True
    detail = [f"{'✅' if found_call else '❌'} save_articles_local 内调用 mix_guard.check",
              f"{'✅' if found_write else '❌'} 出口写 metadata.mix_overshoot"]
    return {"ok": found_call and found_write, "detail": detail}


# ---------------------------------------------------------------- 负向自检
def negative_selftest() -> dict:
    """合成台账：20 篇战报必须报超配；20 篇官宣则战报不进超配列表。"""
    tmp = Path(tempfile.mkdtemp(prefix="mix_verify_"))
    old = mix_guard.AUDIT_DIR
    try:
        mix_guard.AUDIT_DIR = tmp
        day = "2026-11-16"

        def write(titles):
            (tmp / f"publish_ledger_{day}.json").write_text(json.dumps(
                {"date": day, "records": [{"title": t, "status": "published"} for t in titles]},
                ensure_ascii=False), encoding="utf-8")

        write([f"曼联4-{i}-0维拉，多古补时救主" for i in range(20)])
        hot = mix_guard.snapshot(date_str=day, window_days=1)
        ok_hot = (hot["ok"] is False and
                  any(o["type"] == "战报比分" for o in hot["overshoot"]))

        write([f"皇马官宣签下第{i}名新援，五年合同锁死" for i in range(20)])
        cold = mix_guard.snapshot(date_str=day, window_days=1)
        ok_cold = not any(o["type"] == "战报比分" for o in cold["overshoot"])

        # 配置读不到 → 未判定，不是「通过」
        _orig = mix_guard.load_targets
        mix_guard.load_targets = lambda *a, **k: None
        try:
            none_rep = mix_guard.snapshot(date_str=day, window_days=1)
        finally:
            mix_guard.load_targets = _orig
        ok_none = none_rep["ok"] is None
    except Exception as e:
        return {"ok": None, "detail": [f"负向自检异常（{type(e).__name__}: {e}），未判定"]}
    finally:
        mix_guard.AUDIT_DIR = old
    return {"ok": bool(ok_hot and ok_cold and ok_none),
            "detail": [f"{'✅' if ok_hot else '❌'} 20 篇战报 → 报超配",
                       f"{'✅' if ok_cold else '❌'} 20 篇官宣 → 战报不进超配（指标会动）",
                       f"{'✅' if ok_none else '❌'} 配置读不到 → 未判定（不是通过）"]}


# ---------------------------------------------------------------- 主流程
def verify(days: int = 14, end: str | None = None) -> dict:
    per_day = daily_counts(days, end)
    new = {d: c for d, c in per_day.items() if d >= MIX_SINCE}
    old = {d: c for d, c in per_day.items() if d < MIX_SINCE}
    rep = {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
           "days": days, "since": MIX_SINCE, "checks": []}
    checks = rep["checks"]

    def add(item, ok, detail, advisory=False):
        checks.append({"item": item, "ok": ok, "detail": detail, "advisory": advisory})

    def _tot(dct):
        t: dict = {}
        for c in dct.values():
            for k, v in c.items():
                t[k] = t.get(k, 0) + v
        return t

    if not new:
        add("A 战报比分占比 ≤15%", None,
            [f"生效日 {MIX_SINCE} 之后窗口内 0 篇台账记录 → 样本不足，未判定（不等于通过）"])
        add("B 转会官宣占比 ≥20%", None, ["同上：无生效日后样本"])
    elif sum(sum(c.values()) for c in new.values()) < mix_guard.MIN_SAMPLE:
        # 生效日当天只有 1~2 个批次，6 篇的分母上一篇战报就翻转 17pt —— 那是噪声。
        # 样本门槛与 mix_guard 共用同一个常量，避免「两处各写一个 20」。
        n = sum(sum(c.values()) for c in new.values())
        add("A 战报比分占比 ≤15%", None,
            [f"生效日后仅 {n} 篇 < {mix_guard.MIN_SAMPLE} → 样本不足，未判定（不等于通过）"])
        add("B 转会官宣占比 ≥20%", None, [f"同上：样本 {n} 篇不足"])
    else:
        t = _tot(new)
        n = sum(t.values())
        sc = 100.0 * t.get("战报比分", 0) / n
        tr = 100.0 * t.get("转会官宣", 0) / n
        add(f"A 战报比分占比 ≤{SCORELINE_MAX:.0f}%", sc <= SCORELINE_MAX,
            [f"{t.get('战报比分', 0)}/{n} = {sc:.1f}%（目标 10%，验收线 {SCORELINE_MAX:.0f}%）"])
        add(f"B 转会官宣占比 ≥{TRANSFER_MIN:.0f}%", tr >= TRANSFER_MIN,
            [f"{t.get('转会官宣', 0)}/{n} = {tr:.1f}%（目标 30%，验收线 {TRANSFER_MIN:.0f}%）"])

    if old:
        t = _tot(old)
        n = sum(t.values())
        add("生效日前基线（留证，不判红）", None,
            [f"{n} 篇：" + "、".join(f"{k} {v}（{100 * v / n:.0f}%）"
                                     for k, v in sorted(t.items(), key=lambda kv: -kv[1])),
             f"战报 {100 * t.get('战报比分', 0) / n:.1f}% vs 目标 10%；"
             f"转会 {100 * t.get('转会官宣', 0) / n:.1f}% vs 目标 30%"], advisory=True)

    w = check_wiring()
    add("C 接线（AST）", w["ok"], w["detail"])
    nt = negative_selftest()
    add("D 负向自检", nt["ok"], nt["detail"])

    rep["hard_failed"] = [c["item"] for c in checks if c["ok"] is False]
    rep["undetermined"] = [c["item"] for c in checks if c["ok"] is None and not c["advisory"]]
    rep["ok"] = not rep["hard_failed"]
    return rep


def render_md(rep: dict) -> str:
    verdict = "通过 ✅" if rep["ok"] else "未通过 ❌"
    if rep["ok"] and rep.get("undetermined"):
        verdict = f"部分通过 ⏸（{len(rep['undetermined'])} 项样本不足未判定，不等于通过）"
    L = ["# 选题类型配比验收（2026-11-16 改动点 3）", "",
         f"- 生成时间：{rep['generated_at']}｜窗口 {rep['days']} 天｜生效日 {rep['since']}",
         f"- 验收结论：**{verdict}**",
         "- 基线（11-02~11-15）：战报比分 30%（目标 10）、未分类 45%（目标 15）、"
         "转会官宣 8%（目标 30）、冲突舆情 0%（目标 20）", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    if rep.get("undetermined"):
        L += [f"> 未判定（样本不足，不等于通过）：{'、'.join(rep['undetermined'])}", ""]
    for c in rep["checks"]:
        mark = {True: "✅", False: "❌", None: "⏸"}[c["ok"]]
        L.append(f"## {c['item']}：{mark}" + ("（告警项）" if c.get("advisory") else ""))
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="选题类型配比验收")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--end", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    rep = verify(args.days, args.end)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"topic_mix_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "topic_mix_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    for item in rep.get("undetermined") or []:
        print(f"::warning::配比验收「{item}」样本不足，本次未判定（不等于通过）")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
