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
- **队标只作点缀**：缩小到 ~16% 宽、叠一层 gblur 光晕，按句 `enable='between(t,..)'` 显隐。

本机 ffmpeg 能力限制（已实测）：
- 无 `libx264` → 复用 compose.select_video_encoder() 并配 `-b:v`
- 无 `boxblur` → 光晕必须用 `gblur=sigma=`
- `gradients` 多用色时必须显式 `nb_colors=N`，否则写 c2/c3 直接报错
"""

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


def _fit_font_size(text, base, max_px, max_lines=3):
    """按「最多 max_lines 行」选字号，防止大字溢出屏幕。

    大字口播的常见坑：一句 10 个汉字在 1080 宽下用 96px 就会横向溢出。
    做法：先按基准字号折行，若行数超过 max_lines（尤其句子很长时），
    逐步缩小字号（下限 56）直到行数收敛，兼顾「字够大」和「不溢出、不刷屏」。
    """
    if not text:
        return base
    units = _text_units(text)
    size = base
    while size > 56:
        per_line = max_px / size            # 该字号下一行最多几个字符单位
        if units / per_line <= max_lines:   # 行数达标
            break
        size -= 4
    return max(56, min(base, size))


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


def build_motion_ass(segments, *, width=1080, height=1920,
                     font_name="Noto Sans CJK SC", total=None,
                     body_font_size=96, text_color="0xE8ECF4",
                     hl_color="0xFF3B30", num_color="0xFFD60A",
                     outline=6, outline_color="0x10131A",
                     max_chars_per_line=9, keyword_stagger=0.15,
                     in_anim="pop", teams_table=None, extra_words=None,
                     max_kw=3, end_hold=0.35):
    """生成「文字动效」ASS：整屏大字逐句弹出 + 关键词内联高亮 + 入场动效。

    Args:
        segments: [{'start':s,'end':e,'text':t}, ...]（SRT 解析结果）。
        font_name: ASS 的 Fontname（走 fontconfig）。
        total: 成片总时长；最后一句的 end 会被夹到 total 内。
        max_chars_per_line: 每行最多几个全角字（超出自动折行）。
        keyword_stagger: 关键词比正文晚多久弹入（秒），制造「击打感」。
        in_anim: 入场动效 pop | slideup | fade。
    Returns:
        str: ASS 文本。
    """
    max_px = int(width * 0.86)      # 左右各留 7% 边距
    primary = _ass_color(text_color)
    hl_c = _ass_color(hl_color)
    num_c = _ass_color(num_color)
    out_c = _ass_color(outline_color)

    header = [
        "[Script Info]",
        "; Generated by video_pipeline.text_motion.build_motion_ass",
        "ScriptType: v4.00+",
        f"PlayResX: {int(width)}",
        f"PlayResY: {int(height)}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        # 正文：居中、加粗、带描边（深色底上更清晰）
        f"Style: Body,{font_name},{int(body_font_size)},{primary},{primary},"
        f"{out_c},&H00000000,-1,0,0,0,100,100,0,0,1,{int(outline)},2,5,60,60,60,1",
        # 高亮词：比正文大 15%
        f"Style: HL,{font_name},{int(body_font_size * 1.15)},{hl_c},{hl_c},"
        f"{out_c},&H00000000,-1,0,0,0,100,100,0,0,1,{int(outline)},2,5,60,60,60,1",
        # 数字/比分：更大更黄
        f"Style: Num,{font_name},{int(body_font_size * 1.25)},{num_c},{num_c},"
        f"{out_c},&H00000000,-1,0,0,0,100,100,0,0,1,{int(outline) + 1},3,5,60,60,60,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    body = []
    for i, seg in enumerate(segments):
        st = float(seg.get("start", 0) or 0)
        en = float(seg.get("end", 0) or 0)
        text = (seg.get("text") or "").strip()
        if not text or en <= st:
            continue
        if total:
            en = min(en, float(total))
        if i == len(segments) - 1 and total:      # 末句多停留一点，收尾更稳
            en = min(en + end_hold, float(total)) if end_hold else en

        # 关键词 & 内联高亮拼接
        highlights = tk.extract_highlights(
            text, teams_table=teams_table, extra_words=extra_words, max_kw=max_kw)
        pieces = tk.split_by_highlights(text, highlights)
        # 按句动态字号（长句自动缩小），再按行宽折行 → 逐行加内联样式
        fs = _fit_font_size(text, body_font_size, max_px)
        per_line = max(2.0, max_px / fs)
        rendered = _render_line_styles(pieces, per_line,
                                       stagger_ms=int(keyword_stagger * 1000))
        content = r"\N".join(rendered)
        # 本句字号相对基准的缩放系数（内联 \fs 覆盖 Style，保证按句生效）
        scale = fs / float(body_font_size) if body_font_size else 1.0

        start_ts = _ass_time(st + 0.04)
        end_ts = _ass_time(en)
        body.append(f"Dialogue: 0,{start_ts},{end_ts},Body,,0,0,0,,"
                    f"{_anim_prefix(in_anim, scale)}{content}")

    return "\n".join(header + body) + "\n"


def _anim_prefix(anim, scale=1.0):
    """入场动效前缀；scale<1 时按句缩放字号（长句自动变小）。"""
    fs_scale = "" if abs(scale - 1.0) < 0.01 else f"\\fscx{scale*100:.0f}\\fscy{scale*100:.0f}"
    if anim == "slideup":
        base = r"{\fad(180,0)\move(540,1010,540,900)}"
    elif anim == "fade":
        base = r"{\fad(220,0)}"
    else:  # pop
        base = r"{\fad(120,0)\t(0,180,\fscx108\fscy108)}"
    if fs_scale:
        return base[:-1] + fs_scale + "}"   # 插到 } 之前
    return base


def _render_line_styles(pieces, max_units, stagger_ms=0):
    """把 [(片段, kind_or_None)] 按行宽折行，并对高亮片段加内联样式。

    返回 ASS 文本行列表（调用方用 \\N 连接）。
    """
    lines, cur, cur_u = [], "", 0.0
    for frag, kind in pieces:
        if not frag:
            continue
        # 片段可能很长，需内部再切
        for sub in _split_frag(frag, kind, max_units):
            s_text, s_kind = sub
            u = _text_units(s_text)
            if cur_u + u > max_units and cur:
                lines.append(cur)
                cur, cur_u = "", 0.0
            cur += _style_frag(s_text, s_kind, stagger_ms)
            cur_u += u
    if cur:
        lines.append(cur)
    return lines or [""]


def _split_frag(frag, kind, max_units):
    """把一个片段按行宽切成若干子片段（保持 kind 不变）。"""
    if _text_units(frag) <= max_units:
        return [(frag, kind)]
    out, cur, cur_u = [], "", 0.0
    for ch in frag:
        u = _char_units(ch)
        if cur_u + u > max_units and cur:
            out.append((cur, kind))
            cur, cur_u = "", 0.0
        cur += ch
        cur_u += u
    if cur:
        out.append((cur, kind))
    return out


def _style_frag(frag, kind, stagger_ms=0):
    """给片段加内联 ASS 样式；普通正文原样返回。

    stagger_ms > 0 时，该片段延迟一点点再放大（关键词「砸下来」的击打感），
    但仍在同一条 Dialogue 内 —— 不会与正文叠字（叠字是拆成两条 Dialogue 造成的）。
    """
    if not kind or not frag:
        return _escape_ass_text(frag)
    style = _KIND_STYLE.get(kind, "HL")
    safe = _escape_ass_text(frag)
    if stagger_ms > 0:
        # 先保持原大小，延迟 stagger_ms 后放大到 132% —— 制造「后砸下来」的节奏
        anim = f"\\t({int(stagger_ms)},{int(stagger_ms) + 160},\\fscx132\\fscy132)"
    else:
        anim = "\\fscx132\\fscy132"
    # 结尾 \rBody 复位，避免后续正文继承高亮样式（漏了会整句变大变色）
    return f"{{\\r{style}{anim}}}{safe}{{\\rBody}}"


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
                       body_font_size=96, text_color="0xE8ECF4",
                       hl_color="0xFF3B30", num_color="0xFFD60A",
                       outline=6, outline_color="0x10131A",
                       max_chars_per_line=9, in_anim="pop",
                       keyword_stagger=0.15,
                       crest_map=None, crest_size=170, crest_y=0.78,
                       crest_glow=True, teams_table=None, extra_words=None,
                       max_kw=3, keep_ass=False):
    """纯文字动效口播：动态渐变背景 + 大字逐句弹出 + 关键词高亮 + 队标点缀 + 原音轨。

    **单趟 ffmpeg 出片**（含音频），总时长 ≡ 音频时长 —— 不会黑屏、不会切末句。

    Args:
        audio_path: 旁白音频（wav/mp3），其时长决定成片时长。
        srt_or_segments: SRT 文本 或 parse_segments 结果。
        out_mp4: 输出 mp4。
        font_path: 中文字体路径；None 时自动探测。
        bg_colors: 渐变底色（2~8 个 '0xRRGGBB'）。
        crest_map: {段下标: 队标 png 路径}，按该段时间窗显示为小图标点缀。
        teams_table: 球队词表（用于关键词高亮识别队名）。
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
        teams_table=teams_table, extra_words=extra_words, max_kw=max_kw)
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

