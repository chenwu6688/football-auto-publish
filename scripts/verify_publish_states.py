#!/usr/bin/env python3
"""发布终态三态契约 + 重拟链未判定口径的验收（2026-11-05 新增）。

## 补的缺口

`verify_publish_success` 在看「发出去没有」，但**没有人看「没发出去时，系统有没有说清是为什么」**。
2026-11-05 晚间就是灰区：0 篇、退出码 1、批次却标成「已完成」，
补发被幂等跳过——三件事同时成立，任何单一日志都看不出全貌。

## 判据

  A. 退出码四态互异（硬项）——0/1/2/3 各自语义不同，`all_skipped→2`、`empty→3`。
     负向自检：把 EXIT_NOTHING_SKIPPED 改成 1，本项必须报红。
  B. `load_articles` 不得 `sys.exit`（硬项）——判据是 AST：函数体里出现
     `sys.exit(...)` 即 FAIL。理由：exit 发生在库函数里，调用方只能看到「进程没了」，
     没法把它翻成「本批无事可做」，也没法落台账留证。
  C. batch.yml 处理 rc=2/3（硬项）——发布步骤必须显式 `case "$RC"`，
     rc=3 退出非零（不标完成），rc=2 收成功（标完成）。缺任一条就等于
     「三态」只写在 Python 里，流水线仍然一视同仁。
  D. 重拟链不得裸调评分器（硬项）——`_rewrite_with_retry` /
     `generate_prediction_article` / `ensure_fixture_title` 里直接 `score_title_plan(...)`
     即 FAIL：评分器抛异常会穿到重试循环外，把已经生成好的正文整篇带走。
  E. 预测稿分级口径唯一（硬项）——publisher 用 prediction_gate，
     看板/配额/配比/绩效四处必须调 `counts_as_content`，不得各写一遍
     `not a.get("is_prediction")`（同一规则写四遍，改一处必漏三处）。
  F. 「已生成」≠「已完成」（硬项，2026-11-06 新增）——`save_batch_state` 里
     只要还给 `batches_completed` 赋值就 FAIL；batch.yml 必须调用
     `mark_batch_completed.py`（completed 的唯一写入点）。
     11-06 noon：生成 2 篇 → 配图硬项失败 → 发布 skipped，但 completed 早在
     生成回写时就进了仓库 → 幂等跳过补发与兜底 cron → 两篇稿永久丢。

用法：
  python3 scripts/verify_publish_states.py
  python3 scripts/verify_publish_states.py --quiet
"""

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

#: 必须走安全入口的三个函数（异常穿出即整篇丢稿）
SAFE_REQUIRED = ("_rewrite_with_retry", "generate_prediction_article",
                 "ensure_fixture_title", "need_safe_score")

#: 口径必须收敛到 prediction_gate.counts_as_content 的四处
SCOPE_FILES = (
    "scripts/build_metrics_dashboard.py",
    "scripts/verify_supply_mix.py",
    "scripts/verify_batch_quota.py",
    "scripts/check_perf_lag.py",
)


def _check_exit_codes() -> dict:
    import publisher as P
    codes = {"ok": P.EXIT_OK, "fail": P.EXIT_FAIL,
             "nothing_skipped": P.EXIT_NOTHING_SKIPPED, "nothing_empty": P.EXIT_NOTHING_EMPTY}
    detail, ok = [], True
    if len(set(codes.values())) != 4:
        ok = False
        detail.append(f"❌ 退出码有重复：{codes}")
    else:
        detail.append(f"✅ 四态互异：{codes}")
    if P.NoPublishableBatch("all_skipped", "x").exit_code != P.EXIT_NOTHING_SKIPPED:
        ok = False
        detail.append("❌ all_skipped 未映射到 EXIT_NOTHING_SKIPPED")
    if P.NoPublishableBatch("empty", "x").exit_code != P.EXIT_NOTHING_EMPTY:
        ok = False
        detail.append("❌ empty 未映射到 EXIT_NOTHING_EMPTY")

    # 负向自检：把 2 改成 1 之后，本判据必须能发现（证明它真的在判，不是恒真）
    orig = P.EXIT_NOTHING_SKIPPED
    try:
        P.EXIT_NOTHING_SKIPPED = P.EXIT_FAIL
        dup = len({P.EXIT_OK, P.EXIT_FAIL, P.EXIT_NOTHING_SKIPPED, P.EXIT_NOTHING_EMPTY}) != 4
        if dup:
            detail.append("✅ 负向自检：把 EXIT_NOTHING_SKIPPED 设成 1 后本判据会报红")
        else:
            ok = False
            detail.append("❌ 负向自检失败：判据对「两态同码」无感，等于恒真")
    finally:
        P.EXIT_NOTHING_SKIPPED = orig
    return {"item": "退出码四态互异（含负向自检）", "ok": ok, "advisory": False,
            "detail": detail}


