#!/usr/bin/env python3
"""音频混音（氛围增强）—— 旁白 + 背景音乐(BGM) + 关键词音效(SFX)。

设计：
- 输入：旁白音(narration_audio) + 字幕时间轴(srt_path) + 配置(cfg) + BGM 目录 + 音效目录。
- 处理：
  * BGM：取 bgm_dir 下第一条曲目，loop 到旁白时长，音量压低（默认 ≈0.18 / -18dB），
    置于旁白下层（不喧宾夺主）。
  * 音效(SFX)：读 SRT 逐句时间轴，对每句字幕做关键词匹配 → 在对应 start 时刻叠加
    sfx_dir 下对应音效（默认 ≈0.4 / -8dB），制造"进球欢呼/哨声/官宣"等情境音。
  * 合成：amix(normalize=0) 保留旁白主音量，末尾 alimiter 防削波（无 limiter 退化为 volume）。
- 优雅降级：无 bgm/sfx 文件、目录不存在、ffmpeg 失败 → 直接返回原旁白路径，绝不让出片中断。
- 任意异常被吞掉并返回原旁白，保证上游 pipeline 永远拿得到一条可用音频。
"""

import re
import subprocess
from pathlib import Path

from video_pipeline import compose
from video_pipeline import subtitles

_AUDIO_EXT = (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".mp4")

# 关键词 → 音效名 映射（可被 cfg["keyword_map"] 覆盖/合并）。
# 文本命中任一关键词即触发对应音效，叠加于该句字幕的 start 时刻。
DEFAULT_KEYWORD_MAP = {
    # 进球 / 高潮时刻 → 欢呼
    "cheer": ["进球", "绝杀", "破门", "扳平", "得分", "梅开二度", "帽子戏法",
              "世界波", "逆转", "反超", "扳回", "读秒"],
    # 判罚 / 冲突 → 哨声
    "whistle": ["红牌", "争议", "判罚", "点球", "犯规", "VAR", "黄牌", "越位", "误判"],
    # 转会 / 官宣 → 播报提示音
    "news": ["转会", "签约", "官宣", "续约", "加盟", "离队", "下课"],
    # 夺冠 / 胜利 → 队歌 / 胜利号角
    "anthem": ["夺冠", "胜利", "升级", "捧杯", "登顶", "夺魁", "问鼎", "加冕", "封王"],
    # 失误 / 崩盘 → 低落音
    "downer": ["失误", "乌龙", "送礼", "丢球", "崩盘", "惨败", "翻车", "哑火"],
    # 开场 / 引入 → whoosh 转场音
    "whoosh": ["开场", "引入", "先看", "首先", "话说回来", "咱们先"],
}

# 单次成片最多叠加多少个音效（防止极端长稿产生过多输入导致 amix 负载过高）
_MAX_SFX_OVERLAYS = 16

# ffmpeg -filters 结果缓存
_FILTER_CACHE = None


def _available_filters():
    global _FILTER_CACHE
    if _FILTER_CACHE is not None:
        return _FILTER_CACHE
    found = set()
    try:
        rc, out, _ = compose._run(["ffmpeg", "-hide_banner", "-filters"])
        if rc == 0:
            for line in out.splitlines():
                m = re.match(r"\s*\S+\s+(\w+)\s", line)
                if m:
                    found.add(m.group(1))
    except Exception:
        pass
    _FILTER_CACHE = found
    return found


def _has_filter(name):
    return name in _available_filters()


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _find_first_audio(directory):
    """返回目录下第一条音频文件路径；不存在/空目录返回 None。"""
    d = Path(directory)
    if not d.exists():
        return None
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix.lower() in _AUDIO_EXT:
            return str(p)
    return None


