#!/usr/bin/env python3
"""自动调参纪律与四道防呆验收（计划 8.1 / 8.2 / 11.5）。

计划 11.5 把「全自动调参」的安全边界写成四道必须同时具备的防呆：
  样本阈值 / 探索配额 / 死区 / 幅度与冷静期；
计划 8.1 另给四条共用纪律（滚动窗口 / 最小样本 / 单次幅度 / 冷静期）。
本脚本校验这些纪律不仅「写在配置里」，而且**代码真的在执行**——防呆最危险的失效
方式不是配置缺失，而是配置存在却没人读。

校验项：
  A. 调整纪律配置数值（硬项）—— config/ratio_config.yaml 六项数值落在计划区间；
  B. 代码执行点（硬项）—— ratio_tuner 实际读取并执行这六项；
  C. 四道防呆冒烟（硬项）—— 样本不足冻结 / 死区不调 / 幅度≤10 且探索配额托底 /
     冷静期内不重复调整，全部用 compute_suggestion 实跑验证；
  D. 分层指标（告警项）—— 第八章「日/周/月」三层指标口径齐备（文档口径，无代码断言）。

产出 data/audit/tuning_discipline_*.md + tuning_discipline_latest.json；硬项 FAIL 退出码 1。

用法：
  python3 scripts/verify_tuning_discipline.py
  python3 scripts/verify_tuning_discipline.py --quiet
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ratio_tuner as rt  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

# 计划 8.1 / 11.5 规定的纪律区间（key: (期望值或区间, 说明)）；调参轴=选题类型（R11）
EXPECT = {
    "滚动窗口天数": (14, "计划 8.1：滚动窗口 14 天"),
    "最小样本": (30, "计划 8.1/11.5：单类型样本 ≥30 篇"),
    "单次调整上限": (10, "计划 8.1/11.5：单次变动 ≤10 个百分点"),
    "冷静期天数": (14, "计划 8.1/11.5：同一类型 2 周内不重复调整"),
    "死区阈值": (0.20, "计划 11.5：差异不足 20% 不调整"),
    "探索配额": ((0.10, 0.15), "计划 11.5：固定保留 10%-15% 发布位"),
}

# 代码中必须出现的执行点片段（B 项）
EXEC_POINTS = {
    "样本冻结": "sample_frozen",
    "死区判定": "dead_zone",
    "冷静期判定": "cooldown",
    "幅度上限": "max_step",
    "探索配额托底": "explore_lo",
}


def _load_cfg() -> dict:
    return rt.load_config()


def check_config() -> dict:
    """A. 调整纪律配置数值（硬项）。"""
    cfg = (rt.load_config() or {}).get("调整纪律", {}) or {}
    problems, rows = [], []
    for key, (want, desc) in EXPECT.items():
        got = cfg.get(key)
        if isinstance(want, tuple):
            ok = (isinstance(got, (list, tuple)) and len(got) >= 2
                  and abs(got[0] - want[0]) < 1e-9 and abs(got[1] - want[1]) < 1e-9)
        else:
            ok = (got is not None and abs(float(got) - float(want)) < 1e-9)
        rows.append({"key": key, "got": got, "want": want, "ok": ok})
        if not ok:
            problems.append(f"{key}={got!r}（应 {want!r}）")
    return {"item": "调整纪律配置数值", "ok": not problems, "rows": rows,
            "detail": ([f"{len(rows)} 项纪律数值全部落在计划区间 ✅"] if not problems else problems)}


def check_exec() -> dict:
    """B. 代码执行点（硬项）。"""
    src = (Path(rt.__file__)).read_text(encoding="utf-8")
    missing = [name for name, frag in EXEC_POINTS.items() if frag not in src]
    return {"item": "四道防呆代码执行点", "ok": not missing,
            "missing": missing,
            "detail": ([f"五处执行点全部命中：{'、'.join(EXEC_POINTS)} ✅"] if not missing
                       else [f"❌ 源码未见执行点：{'、'.join(missing)}"])}


# ------------------------------------------------------------
# C. 四道防呆冒烟（实跑 compute_suggestion）
# ------------------------------------------------------------
def _cfg():
    """五类选题类型近似等权（合计 100）。"""
    cats = rt.PLAN_CATEGORIES
    base = 100 // len(cats)
    d = {c: base for c in cats}
    d[cats[0]] += 100 - base * len(cats)     # 取整误差补到第一类
    return {"选题类型配比": d,
            "调整纪律": {"滚动窗口天数": 14, "最小样本": 30, "单次调整上限": 10,
                         "冷静期天数": 14, "死区阈值": 0.20, "探索配额": [0.10, 0.15]}}


def _obs(category, n, reads):
    return [{"date": "2026-10-12", "index": i, "category": category,
             "reads": reads, "retention": 0.4} for i in range(n)]


def _smoke_freeze() -> dict:
    """样本不足 → 冻结并交人工，绝不自动执行。"""
    cfg = _cfg()
    obs = _obs(rt.PLAN_CATEGORIES[0], 5, 100)  # 其余类型无样本
    sug = rt.compute_suggestion(cfg, obs)
    ok = (sug["是否自动执行"] is False
          and len(sug["冻结类型"]) == len(rt.PLAN_CATEGORIES)
          and sug["建议配比"] == cfg["选题类型配比"])
    return {"case": "样本不足冻结", "ok": ok,
            "detail": f"自动执行={sug['是否自动执行']}，冻结 {len(sug['冻结类型'])} 类型，配比不变={sug['建议配比'] == cfg['选题类型配比']}"}


def _smoke_deadzone() -> dict:
    """类型差异不足死区（20%）→ 不做任何调整。"""
    cfg = _cfg()
    obs = []
    for s, r in zip(rt.PLAN_CATEGORIES, (100, 105, 98, 102, 101)):
        obs += _obs(s, 30, r)  # 差异均 < 20%
    sug = rt.compute_suggestion(cfg, obs)
    ok = (sug["是否自动执行"] is False and sug["建议配比"] == cfg["选题类型配比"]
          and all("死区" in sug["类别指标"][s]["决策"] for s in rt.PLAN_CATEGORIES))
    return {"case": "死区不调整", "ok": ok,
            "detail": f"配比不变={sug['建议配比'] == cfg['选题类型配比']}，决策={[sug['类别指标'][s]['决策'] for s in rt.PLAN_CATEGORIES][:2]}"}


def _smoke_amplitude() -> dict:
    """存在显著差异 → 变动幅度 ≤10 且各类型不低于探索配额下限，合计仍为 100。"""
    cfg = _cfg()
    obs = []
    for s, r in zip(rt.PLAN_CATEGORIES, (1000, 100, 100, 100, 100)):
        obs += _obs(s, 30, r)
    sug = rt.compute_suggestion(cfg, obs)
    prop, cur = sug["建议配比"], cfg["选题类型配比"]
    changes = {s: prop[s] - cur[s] for s in rt.PLAN_CATEGORIES}
    floor = int(round(0.10 * 100))
    ok = (all(abs(v) <= 10 for v in changes.values())
          and all(prop[s] >= floor for s in rt.PLAN_CATEGORIES)
          and sum(prop.values()) == 100
          and max(changes.values()) > 0)
    return {"case": "幅度上限 + 探索配额托底", "ok": ok,
            "detail": f"变动={changes}，最低权={min(prop.values())}（floor={floor}），合计={sum(prop.values())}"}


def _smoke_cooldown() -> dict:
    """同一类型冷静期内（<14 天）不重复调整。"""
    cfg = _cfg()
    cats = rt.PLAN_CATEGORIES
    sec, other = cats[0], cats[1]
    recent = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    cur = dict(cfg["选题类型配比"])
    after = dict(cur)
    after[sec] = cur[sec] + 5
    after[other] = cur[other] - 5
    cfg["history"] = [{"生效日期": recent, "变更前配比": cur, "变更后配比": after}]
    obs = []
    for s, r in zip(cats, (1000, 100, 100, 100, 100)):
        obs += _obs(s, 30, r)
    sug = rt.compute_suggestion(cfg, obs)
    ok = ("冷静期" in sug["类别指标"][sec]["决策"]
          and sug["建议配比"][sec] == cfg["选题类型配比"][sec])
    return {"case": "冷静期内不重复调整", "ok": ok,
            "detail": f"{sec} 决策={sug['类别指标'][sec]['决策']}，权重不变={sug['建议配比'][sec] == cfg['选题类型配比'][sec]}"}


def check_four_guards() -> dict:
    """C. 四道防呆冒烟（硬项）。"""
    cases = [_smoke_freeze(), _smoke_deadzone(), _smoke_amplitude(), _smoke_cooldown()]
    bad = [c for c in cases if not c["ok"]]
    return {"item": "四道防呆冒烟", "ok": not bad, "cases": cases,
            "detail": ([f"{len(cases)} 项冒烟全部通过：样本冻结／死区／幅度+探索配额／冷静期 ✅"]
                       if not bad else [f"❌ {c['case']}：{c['detail']}" for c in bad])}


def check_layered_metrics() -> dict:
    """D. 分层指标口径（告警项）。"""
    plan_layers = {"短期/日": "评分器只做及格线，不参与策略调整",
                   "中期/周": "选题类型篇均阅读与点击率（调选题类型配比）",
                   "长期/月": "粉丝阅读占比与互动率（决定类型增减）"}
    # 代码侧证据：ratio_tuner 使用滚动窗口 + 剔除极值（_trimmed_mean），对齐 8.2 幂律纪律
    src = Path(rt.__file__).read_text(encoding="utf-8")
    trim_ok = "_trimmed_mean" in src
    return {"item": "分层指标纪律（8.2）", "ok": True, "advisory": True,
            "layers": list(plan_layers), "trimmed_mean": trim_ok,
            "detail": [f"计划三层口径：{'、'.join(plan_layers)}",
                       f"代码侧剔除极值（应对幂律分布）：{'✅ _trimmed_mean' if trim_ok else '❌ 未见'}",
                       "（本项为口径提示，不作硬断言）"]}


def verify() -> dict:
    checks = [check_config(), check_exec(), check_four_guards(), check_layered_metrics()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 自动调参纪律与四道防呆验收报告（计划 8.1 / 8.2 / 11.5）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警" if c.get("advisory") else "硬项"
        status = "✅" if c["ok"] else "❌"
        L.append(f"## {c['item']}（{tag}）：{status}")
        if c["item"] == "调整纪律配置数值":
            for r in c["rows"]:
                L.append(f"- {'✅' if r['ok'] else '❌'} {r['key']} = {r['got']!r}（应 {r['want']!r}）")
        elif c["item"] == "四道防呆冒烟":
            for cs in c["cases"]:
                L.append(f"- {'✅' if cs['ok'] else '❌'} {cs['case']}：{cs['detail']}")
        else:
            for d in c.get("detail", []):
                L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="自动调参纪律与四道防呆验收（计划 8.1/11.5）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"tuning_discipline_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "tuning_discipline_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
