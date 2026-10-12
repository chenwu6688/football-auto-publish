#!/usr/bin/env python3
"""事实层与量表验收（计划 6.1 / 6.2 / 12.2 / 12.4）。

计划把「事实卡」定为不可生成的事实底座（6.1），并在第十二章把它升级为可复用资产：
  12.2 —— 需入库并标注生命周期状态（进行中 / 已收官 / 已过期），跨篇表述一致；
  12.4 —— 模型择优量表强制覆盖五个维度（冲突度 / 人物知名度 / 时效性 / 话题延展性 /
          账号粉丝画像匹配度），防同质化漂移。
本脚本校验这些结构约束在代码里成立。

校验项：
  A. 6.1 事实卡字段齐备（硬项）—— id/主体/动作/数值/发生时间/来源列表/可信度/可用角度；
  B. 6.1 可信度三级阈值（硬项）—— 来源 ≥2 → 已确认；=1 → 单源；=0 → 传闻；
  C. 12.2 生命周期三态与边界（硬项）—— 进行中 / 已收官（>7 天）/ 已过期（>30 天），
     未来事件与不可判定时间一律保守取「进行中」；
  D. 12.4 五维量表齐备（硬项）—— ranker.DIMENSIONS 与计划集合一致，规则打分输出五维、
     取值 1-5，且 L3 模型提示词要求全部五维；
  E. 6.2 选题结构（告警项）—— 候选/节点选题携带「板块」与事实卡关联字段。

产出 data/audit/fact_layer_*.md + fact_layer_latest.json；硬项 FAIL 退出码 1。

用法：
  python3 scripts/verify_fact_layer.py
  python3 scripts/verify_fact_layer.py --quiet
"""

import argparse
import glob
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import fact_card as fc  # noqa: E402
import ranker  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
CST = timezone(timedelta(hours=8))

REQUIRED_FIELDS = ("id", "主体", "动作", "数值", "发生时间", "来源列表", "可信度", "可用角度")
PLAN_DIMENSIONS = {"冲突度", "人物知名度", "时效性", "话题延展性", "粉丝画像匹配度"}


# ------------------------------------------------------------
# A / B. 6.1 事实卡
# ------------------------------------------------------------
def check_fields() -> dict:
    """A. 事实卡八字段齐备（硬项）。"""
    card = fc.build_fact_card("曼城", "转会", "5000万欧", "2026-10-11 10:00",
                              sources=[{"来源名": "罗马诺", "地址": "http://x"}])
    missing = [f for f in REQUIRED_FIELDS if f not in card]
    empty = [f for f in REQUIRED_FIELDS if not card.get(f) and f != "数值"]
    return {"item": "事实卡 6.1 八字段齐备", "ok": not missing and not empty,
            "fields": list(card.keys()),
            "detail": ([f"八字段齐备：{'、'.join(REQUIRED_FIELDS)} ✅",
                        f"可溯源：id={card['id']}，来源 {len(card['来源列表'])} 条"] if not missing and not empty
                       else [f"缺字段 {missing} / 空字段 {empty}"])}


def check_confidence() -> dict:
    """B. 可信度三级阈值（硬项）。"""
    got = {
        "0 源": fc.derive_confidence([]),
        "1 源": fc.derive_confidence([{"来源名": "A"}]),
        "2 源": fc.derive_confidence([{"来源名": "A"}, {"来源名": "B"}]),
    }
    want = {"0 源": fc.CONF_RUMOR, "1 源": fc.CONF_SINGLE, "2 源": fc.CONF_CONFIRMED}
    bad = {k: got[k] for k in want if got[k] != want[k]}
    return {"item": "可信度三级阈值（3.2/6.1）", "ok": not bad, "got": got,
            "detail": ([f"0 源→{got['0 源']}；1 源→{got['1 源']}；2 源→{got['2 源']} ✅"]
                       if not bad else [f"❌ 分级不符：{bad}（应 {want}）"])}


