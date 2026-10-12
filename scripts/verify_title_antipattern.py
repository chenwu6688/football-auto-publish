#!/usr/bin/env python3
"""标题反模式闸门验收（完善计划 3.3 / 第十章）。

背景：2026-10-12 用头条后台 869 条真实图文回测，定位到最大的一块亏损内容形态——
「以比分/赛果开场 + 无具体人物 + 无悬念评价钩子」的赛果播报式标题。本脚本把该规则
钉死，防止回归到「把比分当标题」的老路。

硬项（不达标退出码 1）
  A. 配置结构——config/title_antipattern.yaml 字段齐备、正则可编译、判罚口径完整
  B. 规则冒烟——title_antipattern.selftest() 全通过（正例必拦 / 负例必放 / 降级放行）
  C. 回归样例——用真实数据固化的标题样例集，逐条比对期望判定
  D. 生成侧接入——orchestrator.score_title_plan 对正例判 0 分打回，对负例不误判 0 分
  E. 真实数据回测——命中组 0 条百阅读 / 0 爆款（有导出文件时才跑，否则跳过）

告警项（只提示）
  F. 近 14 天成稿命中——规则生效前的存量产出（仅提示，不阻断）

用法：
  python3 scripts/verify_title_antipattern.py [--from-export <xlsx|目录>] [--quiet]
产出：data/audit/title_antipattern_<date>.md + title_antipattern_latest.json
"""

from __future__ import annotations

import argparse
import glob
import json
import re as _re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import title_antipattern as ta  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
CST = timezone(timedelta(hours=8))

# 默认导出探测位置（本机头条后台导出；CI 无此目录则本项跳过）
DEFAULT_EXPORT_GLOBS = ("/home/chenwu/下载/图文_*.xlsx",)

# 回归样例：取自 2026-10-12 头条真实导出，标注阅读量以固化「零误杀」证据。
#   hit=True  → 真实命中且实测极低阅读（该拦）
#   hit=False → 真实有效或含人物/钩子（必放）
REGRESSION_CASES = [
    ("亚泰主场1-5惨败，无锡吴钩狂轰5球血洗奥林匹克公园", True, 11),
    ("5-1、2-1、0-1，三场比赛算出世界杯16强数学题", True, 3),
    ("1-0到1-0, 加拿大等了40年", True, 2),
    ("荷兰2-2日本：傲慢的代价", True, 15),
    ("7-0！长沙小将把对手打成了背景板", True, 1),
    ("张玉宁62球追平队史第一！国安残阵逼平领头羊，他才是真大腿", False, 36),
    ("切尔西6-3逆转，阿隆索主动揽责：上半场是我的锅，别骂球员！", False, 57),
    ("西班牙4-1克罗地亚，最动人的却是第81分钟那一幕", False, 26),
    ("别被4比0骗了，韩国队这进攻还没成型", False, 67),
    ("1-5惨败后，费耶诺德后卫说了句大实话，巴萨球迷听了都点头", False, 325),
]


def check_config() -> dict:
    raw = ta.load_config()
    if raw is None:
        return {"item": "配置结构（config/title_antipattern.yaml）", "ok": False, "advisory": False,
                "detail": ["❌ 配置缺失或不可解析（闸门将降级放行，属漂移隐患）"]}
    problems = []
    for key in ("版本", "规则名", "开场窗口", "比分模式", "赛果模式", "人物名", "钩子模式", "判罚"):
        if raw.get(key) in (None, "", [], {}):
            problems.append(f"缺字段 {key}")
    pen = raw.get("判罚") or {}
    if int(pen.get("分数", -1)) != 0:
        problems.append(f"判罚分数应 0（硬违规），实为 {pen.get('分数')}")
    if "打回" not in str(pen.get("动作", "")):
        problems.append(f"判罚动作应含「打回」，实为 {pen.get('动作')}")
    if not str(pen.get("提示", "")).strip():
        problems.append("判罚提示为空（打回后无重写指引）")
    if int(raw.get("开场窗口", -1)) < 0:
        problems.append("开场窗口为负")
    # 正则可编译 + 人物表非空
    for k in ("比分模式", "赛果模式", "钩子模式"):
        for pat in (raw.get(k) or []):
            try:
                import re as _re
                _re.compile(str(pat))
            except Exception as e:
                problems.append(f"{k} 正则不可编译：{pat}（{e}）")
    if not (raw.get("人物名") or []):
        problems.append("人物名表为空（会导致大量误杀）")
    persons = ta.person_names(raw)
    return {"item": "配置结构（config/title_antipattern.yaml）", "ok": not problems,
            "advisory": False, "version": raw.get("版本"),
            "person_names": len(persons),
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"配置 v{raw.get('版本')} 字段齐备；人物名表 {len(persons)} 个"
                             f"（人工 {len(raw.get('人物名') or [])} + entity_map 球员）✅",
                             f"判罚：{pen.get('分数')} 分 / {pen.get('动作')} ✅"])}


