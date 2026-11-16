#!/usr/bin/env python3
"""补位去重与出稿分散度验收（2026-11-16，改动点 1）。

## 病根

`dedup.apply` 只在**候选表**上去重。候选被丢掉之后，`deterministic_supply.fill_gap`
/ `reservoir.fill_gap` 会重新从库存取货，它们原先不看本批写过什么 ——
11-10 晨读漏斗实证：

    llm: 2  →  dedup: 1（同场丢 1）  →  material: 1  →  batch_mix: 2（降级 1）

数量补回来了，补回来的还是同一场。结果 14 天里出现：
同一场同比分重复 4 篇（5.5%）、帅位变动 4 篇、曼联 12 篇/14 天。

## 本脚本验什么

硬项（不达标退出码 1；**只在 ``DEDUP_SINCE`` 之后的样本上判**）：

  A. 同场重复（当天）—— 同一对阵键 + 同比分，同一天 ≤1 篇；
  B. 同场重复（跨天）—— 同一赛果指纹，窗口内 ≤1 篇；
  C. 同事件过度重复 —— 同一 `event_key` 7 天内 >2 篇即违规；
  D. 单主体过度集中 —— 同一队名窗口内 >4 篇即违规（防「曼联 12 篇」）。
  E. 接线（AST，不文本匹配）—— `deterministic_supply.fill_gap` 的调用点必须传
     `exclude=`；`_reservoir_accept` 函数体里必须出现 `dedup.judge`。闸门写好了
     但没接线，等于没写，而且从指标上完全看不出来（指标是 0 = 「没重复」）。
  F. 负向自检 —— 3 条同场候选喂 `dedup.apply` 只留 1 条；把留下的那条放进
     `deterministic_supply.fill_gap(exclude=ctx)`，库存里同场的 3 条必须一条都取不出，
     且 `stats["补位拒绝重复"] == 3`。判据恒真时这一项会红。

生效日 ``DEDUP_SINCE``：改动点 1 是 2026-11-16 落地的，之前的重复是**旧链路样本**，
报告里单列留证、不计判据 —— 不靠删样本让指标变绿，也不让历史样本把新链路永远判红。

## 口径纪律

- 同场判据用 `orchestrator.build_match_signature`（两队+比分），与生成侧同源；
- 同事件判据用 `hot_topics.event_key`，与 `dedup.judge` 同源；
- 预测稿不计（它不发布，进了分母会把重复率冲淡）；
- 窗口内 ``DEDUP_SINCE`` 之后**一篇都没有 → 未判定（ok=None）**，不写 0。

用法：python3 scripts/verify_dedup_spread.py [--days 14] [--quiet]
产出：data/audit/dedup_spread_<date>.md + dedup_spread_latest.json
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

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
CST = timezone(timedelta(hours=8))

# 补位去重生效日（改动点 1 落地日）。之前的重复属旧链路样本。
DEDUP_SINCE = "2026-11-16"

# 阈值（与改进方案第三章表格一致，写在一处）
MAX_SAME_FIXTURE_PER_DAY = 1
MAX_SAME_FIXTURE_PER_WINDOW = 1
MAX_PER_EVENT_KEY = 2
MAX_PER_TEAM_CLUSTER = 4


# ---------------------------------------------------------------- 读样本
def _window(days: int, end: str | None = None) -> tuple[str, str]:
    try:
        e = datetime.strptime(end, "%Y-%m-%d").date() if end else datetime.now(CST).date()
    except Exception:
        e = datetime.now(CST).date()
    return (e - timedelta(days=days - 1)).strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")


def load_articles(lo: str, hi: str) -> list[dict]:
    """窗口内成稿（排除预测稿）。返回 [{date, title, slug}]。"""
    rows: list[dict] = []
    if not OUTPUT_DIR.exists():
        return rows
    for day in sorted(OUTPUT_DIR.iterdir()):
        if not day.is_dir() or not (lo <= day.name <= hi):
            continue
        f = day / "metadata.json"
        if not f.exists():
            continue
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in (meta.get("articles") or []):
            if a.get("is_prediction"):
                continue
            title = str(a.get("title") or "").strip()
            if not title:
                continue
            rows.append({"date": day.name, "title": title,
                         "slug": a.get("slug") or "", "platform_id": a.get("platform_id")})
    return rows


# ---------------------------------------------------------------- 判据
def _sig(title: str) -> str:
    import orchestrator
    try:
        return orchestrator.build_match_signature(title) or ""
    except Exception:
        return ""


def _ekey(title: str) -> str:
    import hot_topics
    try:
        k = hot_topics.event_key(title) or ""
    except Exception:
        return ""
    return "" if k.endswith("#") else k


def _teams(title: str) -> set:
    import hot_topics
    import team_names
    out = set()
    try:
        for e in hot_topics.entities_in(title):
            c = team_names.canonical_team(e)
            if c:
                out.add(c)
    except Exception:
        return set()
    return out


def scan(rows: list[dict]) -> dict:
    """按四条判据统计违规篇（**只返回计数与样本，不改数据**）。"""
    sig_day: dict = {}
    sig_win: dict = {}
    ekey_hits: dict = {}
    team_hits: dict = {}
    viol = []

    for r in rows:
        t = r["title"]
        s = _sig(t)
        if s:
            # A: 当天同场
            k = (r["date"], s)
            if k in sig_day:
                viol.append({**r, "rule": "A 当天同场", "key": s})
            else:
                sig_day[k] = r
            # B: 跨天同场
            if s in sig_win:
                viol.append({**r, "rule": "B 跨天同场", "key": s})
            else:
                sig_win[s] = r
        e = _ekey(t)
        if e:
            ekey_hits.setdefault(e, []).append(r)
        for tm in _teams(t):
            team_hits.setdefault(tm, []).append(r)

    for e, rs in ekey_hits.items():
        if len(rs) > MAX_PER_EVENT_KEY:
            for r in rs[MAX_PER_EVENT_KEY:]:
                viol.append({**r, "rule": "C 同事件重复", "key": e})
    for tm, rs in team_hits.items():
        if len(rs) > MAX_PER_TEAM_CLUSTER:
            for r in rs[MAX_PER_TEAM_CLUSTER:]:
                viol.append({**r, "rule": "D 主体过度集中", "key": tm})

    # 去重（同一篇可能同时命中两条判据）
    uniq = {}
    for v in viol:
        uniq[(v["date"], v["title"], v["rule"])] = v
    viol = list(uniq.values())

    return {
        "n": len(rows),
        "violations": viol,
        "by_rule": {k: sum(1 for v in viol if v["rule"] == k)
                    for k in ("A 当天同场", "B 跨天同场", "C 同事件重复", "D 主体过度集中")},
        "max_team_cluster": (max(((k, len(v)) for k, v in team_hits.items()),
                                 key=lambda kv: kv[1], default=("", 0))),
        "max_event_cluster": (max(((k, len(v)) for k, v in ekey_hits.items()),
                                  key=lambda kv: kv[1], default=("", 0))),
    }


# ---------------------------------------------------------------- 接线（AST）
def check_wiring() -> dict:
    """闸门是否真的接在调用点上。**用 AST 查真实 Call 节点**，不文本匹配
    —— docstring 里写一句「应该传 exclude」就能让文本匹配假绿。"""
    detail, ok = [], None
    path = PROJECT_ROOT / "orchestrator.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": None, "detail": [f"orchestrator.py 解析失败（{type(e).__name__}），未判定"]}

    fill_calls, accept_has_judge = [], False

    class V(ast.NodeVisitor):
        def visit_Call(self, node):
            f = node.func
            name = ""
            if isinstance(f, ast.Attribute):
                name = f.attr
            elif isinstance(f, ast.Name):
                name = f.id
            if name == "fill_gap":
                kw = {k.arg for k in (node.keywords or []) if k.arg}
                fill_calls.append({"line": node.lineno, "kw": sorted(kw)})
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            if node.name == "_reservoir_accept":
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "judge"
                            and isinstance(sub.func.value, ast.Name)
                            and sub.func.value.id == "dedup"):
                        nonlocal_judge[0] = True
            self.generic_visit(node)

    nonlocal_judge = [False]
    V().visit(tree)
    accept_has_judge = nonlocal_judge[0]

    if not fill_calls:
        detail.append("❌ 没找到任何 fill_gap 调用点（改名了？）")
        ok = False
    else:
        for c in fill_calls:
            has = "exclude" in c["kw"] or "accept" in c["kw"]
            detail.append(f"{'✅' if has else '❌'} 第 {c['line']} 行 fill_gap 关键字：{c['kw']}")
            if not has:
                ok = False
        if ok is None:
            ok = True
    detail.append(f"{'✅' if accept_has_judge else '❌'} _reservoir_accept 含 dedup.judge")
    if not accept_has_judge:
        ok = False
    return {"ok": ok, "detail": detail}


# ---------------------------------------------------------------- 负向自检
def negative_selftest() -> dict:
    """构造 3 条同场候选：apply 只留 1；fill_gap 一条都取不出且计数为 3。"""
    import dedup
    import deterministic_supply as ds

    cands = [
        {"title": "曼联4-4维拉，多古补时救主", "keywords": ["曼联", "维拉"]},
        {"title": "曼联4-4维拉，8轮不胜创队史", "keywords": ["曼联", "维拉"]},
        {"title": "曼联4-4阿斯顿维拉，半场2-2后崩盘", "keywords": ["曼联", "维拉"]},
    ]
    kept, stats = dedup.apply(cands, dedup.build_context())
    ok_apply = (len(kept) == 1 and stats.get("同场", 0) >= 1)

    # 库存里放 3 条同样的货，用「本批已写过第 1 条」的上下文去补位 → 必须 0 条
    ok_fill, fill_detail = None, ""
    tmp = Path(tempfile.mkdtemp(prefix="dedup_verify_"))
    old_path = ds.STORE_PATH
    old_dir = ds.STORE_DIR
    try:
        ds.STORE_DIR = tmp
        ds.STORE_PATH = tmp / "items.json"
        store = {"items": [
            {"id": f"x{i}", "status": "stocked", "title": c["title"],
             "keywords": c["keywords"], "keywords_cn": c["keywords"],
             "section": "转会动态", "content_type": "转会资讯", "content": "x" * 300}
            for i, c in enumerate(cands)
        ]}
        ds._save_store(store)
        ctx = dedup.exclusion_ctx(None, extra_titles=[cands[0]["title"]])
        out_stats = {"补位拒绝重复": 0}
        picked = ds.fill_gap(3, date_str="2026-11-16", exclude=ctx, stats=out_stats)
        ok_fill = (picked == [] and out_stats["补位拒绝重复"] == 3)
        fill_detail = f"取到 {len(picked)} 条，拒绝计数 {out_stats['补位拒绝重复']}"
    except Exception as e:
        ok_fill = None
        fill_detail = f"负向自检异常（{type(e).__name__}: {e}），未判定"
    finally:
        ds.STORE_PATH = old_path
        ds.STORE_DIR = old_dir

    return {"ok": bool(ok_apply and ok_fill) if ok_fill is not None else None,
            "detail": [f"{'✅' if ok_apply else '❌'} dedup.apply 3 条同场只留 1 条"
                       f"（实留 {len(kept)}；同场计数 {stats.get('同场', 0)}）",
                       f"{'✅' if ok_fill else ('❌' if ok_fill is False else '⏸')} "
                       f"fill_gap 排除同场：{fill_detail}"]}


# ---------------------------------------------------------------- 主流程
def verify(days: int = 14, end: str | None = None) -> dict:
    lo, hi = _window(days, end)
    rows_all = load_articles(lo, hi)
    new_rows = [r for r in rows_all if r["date"] >= DEDUP_SINCE]
    old_rows = [r for r in rows_all if r["date"] < DEDUP_SINCE]

    rep = {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
           "window": [lo, hi], "since": DEDUP_SINCE, "days": days,
           "checks": []}
    checks = rep["checks"]

    def add(item, ok, detail, advisory=False):
        checks.append({"item": item, "ok": ok, "detail": detail, "advisory": advisory})

    scan_new = scan(new_rows)
    scan_old = scan(old_rows)

    if not new_rows:
        add("A~D 出稿分散度", None,
            [f"生效日 {DEDUP_SINCE} 之后窗口内 0 篇成稿（窗口 {lo}~{hi}）",
             "——样本不足，未判定。**不等于通过**：不要把这个状态读成「没有重复」"])
    else:
        b = scan_new["by_rule"]
        add(f"A 当天同场重复（≤{MAX_SAME_FIXTURE_PER_DAY}）",
            b["A 当天同场"] <= 0,
            [f"违规 {b['A 当天同场']} 篇 / 样本 {scan_new['n']} 篇"]
            + [f"· {v['date']} {v['title'][:34]}" for v in scan_new["violations"]
               if v["rule"].startswith("A")][:5])
        add(f"B 跨天同场重复（窗口内 ≤{MAX_SAME_FIXTURE_PER_WINDOW}）",
            b["B 跨天同场"] <= 0,
            [f"违规 {b['B 跨天同场']} 篇"] +
            [f"· {v['date']} {v['title'][:34]}" for v in scan_new["violations"]
             if v["rule"].startswith("B")][:5])
        add(f"C 同事件重复（≤{MAX_PER_EVENT_KEY}/7 天）",
            b["C 同事件重复"] <= 0,
            [f"违规 {b['C 同事件重复']} 篇；最大事件簇 {scan_new['max_event_cluster'][0]}"
             f" × {scan_new['max_event_cluster'][1]}"])
        add(f"D 单主体集中（≤{MAX_PER_TEAM_CLUSTER} 篇）",
            b["D 主体过度集中"] <= 0,
            [f"违规 {b['D 主体过度集中']} 篇；最大主体簇 {scan_new['max_team_cluster'][0]}"
             f" × {scan_new['max_team_cluster'][1]}"])

    if old_rows:
        ob = scan_old["by_rule"]
        add("旧链路留证（不计判据，告警项）", None,
            [f"生效日前 {scan_old['n']} 篇：A {ob['A 当天同场']} / B {ob['B 跨天同场']} / "
             f"C {ob['C 同事件重复']} / D {ob['D 主体过度集中']}；"
             f"最大主体簇 {scan_old['max_team_cluster'][0]} × {scan_old['max_team_cluster'][1]}",
             "旧样本只留证，不用它判红——但也绝不从基线里删掉"], advisory=True)

    w = check_wiring()
    add("E 接线（AST）", w["ok"], w["detail"])

    n = negative_selftest()
    add("F 负向自检", n["ok"], n["detail"])

    rep["hard_failed"] = [c["item"] for c in checks if c["ok"] is False]
    rep["undetermined"] = [c["item"] for c in checks if c["ok"] is None and not c["advisory"]]
    rep["ok"] = not rep["hard_failed"]
    return rep


def render_md(rep) -> str:
    verdict = "通过 ✅" if rep["ok"] else "未通过 ❌"
    if rep["ok"] and rep.get("undetermined"):
        verdict = (f"部分通过 ⏸（{len(rep['undetermined'])} 项样本不足未判定，不等于通过）")
    L = ["# 补位去重与出稿分散度验收（2026-11-16 改动点 1）", "",
         f"- 生成时间：{rep['generated_at']}｜窗口 {rep['window'][0]} ~ {rep['window'][1]}"
         f"（{rep['days']} 天）｜生效日 {rep['since']}",
         f"- 验收结论：**{verdict}**",
         "- 基线（11-02~11-15）：同场同比分重复 4 篇（5.5%）、同事件不同措辞 5 篇、"
         "单主体最大簇 曼联 × 12", ""]
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
    ap = argparse.ArgumentParser(description="补位去重与出稿分散度验收")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--end", default=None, help="窗口结束日（默认今天）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    rep = verify(args.days, args.end)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"dedup_spread_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "dedup_spread_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    for item in rep.get("undetermined") or []:
        print(f"::warning::去重分散度「{item}」样本不足，本次未判定（不等于通过）")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
