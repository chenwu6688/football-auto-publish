#!/usr/bin/env python3
"""认知度分级硬门槛验收（计划 13.1）。

计划 13.1 的三条硬规则：
  1. A 可进新闻流／B 需配认知锚点或走故事角度／C 降权或只进数据类与榜单类；
  2. 未登记的实体不得进入生成环节；
  3. C 级（confidence=low）不参与标题生成，仅作背景数据。

校验项：
  A. 分级规则自检（硬项）—— awareness_gate 的表驱动用例全部符合计划 13.1；
  B. 映射表合法性（硬项）—— config/entity_map.json 每个实体的 awareness ∈ {A,B,C}，
     且 confidence 与 awareness 一致（A→high／B→medium／C→low）；
  C. 供给物零违规（硬项，无产物时跳过）—— 选题池/节点库存中无未登记主体、
     无 C 级越过「数据类/榜单类」边界；
  D. 分级分布与 B 级锚点覆盖（告警项）。

产出 data/audit/awareness_gate_*.md + awareness_gate_latest.json；硬项 FAIL 退出码 1。

用法：
  python3 scripts/verify_awareness_gate.py
  python3 scripts/verify_awareness_gate.py --quiet
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import awareness_gate as ag  # noqa: E402

ENTITY_MAP = PROJECT_ROOT / "config" / "entity_map.json"
POOL_PATH = PROJECT_ROOT / "data" / "fixtures" / "topic_pool.json"
NODE_STORE = PROJECT_ROOT / "data" / "deterministic" / "items.json"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"

CST = timezone(timedelta(hours=8))


def check_rules() -> dict:
    """A. 分级规则自检（硬项）。"""
    bad = ag.selftest()
    return {"item": f"分级规则自检（{len(ag.RULE_CASES)} 用例）", "ok": not bad,
            "bad": bad,
            "detail": ([f"❌ {len(bad)} 条用例不符：{bad[:5]}"] if bad
                       else ["A 可进新闻流／B 需锚点或走故事角度／C 降权或仅数据榜单／"
                             "未登记不得进入生成 —— 全部符合 ✅"])}


def check_entity_map() -> dict:
    """B. 映射表合法性（硬项）。"""
    try:
        raw = json.loads(ENTITY_MAP.read_text(encoding="utf-8"))
    except Exception as e:
        return {"item": "映射表 awareness 合法性", "ok": False,
                "detail": [f"读取失败：{e}"]}
    ents = list((raw.get("entities") or {}).values())
    illegal, mismatch, no_zh = [], [], 0
    for e in ents:
        if not (e or {}).get("name_zh"):
            no_zh += 1
            continue
        aw = str((e or {}).get("awareness") or "").strip().upper()
        if aw not in ag.LEVELS:
            illegal.append({"name_zh": e.get("name_zh"), "awareness": aw})
            continue
        conf = e.get("confidence")
        if conf and conf != ag.CONFIDENCE.get(aw):
            mismatch.append({"name_zh": e.get("name_zh"), "awareness": aw, "confidence": conf})
    ok = not illegal and not mismatch
    return {"item": "映射表 awareness 合法性", "ok": ok,
            "total": len(ents), "illegal": illegal[:10], "confidence_mismatch": mismatch[:10],
            "detail": [f"实体 {len(ents)} 个（无中文名 {no_zh} 个跳过）",
                       f"非法 awareness：{len(illegal)}　confidence 不一致：{len(mismatch)}",
                       "（A→high／B→medium／C→low，与 scripts/build_player_map.py 同口径）"]}


def check_supply_artifacts() -> dict:
    """C. 供给物零违规（硬项；无产物时跳过）。"""
    checked, violations, skipped = 0, [], []
    # 1) 赛程选题池：awareness_pair 必须是 A/B/C 组合，且不得有 C/C
    if POOL_PATH.exists():
        try:
            pool = json.loads(POOL_PATH.read_text(encoding="utf-8"))
            for t in (pool.get("topics") or []):
                checked += 1
                pair = str(t.get("awareness_pair") or "")
                lvls = [p for p in pair.split("/") if p]
                if not lvls or any(p not in ag.LEVELS for p in lvls):
                    violations.append({"kind": "未登记/非法认知度", "topic_id": t.get("topic_id"),
                                       "awareness_pair": pair})
                elif lvls.count("C") == len(lvls):
                    violations.append({"kind": "C 级双主体仍进新闻流", "topic_id": t.get("topic_id"),
                                       "awareness_pair": pair})
        except Exception as e:
            violations.append({"kind": "选题池解析失败", "error": str(e)[:80]})
    else:
        skipped.append("topic_pool.json")

    # 2) 一级节点库存：留痕的 awareness/决策不得越界
    if NODE_STORE.exists():
        try:
            store = json.loads(NODE_STORE.read_text(encoding="utf-8"))
            for it in (store.get("items") or []):
                checked += 1
                aw = str(it.get("awareness") or "")
                dec = str(it.get("awareness_decision") or "")
                sec = str(it.get("section") or "")
                if aw and aw not in ag.LEVELS:
                    violations.append({"kind": "未登记认知度入库", "id": it.get("id"), "awareness": aw})
                if aw == "C" and not any(k in sec for k in ag.C_ALLOWED_SECTIONS):
                    violations.append({"kind": "C 级越板（非数据/榜单）", "id": it.get("id"), "section": sec})
                if dec == "block":
                    violations.append({"kind": "block 决策仍入库", "id": it.get("id")})
        except Exception as e:
            violations.append({"kind": "节点库存解析失败", "error": str(e)[:80]})
    else:
        skipped.append("items.json")

    if checked == 0:
        return {"item": "供给物零违规", "ok": True, "skipped": True, "checked": 0,
                "detail": [f"未找到供给产物（{'、'.join(skipped)}），本项跳过"]}
    return {"item": "供给物零违规", "ok": not violations, "skipped": False,
            "checked": checked, "violations": violations[:10],
            "detail": [f"已检查 {checked} 条供给物",
                       (f"❌ 违规 {len(violations)} 条：{violations[:5]}" if violations
                        else "无未登记主体、无 C 级越板 ✅"),
                       f"（跳过：{'、'.join(skipped) or '无'}）"]}


def check_distribution() -> dict:
    """D. 分级分布与 B 级锚点覆盖（告警项）。"""
    m = ag.load_map()
    from collections import Counter
    cnt = Counter(m.values())
    total = max(1, len(m))
    rows = {k: {"count": cnt.get(k, 0), "pct": round(100.0 * cnt.get(k, 0) / total, 1)}
            for k in ag.LEVELS}
    unregistered = cnt.get(ag.UNREGISTERED, 0)
    return {"item": "分级分布与 B 级锚点覆盖", "advisory": True,
            "ok": unregistered == 0 and rows["A"]["count"] > 0,
            "total": len(m), "rows": rows, "unregistered": unregistered,
            "detail": [f"映射表 {len(m)} 个实体：A={rows['A']['count']}（{rows['A']['pct']}%）"
                       f" B={rows['B']['count']}（{rows['B']['pct']}%）"
                       f" C={rows['C']['count']}（{rows['C']['pct']}%）",
                       f"非法/未登记 awareness：{unregistered}",
                       "（B 级候选需在装配层补认知锚点或改走人物故事角度）"]}


def verify() -> dict:
    checks = [check_rules(), check_entity_map(), check_supply_artifacts(), check_distribution()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 认知度分级硬门槛验收报告（计划 13.1）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警" if c.get("advisory") else "硬项"
        status = "跳过" if c.get("skipped") else ("✅" if c["ok"] else "❌")
        L.append(f"## {c['item']}（{tag}）：{status}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="认知度分级硬门槛验收（计划 13.1）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"awareness_gate_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "awareness_gate_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
