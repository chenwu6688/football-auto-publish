#!/usr/bin/env python3
"""纯文字动效口播渲染器 —— 深色动态渐变底 + 大字逐句弹出 + 关键词高亮 + 队标点缀。

设计要点（为什么这么做）：
- **绝不黑屏**：背景是**一条贯穿全片的 lavfi 渐变源**（gradients 滤镜自带流动），
  句子只是叠加在它上面的 ASS 字幕，物理上不存在「段间黑场」「开场黑屏」。
- **时长 = 音频时长**：`total = ffprobe(audio)`，背景 `d=total` 与输出 `-t total` 双保险，
  从根上杜绝「末句被切」。
- **关键词高亮用 ASS 内联样式，不用 drawtext**：
  drawtext 拆多段需要手算中文字宽（误差大，实测会叠字重影）；
  ASS 的 `{\\c&H..&\\fscx130}词{\\rBody}` 由 libass 自动重排前后文宽度，**零偏移计算**。
- **卡拉OK逐字点亮（v3 核心）**：ASS 原生 `\\k`（厘秒精度）驱动
  「未读暗灰（SecondaryColour）→ 已读白（PrimaryColour）」的颜色填充，
  逐字分摊时间（累计取整，Σ\\k == 块时长，永不漂移）；
  关键词在被读到的那一刻做一次「弹大 112% → 回落」的跳球动效。
  视线跟随朗读节奏 —— 这是抖音/Hormozi 口播号的头部打法，动效是节拍器不是烟花。
- **入场动效全片统一 pop**：翻转/模糊/滑入等花式轮换已证明「不如上一版」；
  抖音铁律 = 一种字体 + 一种入场 + 逐字点亮。因 libass 的 `\\r` 会清空
  此前累积的全部覆盖标签（含 `\\fad`/`\\t`），入场标签在**每个样式组开头重挂**。
- **队标只作点缀**：缩小到 ~16% 宽、叠一层 gblur 光晕，按句 `enable='between(t,..)'` 显隐。

本机 ffmpeg 能力限制（已实测）：
- 无 `libx264` → 复用 compose.select_video_encoder() 并配 `-b:v`
- 无 `boxblur` → 光晕必须用 `gblur=sigma=`
- `gradients` 多用色时必须显式 `nb_colors=N`，否则写 c2/c3 直接报错
"""

import random
import subprocess
from pathlib import Path

from video_pipeline import text_keywords as tk

# ---------------------------------------------------------------- 字体

