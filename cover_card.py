#!/usr/bin/env python3
"""封面兜底：本地生成「数据卡」图（P0-3，2026-11-04 有图率验收）。

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
     否则「本地有字体、CI 没字体」这种差异会再次静默复发。

## 纪律

- 只用**标题里已有的**数字（`title_elements.extract(...).numbers` 的匹配文本由本模块
  重新扫一遍，因为那里只存类别名）；
- 字体缺失 / PIL 不可用 → 返回 None，调用方照旧走「不配图」，**绝不抛异常阻塞发布**；
- 生成失败不写半张图（先写临时文件再 rename）。

命令行：
    python3 cover_card.py --selftest
    python3 cover_card.py --fontstatus
    python3 cover_card.py --title "1平2负只拿0.33分，中超这根独苗还能撑到2028吗？"
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


def render(topic: dict, out_dir: Path | str | None = None,
           filename: str | None = None) -> str | None:
    """生成一张数据卡，返回文件路径；失败返回 None（不抛）。

    filename: 指定文件名。发布链路必须传 `article-N-img-001.jpg` 这种规范名，
    因为 publisher.extract_images 只认 `images/article-\\d+-img-\\d+\\.jpg`。
    不传则按标题 hash 命名（debug 用）。
    """
    global LAST_ERROR
    LAST_ERROR = ""
    topic = topic or {}
    title = str(topic.get("title") or "").strip()
    if not title:
        LAST_ERROR = "标题为空"
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        LAST_ERROR = "PIL 不可用"
        return None

    try:
        f_brand = _font(30)
        f_tag = _font(24)
        f_num = _font(32)
        f_title = _font(58 if len(title) <= 30 else 50)
        f_foot = _font(24)
    except NoCJKFont as e:
        LAST_ERROR = str(e)
        print(f"   ⚠️ 数据卡跳过（不出版方框图）：{e}")
        return None
    except Exception as e:
        LAST_ERROR = f"{type(e).__name__}: {str(e)[:80]}"
        return None

    nums = _key_numbers(title)
    column = str(topic.get("column") or "").strip()
    source = str(topic.get("source") or "数据来自公开赛事信息").strip()

    # 背景：深海军蓝渐变 + 极淡的球场纹理（放右下角，绝不压标题）
    img = _gradient((W, H), (12, 17, 25), (27, 36, 50))
    draw = ImageDraw.Draw(img)
    line_color = (30, 39, 54)
    draw.ellipse([W - 330, -60, W + 210, 480], outline=line_color, width=3)
    draw.line([(W - 520, H), (W + 40, -20)], fill=line_color, width=3)

    # 顶部品牌条 + 头部细分隔线
    draw.rectangle([0, 0, W, 10], fill=(214, 48, 49))
    draw.text((MARGIN, 44), "球评人老六", font=f_brand, fill=(255, 255, 255))
    if column:                                   # 右上角栏目胶囊
        tw = draw.textlength(column, font=f_tag)
        x0 = W - MARGIN - tw - 34
        draw.rounded_rectangle([x0, 40, W - MARGIN, 82], radius=21,
                               outline=(90, 102, 122), width=2)
        draw.text((x0 + 17, 52), column, font=f_tag, fill=(200, 208, 220))
    draw.line([(MARGIN, 118), (W - MARGIN, 118)], fill=(52, 63, 82), width=1)

    # 标题（最多 3 行）：左红竖条 + 大字号，是整张卡的主体
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

    # 关键数字做「数据标签」横排（不放大到抢标题的位置，避免和标题里同样的字重复抢眼）
    if nums:
        x = MARGIN
        y_chip = H - 196
        for n in nums:
            tw = draw.textlength(n, font=f_num)
            draw.rounded_rectangle([x, y_chip, x + tw + 36, y_chip + 52],
                                   radius=26, fill=(46, 55, 72),
                                   outline=(255, 196, 61), width=2)
            draw.text((x + 18, y_chip + 8), n, font=f_num, fill=(255, 196, 61))
            x += tw + 36 + 18
            if x > W - MARGIN:
                break

    # 底部：细分隔线 + 来源标注
    draw.line([(MARGIN, H - 108), (W - MARGIN, H - 108)], fill=(60, 72, 92), width=1)
    draw.text((MARGIN, H - 84), source, font=f_foot, fill=(150, 158, 172))
    draw.text((W - MARGIN - 96, H - 84), "老六说球", font=f_foot, fill=(120, 128, 142))

    out = Path(out_dir) if out_dir else CARD_DIR
    out.mkdir(parents=True, exist_ok=True)
    if filename:
        name = Path(filename).name
    else:
        name = "card-" + hashlib.md5(title.encode("utf-8")).hexdigest()[:12] + ".jpg"
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


def selftest() -> list[tuple[str, bool]]:
    cases: list[tuple[str, bool]] = []
    title = "1平2负只拿0.33分，中超这根独苗还能撑到2028吗？"
    cases.append(("数字抽取只取已有文本", _key_numbers(title)[:2] == ["1平2负", "0.33分"]))
    cases.append(("无数字时返回空列表", _key_numbers("这场球看得人心里堵得慌") == []))

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
        # 发布链路契约：文件名必须落在 publisher.extract_images 的匹配范围里，
        # 否则图生成了但台账/头条封面都看不到 → 等于没配图。
        p2 = render({"title": title}, out_dir=d, filename="article-1-img-001.jpg")
        cases.append(("可指定发布端命名", bool(p2) and re.fullmatch(
            r"article-\d+-img-\d+\.jpg", Path(p2).name) is not None))
        cases.append(("空标题返回 None", render({"title": ""}, out_dir=d) is None))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="封面兜底数据卡（原创图，不引版权素材）")
    ap.add_argument("--title", help="按标题生成一张卡（写到 output/_cards/）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--fontstatus", action="store_true", help="打印中文字体可用性")
    args = ap.parse_args()
    if args.fontstatus:
        ok, detail = font_status()
        print(("✅ " if ok else "❌ ") + detail)
        return 0 if ok else 1
    if args.title:
        p = render({"title": args.title})
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
