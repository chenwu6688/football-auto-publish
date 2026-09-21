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

import re
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
        shortest = True
    else:
        # 无肖像 → 渐变兜底。注意：lavfi 源必须显式给 d=<时长>，
        # 且**不能用 -shortest**：实测 -shortest 会让 lavfi 无限源在约 1.9s 处
        # 提前结束，导致锚层时长远短于旁白（整片被截短）。改用源时长 + -t 精确控制。
        src = ["-f", "lavfi", "-i", f"gradients=c0={bg_color}:c1=0x000000:"
               f"x0=0:y0=0:x1=0:y1=1:s={width}x{height}:r={fps}:d={duration:.3f}"]
        shortest = False
    cmd = [
        "ffmpeg", "-y", *src, "-i", str(audio_path),
        "-filter_complex",
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={bg_color},"
        f"setsar=1,format=yuv420p[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", _venc(), "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-r", str(fps),
        "-t", f"{duration:.3f}",
    ]
    if shortest:  # 仅肖像（-loop 1 无限）时保留 -shortest，确保与音频同长
        cmd.append("-shortest")
    cmd.append(str(out_path))
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


def _probe_audio_duration(anchor_path):
    """探测 anchor（成片音轨来源，-map {anchor_idx}:a）里的音频时长（拿不到则返回 0）。

    用途：把成片时长对齐到「音频真实长度」，防止末句口播被 -t 截断。
    优先取音频流自身 duration；拿不到（部分容器不写）回退容器总时长。
    """
    p = str(anchor_path)

    def _ffprobe(args):
        try:
            proc = subprocess.run(["ffprobe", "-v", "error", *args, p],
                                  capture_output=True, text=True)
            return float(proc.stdout.strip() or 0)
        except Exception:
            return 0.0

    dur = _ffprobe(["-select_streams", "a:0", "-show_entries", "stream=duration",
                    "-of", "default=nw=1:nk=1"])
    if not dur:
        dur = _ffprobe(["-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1"])
    return dur or 0.0