def _find_sfx_file(directory, sfx_name):
    """在 sfx_dir 里找与 sfx_name 匹配的音效文件（stem 相等或前缀匹配）。"""
    d = Path(directory)
    if not d.exists():
        return None
    name = sfx_name.lower()
    files = [p for p in sorted(d.iterdir())
             if p.is_file() and p.suffix.lower() in _AUDIO_EXT]
    # 1) 精确 stem 匹配（大小写不敏感）
    for p in files:
        if p.stem.lower() == name:
            return str(p)
    # 2) 前缀匹配（如 cheer_01.wav）
    for p in files:
        if p.stem.lower().startswith(name):
            return str(p)
    return None


def _build_keyword_map(cfg):
    """合并默认映射与配置覆盖。

    Args:
        cfg: audio 配置块；其 "keyword_map" 子项用于覆盖/扩展默认映射。
            也接受直接传入一张 {sfx: [kw,...]} 映射表（便于单测/复用）。
    """
    km = {k: list(v) for k, v in DEFAULT_KEYWORD_MAP.items()}
    cfg = cfg or {}
    # 兼容两种传法：{"keyword_map": {...}}（配置块）或直接 {...}（映射表本身）
    override = cfg.get("keyword_map") if "keyword_map" in cfg else cfg
    if isinstance(override, dict):
        for sfx, kws in override.items():
            if isinstance(kws, list):
                km[sfx] = list(kws)
    return km


def _detect_sfx_triggers(segments, keyword_map):
    """逐句字幕关键词匹配，返回 [(sfx_name, start_sec), ...]（按出现顺序去重）。"""
    triggers = []
    seen = set()
    for seg in segments:
        text = seg.get("text", "") or ""
        start = float(seg.get("start", 0) or 0)
        for sfx, kws in keyword_map.items():
            for kw in kws:
                if kw and kw in text:
                    key = (sfx, round(start, 3))
                    if key not in seen:
                        seen.add(key)
                        triggers.append((sfx, start))
                    break  # 同一音效对该句只触发一次
    return triggers


def mix(narration_audio, srt_path, cfg, bgm_dir, sfx_dir, out_path=None):
    """把旁白与 BGM / 关键词音效混成一条音频。

    Args:
        narration_audio: 旁白音路径（wav/mp3）。
        srt_path: 字幕 SRT（用于音效时间轴对齐）。
        cfg: audio 配置块（bgm_volume / sfx_volume / master_volume / bgm_fade_out / keyword_map）。
        bgm_dir: 背景音乐目录（取第一条曲目）。
        sfx_dir: 音效目录（按关键词映射找文件）。
        out_path: 输出 wav；None 则就近生成 <narration>.mixed.wav。
    Returns:
        str: 混音后路径；无内容可混或失败时返回原 narration_audio 路径（优雅降级）。
    """
    try:
        return _mix_impl(narration_audio, srt_path, cfg, bgm_dir, sfx_dir, out_path)
    except Exception as e:
        # 任何异常都降级为原旁白，保证出片不中断
        print(f"   ⚠️ 音频混音失败：{e}，使用原旁白音")
        return str(narration_audio)


