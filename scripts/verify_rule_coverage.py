#!/usr/bin/env python3
"""规则覆盖反向校验（2026-11-06 立）。

## 它防的是什么

`config/toutiao_rules.yaml` 是审核逻辑的唯一依据，但它有两个失效方式，
而且都不会自己报错：

1. **规则表写了，判据没接**。`judge: xxx` 里的名字拼错，`verify_compliance`
   会静默判「未判定」，报告看起来是「没违规」。
2. **门禁被慢慢塞回非合规项**。pre-publish 一旦混入 quality/growth/ops 类，
   生产线抖一下就停发——这正是 2026-11-06 精简掉的那一半复杂度。
   没有反向校验的话，下一次「顺手加一个验收」就会把它加回去。

所以这里做七件事：规则表自洽、判据可达、发布前门禁纯净、合规项数不反弹、
每条规则都有人维护、覆盖缺口可见、**首发条件一致**。外加一条负向自检，
证明上面几条真的会红（每一条都配了「改坏它」的用例）。

其中「首发条件一致」是唯一一条针对**跨文件一致性**的检查：TT-12 判 `pass`
的依据一半在规则表、一半在 `publish_scope.platforms`，只改一处就会假绿。

## 退出码

    0  通过
    1  有硬项不通过
    2  缺少输入（规则表或分层清单不存在）—— **不等于通过**

用法：
    python3 scripts/verify_rule_coverage.py
    python3 scripts/verify_rule_coverage.py --quiet
    python3 scripts/verify_rule_coverage.py --selftest
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

RULES_PATH = ROOT / "config" / "toutiao_rules.yaml"
TIERS_PATH = ROOT / "config" / "audit_tiers.yaml"
AUDIT_DIR = ROOT / "data" / "audit"
OUT_PATH = AUDIT_DIR / "rule_coverage_latest.json"

#: compliance 项数上限。**只许降不许升**：它是「审核逻辑复杂度」的代理指标。
#: 2026-11-06 精简后为 10（9 个原脚本 + 新增的 compliance 入口）。
MAX_COMPLIANCE_ITEMS = 10


def _load():
    import yaml
    rules = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")) or {}
    tiers = yaml.safe_load(TIERS_PATH.read_text(encoding="utf-8")) or {}
    return rules, tiers


def _judges(rules: dict) -> tuple[dict, dict]:
    """从 verify_compliance 取已实现的判据名（唯一实现，不在这里另列一份）。"""
    import verify_compliance as vc
    return vc._SINGLE, vc._BATCH


def check_rules_selfconsistent(rules: dict) -> dict:
    """A. 规则表自洽：必填字段齐、分值 ≥10 的规则不得只告警。"""
    bad = []
    for r in rules.get("rules") or []:
        rid = r.get("id") or "?"
        for f in ("id", "name", "credit", "tier", "judge"):
            if r.get(f) in (None, ""):
                bad.append(f"{rid} 缺字段 {f}")
        credit = int(r.get("credit") or 0)
        tier = str(r.get("tier") or "")
        if credit >= 10 and tier == "warn":
            bad.append(f"{rid} 标注 {credit} 分档却只 warn（扣分的项不能只告警）")
        if tier not in ("block", "rewrite", "warn", "pass", "n/a"):
            bad.append(f"{rid} tier 非法：{tier}")
    ok = not bad
    return {
        "item": "规则表自洽", "ok": ok,
        "detail": ([f"✅ {len(rules.get('rules') or [])} 条规则字段齐备、分值档位一致"] if ok
                   else bad),
    }


def check_judges_exist(rules: dict) -> dict:
    """B. 判据可达：规则表里的 judge 名必须真有实现，拼错不许静默变「未判定」。"""
    single, batch = _judges(rules)
    known = set(single) | set(batch)
    missing = []
    for r in rules.get("rules") or []:
        raw = str(r.get("judge") or "")
        for name in [x.strip() for x in raw.split("+") if x.strip()]:
            if name not in known:
                missing.append(f"{r.get('id')}: `{name}` 无实现")
    ok = not missing
    return {
        "item": "判据可达", "ok": ok,
        "detail": ([f"✅ 已实现判据 {len(known)} 个，规则表引用全部可解析"] if ok
                   else missing),
    }


def check_prepublish_pure(tiers: dict) -> dict:
    """C. 发布前门禁纯净：tiers 含 pre-publish 的项必须 scope=compliance 且挂规则。"""
    bad = []
    for name, it in (tiers.get("items") or {}).items():
        if "pre-publish" not in (it.get("tiers") or []):
            continue
        if it.get("scope") != "compliance":
            bad.append(f"{name} scope={it.get('scope')}，不该进 pre-publish")
        if not it.get("rule_ids"):
            bad.append(f"{name} 进 pre-publish 却没挂 rule_ids（无法说明它挡哪条规则）")
    pre = [n for n, it in (tiers.get("items") or {}).items()
           if "pre-publish" in (it.get("tiers") or [])]
    ok = not bad
    return {
        "item": "发布前门禁纯净", "ok": ok,
        "detail": ([f"✅ pre-publish 仅 {len(pre)} 项，全部为 compliance：{pre}"] if ok
                   else bad),
    }


def check_compliance_not_grow(tiers: dict) -> dict:
    """D. 合规项数不反弹 —— 「审核复杂度」的唯一可量化指标。"""
    items = tiers.get("items") or {}
    comp = sorted(n for n, it in items.items() if it.get("scope") == "compliance")
    ok = len(comp) <= MAX_COMPLIANCE_ITEMS
    detail = [f"{'✅' if ok else '❌'} compliance 项 {len(comp)} 个（上限 {MAX_COMPLIANCE_ITEMS}）：{comp}"]
    if not ok:
        detail.append("❌ 审核逻辑又变复杂了。要加合规项，先删一个旧的，或说明为什么这两件事不能合并")
    return {"item": "合规项数不反弹", "ok": ok, "detail": detail,
            "counts": {"compliance": len(comp), "limit": MAX_COMPLIANCE_ITEMS}}


def check_every_rule_owned(rules: dict, tiers: dict) -> dict:
    """E. 每条规则至少被一个 compliance 项引用 —— 否则它只是文档里的一句话。"""
    owned = set()
    for it in (tiers.get("items") or {}).values():
        if it.get("scope") == "compliance":
            owned.update(it.get("rule_ids") or [])
    orphan = [r.get("id") for r in (rules.get("rules") or [])
              if r.get("tier") != "n/a" and r.get("id") not in owned]
    ok = not orphan
    return {
        "item": "每条规则有人维护", "ok": ok,
        "detail": ([f"✅ {len(owned)} 条规则均被 compliance 项引用"] if ok
                   else [f"❌ 以下规则没有任何验收项引用（等于没实现）：{orphan}"]),
    }


def check_coverage_gaps(rules: dict) -> dict:
    """F. 判据覆盖缺口必须显式登记（告警项，不阻断）。

    「未覆盖」不是错，**瞒着不说**才是错。有 coverage_gap 的规则会被列出来，
    让缺口一直是可见的债，而不是被当成「已覆盖」。
    """
    gaps = [f"{r.get('id')}：{' '.join(str(r.get('coverage_gap')).split())[:60]}"
            for r in (rules.get("rules") or []) if r.get("coverage_gap")]
    return {
        "item": "覆盖缺口可见", "ok": True, "advisory": True,
        "detail": ([f"⚠️ {len(gaps)} 条规则有已知覆盖缺口（不阻断，但必须能看见）：" + "；".join(gaps)]
                   if gaps else ["✅ 无登记缺口"]),
    }


def check_publish_scope_consistent(rules: dict) -> dict:
    """G. 首发条件一致：`tier: pass` 只在「单平台且是头条」时才成立。

    为什么要单列一项：TT-12 判 pass 的依据不在规则本身，而在
    `publish_scope.platforms`。**同一个结论由两处共同决定**，那就必须有人
    盯着它们一起改 —— 否则接了公众号分发之后，规则表还在说「都是首发」，
    30 天内最多扣 50 分，而且报告上一直显示绿色。

    只盯危险方向（平台变多了还判 pass），不禁止有人把档位调得更严：
    收紧不会漏扣分，放宽才会。
    """
    scope = rules.get("publish_scope") or {}
    plats = [str(x).strip().lower() for x in (scope.get("platforms") or []) if str(x).strip()]
    by_id = {r.get("id"): r for r in (rules.get("rules") or [])}
    tt12 = by_id.get("TT-12") or {}
    tier = str(tt12.get("tier") or "")
    bad: list[str] = []

    if not tt12:
        bad.append("规则表缺 TT-12（首发 + 原创罚则）")
    if not plats:
        bad.append("publish_scope.platforms 缺失或为空：无法判定是否首发，TT-12 不能靠默认值")
    elif len(plats) > 1 and tier in ("pass", "n/a"):
        bad.append(f"平台已变为 {plats}，TT-12 仍是 {tier}：非首发每篇扣 5 分、30 天上限 50 分，"
                   f"应改 rewrite/block 并加 72 小时窗口检查")

    # 原创那一半由 depends_on 指过去，被指的那条必须真的还在拦
    for dep in (tt12.get("depends_on") or []):
        d = by_id.get(dep)
        if d is None:
            bad.append(f"TT-12 depends_on {dep}，但规则表里没有这条（原创半边无人判）")
        elif str(d.get("tier")) not in ("block", "rewrite"):
            bad.append(f"TT-12 depends_on {dep}，但该条 tier={d.get('tier')}，拦不住洗稿")

    ok = not bad
    return {
        "item": "首发条件一致", "ok": ok,
        "detail": ([f"✅ 发布范围 {plats}，TT-12={tier}，原创半边由 "
                    f"{'/'.join(tt12.get('depends_on') or ['—'])} 判"] if ok else bad),
    }


#: 会**逐字进模型提示词**的文本。判定侧判为违规的话术一旦出现在这里，
#: 模型就会照抄，然后被自己的闸门拦掉 —— 2026-11-06 晚就是这么整批 0 篇的。
PROMPT_TXT_GLOBS = ("prompts/*.txt",)
#: 代码里的内联提示词（f-string / 普通字符串常量）。**只取字符串字面量**，
#: 不扫注释 —— 判定逻辑的注释里到处是「关注引导」这类词，扫全文会全是误报。
PROMPT_PY_FILES = ("constants.py", "orchestrator.py", "scripts/refill_reservoir.py")
#: 渲染后整块进提示词的配置文件。2026-11-12 补：``config/brand_manual.yaml``
#: 的 hook_strategy 里躺着两条「账号沉淀」示例与一条「数字表态」示例，
#: ``_render_brand_manual`` 把值原样渲染进改写 prompt，模型照抄 → 出口合规
#: TT-07/TT-08 各判 20 分 → 11-11 晚间 2 篇全丢、发布 0 篇。
#: 只扫 prompts/ + constants.py + orchestrator.py 的清单漏了这份配置，
#: 而它恰好是「示例即模板」的那类文件。**只扫值，不扫注释**（注释不进 prompt）。
PROMPT_YAML_VALUES = ("config/brand_manual.yaml",)
BAIT_CFG = ROOT / "config" / "interaction_bait.yaml"


def _inducement_patterns() -> list[tuple[str, "re.Pattern"]]:
    """诱导话术黑名单**从 interaction_bait.yaml 读**，不在这里再列一遍。

    同一份词表被判定侧和检查侧各写一份，就是给下一次「只改一处」埋伏笔。
    """
    import yaml
    cfg = yaml.safe_load(BAIT_CFG.read_text(encoding="utf-8")) or {}
    out = []
    for group, pats in (cfg.get("诱导话术") or {}).items():
        for p in pats or []:
            try:
                out.append((str(group), re.compile(str(p))))
            except re.error:
                continue
    return out


def _py_literals(path: Path) -> list[tuple[int, str]]:
    """取 .py 里**会进模型**的字符串字面量（(行号, 内容)）。

    两处排除，都是踩过的：
    - **注释**：天然不在 AST 里；
    - **docstring**：它是给维护者看的说明，不会进提示词。但它是字符串常量，
      不排除就会把「旧口径反过来强制写『关注我』，等于批量生产扣分话术」
      这类**讲事故的文档**判成违规（本项目已多次栽在「判据短串误报」上）。
    """
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return []
    docs: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docs.add(id(first.value))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            out.append((int(getattr(node, "lineno", 0) or 0), node.value))
    return out


def _yaml_values(path: Path) -> list[tuple[int, str]]:
    """取 yaml 里**会被渲染进提示词**的字符串值（(序号, 内容)）。

    只取值不取注释：``_render_brand_manual`` 遍历的是解析后的 dict，注释永远
    到不了模型；反过来，如果把注释也算进来，这份文件里「解释事故」的说明
    就会把检查自己刷红 —— 与 AST 那条「注释不算证据」是同一个道理。
    """
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    out: list[tuple[int, str]] = []

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif node is not None:
            out.append((len(out) + 1, str(node)))

    walk(data)
    return out


def prompt_texts() -> dict:
    """收集所有「会进提示词」的文本：{来源: [(行号, 文本), …]}。

    `.txt` 按行切，`.py` 只取字符串字面量，`.yaml` 只取渲染后进 prompt 的值。
    **三类都要扫**：2026-11-06 晚除了 prompts/ 里的硬指标，`constants.py` 的
    interaction_guidance 也在教「支持XX的扣1」，`orchestrator.py:3610`
    还内联着「关注老六不迷路」的示例 —— 只扫 prompts/ 会漏掉其中两类。
    2026-11-12 再补两类：`config/brand_manual.yaml`（示例即模板）与
    `scripts/refill_reservoir.py`（蓄水池生产提示词，30% 稿源的源头）。
    """
    units: dict[str, list[tuple[int, str]]] = {}
    for g in PROMPT_TXT_GLOBS:
        for p in sorted(ROOT.glob(g)):
            if p.is_file():
                txt = p.read_text(encoding="utf-8", errors="ignore")
                units[str(p.relative_to(ROOT))] = list(enumerate(txt.splitlines(), 1))
    for name in PROMPT_PY_FILES:
        p = ROOT / name
        if p.is_file():
            units[name] = _py_literals(p)
    for name in PROMPT_YAML_VALUES:
        p = ROOT / name
        if p.is_file():
            units[name] = _yaml_values(p)
    return units


def check_prompt_alignment(texts: dict | None = None) -> dict:
    """H. 提示词方向一致：判定侧判违规的话术，不许出现在喂给模型的文本里。

    **为什么单列这一项**（2026-11-06 晚的真实事故）：白天把「索要关注 / 扣1 /
    站队」改成 20 分档违规并硬拦，但生成侧仍在**强制要求** ——
    `prompts/rewrite_article.txt` 写着「必须能被检出关注引导」，
    `constants.py` 的 `interaction_guidance` 教「支持XX的扣1，反对的扣2」，
    `orchestrator.py` 的系列提示还内联着「关注老六不迷路」。模型照写，
    写完被自己的闸门判违规，重试两次仍命中，整批 0 篇、晚间发布失败。

    这是「同一规矩写两处、必然只改一处」的**第四方**：规则表 / 判据 / 发布门禁
    之外还有生成提示词。前三方都有检查，唯独提示词没有。

    **零容忍，不做「禁止语境」豁免**：提示词里出现的负例（「不要写点关注」）
    本身就会被模型当模板抄 —— 第一版修复就是把这句话写成负例，扫描器照样
    报红，反而逼出了更好的写法（只说「不要索取任何东西」，不列具体话术）。
    """
    sets = texts if texts is not None else prompt_texts()
    pats = _inducement_patterns()
    if not pats:
        return {"item": "提示词方向一致", "ok": False,
                "detail": ["interaction_bait.yaml 里没有诱导话术黑名单，这项检查形同虚设"]}
    if not sets:
        return {"item": "提示词方向一致", "ok": False,
                "detail": ["没找到任何提示词（prompts/*.txt、constants.py、orchestrator.py）"]}

    bad: list[str] = []
    for name, unit in sets.items():
        # selftest 直接传字符串；真实扫描传 [(行号, 文本), …]
        pairs = list(enumerate(str(unit).splitlines(), 1)) if isinstance(unit, str) else unit
        for lineno, chunk in pairs:
            for group, rx in pats:
                m = rx.search(chunk)
                if m:
                    bad.append(f"{name}:{lineno} [{group}] 命中 {m.group(0)!r}"
                               f" → {chunk.strip()[:60]}")
                    break
    ok = not bad
    return {
        "item": "提示词方向一致", "ok": ok,
        "detail": ([f"✅ {len(sets)} 个来源、{sum(len(v) for v in sets.values())} 段文本里"
                    f"没有诱导话术（{len(pats)} 条模式）"]
                   if ok else ["判定侧判它违规，生成侧却在教它这么写："] + bad),
    }


def verify() -> dict:
    rules, tiers = _load()
    checks = [
        check_rules_selfconsistent(rules),
        check_judges_exist(rules),
        check_prepublish_pure(tiers),
        check_compliance_not_grow(tiers),
        check_every_rule_owned(rules, tiers),
        check_coverage_gaps(rules),
        check_publish_scope_consistent(rules),
        check_prompt_alignment(),
    ]
    hard = [c for c in checks if not c.get("advisory")]
    return {
        "checks": checks,
        "ok": all(c["ok"] for c in hard),
        "hard_failed": [c["item"] for c in hard if not c["ok"]],
    }


# ──────────────────────────────────────────────────────────── 负向自检
def selftest() -> list[tuple[str, bool]]:
    """把规则表/清单改坏，对应检查必须报红。"""
    import copy
    rules, tiers = _load()
    cases: list[tuple[str, bool]] = []

    cases.append(("基线：当前规则表与清单通过",
                  all(c["ok"] for c in verify()["checks"] if not c.get("advisory"))))

    bad = copy.deepcopy(rules)
    bad["rules"][0]["credit"] = 70
    bad["rules"][0]["tier"] = "warn"
    cases.append(("扣分规则只标 warn → A 项必红",
                  check_rules_selfconsistent(bad)["ok"] is False))

    bad2 = copy.deepcopy(rules)
    bad2["rules"][0]["judge"] = "no_such_judge"
    cases.append(("判据名拼错 → B 项必红", check_judges_exist(bad2)["ok"] is False))

    bad3 = copy.deepcopy(tiers)
    bad3["items"]["title_positive"]["tiers"] = ["pre-publish"]
    cases.append(("质量项塞进发布前门禁 → C 项必红",
                  check_prepublish_pure(bad3)["ok"] is False))

    bad4 = copy.deepcopy(tiers)
    for i in range(MAX_COMPLIANCE_ITEMS + 3):
        bad4["items"][f"fake_{i}"] = {"tiers": ["daily"], "kind": "advisory",
                                      "scope": "compliance"}
    cases.append(("合规项数反弹 → D 项必红",
                  check_compliance_not_grow(bad4)["ok"] is False))

    bad5 = copy.deepcopy(tiers)
    for it in bad5["items"].values():
        it["rule_ids"] = []
        it["scope"] = "quality"
    cases.append(("规则无人引用 → E 项必红",
                  check_every_rule_owned(rules, bad5)["ok"] is False))

    # G 项：TT-12 的 pass 依赖 publish_scope，两处必须一起改
    bad6 = copy.deepcopy(rules)
    bad6["publish_scope"]["platforms"] = ["toutiao", "wechat"]
    cases.append(("多平台分发却仍判 pass → G 项必红",
                  check_publish_scope_consistent(bad6)["ok"] is False))

    bad7 = copy.deepcopy(rules)
    bad7["publish_scope"]["platforms"] = []
    cases.append(("平台清单清空 → G 项必红（不许默认当首发）",
                  check_publish_scope_consistent(bad7)["ok"] is False))

    bad8 = copy.deepcopy(rules)
    for r in bad8["rules"]:
        if r.get("id") == "TT-06":
            r["tier"] = "n/a"
    cases.append(("原创半边被降级 → G 项必红",
                  check_publish_scope_consistent(bad8)["ok"] is False))

    bad9 = copy.deepcopy(rules)
    bad9["rules"] = [r for r in bad9["rules"] if r.get("id") != "TT-06"]
    cases.append(("原创半边规则被删 → G 项必红",
                  check_publish_scope_consistent(bad9)["ok"] is False))

    # H 项：判定侧判违规的话术不许出现在提示词里（2026-11-06 晚整批 0 篇的直接原因）
    cases.append(("提示词教模型写「扣1」 → H 项必红",
                  check_prompt_alignment({"fake.txt": "文末说说，支持XX的扣1。"})["ok"] is False))
    cases.append(("提示词教模型索要关注 → H 项必红",
                  check_prompt_alignment({"fake.txt": "点个关注，明天继续。"})["ok"] is False))
    cases.append(("提示词干净 → H 项必绿",
                  check_prompt_alignment({"fake.txt": "这球换你当裁判，吹不吹？说说你的判断。"})["ok"] is True))
    cases.append(("提示词一份都没有 → H 项必红（没文本就没法保证一致）",
                  check_prompt_alignment({})["ok"] is False))
    return cases


def render(rep: dict) -> str:
    lines = ["规则覆盖校验（规则表 ↔ 判据 ↔ 发布门禁）", ""]
    for c in rep["checks"]:
        icon = "⚠️" if c.get("advisory") else ("✅" if c["ok"] else "❌")
        lines.append(f"  {icon} {c['item']}")
        for d in c["detail"]:
            lines.append(f"      {d}")
    lines.append("")
    lines.append("✅ 全部通过" if rep["ok"] else f"❌ 硬项未通过：{rep['hard_failed']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="规则覆盖反向校验")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        bad = 0
        for name, ok in selftest():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            bad += 0 if ok else 1
        print(f"\n负向自检：{'全部通过 ✅' if not bad else f'{bad} 项未通过 ❌'}")
        return 1 if bad else 0

    if not (RULES_PATH.exists() and TIERS_PATH.exists()):
        print("缺少输入：规则表或分层清单不存在（未判定）")
        return 2

    rep = verify()
    try:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if not args.quiet:
        print(render(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
