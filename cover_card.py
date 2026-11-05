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
import os
import re
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CARD_DIR = PROJECT_ROOT / "output" / "_cards"

W, H = 1200, 675            # 1200×675 ≥ 下载端 800×600 闸门
MARGIN = 72

VARIANTS = ("title", "points", "numbers")

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


def _chrome(draw, topic: dict, f_brand, f_tag):
    """顶部品牌条 + 栏目胶囊 + 头部细分隔线（三种版式共用）。"""
    draw.rectangle([0, 0, W, 10], fill=(214, 48, 49))
    draw.text((MARGIN, 44), "球评人老六", font=f_brand, fill=(255, 255, 255))
    column = str(topic.get("column") or "").strip()
    if column:
        tw = draw.textlength(column, font=f_tag)
        x0 = W - MARGIN - tw - 34
        draw.rounded_rectangle([x0, 40, W - MARGIN, 82], radius=21,
                               outline=(90, 102, 122), width=2)
        draw.text((x0 + 17, 52), column, font=f_tag, fill=(200, 208, 220))
    draw.line([(MARGIN, 118), (W - MARGIN, 118)], fill=(52, 63, 82), width=1)
    return column


def _footer(draw, topic: dict, f_foot):
    source = str(topic.get("source") or "数据来自公开赛事信息").strip()
    draw.line([(MARGIN, H - 108), (W - MARGIN, H - 108)], fill=(60, 72, 92), width=1)
    draw.text((MARGIN, H - 84), source, font=f_foot, fill=(150, 158, 172))
    draw.text((W - MARGIN - 96, H - 84), "老六说球", font=f_foot, fill=(120, 128, 142))


def _body_title(draw, title: str, f_title, f_num):
    """标题卡（原版式）：左红竖条 + 最多 3 行大标题 + 关键数字标签行。"""
    lines = _wrap(draw, title, f_title, W - 2 * MARGIN - 28, 3)
    line_h = int(f_title.size * 1.44)
    block_h = line_h * len(lines)
    y_title = 168 + max(0, (250 - block_h) // 2)
    draw.rectangle([MARGIN - 22, y_title + 8, MARGIN - 12, y_title + block_h - 20],
                   fill=(214, 48, 49))
    y = y_title
    for ln in lines:
        draw.text((MARGIN, y), ln, font=f_title, fill=(242, 244, 248))
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
                                   radius=26, fill=(46, 55, 72),
                                   outline=(255, 196, 61), width=2)
            draw.text((x + 18, y_chip + 8), n, font=f_num, fill=(255, 196, 61))
            x += tw + 36 + 18


def _body_numbers(draw, title: str, number: str, origin: str, f_num_big, f_title_s):
    """数字卡：一个数字撑满上半屏，标题在下半屏。

    为什么值得单独一张：正文里最能抓住眼睛的是数字，而数字卡把「这篇在说什么」
    压缩成一眼可读的一个值。标题卡已经出现过的地方，这张提供的是不同的信息形态。
    """
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
    draw.text((x, y), number, font=f, fill=(255, 196, 61))
    if origin:
        f_small = _font(22)
        draw.text((x + tw + 12, y + size - 34), origin, font=f_small, fill=(150, 158, 172))
    # 数字下的一条红线做视觉分隔
    cy = y + int(size * 1.35)
    draw.line([(MARGIN + 60, cy), (W - MARGIN - 60, cy)], fill=(214, 48, 49), width=4)

    lines = _wrap(draw, title, f_title_s, W - 2 * MARGIN, 2)
    ty = cy + 34
    for ln in lines:
        lw = draw.textlength(ln, font=f_title_s)
        draw.text(((W - lw) / 2, ty), ln, font=f_title_s, fill=(232, 236, 244))
        ty += int(f_title_s.size * 1.4)


def _body_points(draw, points, f_head, f_body):
    """要点卡：三条「小标题 + 一句摘要」，左侧编号 + 红竖条。"""
    y = 158
    row_h = 136
    for i, (head, line) in enumerate(points):
        # 编号：大号描边数字做装饰，不抢内容
        draw.text((MARGIN, y - 6), f"0{i + 1}", font=f_head, fill=(46, 55, 72))
        draw.rectangle([MARGIN + 74, y + 4, MARGIN + 80, y + 96], fill=(214, 48, 49))
        draw.text((MARGIN + 104, y), head, font=f_head, fill=(242, 244, 248))
        wrapped = _wrap(draw, line, f_body, W - MARGIN - (MARGIN + 104), 2)
        ly = y + int(f_head.size * 1.5)
        for ln in wrapped:
            draw.text((MARGIN + 104, ly), ln, font=f_body, fill=(168, 178, 194))
            ly += int(f_body.size * 1.42)
        y += row_h


