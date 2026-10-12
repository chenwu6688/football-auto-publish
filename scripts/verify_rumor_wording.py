#!/usr/bin/env python3
"""传闻分级措辞与传闻标题验收（计划 4.1）。

计划表 4 的两条对策：
  - 失实表述（扣 10）→ 传闻分级措辞系统；
  - 严重标题党（扣 30）→ 传闻类标题禁用肯定语气。

校验项：
  A. 措辞规则自检（硬项）—— rumor_wording 的表驱动用例全部符合计划 4.1；
  B. 分级常量一致性（硬项）—— 本模块分级与 fact_card 三级可信度同源，防止两处漂移；
  C. 成稿标题零「传闻当官宣」（硬项，无产物时跳过）—— 扫描 output/**/metadata.json，
     标题同时含「传闻标记」与「断言词」即违规（传闻级标题禁肯定语气）；
  D. 词表与分级覆盖（告警项）。

产出 data/audit/rumor_wording_*.md + rumor_wording_latest.json；硬项 FAIL 退出码 1。

用法：
  python3 scripts/verify_rumor_wording.py
  python3 scripts/verify_rumor_wording.py --quiet
"""

import argparse
import glob
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import rumor_wording as rw  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "output"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"

CST = timezone(timedelta(hours=8))


def check_rules() -> dict:
    """A. 措辞规则自检（硬项）。"""
    bad = rw.selftest()
    return {"item": f"措辞规则自检（{len(rw.RULE_CASES)} 用例）", "ok": not bad,
            "bad": bad,
            "detail": ([f"❌ {len(bad)} 条用例不符：{bad[:3]}"] if bad
                       else ["传闻级标题禁肯定语气／传闻级正文须带措辞／单源级须写来源／"
                             "已确认级可陈述／分级未知不判定 —— 全部符合 ✅"])}


def check_tier_source() -> dict:
    """B. 分级常量一致性（硬项）。"""
    try:
        import fact_card as fc
        src = {fc.CONF_CONFIRMED, fc.CONF_SINGLE, fc.CONF_RUMOR}
    except Exception as e:
        return {"item": "分级常量与 fact_card 同源", "ok": False,
                "detail": [f"无法读取 fact_card 分级常量：{e}"]}
    ok = src == set(rw.TIERS)
    return {"item": "分级常量与 fact_card 同源", "ok": ok,
            "fact_card": sorted(src), "rumor_wording": sorted(rw.TIERS),
            "detail": [f"fact_card：{sorted(src)}　rumor_wording：{sorted(rw.TIERS)}",
                       ("一致 ✅（计划 3.2 三级可信度单一事实来源）" if ok
                        else "❌ 出现两处定义漂移，必须统一")]}


def check_article_titles() -> dict:
    """C. 成稿标题零「传闻当官宣」（硬项；无产物时跳过）。"""
    paths = sorted(glob.glob(str(OUTPUT_DIR / "**" / "metadata.json"), recursive=True))
    if not paths:
        return {"item": "成稿标题零「传闻当官宣」", "ok": True, "skipped": True,
                "detail": ["未找到 output/**/metadata.json，本项跳过"]}
    checked, violations, rumor_titled = 0, [], 0
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
            hint = [k for k in rw.RUMOR_TITLE_HINTS if k in title]
            assertive = [k for k in rw.ASSERTIVE_MARKERS if k in title]
            if hint:
                rumor_titled += 1
            if hint and assertive:
                violations.append({"date": data.get("date"), "title": title,
                                   "rumor_hint": hint[:3], "assertive": assertive[:3]})
    return {"item": "成稿标题零「传闻当官宣」", "ok": not violations, "skipped": False,
            "files": len(paths), "checked": checked, "rumor_titled": rumor_titled,
            "violations": violations[:10],
            "detail": [f"扫描 {len(paths)} 份元数据、{checked} 条成稿标题",
                       f"含传闻标记的标题 {rumor_titled} 条",
                       (f"❌ {len(violations)} 条「传闻当官宣」：{violations[:3]}" if violations
                        else "无传闻与断言混用的标题 ✅"),
                       "（传闻标记： 或/据悉/曝/传闻…；断言词：官宣/确定/敲定/达成/确认…）"]}


def check_vocab() -> dict:
    """D. 词表与分级覆盖（告警项）。"""
    return {"item": "措辞词表与分级覆盖", "advisory": True,
            "ok": bool(rw.HEDGE_MARKERS) and bool(rw.ASSERTIVE_MARKERS) and len(rw.TIERS) == 3,
            "sizes": {"模糊标记": len(rw.HEDGE_MARKERS), "断言词": len(rw.ASSERTIVE_MARKERS),
                      "分级": len(rw.TIERS), "用例": len(rw.RULE_CASES)},
            "detail": [f"模糊标记 {len(rw.HEDGE_MARKERS)} 个／断言词 {len(rw.ASSERTIVE_MARKERS)} 个／"
                       f"分级 {len(rw.TIERS)} 档／用例 {len(rw.RULE_CASES)} 条",
                       "（调用方未携带分级时本模块不判定；分级缺失属流程缺陷，需在装配层补齐）"]}


def verify() -> dict:
    checks = [check_rules(), check_tier_source(), check_article_titles(), check_vocab()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 传闻分级措辞与传闻标题验收报告（计划 4.1）", "",
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
    ap = argparse.ArgumentParser(description="传闻分级措辞与传闻标题验收（计划 4.1）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"rumor_wording_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "rumor_wording_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
