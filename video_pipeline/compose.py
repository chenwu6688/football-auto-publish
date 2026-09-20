#!/usr/bin/env python3
"""视频合成（Phase 1）—— ffmpeg 把肖像 + 音频 + 烧录字幕 → 竖屏 mp4。

- 肖像：scale 覆盖裁剪 + 缓慢推拉（zoompan, d=1 保证 zoom 状态跨帧连续，平滑 Ken Burns）。
- 无肖像：用渐变/纯色背景兜底（用老六本人照时无需此兜底）。
- 字幕：subtitles 滤镜烧录，force_style 大字号白字黑描边，适配手机竖屏。
- 全程 CPU 轻负载，无需 GPU。ffprobe 校验产出。

依赖：系统 ffmpeg（本项目已使用）。
"""

import json
import re
import subprocess
from pathlib import Path


def _run(cmd):
    """运行命令，返回 (returncode, stdout, stderr)。"""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


# 编码器优先级：H.264 兼容性最佳；部分精简版 ffmpeg 仅有 libopenh264 / mpeg4
_ENCODER_PREFERENCE = ["libx264", "libopenh264", "h264_v4l2m2m", "mpeg4"]
_cached_encoders = None


def _available_encoders():
    global _cached_encoders
    if _cached_encoders is not None:
        return _cached_encoders
    rc, out, _ = _run(["ffmpeg", "-hide_banner", "-encoders"])
    found = set()
    if rc == 0:
        for line in out.splitlines():
            if line.strip().startswith("V") and not line.strip().startswith("V."):
                # 形如 " V....D libx264 ..."，第二部分为名称
                parts = line.split()
                if len(parts) >= 2:
                    found.add(parts[1])
    _cached_encoders = found
    return found


def select_video_encoder():
    """选出当前 ffmpeg 可用的视频编码器（优先 H.264）。"""
    avail = _available_encoders()
    for enc in _ENCODER_PREFERENCE:
        if enc in avail:
            return enc
    return "mpeg4"  # 最后兜底（几乎必然存在）


