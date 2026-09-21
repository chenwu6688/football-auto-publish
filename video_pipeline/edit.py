#!/usr/bin/env python3
"""多轨剪接（Phase 3 · B-roll 切入）—— 让口播视频"有画面"，更自然。

设计要点：
- 输入：主讲人视频(anchor，含旁白音) + 句时间轴(segments) + 素材池(footage_pool)。
- 处理：把时间轴切成若干段，每段优先用一条 B-roll 素材（图片则静止 + 深色底，视频则裁剪铺满），
  段与段之间用 **xfade 交叉淡化** 转场串联；可选叠加 **花字条(lower-third)**。
- 音频：只用主讲人旁白音贯穿全片（B-roll 自带音丢弃），保证口播连续。
- 输出竖屏 mp4（不含字幕；字幕由 compose 后续烧录，复用既有流程）。
- 任何失败（素材为空/ffmpeg 异常）由调用方回退到纯主讲人，保证出片。
"""

import subprocess
from pathlib import Path

from video_pipeline.compose import select_video_encoder

_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _is_image(p):
    return Path(p).suffix.lower() in _IMG_EXT


def build_image_anchor(portrait_path, audio_path, out_path, duration, *,
                       width=1080, height=1920, fps=30, bg_color="0x10131A"):
    """把静态肖像(或渐变兜底) + 音频 合成一段锚层视频，供 edit_with_broll 使用。

    当没有真·说话脸（剪映/SadTalker）时，用肖像循环作为"主讲人"层，
    剪接时 B-roll 会替换掉部分片段，仍比纯静态更自然。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    has_portrait = bool(portrait_path) and Path(portrait_path).exists()
    if has_portrait:
        src = ["-loop", "1", "-i", str(portrait_path)]
    else:
        src = ["-f", "lavfi", "-i", f"gradients=c0={bg_color}:c1=0x000000:"
               f"x0=0:y0=0:x1=0:y1=1:s={width}x{height}:r={fps}"]
    cmd = [
        "ffmpeg", "-y", *src, "-i", str(audio_path),
        "-filter_complex",
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={bg_color},"
        f"setsar=1,format=yuv420p[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", _venc(), "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-r", str(fps),
        "-t", f"{duration:.3f}", "-shortest", str(out_path),
    ]
    rc, out, err = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"锚层视频合成失败 (rc={rc}):\n{(err or out)[-1500:]}")
    return str(out_path)


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


_VENC = None


def _venc():
    global _VENC
    if _VENC is None:
        _VENC = select_video_encoder()
    return _VENC


def _escape_text(t):
    return t.replace("\\", "\\\\").replace("'", "\\'")


def edit_with_broll(
    anchor_path, audio_path, segments, footage_pool, out_path,
    *, width=1080, height=1920, fps=30, transition=0.3,
    lower_third=None, fontfile=None, broll_every=2, keep_bookends=True,
):
    """按句时间轴把 B-roll 切入主讲人视频，转场串联，输出竖屏 mp4。

    设计（纪录片式「主讲人 + 素材切剪」）：
    - 主讲人(anchor)音频贯穿全片，保证口播连续；
    - 视频按句切成 n 段，部分段用 B-roll 素材（图片则静止 + 深色底；视频则裁剪铺满），
      其余段仍显示主讲人（首尾默认保留为主讲人，避免「人凭空消失」）；
    - 段与段之间 xfade 交叉淡化转场；可选叠加底部花字条(lower-third)。
    - 音频只取 anchor 音轨，B-roll 自带音丢弃。

    Args:
        anchor_path: 主讲人视频（含旁白音；剪映数字人 / 本地锚层 / 说话脸）。
        audio_path: 旁白音路径（仅作校验，实际音频取 anchor 音轨）。
        segments: 句时间轴 list[{start,end,text}]（秒）。
        footage_pool: 素材池 list[{path,is_image,duration}]。
        out_path: 输出 mp4。
        transition: xfade 转场时长（秒）。
        lower_third: 可选花字条文本（如"老六说球"）。
        fontfile: 可选 drawtext 字体文件路径。
        broll_every: 每隔几段用一次 B-roll（2=每两段用一次素材）。
        keep_bookends: 保留首尾段为主讲人（默认 True）。
    Returns:
        str: out_path
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 解析句时间轴：(start, end, dur)
    segs = []
    for s in segments:
        st = float(s["start"]); en = float(s["end"])
        if en > st:
            segs.append((st, en, en - st))
    if not segs:
        raise RuntimeError("segments 为空或无效")
    n = len(segs)
    pool = list(footage_pool or [])
    if not pool:
        raise RuntimeError("素材池为空，回退纯主讲人")

    # 决定哪些段用 B-roll（主讲人 + 素材交替，首尾默认保留主讲人）
    use_broll = [False] * n
    mid = (broll_every // 2) if broll_every > 1 else 1
    for i in range(n):
        if keep_bookends and (i == 0 or i == n - 1):
            use_broll[i] = False
        else:
            use_broll[i] = (i % broll_every) == mid
    if not any(use_broll):  # 兜底：若全 False，强制隔段用素材
        for i in range(1, n, 2):
            if not (keep_bookends and i == n - 1):
                use_broll[i] = True

    # 收集需要用的 B-roll 输入（每用一次素材占一个输入文件，按 pool 轮询）
    inputs = []
    clip_idx = {}   # segment i -> 输入文件序号（仅 B-roll 段）
    n_inputs = 0    # 已加入的输入文件数（与 inputs 参数列表长度无关）
    for i in range(n):
        if use_broll[i]:
            clip = pool[len(clip_idx) % len(pool)]["path"]
            if _is_image(clip):
                inputs += ["-loop", "1", "-i", str(clip)]
            else:
                inputs += ["-stream_loop", "-1", "-i", str(clip)]
            clip_idx[i] = n_inputs
            n_inputs += 1
    anchor_idx = n_inputs
    inputs += ["-i", str(anchor_path)]

    W, H = width, height
    sfilters = []
    prev_cum = 0.0
    for i, (st, en, d) in enumerate(segs):
        if use_broll[i]:
            sfilters.append(
                f"[{clip_idx[i]}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x10131A,setsar=1,"
                f"trim=duration={d:.3f},setpts=PTS-STARTPTS[sv{i}]")
        else:
            # 主讲人段：从 anchor 视频按时间轴裁出对应口播段（保证嘴型/口播同步）
            sfilters.append(
                f"[{anchor_idx}:v]trim=start={prev_cum:.3f}:duration={d:.3f},"
                f"setpts=PTS-STARTPTS,scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x10131A,setsar=1[sv{i}]")
        prev_cum += d

    # xfade 串联（段时长可变）：第 i 次转场 offset = 前 i 段时长之和 - i×转场时长
    # （xfade 要求首个输入长度 ≥ offset + 转场时长，此公式恰好满足，避免裁掉前面内容）
    vf = ";".join(sfilters)
    if n == 1:
        vf += ";[sv0]format=yuv420p[vout]"
    else:
        prev = "sv0"
        cum = 0.0
        for i in range(1, n):
            cum += segs[i - 1][2]
            off = cum - i * transition
            nxt = f"v{i}" if i < n - 1 else "vout"
            vf += (f";[{prev}][sv{i}]xfade=transition=fade:duration={transition:.3f}"
                   f":offset={off:.3f}[{nxt}]")
            prev = nxt
        vf += ";[vout]format=yuv420p[vout]"

    # 花字条（可选）
    if lower_third:
        txt = _escape_text(lower_third)
        ff = f"fontfile='{fontfile}'" if fontfile else ""
        vf += (f";[vout]drawtext={ff}text='{txt}':fontcolor=white:fontsize=46:"
               f"box=1:boxcolor=black@0.5:boxborderw=12:x=(w-tw)/2:y=h-th-90[vout]")

    total = sum(d for _, _, d in segs) - (n - 1) * transition
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", vf,
        "-map", "[vout]",
        "-map", f"{anchor_idx}:a",
        "-c:v", _venc(), "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-r", str(fps), "-t", f"{total:.3f}", "-shortest",
        "-movflags", "+faststart", str(out_path),
    ]
    rc, out, err = _run(cmd)
    if rc != 0:
        snippet = (err or out)[-2000:]
        raise RuntimeError(f"ffmpeg 剪接失败 (rc={rc}):\n{snippet}")
    return str(out_path)
