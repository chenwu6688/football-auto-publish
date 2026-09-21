#!/usr/bin/env python3
"""文字动效的关键词抽取 —— 从一句口播里挑出「值得放大高亮」的词。

与 footage.extract_keywords_rule 的区别（**语义相反，不可复用**）：
    · footage 抽的是「搜 B-roll 素材的安全词」，会**剔除**球队名/球员名；
    · 本模块抽的是「画面上要突出显示的词」，**恰恰要**球队名/比分/情绪动词。

四类关键词（按优先级从高到低）：
    num      —— 比分 / 时间 / 场次（如「3比1」「第67分钟」）：最抢眼
    team     —— 球队名（复用 teams 词表，含本地覆盖表）
    emotion  —— 情绪/动作词（逆转、绝杀、破门…）：口播号最出戏的部分
    decision —— 判罚词（红牌、点球、VAR…）：次级强调
"""

import re

# 比分：3比1 / 3:1 / 3-1 / 3–1；场次：第67分钟 / 第6轮
_NUM_RE = re.compile(
    r"\d+\s*[:：比\-–]\s*\d+"                    # 3比1 / 3:1 / 3-1
    r"|第\s*\d+\s*(?:分钟|轮|场|节|半|秒|球)?"   # 第67分钟 / 第6轮（整词优先）
    r"|\d+\s*(?:分钟|轮|场|节|半|秒)"            # 88分钟
)

# 情绪/动作词（口播号最出戏的词；与 audio_mix 的音效触发词同源，保证「听到欢呼就看到红字」）
EMOTION_WORDS = [
    "梅开二度", "帽子戏法", "读秒绝杀", "世界波", "绝杀", "绝平", "逆转", "反超",
    "扳平", "破门", "爆冷", "翻车", "崩盘", "内讧", "互喷", "炸了", "下课",
    "官宣", "签约", "加盟", "夺冠", "登顶", "捧杯", "连胜", "连追", "血洗",
    "大胜", "惨败", "丢冠", "卫冕", "复仇", "首秀", "惊艳", "封神",
]

# 判罚词（次级强调，用黄色）
DECISION_WORDS = ["红牌", "黄牌", "点球", "VAR", "越位", "争议", "犯规", "误判", "手球"]


def extract_highlights(text, *, teams_table=None, extra_words=None, max_kw=3):
    """从一句话里抽取要高亮的关键词。

    Args:
        text: 单句文本（口播稿切句后的一句）。
        teams_table: 球队词表（dict 的 keys 或 list），如 teams.get_teams(cfg)。
        extra_words: 额外自定义关键词列表（用户配置追加）。
        max_kw: 每句最多高亮几个（默认 3，避免满屏花）。
    Returns:
        list[(kw, kind)]，按优先级与出现位置排序；kw 互不重叠。
        kind ∈ {"num", "team", "emotion", "decision"}
    """
    s = text or ""
    if not s.strip():
        return []

    # 收集候选：(start, end, word, kind)
    cands = []

    # ① 数字/比分
    for m in _NUM_RE.finditer(s):
        cands.append((m.start(), m.end(), m.group(0), "num"))

    # ② 球队名（长词优先，减少子串误命中）
    team_names = sorted(set(teams_table or []), key=len, reverse=True)
    for name in team_names:
        if not name:
            continue
        for m in re.finditer(re.escape(name), s):
            cands.append((m.start(), m.end(), name, "team"))

    # ③ 情绪词 + 自定义词
    emo = sorted(set(EMOTION_WORDS) | set(extra_words or []), key=len, reverse=True)
    for w in emo:
        if not w:
            continue
        for m in re.finditer(re.escape(w), s):
            cands.append((m.start(), m.end(), w, "emotion"))

    # ④ 判罚词
    for w in sorted(set(DECISION_WORDS), key=len, reverse=True):
        for m in re.finditer(re.escape(w), s):
            cands.append((m.start(), m.end(), w, "decision"))

    if not cands:
        return []

    # 优先级：num > team > emotion > decision；同级按「长词优先 → 靠前优先」
    prio = {"num": 0, "team": 1, "emotion": 2, "decision": 3}
    cands.sort(key=lambda c: (prio[c[3]], -(c[1] - c[0]), c[0]))

    # 去重 + 防区间重叠（已被选中的字符区间不再选）
    picked, occupied = [], []
    for st, en, w, kind in cands:
        if len(picked) >= max_kw:
            break
        if any(st < o_en and en > o_st for o_st, o_en in occupied):
            continue
        if any(w == p[0] for p in picked):
            continue
        picked.append((w, kind))
        occupied.append((st, en))

    # 最终按出现位置排序，便于 ASS 顺序拼接
    order = {w: s.find(w) for w, _ in picked}
    picked.sort(key=lambda p: order.get(p[0], 0))
    return picked


def split_by_highlights(text, highlights):
    """把一句文本按高亮词切成 [(片段, kind_or_None), ...]，供 ASS 内联样式拼接。

    kind 为 None 表示普通正文片段。高亮词若在文中多次出现，只切第一个（与
    extract_highlights 的首次命中一致）。
    """
    s = text or ""
    if not highlights:
        return [(s, None)] if s else []

    # 计算每个高亮词的首次区间
    spans = []
    cursor = 0
    for kw, kind in highlights:
        at = s.find(kw, cursor)
        if at < 0:
            at = s.find(kw)          # 回退全局查找
        if at < 0:
            continue
        spans.append((at, at + len(kw), kw, kind))
        cursor = at + len(kw)

    if not spans:
        return [(s, None)] if s else []

    spans.sort(key=lambda x: x[0])
    out, pos = [], 0
    for st, en, kw, kind in spans:
        if st > pos:
            out.append((s[pos:st], None))
        out.append((kw, kind))
        pos = en
    if pos < len(s):
        out.append((s[pos:], None))
    return [seg for seg in out if seg[0]]
