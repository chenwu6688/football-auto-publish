#!/usr/bin/env python3
"""传闻分级措辞系统 + 传闻标题硬校验（计划 4.1 / 3.2 / 13.3）。

计划表 4（扣分项与对策）里与本模块相关的两条：
  - 失实表述（扣 10 分）→ **传闻分级措辞系统**，并强制显式标注 AI 生成；
  - 严重标题党（扣 30 分）→ **传闻类标题禁用肯定语气**。

分级口径沿用事实卡三级可信度（计划 3.2，见 fact_card.py，本模块不另立标准）：
  - ``已确认``（来源 ≥2 个独立来源）→ 正文可用陈述句；
  - ``单源``（来源 =1 个）        → 正文须写「据某媒体报道」，标题宜携带信源（计划 13.3 表 27）；
  - ``传闻``（来源 =0 / 未核实） → 正文须写「传闻」「有消息称」，**标题禁用肯定语气**。

三条硬规则（本模块唯一职责，绝不猜测）：
  1. 传闻级**标题**：必须含模糊/信源标记，且不得含断言式动词（官宣/确定/敲定/达成/
     确认/签下/板上钉钉 …）。命中即 hard violation，标题必须重拟。
  2. 传闻级**正文**：必须出现「传闻」「有消息称」「据悉」「据…报道」等措辞；
     单源级正文必须出现「据…报道」一类来源措辞。
  3. 分级未知 → **不做任何判定**（不误杀、也不假装通过）：返回 ``tier=None``。
     调用方必须先确定分级；未登记分级视为流程缺陷，由验收脚本统计。

命令行：
  python3 rumor_wording.py --selftest
  python3 rumor_wording.py --tier 传闻 --title "曼城官宣签下某某" --content "……"
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# 分级常量统一取自 fact_card（计划 3.2 单一事实来源）；取不到时回落到同名字面量。
try:
    from fact_card import CONF_CONFIRMED, CONF_RUMOR, CONF_SINGLE
except Exception:                                   # pragma: no cover
    CONF_CONFIRMED, CONF_SINGLE, CONF_RUMOR = "已确认", "单源", "传闻"

TIERS = (CONF_CONFIRMED, CONF_SINGLE, CONF_RUMOR)

#: 由弱到强的保守序：一篇多事实成稿按**最弱**分级措辞（计划 4.1 取严不取宽）
_WEAKNESS = {CONF_RUMOR: 3, CONF_SINGLE: 2, CONF_CONFIRMED: 1}

#: 英文/别名字段取值 → 中文分级
_TIER_ALIASES = {
    "confirmed": CONF_CONFIRMED, "high": CONF_CONFIRMED, "已确认": CONF_CONFIRMED,
    "single": CONF_SINGLE, "medium": CONF_SINGLE, "单源": CONF_SINGLE,
    "rumor": CONF_RUMOR, "low": CONF_RUMOR, "传闻": CONF_RUMOR,
}

#: 标题/正文用的「模糊·信源」标记（传闻级必须至少命中一个）
HEDGE_MARKERS = (
    "传闻", "流言", "绯闻", "据悉", "据报", "据报道", "据媒体", "消息人士", "有消息",
    "曝", "曝光", "或", "或将", "可能", "预计", "接近", "有意", "考虑", "询价",
    "报价", "考察", "正在谈", "洽谈", "尚未", "未确认", "待定", "疑似", "被曝",
    "传出", "风声", "不排除",
)

#: 传闻级标题**禁用**的断言式动词/表述（计划 4.1「传闻类标题禁用肯定语气」）
ASSERTIVE_MARKERS = (
    "官宣", "正式宣布", "宣布", "确定", "敲定", "达成", "已签", "签下", "签约完成",
    "已完成", "板上钉钉", "确认", "实锤", "已成定局", "铁定", "锁定", "完成转会",
    "正式加盟", "确定加盟", "官宣加盟", "大结局", "一锤定音", "尘埃落定",
)

#: 单源级正文必须出现的「来源措辞」（计划 3.2）
SINGLE_BODY_REQUIRE = ("据",)

#: 单源级标题「宜携带信源」（计划 13.3 表 27：罗马诺：／天空：／图片报：）
SOURCE_TITLE_RE = re.compile(r"^\s*[^：:]{1,8}[：:]")

#: 标题里出现这些词即视为「传闻类标题」（用于历史产物扫描）
RUMOR_TITLE_HINTS = ("传闻", "流言", "绯闻", "曝", "据悉", "或", "有望", "有意", "被曝", "接近")


# ------------------------------------------------------------
# 分级解析
# ------------------------------------------------------------
def normalize_tier(value) -> str | None:
    """把任意取值规整成 已确认/单源/传闻；无法判定 → None（绝不猜）。"""
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if v in TIERS:
        return v
    return _TIER_ALIASES.get(v.lower())


def _iter_nested(obj, depth: int = 0):
    """在嵌套结构里找可能带分级的子对象（facts / cards / 关联事实卡）。"""
    if depth > 2 or not isinstance(obj, dict):
        return
    for key in ("facts", "cards", "fact_cards", "关联事实卡"):
        v = obj.get(key)
        if isinstance(v, dict):
            yield v
            yield from _iter_nested(v, depth + 1)
        elif isinstance(v, list):
            for it in v:
                if isinstance(it, dict):
                    yield it
                    yield from _iter_nested(it, depth + 1)


def tier_of(obj) -> str | None:
    """从选题/事实卡/facts 里解析分级；多处分级取**最弱**（计划 4.1 取严不取宽）。"""
    if isinstance(obj, str):
        return normalize_tier(obj)
    if not isinstance(obj, dict):
        return None
    keys = ("可信度", "fact_tier", "tier", "credibility", "confidence")
    found = []
    for key in keys:
        t = normalize_tier(obj.get(key))
        if t:
            found.append(t)
    for sub in _iter_nested(obj):
        for key in keys:
            t = normalize_tier(sub.get(key))
            if t:
                found.append(t)
    return weakest(found)


def weakest(tiers) -> str | None:
    vals = [t for t in (normalize_tier(x) for x in (tiers or [])) if t]
    return max(vals, key=lambda t: _WEAKNESS.get(t, 0)) if vals else None


# ------------------------------------------------------------
# 校验
# ------------------------------------------------------------
def title_violations(tier: str | None, title: str) -> list[str]:
    """标题违规项（传闻级为硬违规，单源级为提醒）。"""
    t = normalize_tier(tier)
    title = (title or "").strip()
    if not t or not title:
        return []
    out = []
    if t == CONF_RUMOR:
        hit = [k for k in ASSERTIVE_MARKERS if k in title]
        if hit:
            out.append(f"传闻类标题禁用肯定语气，命中断言词：{'、'.join(hit[:3])}")
        if not any(k in title for k in HEDGE_MARKERS):
            out.append("传闻类标题缺少模糊/信源标记（如 或/或将/据悉/传闻/曝）")
    elif t == CONF_SINGLE:
        if not any(k in title for k in HEDGE_MARKERS) and not SOURCE_TITLE_RE.match(title):
            out.append("单源级标题宜携带信源或模糊标记（计划 13.3：罗马诺：／天空：…）")
    return out


def body_violations(tier: str | None, content: str) -> list[str]:
    """正文措辞违规项（计划 3.2 三级措辞阈值）。"""
    t = normalize_tier(tier)
    text = (content or "")
    if not t or not text:
        return []
    out = []
    if t == CONF_RUMOR:
        if not any(k in text for k in ("传闻", "流言", "据悉", "有消息", "据报道", "消息人士", "曝")):
            out.append("传闻级正文缺少措辞（须写「传闻」「有消息称」「据悉」「据…报道」）")
    elif t == CONF_SINGLE:
        if not any(k in text for k in ("据", "消息人士", "据悉")):
            out.append("单源级正文缺少来源措辞（须写「据某媒体报道」）")
    return out


def check(tier, title: str = "", content: str = "", *, blocking_tiers=(CONF_RUMOR,)):
    """综合校验，返回 ``{tier, ok, hard, warn, hint}``。

    - ``hard``：必须重拟（默认仅传闻级；可经 ``blocking_tiers`` 扩展）。
    - ``warn``：建议改进，不阻断。
    - ``tier`` 为 None（分级未知）时 ``ok=True`` 且不做任何判定。
    """
    t = normalize_tier(tier)
    if not t:
        return {"tier": None, "ok": True, "hard": [], "warn": [],
                "hint": "", "judged": False}
    tv = title_violations(t, title)
    bv = body_violations(t, content)
    hard, warn = [], []
    for msg in tv + bv:
        (hard if t in blocking_tiers else warn).append(msg)
    hint = ""
    if hard:
        hint = ("该事实为「{}」级：标题不得用肯定语气，请改用「或将／据悉／传闻／曝」等"
                "模糊措辞并保留信源；正文须写明「传闻」「有消息称」或「据某媒体报道」。"
                .format(t))
    return {"tier": t, "ok": not hard, "hard": hard, "warn": warn,
            "hint": hint, "judged": True}


def check_topic(topic, title: str = "", content: str = "") -> dict:
    """从选题/事实结构里自动解析分级后校验（分级未知 → 不判定）。"""
    return check(tier_of(topic), title, content)


# ------------------------------------------------------------
# 表驱动自检（计划 4.1 / 3.2 的可执行副本）
# ------------------------------------------------------------
#: (tier, title, content) → 是否应判硬违规
RULE_CASES = (
    # 传闻级：断言式标题必须打回
    (CONF_RUMOR, "曼城官宣签下某某", "", True),
    (CONF_RUMOR, "某某确定加盟皇马", "", True),
    (CONF_RUMOR, "交易敲定，某某转会完成", "", True),
    (CONF_RUMOR, "某某加盟曼城", "", True),                 # 无任何模糊标记 → 打回
    # 传闻级：带模糊标记即放行
    (CONF_RUMOR, "某某或将加盟曼城，据悉报价已递交", "据悉，双方仍在谈。", False),
    (CONF_RUMOR, "传闻某某被曝接近转会", "有消息称双方已接触。", False),
    # 传闻级：标题合规但正文缺措辞 → 打回（措辞系统覆盖正文）
    (CONF_RUMOR, "某某或将加盟曼城", "双方已经谈妥，只待落笔。", True),
    # 单源级：正文须有来源措辞；断言本身不硬打回（不属传闻类标题）
    (CONF_SINGLE, "罗马诺：某某接近加盟曼城", "据罗马诺报道，双方谈判进入尾声。", False),
    (CONF_SINGLE, "某某加盟曼城", "双方已经谈妥。", False),
    # 已确认级：陈述句可用
    (CONF_CONFIRMED, "某某正式加盟曼城", "俱乐部已官宣。", False),
    (CONF_CONFIRMED, "某某完成转会", "官方公告已发布。", False),
    # 分级未知 → 不判定（绝不误杀）
    (None, "某某官宣加盟", "官方公告已发布。", False),
    ("", "某某官宣加盟", "", False),
)


def selftest() -> list[dict]:
    """跑表驱动用例，返回不符合计划 4.1 的项（空 = 全部符合）。"""
    bad = []
    for tier, title, content, want_hard in RULE_CASES:
        res = check(tier, title, content)
        got_hard = not res["ok"]
        if got_hard != want_hard:
            bad.append({"tier": tier, "title": title, "want_hard": want_hard,
                        "got_hard": got_hard, "hard": res["hard"]})
    return bad


def render_selftest(bad=None) -> str:
    bad = selftest() if bad is None else bad
    L = ["# 传闻分级措辞规则自检（计划 4.1）", "",
         f"- 用例数：{len(RULE_CASES)}　不通过：{len(bad)}",
         f"- 结论：**{'通过 ✅' if not bad else '未通过 ❌'}**", ""]
    if bad:
        for b in bad:
            L.append(f"- ❌ tier={b['tier']!r} title={b['title']!r}："
                     f"期望硬违规={b['want_hard']}，实得={b['got_hard']}（{b['hard']}）")
    else:
        L.append("传闻级标题禁肯定语气／传闻级正文须带措辞／单源级须写来源／"
                 "已确认级可陈述／分级未知不判定 —— 全部符合。")
    return "\n".join(L)


def _cli() -> int:
    ap = argparse.ArgumentParser(description="传闻分级措辞与传闻标题硬校验（计划 4.1）")
    ap.add_argument("--selftest", action="store_true", help="跑表驱动规则自检")
    ap.add_argument("--tier", help="分级：已确认／单源／传闻（或 confirmed/single/rumor）")
    ap.add_argument("--title", default="", help="待校验标题")
    ap.add_argument("--content", default="", help="待校验正文")
    args = ap.parse_args()

    if args.selftest:
        bad = selftest()
        print(render_selftest(bad))
        return 0 if not bad else 1

    res = check(args.tier, args.title, args.content)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
