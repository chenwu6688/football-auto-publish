#!/usr/bin/env python3
"""选题漏斗台账验收（2026-11-04，S2 配额归并的前置度量）。

验收的不是「漏斗顺不顺」，而是**这根管子有没有真的接在流水线上** ——
台账类模块最典型的失效不是算错，是**根本没被调用**（11-04 `_img_topic` 假接线：
函数在、调用在、单测绿、数据恒空）。所以这里的硬项全部围绕「接线」：

  A. 模块口径自检（硬）—— `topic_funnel.selftest()` 17 项全绿（相邻层算损失、
     缺层不当 0、变大记 gain、拼错层级名必抛错）
  B. 接线（硬）—— orchestrator 里 **8 个层级一个都不能少**，且必须是 AST 里
     真实的 `_funnel("<key>", …)` 调用；`begin` 至少 1 处、`flush` 至少 2 处
     （正常收尾 + 异常兜底）
  C. 负向验证（硬）—— 把源码里的 `dedup` 埋点改名后，B 必须报红；
     否则「8/8 全绿」可能只是检查器恒真
  D. 隔离（硬）—— 台账落在 `data/audit/`；跑一次自检后**真实台账行数不变**
     （自检若写进真实账本，验收跑一次就污染一次）
  E. 不是闸门（硬）—— 埋点函数必须被 try/except 包住（埋点炸了不许影响发布），
     且本项若登记进 `config/audit_tiers.yaml`，**不得进 pre-publish**
  F. 真实数据（三态）—— 台账为空 → ⏸ 未判定（不是「零损失」）；
     有记录 → 打印批均剩余/达成率/最大损失层，供 S2 决策

退出码：硬项 FAIL → 1｜全通过 → 0｜结果项未判定 → 2（≠ 通过）
产物：``data/audit/topic_funnel_latest.json``

用法：
  python3 scripts/verify_topic_funnel.py [--quiet]
  python3 scripts/verify_topic_funnel.py --report --days 14   # 只看漏斗报告
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import topic_funnel as tf  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"
TIERS = PROJECT_ROOT / "config" / "audit_tiers.yaml"
STAGES = tf._KEYS
CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def check_selftest() -> dict:
    """A. 模块口径自检（硬）。"""
    cases = tf.selftest()
    bad = [n for n, ok in cases if not ok]
    return {"item": "模块口径自检", "ok": not bad,
            "detail": [("✅ " if not bad else "❌ ")
                       + f"{len(cases)} 项自检" + (f"，失败：{'、'.join(bad)}" if bad else "全绿")]}


def _funnel_calls(src: str) -> tuple[dict, dict, list]:
    """从源码里取真实 Call 节点：(_funnel 层级 → 行号, topic_funnel 调用 → 行号, 非法首参)。"""
    tree = ast.parse(src)
    stages: dict = {}
    module_calls: dict = {}
    bad_first_arg: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None)
        if name == "_funnel":
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                stages.setdefault(first.value, []).append(node.lineno)
            else:
                bad_first_arg.append(node.lineno)
        elif (getattr(fn, "attr", None) in ("begin", "flush")
              and getattr(getattr(fn, "value", None), "id", None) == "topic_funnel"):
            module_calls.setdefault(fn.attr, []).append(node.lineno)
    return stages, module_calls, bad_first_arg


def check_wiring(src: str | None = None) -> dict:
    """B. 接线（硬）：8 个层级 + begin + flush 都必须真实存在。"""
    if src is None:
        src = ORCHESTRATOR.read_text(encoding="utf-8")
    try:
        stages, calls, bad = _funnel_calls(src)
    except SyntaxError as e:                       # pragma: no cover
        return {"item": "接线：8 层埋点齐全", "ok": False,
                "detail": [f"❌ 解析 orchestrator 失败：{e}"]}

    missing = [k for k in STAGES if k not in stages]
    unknown = [k for k in stages if k not in STAGES]
    detail, ok = [], True
    detail.append(f"埋点层级 {len([k for k in STAGES if k in stages])}/{len(STAGES)}")
    if missing:
        ok = False
        detail.append(f"❌ 缺层级埋点：{'、'.join(missing)}"
                      "—— 缺一层就等于那一段候选凭空消失，报表里永远看不见")
    else:
        detail.append("✅ 8 层齐全：" + " → ".join(
            f"{k}(L{min(v)})" for k, v in ((k, stages[k]) for k in STAGES)))
    if unknown:
        ok = False
        detail.append(f"❌ 用了未定义的层级名：{'、'.join(unknown)}（拼错名=静默失效）")
    if bad:
        ok = False
        detail.append(f"❌ 有 {len(bad)} 处 `_funnel` 首参不是字符串常量（行号 {bad[:5]}）"
                      "—— 验收无法复核，等于没接")
    if not calls.get("begin"):
        ok = False
        detail.append("❌ 找不到 topic_funnel.begin()——台账永远不会开批")
    else:
        detail.append(f"✅ begin 在 L{min(calls['begin'])}")
    if len(calls.get("flush") or []) < 2:
        ok = False
        detail.append(f"❌ topic_funnel.flush() 只有 {len(calls.get('flush') or [])} 处，"
                      "至少要有「正常收尾」和「异常兜底」两处（否则崩溃的批次不留证）")
    else:
        detail.append(f"✅ flush {len(calls['flush'])} 处（收尾 + 兜底）")
    return {"item": "接线：8 层埋点齐全", "ok": ok, "detail": detail}


def check_negative() -> dict:
    """C. 负向验证（硬）：把 dedup 埋点改名后，接线检查必须报红。"""
    src = ORCHESTRATOR.read_text(encoding="utf-8")
    mutated = src.replace('_funnel("dedup"', '_funnel("dedupn"')
    if mutated == src:
        return {"item": "负向验证：埋点被删/改名后必须报红", "ok": False,
                "detail": ["❌ 负向用例失效：源码里找不到 `_funnel(\"dedup\"`，"
                           "说明埋点已被删或写法变了，验收器无法证伪"]}
    rep = check_wiring(mutated)
    caught = not rep["ok"]
    # 再验一次「首参不是常量」的负向（保证校验真的看 AST 形态，不是看字符串）
    rep2 = check_wiring(src.replace('_funnel("llm", len(topics))',
                                    '_funnel(_k, len(topics))', 1)
                        if '_funnel("llm", len(topics))' in src else src)
    caught2 = not rep2["ok"]
    ok = caught and caught2
    detail = [("✅ " if caught else "❌ ")
              + "删掉 dedup 埋点 → 接线检查报红"]
    detail.append(("✅ " if caught2 else "❌ ")
                  + "层级名改成变量 → 接线检查报红（不是只看字符串）")
    return {"item": "负向验证：埋点被删/改名后必须报红", "ok": ok, "detail": detail}


def check_isolation() -> dict:
    """D. 隔离（硬）：台账在 data/audit 下，且跑自检不污染真实台账。"""
    detail, ok = [], True
    path = Path(tf.LEDGER_PATH)
    if AUDIT_DIR not in path.parents:
        ok = False
        detail.append(f"❌ 台账不在 data/audit 下：{path}")
    else:
        detail.append(f"✅ 台账路径 {path.relative_to(PROJECT_ROOT)}")
    try:
        before = len(tf.load())
    except Exception:
        before = 0
    tf.selftest()
    after = len(tf.load())
    if after != before:
        ok = False
        detail.append(f"❌ 自检污染了真实台账：{before} → {after} 行"
                      "（自检必须用临时文件）")
    else:
        detail.append(f"✅ 跑完自检真实台账行数不变（{after} 行）")
    return {"item": "台账隔离与路径", "ok": ok, "detail": detail}


def check_not_a_gate() -> dict:
    """E. 不是闸门（硬）：埋点异常不阻断发布；不得登记进 pre-publish。"""
    detail, ok = [], True
    src = ORCHESTRATOR.read_text(encoding="utf-8")
    tree = ast.parse(src)
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_funnel":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Try) and sub.handlers:
                guarded = True
    if guarded:
        detail.append("✅ `_funnel` 用 try/except 包住 `stage()`（埋点炸了不影响发布）")
    else:
        ok = False
        detail.append("❌ `_funnel` 没有 try/except——拼错层级名会让整批发布中断")
    # 本项若登记，不得进 pre-publish。
    # 必须按 yaml 结构判 tiers，**不能全文找 "pre-publish" 字样** —— 首版就是那么写的，
    # 结果被 why 里那句「不要把它挪进 pre-publish」判成红。判词必须带语境/结构，
    # 这是全仓的老规矩（闸门判词不许裸匹配）。
    try:
        import yaml
        raw = yaml.safe_load(TIERS.read_text(encoding="utf-8")) or {}
        item = (raw.get("items") or {}).get("topic_funnel")
        if not isinstance(item, dict):
            detail.append("· 未登记进 config/audit_tiers.yaml（登记后须保持非 pre-publish）")
        else:
            tiers = [str(t) for t in (item.get("tiers") or [])]
            if "pre-publish" in tiers:
                ok = False
                detail.append(f"❌ 登记进了 pre-publish（tiers={tiers}）——它是台账不是闸门，"
                              "会把「不发这批」绑在一个纯观测项上")
            else:
                detail.append(f"✅ 已登记（tiers={tiers}，kind={item.get('kind')}），"
                              "不在 pre-publish（台账不阻断发布）")
    except Exception as e:
        detail.append(f"· 读 audit_tiers.yaml 失败（不判）：{e}")
    return {"item": "不是闸门：不阻断发布", "ok": ok, "detail": detail}


def check_data(days: int = 7) -> dict:
    """F. 真实数据（三态）：空 → 未判定；有 → 打印报告。"""
    s = tf.summarize(days)
    if not s["decided"]:
        return {"item": f"近 {days} 天漏斗数据", "ok": None, "skipped": True,
                "detail": [f"⏸ 近 {days} 天台账为空——本项未判定（空台账 ≠ 零损失）："
                           "埋点刚接上，等第一批真实运行落盘"]}
    detail = tf.report(days).splitlines()
    detail.append(f"批数 {s['records']}｜请求 {s['requested']} 篇→成稿 {s['articles']} 篇"
                  f"（达成率 {s['fill_rate']:.0%}）" if s["fill_rate"] is not None else "")
    return {"item": f"近 {days} 天漏斗数据", "ok": True, "advisory": True,
            "detail": [d for d in detail if d]}


def verify(days: int = 7) -> dict:
    checks = [check_selftest(), check_wiring(), check_negative(),
              check_isolation(), check_not_a_gate(), check_data(days)]
    hard = [c for c in checks if not c.get("advisory") and not c.get("skipped")]
    failed = [c["item"] for c in hard if c["ok"] is False]
    pending = [c["item"] for c in checks if c.get("skipped") or c.get("ok") is None]
    return {"generated_at": _now(), "ok": not failed, "checks": checks,
            "hard_failed": failed, "pending": pending}


def render_md(rep: dict) -> str:
    L = ["# 选题漏斗台账验收", "", f"- 生成时间：{rep['generated_at']}",
         f"- 结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**"
         + (f"（未判定：{'、'.join(rep['pending'])}）" if rep["pending"] else ""), ""]
    for c in rep["checks"]:
        status = "⏸" if (c.get("ok") is None or c.get("skipped")) else ("✅" if c["ok"] else "❌")
        L.append(f"## {c['item']}：{status}")
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="选题漏斗台账验收")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--report", action="store_true", help="只打印漏斗报告")
    args = ap.parse_args(argv)

    if args.report:
        print(tf.report(args.days))
        return 0

    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / "topic_funnel_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.quiet:
        print(f"topic_funnel: {'ok' if rep['ok'] else 'FAIL ' + '、'.join(rep['hard_failed'])}"
              + (f" pending={rep['pending']}" if rep["pending"] else ""))
    else:
        print(render_md(rep))
    if rep["pending"]:
        print(f"::warning::topic_funnel 有 {len(rep['pending'])} 项未判定，不是通过")
    if not rep["ok"]:
        return 1
    return 2 if rep["pending"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