def check_selftest() -> dict:
    cases = ta.selftest()
    bad = [n for n, ok in cases if not ok]
    return {"item": "规则冒烟（正例拦 / 负例放 / 降级放行）", "ok": not bad, "advisory": False,
            "total": len(cases),
            "detail": ([f"❌ 未通过：{bad}"] if bad
                       else [f"{len(cases)} 项冒烟全部通过 ✅"])}


def check_regression() -> dict:
    fails = []
    for title, expect_hit, reads in REGRESSION_CASES:
        got = ta.check(title)["hit"]
        if got != expect_hit:
            fails.append(f"{'应拦' if expect_hit else '应放'}但判定相反（实测 {reads} 阅）：{title}")
    return {"item": "回归样例（真实标题固化）", "ok": not fails, "advisory": False,
            "total": len(REGRESSION_CASES),
            "detail": ([f"❌ {'；'.join(fails)}"] if fails
                       else [f"{len(REGRESSION_CASES)} 条真实标题判定全部正确 ✅",
                             "其中应拦 5 条实测阅读 ≤15（最低 1）；应放 5 条含人物/钩子（最高 325）"])}


def check_orchestrator() -> dict:
    try:
        import orchestrator
    except Exception as e:  # pragma: no cover
        return {"item": "生成侧接入（score_title_plan）", "ok": False, "advisory": False,
                "detail": [f"❌ 无法载入 orchestrator：{e}"]}
    problems = []
    for title in ("3-1暴揍中亚狼", "亚泰主场1-5惨败"):
        sc, ok, _ = orchestrator.score_title_plan({}, title, "热点球评")
        if sc != 0 or ok:
            problems.append(f"「{title}」应判 0 分打回，实为 {sc}/{ok}")
    # 负例不得因本规则被判 0 分（其余维度扣分不受限制）
    for title in ("张玉宁62球追平队史第一！国安残阵逼平领头羊，他才是真大腿",
                  "1-5惨败后，费耶诺德后卫说了句大实话，巴萨球迷听了都点头"):
        sc, _ok, reasons = orchestrator.score_title_plan({}, title, "热点球评")
        if sc == 0 and any("比分开场" in r or "赛果播报" in r for r in reasons):
            problems.append(f"「{title}」被本规则误判 0 分")
    return {"item": "生成侧接入（score_title_plan）", "ok": not problems, "advisory": False,
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else ["正例判 0 分打回、负例不误判 ✅"])}


def _detect_exports(from_export: str | None) -> list[str]:
    if from_export:
        p = Path(from_export)
        if p.is_dir():
            return sorted(glob.glob(str(p / "*.xlsx")))
        return [str(p)] if p.exists() else []
    cands: list[str] = []
    for pat in DEFAULT_EXPORT_GLOBS:
        cands += sorted(glob.glob(pat))
    if not cands:
        return []
    # 同目录可能同时留有不同日期的导出（如 20261010 与 20261012），只取最新那一组
    def stamp(fn: str) -> str:
        m = _re.search(r"-(\d{8})_", Path(fn).name)
        return m.group(1) if m else ""
    stamps = [stamp(f) for f in cands]
    latest = max(stamps)
    return [f for f in cands if stamp(f) == latest] if any(stamps) else cands