def render(topic: dict, out_dir: Path | str | None = None,
           filename: str | None = None, variant: str = "title") -> str | None:
    """生成一张信息图，返回文件路径；失败返回 None（不抛）。

    variant:
      - ``title``   标题卡（封面用，默认）
      - ``points``  要点卡（需要 ``topic["content"]`` 里有 `## ` 分节，且 ≥2 节）
      - ``numbers`` 数字卡（需要标题或正文里有数字）

    filename: 指定文件名。发布链路必须传 `article-N-img-001.jpg` 这种规范名，
    因为 publisher.extract_images 只认 `images/article-\\d+-img-\\d+\\.jpg`。
    不传则按「版式 + 标题 hash」命名（debug 用）。
    """
    global LAST_ERROR
    LAST_ERROR = ""
    topic = topic or {}
    title = str(topic.get("title") or "").strip()
    if not title:
        LAST_ERROR = "标题为空"
        return None
    variant = variant if variant in VARIANTS else "title"

    content = str(topic.get("content") or "")
    points = _extract_points(content, limit=3) if variant == "points" else []
    number, origin = _big_number(title, content) if variant == "numbers" else ("", "")
    if variant == "points" and len(points) < 2:
        LAST_ERROR = f"正文可用小标题不足（{len(points)} 节），要点卡跳过"
        return None
    if variant == "numbers" and not number:
        LAST_ERROR = "标题与正文都没有可用数字，数字卡跳过"
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
            f_num = None
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

    # 背景：深海军蓝渐变 + 极淡的球场纹理（放右下角，绝不压内容）
    img = _gradient((W, H), (12, 17, 25), (27, 36, 50))
    draw = ImageDraw.Draw(img)
    line_color = (30, 39, 54)
    draw.ellipse([W - 330, -60, W + 210, 480], outline=line_color, width=3)
    draw.line([(W - 520, H), (W + 40, -20)], fill=line_color, width=3)

    _chrome(draw, topic, f_brand, f_tag)
    if variant == "points":
        _body_points(draw, points, f_head, f_body)
    elif variant == "numbers":
        _body_numbers(draw, title, number, origin, f_num_big, f_title_s)
    else:
        _body_title(draw, title, f_title, f_num)
    _footer(draw, topic, f_foot)

    out = Path(out_dir) if out_dir else CARD_DIR
    out.mkdir(parents=True, exist_ok=True)
    if filename:
        name = Path(filename).name
    else:
        # 版式进 hash：同一标题的三种卡不能互相覆盖
        seed = f"{variant}|{title}"
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
VARIANT_LABEL = {"title": "数据卡", "points": "要点卡", "numbers": "数字卡"}


def fill(art: dict, *, out_dir, prefix: str, start_index: int, need: int,
         used_hashes: set | None = None, skip_variants=()) -> list[dict]:
    """生成最多 ``need`` 张**不同版式**的信息图，补齐「每篇 3 张」的下限。

    参数:
      art: 文章对象（用 title / content / column_name / content_type）
      prefix: 发布端命名前缀，通常是 ``article-N-img``
      start_index: 起始序号（1 起），避免覆盖已下载的真实图
      need: 还缺几张
      used_hashes: 已用 md5 集合，生成后写回（去重靠它）
      skip_variants: 已经用过的版式（同一篇不重复出同一种卡）

    返回与 `image_service.download_image` 同构的 dict 列表（额外的 ``source`` 标明版式）。
    任何失败都只跳过该版式，不抛。
    """
    used_hashes = used_hashes if used_hashes is not None else set()
    need = max(0, int(need))
    if need <= 0:
        return []

    topic = {"title": art.get("title", ""),
             "content": art.get("content", ""),
             "column": art.get("column_name") or art.get("content_type", "")}
    got: list[dict] = []
    # 顺序有讲究：points 提供「结构」、numbers 提供「焦点」、title 是封面本体。
    # 已有 title 卡（封面兜底）时它会被 skip_variants 跳过，不会出现两张一样的。
    for variant in ("points", "numbers", "title"):
        if len(got) >= need:
            break
        if variant in skip_variants:
            continue
        path = render(topic, out_dir=out_dir,
                      filename=f"{prefix}-{start_index + len(got):03d}.jpg",
                      variant=variant)
        if not path:
            continue
        p = Path(path)
        try:
            md5 = hashlib.md5(p.read_bytes()).hexdigest()
        except Exception:
            continue
        if md5 in used_hashes:
            continue
        used_hashes.add(md5)
        got.append({"local_path": str(p), "filename": p.name, "url": "", "md5": md5,
                    "width": W, "height": H,
                    "description": f"原创信息图·{VARIANT_LABEL[variant]}",
                    "source": f"card:{variant}"})
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
        cases.append(("skip_variants 生效",
                      len(fill(art, out_dir=d, prefix="article-2-img", start_index=1,
                               need=3, skip_variants=("title", "points"))) == 1))

        # 发布链路契约：文件名必须落在 publisher.extract_images 的匹配范围里，
        # 否则图生成了但台账/头条封面都看不到 → 等于没配图。
        p2 = render({"title": title}, out_dir=d, filename="article-1-img-001.jpg")
        cases.append(("可指定发布端命名", bool(p2) and re.fullmatch(
            r"article-\d+-img-\d+\.jpg", Path(p2).name) is not None))
        cases.append(("空标题返回 None", render({"title": ""}, out_dir=d) is None))
        cases.append(("要点不足时跳过（不硬凑）",
                      render({"title": title, "content": "一段话"}, out_dir=d,
                             variant="points") is None))
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
