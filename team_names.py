# -*- coding: utf-8 -*-
"""队名清洗：把「科莫血洗」「狂胜朗斯」这类赛果串还原成球队名。

## 为什么需要这个模块（2026-11-05 熔断事故）

早间批次在 15:13 触发第十章自动熔断，提示
「[朗斯主场1-9被科莫血洗…] 缺少客队名: 血洗」，同批打回 5 次后全批停发。

链路是三层叠加：

1. **采集层切错了**：战报标题「朗斯主场1-9被科莫血洗」按「比分前=主队、
   比分后=客队、去掉已知前缀」切成 ``home=朗斯 / away=科莫血洗``。这是对的，
   但切除词表只认「绝杀/逆转/大胜/横扫…」，不认「血洗」，于是把动词留在了队名里。
2. **校验层不认这个脏串**：纯属「是否像队名」的黑白名单判定，
   `_TEAM_DIRTY_TOKENS` 同样漏了「血洗」→ 判为合法队名 → 硬要求正文出现「血洗」。
   正文当然不会把「血洗」当队名写，于是必然打回。
3. **熔断层按次数计**：同一篇重试 + 降级重生成共 5 次 = 阈值，一篇稿子把关卡
   从「这一篇不过」放大成「全批停发」。

## 为什么不用「继续补词表」

赛果动词是一个开放集合（血洗/屠杀/吊打/零封/双杀/掀翻/狂胜/溃败/翻盘…），
历史上每补一次只挡住下一次复发的那个词：10-13 补了「首发/替补/阵容」，
11-05 又被「血洗」打穿。所以这里改**结构性清洗**：

* 队名两端的赛果动词一律剥掉，剥到不再变化为止；
* 剥完不足 2 字 ⇒ 整个字符串就是动词（如「血洗」），视为脏数据，返回空串；
* 词表只维护**这一份**，采集层（media_scraper）与校验层（orchestrator）共用。

这样「科莫血洗 → 科莫」「狂胜朗斯 → 朗斯」「惨败科莫 → 科莫」都能救回来，
而不是简单地把这一场的事实校验整个跳过——**能救则救，救不回来才跳过**，
否则就是拿「少校验一场」换「不出红」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTITY_MAP_PATH = ROOT / "config" / "entity_map.json"

#: 赛果 / 动作动词。按长度降序排列，保证「大胜」先于「胜」被匹配。
#: 只收**两字及以上**的复合词：单字「胜/负/平」在真队名里也可能出现
#: （「胜利船」「全北现代」不含，但「圣保利」「平」等边界不值得冒险）。
RESULT_VERBS: tuple[str, ...] = tuple(sorted({
    # 大比分/悬殊
    "血洗", "屠杀", "狂胜", "大胜", "完胜", "大捷", "吊打", "暴打", "击溃",
    "横扫", "零封", "双杀", "掀翻", "打崩", "崩盘", "溃败", "惨败", "惨案",
    # 小比分/过程
    "小胜", "险胜", "绝杀", "绝平", "逆转", "翻盘", "逆袭", "反超", "扳平",
    "逼平", "战平", "闷平", "惜败", "落败", "告负", "不敌", "输给", "负于",
    "击败", "战胜", "取胜", "赢下", "拿下", "力克", "击退", "斩杀", "复仇",
    # 赛制结果
    "淘汰出局", "淘汰", "出局", "晋级", "出线", "降级", "夺冠", "登顶",
    # 其它常见粘连
    "爆冷", "大冷", "再爆冷", "提前", "锁定", "开局", "首战", "次回合",
}, key=len, reverse=True))

#: 队名前的介词/被动标记（「被科莫血洗」→「科莫血洗」→「科莫」）
NOISE_PREFIXES: tuple[str, ...] = ("被", "遭", "让", "由", "被罚", "遭遇")

#: 首尾允许保留的字符（中文、拉丁字母、数字）。其余（标点/空格/竖线）一律去掉。
_RE_EDGE = re.compile(r"^[^0-9A-Za-z\u4e00-\u9fff]+|[^0-9A-Za-z\u4e00-\u9fff]+$")


def clean_team_name(name: str) -> str:
    """剥离队名两端的赛果动词与介词，返回最小干净队名；救不回来返回空串。

    >>> clean_team_name("科莫血洗")
    '科莫'
    >>> clean_team_name("狂胜朗斯")
    '朗斯'
    >>> clean_team_name("血洗")
    ''
    >>> clean_team_name("拜仁慕尼黑")
    '拜仁慕尼黑'
    """
    if not isinstance(name, str):
        return ""
    s = _RE_EDGE.sub("", name.strip())
    # 循环剥离：可能同时有前缀介词和尾部动词（「被科莫血洗」）
    for _ in range(4):
        before = s
        for p in NOISE_PREFIXES:
            if s.startswith(p) and len(s) - len(p) >= 2:
                s = s[len(p):]
                break
        for v in RESULT_VERBS:
            if s.endswith(v) and len(s) - len(v) >= 2:
                s = s[: -len(v)]
                break
            if s.startswith(v) and len(s) - len(v) >= 2:
                s = s[len(v):]
                break
        if s == before:
            break
    s = s.strip()
    # 剥到只剩动词本身（「血洗」「逆转」）→ 这不是队名，返回空让调用方跳过校验，
    # 而不是把这个动词当成队名去要求正文出现它。
    if s in RESULT_VERBS:
        return ""
    return s


def is_result_only(name: str) -> bool:
    """字符串整体就是一个赛果动词（如「血洗」「逆转」）——它不是队名。"""
    if not isinstance(name, str):
        return False
    s = _RE_EDGE.sub("", name.strip())
    return bool(s) and s in RESULT_VERBS


def looks_like_team(name: str) -> bool:
    """清洗后是否像一个干净队名（长度 2~30 且不是纯赛果动词）。

    注意：这里**允许**「科莫血洗」判为 True——因为它清洗后是「科莫」，
    是一个真队名，只是来源粘连了动词。判定「是不是队名」与「要不要原样使用」
    是两件事：前者交给本函数，后者必须用 :func:`clean_team_name` 的结果。
    """
    s = clean_team_name(name)
    return bool(s) and 2 <= len(s) <= 30


# ---------------------------------------------------------------- 跨语言归一
#
# 2026-11-06 事故：同一场 Lazio vs Napoli 在晨读与午间各写了一篇
# （《领跑，那不勒斯这一关…》/《登顶遇上那不勒斯…》），两篇 keywords 重叠 4/5。
# 去重没拦住，是因为判据两侧语言不同：
#   * 已发侧 `covered["keywords"]` 是 **英文**（serie a / lazio / napoli）；
#   * 候选侧 `home_zh/away_zh` 是 **中文**（拉齐奥 / 那不勒斯）。
# `"napoli" in "那不勒斯"` 恒为 False，去重形同不存在。
# 另外 `covered["teams"]` 用的是 constants.WIKI_TEAMS —— 只有 15 支队，
# 那不勒斯/拉齐奥都不在里面，这一路也完全空转。
#
# 所以归一的单位不是「字符串」而是「实体」：把中/英/别名/俱乐部简名
# 全部映射到 entity_map 的规范名（name_zh），比对在实体层面做。
#
# ⚠️ 简名（Milan、Madrid）可能是**多支队共享**的：Inter Milan 与 AC Milan
# 都有 "Milan"。这类多义简名一律**不映射**（宁可漏判一次重复，也不能把
# 国米的稿算成 AC 米兰的），与 data_chart.normalize_team_zh「唯一才归一」同一哲学。

_CACHE: dict = {"by_form": None, "by_canon": None, "by_token": None}


def _load_team_tables() -> tuple[dict[str, str], dict[str, set[str]], dict[str, set[str]]]:
    """建两张表 + 一张英文单词表：写法→规范名、规范名→全部写法、英文整词→规范名。

    ⚠️ 英文整词表**只在归属唯一时成立**。实测：``AC Milan`` 的 name_en 含单词
    ``Milan``，而 ``FC Internazionale Milano`` 含 ``Milano``（不是 Milan），
    所以 ``Milan`` 唯一归属 AC 米兰。这条规则本身没错，但**绝不能拿它去拆
    "Inter Milan" 这种整串**——那样会把国米认成 AC 米兰（第一版就是这么错的）。
    整串只在「等于某个 name_en 的前缀/后缀短语」时才算，见 ``canonical_team``。
    """
    if _CACHE["by_form"] is not None:
        return _CACHE["by_form"], _CACHE["by_canon"], _CACHE["by_token"]

    by_form: dict[str, str] = {}
    by_canon: dict[str, set[str]] = {}
    word_owners: dict[str, set[str]] = {}
    en_pairs: list[tuple[str, str]] = []
    try:
        ents = json.loads(ENTITY_MAP_PATH.read_text(encoding="utf-8")).get("entities") or {}
    except Exception:
        ents = {}

    for item in ents.values() if isinstance(ents, dict) else []:
        if not isinstance(item, dict) or item.get("category") != "club":
            continue
        canon = str(item.get("name_zh") or "").strip()
        if not canon:
            continue
        forms: set[str] = {canon.lower()}
        for a in (item.get("aliases") or []):
            if isinstance(a, str) and a.strip() and len(a.strip()) >= 2:
                forms.add(a.strip().lower())
        en = str(item.get("name_en") or "").strip()
        if en:
            forms.add(en.lower())
            en_pairs.append((en.lower(), canon))
            for w in re.findall(r"[A-Za-zÀ-ÿ]{4,}", en):
                word_owners.setdefault(w.lower(), set()).add(canon)

        by_canon[canon] = forms
        for f in forms:
            if len(f) >= 2:
                by_form.setdefault(f, canon)

    word_unique = {w: next(iter(o)) for w, o in word_owners.items() if len(o) == 1}
    _CACHE.update({"by_form": by_form, "by_canon": by_canon,
                   "by_token": word_unique, "en_pairs": en_pairs})
    return by_form, by_canon, word_unique


def canonical_team(name: str) -> str | None:
    """任意写法（中文/英文全名/别名/唯一英文词）→ 规范中文名；认不准返回 None。

    三级判据，逐级收紧：

    1. **整串命中登记写法** → 直接返回（``那不勒斯`` / ``SSC Napoli`` / ``蓝鹰``）；
    2. **整串是某个 name_en 的前缀或后缀短语** → 唯一时返回
       （``Napoli`` ⊂ ``SSC Napoli``、``Real Madrid`` ⊂ ``Real Madrid CF``）；
    3. **整串等于某个 name_en 里的英文整词** → 该词归属唯一才返回
       （``Napoli`` / ``Lens`` / ``Internazionale``）。

    绝不拆多词串兜底：``Inter Milan`` 拆开只剩 ``Milan`` 会指向 AC 米兰，
    而那其实是国米的口语混写。认不准就 None，与 ``normalize_team_zh`` 同一哲学。

    >>> canonical_team("那不勒斯")
    '那不勒斯'
    >>> canonical_team("Napoli")
    '那不勒斯'
    >>> canonical_team("Inter Milan") is None   # 拆词必串味，不猜
    True
    """
    if not isinstance(name, str):
        return None
    q = " ".join(name.strip().lower().split())
    if not q:
        return None
    by_form, _by_canon, by_token = _load_team_tables()
    if q in by_form:
        return by_form[q]

    en_pairs = _CACHE.get("en_pairs") or []
    # 2. 前缀 / 后缀短语（要求按空格切开的整词边界）
    hits = {canon for en, canon in en_pairs
            if en.startswith(q + " ") or en.endswith(" " + q)}
    if len(hits) == 1:
        return hits.pop()
    # 3. 单个英文整词
    if " " not in q and q in by_token:
        return by_token[q]
    return None


def alias_forms(name: str) -> set[str]:
    """规范名 → 该队全部登记写法（小写）。用于「已发关键词里有没有这支队」。

    除中文名/别名/英文全名外，还要带上**唯一归属的英文单词**：
    已发 keywords 里写的是 ``napoli`` 而不是 ``ssc napoli``，只给全名就比对不上
    （2026-11-06 重复发同一场的直接原因之一）。
    """
    canon = canonical_team(name) or (name.strip() if isinstance(name, str) else "")
    if not canon:
        return set()
    _by_form, by_canon, by_token = _load_team_tables()
    forms = set(by_canon.get(canon) or {canon.lower()})
    # 反向补英文单词：只在「该单词归属唯一」时补，多义词（milan）不补。
    forms |= {w for w, owner in by_token.items() if owner == canon}
    return forms


def mentions_team(text: str, team: str) -> bool:
    """text 里是否提到 team（跨语言、别名感知）。

    只做「任一写法出现」，不做最长匹配消解 —— 需要消解串味时用
    ``data_chart.entity_hits``（那是唯一实现）；本函数用于去重的粗判，
    宁可多判一次「提到过」，也不要漏掉「英文写的已发记录」。
    """
    low = (text or "").lower()
    if not low or not isinstance(team, str):
        return False
    return any(f and f in low for f in alias_forms(team))


def teams_in_tokens(tokens) -> set[str]:
    """从一组关键词（中英混排）里认出**规范队名**集合。

    「Inter Milan」与「AC Milan」这类共享简名由 ``by_token`` 唯一性规则挡掉，
    所以本函数认不出它们 —— 这是有意的：宁可少认，不认错。
    """
    out: set[str] = set()
    for t in tokens or []:
        if not isinstance(t, str) or not t.strip():
            continue
        c = canonical_team(t)
        if c:
            out.add(c)
    return out


def same_fixture(a, b) -> bool:
    """两组队名是否指向同一场比赛（无序对比较）。

    >>> same_fixture(("拉齐奥", "那不勒斯"), ("Napoli", "Lazio"))
    True
    """
    ca = {canonical_team(x) or str(x) for x in a if x}
    cb = {canonical_team(x) or str(x) for x in b if x}
    return len(ca) >= 2 and len(cb) >= 2 and ca == cb
