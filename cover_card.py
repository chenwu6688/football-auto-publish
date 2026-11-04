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

## 纪律

- 只用**标题里已有的**数字（`title_elements.extract(...).numbers` 的匹配文本由本模块
  重新扫一遍，因为那里只存类别名）；
- 字体缺失 / PIL 不可用 → 返回 None，调用方照旧走「不配图」，**绝不抛异常阻塞发布**；
- 生成失败不写半张图（先写临时文件再 rename）。

命令行：
    python3 cover_card.py --selftest
    python3 cover_card.py --title "1平2负只拿0.33分，中超这根独苗还能撑到2028吗？"
"""

from __future__ import annotations

import argparse
import hashlib
import re
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CARD_DIR = PROJECT_ROOT / "output" / "_cards"

W, H = 1200, 675            # 1200×675 ≥ 下载端 800×600 闸门
MARGIN = 72

# 中文黑体优先；找不到就退回 DejaVu（只影响观感，不影响可用性）
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

# 标题里可上卡的数字（比分 / 名次 / 度量），只取已有文本，不重算。
# 顺序有讲究：「战绩组合」必须排在单度量之前，否则「1平2负」会被切成「2负」。
# 这与 title_elements._NUMBER_PATTERNS 把「战绩」独立成一类的做法一致。
_RE_NUM_TOKEN = re.compile(
    r"\d{1,2}\s*(?:胜|平|负)\s*\d{1,2}\s*(?:胜|平|负)?"
    r"|\d{1,2}\s*[-:比]\s*\d{1,2}"
    r"|\d+(?:\.\d+)?\s*(?:球|分|场|轮|人|次|天|年|岁|分钟|%|万|亿|倍)"
    r"|[一二三四五六七八九十]{1,3}\s*(?:球|分|场|轮|人|次|年)")


def _font(size: int):
    from PIL import ImageFont
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


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


def render(topic: dict, out_dir: Path | str | None = None,
           filename: str | None = None) -> str | None:
    """生成一张数据卡，返回文件路径；失败返回 None（不抛）。

    filename: 指定文件名。发布链路必须传 `article-N-img-001.jpg` 这种规范名，
    因为 publisher.extract_images 只认 `images/article-\\d+-img-\\d+\\.jpg`。
    不传则按标题 hash 命名（debug 用）。
    """
    title = str((topic or {}).get("title") or "").strip()
    if not title:
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    nums = _key_numbers(title)
    img = Image.new("RGB", (W, H), (18, 24, 32))
    draw = ImageDraw.Draw(img)

    # 顶部品牌条
    draw.rectangle([0, 0, W, 8], fill=(214, 48, 49))
    brand_font = _font(34)
    draw.text((MARGIN, 56), "球评人老六", font=brand_font, fill=(255, 255, 255))

    # 关键数字（最多 3 个，横向排）
    if nums:
        num_font = _font(150)
        x = MARGIN
        for n in nums:
            draw.text((x, 120), n, font=num_font, fill=(255, 196, 61))
            x += draw.textlength(n, font=num_font) + 48
            if x > W - MARGIN:
                break

    # 标题（最多 3 行）
    title_font = _font(56 if len(title) <= 26 else 48)
    lines = _wrap(draw, title, title_font, W - 2 * MARGIN, 3)
    y = 340
    for ln in lines:
        draw.text((MARGIN, y), ln, font=title_font, fill=(240, 240, 240))
        y += int(title_font.size * 1.35)

    # 底部来源标注
    foot_font = _font(26)
    draw.text((MARGIN, H - 68), "数据来自公开赛事信息 · 观点仅供参考",
              font=foot_font, fill=(150, 156, 165))

    out = Path(out_dir) if out_dir else CARD_DIR
    out.mkdir(parents=True, exist_ok=True)
    if filename:
        name = Path(filename).name
    else:
        name = "card-" + hashlib.md5(title.encode("utf-8")).hexdigest()[:12] + ".jpg"
    dest = out / name
    if dest.exists():
        return str(dest)
    tmp = Path(tempfile.mkdtemp()) / name
    try:
        img.save(tmp, format="JPEG", quality=90)
        tmp.replace(dest)
    except Exception:
        return None
    return str(dest)


def selftest() -> list[tuple[str, bool]]:
    cases: list[tuple[str, bool]] = []
    title = "1平2负只拿0.33分，中超这根独苗还能撑到2028吗？"
    cases.append(("数字抽取只取已有文本", _key_numbers(title)[:2] == ["1平2负", "0.33分"]))
    cases.append(("无数字时返回空列表", _key_numbers("这场球看得人心里堵得慌") == []))

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
    args = ap.parse_args()
    if args.title:
        p = render({"title": args.title})
        print(p or "（生成失败）")
        return 0 if p else 1
    bad = 0
    for name, ok in selftest():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n自检 {len(selftest())} 项，失败 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
