#!/usr/bin/env python3
"""标题正向规则 —— R11 内容诊断结论的「加分/减分」落地（完善计划 3.3）。

背景（2026-10-12 头条后台 869 条真实图文回测，R11 内容诊断）：
  最大亏损形态已用 title_antipattern.py 以硬规则（0 分打回）钉死；本模块处理
  **剩余可被数据证实的正/负向信号**，且一律用「加分/减分」表达，绝不硬阻塞：

    - 悬念词（没想到/原来/竟然/居然/反转）→ 回测点击率 +87%  → 加分
    - 标题长度落在 21-30 字             → 回测最优区间        → 加分
    - 赛事名词（世界杯/决赛/小组赛…）无具体人物/球队 → 回测 CTR 0.47% 垫底 → 减分
    - 情绪词（炸/崩/泪/狠/绝了）        → 回测点击率 -15%     → 减分

本模块唯一职责：对一条标题给出「正向/负向」分数修正项，供 score_title_plan /
score_fixture_title 等评分器累加。它**只打分、不生成、不阻塞**——配置不可用
则降级放行（bonus=0, degraded=True），绝不因读不到配置而拖红任何标题。

纪律（与全仓一致，并刻意与 title_antipattern 的硬规则区分）：
  - 所有判罚口径来自 config/title_positive.yaml（分数 = 加分/减分，非 0 分硬打回）；
  - 配置不可用 → **加分 0、标记 degraded、放行**，绝不阻塞发布（计划九）；
  - 本模块只判定、不生成、不介入选题；
  - 所有 reasons 以「R11数据:」前缀，便于下游 verify 把这批理由与原本的硬违规
    理由分桶——历史存量标题若被本规则减到 0 分，应归入 r11 桶而非 A1 硬违规桶。

关于「反转」的双向计分：反转既在 title_antipattern 的钩子词表里（作为冲突/悬念
钩子被奖励），也是本模块悬念词之一（+87% 的悬念收益已实测包含它）。两个维度的
加分都真实存在且有数据支撑，故此处保留悬念加分，属「有意叠加」而非口径冲突。

关于「崩盘」豁免：「崩盘」已是 plan-3.3 既有的冲突词（被冲突维度奖励），故情绪
减分在探测标题时先剔除「崩盘」再判定「崩」，避免对同一词既奖又罚。

关于「联赛名」不进赛事名词表：中超/英超/西甲/意甲/德甲/法甲是有效的中国足球
内容类别，且回测中并未测得联赛名作标题核心会垫底，故本列表只收世界杯/决赛/小组赛
等「大而无当」的赛事泛指名词（详见 yaml 注释）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import title_antipattern  # 复用其 ~707 人名的 person_names() 表做具体人物判定

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "title_positive.yaml"

# 兜底仅用于「配置存在但字段缺失」时的最小可用集；配置整体不可用则放行（见 evaluate）。
_FALLBACK = {
    "版本": "fallback",
    "生效日期": "2026-10-12",
    "口径出处": "R11 内容诊断（头条后台 869 条真实图文回测）",
    "悬念词": ["没想到", "原来", "竟然", "居然", "反转"],
    "最优长度带": [21, 30],
    "赛事名词": ["世界杯", "决赛", "小组赛", "淘汰赛", "半决赛"],
    "情绪词": ["炸", "崩", "泪", "狠", "绝了"],
    "加分": {"悬念": 12, "最优带": 10},
    "减分": {"赛事名词": 12, "情绪词": 8},
}

_CACHE: dict = {}


def load_config(path: Path | str | None = None) -> dict | None:
    """读取规则配置；不可用返回 None（调用方据此放行，不阻塞）。"""
    p = Path(path or CONFIG_PATH)
    key = str(p)
    if key in _CACHE:
        return _CACHE[key]
    cfg = None
    try:
        import yaml
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                cfg = data
    except Exception:
        cfg = None
    _CACHE[key] = cfg
    return cfg


def evaluate(title, *, known_teams=None, has_specific=False, cfg=None) -> dict:
    """对标题给出 R11 数据结论的加分/减分。

    返回 dict：
      bonus     int —— 正为加分、负为减分、0 为无影响；
      reasons   list[str] —— 所有理由均以「R11数据:」前缀，便于下游分桶；
      detail    list[str] —— 调试/可观测细节（空标题或降级时给一句说明）；
      degraded  bool —— 配置不可用，已放行（不阻塞）。

    设计约束（与硬规则区分）：本函数**永不硬阻塞**——即便减分项把分数拉到很低，
    也只是减去一个有限分值；配置不可用时直接 bonus=0 并标记 degraded。
    """
    title = (title or "").strip()
    if not title:
        return {"bonus": 0, "reasons": [], "detail": ["标题为空，不判定"], "degraded": False}

    raw = load_config() if cfg is None else cfg
    degraded = raw is None
    cfg = raw or _FALLBACK
    if degraded:
        return {"bonus": 0, "reasons": [],
                "detail": ["配置不可用（config/title_positive.yaml 缺失或不可解析），本规则放行"],
                "degraded": True}

    suspense = [str(x) for x in (cfg.get("悬念词") or [])]
    band = cfg.get("最优长度带") or [21, 30]
    lo, hi = int(band[0]), int(band[1])
    event_nouns = [str(x) for x in (cfg.get("赛事名词") or [])]
    emotion = [str(x) for x in (cfg.get("情绪词") or [])]
    add = cfg.get("加分") or {}
    sub = cfg.get("减分") or {}

    bonus = 0
    reasons: list[str] = []
    detail: list[str] = []

    # 1) 悬念词 → 加分（命中一次即加，避免叠加放大）
    hit_sus = next((w for w in suspense if w in title), None)
    if hit_sus is not None:
        bonus += int(add.get("悬念", 0))
        reasons.append(f"R11数据:标题含悬念词「{hit_sus}」，回测点击率 +87%")
        detail.append(f"悬念词 {hit_sus} → +{add.get('悬念', 0)}")

    # 2) 最优长度带 → 加分
    if lo <= len(title) <= hi:
        bonus += int(add.get("最优带", 0))
        reasons.append(f"R11数据:标题 {len(title)} 字落在 21-30 回测最优区间")
        detail.append(f"长度 {len(title)} 字 ∈ [{lo},{hi}] → +{add.get('最优带', 0)}")

    # 3) 赛事名词且无具体人物/球队 → 减分
    #    具体性 = 具体人名（title_antipattern 的 ~707 表）∪ known_teams 命中 ∪ has_specific
    persons = title_antipattern.person_names()
    known_teams = {str(x) for x in (known_teams or set()) if str(x).strip()}
    specific = (
        bool(has_specific)
        or any(p in title for p in persons)
        or any(t in title for t in known_teams)
    )
    hit_event = next((w for w in event_nouns if w in title), None)
    if hit_event is not None and not specific:
        bonus -= int(sub.get("赛事名词", 0))
        reasons.append(
            f"R11数据:标题含赛事名词「{hit_event}」且无具体人物/球队，"
            f"回测 CTR 0.47% 垫底，请把具体的人/队提到最前")
        detail.append(f"赛事名词 {hit_event}（无具体性）→ -{sub.get('赛事名词', 0)}")
    elif hit_event is not None and specific:
        detail.append(f"赛事名词 {hit_event} 但已含具体人物/球队 → 豁免减分")

    # 4) 情绪词 → 减分（崩盘豁免：先剔除「崩盘」再判「崩」）
    title_probe = title.replace("崩盘", "")
    hit_emo = None
    for w in emotion:
        target = title_probe if w == "崩" else title
        if w in target:
            hit_emo = w
            break
    if hit_emo is not None:
        bonus -= int(sub.get("情绪词", 0))
        reasons.append(f"R11数据:标题含情绪词「{hit_emo}」，回测点击率 -15%")
        detail.append(f"情绪词 {hit_emo} → -{sub.get('情绪词', 0)}")

    return {"bonus": int(bonus), "reasons": reasons, "detail": detail, "degraded": False}


def selftest() -> list[tuple[str, bool]]:
    """冒烟自检：每条规则的方向与数值正确、降级放行。"""
    cases: list[tuple[str, bool]] = []

    def _pad(base: str, n: int) -> str:
        base = base or ""
        if len(base) >= n:
            return base
        return base + "某" * (n - len(base))

    # 悬念词 → +12
    cases.append(("悬念词加分 +12", evaluate(_pad("没想到", 22))["bonus"] == 12 + 10))
    # 21-30 最优带 → +10（单独看长度贡献，用无悬念/无事件/无情绪的标题）
    cases.append(("21-30 最优带 +10",
                  evaluate(_pad("这场较量", 25))["bonus"] == 10))
    # 18-20 / 31+ → 无最优带加分
    cases.append(("18-20 字无最优带", evaluate(_pad("这场较量", 19))["bonus"] == 0))
    cases.append(("31+ 字无最优带", evaluate(_pad("这场较量", 33))["bonus"] == 0))

    # 赛事名词无具体性 → -12
    cases.append(("赛事名词-only -12", evaluate(_pad("世界杯", 15))["bonus"] == -12))
    # 赛事名词 + 具体人名（梅西）→ 豁免
    cases.append(("赛事名词+梅西 豁免",
                  evaluate(_pad("世界杯梅西", 15))["bonus"] == 0))
    # 赛事名词 + known_teams 命中 → 豁免
    cases.append(("赛事名词+known_teams 豁免",
                  evaluate(_pad("世界杯皇马", 15), known_teams={"皇马"})["bonus"] == 0))
    # 赛事名词 + has_specific=True → 豁免
    cases.append(("赛事名词+has_specific 豁免",
                  evaluate(_pad("世界杯", 15), has_specific=True)["bonus"] == 0))
    # 联赛名（中超）不在赛事名词表 → 永不减分
    cases.append(("联赛名中超 不减分",
                  evaluate(_pad("中超争冠白热化", 15))["bonus"] == 0))

    # 情绪词 → -8
    cases.append(("情绪词炸 -8", evaluate("防线炸了")["bonus"] == -8))
    # 崩盘豁免：下半场崩盘 不应触发「崩」的情绪减分
    cases.append(("崩盘豁免", evaluate("下半场崩盘")["bonus"] == 0))
    # 巴萨崩了 → 触发「崩」情绪减分（无崩盘）
    cases.append(("巴萨崩了 -8", evaluate("巴萨崩了")["bonus"] == -8))

    # 空标题 → bonus 0
    r_empty = evaluate("")
    cases.append(("空标题 bonus 0",
                  r_empty["bonus"] == 0 and r_empty["reasons"] == []
                  and r_empty["detail"] == ["标题为空，不判定"]))

    # 降级：配置不可用 → bonus 0 且 degraded
    _orig = globals()["load_config"]
    globals()["load_config"] = lambda *a, **k: None
    try:
        r_deg = evaluate("世界杯来了")
    finally:
        globals()["load_config"] = _orig
    cases.append(("配置不可用 → 放行且 degraded",
                  r_deg["bonus"] == 0 and r_deg["degraded"] is True))

    # 组合算术：悬念 + 最优带 + 赛事名词 + 情绪词 = 12 + 10 - 12 - 8 = +2
    combo = "没想到这届决赛竟成绝唱谁能想到结局如此崩了"  # 22 字，含 没想到/决赛/崩
    cases.append(("组合算术 12+10-12-8=+2", evaluate(combo)["bonus"] == 2))

    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="标题正向规则（R11 内容诊断结论）")
    ap.add_argument("--title", help="评估单条标题并打印结果")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.title:
        r = evaluate(args.title)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    cfg = load_config() or _FALLBACK
    print(f"标题正向规则 v{cfg.get('版本')}（R11 内容诊断结论）")
    print(f"  口径出处：{cfg.get('口径出处', '')}")
    print(f"  生效日期：{cfg.get('生效日期', '')}")
    print(f"  悬念词：{cfg.get('悬念词')}；最优长度带：{cfg.get('最优长度带')}")
    print(f"  赛事名词：{cfg.get('赛事名词')}；情绪词：{cfg.get('情绪词')}")
    print(f"  加分：{cfg.get('加分')}；减分：{cfg.get('减分')}")
    print(f"  注：本模块只打分、不阻塞；reasons 以「R11数据:」前缀便于下游分桶。")
    print("\n冒烟自检：")
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检结果：{'全部通过 ✅' if not bad else f'{bad} 项未通过 ❌'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