def _mix_impl(narration_audio, srt_path, cfg, bgm_dir, sfx_dir, out_path):
    cfg = cfg or {}
    narration_audio = str(narration_audio)
    if not Path(narration_audio).exists():
        return narration_audio

    bgm_vol = float(cfg.get("bgm_volume", 0.18))
    sfx_vol = float(cfg.get("sfx_volume", 0.40))
    master_vol = float(cfg.get("master_volume", 1.0))
    fade_out = float(cfg.get("bgm_fade_out", 1.0))

    # 旁白时长（秒）
    D = compose.ffprobe_duration(narration_audio) or 60.0

    # BGM 文件
    bgm_file = _find_first_audio(bgm_dir)

    # 音效触发
    triggers = []
    if sfx_dir and Path(sfx_dir).exists():
        try:
            srt_text = Path(srt_path).read_text(encoding="utf-8")
            segs = subtitles.parse_segments(srt_text)
        except Exception:
            segs = []
        km = _build_keyword_map(cfg)
        raw = _detect_sfx_triggers(segs, km) if segs else []
        # 解析每条触发对应的音效文件；找不到文件的触发丢弃
        for sfx_name, start in raw:
            if len(triggers) >= _MAX_SFX_OVERLAYS:
                break
            f = _find_sfx_file(sfx_dir, sfx_name)
            if f:
                triggers.append((f, start))

    # 无任何可混内容 → 直接返回原旁白
    if not bgm_file and not triggers:
        return narration_audio

    if out_path is None:
        out_path = str(Path(narration_audio).with_suffix("")) + ".mixed.wav"
    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    # 输入： [0]=旁白  [1]=BGM(可选)  [2..]=音效
    inputs = ["-i", narration_audio]
    if bgm_file:
        # -stream_loop -1 让 BGM 可循环；滤镜内 atrim 截到旁白时长
        inputs += ["-stream_loop", "-1", "-i", bgm_file]

    # 每条音轨先单独处理，产出带标签的中间流；最后用一条 amix 链把
    # 所有标签 [n][b][s0]... 串起来（标签必须列在 amix 之前，否则会「unconnected」）。
    chains = []
    labels = []

    # 旁白：统一采样格式（主音量，保持原样），标 [n]
    chains.append("[0:a]aformat=sample_fmts=fltp[n]")
    labels.append("[n]")

    # BGM：压低 + 截到 D 秒 + 结尾淡出
    if bgm_file:
        fade_st = max(0.0, D - fade_out)
        chains.append(
            f"[1:a]volume={bgm_vol:.3f},atrim=0:{D:.3f},"
            f"afade=t=out:st={fade_st:.3f}:d={fade_out:.3f},"
            f"aformat=sample_fmts=fltp[b]")
        labels.append("[b]")

    # 音效：压低 + 延时到 start + 标 [s_i]
    for i, (f, start) in enumerate(triggers):
        inputs += ["-i", f]
        delay_ms = max(0, int(round(start * 1000)))
        # adelay all=1：同一延时应用到所有声道（无需关心声道数）
        chains.append(
            f"[{2 + i}:a]volume={sfx_vol:.3f},"
            f"adelay=delays={delay_ms}:all=1,"
            f"aformat=sample_fmts=fltp[s{i}]")
        labels.append(f"[s{i}]")

    n_inputs = len(labels)
    amix = f"amix=inputs={n_inputs}:duration=first:normalize=0"
    # 防削波：优先 alimiter，否则退化为整体压低
    if _has_filter("alimiter"):
        tail = f"{amix},{_limiter()}[out]"
    else:
        tail = f"{amix},volume={master_vol * 0.75:.3f}[out]"
    # 关键：把所有标签列在 amix 之前作为它的输入
    chains.append("".join(labels) + tail)

    vf = ";".join(chains)
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", vf,
        "-map", "[out]",
        # 输出真·PCM WAV：文件名后缀是 .wav，编码就必须是 pcm_s16le。
        # 若此处用 aac 却存成 .wav，ffmpeg 会按扩展名当 WAV 解析，读到的是 ADTS/AAC，
        # 触发 "Invalid data found" 并把音频截断到 ~1.9s（曾导致整片时长被砍）。
        "-c:a", "pcm_s16le",
        "-ar", "44100", str(out_path),
    ]
    rc, out, err = _run(cmd)
    if rc != 0:
        snippet = (err or out)[-1500:]
        raise RuntimeError(f"ffmpeg 混音失败 (rc={rc}):\n{snippet}")
    if not Path(out_path).exists():
        raise RuntimeError("混音输出文件不存在")
    return out_path


def _limiter():
    """limiter 滤镜串（lookahead，限制峰值到 0.99，避免叠加削波）。

    注意：新版 ffmpeg 的 alimiter `level` 为布尔（自动音量），不再接受数值；
    用 limit=0.99 + asc=1（自动电平缩放）即可安全压住叠加峰值。
    """
    return "alimiter=limit=0.99:asc=1"
