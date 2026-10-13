#!/usr/bin/env python3
"""标题反模式闸门 —— 「赛果播报式」+「纯赛事名词式」硬规则（完善计划 3.3 / 第十章）。

背景（2026-10-12 头条后台 869 条真实图文回测）：
  最大的一块亏损内容形态，不是写得不够多，而是**把比分/赛果当标题核心**。
  含比分的标题 173 条（占产量 21.6%）点击率仅 0.46%（全体 1.04%）、0 个爆款；
  其中「以比分/赛果开场 + 无具体人物 + 无悬念/评价钩子」45 条更是 0 条达到百阅读、
  最高仅 67。而「有具体人物」的标题均值阅读 310（全体 111）、贡献 6 个爆款。

  第二块亏损形态（2026-10-13 R11 内容诊断）：把**赛事泛指名词**（世界杯/决赛/小组赛…）
  当标题核心。同一形态此前只在 title_positive 里做「减分 12」，而 score_title_plan
  及格线 60 → **不扣也及格**，实测采用率 0%，即减分是无效约束；故升级为同级硬打回。

本模块唯一职责：判定一条标题是否命中上述反模式，供生成侧打回重写。
  每条规则都要求下列条件**同时**成立才算命中（宁漏勿误杀）：
    1. 信号出现在标题起首（起始位置 ≤ 开场窗口）——规则一是赛果/比分，规则二是赛事名词；
    2. 不含具体人物名（球员/教练/裁判/解说；人工表 ∪ entity_map 球员）；
    3. 不含叙事钩子（词表 ∪ 疑问号）；规则二另要求不含具体球队名（entity_map club）。

纪律（与全仓一致）：
  - 命中的判罚口径来自 config/title_antipattern.yaml（分数 0 = 硬违规打回重写）；
  - 配置不可用 → **判定放行并置 degraded**，绝不因读不到配置而阻塞发布（计划九）；
  - 本模块只判定、不生成、不介入选题。

命令行：
  python3 title_antipattern.py --selftest
  python3 title_antipattern.py --title "7-0！长沙小将把对手打成了背景板"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "title_antipattern.yaml"
ENTITY_MAP_PATH = PROJECT_ROOT / "config" / "entity_map.json"

# 兜底仅用于「配置存在但字段缺失」时的最小可用集；配置整体不可用则放行（见 check）。
_FALLBACK = {
    "版本": "fallback",
    "规则名": "赛果播报式",
    "开场窗口": 4,
    "比分模式": [r"\d+\s*[-:比]\s*\d+"],
    "赛果模式": ["大胜|血洗|绝杀|逆转|爆冷|告负|平局|取胜|击败|战胜|输给|横扫|惨败|小胜|战平|不敌|险胜|逼平"],
    "人物名": ["梅西", "C罗", "姆巴佩", "哈兰德", "内马尔", "凯恩", "萨拉赫", "孙兴慜", "武磊"],
    "钩子模式": ["没想到|原来|竟然|居然|反转|翻车|凭什么|为什么|到底|究竟|其实|真相", "[?？]"],
    "判罚": {"分数": 0, "动作": "打回重写", "提示": "标题不要以比分/赛果开场。"},
    "规则二": {
        "规则名": "纯赛事名词式",
        "开场窗口": 4,
        "赛事名词": ["世界杯", "欧洲杯", "美洲杯", "亚洲杯", "世预赛", "欧冠", "欧联", "欧协联",
                     "小组赛", "淘汰赛", "半决赛", "决赛", "揭幕战"],
        "球队名": [],
        "判罚": {"分数": 0, "动作": "打回重写",
                 "提示": "标题不要以「世界杯/决赛/小组赛」这类赛事泛指名词打头。请把「谁」提到最前面。"},
    },
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


def _entity_player_names() -> set[str]:
    """entity_map.json 里的球员中文名与别名（build_player_map.py 增量维护）。"""
    names: set[str] = set()
    try:
        data = json.loads(ENTITY_MAP_PATH.read_text(encoding="utf-8"))
        ents = data.get("entities") or {}
        for k, v in ents.items():
            if not str(k).startswith("player"):
                continue
            if v.get("name_zh"):
                names.add(str(v["name_zh"]))
            for a in (v.get("aliases") or []):
                names.add(str(a))
    except Exception:
        pass
    return names


def person_names(cfg: dict | None = None) -> set[str]:
    """具体人物名集合 = 配置人工表 ∪ entity_map 球员译名。"""
    cfg = cfg if cfg is not None else (load_config() or _FALLBACK)
    names = {str(x) for x in (cfg.get("人物名") or []) if str(x).strip()}
    names |= _entity_player_names()
    return names


def _entity_club_names() -> set[str]:
    """entity_map.json 里的球队中文名与别名（club_* 条目）。"""
    names: set[str] = set()
    try:
        data = json.loads(ENTITY_MAP_PATH.read_text(encoding="utf-8"))
        ents = data.get("entities") or {}
        for k, v in ents.items():
            if not str(k).startswith("club"):
                continue
            if v.get("name_zh"):
                names.add(str(v["name_zh"]))
            for a in (v.get("aliases") or []):
                names.add(str(a))
    except Exception:
        pass
    return names


def rule2_config(cfg: dict | None = None) -> dict:
    """规则二（纯赛事名词式）配置块；缺失时用兜底最小集。"""
    cfg = cfg if cfg is not None else (load_config() or _FALLBACK)
    r2 = cfg.get("规则二")
    return r2 if isinstance(r2, dict) and r2 else _FALLBACK["规则二"]


def team_names(cfg: dict | None = None) -> set[str]:
    """具体球队名集合 = 规则二配置「球队名」 ∪ entity_map 的 club 译名。"""
    cfg = cfg if cfg is not None else (load_config() or _FALLBACK)
    rule2 = rule2_config(cfg)
    names = {str(x) for x in (rule2.get("球队名") or []) if str(x).strip()}
    names |= _entity_club_names()
    return names


def tournament_names(cfg: dict | None = None) -> set[str]:
    """规则二的赛事泛指名词集合（世界杯/决赛/小组赛…；刻意不含联赛名）。"""
    rule2 = rule2_config(cfg)
    return {str(x) for x in (rule2.get("赛事名词") or []) if str(x).strip()}


def _patterns(cfg: dict, key: str) -> list[re.Pattern]:
    out = []
    for pat in (cfg.get(key) or []):
        try:
            out.append(re.compile(str(pat)))
        except re.error:
            continue
    return out


def _first_on_open(title: str, pats: list[re.Pattern], window: int):
    """返回所有模式中「起始位置 ≤ window」的最早匹配；无则 None。"""
    best = None
    for p in pats:
        m = p.search(title)
        if m and m.start() <= window:
            if best is None or m.start() < best.start():
                best = m
    return best


def _first_literal_on_open(title: str, names: set[str], window: int):
    """字面词表版：返回「起始位置 ≤ window 」的最早命中 (词, 位置)；无则 None。"""
    best: tuple[str, int] | None = None
    for n in names:
        at = title.find(n)
        if at < 0 or at > window:
            continue
        if best is None or at < best[1]:
            best = (n, at)
    return best


def _exemptions(title: str, cfg: dict, hook_pats: list[re.Pattern],
                *, with_teams: bool) -> tuple[list, list, list]:
    """返回 (人物命中, 球队命中, 钩子命中)；任一非空即「有具体性」，放行不拦。"""
    who = [p for p in person_names(cfg) if p in title]
    teams = [t for t in team_names(cfg) if t in title] if with_teams else []
    hooks = [p.pattern for p in hook_pats if p.search(title)]
    return who, teams, hooks


def _hit_rule1(title: str, cfg: dict, hook_pats: list[re.Pattern]) -> dict | None:
    """规则一：赛果播报式。命中返回判罚 dict，否则 None。"""
    window = int(cfg.get("开场窗口", _FALLBACK["开场窗口"]))
    score_pats = _patterns(cfg, "比分模式") or _patterns(_FALLBACK, "比分模式")
    result_pats = _patterns(cfg, "赛果模式") or _patterns(_FALLBACK, "赛果模式")

    m_score = _first_on_open(title, score_pats, window)
    m_result = _first_on_open(title, result_pats, window)
    signal = None
    if m_score is not None and (m_result is None or m_score.start() <= m_result.start()):
        signal = ("比分", m_score)
    elif m_result is not None:
        signal = ("赛果", m_result)
    if signal is None:                       # 条件一不成立
        return None

    who, _teams, hooks = _exemptions(title, cfg, hook_pats, with_teams=False)
    if who or hooks:                          # 条件二/三成立 → 放行
        return None

    penalty = cfg.get("判罚") or {}
    kind, m = signal
    return {
        "ok": False, "hit": True, "degraded": False,
        "rule": cfg.get("规则名", "赛果播报式"),
        "signal": {"kind": kind, "text": m.group(0), "at": m.start()},
        "reasons": [
            f"标题以{kind}开场（「{m.group(0)}」在起首 {m.start()} 字内）",
            "全文无具体人物名",
            "无悬念/评价/叙事钩子",
        ],
        "hint": str(penalty.get("提示") or _FALLBACK["判罚"]["提示"]),
        "penalty_score": int(penalty.get("分数") or 0),
        "detail": [f"命中「{cfg.get('规则名', '赛果播报式')}」反模式，按配置判 {int(penalty.get('分数') or 0)} 分并打回重写"],
    }


def _hit_rule2(title: str, cfg: dict, hook_pats: list[re.Pattern]) -> dict | None:
    """规则二：纯赛事名词式。命中返回判罚 dict，否则 None。"""
    r2 = rule2_config(cfg)
    window = int(r2.get("开场窗口", cfg.get("开场窗口", _FALLBACK["开场窗口"])))
    hit = _first_literal_on_open(title, tournament_names(cfg), window)
    if hit is None:                           # 条件一不成立
        return None

    who, teams, hooks = _exemptions(title, cfg, hook_pats, with_teams=True)
    if who or teams or hooks:                 # 条件二/三成立 → 放行
        return None

    noun, at = hit
    penalty = r2.get("判罚") or {}
    rule = r2.get("规则名", _FALLBACK["规则二"]["规则名"])
    return {
        "ok": False, "hit": True, "degraded": False,
        "rule": rule,
        "signal": {"kind": "赛事名词", "text": noun, "at": at},
        "reasons": [
            f"标题以赛事名词开场（「{noun}」在起首 {at} 字内）",
            "全文无具体人物名、无具体球队名",
            "无悬念/评价/叙事钩子",
        ],
        "hint": str(penalty.get("提示") or _FALLBACK["规则二"]["判罚"]["提示"]),
        "penalty_score": int(penalty.get("分数") or 0),
        "detail": [f"命中「{rule}」反模式，按配置判 {int(penalty.get('分数') or 0)} 分并打回重写"],
    }


def check(title: str, *, cfg: dict | None = None) -> dict:
    """判定标题是否命中标题反模式（规则一 赛果播报式 / 规则二 纯赛事名词式）。

    返回 dict：
      ok          True = 放行；False = 命中反模式（应打回重写）
      hit         bool，是否命中
      degraded    True = 配置不可用，已放行（不阻塞）
      rule / reasons / hint / penalty_score / signal / detail
    """
    title = (title or "").strip()
    if not title:
        return {"ok": True, "hit": False, "degraded": False, "rule": None,
                "reasons": [], "hint": "", "penalty_score": 0, "detail": ["标题为空，不判定"]}

    raw = cfg if cfg is not None else load_config()
    degraded = raw is None
    cfg = raw or _FALLBACK
    if degraded:
        return {"ok": True, "hit": False, "degraded": True,
                "rule": cfg.get("规则名"), "reasons": [], "hint": "",
                "penalty_score": 0,
                "detail": ["配置不可用（config/title_antipattern.yaml 缺失或不可解析），本闸门放行"]}

    hook_pats = _patterns(cfg, "钩子模式") or _patterns(_FALLBACK, "钩子模式")

    for probe in (_hit_rule1(title, cfg, hook_pats), _hit_rule2(title, cfg, hook_pats)):
        if probe is not None:
            return probe

    return {"ok": True, "hit": False, "degraded": False, "rule": cfg.get("规则名"),
            "reasons": [], "hint": "", "penalty_score": 0,
            "detail": ["未命中「赛果播报式 / 纯赛事名词式」任一条，放行"]}


def selftest() -> list[tuple[str, bool]]:
    """冒烟自检：正例必拦、负例必放、降级必放行。"""
    cases: list[tuple[str, bool]] = []

    # 正例：赛果播报式（应拦）
    for t in ("3-1暴揍中亚狼",
              "5-1荷兰拿下瑞典",
              "曼联点球6-5利兹联",
              "亚泰主场1-5惨败",
              "7-0踢崩对手"):
        cases.append((f"应拦：{t}", check(t)["hit"] is True))

    # 负例：含具体人物（应放）
    for t in ("梅西绝杀皇马2-1",
              "张玉宁62球追平队史第一！国安残阵逼平领头羊",
              "曼联点球6-5利兹联，B费丢点"):
        cases.append((f"应放（含人物）：{t}", check(t)["hit"] is False))

    # 负例：含悬念/评价钩子（应放）
    for t in ("4-3！切尔西这场球看得我心脏受不了",
              "1-0的争议，比丢球更难咽",
              "0-5！国足又输巴勒斯坦，这剧本我们看了二十多年"):
        cases.append((f"应放（含钩子）：{t}", check(t)["hit"] is False))

    # 负例：比分不在起首（应放）
    for t in ("西班牙4-1克罗地亚，最动人的却是第81分钟那一幕",
              "别被4比0骗了，韩国队这进攻还没成型"):
        cases.append((f"应放（比分不在起首/有钩子）：{t}", check(t)["hit"] is False))

    # 规则二正例：纯赛事名词式（应拦）
    for t in ("世界杯夺冠赔率榜出炉，豪门集体看衰",
              "欧冠抽签结果出炉，强强对话提前上演",
              "小组赛第二轮，出线名额争夺进入白热化",
              "决赛前的准备，两队都在憋大招"):
        cases.append((f"应拦（规则二·赛事名词）：{t}", check(t)["hit"] is True))

    # 规则二负例：含具体人物 / 具体球队 / 钩子（应放）
    for t in ("世界杯决赛，梅西圆梦",
              "世界杯决赛，姆巴佩上演帽子戏法",
              "欧冠决赛，皇马第15次捧杯",
              "世界杯小组赛，为什么总是这几支球队出局",
              "中超决赛悬念留到最后，这剧本太熟悉了"):
        cases.append((f"应放（规则二豁免）：{t}", check(t)["hit"] is False))

    # 规则二负例：赛事名词不在起首（应放）
    for t in ("三支球队的世界杯出线形势分析",
              "他等了八年，才等到这届世界杯"):
        cases.append((f"应放（规则二·不在起首）：{t}", check(t)["hit"] is False))

    # 降级：配置不可用必须放行，绝不阻塞
    _orig = globals()["load_config"]
    globals()["load_config"] = lambda *a, **k: None
    try:
        r1 = check("3-1暴揍中亚狼")
        r2 = check("世界杯小组赛出线形势分析")
    finally:
        globals()["load_config"] = _orig
    cases.append(("配置不可用 → 放行且标记 degraded（规则一）",
                  r1["ok"] is True and r1["degraded"] is True))
    cases.append(("配置不可用 → 放行且标记 degraded（规则二）",
                  r2["ok"] is True and r2["degraded"] is True))

    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="标题反模式闸门（赛果播报式 / 纯赛事名词式）")
    ap.add_argument("--title", help="判定单条标题并打印结果")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.title:
        r = check(args.title)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1

    cfg = load_config() or _FALLBACK
    r2 = rule2_config(cfg)
    print(f"标题反模式闸门 v{cfg.get('版本')}（规则一：{cfg.get('规则名')} / 规则二：{r2.get('规则名')}）")
    print(f"  口径出处：{cfg.get('口径出处', '')}")
    print(f"  规则一·开场窗口：{cfg.get('开场窗口')}；判罚：{cfg.get('判罚')}")
    print(f"  规则二·开场窗口：{r2.get('开场窗口', cfg.get('开场窗口'))}；判罚：{r2.get('判罚')}")
    print(f"  人物名表：{len(person_names(cfg))} 个（人工表 {len(cfg.get('人物名') or [])} + entity_map 球员）")
    print(f"  赛事名词：{len(tournament_names(cfg))} 个；具体球队名：{len(team_names(cfg))} 个（含 entity_map club）")
    print("\n冒烟自检：")
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检结果：{'全部通过 ✅' if not bad else f'{bad} 项未通过 ❌'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
