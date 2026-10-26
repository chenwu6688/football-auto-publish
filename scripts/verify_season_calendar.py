#!/usr/bin/env python3
"""赛季节点日历 · 可信度验收（2026-10-26 金球奖节点暴露的缺口）。

起因：把金球奖写进 config/season_calendar.yaml 时发现三件事没有验收：
  1. **事实可追**——节点稿要写具体数字（36 球、61 球），但日历里没有任何机制
     保证这些数字有来源；查证过程中就撞到一篇把「1996 年萨默尔」算成
     「44 年金球荒」的稿子（实际 30 年）。单源事实不可信，必须落到
     fact_source_url 上，缺来源的不许进 facts。
  2. **揭晓时刻**——节点日只到「天」，但颁奖礼 20:00 伦敦 = 次日 03:00 北京。
     没有 reveal_at 就会出现「结果已出还在写谁该拿」（旧闻新发红线）。
  3. **生成侧真接入**——facts 存进日历但不注入提示词，等于白存。

本脚本四项硬项：
  A. 日历结构合法（id 唯一 / 日期可解析 / 认知度 A|B|C / 板块四选一 / 无前瞻禁用词）
  B. 有 facts 必须有 fact_source_url（负向：塞一条无来源的假节点必须被抓到）
  C. reveal_at 可解析、且节点日不晚于揭晓日；过了 reveal_at 该节点必须不再生成
  D. 生成侧真接入（pregen 把 facts 注入提示词 + 模板含 {facts_block} 占位）

产出 data/audit/season_calendar_<date>.md + season_calendar_latest.json；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_season_calendar.py [--quiet]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import deterministic_supply as ds  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
PREGN = PROJECT_ROOT / "scripts" / "pregen_deterministic.py"
CST = timezone(timedelta(hours=8))

NODE_COLUMNS = ("转会动态", "战术榜单", "人物故事", "中国足球")
BANNED_TEMPLATE_WORDS = ("前瞻", "预览", "展望", "赛前预测", "首发预测")


def check_structure(cal: dict) -> dict:
    """A. 日历结构合法。"""
    detail, bad = [], []
    nodes = cal.get("nodes") or []
    ids = [n.get("id") for n in nodes]
    dup = {i for i in ids if i and ids.count(i) > 1}
    if dup:
        bad.append(f"id 重复：{sorted(dup)}")
    for n in nodes:
        nid = n.get("id") or "(无 id)"
        if not n.get("id"):
            bad.append("存在无 id 的节点")
        if ds._as_date(n.get("date")) is None:
            bad.append(f"{nid}：date 无法解析「{n.get('date')}」")
        if ds.awareness_gate.classify(n.get("awareness")) not in ("A", "B", "C"):
            bad.append(f"{nid}：awareness「{n.get('awareness')}」非法（只允许 A/B/C）")
        if (n.get("column") or "") not in NODE_COLUMNS:
            bad.append(f"{nid}：column「{n.get('column')}」不在四板块内")
        t = n.get("template") or ""
        if not t.strip():
            bad.append(f"{nid}：template 为空")
        for w in BANNED_TEMPLATE_WORDS:
            if w in t:
                bad.append(f"{nid}：模板含前瞻禁用词「{w}」（计划 7.1）")
    detail.append(f"节点 {len(nodes)} 个，结构检查 {len(bad)} 处问题")
    if bad:
        detail += [f"❌ {b}" for b in bad[:10]]
    else:
        detail.append("✅ id 唯一、日期可解析、认知度与板块合法、无前瞻禁用词")
    return {"item": "日历结构合法（A）", "ok": not bad, "detail": detail}


def check_facts_traceable(cal: dict) -> dict:
    """B. 有 facts 必须有来源；并做负向验证（无来源的必须被抓到）。"""
    detail, bad = [], []
    for n in cal.get("nodes") or []:
        if not n.get("facts"):
            continue
        url = n.get("fact_source_url") or ""
        if not url.startswith("http"):
            bad.append(f"{n.get('id')}：有 {len(n['facts'])} 条 facts 但缺 fact_source_url")
        if any(not str(f).strip() for f in n["facts"]):
            bad.append(f"{n.get('id')}：facts 含空白条目（会被拼进提示词）")
    n_with = sum(1 for n in (cal.get("nodes") or []) if n.get("facts"))

    # 负向验证：伪造一条「有 facts、无来源」的节点，检查器必须判它不合法
    fake = {"nodes": [{"id": "fake", "type": "赛季节点", "label": "假", "date": "2026-12-01",
                       "column": "人物故事", "awareness": "A", "template": "t",
                       "facts": ["某球员上赛季 99 球"]}]}
    neg_caught = not check_facts_traceable_only(fake)
    detail.append(f"带 facts 的节点 {n_with} 个；负向验证{'通过（无来源被抓到）✅' if neg_caught else '失败 ❌'}")
    if bad:
        detail += [f"❌ {b}" for b in bad]
    elif n_with:
        detail.append("✅ 每个 facts 节点都能追到来源链接")
    else:
        detail.append("⚠️ 当前日历无 facts 节点（金球奖节点应命中）")
    return {"item": "事实可追（B）", "ok": not bad and neg_caught, "detail": detail}


def check_facts_traceable_only(cal: dict) -> bool:
    """B 的纯判定版（供负向验证使用，不打印）。"""
    for n in cal.get("nodes") or []:
        if n.get("facts") and not (n.get("fact_source_url") or "").startswith("http"):
            return False
    return True


def check_reveal_window(cal: dict) -> dict:
    """C. reveal_at 合法，且过了揭晓时刻不再生成。"""
    detail, bad = [], []
    timed = [n for n in (cal.get("nodes") or []) if n.get("reveal_at")]
    for n in timed:
        rv = ds._as_dt(n.get("reveal_at"))
        nd = ds._as_date(n.get("date"))
        if rv is None:
            bad.append(f"{n.get('id')}：reveal_at 无法解析「{n.get('reveal_at')}」")
            continue
        if nd and rv.date() < nd:
            bad.append(f"{n.get('id')}：reveal_at({rv.date()}) 早于节点日({nd})，窗口矛盾")
        # 行为验证：把时钟推到揭晓后 1 分钟，该节点必须从选题里消失
        after = rv + timedelta(minutes=1)
        got = ds.build_node_topics(nd or rv.date(), horizon_days=7, calendar={"nodes": [n]},
                                   now=after)
        if got:
            bad.append(f"{n.get('id')}：过了 reveal_at 仍在生成（旧闻新发）")

    detail.append(f"带 reveal_at 的节点 {len(timed)} 个")
    if timed:
        for n in timed:
            detail.append(f"· {n.get('id')}：节点日 {n.get('date')} → 揭晓 {n.get('reveal_at')}（CST）")
    if bad:
        detail += [f"❌ {b}" for b in bad]
    else:
        detail.append("✅ 揭晓时刻可解析、窗口不矛盾、过点即停止生成")
    return {"item": "揭晓时刻守卫（C）", "ok": not bad, "detail": detail}


def check_pregen_wired() -> dict:
    """D. 生成侧真接入：facts 必须真的进提示词。"""
    detail, bad = [], []
    if not PREGN.exists():
        return {"item": "生成侧真接入（D）", "ok": False,
                "detail": ["❌ scripts/pregen_deterministic.py 不存在"]}
    text = PREGN.read_text(encoding="utf-8")
    if "{facts_block}" not in text:
        bad.append("提示词模板缺 {facts_block} 占位")
    if "facts_block=_facts_block(" not in text:
        bad.append("_make_one 未调用 _facts_block（facts 存了但没注入）")
    if "只能用下面这些" not in text:
        bad.append("事实区块缺「只能用这些」边界（模型会自行补数字）")
    if "不得改写" not in text:
        bad.append("缺「不得改写」约束（36 球会被写成别的数）")
    if bad:
        detail += [f"❌ {b}" for b in bad]
    else:
        detail.append("✅ 模板含 {facts_block}、生成侧调用 _facts_block、边界与禁止改写的约束都在")
    return {"item": "生成侧真接入（D）", "ok": not bad, "detail": detail}


def verify(cal: dict | None = None) -> dict:
    cal = cal if cal is not None else ds.load_calendar()
    checks = [check_structure(cal), check_facts_traceable(cal),
              check_reveal_window(cal), check_pregen_wired()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "nodes": len(cal.get("nodes") or []),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 赛季节点日历 · 可信度验收", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 节点总数：{rep['nodes']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}：{'✅' if c['ok'] else '❌'}")
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="赛季节点日历可信度验收")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()
    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"season_calendar_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "season_calendar_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