def ffprobe_duration(path):
    """用 ffprobe 取时长（秒）。失败返回 0.0。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        rc, out, err = _run(cmd)
        if rc == 0 and out.strip():
            return float(out.strip())
    except Exception:
        pass
    return 0.0


def _to_ff_color(v):
    """'0xRRGGBB' → '&HRRGGBB&'（ffmpeg 滤镜颜色格式）。"""
    if not v:
        return "&HFFFFFF&"
    s = v.strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    return f"&H{s}&"


def _escape_sub_path(path):
    """转义字幕文件路径，供 subtitles 滤镜使用（滤镜图内单引号包裹）。"""
    p = str(path)
    p = p.replace("\\", "\\\\").replace("'", "\\'")
    return p


def compose_video(
    portrait_path, audio_path, srt_path, out_mp4,
    *, width=1080, height=1920, fps=30,
    bg_fallback="gradient", bg_color="0x10131A",
    ken_burns=True,
    sub_style=None,
):
    """合成竖屏视频。

    Args:
        portrait_path: 肖像图路径（不存在/为空则用背景兜底）。
        audio_path: 音频（wav/mp3）。
        srt_path: 字幕 SRT。
        out_mp4: 输出 mp4 路径（父目录会被创建）。
        width/height/fps: 输出分辨率与帧率。
        bg_fallback: "gradient" | "color"。
        bg_color: 纯色兜底色（0xRRGGBB）。
        ken_burns: 肖像缓慢推拉。
        sub_style: dict(font_size, primary_color, outline_color, outline, back_color, margin_v)。
    Returns:
        str: out_mp4 路径。
    """
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    sub_style = sub_style or {}
    fs = sub_style.get("font_size", 46)
    ol = sub_style.get("outline", 4)
    mv = sub_style.get("margin_v", 140)

    has_portrait = bool(portrait_path) and Path(portrait_path).exists()
    dur = ffprobe_duration(audio_path) or 75.0

    # ---- 视频源输入 ----
    inputs = []
    if has_portrait:
        inputs += ["-loop", "1", "-i", str(portrait_path)]
    else:
        if bg_fallback == "color":
            src = f"color=c={bg_color}:s={width}x{height}:r={fps}"
        else:  # gradient（深蓝黑渐变，更耐看）
            src = f"gradients=c0={bg_color}:c1=0x000000:x0=0:y0=0:x1=0:y1=1:s={width}x{height}:r={fps}"
        inputs += ["-f", "lavfi", "-i", src]

    # 音频是最后一个输入（索引 1）
    inputs += ["-i", str(audio_path)]

    # ---- 视频滤镜链 ----
    if has_portrait:
        # 覆盖缩放，保证两维均 ≥ 目标
        vf = (f"[0:v]scale='trunc(iw*max({width}/iw\\,{height}/ih))':"
              f"'trunc(ih*max({width}/iw\\,{height}/ih))',crop={width}:{height}")
        if ken_burns:
            maxzoom = 1.06
            step = (maxzoom - 1.0) / max(dur * fps, 1)
            vf += (f",zoompan=z='min(zoom+{step:.7f}\\,{maxzoom})':d=1:"
                   f"s={width}x{height}:fps={fps}:"
                   f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'")
        vf += ",format=yuv420p"
    else:
        vf = "[0:v]format=yuv420p"

    # 字幕烧录（末端统一标 [v]，供 -map 引用）
    # 关键：先把 SRT 转成 PlayRes=视频尺寸 的 ASS 再烧录。直接把 SRT 交给 subtitles
    # 滤镜时 ffmpeg 会把 PlayRes 固定为 384x288，FontSize/MarginV 被按 288 缩放
    # （在 1080x1920 上放大 ~6.7 倍并跑到画面中部盖住人脸）；original_size 选项在
    # 部分 ffmpeg 构建下无效。生成 ASS 可彻底规避该坑。
    from video_pipeline import subtitles as _subs
    srt_text = Path(srt_path).read_text(encoding="utf-8")
    ass_text = _subs.srt_to_ass(
        srt_text, width=width, height=height, font_size=fs,
        primary_color=sub_style.get("primary_color", "0xFFFFFF"),
        outline_color=sub_style.get("outline_color", "0x000000"),
        back_color=sub_style.get("back_color", "0x80000000"),
        outline=ol, margin_v=mv,
        margin_h=sub_style.get("margin_h", 60),
        font_name=sub_style.get("font_name", "Arial"),
    )
    ass_path = Path(srt_path).with_suffix(".ass")
    ass_path.write_text(ass_text, encoding="utf-8")
    esc = _escape_sub_path(ass_path)
    vf += f",subtitles=filename='{esc}'[v]"

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", vf,
        "-map", "[v]",
        "-map", "1:a",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-shortest", "-movflags", "+faststart",
        str(out_mp4),
    ]
    # 选可用编码器；libx264 用 crf，其他（libopenh264/mpeg4）用目标码率
    venc = select_video_encoder()
    if venc == "libx264":
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    else:
        cmd += ["-c:v", venc, "-b:v", "2M"]
    cmd += ["-c:a", "aac", "-b:a", "192k"]

    rc, out, err = _run(cmd)
    if rc != 0:
        snippet = (err or out)[-1500:]
        raise RuntimeError(f"ffmpeg 合成失败 (rc={rc}):\n{snippet}")

    info = verify_video(out_mp4)
    return str(out_mp4), info


def verify_video(path):
    """ffprobe 校验：返回 {duration, has_video, has_audio, width, height}。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,width,height",
        "-of", "json",
        str(path),
    ]
    rc, out, err = _run(cmd)
    if rc != 0:
        return {"ok": False, "error": (err or "")[:500]}
    try:
        data = json.loads(out)
    except Exception:
        return {"ok": False, "error": "ffprobe 输出无法解析"}
    dur = float(data.get("format", {}).get("duration", 0) or 0)
    has_v = has_a = False
    w = h = 0
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            has_v = True
            w = s.get("width", 0) or w
            h = s.get("height", 0) or h
        elif s.get("codec_type") == "audio":
            has_a = True
    return {"ok": True, "duration": dur, "has_video": has_v,
            "has_audio": has_a, "width": w, "height": h}