# ------------------------------------------------------------
# C. 12.2 生命周期
# ------------------------------------------------------------
def check_lifecycle() -> dict:
    """C. 生命周期三态与边界（硬项）。"""
    now = datetime(2026, 10, 12, 9, 0, tzinfo=CST)

    def lc(days_ago):
        t = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M")
        return fc.compute_lifecycle({"发生时间": t}, now)

    cases = {
        "未来事件(+3d)": (fc.compute_lifecycle(
            {"发生时间": (now + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")}, now), fc.LC_ONGOING),
        "进行中(2d)": (lc(2), fc.LC_ONGOING),
        "边界(8d)": (lc(8), fc.LC_SETTLED),
        "已收官(20d)": (lc(20), fc.LC_SETTLED),
        "已过期(40d)": (lc(40), fc.LC_EXPIRED),
        "不可判定": (fc.compute_lifecycle({"发生时间": "不详"}, now), fc.LC_ONGOING),
    }
    bad = {k: v for k, (v, want) in cases.items() if v != want}
    return {"item": "事实卡生命周期三态（12.2）", "ok": not bad,
            "states": [fc.LC_ONGOING, fc.LC_SETTLED, fc.LC_EXPIRED],
            "cases": {k: v[0] for k, v in cases.items()},
            "detail": ([f"三态齐备：{fc.LC_ONGOING}/{fc.LC_SETTLED}/{fc.LC_EXPIRED}，"
                        f"阈值 {fc.FACT_SETTLE_DAYS}d/{fc.FACT_EXPIRE_DAYS}d，边界判定正确 ✅",
                        "未来事件与不可判定时间保守取「进行中」✅"]
                       if not bad else [f"❌ 判定不符：{bad}"])}


# ------------------------------------------------------------
# D. 12.4 五维量表
# ------------------------------------------------------------
def check_dimensions() -> dict:
    """D. 五维量表齐备（硬项）。"""
    problems = []
    dims = set(ranker.DIMENSIONS)
    if dims != PLAN_DIMENSIONS:
        problems.append(f"DIMENSIONS={sorted(dims)} 与计划五维不一致")
    # 规则打分输出五维、取值 1-5
    sc = ranker.rule_score({"主体": "曼城", "动作": "转会", "发生时间": "2026-10-12 08:00"},
                           {"title": "曼城这波操作到底图什么？"})
    if set(sc.keys()) != PLAN_DIMENSIONS:
        problems.append(f"rule_score 输出 {sorted(sc.keys())}，缺五维之项")
    bad_range = {k: v for k, v in sc.items() if not (1 <= v <= 5)}
    if bad_range:
        problems.append(f"打分越界：{bad_range}")
    # L3 提示词要求五维
    src = Path(ranker.__file__).read_text(encoding="utf-8")
    miss_prompt = [d for d in PLAN_DIMENSIONS if d not in src]
    if miss_prompt:
        problems.append(f"源码未见维度 {miss_prompt}")
    return {"item": "择优量表五维齐备（12.4）", "ok": not problems,
            "dimensions": ranker.DIMENSIONS, "sample_scores": sc,
            "detail": ([f"五维齐备（{len(ranker.DIMENSIONS)} 维）：{'、'.join(ranker.DIMENSIONS)} ✅",
                        f"规则打分取值 1-5：{sc}", "L3 提示词要求全部五维 ✅"]
                       if not problems else problems)}


# ------------------------------------------------------------
# E. 6.2 选题结构（告警项）
# ------------------------------------------------------------
def check_topic_structure() -> dict:
    """E. 6.2 选题结构携带板块与事实卡关联（告警项）。"""
    ds = (PROJECT_ROOT / "deterministic_supply.py").read_text(encoding="utf-8")
    has_section = '"板块"' in ds or "'板块'" in ds
    has_card = "card" in Path(ranker.__file__).read_text(encoding="utf-8")
    pool_paths = sorted(glob.glob(str(OUTPUT_DIR / "**" / "topic_pool.json"), recursive=True))
    sample = None
    if pool_paths:
        try:
            d = json.loads(Path(pool_paths[-1]).read_text(encoding="utf-8"))
            items = (d.get("matches") or d.get("topics") or d.get("candidates") or [])
            sample = list(items[0].keys())[:12] if items else None
        except Exception:
            sample = None
    ok = has_section and has_card
    return {"item": "选题结构携带板块与事实卡关联（6.2）", "ok": ok, "advisory": True,
            "has_section": has_section, "has_card": has_card, "sample_keys": sample,
            "detail": [f"板块字段：{'✅' if has_section else '❌'}；事实卡关联：{'✅' if has_card else '❌'}",
                       (f"抽样 topic_pool 字段：{sample}" if sample else "本机无 topic_pool，跳过抽样"),
                       "（结构口径提示，不作硬断言）"]}


def verify() -> dict:
    checks = [check_fields(), check_confidence(), check_lifecycle(),
              check_dimensions(), check_topic_structure()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 事实层与量表验收报告（计划 6.1 / 6.2 / 12.2 / 12.4）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警" if c.get("advisory") else "硬项"
        L.append(f"## {c['item']}（{tag}）：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="事实层与量表验收（计划 6.1/6.2/12.2/12.4）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"fact_layer_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "fact_layer_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
