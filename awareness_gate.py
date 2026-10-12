#!/usr/bin/env python3
"""受众认知度分级硬门槛（计划 13.1「中文实体映射层」）。

计划 13.1 的原文口径（映射表 awareness 列 + 分级处理表）：

  A 泛球迷普遍认识（皇马、曼城、姆巴佩） → 可进新闻流
  B 关注者认识（那不勒斯、勒沃库森）     → 需配认知锚点，或改走故事角度
  C 仅核心球迷认识（科莫、小基恩）       → 降权，或只进数据类与榜单类

另有三条附注同样属硬规则：
  - **未登记的实体不得进入生成环节**（该规则直接复用一致性校验器）；
  - C 级标 confidence=low，**不进标题生成**，仅作背景数据；
  - 「78 篇欧冠仅获 852 阅读」的根因不是语言，而是选题落在 C 级实体上。

为什么要有"通道（channel）"这一维：
  同一张分级表，落地动作随供给通道不同（计划表 19 与表 25 的交叉约束）：
  - ``news``（新闻流 / 二级·突发层）：L1 预筛要剔除「主体冷门度超阈值」的候选；
    A 放行，B 需锚点，**C 一律不进新闻流**。
  - ``data_list``（数据类 / 榜单类 / 一级·确定性供给）：按 13.1，C 可"只进数据类与榜单类"，
    即在这些板块内放行但标记降权。

三条硬规则（本模块唯一职责，绝不猜测、绝不默认放行）：
  1. 未登记 → 拒绝（不得进入生成环节）。
  2. ``news`` 通道 C 级一律拒绝；其他通道 C 级仅允许落在数据类/榜单类板块。
  3. C 级（confidence=low）不参与标题生成，仅作背景数据。

命令行：
  python3 awareness_gate.py --selftest        # 跑表驱动规则自检
  python3 awareness_gate.py --explain 科莫 --section 转会动态 --channel news
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ENTITY_MAP_PATH = PROJECT_ROOT / "config" / "entity_map.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LEVELS = ("A", "B", "C")

#: 未登记实体（不在映射表中）。**绝不**等同于 C 级 —— C 级是"认识的人少"，未登记是"没定过译名"。
UNREGISTERED = "?"

#: 认知度 → 置信度（与 scripts/build_player_map.py 的 CONF 保持一致，计划 13.1 扩展字段）
CONFIDENCE = {"A": "high", "B": "medium", "C": "low"}

#: C 级实体允许进入的板块（计划 13.1「只进数据类与榜单类」）
C_ALLOWED_SECTIONS = ("战术榜单", "数据对照", "数据类", "榜单类", "排行榜")

#: B 级实体可"改走故事角度"以替代认知锚点的板块
STORY_SECTIONS = ("人物故事",)

CHANNELS = ("news", "data_list")

_map_cache: dict | None = None


# ------------------------------------------------------------
# 映射表加载
# ------------------------------------------------------------
def parse_entity_map(path: Path | str | None = None) -> dict:
    """读 config/entity_map.json → {name_zh: awareness}；缺文件/坏文件 → {}。"""
    p = Path(path) if path else ENTITY_MAP_PATH
    out: dict[str, str] = {}
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return out
    for v in (data.get("entities") or {}).values():
        zh = (v or {}).get("name_zh")
        if not zh:
            continue
        lvl = str((v or {}).get("awareness") or "").strip().upper()
        out[zh] = lvl if lvl in LEVELS else UNREGISTERED
    return out


def load_map(path: Path | str | None = None) -> dict:
    """带缓存的映射表（{name_zh: A/B/C/?}）。"""
    global _map_cache
    if _map_cache is None:
        _map_cache = parse_entity_map(path)
    return _map_cache


def reset_cache() -> None:
    global _map_cache
    _map_cache = None


def level_of(name: str, emap: dict | None = None) -> str:
    """实体认知度：A/B/C；未登记 → ``UNREGISTERED``（不猜测、不默认成 C）。"""
    if not name:
        return UNREGISTERED
    m = emap if emap is not None else load_map()
    return m.get(str(name).strip(), UNREGISTERED)


def is_registered(name: str, emap: dict | None = None) -> bool:
    return level_of(name, emap) in LEVELS


def confidence_of(name: str, emap: dict | None = None) -> str:
    """A→high / B→medium / C→low；未登记 → ``""``（无置信度可言）。"""
    return CONFIDENCE.get(level_of(name, emap), "")


def can_generate_title(name: str, emap: dict | None = None) -> bool:
    """计划 13.1：C 级（confidence=low）不参与标题生成，仅作背景数据。"""
    return level_of(name, emap) in ("A", "B")


def classify(value: str) -> str:
    """把任意输入规整成 A/B/C/UNREGISTERED（用于配置项校验，未知即未登记）。"""
    v = str(value or "").strip().upper()
    return v if v in LEVELS else UNREGISTERED


# ------------------------------------------------------------
# 决策
# ------------------------------------------------------------
def _section_allowed_for_c(section: str) -> bool:
    s = str(section or "").strip()
    return any(k in s for k in C_ALLOWED_SECTIONS)


def _is_story_section(section: str) -> bool:
    s = str(section or "").strip()
    return any(k in s for k in STORY_SECTIONS)


def decide_level(level: str, section: str = "", *, channel: str = "news",
                 has_anchor: bool = False, name: str = "") -> dict:
    """按计划 13.1 判定单个实体在给定板块/通道下的动作。

    action：
      ``pass``        放行（A 级，或 B 级已配锚点/走故事角度，或数据通道里的 C 级降权）
      ``demote``      放行但降权（C 级进数据/榜单类）
      ``needs_anchor``放行但必须补认知锚点或改走故事角度（B 级）
      ``block``       拒绝（未登记 / C 级进新闻流或非数据板块）
    """
    lvl = classify(level)
    ch = str(channel or "news").strip() or "news"
    sec = str(section or "").strip()

    def res(action: str, ok: bool, reason: str, **extra) -> dict:
        d = {"name": name, "level": lvl, "section": sec, "channel": ch,
             "action": action, "ok": ok, "reason": reason,
             "registered": lvl in LEVELS,
             "confidence": CONFIDENCE.get(lvl, ""),
             "title_allowed": lvl in ("A", "B"),
             "demote": action == "demote",
             "requires_anchor": action == "needs_anchor"}
        d.update(extra)
        return d

    if lvl == UNREGISTERED:
        return res("block", False,
                   "未登记实体不得进入生成环节（计划 13.1）")

    if lvl == "C":
        if ch == "news":
            return res("block", False,
                       f"C 级实体降权：不进新闻流，仅可用于数据类/榜单类（{name or '主体'}）")
        if not _section_allowed_for_c(sec):
            return res("block", False,
                       f"C 级实体只允许落在数据类/榜单类板块，当前为「{sec or '未指定'}」")
        return res("demote", True,
                   f"C 级实体降权放行（数据/榜单类：{sec}），不参与标题生成")

    if lvl == "B":
        if has_anchor or _is_story_section(sec):
            return res("pass", True,
                       "B 级实体：已配认知锚点" if has_anchor
                       else f"B 级实体改走故事角度（{sec}）")
        return res("needs_anchor", True,
                   "B 级实体需配认知锚点，或改走故事角度")

    return res("pass", True, "A 级实体：可进新闻流")


def decide(name: str, section: str = "", *, channel: str = "news",
           has_anchor: bool = False, emap: dict | None = None) -> dict:
    """按实体名判定（先查映射表，再走 :func:`decide_level`）。"""
    return decide_level(level_of(name, emap), section,
                        channel=channel, has_anchor=has_anchor, name=str(name or ""))


def subject_of(candidate) -> str:
    """从候选/事实卡里取主体（兼容 {card:{...}} 与裸事实卡两种形态）。"""
    if not isinstance(candidate, dict):
        return ""
    card = candidate.get("card") if isinstance(candidate.get("card"), dict) else candidate
    return str(card.get("主体") or card.get("subject") or "")


def section_of(candidate) -> str:
    if not isinstance(candidate, dict):
        return ""
    card = candidate.get("card") if isinstance(candidate.get("card"), dict) else candidate
    return str(candidate.get("板块") or candidate.get("section")
               or card.get("板块") or card.get("section") or "")


def gate_candidates(candidates, *, channel: str = "news", emap: dict | None = None):
    """批量判定，返回 ``(kept, dropped, decisions)``。

    kept 里的候选会带上 ``_awareness`` 决策（供装配层补锚点/降权排序）。
    """
    kept, dropped, decisions = [], [], []
    for c in (candidates or []):
        name = subject_of(c)
        dec = (decide(name, section_of(c), channel=channel, emap=emap)
               if name else
               decide_level("A", section_of(c), channel=channel, name=""))
        decisions.append(dec)
        if not dec["ok"]:
            dropped.append((c, f"认知度门槛：{dec['reason']}"))
            continue
        if isinstance(c, dict):
            c["_awareness"] = dec
            if dec["demote"]:
                c["_demoted"] = True
            if dec["requires_anchor"]:
                c["_requires_anchor"] = True
        kept.append(c)
    return kept, dropped, decisions


def summarize(decisions) -> dict:
    """统计各动作/等级条数，便于看板与验收。"""
    from collections import Counter
    acts, lvls = Counter(), Counter()
    for d in (decisions or []):
        acts[d.get("action", "?")] += 1
        lvls[d.get("level", "?")] += 1
    return {"total": len(list(decisions or [])),
            "by_action": dict(acts), "by_level": dict(lvls),
            "blocked": acts.get("block", 0),
            "needs_anchor": acts.get("needs_anchor", 0),
            "demoted": acts.get("demote", 0)}


# ------------------------------------------------------------
# 表驱动规则自检（计划 13.1 分级表的可执行副本）
# ------------------------------------------------------------
#: (level, section, channel, has_anchor) → 期望 action
RULE_CASES = (
    ("A", "转会动态", "news", False, "pass"),
    ("A", "战术榜单", "news", False, "pass"),
    ("A", "战术榜单", "data_list", False, "pass"),
    ("B", "转会动态", "news", False, "needs_anchor"),
    ("B", "转会动态", "news", True, "pass"),
    ("B", "人物故事", "news", False, "pass"),
    ("B", "战术榜单", "news", False, "needs_anchor"),
    ("C", "转会动态", "news", False, "block"),
    ("C", "战术榜单", "news", False, "block"),
    ("C", "战术榜单", "data_list", False, "demote"),
    ("C", "数据对照", "data_list", False, "demote"),
    ("C", "转会动态", "data_list", False, "block"),
    (UNREGISTERED, "转会动态", "news", False, "block"),
    (UNREGISTERED, "战术榜单", "news", False, "block"),
    (UNREGISTERED, "战术榜单", "data_list", False, "block"),
    ("", "战术榜单", "data_list", False, "block"),
)


def selftest() -> list[dict]:
    """跑表驱动用例，返回不通过的项（空列表 = 全部符合计划 13.1）。"""
    bad = []
    for lvl, sec, ch, anchor, want in RULE_CASES:
        got = decide_level(lvl, sec, channel=ch, has_anchor=anchor)["action"]
        if got != want:
            bad.append({"level": lvl, "section": sec, "channel": ch,
                        "has_anchor": anchor, "want": want, "got": got})
    return bad


def render_selftest(bad=None) -> str:
    bad = selftest() if bad is None else bad
    L = ["# 认知度分级规则自检（计划 13.1）", "",
         f"- 用例数：{len(RULE_CASES)}　不通过：{len(bad)}",
         f"- 结论：**{'通过 ✅' if not bad else '未通过 ❌'}**", ""]
    if bad:
        for b in bad:
            L.append(f"- ❌ level={b['level']!r} section={b['section']} channel={b['channel']} "
                     f"anchor={b['has_anchor']}：期望 {b['want']}，实得 {b['got']}")
    else:
        L.append("A 可进新闻流／B 需锚点或走故事角度／C 降权或仅数据榜单／未登记不得进入生成 "
                 "—— 全部符合。")
    return "\n".join(L)


def _cli() -> int:
    ap = argparse.ArgumentParser(description="受众认知度分级硬门槛（计划 13.1）")
    ap.add_argument("--selftest", action="store_true", help="跑表驱动规则自检")
    ap.add_argument("--explain", metavar="NAME", help="解释某个实体的判定")
    ap.add_argument("--section", default="", help="板块名")
    ap.add_argument("--channel", default="news", choices=list(CHANNELS), help="供给通道")
    ap.add_argument("--anchor", action="store_true", help="该候选是否已配认知锚点")
    ap.add_argument("--stats", action="store_true", help="打印映射表 A/B/C 分布")
    args = ap.parse_args()

    if args.selftest:
        bad = selftest()
        print(render_selftest(bad))
        return 0 if not bad else 1

    if args.explain:
        dec = decide(args.explain, args.section, channel=args.channel, has_anchor=args.anchor)
        print(json.dumps(dec, ensure_ascii=False, indent=2))
        return 0 if dec["ok"] else 1

    m = load_map()
    from collections import Counter
    cnt = Counter(m.values())
    print(f"映射表实体：{len(m)}（A={cnt.get('A', 0)} B={cnt.get('B', 0)} "
          f"C={cnt.get('C', 0)} 未登记={cnt.get(UNREGISTERED, 0)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
