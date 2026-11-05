#!/usr/bin/env python3
"""本地原创信息图（数据卡）—— 「每篇至少 3 张配图」的最后一道保底。

## 为什么需要离线兜底

2026-11-02~11-04 已发布 6 篇里仍有 2 篇零配图（33%，上限 5%）。诊断下来不是实体识别
的问题——修好之后「梅西C罗数据大对比」已能抽出 ['C罗','梅西']——而是**图源全断**：

    维基百科        本机与 CI 出口均可能超时（本地实测连续超时 → 熔断）
    Unsplash        无 Key
    DuckDuckGo      出口 IP 常被挡
    footyrenders    已停用（只返 50×50 占位图）

也就是说：即使实体识别 100% 正确，网络一断就回到「0 图 → 无封面发布」，
而「无封面」这条路径的聚合 CTR 是 0.52%（有封面 2.07%）。

## 为什么是数据卡而不是随机图库

计划 2.2 明确禁用随机图库配图（图文不符比没图更伤）。数据卡是**原创信息图**：
只有标题、标题里的数字、一句来源标注，不存在版权与「图文不符」问题。
它也不是「好看」的图——它的作用是把「无封面」这条最差路径堵掉。

## 2026-11-05 事故：CI 上没有中文字体，卡片全是方框

noon 批次两篇软性稿零配图走了兜底，用户看到的封面是「黑底 + 一排 □□□□」。
根因不是绘制逻辑，而是 `_font()` 的退化路径：候选字体在 `ubuntu-latest` 上一个都不存在，
于是 `ImageFont.load_default()`（Pillow 内置位图字体）顶上来，它**没有汉字字形**，
`draw.text("老六")` 画出来就是一排 .notdef 方框——而代码认为「成功生成了」。

三条修法（缺一不可）：
  1. **不认位图字体当成功**：找不到能画中文的字体时抛 `NoCJKFont`，`render()` 返回 None，
     宁可回到「无封面」并留痕，也不发布一张方框图；
  2. **字体发现不写死路径**：候选表之外再用 `fc-match :lang=zh` 问系统，CI 装完
     `fonts-noto-cjk` 就能命中，将来换 runner 镜像也不用改代码；
  3. **CI 装字体**：`batch.yml` 安装 `fonts-noto-cjk`；单测里静态断言这行存在，
     否则「本地有、CI 没字体」这种差异会再次静默复发。

## 2026-11-05 追加：从「一张封面」到「每篇 3 张」

用户当天的原话是「光有文字，没有配图不行，单篇文章至少保证 3 张相关配图」。
新闻站爬取层（`news_images`）和 Commons 能解决大部分，但**任何网络图源都会在某天
断掉**，所以下限必须由本地能画的东西兜住。于是 `render()` 支持三种版式：

    variant="title"     标题卡（原版式，做封面）
    variant="points"    要点卡：正文 `## ` 小标题 + 该节首句，三条横排
    variant="numbers"   数字卡：标题里最关键的那个数字做 160px 大字

三种都由**这篇稿子自己**的内容派生，所以「相关」是结构性的，不是靠检索碰运气。
`fill()` 按「已有的不重复」补齐到指定张数；同一篇稿子不会出现两张同版式的卡。

## 纪律

- 只用**标题/正文里已有的**数字（`title_elements.extract(...).numbers` 的匹配文本由本模块
  重新扫一遍，因为那里只存类别名）；不重算、不推断；
- 字体缺失 / PIL 不可用 → 返回 None，调用方照旧走「不配图」，**绝不抛异常阻塞发布**；
- 生成失败不写半张图（先写临时文件再 rename）。

命令行：
    python3 cover_card.py --selftest
    python3 cover_card.py --fontstatus
    python3 cover_card.py --title "1平2负只拿0.33分，中超这根独苗还能撑到2028吗？"
    python3 cover_card.py --title "..." --variant points --content-file article.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CARD_DIR = PROJECT_ROOT / "output" / "_cards"

W, H = 1200, 675            # 1200×675 ≥ 下载端 800×600 闸门
MARGIN = 72

VARIANTS = ("title", "points", "numbers", "quote", "qa", "outline", "compare")

# 中文黑体优先；找不到就用 fc-match 问系统（CI 装 fonts-noto-cjk 后靠这条命中）。
# 注意：这里**不能**放 DejaVuSans 之类无汉字字形的字体当兜底——那正是 2026-11-05
# 方框事故的成因（文件存在、能打开、画不出汉字，还静默「成功」）。
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/arphic/ukai.ttc",
)

# 最近一次失败原因（调用方拿去写 degrade.note，避免「有结论没证据」）
LAST_ERROR = ""

# 最近一张卡「用了正文里的什么」（句子 / 数字组合）。
# 供 `fill()` 做内容级去重：没有它，要点卡的摘要句和金句卡会抽到同一句，
# 同一篇稿子的三张图里有两张在说一模一样的话——张数够了，读者还是觉得在凑数。
LAST_SUBJECT = ""

_FONT_PATH_CACHE: str | None = None


class NoCJKFont(RuntimeError):
    """系统里找不到能画汉字的字体——宁可不出图，也不出版方框。"""


# 标题里可上卡的数字（比分 / 名次 / 度量），只取已有文本，不重算。
# 顺序有讲究：「战绩组合」必须排在单度量之前，否则「1平2负」会被切成「2负」。
# 这与 title_elements._NUMBER_PATTERNS 把「战绩」独立成一类的做法一致。
_RE_NUM_TOKEN = re.compile(
    r"\d{1,2}\s*(?:胜|平|负)\s*\d{1,2}\s*(?:胜|平|负)?"
    r"|\d{1,2}\s*[-:比]\s*\d{1,2}"
    r"|\d+(?:\.\d+)?\s*(?:球|分|场|轮|人|次|天|年|岁|分钟|%|万|亿|倍)"
    r"|[一二三四五六七八九十]{1,3}\s*(?:球|分|场|轮|人|次|年)")


def _has_han_glyph(path: str) -> bool:
    """粗判字体是否真有汉字字形：比对「中」与「〇」的位图。

    DejaVu 这类西文字体两个字符都会渲染成同一个 .notdef 方框（位图完全相同），
    真正的中文字体两者不同。只用于 fc-match 的返回值，候选表里的字体默认可信。
    """
    try:
        from PIL import ImageFont
        f = ImageFont.truetype(path, 40)
        a = bytes(f.getmask("中"))
        b = bytes(f.getmask("〇"))
        return len(a) > 0 and a != b
    except Exception:
        return False


def _cjk_font_path() -> str:
    """返回一个**能画汉字**的字体文件路径；找不到返回空串。

    顺序：环境变量（`COVER_CARD_FONT`）→ 内置候选表 → `fc-match :lang=zh`。
    `fc-match` 是 2026-11-05 之后加的那条：CI 镜像里字体路径会变，写死路径必然
    再次「本地有、CI 没有」（这正是方框事故的形态）。
    """
    global _FONT_PATH_CACHE
    if _FONT_PATH_CACHE is not None:
        return _FONT_PATH_CACHE

    env = (os.environ.get("COVER_CARD_FONT") or "").strip()
    if env and Path(env).exists():
        _FONT_PATH_CACHE = env
        return _FONT_PATH_CACHE

    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            _FONT_PATH_CACHE = p
            return _FONT_PATH_CACHE

    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", ":lang=zh"],
                             capture_output=True, text=True, timeout=5)
        cand = (out.stdout or "").strip()
        if cand and Path(cand).exists() and _has_han_glyph(cand):
            _FONT_PATH_CACHE = cand
            return _FONT_PATH_CACHE
    except Exception:
        pass

    _FONT_PATH_CACHE = ""
    return ""


def _font(size: int):
    """取字体。找不到能画汉字的字体时抛 NoCJKFont（绝不退化成位图字体）。"""
    p = _cjk_font_path()
    if not p:
        raise NoCJKFont("系统里没有可画汉字的中文字体（候选路径与 fc-match 均未命中）")
    from PIL import ImageFont
    try:
        return ImageFont.truetype(p, size)
    except Exception as e:                      # 字体文件坏了也不能静默降级
        raise NoCJKFont(f"字体无法加载: {p} ({type(e).__name__})")


def font_status() -> tuple[bool, str]:
    """给验收脚本/单测用：当前环境能不能画中文卡。"""
    p = _cjk_font_path()
    if not p:
        return False, "未找到中文字体（CI 未装 fonts-noto-cjk？）"
    return True, p


def _wrap(draw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    """逐字折行（中文无空格），超出 max_lines 用省略号收尾。"""
    lines, cur = [], ""
    for ch in text:
        trial = cur + ch
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
            continue
        lines.append(cur)
        cur = ch
        if len(lines) >= max_lines:
            break
    if len(lines) >= max_lines:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1]
        lines[-1] = last + "…"
    elif cur:
        lines.append(cur)
    return lines[:max_lines]


# 可在其后断行的中文标点（顿号/逗号优先，句号次之）
_BREAK_AFTER = "，、；：,;:。"


def _wrap_cjk(draw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    """优先在标点处断行，放不下才硬切。

    为什么不用 `_wrap`：它逐字贪心，会把「……但射门次数依|然是全队第一」断在词中间，
    居中排版下第二行只剩几个字，读起来像被截断。中文排版的正解是**先找标点**，
    标点位置可接受时就在那里断。
    """
    text = (text or "").strip()
    if not text or max_lines <= 0:
        return []
    # 切成「短句 + 其后的标点」，标点跟着前一段走
    chunks = re.findall(r"[^，、；：,;:。]*[，、；：,;:。]?", text)
    chunks = [c for c in chunks if c]
    lines: list[str] = []
    cur = ""
    for ch in chunks:
        trial = cur + ch
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if len(lines) >= max_lines:
            break
        # 这一段自己就超宽 → 退回逐字硬切
        if draw.textlength(ch, font=font) > max_width:
            for piece in _wrap(draw, ch, font, max_width, max_lines - len(lines)):
                lines.append(piece)
            cur = ""
        else:
            cur = ch
    if cur and len(lines) < max_lines:
        lines.append(cur)
    lines = lines[:max_lines]
    # 最后一行仍超宽（硬切残留）→ 收个省略号，别画到卡片外
    if lines and draw.textlength(lines[-1], font=font) > max_width:
        lines[-1] = _wrap(draw, lines[-1], font, max_width, 1)[0]
    return lines


def _key_numbers(title: str, limit: int = 3) -> list[str]:
    out: list[str] = []
    for m in _RE_NUM_TOKEN.finditer(title or ""):
        t = re.sub(r"\s+", "", m.group(0))
        if t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


# ——————————————————————————————————————————————
# 正文派生：要点卡 / 数字卡的素材
# ——————————————————————————————————————————————

_MD_NOISE = re.compile(r"[*_`>#]+|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)")
_SENT_END = re.compile(r"[。！？!?；;]")


def _plain(line: str) -> str:
    """去掉 markdown 标记（图片/链接/加粗），留着话本身。"""
    return _MD_NOISE.sub(lambda m: m.group(1) or "", line or "").strip()


def _extract_points(content: str, limit: int = 3) -> list[tuple[str, str]]:
    """从正文抽「小标题 + 该节第一句」，用于要点卡。

    只认 `## ` 分节：这是本项目所有生成 prompt 约定的结构。没有分节就返回空，
    由 `fill()` 跳过 points 版式——**不拿正文开头两句硬凑**，那样三条要点会和三张
    一模一样的卡一样水。
    """
    if not content:
        return []
    out: list[tuple[str, str]] = []
    heads = re.split(r"(?m)^##\s+", content)
    for seg in heads[1:]:
        seg = seg.strip()
        if not seg:
            continue
        first_nl = seg.find("\n")
        head = _plain(seg[:first_nl] if first_nl > 0 else seg)
        body = seg[first_nl:].strip() if first_nl > 0 else ""
        # 取该节第一句（跳过空行与图片行）
        sentence = ""
        for line in body.split("\n"):
            line = line.strip()
            if not line or line.startswith("!["):
                continue
            sentence = _plain(line)
            break
        m = _SENT_END.search(sentence)
        if m:
            sentence = sentence[:m.end()]
        if head and sentence:
            out.append((head[:24], sentence[:56]))
        if len(out) >= limit:
            break
    return out


def _big_number(title: str, content: str = "") -> tuple[str, str]:
    """数字卡的主角：优先标题里的数字，其次正文里第一个数字 token。

    返回 ``(数字, 出处说明)``；都没有返回 ``("", "")``。
    """
    nums = _key_numbers(title, limit=1)
    if nums:
        return nums[0], ""
    m = _RE_NUM_TOKEN.search(content or "")
    if m:
        return re.sub(r"\s+", "", m.group(0)), "正文"
    return "", ""


# ——————————————————————————————————————————————
# 配色主题（2026-11-05 追加）
# ——————————————————————————————————————————————
#
# 为什么需要主题：只换版式不换配色，读者看到的三张图仍然是「同一种设计的三行字」。
# 主题的作用是让同一篇稿子的多张图在**视觉上分清**，同时给不同栏目留出可调空间。
# 每套主题都必须是完整的一套（背景 / 正文 / 弱化 / 强调 / 卡片底），不允许只覆盖一半——
# 浅色主题（paper）一旦漏了某个前景色，就会在白底上画白字，等于一张废图。

THEMES: dict[str, dict] = {
    # 默认：深海军蓝渐变（原配色，改之前的所有图都是它）
    "navy": {
        "bg_top": (12, 17, 25), "bg_bottom": (27, 36, 50),
        "ink": (242, 244, 248), "ink_dim": (168, 178, 194),
        "ink_faint": (150, 158, 172), "line": (52, 63, 82),
        "accent": (214, 48, 49), "chip": (46, 55, 72),
        "highlight": (255, 196, 61), "art": (30, 39, 54),
    },
    # 暗红：适合「冲突 / 更衣室 / 争议」类稿子，压迫感强
    "crimson": {
        "bg_top": (26, 11, 13), "bg_bottom": (62, 20, 24),
        "ink": (252, 240, 238), "ink_dim": (206, 168, 166),
        "ink_faint": (172, 138, 136), "line": (92, 44, 46),
        "accent": (236, 92, 74), "chip": (74, 30, 32),
        "highlight": (255, 214, 160), "art": (44, 18, 20),
    },
    # 米白纸感：适合「盘点 / 历史 / 人物」类稿子，阅读负担最低
    "paper": {
        "bg_top": (246, 243, 236), "bg_bottom": (232, 226, 214),
        "ink": (28, 30, 36), "ink_dim": (86, 92, 104),
        "ink_faint": (126, 130, 138), "line": (198, 192, 180),
        "accent": (176, 44, 40), "chip": (222, 216, 204),
        "highlight": (150, 34, 30), "art": (218, 212, 200),
    },
    # 墨绿：适合「战术 / 数据 / 复盘」类稿子，冷静
    "forest": {
        "bg_top": (9, 25, 21), "bg_bottom": (20, 50, 42),
        "ink": (238, 246, 242), "ink_dim": (166, 194, 182),
        "ink_faint": (140, 168, 156), "line": (44, 78, 66),
        "accent": (58, 196, 142), "chip": (26, 62, 52),
        "highlight": (255, 206, 96), "art": (22, 56, 46),
    },
}

THEME_KEYS = tuple(THEMES)


def theme(name: str | None) -> dict:
    """取一套主题；未知名字退回 navy（不抛，绘制层不该因配色名拼错而停工）。"""
    return THEMES.get(str(name or "navy"), THEMES["navy"])


# ——————————————————————————————————————————————
# 新版式素材（2026-11-05 追加：让「3 张」不再依赖「正文有没有 ## 分节」）
# ——————————————————————————————————————————————
#
# 为什么要加：`points` 要求正文 ≥2 个 `## ` 分节，`numbers` 要求标题或正文里有数字。
# 两条都不满足的稿子（实测存在）过去只能出 1 张标题卡，「每篇 3 张」直接落空。
# 新增的四种版式只用「句子本身」，不要求任何格式约定：
#
#     quote    金句卡：正文里信息密度最高的一句
#     qa       问答卡：标题提的问题 + 正文给的答案
#     outline  结构卡：按段落切（不依赖 `## `）
#     compare  对比卡：两个数字并排（有数字才出）

_RE_SENT_SPLIT = re.compile(r"(?<=[。！？!?])")
# 句子里出现这些词，说明它在讲「事儿」而不是在铺垫
_RE_SUBSTANCE = re.compile(
    r"但是|但|然而|其实|真正|问题|原因|关键|因为|所以|结果|反而|偏偏|始终|根本"
    r"|不是.{0,12}而是|不在.{0,12}而在|并不|没能|输了|赢了|丢|进|签|拒|换")

_TEAM_CACHE: dict = {"table": None}


def _team_table() -> dict[str, str]:
    """中文队名/别名 → 标准中文名。只读 entity_map.json，不 import 其它模块。

    为什么不复用 `data_chart.resolve_team`：那个模块反过来 import 本模块（复用
    字体与配色），函数内互导虽然能跑，但一旦有人在模块级加一行 `import` 就成环。
    这里只需要一张「字面量 → 标准名」的表，读 JSON 比引模块便宜也更稳。
    """
    if _TEAM_CACHE["table"] is not None:
        return _TEAM_CACHE["table"]
    table: dict[str, str] = {}
    try:
        data = json.loads((PROJECT_ROOT / "config" / "entity_map.json")
                          .read_text(encoding="utf-8")) or {}
        for item in (data.get("entities") or {}).values():
            if not isinstance(item, dict):
                continue
            canon = str(item.get("name_zh") or "").strip()
            if not canon:
                continue
            table[canon] = canon
            for a in item.get("aliases") or []:
                a = str(a or "").strip()
                if len(a) >= 2:
                    table.setdefault(a, canon)
    except Exception:
        table = {}
    _TEAM_CACHE["table"] = table
    return table


def _team_near(text: str, pos: int, before: bool) -> str:
    """取 pos 之前/之后**最近的**那个队名；找不到返回空串。

    用于给对比卡的两个数字配左右标签——「2 | 1」不说是谁对谁，读者只能猜。
    """
    table = _team_table()
    if not table:
        return ""
    if before:
        best, best_at = "", -1
        for name in table:
            at = text.rfind(name, 0, pos)
            if at >= 0 and at + len(name) > best_at:
                best, best_at = name, at + len(name)
        return table.get(best, "")
    best, best_at = "", len(text) + 1
    for name in table:
        at = text.find(name, pos)
        if 0 <= at < best_at:
            best, best_at = name, at
    return table.get(best, "")


# 带单位的数字：对比卡只允许「同一单位的两个值」并排，否则就是拿比分去比积分
_RE_UNIT_NUM = re.compile(
    r"(\d+(?:\.\d+)?)\s*(球|分|场|轮|人|次|天|年|岁|分钟|%|万|亿|倍)")
# 比分：对比卡可以把它拆成左右两个数（2 比 1 天然就是一次对比）
_RE_SCORE_PAIR = re.compile(r"(\d{1,2})\s*[-:比]\s*(\d{1,2})")


def _sentences(content: str) -> list[str]:
    """正文切句（去掉 markdown 噪声与图片行），保留原顺序。"""
    out: list[str] = []
    for raw in (content or "").split("\n"):
        line = raw.strip()
        if not line or line.startswith("![") or line.startswith("#"):
            continue
        line = _plain(line)
        for s in _RE_SENT_SPLIT.split(line):
            s = s.strip()
            # 太短的句子（「是的。」）当不了主角；太长的一句塞不进卡片
            if 12 <= len(s) <= 60:
                out.append(s)
    return out


def _extract_quote(content: str, skip: tuple[str, ...] = (),
                   allow_lead: bool = False) -> str:
    """金句卡的素材：信息密度最高的一句。

    打分只看「像不像一个判断」：有转折/因果词加分，有数字加分，带问号减分
    （问句是标题的活，金句卡要给答案）。已经用过的句子（``skip``）跳过，
    这样同一篇能连出多张不同句子的金句卡——极端稿凑够 3 张靠的就是这一步。

    ``allow_lead``：是否允许选中**正文第一句**（导语）。默认不允许——导语说的是
    「谁几比几赢了」，那正是数字卡已经画过的东西。只有正文短到只剩导语一句时，
    调用方才应该传 True，否则正文仅一句的稿子会一张金句卡都出不来。
    """
    sents = _sentences(content)
    lead = sents[0] if sents else ""
    best, best_score = "", 0
    for s in sents:
        if s in skip:
            continue
        if not allow_lead and s == lead and len(sents) > 1:
            continue
        score = 1 + 2 * len(_RE_SUBSTANCE.findall(s)) + (2 if _RE_NUM_TOKEN.search(s) else 0)
        if s.endswith(("？", "?")):
            score -= 1
        if score > best_score:
            best, best_score = s, score
    return best


def _extract_qa(title: str, content: str) -> tuple[str, str]:
    """问答卡素材：(标题问句, 正文里最像答案的一句)。

    只在标题确实是问句时出卡——标题不是问句却硬做「问答」，读者会觉得答非所问。
    """
    q = (title or "").strip()
    if not q.endswith(("？", "?")):
        return "", ""
    sents = _sentences(content)
    if not sents:
        return "", ""
    # 答案优先挑「不是问句、且带判断词」的那句
    for s in sorted(sents, key=lambda x: -len(_RE_SUBSTANCE.findall(x))):
        if not s.endswith(("？", "?")):
            return q, s
    return q, sents[0]


def _extract_outline(content: str, want: int = 3) -> list[str]:
    """结构卡素材：把正文切成几段，每段给一个短标签。

    两种切法，按正文实际情况选：

      * **有小标题**（`## 一、xxx`）→ 用小标题当标签。那是作者自己给这段起的
        名字，比从正文里截一句准得多。
      * **没有小标题** → 按**空行分段**，每段取首句前 22 字。

    第二支是这次（11-05）补上的：第一版只按小标题切，遇到「三个自然段、没有
    小标题」的稿子（这是实测最常见的一种）会把全文并成一段，只产出一个标签，
    结构卡直接出不来。
    """
    lines = (content or "").split("\n")
    heads = [(i, re.match(r"^#{2,4}\s+(.+)$", l.strip()))
             for i, l in enumerate(lines)]
    has_head = any(m for _, m in heads)

    if has_head:
        segs: list[tuple[str, str]] = []
        cur_head, cur_lines = "", []
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            m = re.match(r"^#{2,4}\s+(.+)$", line)
            if m:
                if cur_head or cur_lines:
                    segs.append((cur_head, " ".join(cur_lines)))
                cur_head, cur_lines = _plain(m.group(1))[:22], []
            elif line.startswith("!["):
                continue
            else:
                cur_lines.append(_plain(line))
        if cur_head or cur_lines:
            segs.append((cur_head, " ".join(cur_lines)))
        out = []
        for head, body in segs:
            if not head:
                m2 = _RE_SENT_SPLIT.split(body.strip())
                head = (m2[0] if m2 else body)[:22]
            if head:
                out.append(head)
            if len(out) >= want:
                break
        return out

    # 无小标题：空行分段，每段取首句
    out = []
    for block in re.split(r"\n\s*\n", content or ""):
        block = "\n".join(l for l in block.split("\n")
                          if l.strip() and not l.strip().startswith("!["))
        block = _plain(block.replace("\n", " ")).strip()
        if not block:
            continue
        first = _RE_SENT_SPLIT.split(block)[0].strip()
        if len(first) < 4:
            continue
        out.append(first[:22])
        if len(out) >= want:
            break
    return out


def _extract_compare(title: str, content: str = "") -> tuple[str, str, str, str, str]:
    """对比卡素材：(值A, 值B, 说明, 左标签, 右标签)。

    这里有一条硬规则：**只有语义可比的量才能并排**。第一版直接取标题里前两个
    数字 token，结果画出了「2-1 vs 27分」——比分和积分并排放在一起，读者只会
    觉得这张图在胡说。所以现在只认两种合法对比：

      1. 同单位的两个值（两个「N分」、两个「N轮」…），左右标签都是单位；
      2. 一个比分（`2-1`），拆成左右两半，左右标签取比分两侧最近的**队名**。

    第 2 种的标签是后补的：只画「2 | 1」而不说是谁对谁，读者根本不知道在看什么。

    两种都没有就返回空，让调用方跳过这张卡——宁可少一张，也不出一张误导图。
    数字一律取自**已有文本**，不重算、不推断。
    """
    text = f"{title} {content}"

    # ① 同单位：按单位分组，取第一组里出现的两个值
    buckets: dict[str, list[tuple[str, int]]] = {}
    for m in _RE_UNIT_NUM.finditer(text):
        val, unit = m.group(1), m.group(2)
        buckets.setdefault(unit, [])
        raw = f"{val}{unit}"
        if raw not in [v for v, _ in buckets[unit]]:
            buckets[unit].append((raw, m.start()))
    for unit, vals in buckets.items():
        if len(vals) < 2:
            continue
        (a, pa), (b, pb) = vals[0], vals[1]
        # 标签优先用「这个数字前面最近的队名」（巴萨34分 / 皇马27分），
        # 找不到队名才退回单位——只写「分 / 分」等于没标。
        tag_a = _team_near(text, pa, before=True) or unit
        tag_b = _team_near(text, pb, before=True) or unit
        return a, b, f"{unit}数对比", tag_a, tag_b

    # ② 比分：拆成左右两半（这是同一场的两个对手，天然可比）
    m = _RE_SCORE_PAIR.search(text)
    if m:
        left = _team_near(text, m.start(), before=True)
        right = _team_near(text, m.end(), before=False)
        return m.group(1), m.group(2), "比分", left, right

    return "", "", "", "", ""


# ——————————————————————————————————————————————
# 画布
# ——————————————————————————————————————————————

def _gradient(size: tuple[int, int], top: tuple[int, int, int],
              bottom: tuple[int, int, int]):
    """竖渐变底。逐行画，1200×675 只需 675 次，比引 numpy 轻。"""
    from PIL import Image
    w, h = size
    img = Image.new("RGB", (1, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((w, h))


def _chrome(draw, topic: dict, f_brand, f_tag, t: dict | None = None):
    """顶部品牌条 + 栏目胶囊 + 头部细分隔线（所有版式共用）。"""
    t = t or THEMES["navy"]
    draw.rectangle([0, 0, W, 10], fill=t["accent"])
    draw.text((MARGIN, 44), "球评人老六", font=f_brand, fill=t["ink"])
    column = str(topic.get("column") or "").strip()
    if column:
        tw = draw.textlength(column, font=f_tag)
        x0 = W - MARGIN - tw - 34
        draw.rounded_rectangle([x0, 40, W - MARGIN, 82], radius=21,
                               outline=t["line"], width=2)
        draw.text((x0 + 17, 52), column, font=f_tag, fill=t["ink_dim"])
    draw.line([(MARGIN, 118), (W - MARGIN, 118)], fill=t["line"], width=1)
    return column


def _footer(draw, topic: dict, f_foot, t: dict | None = None):
    t = t or THEMES["navy"]
    source = str(topic.get("source") or "数据来自公开赛事信息").strip()
    draw.line([(MARGIN, H - 108), (W - MARGIN, H - 108)], fill=t["line"], width=1)
    draw.text((MARGIN, H - 84), source, font=f_foot, fill=t["ink_faint"])
    draw.text((W - MARGIN - 96, H - 84), "老六说球", font=f_foot, fill=t["ink_faint"])


def _body_title(draw, title: str, f_title, f_num, t: dict | None = None):
    """标题卡（原版式）：左竖条 + 最多 3 行大标题 + 关键数字标签行。"""
    t = t or THEMES["navy"]
    lines = _wrap(draw, title, f_title, W - 2 * MARGIN - 28, 3)
    line_h = int(f_title.size * 1.44)
    block_h = line_h * len(lines)
    y_title = 168 + max(0, (250 - block_h) // 2)
    draw.rectangle([MARGIN - 22, y_title + 8, MARGIN - 12, y_title + block_h - 20],
                   fill=t["accent"])
    y = y_title
    for ln in lines:
        draw.text((MARGIN, y), ln, font=f_title, fill=t["ink"])
        y += line_h

    nums = _key_numbers(title)
    if nums:
        x = MARGIN
        y_chip = H - 196
        for n in nums:
            tw = draw.textlength(n, font=f_num)
            if x + tw + 36 > W - MARGIN:
                break
            draw.rounded_rectangle([x, y_chip, x + tw + 36, y_chip + 52],
                                   radius=26, fill=t["chip"],
                                   outline=t["highlight"], width=2)
            draw.text((x + 18, y_chip + 8), n, font=f_num, fill=t["highlight"])
            x += tw + 36 + 18


def _body_numbers(draw, title: str, number: str, origin: str, f_num_big, f_title_s,
                  t: dict | None = None):
    """数字卡：一个数字撑满上半屏，标题在下半屏。

    为什么值得单独一张：正文里最能抓住眼睛的是数字，而数字卡把「这篇在说什么」
    压缩成一眼可读的一个值。标题卡已经出现过的地方，这张提供的是不同的信息形态。
    """
    t = t or THEMES["navy"]
    # 数字居中，字号自适应：数字越长越小，保证不溢出
    size = f_num_big.size
    while size > 60:
        f = _font(size)
        if draw.textlength(number, font=f) <= W - 2 * MARGIN - 40:
            break
        size -= 8
    f = _font(size)
    tw = draw.textlength(number, font=f)
    x = (W - tw) / 2
    y = 176
    draw.text((x, y), number, font=f, fill=t["highlight"])
    if origin:
        f_small = _font(22)
        draw.text((x + tw + 12, y + size - 34), origin, font=f_small, fill=t["ink_faint"])
    # 数字下的一条线做视觉分隔
    cy = y + int(size * 1.35)
    draw.line([(MARGIN + 60, cy), (W - MARGIN - 60, cy)], fill=t["accent"], width=4)

    lines = _wrap(draw, title, f_title_s, W - 2 * MARGIN, 2)
    ty = cy + 34
    for ln in lines:
        lw = draw.textlength(ln, font=f_title_s)
        draw.text(((W - lw) / 2, ty), ln, font=f_title_s, fill=t["ink"])
        ty += int(f_title_s.size * 1.4)


def _body_points(draw, points, f_head, f_body, t: dict | None = None):
    """要点卡：三条「小标题 + 一句摘要」，左侧编号 + 竖条。"""
    t = t or THEMES["navy"]
    y = 158
    row_h = 136
    for i, (head, line) in enumerate(points):
        # 编号：大号装饰，不抢内容
        draw.text((MARGIN, y - 6), f"0{i + 1}", font=f_head, fill=t["chip"])
        draw.rectangle([MARGIN + 74, y + 4, MARGIN + 80, y + 96], fill=t["accent"])
        draw.text((MARGIN + 104, y), head, font=f_head, fill=t["ink"])
        wrapped = _wrap(draw, line, f_body, W - MARGIN - (MARGIN + 104), 2)
        ly = y + int(f_head.size * 1.5)
        for ln in wrapped:
            draw.text((MARGIN + 104, ly), ln, font=f_body, fill=t["ink_dim"])
            ly += int(f_body.size * 1.42)
        y += row_h


# ——————————————————————————————————————————————
# 新版式（2026-11-05）：不依赖 `## ` 分节，也不强求有数字
# ——————————————————————————————————————————————

def _body_quote(draw, sentence: str, f_quote, f_small, t: dict | None = None):
    """金句卡：一个巨大的引号 + 居中的一句话。

    版式上刻意与标题卡区分：标题卡左对齐、带竖条；金句卡居中、带引号装饰。
    读者扫过去就知道「这是文章里的一句话」，不是又一张标题。
    """
    t = t or THEMES["navy"]
    f_mark = _font(190)
    draw.text((MARGIN - 8, 96), "“", font=f_mark, fill=t["art"])
    lines = _wrap_cjk(draw, sentence, f_quote, W - 2 * MARGIN - 40, 4)
    line_h = int(f_quote.size * 1.6)
    block = line_h * len(lines)
    y = 168 + max(0, (300 - block) // 2)
    for ln in lines:
        lw = draw.textlength(ln, font=f_quote)
        draw.text(((W - lw) / 2, y), ln, font=f_quote, fill=t["ink"])
        y += line_h
    # 句尾的来源标注：这句话出自本文正文，不是外采引文，必须说清楚
    label = "——本文正文"
    lw = draw.textlength(label, font=f_small)
    draw.text(((W - lw) / 2, y + 18), label, font=f_small, fill=t["ink_faint"])


def _body_qa(draw, question: str, answer: str, f_q, f_a, f_lab, t: dict | None = None):
    """问答卡：上半屏是标题提的问题，下半屏是正文给的那句答案。

    两张卡的位置关系本身就是信息：读者一眼看到「问」和「答」的落差。
    答案区在剩余空间里**垂直居中**——只有一行答案时贴着分隔线会很空，余下大片
    留白看着像图没画完。
    """
    t = t or THEMES["navy"]
    # 问题区
    draw.text((MARGIN, 132), "问", font=f_lab, fill=t["accent"])
    q_lines = _wrap_cjk(draw, question, f_q, W - 2 * MARGIN - 60, 3)
    qy = 186
    for ln in q_lines:
        draw.text((MARGIN, qy), ln, font=f_q, fill=t["ink"])
        qy += int(f_q.size * 1.42)

    # 分隔线
    sep = min(qy + 22, H - 260)
    draw.line([(MARGIN, sep), (W - MARGIN, sep)], fill=t["line"], width=2)

    # 答案区：「答」标签 + 答案文字**作为一个整体**在剩余空间里居中。
    # 只让文字居中会让标签孤零零贴在分隔线上、中间空出一大段，像图没画完。
    a_lines = _wrap_cjk(draw, answer, f_a, W - 2 * MARGIN - 60, 3)
    a_line_h = int(f_a.size * 1.48)
    lab_h = int(f_lab.size * 1.35)
    gap = 18
    block = lab_h + gap + a_line_h * len(a_lines)
    zone_top, zone_bottom = sep + 18, H - 136
    start = zone_top + max(0, (zone_bottom - zone_top - block) // 2)
    draw.text((MARGIN, start), "答", font=f_lab, fill=t["highlight"])
    ay = start + lab_h + gap
    for ln in a_lines:
        if ay > H - 124:
            break
        draw.text((MARGIN, ay), ln, font=f_a, fill=t["ink_dim"])
        ay += a_line_h


def _body_outline(draw, heads: list[str], f_head, f_small, t: dict | None = None):
    """结构卡：把文章的几个自然段浓缩成一条竖排目录。

    适用于「没有 `## ` 小标题、但有好几段」的稿子——这是实测里最常见的一种。
    """
    t = t or THEMES["navy"]
    y = 160
    step = min(132, int((H - 320) / max(1, len(heads))))
    draw.line([(MARGIN + 26, y + 20), (MARGIN + 26, y + step * (len(heads) - 1) + 20)],
              fill=t["line"], width=2)
    for i, head in enumerate(heads):
        cy = y + step * i
        draw.ellipse([MARGIN + 16, cy + 10, MARGIN + 36, cy + 30], fill=t["accent"])
        draw.text((MARGIN + 58, cy - 2), f"{i + 1:02d}", font=f_small, fill=t["ink_faint"])
        draw.text((MARGIN + 112, cy), head, font=f_head, fill=t["ink"])


def _body_compare(draw, a: str, b: str, note: str, f_big, f_lab,
                  tag_a: str = "", tag_b: str = "", t: dict | None = None):
    """对比卡：两个数字左右并排，中间一条竖线，各自下方挂标签。

    标签不能省。只画「2 | 1」而不说是哪两支球队，读者得回头翻标题才知道在看什么。
    """
    t = t or THEMES["navy"]
    mid = W // 2
    draw.line([(mid, 180), (mid, 470)], fill=t["line"], width=2)

    size = f_big.size
    while size > 70:
        f = _font(size)
        if max(draw.textlength(a, font=f), draw.textlength(b, font=f)) <= mid - MARGIN - 40:
            break
        size -= 8
    f = _font(size)
    pairs = ((a, (MARGIN + mid - 40) / 2, t["highlight"], tag_a),
             (b, (mid + 40 + W - MARGIN) / 2, t["ink"], tag_b))
    top = 214
    for val, cx, color, tag in pairs:
        tw = draw.textlength(val, font=f)
        draw.text((cx - tw / 2, top), val, font=f, fill=color)
        if not tag:
            continue
        # 标签位置用**字形实际包围盒**算，不用「字号 × 系数」估算——
        # 估算法在「34分」这种带下沉笔画的串上会把标签压到数字上（第一版就是这样）。
        bbox = draw.textbbox((0, 0), val, font=f)
        tag_y = top + bbox[3] + 14
        tgw = draw.textlength(tag, font=f_lab)
        draw.text((cx - tgw / 2, tag_y), tag, font=f_lab, fill=t["ink_dim"])

    lab = note or "对比"
    lw = draw.textlength(lab, font=f_lab)
    draw.text(((W - lw) / 2, 520), lab, font=f_lab, fill=t["ink_faint"])


def render(topic: dict, out_dir: Path | str | None = None,
           filename: str | None = None, variant: str = "title",
           theme_name: str | None = None, nth: int = 0,
           avoid: tuple[str, ...] = ()) -> str | None:
    """生成一张信息图，返回文件路径；失败返回 None（不抛）。

    variant:
      - ``title``   标题卡（封面用，默认）
      - ``points``  要点卡（需要 ``topic["content"]`` 里有 `## ` 分节，且 ≥2 节）
      - ``numbers`` 数字卡（需要标题或正文里有数字）
      - ``quote``   金句卡（需要正文里有一句 12~60 字的判断句）
      - ``qa``      问答卡（需要标题是问句，且正文有答案句）
      - ``outline`` 结构卡（需要正文 ≥2 段，不要求 `## `）
      - ``compare`` 对比卡（需要 ≥2 个数字）

    theme_name: ``navy`` / ``crimson`` / ``paper`` / ``forest``，未知退回 navy。
    nth: 同一版式的第几张（0 起）。``quote`` 靠它换句子、``outline`` 靠它换起点——
    没有这个参数，一篇只有一段话的稿子永远只能出一张金句卡，「3 张」就补不满。

    filename: 指定文件名。发布链路必须传 `article-N-img-001.jpg` 这种规范名，
    因为 publisher.extract_images 只认 `images/article-\\d+-img-\\d+\\.jpg`。
    不传则按「版式 + 主题 + 第几张 + 标题 hash」命名（debug 用）。
    """
    global LAST_ERROR, LAST_SUBJECT
    LAST_ERROR = ""
    LAST_SUBJECT = ""
    topic = topic or {}
    title = str(topic.get("title") or "").strip()
    if not title:
        LAST_ERROR = "标题为空"
        return None
    variant = variant if variant in VARIANTS else "title"
    t = theme(theme_name)
    nth = max(0, int(nth or 0))

    content = str(topic.get("content") or "")

    # —— 素材抽取：每种版式只认自己需要的那一样，抽不到就跳过 ——
    points: list[tuple[str, str]] = []
    number = origin = ""
    quote = answer = question = ""
    heads: list[str] = []
    pair: tuple[str, str, str, str, str] = ("", "", "", "", "")
    if variant == "points":
        points = _extract_points(content, limit=3)
        if len(points) < 2:
            LAST_ERROR = f"正文可用小标题不足（{len(points)} 节），要点卡跳过"
            return None
    elif variant == "numbers":
        number, origin = _big_number(title, content)
        if not number:
            LAST_ERROR = "标题与正文都没有可用数字，数字卡跳过"
            return None
    elif variant == "quote":
        # 让 nth 生效：先按 nth 次迭代收集前面已选中的句子，全部跳过
        skipped: list[str] = [s for s in avoid if s]
        for _ in range(nth):
            s = _extract_quote(content, skip=tuple(skipped))
            if not s:
                break
            skipped.append(s)
        quote = _extract_quote(content, skip=tuple(skipped))
        if not quote:
            LAST_ERROR = ("正文里没有可用的判断句，金句卡跳过" if not skipped
                          else f"正文可用的判断句只有 {len(skipped)} 句，第 {nth + 1} 张金句卡跳过")
            return None
    elif variant == "qa":
        question, answer = _extract_qa(title, content)
        if not question or not answer:
            LAST_ERROR = ("标题不是问句，问答卡跳过" if not question
                          else "正文没有可用于作答的句子，问答卡跳过")
            return None
    elif variant == "outline":
        heads = _extract_outline(content, want=6)
        if len(heads) < 2:
            LAST_ERROR = f"正文段落不足（{len(heads)} 段），结构卡跳过"
            return None
        # nth 大于 0 时换一组（最多切到只剩 2 项）
        if nth:
            heads = heads[min(nth, max(0, len(heads) - 2)):]
            if len(heads) < 2:
                LAST_ERROR = f"结构卡第 {nth + 1} 组不足 2 段，跳过"
                return None
    elif variant == "compare":
        pair = _extract_compare(title, content)
        if not pair[0] or not pair[1]:
            LAST_ERROR = "可用数字不足两个，对比卡跳过"
            return None

    try:
        from PIL import Image, ImageDraw  # noqa: F401
    except Exception:
        LAST_ERROR = "PIL 不可用"
        return None

    try:
        f_brand, f_tag, f_foot = _font(30), _font(24), _font(24)
        if variant == "points":
            f_title = f_head = _font(42)
            f_body = _font(30)
            f_num = f_num_big = f_title_s = None
        elif variant == "quote":
            f_title = f_num = f_head = f_body = None
            f_quote = _font(54 if len(quote) <= 34 else 46)
            f_small = _font(24)
            f_num_big = f_title_s = None
        elif variant == "qa":
            f_title = f_num = f_head = f_body = None
            f_q = _font(46 if len(question) <= 30 else 40)
            f_a = _font(36)
            f_lab = _font(30)
            f_num_big = f_title_s = None
        elif variant == "outline":
            f_title = f_num = f_body = None
            f_head = _font(40)
            f_small = _font(26)
            f_num_big = f_title_s = None
        elif variant == "compare":
            f_title = f_num = f_head = f_body = None
            f_big = _font(150)
            f_lab = _font(30)
            f_num_big = f_title_s = None
        else:
            f_title = _font(58 if len(title) <= 30 else 50)
            f_num = _font(32)
            f_head = f_body = None
            f_num_big = _font(168) if variant == "numbers" else None
            f_title_s = _font(38) if variant == "numbers" else None
    except NoCJKFont as e:
        LAST_ERROR = str(e)
        print(f"   ⚠️ 信息图跳过（不出版方框图）：{e}")
        return None
    except Exception as e:
        LAST_ERROR = f"{type(e).__name__}: {str(e)[:80]}"
        return None

    # 背景：主题渐变 + 极淡的球场纹理（放右下角，绝不压内容）
    img = _gradient((W, H), t["bg_top"], t["bg_bottom"])
    draw = ImageDraw.Draw(img)
    line_color = t["art"]
    draw.ellipse([W - 330, -60, W + 210, 480], outline=line_color, width=3)
    draw.line([(W - 520, H), (W + 40, -20)], fill=line_color, width=3)

    _chrome(draw, topic, f_brand, f_tag, t)
    if variant == "points":
        _body_points(draw, points, f_head, f_body, t)
    elif variant == "numbers":
        _body_numbers(draw, title, number, origin, f_num_big, f_title_s, t)
    elif variant == "quote":
        _body_quote(draw, quote, f_quote, f_small, t)
    elif variant == "qa":
        _body_qa(draw, question, answer, f_q, f_a, f_lab, t)
    elif variant == "outline":
        _body_outline(draw, heads, f_head, f_small, t)
    elif variant == "compare":
        _body_compare(draw, pair[0], pair[1], pair[2], f_big, f_lab,
                      pair[3], pair[4], t)
    else:
        _body_title(draw, title, f_title, f_num, t)
    _footer(draw, topic, f_foot, t)

    # 记下这张卡「用了正文里的什么」，fill() 靠它避开重复内容
    LAST_SUBJECT = {
        "points": "".join(s for _, s in points),
        "numbers": number,
        "quote": quote,
        "qa": answer,
        "outline": "".join(heads),
        "compare": f"{pair[0]}|{pair[1]}",
        "title": "",
    }.get(variant, "")

    out = Path(out_dir) if out_dir else CARD_DIR
    out.mkdir(parents=True, exist_ok=True)
    if filename:
        name = Path(filename).name
    else:
        # 版式 + 主题 + 序号进 hash：同一标题的多张卡不能互相覆盖
        seed = f"{variant}|{theme_name or 'navy'}|{nth}|{title}"
        name = "card-" + hashlib.md5(seed.encode("utf-8")).hexdigest()[:12] + ".jpg"
    dest = out / name
    if dest.exists():
        return str(dest)
    # 临时文件放在目标目录旁边：/tmp 在部分环境是 10MB tmpfs，写不进大图
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        img.save(tmp, format="JPEG", quality=90)
        tmp.replace(dest)
    except Exception as e:
        LAST_ERROR = f"保存失败: {type(e).__name__}: {str(e)[:80]}"
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    return str(dest)


# 版式 → 卡片说明（写进正文 alt 与 metadata，读者看得见「这是信息图」）
VARIANT_LABEL = {
    "title": "标题卡", "points": "要点卡", "numbers": "数字卡",
    "quote": "金句卡", "qa": "问答卡", "outline": "结构卡", "compare": "对比卡",
}

# 候选顺序 = 信息量从高到低。前六项是「内容形式真的不同」，后面是「同版式换配色」。
#
# 为什么排到后面还有牌：实测存在「正文只有一段话、标题没有数字」的稿子，那时
# points / numbers / outline / compare / qa 全部抽不到素材，只剩 title 一张。
# 这时的正确答案不是「少配两张」，而是把标题卡做成不同配色的若干张——观感上仍是
# 三张不同的图，而「图不够」是会被用户直接看见的硬伤。
_FILL_PLAN: tuple[tuple[str, str, int], ...] = (
    ("points", "navy", 0),
    ("numbers", "navy", 0),
    ("quote", "navy", 0),
    ("outline", "navy", 0),
    ("qa", "crimson", 0),
    ("compare", "forest", 0),
    ("quote", "crimson", 1),
    ("quote", "forest", 2),
    ("outline", "paper", 1),
    ("title", "crimson", 0),
    ("title", "paper", 0),
    ("title", "forest", 0),
    ("quote", "paper", 3),
    ("quote", "navy", 4),
)


def fill(art: dict, *, out_dir, prefix: str, start_index: int, need: int,
         used_hashes: set | None = None, skip_variants=(),
         used_cards: set | None = None) -> list[dict]:
    """生成最多 ``need`` 张**互不相同**的信息图，补齐「每篇 3 张」的下限。

    参数:
      art: 文章对象（用 title / content / column_name / content_type）
      prefix: 发布端命名前缀，通常是 ``article-N-img``
      start_index: 起始序号（1 起），避免覆盖已下载的真实图
      need: 还缺几张
      used_hashes: 已用 md5 集合，生成后写回（去重靠它）
      skip_variants: 已经用过的**版式**，整类跳过（调用方明确"这篇不要要点卡"时用）
      used_cards: 已经用过的 ``(版式, 配色)`` 组合，只跳过这几个具体组合。

    为什么这两个要分开：封面兜底那张是 `title + navy`，如果把它当成
    「已经用过 title 版式」整类跳过，那么正文只有一句话的稿子就只剩 quote 一条路，
    只能补到 2 张——「每篇 3 张」当场落空。实际该跳过的只是 `title+navy` 这一个
    组合，`title+crimson` / `title+paper` 仍然可用。

    返回与 `image_service.download_image` 同构的 dict 列表（额外的 ``source`` 标明版式、
    ``theme`` 标明配色）。

    唯一硬承诺：**只要字体可用且标题不为空，返回条数就等于 need**。做不到时返回的
    条数会少于 need，调用方必须据此报警——「配图不足」是硬条件，不允许静默降级。
    """
    used_hashes = used_hashes if used_hashes is not None else set()
    used_cards = set(used_cards or ())
    need = max(0, int(need))
    if need <= 0:
        return []

    topic = {"title": art.get("title", ""),
             "content": art.get("content", ""),
             "column": art.get("column_name") or art.get("content_type", "")}

    content = str(topic.get("content") or "")
    got: list[dict] = []
    # 已经用过的正文句子：要点卡的摘要句一旦上过卡，金句卡就不能再抽同一句。
    # 不做这一步，同一篇的三张图里常常有两张在说同一句话（张数够了但像凑数）。
    avoid: list[str] = [s for _, s in _extract_points(content, limit=3)]
    for variant, theme_name, nth in _FILL_PLAN:
        if len(got) >= need:
            break
        if variant in skip_variants:
            continue
        if (variant, theme_name) in used_cards:
            continue
        # 金句卡要求「还有没被用过的句子」。要点卡已经把这篇稿子讲得最好的
        # 三句用掉时，剩下的往往只有导语——那正是数字卡已经说过的比数。
        # 这时宁可换 outline / 换配色的 title，也不出一张重复内容的金句卡。
        if variant == "quote":
            # 还剩几句没被用过的正文句子。导语是否可用由 `_extract_quote` 自己判断
            # （它知道「全文只有一句」这种例外），这里只负责数够不够。
            spare = [x for x in _sentences(content) if x not in avoid]
            if len(spare) <= nth:
                continue
        path = render(topic, out_dir=out_dir,
                      filename=f"{prefix}-{start_index + len(got):03d}.jpg",
                      variant=variant, theme_name=theme_name, nth=nth,
                      avoid=tuple(avoid))
        if not path:
            continue
        if LAST_SUBJECT:
            avoid.append(LAST_SUBJECT)
        p = Path(path)
        try:
            md5 = hashlib.md5(p.read_bytes()).hexdigest()
        except Exception:
            continue
        if md5 in used_hashes:
            # 同内容同配色 → 删掉这张重复的，别在 images 目录留孤儿文件
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
            continue
        used_hashes.add(md5)
        got.append({"local_path": str(p), "filename": p.name, "url": "", "md5": md5,
                    "width": W, "height": H,
                    "description": f"原创信息图·{VARIANT_LABEL.get(variant, variant)}",
                    "source": f"card:{variant}",
                    "theme": theme_name})
    return got


def selftest() -> list[tuple[str, bool]]:
    cases: list[tuple[str, bool]] = []
    title = "1平2负只拿0.33分，中超这根独苗还能撑到2028吗？"
    cases.append(("数字抽取只取已有文本", _key_numbers(title)[:2] == ["1平2负", "0.33分"]))
    cases.append(("无数字时返回空列表", _key_numbers("这场球看得人心里堵得慌") == []))

    content = ("## 一、三连败不是偶然\n球队在这三场里丢了七个球，后防线的问题早就暴露。\n"
               "## 二、外援不是万能药\n锋线三个人加起来只有两球入账。\n"
               "## 三、还能撑多久\n留给这支球队的时间只剩两轮。\n")
    pts = _extract_points(content)
    cases.append(("要点卡抽取三条", len(pts) == 3 and pts[0][0].startswith("一、三连败")))
    cases.append(("无分节时抽不到要点", _extract_points("就一段话，没有小标题。") == []))

    ok_font, font_detail = font_status()
    cases.append((f"有可画汉字的中文字体（{font_detail[-40:]}）", ok_font))

    with tempfile.TemporaryDirectory() as d:
        p = render({"title": title}, out_dir=d)
        ok = bool(p) and Path(p).exists()
        cases.append(("能生成数据卡", ok))
        if ok:
            from PIL import Image
            with Image.open(p) as im:
                cases.append(("尺寸过 800×600 闸门", im.width >= 800 and im.height >= 600))
                cases.append(("长宽比适合封面（16:9）", abs(im.width / im.height - 16 / 9) < 0.02))
            cases.append(("同标题复用同一张", render({"title": title}, out_dir=d) == p))

        # 三种版式都要能出，且互不相同（否则「3 张配图」就是同一张复制三遍）
        art = {"title": title, "content": content, "column_name": "中超"}
        cards = fill(art, out_dir=d, prefix="article-1-img", start_index=1, need=3)
        cases.append(("补齐三张信息图", len(cards) == 3))
        cases.append(("三张版式互不相同", len({c["md5"] for c in cards}) == 3))
        cases.append(("文件名符合发布端契约",
                      all(re.fullmatch(r"article-1-img-\d{3}\.jpg", c["filename"])
                          for c in cards)))
        _skipped = fill(art, out_dir=d, prefix="article-2-img", start_index=1,
                        need=3, skip_variants=("title", "points"))
        cases.append(("skip_variants 生效（被跳过的版式一个都不出现）",
                      bool(_skipped) and not ({"card:title", "card:points"}
                                              & {c["source"] for c in _skipped})))

        # 发布链路契约：文件名必须落在 publisher.extract_images 的匹配范围里，
        # 否则图生成了但台账/头条封面都看不到 → 等于没配图。
        p2 = render({"title": title}, out_dir=d, filename="article-1-img-001.jpg")
        cases.append(("可指定发布端命名", bool(p2) and re.fullmatch(
            r"article-\d+-img-\d+\.jpg", Path(p2).name) is not None))
        cases.append(("空标题返回 None", render({"title": ""}, out_dir=d) is None))
        cases.append(("要点不足时跳过（不硬凑）",
                      render({"title": title, "content": "一段话"}, out_dir=d,
                             variant="points") is None))

        # —— 2026-11-05 扩充版式后新增的自检 ——
        # 金句卡：抽一句判断句；没有正文就跳过
        q_art = {"title": "半场零封巴黎，里昂下半场到底哪一步走错了？", "column": "法甲",
                 "content": "里昂上半场把巴黎的边路彻底按住。\n\n"
                            "真正的问题不在体能，而在换人顺序。"}
        cases.append(("金句卡能出且有内容",
                      bool(render(q_art, out_dir=d, variant="quote"))))
        cases.append(("空正文时金句卡跳过（不拿标题充数）",
                      render({"title": title, "content": ""}, out_dir=d,
                             variant="quote") is None))
        # 问答卡：标题必须是问句
        cases.append(("问句标题能出问答卡",
                      bool(render(q_art, out_dir=d, variant="qa"))))
        cases.append(("陈述句标题不出问答卡（答非所问）",
                      render({"title": "皇马2-1瓦伦西亚", "content": "赢了。",
                              "column": "西甲"}, out_dir=d, variant="qa") is None))

        # 对比卡：只允许语义可比的量并排
        cases.append(("比分可拆成左右两半", _extract_compare("皇马2-1瓦伦西亚")[2] == "比分"))
        cases.append(("同单位两个值可对比",
                      _extract_compare("巴萨34分领跑，皇马27分紧追")[:2] == ("34分", "27分")))
        cases.append(("只有单个数字不出对比卡（无可比对象）",
                      _extract_compare("皇马27分还不够用")[0] == ""))
        cases.append(("比分卡的左右标签是队名",
                      _extract_compare("皇马2-1瓦伦西亚")[3:] == ("皇马", "瓦伦西亚")))

        # 结构卡：按段落切，不要求 `## `
        cases.append(("无小标题的多段正文能出结构卡",
                      len(_extract_outline("第一段讲防守。\n\n第二段讲中场。", want=6)) == 2))

        # 主题：未知名字必须安全退回默认，不能黑图
        cases.append(("未知主题退回 navy", theme("nonsense") is THEMES["navy"]))
        cases.append(("paper 主题是浅底深字",
                      THEMES["paper"]["bg_top"][0] > 200 and THEMES["paper"]["ink"][0] < 80))

        # 极端稿保底：任何人都会问「正文什么都没有还怎么出三张」
        _tiny = {"title": "这支球队的更衣室到底出了什么问题", "column": "中超",
                 "content": "其实问题不在球场上，而在训练场之外的那些事。"}
        _tiny_cards = fill(_tiny, out_dir=d, prefix="article-9-img", start_index=1, need=3)
        cases.append(("极短正文也能补到 3 张", len(_tiny_cards) == 3))
        # 极短正文时会出现两张标题卡（配色不同）——这是有意的保底行为。
        # 判定标准因此是「版式+配色」组合唯一，而不是版式唯一。
        cases.append(("极短正文的三张视觉互不重复",
                      len({(c["source"], c.get("theme")) for c in _tiny_cards}) == 3))
        _empty_cards = fill({"title": title, "content": "", "column": "中超"},
                            out_dir=d, prefix="article-8-img", start_index=1, need=3)
        cases.append(("空正文靠主题换色补到 3 张", len(_empty_cards) == 3))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="本地原创信息图（封面 / 要点 / 数字三种版式）")
    ap.add_argument("--title", help="按标题生成一张卡（写到 output/_cards/）")
    ap.add_argument("--variant", default="title", choices=VARIANTS)
    ap.add_argument("--content-file", help="正文 markdown（points 版式需要）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--fontstatus", action="store_true", help="打印中文字体可用性")
    args = ap.parse_args()
    if args.fontstatus:
        ok, detail = font_status()
        print(("✅ " if ok else "❌ ") + detail)
        return 0 if ok else 1
    if args.title:
        content = ""
        if args.content_file:
            content = Path(args.content_file).read_text(encoding="utf-8")
        p = render({"title": args.title, "content": content}, variant=args.variant)
        print(p or f"（生成失败：{LAST_ERROR}）")
        return 0 if p else 1
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检 {len(selftest())} 项，失败 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