def edit_with_broll(
    anchor_path, audio_path, segments, footage_pool, out_path,
    *, width=1080, height=1920, fps=30, transition=0.3,
    lower_third=None, fontfile=None, broll_every=2, keep_bookends=True,
    intro_broll=None, intro_map=None,
):
    """按句时间轴把 B-roll 切入主讲人视频，转场串联，输出竖屏 mp4。

    设计（纪录片式「主讲人 + 素材切剪」）：
    - 主讲人(anchor)音频贯穿全片，保证口播连续；
    - 视频按句切成 n 段，部分段用 B-roll 素材（图片则静止 + 深色底；视频则裁剪铺满），
      其余段仍显示主讲人（首尾默认保留为主讲人，避免「人凭空消失」）；
    - 开场可强制前 N 段为 B-roll（intro_broll=球队标识图），用来「去掉真人出镜、
      改放双方球队标识」，口播继续；
    - 段与段之间 xfade 交叉淡化转场；可选叠加底部花字条(lower-third)。
    - 音频只取 anchor 音轨，B-roll 自带音丢弃。

    Args:
        anchor_path: 主讲人视频（含旁白音；剪映数字人 / 本地锚层 / 说话脸）。
        audio_path: 旁白音路径（仅作校验，实际音频取 anchor 音轨）。
        segments: 句时间轴 list[{start,end,text}]（秒）。
        footage_pool: 素材池 list[{path,is_image,duration}]（仅用于非开场 B-roll 段）。
        out_path: 输出 mp4。
        transition: xfade 转场时长（秒）。
        lower_third: 可选花字条文本（如"老六说球"）。
        fontfile: 可选 drawtext 字体文件路径。
        broll_every: 每隔几段用一次 B-roll（2=每两段用一次素材）。
        keep_bookends: 保留首尾段为主讲人（默认 True，但会被 intro_broll 覆盖）。
        intro_broll: 可选，开场强制 B-roll 的图片路径列表（球队标识）。
            前 len(intro_broll) 段会被强制设为 B-roll 并轮流使用这些图，
            覆盖 keep_bookends 的开场真人约束（实现「开场去真人→放球队标识」）。
        intro_map: 可选，{段下标: 图片路径}，把指定队标**精确放到指定段**
            （用于「讲到哪支队就显示哪支队标」，由 teams.align_teams_to_segments 生成）。
            提供时优先于 intro_broll 的顺序铺法。
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
    intro_imgs = [str(p) for p in (intro_broll or [])]
    # intro_map：{段下标: 图片路径}（精确放置，优先级高于 intro_broll 顺序铺）
    intro_by_seg = {}
    if intro_map:
        for k, v in intro_map.items():
            try:
                seg_k = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= seg_k < n and v:
                intro_by_seg[seg_k] = str(v)
    # 向后兼容：没有 intro_map 时，退回「前 len(intro_imgs) 段」的旧行为
    if not intro_by_seg and intro_imgs:
        for i in range(min(len(intro_imgs), n)):
            intro_by_seg[i] = intro_imgs[i]
    intro_set = set(intro_by_seg.keys())

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

    # 开场球队标识：强制这些段为 B-roll（覆盖 keep_bookends 的开场真人约束）
    for i in intro_set:
        use_broll[i] = True

    # 素材池为空时优雅降级：只保留「开场标识」这些 B-roll 段，其余段回到主讲人，
    # 避免因缺 footage 素材而整片回退（开场标识图单独也能出片）。
    if not pool:
        for i in range(n):
            if i not in intro_set:
                use_broll[i] = False
        if not any(use_broll):
            raise RuntimeError("素材池为空且无开场标识，回退纯主讲人")

    # 收集需要用的 B-roll 输入（每用一次素材占一个输入文件，按池轮询）
    inputs = []
    clip_idx = {}   # segment i -> 输入文件序号（仅 B-roll 段）
    n_inputs = 0    # 已加入的输入文件数（与 inputs 参数列表长度无关）
    pool_pos = 0    # footage 素材池轮询指针（仅非开场段使用）
    for i in range(n):
        if not use_broll[i]:
            continue
        if i in intro_by_seg:
            clip = intro_by_seg[i]          # 该段对应的球队标识图（精确放置）
        else:
            if not pool:
                continue                    # 已被 has_footage_broll 拦截，兜底跳过
            clip = pool[pool_pos % len(pool)]["path"]
            pool_pos += 1
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
        # 关键修复：每段统一 fps + settb，强制所有段（25fps 实拍 / 30fps 合成锚层）
        # 使用同一帧率与时间基，否则 xfade 会因时间基不一致而崩溃。
        tb = f"fps={fps},settb=AVTB"
        if use_broll[i]:
            if i in intro_by_seg:
                # 球队标识：白底衬 + 居中留白（避免拉满屏）+ 提亮，
                # 让队标在黑底视频里清晰醒目（透明底 PNG 叠深色底会显得发暗）。
                # 注：此 ffmpeg 构建无 eq 滤镜，改用 colorlevels 提亮。
                sfilters.append(
                    f"[{clip_idx[i]}:v]scale={int(W*0.72)}:{int(H*0.72)}"
                    f":force_original_aspect_ratio=decrease,"
                    f"colorlevels=rimin=0.02:gimin=0.02:bimin=0.02,"
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1,"
                    f"trim=duration={d:.3f},setpts=PTS-STARTPTS,{tb}[sv{i}]")
            else:
                sfilters.append(
                    f"[{clip_idx[i]}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x10131A,setsar=1,"
                    f"trim=duration={d:.3f},setpts=PTS-STARTPTS,{tb}[sv{i}]")
        else:
            # 主讲人段：从 anchor 视频按时间轴裁出对应口播段（保证嘴型/口播同步）
            sfilters.append(
                f"[{anchor_idx}:v]trim=start={prev_cum:.3f}:duration={d:.3f},"
                f"setpts=PTS-STARTPTS,scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x10131A,setsar=1,{tb}[sv{i}]")
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

    # 成片时长取「视觉总长」与「音频时长」的较大者：
    #   各段之和 - 转场重叠 = 画面实际长度；但音频（旁白/BGM混音）可能比它长，
    #   若只按画面长度 -t 截断，末句口播会被切掉（实测 61.56s 音频被截成 58.27s）。
    # 解决：垫满到音频长度（tpad 冻结末帧），保证「说完最后一句」。
    # 注意：必须先改标签再回写，不能 `[vout]...[vout]`（同标签既读又写，ffmpeg 不认）。
    visual_total = sum(d for _, _, d in segs) - (n - 1) * transition
    audio_total = _probe_audio_duration(anchor_path)
    total = max(visual_total, audio_total) if audio_total else visual_total
    if audio_total and audio_total > visual_total + 0.05:
        pad = audio_total - visual_total + 0.2
        # 只把「链路末尾」的 [vout] 改名成 [vpre]，再垫帧回 [vout]。
        # 不能整串替换 `[vout]`（xfade 的读端也叫 [vout]，改错会断链）；
        # 用 rsplit 精确定位最后一次出现。
        head, sep, tail = vf.rpartition("[vout]")
        if sep:
            vf = head + "[vpre]" + tail
        vf += f";[vpre]tpad=stop_mode=clone:stop_duration={pad:.3f},format=yuv420p[vout]"
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", vf,
        "-map", "[vout]",
        "-map", f"{anchor_idx}:a",
        "-c:v", _venc(), "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        # 用显式 -t 精确控制成片时长（= 各段之和 - 转场重叠）。
        # 不要加 -shortest：B-roll 用 -stream_loop -1、锚层音频可能长短不一，
        # -shortest 会取到最短流而把整片截短（曾把 5.2s 截成 1.9s）。
        "-r", str(fps), "-t", f"{total:.3f}",
        "-movflags", "+faststart", str(out_path),
    ]
    rc, out, err = _run(cmd)
    if rc != 0:
        snippet = (err or out)[-2000:]
        raise RuntimeError(f"ffmpeg 剪接失败 (rc={rc}):\n{snippet}")
    return str(out_path)
