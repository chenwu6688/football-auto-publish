#!/usr/bin/env python3
"""内容参照层验收（计划 13.3「抄方法论，不抄选题」）。

校验项：
  A. 排除项规则自检（硬项）—— content_reference 的表驱动用例全部符合 13.3；
  B. 参照层配置完整性（硬项）—— 参照项 / 排除项 / 五家媒体齐备且字段完整；
  C. 选题层接入（硬项）—— ranker L1 预筛已调用 content_reference.check_topic；
  D. 生成提示注入（硬项）—— orchestrator 生成路径已注入排除禁令；
  E. 历史产物零命中排除项（告警项，无产物时跳过）—— 扫描 output/**/metadata.json 标题。

产出 data/audit/content_reference_*.md + content_reference_latest.json；硬项 FAIL 退出码 1。

用法：
  python3 scripts/verify_content_reference.py
  python3 scripts/verify_content_reference.py --quiet
"""

import argparse
import glob
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import content_reference as cr  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "output"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))


def check_rules() -> dict:
    """A. 排除项规则自检（硬项）。"""
    bad = cr.selftest()
    return {"item": f"排除项规则自检（{len(cr.RULE_CASES)} 用例）", "ok": not bad, "bad": bad,
            "detail": ([f"❌ {len(bad)} 条用例不符：{bad[:3]}"] if bad
                       else ["前瞻体／中下游球队细节／二线联赛赛事稿／三千字长文 命中判定正确，"
                             "正常选题零误杀 ✅"])}


def check_config() -> dict:
    """B. 参照层配置完整性（硬项）。"""
    refs, excs, media = cr.reference_items(), cr.exclusion_items(), cr.media_items()
    problems = []
    if len(refs) < 4:
        problems.append(f"参照项仅 {len(refs)} 条（应覆盖信源规范/标题结构/时效判断/栏目分层）")
    if len(excs) < 4:
        problems.append(f"排除项仅 {len(excs)} 条（应含前瞻/中下游/二线联赛/三千字长文）")
    if len(media) != 5:
        problems.append(f"五家媒体为 {len(media)} 家（应为 5）")
    for it in refs:
        if not (it.get("key") and it.get("desc")):
            problems.append(f"参照项字段不全：{it.get('key')}")
    for it in excs:
        if not (it.get("key") and it.get("reason") and (it.get("match_any") or it.get("match_all")
                                                        or it.get("max_chars"))):
            problems.append(f"排除项字段不全：{it.get('key')}")
    return {"item": "参照层配置完整性", "ok": not problems,
            "counts": {"参照项": len(refs), "排除项": len(excs), "媒体": len(media)},
            "detail": ([f"参照项 {len(refs)}／排除项 {len(excs)}／媒体 {len(media)}", "字段完整 ✅"]
                       if not problems else problems)}


def _source_has(rel: str, needle: str) -> bool:
    p = PROJECT_ROOT / rel
    try:
        return needle in p.read_text(encoding="utf-8")
    except Exception:
        return False


def check_ranker_wired() -> dict:
    """C. 选题层接入（硬项）。"""
    ok = _source_has("ranker.py", "content_reference.check_topic")
    return {"item": "ranker L1 预筛已接入排除项", "ok": ok,
            "detail": (["ranker.l1_prefilter 调用 content_reference.check_topic ✅"]
                       if ok else ["❌ ranker.py 未见 content_reference.check_topic 调用"])}


def check_prompt_wired() -> dict:
    """D. 生成提示注入（硬项）。"""
    ok = _source_has("orchestrator.py", "content_reference.exclusion_guidance")
    return {"item": "生成提示已注入排除禁令", "ok": ok,
            "detail": (["orchestrator 生成提示含 content_reference.exclusion_guidance() ✅"]
                       if ok else ["❌ orchestrator 生成提示未见排除禁令注入"])}


def check_artifacts() -> dict:
    """E. 历史产物零命中排除项（告警项）。"""
    paths = sorted(glob.glob(str(OUTPUT_DIR / "**" / "metadata.json"), recursive=True))
    if not paths:
        return {"item": "历史产物零命中排除项", "ok": True, "advisory": True, "skipped": True,
                "detail": ["未找到 output/**/metadata.json，本项跳过"]}
    checked, violations = 0, []
    for p in paths:
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in (data.get("articles") or []):
            title = (a.get("title") or "").strip()
            if not title:
                continue
            checked += 1
            v = cr.check_topic({"title": title, "content_type": a.get("content_type", ""),
                                "target_chars": a.get("target_chars")})
            if v:
                violations.append({"date": data.get("date"), "title": title,
                                   "keys": [x["key"] for x in v]})
    return {"item": "历史产物零命中排除项", "ok": not violations, "advisory": True, "skipped": False,
            "files": len(paths), "checked": checked, "violations": violations[:10],
            "detail": [f"扫描 {len(paths)} 份元数据、{checked} 条标题",
                       (f"⚠️ {len(violations)} 条命中排除项（存量，告警）：{violations[:3]}"
                        if violations else "零命中 ✅"),
                       "（前瞻体为硬拦项之一；存量标题若为历史遗留，仅告警不阻断）"]}


def verify() -> dict:
    checks = [check_rules(), check_config(), check_ranker_wired(),
              check_prompt_wired(), check_artifacts()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 内容参照层验收报告（计划 13.3）", "",
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
    ap = argparse.ArgumentParser(description="内容参照层验收（计划 13.3）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"content_reference_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "content_reference_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