def _check_no_sysexit() -> dict:
    src = (PROJECT_ROOT / "publisher.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "load_articles"), None)
    if fn is None:
        return {"item": "load_articles 不得 sys.exit（改用 raise）", "ok": False,
                "advisory": False, "detail": ["❌ 找不到 load_articles"]}
    hits = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "exit" and \
                    isinstance(f.value, ast.Name) and f.value.id == "sys":
                hits.append(node.lineno)
    if hits:
        return {"item": "load_articles 不得 sys.exit（改用 raise）", "ok": False,
                "advisory": False,
                "detail": [f"❌ publisher.py 第 {hits} 行仍在 sys.exit："
                           f"调用方无法区分「无事可做」与「失败」"]}
    return {"item": "load_articles 不得 sys.exit（改用 raise）", "ok": True,
            "advisory": False,
            "detail": ["✅ 无 sys.exit；没稿可发走 NoPublishableBatch（带 code + 台账）"]}


def _check_workflow() -> dict:
    import yaml
    p = PROJECT_ROOT / ".github" / "workflows" / "batch.yml"
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    steps = (d.get("jobs") or {}).get("publish", {}).get("steps") or []
    pub = next((s for s in steps if s.get("id") == "publish"), None)
    detail, ok = [], True
    if pub is None:
        return {"item": "batch.yml 处理 rc=2/3", "ok": False, "advisory": False,
                "detail": ["❌ 发布步骤缺 id: publish"]}
    run = pub.get("run") or ""
    if 'case "$RC"' not in run:
        ok = False
        detail.append("❌ 发布步骤未按退出码分流（缺 case \"$RC\"）")
    else:
        detail.append("✅ 按退出码分流")
    if not re.search(r"\b2\)", run):
        ok = False
        detail.append("❌ 未处理 rc=2（有稿全被跳过 → 标完成 + 告警）")
    else:
        detail.append("✅ 处理 rc=2（标完成 + warning）")
    # 原来用「`3)` 之后 600 字内必须出现 exit 1」判的，这是个脆判据：11-11 给 rc=3
    # 分支加了「0 篇原因归类」（上游空转 / 风险闸门 / 合规）之后，exit 1 被推到 600 字
    # 之外，行为没变而验收变红 —— 判的不是「有没有 exit 1」，是「离得多远」。
    # 改成切出 3) 到 `;;` 之间的分支体，在体内找 exit 1，与文案长度无关。
    _m3 = re.search(r"\b3\)([\s\S]*?);;", run)
    if not (_m3 and re.search(r"\bexit\s+1\b", _m3.group(1))):
        ok = False
        detail.append("❌ 未处理 rc=3（上游空转 → 退出非零，让兜底 cron 重跑）")
    else:
        detail.append("✅ 处理 rc=3（exit 1 → 不标批次完成）")
        # 0 篇有三种成因（上游空转 / 风险闸门全转人工 / 合规全剔除），处置互相排斥：
        # 告警里只说「上游空转」会把排查方向带偏（11-10/11-11 实证）。
        if "publish_zero_reason" in _m3.group(1):
            detail.append("✅ rc=3 分支引用了 0 篇原因归类（publish_zero_reason）")
        else:
            detail.append("⚠️ rc=3 分支没引用 publish_zero_reason：0 篇只会报「上游空转」")
    comp = next((s for s in steps if "回写批次状态" in (s.get("name") or "")), None)
    if comp and "publish.outcome" not in (comp.get("if") or ""):
        ok = False
        detail.append("❌ 回写批次状态没有绑定 publish.outcome：失败的批次会被标成已完成")
    elif comp:
        detail.append("✅ 回写批次状态绑定 publish.outcome")
    return {"item": "batch.yml 处理 rc=2/3", "ok": ok, "advisory": False, "detail": detail}


def _check_safe_wrappers() -> dict:
    src = (PROJECT_ROOT / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    detail, bad = [], []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        if fn.name not in SAFE_REQUIRED:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "score_title_plan":
                bad.append(f"{fn.name}@{node.lineno}")
    if bad:
        detail.append("❌ 以下位置仍裸调 score_title_plan（崩了会丢整篇）：" + "、".join(bad))
    else:
        detail.append(f"✅ {len(SAFE_REQUIRED)} 个入口均走 _safe_title_score / _safe_elements_ok")
    for name in ("_safe_title_score", "_safe_elements_ok", "_safe_title_candidates", "_unjudged"):
        if f"def {name}(" not in src:
            bad.append(name)
            detail.append(f"❌ 缺安全入口 {name}")
    return {"item": "重拟链不得裸调评分器", "ok": not bad, "advisory": False, "detail": detail}


def _check_scope() -> dict:
    detail, bad = [], []
    for rel in SCOPE_FILES:
        p = PROJECT_ROOT / rel
        if not p.exists():
            bad.append(rel)
            detail.append(f"❌ 缺文件 {rel}")
            continue
        src = p.read_text(encoding="utf-8")
        if "counts_as_content" not in src:
            bad.append(rel)
            detail.append(f"❌ {rel} 未用 prediction_gate.counts_as_content")
        if re.search(r'not\s+a\.get\(\s*["\']is_prediction["\']\s*\)', src):
            bad.append(rel)
            detail.append(f"❌ {rel} 仍按体裁一律剔除（not a.get(\"is_prediction\")）")
    if not bad:
        detail.append(f"✅ {len(SCOPE_FILES)} 处口径一致（按依据分级，不按体裁）")
    return {"item": "预测稿口径唯一（4 处）", "ok": not bad, "advisory": False,
            "detail": detail}


def _check_batch_state_split() -> dict:
    """「已生成」与「已完成」必须是两件事（2026-11-06 丢批事故）。

    判据用 AST，不用文本匹配：`save_batch_state` 函数体里只要出现对
    ``["batches_completed"]`` 的**赋值**就算 FAIL。它一旦在生成侧写完成，
    发布失败的批次也会被标成完成——幂等保护据此跳过补发与兜底 cron，
    当批稿子永久丢（11-06 noon 两篇实证）。

    另一半判据是接线：``batches_completed`` 既然不在生成侧写了，就必须有人写。
    缺 `mark_batch_completed.py` 调用 = 幂等永不触发 = 每批重复生成。
    """
    detail, ok = [], True
    try:
        src = (PROJECT_ROOT / "orchestrator.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception as exc:
        return {"item": "批次已完成只由发布成功写", "ok": False, "advisory": False,
                "detail": [f"❌ orchestrator.py 解析失败：{exc}"]}

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "save_batch_state"), None)
    if fn is None:
        return {"item": "批次已完成只由发布成功写", "ok": False, "advisory": False,
                "detail": ["❌ 找不到 save_batch_state（改名了？本判据需同步）"]}

    bad_lines = []
    for node in ast.walk(fn):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                    and t.slice.value == "batches_completed":
                bad_lines.append(getattr(node, "lineno", "?"))
    if bad_lines:
        detail.append(f"❌ save_batch_state 仍在写 batches_completed（行 {bad_lines}）："
                      "生成成功即标完成 → 发布失败无法补发、兜底 cron 不重跑")
        ok = False
    else:
        detail.append("✅ 生成侧只写 batches_generated，不写 completed")

    wf = PROJECT_ROOT / ".github" / "workflows" / "batch.yml"
    try:
        wf_src = wf.read_text(encoding="utf-8")
    except Exception as exc:
        detail.append(f"❌ 读不到 batch.yml：{exc}")
        return {"item": "批次已完成只由发布成功写", "ok": False, "advisory": False,
                "detail": detail}

    if "mark_batch_completed.py" in wf_src:
        detail.append("✅ 发布成功步骤调用 mark_batch_completed.py（completed 唯一写入点）")
    else:
        detail.append("❌ batch.yml 未调用 mark_batch_completed.py："
                      "completed 永不增长，幂等失效 → 每批重复生成")
        ok = False

    return {"item": "批次已完成只由发布成功写", "ok": ok, "advisory": False, "detail": detail}


def verify() -> dict:
    checks = [_check_exit_codes(), _check_no_sysexit(), _check_workflow(),
              _check_safe_wrappers(), _check_scope(), _check_batch_state_split()]
    hard_failed = [c["item"] for c in checks if c["ok"] is False and not c.get("advisory")]
    return {
        "version": 1,
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "ok": not hard_failed,
        "hard_failed": hard_failed,
        "hard_pending": [],
        "checks": checks,
    }


def render_md(rep: dict) -> str:
    L = ["# 发布终态三态契约验收（2026-11-05 新增）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**",
         "- 补的缺口：没人检查「没发出去时，系统有没有说清为什么」——"
         "0 篇 + exit 1 + 批次标成已完成，三件事同时成立，任何单一日志都看不出全貌。",
         ""]
    if rep["hard_failed"]:
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}：{'✅' if c['ok'] else '❌'}")
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="发布终态三态契约验收")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"publish_states_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "publish_states_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
