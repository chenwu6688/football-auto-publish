#!/usr/bin/env python3
"""数据源与信源清单验收（计划 13.2 / 13.4）。

计划第十三章解决三个前置问题，其中两个是「源从哪来」：
  13.2 —— 四源分工：football-data.org（主力）/ API-Football（补中超·亚冠·国足·伤病转会）/
          聚合数据 juhe.cn（中文校验与兜底）/ 人工赛历（国足·中超）；
          并明确**不接入商业数据商**，媒体站接口无授权只能用于提取实体中文名。
  13.4 —— 信源前移：直接订阅原始信源（罗马诺 / 天空 / 迪马济奥 / 阿斯 / 马卡 / 图片报 /
          队报 / 官方渠道），但原始信源**只作信号**，正文仍按事实卡生成。

校验项：
  A. 13.2 四源分工齐备（硬项）—— 四源各自有落地物；
  B. 13.4 原始信源清单齐备（硬项）—— source_watchlist.yaml 覆盖计划列举的信源；
  C. 信源纪律（硬项）—— source_watch 只驱动 L2 热度信号，未被抽取器/生成器直接引用；
  D. 不接入商业数据商（告警项）—— 纳米/火星/创冰/同道伟业 零引用；
  E. 无授权媒体接口边界（告警项）—— 懂球帝/直播吧/虎扑/雷速 仅作素材与实体名提取。

产出 data/audit/source_roster_*.md + source_roster_latest.json；硬项 FAIL 退出码 1。

用法：
  python3 scripts/verify_source_roster.py
  python3 scripts/verify_source_roster.py --quiet
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

# 13.4 计划列举的原始信源（source_watchlist.yaml 的 name）
PLAN_SOURCES = ("罗马诺", "天空体育", "德国天空体育", "迪马济奥", "米兰体育报", "全市场",
                "阿斯报", "马卡报", "图片报", "队报", "俱乐部官方")

COMMERCIAL_VENDORS = ("纳米数据", "火星数据", "创冰", "同道伟业")
UNAUTHORIZED_MEDIA = ("懂球帝", "直播吧", "虎扑", "雷速")


def _read(rel: str) -> str:
    try:
        return (PROJECT_ROOT / rel).read_text(encoding="utf-8")
    except Exception:
        return ""


def _exists(rel: str) -> bool:
    return (PROJECT_ROOT / rel).exists()


def _grep_files(terms, exts=("*.py", "*.yaml")):
    """返回 {term: [命中文件相对路径]}（排除 tests/ 与本脚本自身）。"""
    import glob
    hits = {t: [] for t in terms}
    self_name = Path(__file__).name
    for ext in exts:
        for p in glob.glob(str(PROJECT_ROOT / "**" / ext), recursive=True):
            if "/tests/" in p or "/.git/" in p or Path(p).name == self_name:
                continue
            try:
                txt = Path(p).read_text(encoding="utf-8")
            except Exception:
                continue
            for t in terms:
                if t in txt:
                    hits[t].append(str(Path(p).relative_to(PROJECT_ROOT)))
    return hits


# ------------------------------------------------------------
# A. 13.2 四源分工
# ------------------------------------------------------------
def check_four_sources() -> dict:
    """A. 四源分工齐备（硬项）。"""
    evidence = {}
    # football-data.org：主力赛程源（出现在赛程/额度/映射层）
    fd_txt = _read("scripts/fixture_library.py") + _read("api_football.py")
    evidence["football-data.org"] = "football-data" in fd_txt or "football_data" in fd_txt
    # API-Football：洲际补充，含中超/亚冠（国足由人工赛历承接，见 11.4）
    af_txt = _read("api_football.py")
    evidence["API-Football"] = ("API_FOOTBALL_KEY" in af_txt and
                                all(k in af_txt for k in ("中超", "亚冠")))
    # 聚合数据 juhe.cn：中文校验与兜底
    fl_txt = _read("scripts/fixture_library.py")
    evidence["聚合数据 juhe.cn"] = ("juhe" in fl_txt and "fetch_juhe_fixtures" in fl_txt)
    # 人工赛历：国足 / 中超
    evidence["人工赛历"] = _exists("config/manual_fixtures.yaml")
    missing = [k for k, v in evidence.items() if not v]
    return {"item": "四源分工齐备（13.2）", "ok": not missing, "evidence": evidence,
            "detail": ([f"四源全部就位：{'、'.join(evidence)} ✅"] if not missing
                       else [f"❌ 缺落地物：{missing}"])}


# ------------------------------------------------------------
# B. 13.4 原始信源清单
# ------------------------------------------------------------
def check_watchlist() -> dict:
    """B. 原始信源清单齐备（硬项）。"""
    txt = _read("config/source_watchlist.yaml")
    present = [s for s in PLAN_SOURCES if s in txt]
    missing = [s for s in PLAN_SOURCES if s not in txt]
    return {"item": "原始信源清单齐备（13.4）", "ok": not missing,
            "present": present, "missing": missing,
            "detail": ([f"计划信源 {len(PLAN_SOURCES)} 项全部就位 ✅"] if not missing
                       else [f"❌ 缺少信源：{missing}"])}


# ------------------------------------------------------------
# C. 信源纪律
# ------------------------------------------------------------
def check_signal_only() -> dict:
    """C. 信源只作信号（硬项）。"""
    problems = []
    if "import source_watch" not in _read("orchestrator.py") and \
       "from source_watch" not in _read("orchestrator.py"):
        problems.append("orchestrator 未接入 source_watch 信号")
    # 抽取器/生成器不得直接引用信源清单文本
    for mod in ("extractor.py", "fact_card.py"):
        if "source_watch" in _read(mod):
            problems.append(f"{mod} 不应引用 source_watch（信源只作信号）")
    # 信号来源仅用于 ranker L2（signals_from_*）
    watch_txt = _read("source_watch.py")
    ok_api = ("signals_from_articles" in watch_txt and "signals_from_match_data" in watch_txt)
    if not ok_api:
        problems.append("source_watch 未见 signals_from_articles/signals_from_match_data")
    return {"item": "信源只作信号（13.4 纪律）", "ok": not problems,
            "detail": (["source_watch 仅驱动 L2 热度信号，抽取器/生成器无引用 ✅"]
                       if not problems else problems)}


# ------------------------------------------------------------
# D / E. 边界（告警项）
# ------------------------------------------------------------
def check_no_vendors() -> dict:
    """D. 不接入商业数据商（告警项）。"""
    hits = {k: v for k, v in _grep_files(COMMERCIAL_VENDORS).items() if v}
    return {"item": "不接入商业数据商（13.2）", "ok": not hits, "advisory": True, "hits": hits,
            "detail": (["商业数据商（纳米/火星/创冰/同道伟业）零引用 ✅"] if not hits
                       else [f"⚠️ 命中：{hits}（接入等于付费买翻译，应自建）"])}


def check_media_boundary() -> dict:
    """E. 无授权媒体接口边界（告警项）。"""
    hits = _grep_files(UNAUTHORIZED_MEDIA)
    noted = {t: sorted({Path(p).name for p in hits[t]}) for t in UNAUTHORIZED_MEDIA if hits[t]}
    return {"item": "无授权媒体接口边界（13.2）", "ok": True, "advisory": True, "hits": noted,
            "detail": ["计划 13.2：懂球帝/直播吧/虎扑/雷速 接口无授权，只能用其名提取实体中文名、",
                       "可作新闻流素材，但**正文不得进生成器**（由 11.3 物理隔离与相似度闸门兜底）。",
                       f"当前命中文件：{noted or '无'}",
                       "（本项为边界提示，不作硬断言）"]}


def verify() -> dict:
    checks = [check_four_sources(), check_watchlist(), check_signal_only(),
              check_no_vendors(), check_media_boundary()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 数据源与信源清单验收报告（计划 13.2 / 13.4）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警" if c.get("advisory") else "硬项"
        L.append(f"## {c['item']}（{tag}）：{'✅' if c['ok'] else '❌'}")
        if c["item"] == "四源分工齐备（13.2）":
            for k, v in c["evidence"].items():
                L.append(f"- {'✅' if v else '❌'} {k}")
        elif c["item"] == "原始信源清单齐备（13.4）":
            L.append(f"- 就位 {len(c['present'])}/{len(PLAN_SOURCES)}：{'、'.join(c['present'])}")
        else:
            for d in c.get("detail", []):
                L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="数据源与信源清单验收（计划 13.2/13.4）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"source_roster_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "source_roster_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
