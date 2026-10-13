#!/usr/bin/env python3
"""ranker 社媒信号接入规则打分验收。

背景（用户 2026-10-13）：规则打分是 LLM 不可用 / 单日 token 超限时的降级路径，
此前却完全没用到社媒信号（社媒讨论量），刚接入的真实抖音热度白采。
本次让「社媒讨论量」真正进入五维（→ 人物知名度），并严守红线：不新增第六维、
无信号（缺/0）时五维分与改动前逐维一致、不污染真实台账。

注：搜索指数实测 97% 恒为 100（百度联想饱和），无区分度，已从维度加分中移除。

硬项（不达标退出码 1）
  A. 强信号候选在规则排序中排在无信号候选之前
  B. 无信号候选的五维分不被缺信号抬高或打压（与手算基线逐维一致）
  C. signals 传空表时 rank_candidates(use_llm=False) 不抛异常且 mode == "rule"

告警项：无

用法：
  python3 scripts/verify_ranker_signals.py [--quiet]
产出：data/audit/ranker_signals_<date>.md + ranker_signals_latest.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ranker  # noqa: E402

CST = timezone(timedelta(hours=8))
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
NOW = datetime(2026, 10, 12, 20, 0, tzinfo=CST)

# 验收用的确定性认知度表（覆盖真实 entity_map，避免环境差异）
_AWARENESS = {"曼城": "A", "内马尔": "B", "科隆": "C"}


def _card(cid, subj, action="转会", occurred="2026-10-12", angles=None, life=None):
    return {"id": cid, "主体": subj, "动作": action, "数值": None,
            "发生时间": occurred,
            "来源列表": [{"来源名": "zhibo8", "地址": "http://z"}],
            "可信度": "单源", "可用角度": angles or ["角度1", "角度2", "角度3"],
            "生命周期": life or "进行中"}


def _install_isolated_env():
    """把所有会落 data/ 的路径指到 tempfile；真实台账/缓存零污染。"""
    ranker._awareness_cache = dict(_AWARENESS)
    ranker.CACHE_PATH = Path(tempfile.mktemp(suffix=".json"))


def check_strong_ranks_first() -> dict:
    """硬项 A：两个除信号外完全相同的候选，强信号者排在前。"""
    cands = [{"card": _card("c1", "内马尔", angles=["角度1"]), "板块": "战术榜单"},
             {"card": _card("c2", "内马尔", angles=["角度1"]), "板块": "战术榜单"}]
    signals = {"c1": {"社媒讨论量": 200_000, "同题报道条数": 0}}
    try:
        res = ranker.rank_candidates(cands, now=NOW, use_llm=False, signals=signals)
    except Exception as e:                       # 抛异常即硬项失败
        return {"item": "强信号优先（规则排序）", "ok": False, "advisory": False,
                "detail": [f"❌ rank_candidates 抛异常：{e}"]}
    top = res["ranking"][0]["id"] if res["ranking"] else None
    ok = (res["mode"] == "rule" and top == "c1")
    return {"item": "强信号优先（规则排序）", "ok": bool(ok), "advisory": False,
            "detail": [f"排序：{[(r['id'], r['总分']) for r in res['ranking']]}；"
                       f"首位 {top}（应为 c1）{'✅' if ok else '❌'}"]}


def check_no_signal_baseline() -> dict:
    """硬项 B：无信号候选五维分与手算基线逐维一致（不被抬高/打压）。"""
    c = _card("m", "曼城")                        # 默认 3 角度；发生时间=NOW 当天
    cand = {"card": c}
    ranker.l2_enrich([cand], {})                 # 缺失信号补 0
    baseline = {"冲突度": 3, "人物知名度": 5, "时效性": 5,
                "话题延展性": 5, "粉丝画像匹配度": 3}
    got = ranker.rule_score(c, cand, NOW)
    ok = (got == baseline)
    detail = [f"无信号五维：{got}",
              f"手算基线：{baseline}",
              f"{'逐维一致 ✅' if ok else '不一致 ❌'}"]
    return {"item": "无信号基线不变（五维逐维一致）", "ok": bool(ok), "advisory": False,
            "detail": detail}


def check_empty_signals_no_crash() -> dict:
    """硬项 C：signals 传空表不抛异常且 mode == rule。"""
    cands = [{"card": _card("e1", "曼城"), "板块": "战术榜单"},
             {"card": _card("e2", "内马尔"), "板块": "战术榜单"}]
    try:
        res = ranker.rank_candidates(cands, now=NOW, use_llm=False, signals={})
    except Exception as e:
        return {"item": "空信号表不崩溃且降级 rule", "ok": False, "advisory": False,
                "detail": [f"❌ rank_candidates(signals={{}}) 抛异常：{e}"]}
    ok = (res["mode"] == "rule" and len(res["ranking"]) == 2)
    detail = [f"mode={res['mode']}（应为 rule）；ranking 条数={len(res['ranking'])} "
              f"（应为 2）{'✅' if ok else '❌'}"]
    return {"item": "空信号表不崩溃且降级 rule", "ok": bool(ok), "advisory": False,
            "detail": detail}


def verify() -> dict:
    _install_isolated_env()
    hard = [check_strong_ranks_first(), check_no_signal_baseline(),
            check_empty_signals_no_crash()]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": hard,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# ranker 社媒信号接入规则打分验收报告", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="ranker 社媒信号接入规则打分验收")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"ranker_signals_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "ranker_signals_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