# 中文字体探测顺序：仓库内置 → Linux → macOS → Windows
_FONT_CANDIDATES = [
    "assets/fonts/NotoSansCJKsc-Bold.otf",
    "assets/fonts/NotoSansCJK-Bold.ttc",
    "assets/fonts/NotoSansSC-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]
# 字体文件名 → ASS 里写的 Fontname（走 fontconfig 查族名）
_FAMILY_HINTS = {
    "NotoSansCJKsc-Bold.otf": "Noto Sans CJK SC",
    "NotoSansCJK-Bold.ttc": "Noto Sans CJK SC",
    "NotoSansCJK-Regular.ttc": "Noto Sans CJK SC",
    "NotoSansSC-Bold.otf": "Noto Sans SC",
    "PingFang.ttc": "PingFang SC",
    "msyhbd.ttc": "Microsoft YaHei",
    "msyh.ttc": "Microsoft YaHei",
}


def resolve_cjk_font(explicit=None):
    """定位可用的中文字体，返回 (fontfile, family_name)。

    顺序：显式指定 → 仓库 assets/fonts → 系统常见路径 → fc-match 兜底。
    **都找不到就抛异常**：绝不静默回退 Arial —— 那正是字幕变方框（乱码）的根因。
    """
    if explicit:
        p = Path(explicit)
        if p.exists():
            return str(p), _FAMILY_HINTS.get(p.name, "Noto Sans CJK SC")
        raise RuntimeError(f"指定的字体不存在：{explicit}")

    root = Path(__file__).resolve().parents[1]
    for c in _FONT_CANDIDATES:
        p = Path(c)
        if not p.is_absolute():
            p = root / c
        if p.exists():
            return str(p), _FAMILY_HINTS.get(p.name, "Noto Sans CJK SC")

    # fc-match 兜底（Linux）
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", "Noto Sans CJK SC"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        if out and Path(out).exists():
            return out, "Noto Sans CJK SC"
    except Exception:
        pass

    raise RuntimeError(
        "未找到中文字体。请把中文字体放到 assets/fonts/ 目录"
        "（推荐 NotoSansCJK-Bold.ttc / NotoSansSC-Bold.otf），"
        "或在配置 textmotion.font_path 指定绝对路径。")


# ---------------------------------------------------------------- 排版

def _char_units(ch):
    """估算字符占位宽度（以全角为 1）：CJK / 全角标点 = 1，ASCII = 0.55。"""
    return 1.0 if ord(ch) > 0x2E80 else 0.55


def _text_units(text):
    return sum(_char_units(c) for c in (text or ""))


def _fit_font_size(text, base, max_px, max_lines=4):
    """按「最多 max_lines 行」选字号，防止大字溢出屏幕。

    大字口播的常见坑：一句 10 个汉字在 1080 宽下用 96px 就会横向溢出。
    做法：先按基准字号折行，若行数超过 max_lines（尤其句子很长时），
    逐步缩小字号（下限 52）直到行数收敛，兼顾「字够大」和「不溢出、不刷屏」。
    """
    if not text:
        return base
    units = _text_units(text)
    size = base
    while size > 52:
        per_line = max_px / size            # 该字号下一行最多几个字符单位
        if units / per_line <= max_lines:   # 行数达标
            break
        size -= 4
    return max(52, min(base, size))


def _wrap_by_units(text, max_units):
    """按字符单位数折行（中文按字、英文按词优先），返回行列表。"""
    text = (text or "").strip()
    if not text:
        return []
    lines, cur, cur_u = [], "", 0.0
    i = 0
    while i < len(text):
        ch = text[i]
        # 英文单词整体处理，避免切断单词
        if ch.isascii() and ch.isalnum():
            j = i
            while j < len(text) and text[j].isascii() and (text[j].isalnum() or text[j] in "-'"):
                j += 1
            token = text[i:j]
            tu = _text_units(token)
            if cur_u + tu > max_units and cur:
                lines.append(cur)
                cur, cur_u = "", 0.0
            cur += token
            cur_u += tu
            i = j
            continue
        u = _char_units(ch)
        if cur_u + u > max_units and cur:
            lines.append(cur)
            cur, cur_u = "", 0.0
        cur += ch
        cur_u += u
        i += 1
    if cur:
        lines.append(cur)
    return lines or [text]


def _ass_time(sec):
    """秒 → ASS 时间 'H:MM:SS.cc'（厘秒）。"""
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs >= 100:      # 四舍五入进位保护
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_color(v, default="&H00FFFFFF"):
    """'0xRRGGBB' / '0xAARRGGBB' → ASS 的 '&HAABBGGRR'（ASS 为 BGR 序）。"""
    if v is None:
        return default
    s = str(v).strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    s = s.upper()
    if len(s) == 8:
        aa, rr, gg, bb = s[0:2], s[2:4], s[4:6], s[6:8]
    elif len(s) == 6:
        aa, rr, gg, bb = "00", s[0:2], s[2:4], s[4:6]
    else:
        return default
    return f"&H{aa}{bb}{gg}{rr}"


# kind → 样式名（num 更大更黄；team/emotion/decision 走 HL 尺寸，颜色由内联 \c 覆盖）
_KIND_STYLE = {"num": "Num", "team": "HL", "emotion": "HL", "decision": "HL"}

# 样式 → 实际渲染宽度放大系数（折行必须按「最终渲染宽度」算，否则行会顶到屏幕边）：
#   HL  字号 = base*1.15（卡拉OK点亮后不额外放大，跳球瞬态 +12% 忽略不计）
#   Num 字号 = base*1.25
_KIND_SCALE = {"HL": 1.15, "Num": 1.25}


def build_motion_ass(segments, *, width=1080, height=1920,
                     font_name="Noto Sans CJK SC", total=None,
                     body_font_size=130, text_color="0xE8ECF4",
                     hl_color="0xFF3B30", num_color="0xFFD60A",
                     outline=6, outline_color="0x10131A",
                     max_chars_per_line=9, keyword_stagger=0.15,
                     in_anim="random", teams_table=None, extra_words=None,
                     max_kw=1, end_hold=0.35, max_block_units=9,
                     word_timings=None, anim_seed=None, margin_v=700):
    """生成「文字动效」ASS：卡拉OK逐字点亮 + 关键词跳球 + 随机入场动效（轻重缓急）。

    Args:
        segments: [{'start':s,'end':e,'text':t}, ...]（SRT 解析结果）。
        font_name: ASS 的 Fontname（走 fontconfig）。
        total: 成片总时长；最后一块的显示会被夹到 total 内。
        max_chars_per_line: 兼容参数（实际每行字数由 max_px/字号 推导）。
        keyword_stagger: **已弃用**（卡拉OK逐字点亮接管了节奏），仅为兼容保留。
        in_anim: random（推荐·12 种动效随机 + 轻重缓急三档，连续不重样）|
                 auto（全片统一 pop）| 固定单种（pop/flipx/...）。
        anim_seed: 随机种子；None=每次随机，整数=可复现（同输入同动效）。
        word_timings: 词级时间轴 [{"start","end","text"}]（秒，来自 tts 的
            .words.json）。**提供后块时间窗与逐字点亮直接跟真实发音对齐**
            （解决「字幕跟不上语速」）；缺省退化为按字数线性分摊。
        margin_v: 底部边距（px），滑入动效的落点锚点 = height - margin_v。
        max_kw: 每块最多高亮几个词（默认 1 —— 每屏只点亮一个最值钱的词）。
        max_block_units: 每屏块最大字符单位（默认 9，抖音大字流）。
    Returns:
        str: ASS 文本。
    """
    max_px = int(width * 0.80)      # 左右各留 10% 边距（高亮词放大后也不顶边）
    primary = _ass_color(text_color)
    hl_c = _ass_color(hl_color)
    num_c = _ass_color(num_color)
    out_c = _ass_color(outline_color)
    # 卡拉OK「未读态」颜色（SecondaryColour）：暗灰 —— \k 逐字从暗灰点亮到主色
    sec_c = _ass_color("0x7A8996")

    header = [
        "[Script Info]",
        "; Generated by video_pipeline.text_motion.build_motion_ass",
        "ScriptType: v4.00+",
        f"PlayResX: {int(width)}",
        f"PlayResY: {int(height)}",
        "ScaledBorderAndShadow: yes",
        # WrapStyle 0 = smart wrapping：即便生成侧折行计算偶有偏差，
        # libass 也会在 MarginL/R 内自动折行兜底，而不是把行画出屏幕
        "WrapStyle: 0",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        # 正文：底部居中（Alignment 2 + MarginV 700 → 稳定落在画面中下 1/3，抖音构图）；
        # PrimaryColour=已读白，SecondaryColour=未读暗灰（\k 逐字点亮）
        f"Style: Body,{font_name},{int(body_font_size)},{primary},{sec_c},"
        f"{out_c},&H00000000,-1,0,0,0,100,100,0,0,1,{int(outline)},2,2,90,90,{int(margin_v)},1",
        # 关键词：比正文大 15%，已读态=红（未读同为暗灰）
        f"Style: HL,{font_name},{int(body_font_size * 1.15)},{hl_c},{sec_c},"
        f"{out_c},&H00000000,-1,0,0,0,100,100,0,0,1,{int(outline)},2,2,90,90,{int(margin_v)},1",
        # 数字/比分：更大更黄
        f"Style: Num,{font_name},{int(body_font_size * 1.25)},{num_c},{sec_c},"
        f"{out_c},&H00000000,-1,0,0,0,100,100,0,0,1,{int(outline) + 1},3,2,90,90,{int(margin_v)},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    # ---------- 阶段 1：全局块流 + 时间窗（词级对齐优先，字数加权兜底） ----------
    body = []
    flat = []                     # [(blk, seg_start, seg_end)]
    for seg in segments:
        st = float(seg.get("start", 0) or 0)
        en = float(seg.get("end", 0) or 0)
        text = (seg.get("text") or "").strip()
        if not text or en <= st:
            continue
        if total:
            en = min(en, float(total))
        for blk in _split_blocks(text, max_block_units):
            flat.append((blk, st, en))
    if not flat:
        return "\n".join(header + body) + "\n"

    win = _block_windows(flat, word_timings, total=total, end_hold=end_hold)
    # win: [(blk, disp_start, disp_end, light or None)]，light 与块字符流对齐

    # ---------- 阶段 2：逐块渲染（卡拉OK + 随机动效） ----------
    body = []
    rng = random.Random(anim_seed)
    prev_anim = None
    anim_idx = 0
    for blk, b_st, b_en, light in win:
        if b_en <= b_st:
            continue
        highlights = tk.extract_highlights(
            blk, teams_table=teams_table, extra_words=extra_words, max_kw=max_kw)
        pieces = tk.split_by_highlights(blk, highlights)
        # 块短 → 字大：块内最多 2 行（抖音风格），超出才缩字号（9 字块一般不触发）
        fs = _fit_font_size(blk, body_font_size, max_px, max_lines=2)
        per_line = max(2.0, max_px / fs)
        scale = fs / float(body_font_size) if body_font_size else 1.0
        # 卡拉OK cs：词级 = 逐字真实点亮时刻；兜底 = 线性分摊（Σ\k=显示窗）
        if light:
            win_cs = max(1, int(round((b_en - b_st) * 100)))
            cs_list, prev_c = [], 0
            for _stc, enc in light:
                cur_c = min(max(0, int(round((enc - b_st) * 100))), win_cs)
                cur_c = max(cur_c, prev_c)        # 单调 + 截断到显示窗
                cs_list.append(cur_c - prev_c)
                prev_c = cur_c
            disp_cs = None
        else:
            cs_list = None
            disp_cs = max(1, int(round((b_en - b_st) * 100)))
        # 入场动效：random = 随机动效 × 轻重缓急；auto = 统一 pop；固定单种
        if in_anim == "random":
            anim = _pick_anim("random", anim_idx, rng=rng, prev=prev_anim)
            dur, power = _pick_rhythm(rng)
            prev_anim = anim
        else:
            anim = _pick_anim(in_anim, anim_idx)
            dur, power = 240, 1.0
        anim_idx += 1
        entrance = _anim_prefix(anim, scale=scale, width=width, height=height,
                                dur=dur, power=power, margin_v=margin_v)
        rendered = _render_line_styles(pieces, per_line, scale=scale,
                                       total_cs=disp_cs, entrance=entrance,
                                       char_cs=cs_list)
        content = r"\N".join(rendered)
        body.append(f"Dialogue: 0,{_ass_time(b_st)},{_ass_time(b_en)},Body,,0,0,0,,{content}")

    return "\n".join(header + body) + "\n"


# 动效池（in_anim=random 时加权随机抽 + 轻重缓急三档，连续不重样 →「对话式插入感」）：
#   翻转类 flipx/flipy/swing · 缩放类 pop/zoomout/zoomin · 位移类 slide×4 · 质感 blurin/fade
# in_anim=auto 仍只走 pop（统一节拍模式，保留给喜欢克制风格的场景）。
_ANIM_POOL = ["pop", "flipx", "flipy", "zoomout", "zoomin", "swing",
              "slideup", "slidedown", "slideleft", "slideright", "blurin", "fade"]

# 轻重缓急档位：时长（ms）× 力度（幅度倍率）
_SPEED_PRESETS = {"fast": 240, "mid": 320, "slow": 460}
_POWER_PRESETS = {"light": 0.8, "mid": 1.0, "heavy": 1.25}


def _pick_anim(mode, index, rng=None, prev=None):
    """选入场动效：random=随机抽（连续不重样，最多回抽 5 次）；auto=统一 pop；其他=固定单种。"""
    if mode == "random" and rng is not None:
        a = prev
        for _ in range(5):
            a = rng.choice(_ANIM_POOL)
            if a != prev:
                break
        return a
    if not mode or mode == "auto":
        return "pop"
    return mode


def _pick_rhythm(rng):
    """轻重缓急：时长档（fast/mid/slow）× 力度档（light/mid/heavy）加权随机。

    快而轻 = 轻快插入感；慢而重 = 重音砸落感 —— 让每块的出现都有自己的「语气」。
    """
    def _weighted(pairs):
        r = rng.random() * sum(w for _v, w in pairs)
        for v, w in pairs:
            r -= w
            if r <= 0:
                return v
        return pairs[-1][0]

    dur = _weighted([(_SPEED_PRESETS["fast"], 0.35),
                     (_SPEED_PRESETS["mid"], 0.45),
                     (_SPEED_PRESETS["slow"], 0.20)])
    power = _weighted([(_POWER_PRESETS["light"], 0.30),
                       (_POWER_PRESETS["mid"], 0.45),
                       (_POWER_PRESETS["heavy"], 0.25)])
    return int(dur), float(power)


# 长句切块的分隔标点（优先在句读处断）
_BLOCK_SPLIT = "，。！？；、：…—"


def _split_blocks(text, max_units):
    """把长句按标点切成 ≤max_units 的「显示块」（抖音口播号风格：每屏字少、字大）。

    先按句读标点切短语，再贪心合并相邻短语到不超 max_units；
    无标点的超长短语按 max_units 硬切。返回块列表。
    """
    phrases, buf = [], ""
    for ch in (text or ""):
        buf += ch
        if ch in _BLOCK_SPLIT:
            phrases.append(buf)
            buf = ""
    if buf:
        phrases.append(buf)
    blocks, cur = [], ""
    for p in phrases:
        # 单短语本身超长（无标点）→ 硬切
        while _text_units(p) > max_units:
            n, u = 0, 0.0
            for i, ch in enumerate(p):
                u += _char_units(ch)
                if u > max_units and i > 0:
                    n = i
                    break
            else:
                n = len(p)
            if cur:
                blocks.append(cur)
                cur = ""
            blocks.append(p[:n])
            p = p[n:]
        if cur and _text_units(cur) + _text_units(p) > max_units:
            blocks.append(cur)
            cur = ""
        cur += p
    if cur:
        blocks.append(cur)
    return [b for b in blocks if b.strip()]


def _anim_prefix(anim, scale=1.0, width=1080, height=1920, dur=240, power=1.0,
                 margin_v=700):
    """入场动效标签（含首尾花括号）。

    原则：**动画终值 = 静态值 = 稳态**（折行按稳态宽度算），动画只负责
    「从初值过渡到稳态」。所有 \\fscx/\\fscy 都乘 scale（长句整句缩小）。
    ⚠️ \\r 会清空此前累积的全部覆盖标签（含本标签），所以返回值会在
    **每个样式组开头重挂**（见 _render_line_styles），不是只写在 Dialogue 开头。

    dur/power：动效时长（ms）与力度倍率（1.0=默认幅度），random 模式由
    _pick_rhythm 按三档随机抽（轻重缓急）；固定/auto 模式用默认值。

    动效一览（random 池 12 种 + spread 展开保留为固定项）：
        pop        轻弹落定（鼓到 108%×power 再回落）
        flipx      竖着翻牌（绕 X 轴翻正，角度 88°×power）
        flipy      横着翻面（绕 Y 轴）
        swing      斜着甩正（-14°×power 旋转 + 从小放大）
        zoomout    从大到小砸定（185%×power → 100%）
        zoomin     从小放大顶定（55%/power → 100%）
        slideup / slidedown / slideleft / slideright
                   四向滑入（「对话式插入感」主力：从锚点对应方向滑入落位；
                   锚点 = (width/2, height−margin_v)，即底部居中构图）
        blurin     模糊 → 清晰
        spread     从画面中线向两侧展开（clip 扫出；仅固定指定，不进随机池）
        fade       纯渐入渐出
    （旧 slideup 的硬编码 \move 坐标已改为按 an2 锚点参数化计算。）
    """
    s = float(scale)
    fsx = lambda v: str(max(1, int(round(v * s))))    # \fscx 值 = 目标% × 句缩放（\fscx 本身就是百分比）
    fs = f"\\fscx{fsx(100)}\\fscy{fsx(100)}"           # 稳态缩放
    dur = max(120, int(dur))
    p = max(0.5, min(1.6, float(power)))
    if anim == "flipx":
        ang = min(135, int(round(88 * p)))
        return "{\\fad(150,90)\\frx" + str(ang) + fs + "\\t(0," + str(dur) + ",\\frx0)}"
    if anim == "flipy":
        ang = min(135, int(round(88 * p)))
        return "{\\fad(150,90)\\fry-" + str(ang) + fs + "\\t(0," + str(dur) + ",\\fry0)}"
    if anim == "swing":
        ang = int(round(14 * p))
        v0 = max(40, int(round(66 / p)))
        return ("{\\fad(150,90)\\frz-" + str(ang) + "\\fscx" + fsx(v0) + "\\fscy" + fsx(v0)
                + "\\t(0," + str(dur) + ",\\frz0\\fscx" + fsx(100) + "\\fscy" + fsx(100) + ")}")
    if anim == "zoomout":
        v0 = min(260, int(round(185 * p)))
        return ("{\\fad(120,90)\\fscx" + fsx(v0) + "\\fscy" + fsx(v0)
                + "\\t(0," + str(dur) + ",\\fscx" + fsx(100) + "\\fscy" + fsx(100) + ")}")
    if anim == "zoomin":
        v0 = max(40, int(round(55 / p)))
        return ("{\\fad(120,90)\\fscx" + fsx(v0) + "\\fscy" + fsx(v0)
                + "\\t(0," + str(dur) + ",\\fscx" + fsx(100) + "\\fscy" + fsx(100) + ")}")
    if anim in ("slideup", "slidedown", "slideleft", "slideright"):
        ax, ay = int(width // 2), int(height - margin_v)
        d = int(round(55 * p))
        dx, dy = {"slideup": (0, d), "slidedown": (0, -d),
                  "slideleft": (d, 0), "slideright": (-d, 0)}[anim]
        # 稳态缩放 fs 必须声明在 \move() 之外（拼进 move 参数会吞掉标签）
        return ("{\\fad(120,90)" + fs + "\\move(" + str(ax + dx) + "," + str(ay + dy) + ","
                + str(ax) + "," + str(ay) + ",0," + str(dur) + ")}")
    if anim == "blurin":
        bl = min(28, int(round(16 * p)))
        return "{\\fad(140,90)\\blur" + str(bl) + fs + "\\t(0," + str(dur) + ",\\blur0.8)}"
    if anim == "spread":
        cx, cy = int(width // 2), int(height // 2)
        x0, y0 = int(width * 0.06), int(height * 0.02)     # 展开终点留 6% 边距
        x1, y1 = width - x0, height - y0
        return ("{\\fad(100,90)"
                f"\\clip({cx},-50,{cx},{height + 50})"
                f"\\t(0,480,\\clip({x0},{y0},{x1},{y1}))" + "}")
    if anim == "fade":
        return "{\\fad(" + str(min(220, dur)) + ",110)}"
    # pop（默认）：鼓到 108%×power 再回落稳态（终值=稳态，不改变行宽）；
    # dur=240、power=1.0 时与上一版完全一致（\t(0,90,...)\t(90,240,...)）
    t1 = max(60, int(dur * 0.375))
    bump = min(128, int(round(108 * p)))
    return ("{\\fad(120,110)" + fs
            + "\\t(0," + str(t1) + ",\\fscx" + fsx(bump) + "\\fscy" + fsx(bump) + ")"
            + "\\t(" + str(t1) + "," + str(dur) + ",\\fscx" + fsx(100) + "\\fscy" + fsx(100) + ")}")


# 行首禁则字符（避头点）：这些标点不应出现在行首
_NO_LINE_START = "，。、；：！？）】》\"'…—%"


def _karaoke_cs_list(stream, total_cs):
    """把 total_cs（厘秒）按字符占位单位分摊到每个字符。

    累计取整法：cs_i = round(total_cs × 累计单位 / 总单位) − 上一项的累计值，
    保证 Σcs == total_cs 恒成立 —— 长句逐字点亮不漂移（末字正好在块尾点亮）。
    中文一字一音，线性插值与语音节奏天然对齐。
    """
    total_u = sum(_char_units(ch) for ch, _ in stream) or 1.0
    out, cum_u, prev = [], 0.0, 0
    for ch, _ in stream:
        cum_u += _char_units(ch)
        cur = int(round(total_cs * cum_u / total_u))
        out.append(max(0, cur - prev))
        prev = cur
    return out


# 不发音字符（切块标点 + 句读 + 常见符号/空白）——对齐词时间轴时跳过
_PUNCT_ALL = set("，。！？；、：…—,.!?;: \t　()（）【】《》\"'“”‘’%·_-")


def _is_voice(ch):
    """是否发音字符（去标点/空白）。"""
    return bool(ch.strip()) and ch not in _PUNCT_ALL


def _block_windows(flat, words, *, total=None, end_hold=0.35,
                   lead=0.04, tail_gap=0.6):
    """计算每块的显示窗与逐字点亮时刻（解决「字幕跟不上语速」的核心）。

    词级模式（words 有效）：
      - 全局「块发音字符流」按占比映射到「词字符流」，每字的点亮完成时刻 =
        所在词区间内按字占比取点（标点继承前一个发音字，随其一起点亮）；
      - disp_start = 块首字 start − lead（提前 ~40ms 入场，淡入不吞首字点亮）；
      - disp_end = 下一块 disp_start（首尾相接**不叠字**）；换句最多多挂
        tail_gap 秒，全片末块最多多挂 end_hold（均夹到 total）；
      - 点亮累计只到「块末字完成时刻」，不足窗的部分保持已读态（自然停留）。
    无词表兜底：句内按字数加权分摊 + 0.6s 借时（旧行为），点亮线性分摊。

    Args:
        flat: [(blk, seg_start, seg_end)] 全局块流。
        words: [{"start","end","text"}]（秒）或 None。
    Returns:
        [(blk, disp_start, disp_end, light or None)]，light 与块字符流对齐。
    """
    wchars = []                      # 词字符流 [(char, word_idx)]
    for wi, w in enumerate(words or []):
        for c in (w.get("text") or ""):
            if _is_voice(c):
                wchars.append((c, wi))
    m = len(wchars)
    spans = {}                       # word_idx -> [流内起, 流内止)
    for i, (_c, wi) in enumerate(wchars):
        if wi in spans:
            spans[wi][1] = i + 1
        else:
            spans[wi] = [i, i + 1]
    n_voice = sum(1 for blk, _s, _e in flat for ch in blk if _is_voice(ch))
    usable = m > 0 and n_voice > 0

    if not usable:
        # ---- 兜底：句内按字数加权分摊 + 0.6s 借时（旧行为）----
        win = []
        i, nb = 0, len(flat)
        while i < nb:
            j = i
            while j < nb and (flat[j][1], flat[j][2]) == (flat[i][1], flat[i][2]):
                j += 1
            st0, en0 = flat[i][1], flat[i][2]
            if j == nb and total:            # 最后一句多停留，收尾更稳
                en0 = min(float(total), en0 + (end_hold or 0))
            blocks = [flat[k][0] for k in range(i, j)]
            units = [_text_units(b) for b in blocks]
            su = sum(units) or 1.0
            t = st0
            for k, blk in enumerate(blocks):
                dt = (en0 - st0) * units[k] / su
                b_st, b_en = t, t + dt
                t = b_en
                cnt = j - i
                if k == cnt - 1:
                    b_en = en0
                elif dt < 0.6 and k + 1 < cnt:
                    b_en += min(0.6 - dt, (en0 - t) * 0.5)
                    t = b_en
                win.append([blk, b_st + 0.04, b_en, None])
            i = j
        return win

    # ---- 词级模式：逐字点亮时刻 ----
    win = []
    q = 0                            # 全局发音字符游标
    for bi, (blk, seg_st, _seg_en) in enumerate(flat):
        light, first_st, last_en = [], None, None
        for ch in blk:
            if _is_voice(ch):
                # floor 中心映射（int 截断）：m==n 时恒等映射；round 的银行家
                # 舍入在词边界处会跳到下一词（单字块窗被挤成 10ms 的根因）。
                qm = min(m - 1, int((q + 0.5) * m / max(1, n_voice)))
                wi = wchars[qm][1]
                i0, i1 = spans[wi]
                w = words[wi]
                wd = max(1e-3, float(w["end"]) - float(w["start"]))
                frac = (qm - i0) / max(1, i1 - i0)
                stc = float(w["start"]) + wd * frac
                enc = float(w["start"]) + wd * min(1.0, frac + 1.0 / max(1, i1 - i0))
                enc = max(enc, stc + 0.01)
                light.append((stc, enc))
                first_st = stc if first_st is None else first_st
                last_en = enc
                q += 1
            elif light:
                light.append(light[-1])          # 标点/空白继承前一个发音字
            else:
                light.append((seg_st, seg_st + 0.2))     # 块首即标点（罕见）兜底
                first_st = seg_st if first_st is None else first_st
                last_en = seg_st + 0.2
        win.append([blk, max(0.0, (first_st or seg_st) - lead), last_en or seg_st,
                    light])

    # 显示窗：首尾相接不叠字；换句多挂 tail_gap；全片末块多挂 end_hold
    for bi, item in enumerate(win):
        disp_start, last_en = item[1], item[2]
        if bi + 1 < len(win):
            disp_end = win[bi + 1][1]
            if (flat[bi + 1][1], flat[bi + 1][2]) != (flat[bi][1], flat[bi][2]):
                disp_end = min(disp_end, last_en + tail_gap)
        else:
            cap = float(total) if total else last_en + end_hold
            disp_end = min(cap, last_en + max(end_hold, tail_gap))
        item[2] = max(disp_end, disp_start + 0.3)        # 最短显示 0.3s 防闪屏
        if bi + 1 < len(win):
            # 快语速连读时 0.3s 最短显示可能越过下一块起点 → libass 会对重叠
            # Dialogue 做堆叠布局（字幕跳动）。不重叠优先：cap 到下一块起点。
            item[2] = max(min(item[2], win[bi + 1][1]), item[1] + 0.01)
    return win


# 「尚未遇到任何字符」哨兵（区别于 None=普通正文，用于组开标签判定）
_UNSET = object()


def _render_line_styles(pieces, max_units, scale=1.0, total_cs=0, entrance="",
                        char_cs=None):
    """把 [(片段, kind_or_None)] 折行，生成「卡拉OK逐字点亮」的 ASS 行。

    - 三态配色：未读=暗灰（Style 的 SecondaryColour）→ 已读=白/红/黄
      （PrimaryColour），由 ASS 原生 \\k（厘秒精度）驱动，逐字填充；
    - char_cs：词级模式下与块字符流对齐的逐字 cs 列表（点亮跟真实语音），
      优先于 total_cs 线性分摊；
    - 关键词组在被读到的那一刻（ms = 累计厘秒×10）做一次
      「弹大 112% → 回落」的跳球动效；普通正文只变色不弹（克制）；
    - 行宽按「渲染占宽」计算（关键词乘放大系数），保证不顶屏幕边；
      避头点：，。等标点不出现在行首（悬挂在上一行行尾）；
    - entrance：入场动效标签（含首尾花括号）。因 libass 的 \\r 会清空
      此前累积的全部覆盖标签（含 \\fad/\\t/\\move 入场动画），入场标签必须
      **在每个样式组开头重新声明**，而不是只写在 Dialogue 开头。
    返回 ASS 文本行列表（调用方用 \\N 连接）。
    """
    # 0) 入场标签去掉外层括号（要嵌进每个组的开标签里）
    inner = ""
    if entrance:
        inner = entrance[1:-1] if (entrance.startswith("{")
                                   and entrance.endswith("}")) else entrance

    # 1) 展平成字符流 [(ch, kind)]（高亮词整词同 kind）
    stream = []
    for frag, kind in pieces:
        for ch in (frag or ""):
            stream.append((ch, kind))
    if not stream:
        return [""]

    # 2) 卡拉OK计时：词级 char_cs 优先（与块字符流对齐）→ 线性分摊 → 静态
    if char_cs is not None:
        cs_list = [max(0, int(c)) for c in list(char_cs)[:len(stream)]]
        cs_list += [0] * (len(stream) - len(cs_list))
    elif total_cs and int(total_cs) > 0:
        cs_list = _karaoke_cs_list(stream, int(total_cs))
    else:
        cs_list = None

    # 3) 逐字符填行（宽度按高亮放大系数加权；避头点悬挂）
    lines_chars, cur, cur_u = [], [], 0.0
    for idx, (ch, kind) in enumerate(stream):
        u = _char_units(ch) * (_KIND_SCALE.get(_KIND_STYLE.get(kind, ""), 1.0)
                               if kind else 1.0)
        if cur and cur_u + u > max_units:
            # 避头点：下一行行首不能是标点 → 标点悬挂在本行行尾（超宽 1 字符可接受）
            if ch in _NO_LINE_START:
                cur.append((idx, ch, kind))
                cur_u += u
                lines_chars.append(cur)
                cur, cur_u = [], 0.0
                continue
            lines_chars.append(cur)
            cur, cur_u = [], 0.0
        cur.append((idx, ch, kind))
        cur_u += u
    if cur:
        lines_chars.append(cur)

    # 4) 按样式组输出：组开标签 = \r样式 + 句级缩放 + 入场动画（关键词组另挂跳球 \t）。
    #    相邻组之间不需要「关闭」标签 —— 下一组的 \r 本身就是完全复位。
    sx = max(1, int(round(scale * 100)))            # 句级缩放（%）
    bump = max(1, int(round(sx * 1.12)))            # 跳球瞬态（+12%）
    out, cum_ms = [], 0                             # cum_ms 跨行累计（卡拉OK时间轴是块级的）
    for line in lines_chars:
        parts, cur_kind = [], _UNSET
        for idx, ch, kind in line:
            if kind is not cur_kind:
                if kind:
                    style = _KIND_STYLE.get(kind, "HL")
                    ms = cum_ms
                    parts.append(f"{{\\r{style}\\fscx{sx}\\fscy{sx}{inner}"
                                 f"\\t({ms},{ms + 140},\\fscx{bump}\\fscy{bump})"
                                 f"\\t({ms + 140},{ms + 280},\\fscx{sx}\\fscy{sx})}}")
                else:
                    parts.append(f"{{\\rBody\\fscx{sx}\\fscy{sx}{inner}}}")
                cur_kind = kind
            if cs_list is not None:
                parts.append(f"{{\\k{cs_list[idx]}}}")
            parts.append(_escape_ass_text(ch))
            cum_ms += cs_list[idx] if cs_list else 0
        out.append("".join(parts))
    return out or [""]


def _escape_ass_text(s):
    """转义 ASS 文本中会破坏语法的字符。"""
    if not s:
        return ""
    return (s.replace("\\", "\\\\")
             .replace("{", "\\{")
             .replace("}", "\\}"))


# ---------------------------------------------------------------- 渲染

def _escape_filter_path(p):
    """转义滤镜里用的文件路径（冒号/反斜杠/单引号）。"""
    s = str(p)
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _build_vf(width, height, crest_specs, crest_size, crest_y, crest_glow,
              ass_path, *, margin_px=None):
    """构造 filter_complex：队标 overlay（可多个，各自 enable 时间窗）+ ass 烧字。

    crest_specs: [(input_idx, seg_start, seg_end), ...]
    """
    parts = []
    cur = "0:v"
    y = int(height * crest_y)
    for i, (idx, st, en) in enumerate(crest_specs):
        win = f":enable='between(t,{st:.3f},{en:.3f})'"
        cx = (width - crest_size) // 2
        parts.append(f"[{idx}:v]scale={crest_size}:{crest_size}"
                     f":force_original_aspect_ratio=decrease,format=rgba[c{i}]")
        parts.append(f"[c{i}]split[c{i}a][c{i}b]")
        if crest_glow:
            g = int(crest_size * 1.5)
            parts.append(f"[c{i}b]scale={g}:{g},gblur=sigma=20,"
                         f"colorchannelmixer=aa=0.9[g{i}]")
            gy = y - (g - crest_size) // 2
            gx = (width - g) // 2
            parts.append(f"[{cur}][g{i}]overlay=x={gx}:y={gy}:format=auto{win}[o{i}g]")
            cur = f"o{i}g"
        parts.append(f"[{cur}][c{i}a]overlay=x={cx}:y={y}:format=auto{win}[o{i}c]")
        cur = f"o{i}c"
    parts.append(f"[{cur}]ass=filename='{_escape_filter_path(ass_path)}',"
                 f"format=yuv420p[vout]")
    return ";".join(parts)


def render_text_motion(audio_path, srt_or_segments, out_mp4, *,
                       width=1080, height=1920, fps=30, font_path=None,
                       bg_colors=("0x22365C", "0x141A28", "0x3A2450"),
                       bg_speed=0.02, bg_type="radial",
                       body_font_size=130, text_color="0xE8ECF4",
                       hl_color="0xFF3B30", num_color="0xFFD60A",
                       outline=6, outline_color="0x10131A",
                       max_chars_per_line=9, in_anim="random",
                       keyword_stagger=0.15,
                       crest_map=None, crest_size=170, crest_y=0.78,
                       crest_glow=True, teams_table=None, extra_words=None,
                       max_kw=1, keep_ass=False, max_block_units=9,
                       word_timings=None, anim_seed=None, margin_v=700):
    """纯文字动效口播：动态渐变背景 + 大字卡拉OK逐字点亮 + 随机动效 + 队标点缀 + 原音轨。

    **单趟 ffmpeg 出片**（含音频），总时长 ≡ 音频时长 —— 不会黑屏、不会切末句。

    Args:
        audio_path: 旁白音频（wav/mp3），其时长决定成片时长。
        srt_or_segments: SRT 文本 或 parse_segments 结果。
        out_mp4: 输出 mp4。
        font_path: 中文字体路径；None 时自动探测。
        bg_colors: 渐变底色（2~8 个 '0xRRGGBB'）。
        crest_map: {段下标: 队标 png 路径}，按该段时间窗显示为小图标点缀。
        teams_table: 球队词表（用于关键词高亮识别队名）。
        in_anim: random（随机 12 种动效+轻重缓急，推荐）| auto（统一 pop）| 固定单种。
        anim_seed: 随机种子（None=每次随机；整数=可复现）。
        word_timings: 词级时间轴 [{"start","end","text"}]（秒）——来自 tts 的
            .words.json；提供后逐字点亮与块时间窗直接跟真实发音对齐。
        keyword_stagger: 已弃用（卡拉OK逐字点亮接管节奏），仅为兼容保留。
        keep_ass: True 时保留生成的 .ass 供排查。
    Returns:
        str: out_mp4
    """
    from video_pipeline import compose

    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    segments = _to_segments(srt_or_segments)
    if not segments:
        raise RuntimeError("text_motion: segments 为空")

    dur = compose.ffprobe_duration(audio_path)
    if not dur or dur <= 0:
        dur = max(float(s.get("end", 0) or 0) for s in segments) or 60.0
    total = float(dur)

    ff, family = resolve_cjk_font(font_path)

    ass_text = build_motion_ass(
        segments, width=width, height=height, font_name=family, total=total,
        body_font_size=body_font_size, text_color=text_color,
        hl_color=hl_color, num_color=num_color, outline=outline,
        outline_color=outline_color, max_chars_per_line=max_chars_per_line,
        keyword_stagger=keyword_stagger, in_anim=in_anim,
        teams_table=teams_table, extra_words=extra_words, max_kw=max_kw,
        max_block_units=max_block_units, word_timings=word_timings,
        anim_seed=anim_seed, margin_v=margin_v)
    ass_path = out_mp4.with_suffix(".motion.ass")
    ass_path.write_text(ass_text, encoding="utf-8")

    # ---- 输入：渐变背景(0) → 队标(1..N) → 音频(N+1) ----
    colors = list(bg_colors or [])[:8]
    if len(colors) < 2:
        colors = ["0x10131A", "0x1B2A4A"]
    gsrc = (f"gradients=s={width}x{height}:r={fps}:d={total:.3f}"
            f":nb_colors={len(colors)}"
            + "".join(f":c{i}={c}" for i, c in enumerate(colors))
            + f":speed={float(bg_speed):.4f}:type={bg_type}")
    inputs = ["-f", "lavfi", "-i", gsrc]

    crest_specs = []
    for seg_i, png in sorted((crest_map or {}).items()):
        try:
            k = int(seg_i)
        except (TypeError, ValueError):
            continue
        if not (0 <= k < len(segments)):
            continue
        p = Path(str(png))
        if not p.exists():
            continue
        inputs += ["-i", str(p)]
        seg = segments[k]
        st = float(seg.get("start", 0) or 0)
        en = min(float(seg.get("end", 0) or 0), total)
        if en <= st:
            en = min(st + 2.0, total)
        crest_specs.append((len(crest_specs) + 1, st, en))

    audio_idx = len(crest_specs) + 1
    inputs += ["-i", str(audio_path)]

    vf = _build_vf(width, height, crest_specs, crest_size, crest_y, crest_glow, ass_path)

    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", vf,
           "-map", "[vout]", "-map", f"{audio_idx}:a",
           "-t", f"{total:.3f}", "-r", str(fps),
           "-movflags", "+faststart"]
    venc = compose.select_video_encoder()
    if venc == "libx264":
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    else:
        cmd += ["-c:v", venc, "-b:v", "3M"]
    cmd += ["-c:a", "aac", "-b:a", "192k", str(out_mp4)]

    rc, out, err = compose._run(cmd)
    if not keep_ass:
        try:
            ass_path.unlink()
        except OSError:
            pass
    if rc != 0:
        raise RuntimeError(f"文字动效渲染失败 (rc={rc}):\n{(err or out)[-1500:]}")

    return str(out_mp4)


def _to_segments(srt_or_segments):
    """把 SRT 文本或 segments 列表统一成 [{'start','end','text'}]。"""
    if isinstance(srt_or_segments, str):
        from video_pipeline.subtitles import parse_segments
        return parse_segments(srt_or_segments)
    out = []
    for s in (srt_or_segments or []):
        try:
            st = float(s.get("start", 0) or 0)
            en = float(s.get("end", 0) or 0)
        except (TypeError, ValueError):
            continue
        txt = (s.get("text") or "").strip()
        if txt and en > st:
            out.append({"start": st, "end": en, "text": txt})
    return out

