#!/usr/bin/env python3
"""标题句式多样性验收（P0-3）。

病根：近 14 天 90 篇里人物故事 33 + 历史回顾 8 = 45.6%，真正的赛后球评只有 ~6 篇。
追下去发现主因不在蓄水池，而在**赛程稿**：26 篇「一级·确定性」赛程稿里 25 篇被标成
人物故事，19 篇标题同一句式（「没想到XX主帅这场压力比YY还大」）。
源头是 ANGLE_RULES 每型只有 1 条模板，而 A/B（一强一弱）是赛程里最常见的组合——
发赛程稿就等于发同款主帅苦情标题，这是结构必然。

本脚本钉死三件事：
  A. 源头（硬项）—— ANGLE_RULES 每型模板 ≥3 条，且 _pick_template 按日轮换；
  B. 出口（硬项）—— 近 14 天任一句式最多 2 篇（template_gate 判定）；
  C. 分类（硬项）—— 强弱反差（AB）不得再整型归「人物故事」
     （人物故事要靠蓄水池的时间沉淀，不是每天现编赛前苦情）；
  D. 生成侧接入（硬项）—— 赛程稿与战报生成函数都必须接 avoid_signatures 闸门。

产出 data/audit/title_templates_<date>.md + title_templates_latest.json；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_title_templates.py [--days 14] [--quiet]
"""

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import template_gate  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"
FIXTURE_LIB = PROJECT_ROOT / "scripts" / "fixture_library.py"
CST = timezone(timedelta(hours=8))

# 模板轮换 + 句式闸门生效日。此日之前的成稿按旧规则产出
# （19/26 篇同一句式），拿它们判新规则＝永远 FAIL，闸门就成噪音了。
# 历史数据只作为基线证据展示，不参与判定。
TEMPLATE_ROTATION_SINCE = "2026-10-27"


def _load_fixture_lib():
    spec = importlib.util.spec_from_file_location("fixture_library", str(FIXTURE_LIB))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_source_templates() -> dict:
    """A. 每型 ≥3 条模板，且轮换函数存在。"""
    detail, ok = [], True
    try:
        mod = _load_fixture_lib()
    except Exception as e:
        return {"item": "供给侧角度模板已解耦", "ok": False,
                "detail": [f"❌ 无法加载 fixture_library：{e}"]}
    thin = []
    for rule in mod.ANGLE_RULES:
        code, name, _column, templates, _hint = rule
        n = len(templates) if isinstance(templates, (list, tuple)) else 1
        if n < 3:
            thin.append(f"{code}（{name}）只有 {n} 条")
    if thin:
        ok = False
        detail.append("❌ 这些分型模板少于 3 条，必然复读：" + "；".join(thin))
    else:
        detail.append(f"✅ {len(mod.ANGLE_RULES)} 个分型各 ≥3 条模板")
    # 轮换必须随日期变化，否则「每天第 1 场」永远同一句
    if not hasattr(mod, "_pick_template"):
        ok = False
        detail.append("❌ 缺少 _pick_template（无按日轮换）")
    else:
        tpl = next(r for r in mod.ANGLE_RULES if isinstance(r[3], (list, tuple)))[3]
        picks = {mod._pick_template("X", tpl, 739000 + d, 0) for d in range(len(tpl))}
        if len(picks) < min(3, len(tpl)):
            ok = False
            detail.append(f"❌ 轮换不随日期变化（{len(picks)} 种）")
        else:
            detail.append(f"✅ 轮换随日期变化（{len(picks)} 种起点）")
    return {"item": "供给侧角度模板已解耦", "ok": ok, "detail": detail}