def check_backtest(from_export: str | None) -> dict:
    files = _detect_exports(from_export)
    if not files:
        return {"item": "真实数据回测（零误杀）", "ok": True, "advisory": True, "skipped": True,
                "detail": ["未找到头条后台导出文件，本项跳过（用 --from-export 指定）"]}
    try:
        import toutiao_import as tti
    except Exception as e:
        return {"item": "真实数据回测（零误杀）", "ok": True, "advisory": True, "skipped": True,
                "detail": [f"无法载入 toutiao_import：{e}"]}

    rows = 0
    hits: list[dict] = []
    for fn in files:
        try:
            raw = tti.read_table(fn)
        except Exception:
            continue
        if not raw:
            continue
        hdr = raw[0]
        ci = {h: i for i, h in enumerate(hdr)}
        for r in raw[1:]:
            def g(name, _r=r, _ci=ci):
                j = _ci.get(name)
                return _r[j] if (j is not None and j < len(_r)) else ""
            m = _re.match(r"(\d{4})-(\d{2})-(\d{2})", g("发布时间"))
            if not m:
                continue
            reads = tti.clean_number(g("阅读量"))
            if reads is None or reads < 0:
                continue
            rows += 1
            if ta.check(g("标题"))["hit"]:
                hits.append({"title": g("标题"), "reads": int(reads), "file": Path(fn).name})
    if rows == 0:
        return {"item": "真实数据回测（零误杀）", "ok": True, "advisory": True, "skipped": True,
                "detail": ["导出文件无可解析数据行，本项跳过"]}

    big100 = [h for h in hits if h["reads"] >= 100]
    big1000 = [h for h in hits if h["reads"] >= 1000]
    mx = max((h["reads"] for h in hits), default=0)
    rate = len(hits) / rows
    problems = []
    if big1000:
        problems.append(f"命中组出现 {len(big1000)} 条千阅读（爆款）——规则误杀")
    if big100:
        problems.append(f"命中组出现 {len(big100)} 条百阅读——规则误杀：{big100[:3]}")
    if not (0.0 < rate < 0.20):
        problems.append(f"命中率 {rate:.2%} 越界（应在 0~20% 内）")
    return {"item": "真实数据回测（零误杀）", "ok": not problems, "advisory": False,
            "rows": rows, "hits": len(hits), "hit_rate": round(rate, 4),
            "max_reads": mx, "over100": len(big100), "over1000": len(big1000),
            "files": [Path(f).name for f in files],
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"回测 {rows} 条真实作品，命中 {len(hits)} 条（{rate:.1%}）",
                             f"命中组最高阅读 {mx}、百阅读 {len(big100)} 条、千阅读 {len(big1000)} 条 ✅",
                             "命中组零爆款、零百阅读 → 判为硬违规不会误伤已验证作品 ✅"])}


def check_recent_output(days: int = 14) -> dict:
    floor = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    total = hit = 0
    samples: list[str] = []
    for p in sorted(glob.glob(str(OUTPUT_DIR / "**" / "metadata.json"), recursive=True)):
        d = Path(p).parent.name
        if d < floor:
            continue
        try:
            meta = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in (meta.get("articles") or []):
            t = (a.get("title") or "").strip()
            if not t:
                continue
            total += 1
            if ta.check(t)["hit"]:
                hit += 1
                if len(samples) < 5:
                    samples.append(f"{d} {t}")
    return {"item": f"近 {days} 天成稿命中（存量，仅提示）", "ok": hit == 0, "advisory": True,
            "total": total, "hits": hit, "samples": samples,
            "detail": [f"近 {days} 天成稿 {total} 条，命中 {hit} 条"
                       + ("（规则生效前存量，将在生成侧逐步淘汰）" if hit else " ✅"),
                       *samples]}


def verify(from_export: str | None = None, days: int = 14) -> dict:
    hard = [check_config(), check_selftest(), check_regression(),
            check_orchestrator(), check_backtest(from_export)]
    soft = [check_recent_output(days)]
    checks = hard + soft
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "advisory_failed": [c["item"] for c in soft if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 标题反模式闸门验收报告（计划 3.3 / 第十章）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警项" if c.get("advisory") else "硬项"
        L.append(f"## {c['item']}（{tag}）：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    if rep.get("advisory_failed"):
        L += [f"> 告警项待关注：{'、'.join(rep['advisory_failed'])}", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="标题反模式闸门验收（计划 3.3 / 第十章）")
    ap.add_argument("--from-export", help="头条导出 xlsx 文件或目录（默认探测 ~/下载/图文_*.xlsx）")
    ap.add_argument("--days", type=int, default=14, help="近 N 天成稿告警窗口（默认 14）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify(from_export=args.from_export, days=args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"title_antipattern_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "title_antipattern_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
