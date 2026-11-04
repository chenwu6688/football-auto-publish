#!/usr/bin/env python3
"""审核机制元验收（2026-11-04 重构 · 第四层）。

这个脚本不审内容，它审**审核本身**。起因是一个具体事故：

    11-04 午间「大罗VS C罗巅峰数据对决，谁才是真正的锋线之王？」穿过三道审核发出去了。
    查下来发现 `verify_publish_gate` 复算标题时不传 `recent_titles`，导致
    「疑问收尾 14 天 ≤2 篇」这条规则**从来没触发过** —— 它每次都在判「合规 ✅」。
    一个恒真的判定，比没有判定更危险：它顶着绿灯把真违规掩掉。

所以这里的判据是「**能不能报红**」，而不是「当前是不是绿」：

  A. 清单完整性（硬项）—— `scripts/verify_*.py` 与 `config/audit_tiers.yaml` **双向一致**：
     漏登记 = 有人偷偷加了验收却没说什么时候跑；幽灵项 = 清单在描述不存在的东西。
  B. 负向用例债务不增长（硬项）—— 没有负向用例的项数 ≤ `baseline_negative_debt`。
     不许一次要求全绿（红久必被 `|| echo` 软化，11-03 的坑），只许债务单调下降。
  C. 硬项必须真的被调用（硬项）—— `kind: hard` 的每一项至少被某个 workflow 调用；
     `tiers` 含 `pre-publish` 且 `kind: hard` 的，调用行**不得带 `|| echo`**
     （带了就等于「FAIL 了稿子照样发」）。2026-11-04 体检时 8 个发布前验收里 7 个是这种状态。
  D. 每日层最小集（硬项）—— `required_daily` 里的项必须真的在 `daily` 层。允许加项，
     不允许悄悄删项，否则「每日结果门禁」会被慢慢掏空成一句空话。
  E. 产物落盘（告警）—— 每个验收都要写 `data/audit/*.json`，否则结论只活在 CI 日志里，
     翻不了旧账。
  F. 负向自检（硬项）—— 把 `baseline_negative_debt` 人为调低 1，B 项**必须**变 FAIL。
     这一条是「元层」的负向用例：如果它不会红，说明整个元验收也是恒真的。

退出码约定（与其余 verify 一致）：硬项 FAIL → 1；未判定 → 0 + `::warning::`；
无产物/无样本 → 该 skip。产物 ``data/audit/audit_health_<date>.md`` + ``_latest.json``。

用法：python3 scripts/verify_audit_health.py [--quiet]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402

CONFIG = PROJECT_ROOT / "config" / "audit_tiers.yaml"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
WORKFLOWS_DIR = PROJECT_ROOT / ".github" / "workflows"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

TIERS = ("pre-publish", "daily", "code-change", "weekly")
KINDS = ("hard", "advisory")


# ──────────────────────────────────────────────────────────────
# 基础取数
# ──────────────────────────────────────────────────────────────

def load_config(path: Path | None = None) -> dict:
    p = Path(path or CONFIG)
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def script_names() -> set[str]:
    """scripts/verify_*.py 对应的登记名（去掉 verify_ 前缀与 .py）。"""
    return {f.stem[len("verify_"):] for f in SCRIPTS_DIR.glob("verify_*.py")}


def workflow_calls() -> dict[str, list[dict]]:
    """扫所有 workflow，返回 {登记名: [{'file':..,'line':..,'softened':bool}]}。

    `softened` = 该次调用后面跟着 `|| echo`（或 `|| true`）——FAIL 不会让步骤失败。
    只认「python3 scripts/verify_xxx.py」这种真实调用，不做全文关键字匹配
    （注释里写一句 verify_xxx.py 就会误报，这是 10-25 僵尸测试清理时踩过的坑）。
    """
    out: dict[str, list[dict]] = {}
    if not WORKFLOWS_DIR.exists():
        return out
    pat = re.compile(r"python3?\s+scripts/verify_([a-z_]+)\.py")
    for wf in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        lines = wf.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                continue
            for m in pat.finditer(line):
                # 软化标记常常折到下一行（YAML 续行 `\` + `|| echo`）：
                # 只判「本行剩余为空」会漏掉真实软化——首版就是这样把 batch_quota 的
                # `|| echo` 漏了，报出「发布前被软化 0 个」的假结论（单测抓到的）。
                tail = line[m.end():]
                if not tail.strip() or tail.rstrip().endswith("\\"):
                    tail = tail + " ".join(lines[i + 1:i + 3])
                softened = "|| echo" in tail or "|| true" in tail
                out.setdefault(m.group(1), []).append(
                    {"file": wf.name, "line": i + 1, "softened": softened})
    return out


def writes_artifact(name: str) -> bool:
    """该脚本是否把结论落到 data/audit（而不是只 print）。"""
    f = SCRIPTS_DIR / f"verify_{name}.py"
    if not f.exists():
        return False
    s = f.read_text(encoding="utf-8")
    return bool(re.search(r"AUDIT_DIR|data/audit", s)) and bool(
        re.search(r"write_text\(|json\.dump", s))


# ──────────────────────────────────────────────────────────────
# 检查项
# ──────────────────────────────────────────────────────────────

def check_roster(cfg: dict) -> dict:
    """A. 清单与脚本双向一致。"""
    declared = set((cfg.get("items") or {}).keys())
    actual = script_names()
    missing = sorted(actual - declared)
    ghost = sorted(declared - actual)
    bad_tier, bad_kind = [], []
    for name, it in (cfg.get("items") or {}).items():
        tiers = it.get("tiers") or []
        if not tiers or any(t not in TIERS for t in tiers):
            bad_tier.append(f"{name}:{tiers}")
        if it.get("kind") not in KINDS:
            bad_kind.append(f"{name}:{it.get('kind')}")
    ok = not (missing or ghost or bad_tier or bad_kind)
    detail = [f"{'✅' if ok else '❌'} 脚本 {len(actual)} 个 / 登记 {len(declared)} 项"]
    if missing:
        detail.append(f"❌ 未登记（有人加了验收却没说什么时候跑）：{'、'.join(missing)}")
    if ghost:
        detail.append(f"❌ 幽灵项（清单描述了不存在的东西）：{'、'.join(ghost)}")
    if bad_tier:
        detail.append(f"❌ tiers 非法：{'、'.join(bad_tier[:5])}")
    if bad_kind:
        detail.append(f"❌ kind 非法（只允许 {'/'.join(KINDS)}）：{'、'.join(bad_kind[:5])}")
    return {"item": "清单完整性", "ok": ok, "detail": detail,
            "counts": {"scripts": len(actual), "declared": len(declared)}}


def check_negative_debt(cfg: dict, *, baseline_override: int | None = None) -> dict:
    """B. 负向用例债务不增长（可被 F 项用 baseline_override 做负向自检）。"""
    meta = cfg.get("meta") or {}
    baseline = meta.get("baseline_negative_debt")
    if baseline is None:
        return {"item": "负向用例债务", "ok": False,
                "detail": ["❌ 清单缺 meta.baseline_negative_debt，无法判断债务是否增长"]}
    if baseline_override is not None:
        baseline = baseline_override
    items = cfg.get("items") or {}
    debt = [n for n, it in items.items() if not (it.get("negative") or "").strip()]
    covered = len(items) - len(debt)
    ok = len(debt) <= baseline
    detail = [f"{'✅' if ok else '❌'} 有负向用例 {covered}/{len(items)} 项，"
              f"无负向 {len(debt)} 项（上限 {baseline}）"]
    if not ok:
        detail.append(f"❌ 债务增长 {len(debt) - baseline} 项：新增项必须同时给出负向用例，"
                      f"否则它只是「看起来在验收」")
        detail += [f"  · {n}" for n in sorted(debt)[:8]]
    else:
        if debt:
            detail.append(f"· 仍无负向用例（可平静偿还，不阻断）：{'、'.join(sorted(debt)[:10])}"
                          + ("…" if len(debt) > 10 else ""))
        else:
            detail.append("🎉 全部项都有负向用例")
    return {"item": "负向用例债务", "ok": ok, "detail": detail,
            "value": len(debt), "limit": baseline, "covered": covered}


def check_hard_wired(cfg: dict) -> dict:
    """C. 硬项必须真的被调用，且发布前硬项不得被 `|| echo` 软化。"""
    calls = workflow_calls()
    items = cfg.get("items") or {}
    not_called, softened = [], []
    for name, it in items.items():
        if it.get("kind") != "hard":
            continue
        got = calls.get(name) or []
        if not got:
            not_called.append(name)
            continue
        if "pre-publish" in (it.get("tiers") or []) and all(c["softened"] for c in got):
            softened.append(name)
    ok = not (not_called or softened)
    detail = [f"{'✅' if ok else '❌'} 硬项 {sum(1 for i in items.values() if i.get('kind') == 'hard')} 个，"
              f"未被调用 {len(not_called)} 个，发布前被软化 {len(softened)} 个"]
    if not_called:
        detail.append(f"❌ 写了却从没被 CI 跑过（「有验收」的假象）：{'、'.join(not_called)}")
    if softened:
        detail.append(f"❌ 声明为发布前硬项但调用行带 `|| echo`（FAIL 了照样发）："
                      f"{'、'.join(softened)}")
    if ok:
        detail.append("· 判定方式：只认 `python3 scripts/verify_xxx.py` 真实调用行，"
                      "不做全文关键字匹配（注释里写一句会误报）")
    return {"item": "硬项接线与阻断有效性", "ok": ok, "detail": detail,
            "not_called": not_called, "softened": softened}


def check_required_daily(cfg: dict) -> dict:
    """D. 每日层最小集不得被删空。"""
    meta = cfg.get("meta") or {}
    req = list(meta.get("required_daily") or [])
    if not req:
        return {"item": "每日层最小集", "ok": False,
                "detail": ["❌ 清单缺 meta.required_daily，每日门禁可能被掏空"]}
    items = cfg.get("items") or {}
    missing = [n for n in req if n not in items]
    not_daily = [n for n in req if n in items and "daily" not in (items[n].get("tiers") or [])]
    ok = not (missing or not_daily)
    detail = [f"{'✅' if ok else '❌'} 最小集 {len(req)} 项，"
              f"缺登记 {len(missing)} / 不在 daily 层 {len(not_daily)}"]
    if missing:
        detail.append(f"❌ 最小集里的项根本没登记：{'、'.join(missing)}")
    if not_daily:
        detail.append(f"❌ 从 daily 层被移走（每日门禁据此掏空）：{'、'.join(not_daily)}")
    detail.append("· 最小集口径：发布成功率 / 无封面率 / 每批结构 / 内容配比 / 标题硬违规 / 效果闭环")
    return {"item": "每日层最小集", "ok": ok, "detail": detail,
            "required": req, "missing": missing, "not_daily": not_daily}


def check_artifact(cfg: dict) -> dict:
    """E. 每个验收都要落产物（告警项）。"""
    items = cfg.get("items") or {}
    bad = [n for n in items if not writes_artifact(n)]
    ok = not bad
    detail = [f"{'⚠️' if bad else '✅'} {len(items) - len(bad)}/{len(items)} 项把结论落到 data/audit"]
    if bad:
        detail.append(f"· 只 print 不落盘（翻不了旧账，翻旧账时只能靠记忆）：{'、'.join(bad[:8])}")
    return {"item": "产物落盘", "ok": ok, "advisory": True, "detail": detail, "missing": bad}


def check_meta_negative(cfg: dict) -> dict:
    """F. 元层负向自检：把 baseline 调低 1，B 项必须变 FAIL。"""
    meta = cfg.get("meta") or {}
    baseline = meta.get("baseline_negative_debt")
    if not isinstance(baseline, int) or baseline <= 0:
        return {"item": "元层负向自检", "ok": False,
                "detail": ["❌ baseline_negative_debt 缺失或非正数，无法构造负向用例"]}
    probe = check_negative_debt(cfg, baseline_override=baseline - 1)
    ok = probe["ok"] is False
    if ok:
        detail = [f"✅ 把 baseline 调低 1（{baseline}→{baseline - 1}）后，债务检查确实变 FAIL"
                  f"（实际债务 {probe.get('value')} 项 > 上限 {baseline - 1}）",
                  "· 这条是元层的负向用例：它不会红，说明整个元验收也是恒真的"]
    else:
        detail = [f"❌ 把 baseline 调低 1（{baseline}→{baseline - 1}）后债务检查仍通过，"
                  f"说明「债务不增长」这条判定不在判定",
                  "· 常见成因：debt 计算恒返回 0（例如 negative 字段被写成空字符串而非 null）"]
    return {"item": "元层负向自检", "ok": ok, "detail": detail}


# ──────────────────────────────────────────────────────────────
# 汇总与渲染
# ──────────────────────────────────────────────────────────────

def verify() -> dict:
    cfg = load_config()
    checks = [check_roster(cfg), check_negative_debt(cfg), check_hard_wired(cfg),
              check_required_daily(cfg), check_meta_negative(cfg), check_artifact(cfg)]
    hard = [c for c in checks if not c.get("advisory")]
    pending = [c["item"] for c in checks if c.get("ok") is None]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "version": cfg.get("version"), "updated": cfg.get("updated"),
            "ok": all(c["ok"] for c in hard) if not pending else None,
            "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "pending": pending}


def render_md(rep: dict) -> str:
    concl = ("通过 ✅" if rep["ok"] else
             ("未判定 ⏸" if rep["ok"] is None else "未通过 ❌"))
    L = ["# 审核机制元验收（2026-11-04 重构 · 第四层）", "",
         f"- 生成时间：{rep['generated_at']}｜清单版本 v{rep['version']}（{rep['updated']}）",
         f"- 验收结论：**{concl}**",
         "- 口径：本脚本审「审核本身」——判据是**能不能报红**，不是「当前是不是绿」",
         ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警" if c.get("advisory") else "硬项"
        status = "⏸" if c.get("ok") is None else ("✅" if c["ok"] else "❌")
        L.append(f"## {c['item']}（{tag}）：{status}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="审核机制元验收")
    ap.add_argument("--quiet", action="store_true", help="只写产物，不打印")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(CST).strftime("%Y-%m-%d")
    if not args.quiet:
        print(render_md(rep))
    (AUDIT_DIR / f"audit_health_{today}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "audit_health_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    if rep["ok"] is None:
        print("::warning::审核机制元验收存在未判定项：" + "、".join(rep["pending"]))
        return 0
    if not rep["ok"]:
        print("::error::审核机制元验收未通过：" + "、".join(rep["hard_failed"]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