def check_recent_diversity(days: int = 14) -> dict:
    """B. 近 N 天（限生效日之后）任一句式 ≤2 篇；历史只作基线。"""
    today = datetime.now(CST).strftime("%Y-%m-%d")
    # 基线：改造前的历史数据，只展示不判定
    base_titles = template_gate.recent_titles(OUTPUT_DIR, today, days=days)
    baseline = ""
    if base_titles:
        b = template_gate.check(base_titles)
        top = sorted(b["counts"].items(), key=lambda x: -x[1])[:3]
        baseline = (f"改造前基线（{b['total']} 篇）：" +
                    ("、".join(f"{k}×{v}" for k, v in top) or "无命中"))

    titles = template_gate.recent_titles(OUTPUT_DIR, today, days=days,
                                         since=TEMPLATE_ROTATION_SINCE)
    if not titles:
        return {"item": f"近 {days} 天标题句式多样性", "ok": True, "advisory": True,
                "detail": [f"⚠️ 生效日 {TEMPLATE_ROTATION_SINCE} 之后本机无成稿（CI 才有 output/），"
                           f"跳过判定", baseline] if baseline else
                          [f"⚠️ 生效日 {TEMPLATE_ROTATION_SINCE} 之后本机无成稿，跳过判定"]}
    rep = template_gate.check(titles)
    detail = [f"生效后样本 {rep['total']} 篇，命中句式：" +
              ("、".join(f"{k}×{v}" for k, v in sorted(rep["counts"].items(),
                                                      key=lambda x: -x[1])) or "无")]
    if not rep["ok"]:
        for k, v in rep["violations"].items():
            detail.append(f"❌ 句式「{k}」{days} 天出现 {v} 篇（上限 {rep['threshold']}）")
    else:
        detail.append(f"✅ 任一句式均未超过 {rep['threshold']} 篇")
    if baseline:
        detail.append(baseline)
    return {"item": f"近 {days} 天标题句式多样性", "ok": rep["ok"], "detail": detail}


def check_ab_not_person_story() -> dict:
    """C. 强弱反差（AB）不得归「人物故事」。"""
    try:
        mod = _load_fixture_lib()
    except Exception as e:
        return {"item": "强弱反差不再整型归人物故事", "ok": False, "detail": [f"❌ {e}"]}
    detail, ok = [], True
    for rule in mod.ANGLE_RULES:
        code, _name, column, _tpl, _hint = rule
        if code == "AB" and column == "人物故事":
            ok = False
            detail.append("❌ AB（强弱反差）仍归「人物故事」——这正是 45.6% 的来源")
    if ok:
        detail.append("✅ AB 归「战术榜单」，人物故事改由蓄水池供给")
    return {"item": "强弱反差不再整型归人物故事", "ok": ok, "detail": detail}


def check_generation_wired() -> dict:
    """D. 生成侧真的接了句式闸门（读 AST 之外用 import 检查最直接）。"""
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    detail, ok = [], True
    if not re.search(r"^import template_gate", text, re.M):
        ok = False
        detail.append("❌ orchestrator 未 import template_gate")
    n_sig = len(re.findall(r"def generate_\w+_article\([^)]*avoid_signatures=None",
                           text, re.S))
    n_pass = len(re.findall(r"avoid_signatures=_avoid_sigs", text))
    if n_sig < 2:
        ok = False
        detail.append(f"❌ 只有 {n_sig} 个生成函数接受 avoid_signatures（赛程稿+战报都要）")
    else:
        detail.append(f"✅ {n_sig} 个生成函数接受 avoid_signatures")
    if n_pass < 2:
        ok = False
        detail.append(f"❌ 只有 {n_pass} 处调用实际传入（应为 2：赛程稿 + 战报）")
    else:
        detail.append(f"✅ {n_pass} 处调用实际传入闸门集合")
    return {"item": "生成侧接入句式闸门", "ok": ok, "detail": detail}


def verify(days: int = 14) -> dict:
    checks = [check_source_templates(), check_recent_diversity(days),
              check_ab_not_person_story(), check_generation_wired()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 标题句式多样性验收（P0-3）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}：{'✅' if c['ok'] else '❌'}"
                 + ("（告警项）" if c.get("advisory") else ""))
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="标题句式多样性验收")
    ap.add_argument("--days", type=int, default=14, help="统计窗口天数（默认 14）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()
    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"title_templates_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "title_templates_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
