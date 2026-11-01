#!/usr/bin/env python3
"""单事件多切口验收（P0-2，2026-11-01 头部账号对标）。

病根（真实数据）：2026-10-26 金球奖当天，我爱英超（10.3 万粉）发了 15 条，我方 1 条。
不是产能不够，是**一个事件只写一个角度**。头部那 15 条反推出六类切口：
结果 / 冲突 / 人物情绪 / 影响 / 历史对比 / 数据。

四项硬项：

  A. 角度池分类完整（硬项）—— 六类齐全、每类有写作指令、真实金球奖标题能被分到
     ≥4 个不同角度；负向：把角度表清空必须 FAIL
  B. 同事件去重有效（硬项）—— 同句式/同开头必须判重复；不同切口必须放行
  C. 非 S 级事件不触发（硬项）—— ``awareness: A`` 不是 S、未来事件不提前多切口、
     无已核实事实不入选（没事实的多角度＝三遍空话）
  D. 已接入编排（硬项）—— orchestrator 真的调用、有配额上限、标签显式可审计

产出 ``data/audit/event_cluster_verify_<date>.md`` + ``event_cluster_verify_latest.json``；
硬项 FAIL 退出码 1。

用法：python3 scripts/verify_event_cluster.py [--quiet]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import event_cluster  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"
CST = timezone(timedelta(hours=8))

# 真实金球奖标题（我爱英超 2026-10-26 抓取，样本见 docs/benchmark/*.md）
REAL_TITLES = [
    "凯恩拿下2026金球奖，第70届颁奖礼在伦敦落幕",
    "73球！凯恩这个金球奖的数据有多硬",
    "英格兰等了25年，这次终于不用再吵该给谁",
    "凯恩上台第一句谢的是穆里尼奥",
    "这座奖杯之后，拜仁更衣室要变天了",
    "梅西不服？金球奖评选再次引发争议",
]
MIN_ANGLE_SPREAD = 4


def check_angle_pool() -> dict:
    """A. 六类角度齐全、有指令、真实标题能展开成多角度。"""
    detail, ok = [], True

    if len(event_cluster.ANGLES) != 6:
        ok = False
        detail.append(f"❌ 角度只有 {len(event_cluster.ANGLES)} 类（应为 6："
                      f"结果/冲突/人物情绪/影响/历史对比/数据）")
    else:
        detail.append(f"✅ 角度池 6 类：{'、'.join(event_cluster.ANGLES)}")

    missing_hint = [a for a in event_cluster.ANGLES if not event_cluster.angle_hint(a)]
    if missing_hint:
        ok = False
        detail.append(f"❌ 缺写作指令：{'、'.join(missing_hint)}")
    else:
        detail.append("✅ 六类都有写作指令（生成侧据此差异化）")

    bad = [n for n, good in event_cluster.selftest() if not good]
    if bad:
        ok = False
        detail.append(f"❌ 模块自检 {len(bad)} 项未过：{'；'.join(bad[:3])}")
    else:
        detail.append(f"✅ 模块自检 {len(event_cluster.selftest())} 项全过")

    spread = event_cluster.angles_of(REAL_TITLES)
    if len(spread) < MIN_ANGLE_SPREAD:
        ok = False
        detail.append(f"❌ 真实金球奖 6 条标题只分出 {len(spread)} 个角度"
                      f"（{'、'.join(spread) or '无'}），至少要 {MIN_ANGLE_SPREAD} 个——"
                      f"分不开就说明「多切口」落不了地")
    else:
        detail.append(f"✅ 真实 6 条标题分出 {len(spread)} 个角度：{'、'.join(spread)}")

    # 负向验证：角度表清空后，分类必须失效（否则判定走的是别的路径）
    _orig = event_cluster.ANGLE_RULES
    event_cluster.ANGLE_RULES = ()
    try:
        emptied = event_cluster.classify_angle(REAL_TITLES[0])
    finally:
        event_cluster.ANGLE_RULES = _orig
    if emptied is not None:
        ok = False
        detail.append("❌ 负向验证失败：清空角度表后仍能分类")
    else:
        detail.append("✅ 负向验证：清空角度表 → 分类失效（说明真的在读词表）")

    return {"item": "角度池分类完整", "ok": ok, "detail": detail}


def check_dedup() -> dict:
    """B. 同事件多篇的去重有效。"""
    detail, ok = [], True

    same_sig = [
        "没想到罗马主帅这场压力比尤文还大",
        "没想到拉齐奥主帅这场压力比米兰还大",
    ]
    r = event_cluster.distinct_enough(same_sig)
    if r["ok"]:
        ok = False
        detail.append("❌ 同句式两篇未被判重复——「多切口」会退化成「同一篇写两遍」")
    else:
        detail.append(f"✅ 同句式两篇判为重复（{r['dups'][0][0]}）")

    diff = [
        "第94分钟丢球，阿森纳遭读秒绝杀",
        "让3追4！拜仁30分钟连轰4球完成大逆转",
    ]
    r2 = event_cluster.distinct_enough(diff)
    if not r2["ok"]:
        ok = False
        detail.append(f"❌ 不同切口被误判重复：{r2['dups'] or r2['heads']}")
    else:
        detail.append("✅ 不同切口放行（不误杀）")

    same_head = ["金球奖揭晓凯恩夺魁73球", "金球奖揭晓，凯恩这次赢得干净"]
    r3 = event_cluster.distinct_enough(same_head)
    if r3["ok"]:
        ok = False
        detail.append("❌ 同开头两篇未被判重复（句式表覆盖不到的新句式要靠开头兜底）")
    else:
        detail.append("✅ 同开头两篇判为重复")

    empty = event_cluster.distinct_enough([])
    if not empty["ok"] or empty["n"] != 0:
        ok = False
        detail.append("❌ 空样本不该判重复")
    else:
        detail.append("✅ 空样本不误判")

    return {"item": "同事件去重有效", "ok": ok, "detail": detail}


def check_s_only() -> dict:
    """C. 只有 S 级 + 今天 + 有事实的事件才触发。"""
    detail, ok = [], True
    day = "2026-11-01"
    cal = {"nodes": [
        {"id": "s1", "date": day, "level": "S", "label": "S级有事实",
         "facts": ["事实一", "事实二"]},
        {"id": "n1", "date": day, "level": "S", "label": "S级无事实"},
        {"id": "n2", "date": day, "awareness": "A", "label": "A级（认知度≠量级）",
         "facts": ["f"]},
        {"id": "n3", "date": "2026-11-09", "level": "S", "label": "未来S级", "facts": ["f"]},
        {"id": "n4", "date": "2026-10-26", "level": "S", "label": "过去S级", "facts": ["f"]},
    ]}
    got = [e["node_id"] for e in event_cluster.s_events(day, cal)]
    if got != ["s1"]:
        ok = False
        detail.append(f"❌ 入选事件应为 ['s1']，实际 {got}")
    else:
        detail.append("✅ 只选「今天 + S 级 + 有已核实事实」的事件")

    if event_cluster.is_s_event({"awareness": "A"}):
        ok = False
        detail.append("❌ awareness=A 被判成 S 级——认知度与事件量级混用会让每天都变多切口日")
    else:
        detail.append("✅ awareness=A 不算 S 级（两套口径不许混用）")

    if event_cluster.plan_angles({"facts": []}):
        ok = False
        detail.append("❌ 无事实事件仍能产出角度计划（会写出三篇空话）")
    else:
        detail.append("✅ 无事实事件不产出角度（宁可不多切口）")

    plan = event_cluster.plan_angles({"event_key": "node:s1", "facts": ["f"],
                                      "label": "S级", "column": "战术榜单"})
    if len(plan) != event_cluster.MAX_ANGLES_PER_EVENT:
        ok = False
        detail.append(f"❌ 角度计划数量 {len(plan)}，应为 {event_cluster.MAX_ANGLES_PER_EVENT}")
    else:
        detail.append(f"✅ 单事件默认出 {len(plan)} 个角度（上限 {event_cluster.MAX_ANGLES_PER_EVENT}）")

    used = [p["angle"] for p in plan]
    plan2 = event_cluster.plan_angles({"event_key": "node:s1", "facts": ["f"],
                                       "label": "S级"}, used_titles=used)
    if set(used) & {p["angle"] for p in plan2}:
        ok = False
        detail.append("❌ 已用角度被重复分配")
    else:
        detail.append("✅ 已用角度不再分配（跨批不重复）")

    return {"item": "非 S 级事件不触发", "ok": ok, "detail": detail}


def check_wired() -> dict:
    """D. 编排侧真的接了，且有配额与审计。"""
    detail, ok = [], True
    if not ORCHESTRATOR.exists():
        return {"item": "已接入编排", "ok": False, "detail": ["❌ 找不到 orchestrator.py"]}
    text = ORCHESTRATOR.read_text(encoding="utf-8")

    if "def _add_event_angle_articles" not in text:
        ok = False
        detail.append("❌ 缺 _add_event_angle_articles（写了模块但没人调用＝没做）")
    else:
        detail.append("✅ 存在 _add_event_angle_articles")

    if "def generate_node_angle_article" not in text:
        ok = False
        detail.append("❌ 缺 generate_node_angle_article（角度计划没法变成稿子）")
    else:
        detail.append("✅ 存在 generate_node_angle_article")

    if "_add_event_angle_articles(" not in text.split("def _add_event_angle_articles")[1]:
        ok = False
        detail.append("❌ _add_event_angle_articles 未被批次流程调用")
    else:
        detail.append("✅ 批次流程已调用多切口编排")

    # 配额上限：必须有 cap 且在生成前 break，否则会重回「日更 10 篇」
    if "cap" not in text.split("def _add_event_angle_articles")[1][:2600]:
        ok = False
        detail.append("❌ 多切口没有配额上限参数（会超发）")
    else:
        detail.append("✅ 多切口受 cap 约束（占批次配额，不超发）")

    if "一级·确定性·事件多切口" not in text:
        ok = False
        detail.append("❌ 多切口稿没有显式 supply_layer 标签（配比与台账会把它算成未知）")
    else:
        detail.append("✅ 多切口稿带显式供给层标签")

    return {"item": "已接入编排", "ok": ok, "detail": detail}


def verify() -> dict:
    checks = [check_angle_pool(), check_dedup(), check_s_only(), check_wired()]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in checks), "checks": checks,
            "hard_failed": [c["item"] for c in checks if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 单事件多切口验收（P0-2）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**",
         "- 依据：金球奖当天我爱英超 15 条 vs 我方 1 条（2026-11-01 对标）", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}：{'✅' if c['ok'] else '❌'}")
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="单事件多切口验收（P0-2）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()
    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"event_cluster_verify_{stamp}.md").write_text(
        render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "event_cluster_verify_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
