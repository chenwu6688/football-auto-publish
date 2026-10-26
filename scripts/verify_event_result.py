#!/usr/bin/env python3
"""揭晓型事件「结果稿」通道验收（计划 11.1 时效闸门 / 13.4 原始信源纪律）。

病根：金球奖这类事件过了揭晓时刻，赛前稿（谁该拿）立刻变旧闻，而结果事实
（谁拿了）在揭晓前根本不存在 —— 预生成拿不到，只能当时去抓、去对。
抓来就写＝把外媒预测当结论（2026-10-26 10:28 懂球帝就有一条《【CBS Sports】姆巴佩夺金球》，
而开奖在 10-27 03:00）。所以这条通道的价值全在**五道闸门**，不在于能写稿。

本脚本钉死六件事（全部硬项，不带 advisory）：
  A. 模块在位——event_result 提供 collect/verify/resolve/extract_winner；
  B. 时间闸门——揭晓前发布的结果句必须拒（用真实陷阱标题做样本）；
  C. 多源门槛——单源必拒、同站两条仍算单源、两源冲突必拒；
  D. 节点配置——有 reveal_at 的节点必须有 result_keywords，否则结果期空转；
  E. 生成侧接入——orchestrator 必须真调 event_result.resolve 与
     generate_event_result_article，且「不点名获奖者」的稿子必须被丢弃；
  F. 审计落盘——判定不通过也要留证据，否则「为什么没发」无从复盘。

产出 data/audit/event_result_<date>.md + event_result_latest.json；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_event_result.py [--quiet]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import event_result as er  # noqa: E402
import deterministic_supply as ds  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"
CST = timezone(timedelta(hours=8))

# 用于负向验证的「揭晓时刻」：2026 金球奖（伦敦 20:00 = 北京次日 03:00）
REVEAL = datetime(2026, 10, 27, 3, 0, tzinfo=CST)
KW = ["金球奖", "金球"]


def _m(source, title, pub="2026-10-27 05:10:00", url=""):
    return {"source": source, "title": title, "published_at": pub, "url": url, "text": ""}


def check_module_api() -> dict:
    """A. 模块接口齐备且五道闸门都有对应实现。"""
    missing = [n for n in ("collect_mentions", "verify", "resolve", "extract_winner",
                           "is_result_sentence", "audit")
               if not hasattr(er, n)]
    src = Path(er.__file__).read_text(encoding="utf-8")
    gates = {
        "时间闸门": "早于揭晓",
        "预测词拦截": "含预测/悬念标记",
        "多源门槛": "独立源不足",
        "冲突判定": "多源指认不一致",
        "审计": "AUDIT_PATH",
    }
    gone = [k for k, v in gates.items() if v not in src]
    ok = not missing and not gone
    detail = []
    if missing:
        detail.append(f"❌ event_result 缺接口：{missing}")
    if gone:
        detail.append(f"❌ 五道闸门缺实现痕迹：{gone}")
    if ok:
        detail.append(f"✅ 接口齐备（6 个）；五道闸门均在位：{'、'.join(gates)}")
    return {"item": "A. 模块与闸门在位", "ok": ok, "detail": detail}


def check_time_gate() -> dict:
    """B. 揭晓前的结果句必须拒——这是整条通道最关键的一刀。"""
    before = er.verify([
        _m("dongqiudi", "【CBS Sports】姆巴佩夺金球", pub="2026-10-26 10:28:14"),
        _m("zhibo8", "姆巴佩夺金球", pub="2026-10-26 11:00:00"),
    ], keywords=KW, reveal_at=REVEAL)
    after = er.verify([
        _m("dongqiudi", "姆巴佩夺金球", pub="2026-10-27 05:10:00"),
        _m("zhibo8", "官方：姆巴佩荣膺2026金球奖", pub="2026-10-27 05:20:00"),
    ], keywords=KW, reveal_at=REVEAL)
    ok = (not before["ok"]) and after["ok"] and after["winner"] == "姆巴佩"
    detail = [f"{'✅' if not before['ok'] else '❌'} 揭晓前发布的结果句被判拒"
              f"（实际 ok={before['ok']} winner={before['winner']!r}）",
              f"{'✅' if after['ok'] else '❌'} 同标题改到揭晓后即通过"
              f"（实际 ok={after['ok']} winner={after['winner']!r}）"]
    return {"item": "B. 时间闸门（揭晓前后必须分得开）", "ok": ok, "detail": detail}


def check_multi_source() -> dict:
    """C. 多源门槛：单源≠交叉验证。"""
    single = er.verify([_m("zhibo8", "官方：登贝莱荣膺2026金球奖")],
                       keywords=KW, reveal_at=REVEAL)
    same_src = er.verify([_m("zhibo8", "登贝莱获得2026年金球奖"),
                          _m("zhibo8", "登贝莱夺金球")],
                         keywords=KW, reveal_at=REVEAL)
    conflict = er.verify([_m("zhibo8", "官方：登贝莱荣膺2026金球奖"),
                          _m("dongqiudi", "登贝莱当选2026金球奖"),
                          _m("sina", "2026金球奖揭晓：亚马尔当选"),
                          _m("netease", "金球奖得主亚马尔")],
                         keywords=KW, reveal_at=REVEAL)
    two = er.verify([_m("zhibo8", "官方：登贝莱荣膺2026金球奖"),
                     _m("dongqiudi", "登贝莱当选2026金球奖")],
                    keywords=KW, reveal_at=REVEAL)
    ok = (not single["ok"]) and (not same_src["ok"]) and (not conflict["ok"]) and two["ok"]
    detail = [
        f"{'✅' if not single['ok'] else '❌'} 单源拒发",
        f"{'✅' if not same_src['ok'] else '❌'} 同站两条仍按单源处理",
        f"{'✅' if not conflict['ok'] else '❌'} 两源指认不同人判冲突（{conflict['reason'][:30]}…）",
        f"{'✅' if two['ok'] else '❌'} 两源一致才放行（winner={two['winner']}）",
    ]
    return {"item": "C. 多源门槛（单源/同源/冲突/一致）", "ok": ok, "detail": detail}


def check_node_config() -> dict:
    """D. 有 reveal_at 的节点必须配 result_keywords，否则结果期永远空转。"""
    cal = ds.load_calendar()
    bad = []
    n_timed = 0
    for n in cal.get("nodes", []) or []:
        if not n.get("reveal_at"):
            continue        # 非揭晓型事件，不需要结果稿
        n_timed += 1
        kws = [str(k).strip() for k in (n.get("result_keywords") or []) if str(k).strip()]
        if not kws:
            bad.append(f"{n.get('id')}：有 reveal_at 但无 result_keywords（结果期无法取证）")
        if not n.get("facts"):
            bad.append(f"{n.get('id')}：无 facts（结果稿只能写「XX 获奖了」，撑不住正文）")
        try:
            float(n.get("result_window_hours", ds.DEFAULT_RESULT_WINDOW_HOURS))
        except (TypeError, ValueError):
            bad.append(f"{n.get('id')}：result_window_hours 非数值")
    ok = not bad
    detail = [f"❌ {b}" for b in bad] if bad else [
        f"✅ {n_timed} 个揭晓型节点均配置了 result_keywords / facts / 结果窗"]
    return {"item": "D. 揭晓型节点配置完整", "ok": ok, "detail": detail}


def check_generation_wired() -> dict:
    """E. 生成侧真接入：只 import 不调用＝白做。"""
    if not ORCHESTRATOR.exists():
        return {"item": "E. 生成侧接入", "ok": False,
                "detail": ["❌ 找不到 orchestrator.py"]}
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    bad = []
    if "event_result.resolve(" not in text:
        bad.append("orchestrator 未调用 event_result.resolve（取证环节缺失）")
    if "generate_event_result_article(" not in text:
        bad.append("orchestrator 未调用 generate_event_result_article")
    if "deterministic_supply.result_topics(" not in text:
        bad.append("orchestrator 未取用 result_topics（结果期选题不会进入批次）")
    # 「不点名获奖者就丢弃」必须存在：这是结果稿唯一的硬事实校验
    if "结果稿正文未出现获奖者" not in text:
        bad.append("缺「正文必须点名获奖者」硬校验（结果稿最容易被换人）")
    # 去重：同一场颁奖不能写两遍
    if "_event_result_sent(" not in text:
        bad.append("缺 event_result_key 跨批去重（同场颁奖会被写两遍）")
    if "_event_result_sent(" not in text or "event_result_key" not in text:
        bad.append("event_result_key 未落 metadata（去重依据会丢）")
    ok = not bad
    detail = [f"❌ {b}" for b in bad] if bad else [
        "✅ 取证/生成/去重三处均已接入 orchestrator",
        "✅ 含「正文必须点名获奖者」硬校验"]
    return {"item": "E. 生成侧接入完整", "ok": ok, "detail": detail}


def check_audit_written() -> dict:
    """F. 判定不通过也必须留审计——「为什么没发」比「发了什么」更需要复盘。"""
    before = er.AUDIT_PATH.read_text(encoding="utf-8") if er.AUDIT_PATH.exists() else None
    res = {"ok": False, "winner": "", "sources": [], "kept": [], "dropped": [],
           "keyword_hits": 0, "reason": "验收脚本自检：故意构造未通过"}
    er.audit(res, node_id="verify_selftest", keywords=KW, reveal_at=REVEAL,
             errors=["selftest"])
    ok = False
    detail = []
    try:
        payload = json.loads(er.AUDIT_PATH.read_text(encoding="utf-8"))
        need = ("checked_at", "ok", "reason", "kept", "dropped", "source_errors")
        missing = [k for k in need if k not in payload]
        ok = (not missing) and payload.get("ok") is False and payload.get("reason")
        if missing:
            detail.append(f"❌ 审计缺字段：{missing}")
        else:
            detail.append("✅ 未通过判定也落盘，含原因/证据/抓取错误三块")
            detail.append(f"   落盘路径：{er.AUDIT_PATH.relative_to(PROJECT_ROOT)}")
    except Exception as e:
        detail.append(f"❌ 审计不可读：{e}")
    # 还原现场：验收脚本不该污染真实审计产物
    if before is not None:
        er.AUDIT_PATH.write_text(before, encoding="utf-8")
    elif er.AUDIT_PATH.exists():
        er.AUDIT_PATH.unlink()
    return {"item": "F. 未通过也落审计", "ok": ok, "detail": detail}


def verify() -> dict:
    checks = [check_module_api(), check_time_gate(), check_multi_source(),
              check_node_config(), check_generation_wired(), check_audit_written()]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in checks), "checks": checks,
            "hard_failed": [c["item"] for c in checks if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 事件结果稿通道验收（计划 11.1 / 13.4）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}：{'✅' if c['ok'] else '❌'}")
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    L += ["## 判定口径（不发比发错好）", "",
          "| 场景 | 处理 |", "|---|---|",
          "| 揭晓前发布的结果句（外媒预测/前瞻） | 拒发 |",
          "| 只有 1 个站点指认 | 拒发（可能只是互相转载） |",
          "| 同一站点两条 | 仍按 1 个源计 |",
          "| 两个达标源指认不同的人 | 拒发（事实冲突） |",
          "| 抽不出姓名 / 发布时间不明 | 拒发（不猜） |",
          "| ≥2 个独立站点一致 | 放行，姓名原文照抄 |", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="事件结果稿通道验收")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()
    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"event_result_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "event_result_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
