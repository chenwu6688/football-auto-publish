#!/usr/bin/env python3
"""TTS 语音合成（Phase 1）—— Edge TTS（免 key，免费），输出音频 + 字幕时间轴。

- 默认 zh-CN-YunxiNeural（云希·沉稳）；云健/晓睿作降级备选。
- **boundary="WordBoundary"**：一次请求同时拿
  ① 词级时间轴（.words.json，供纯文字动效做「逐字点亮跟语音」）；
  ② 句级 SRT（由词序列 + 原文标点自行切句合成，断句更可控，下游无感知）。
  （服务端 boundary 二选一；神经合成同参数确定性输出，词轴即真实发音时刻。）
- 预留 synthesize_clone()（GPT-SoVITS），由 config.clone.enabled 控制，默认关闭。
- volcanic/火山引擎分支预留接口（config.volcano.enabled）。
"""

import asyncio
import json
from pathlib import Path

import edge_tts

# 友好名 → Edge TTS 音色 ID（均为中文男声，免费）
VOICE_PRESETS = {
    "yunxi": "zh-CN-YunxiNeural",     # 云希（默认·沉稳）
    "yunjian": "zh-CN-YunjianNeural", # 云健（激情张力·体育吐槽）
    "yunyang": "zh-CN-YunyangNeural", # 晓睿（男声备选）
}

DEFAULT_VOICE = "zh-CN-YunxiNeural"
DEFAULT_FALLBACK = ["zh-CN-YunxiNeural", "zh-CN-YunjianNeural", "zh-CN-YunyangNeural"]


def resolve_voice(voice_cfg):
    """从配置解析实际使用的音色列表（主 + 降级）。"""
    if not isinstance(voice_cfg, dict):
        return list(DEFAULT_FALLBACK)
    primary = voice_cfg.get("default") or DEFAULT_VOICE
    order = voice_cfg.get("fallback_order") or []
    merged = [primary] + [v for v in order if v != primary]
    if not merged:
        merged = list(DEFAULT_FALLBACK)
    # 去重保序
    seen, out = set(), []
    for v in merged:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# 句读标点（切句用）
_SENT_SPLIT = "，。！？；、：…—,.!?;:"


def _ticks_to_sec(ticks):
    """edge-tts 的 100ns tick → 秒。"""
    try:
        return int(ticks) / 1e7
    except (TypeError, ValueError):
        return 0.0


def _plain_units(s):
    """有效发音字符序列（去标点/空白）—— 对齐词时间轴的基准。"""
    return [c for c in (s or "") if c.strip() and c not in _SENT_SPLIT]


def srt_from_words(text, words, *, max_chars=20, gap=0.5):
    """由词级时间轴 + 原文标点，合成句级 SRT。

    做法：原文按句读标点切成候选句 → 每句的「发音字符数」占比，
    在词字符流上按比例映射出该句的词区间 → 句窗 = 首词 start → 末词 end
    （末尾留 gap 秒，且不越过下一句起点）。发音字符流与词字符流长度一致时
    即精确对齐（大多数中文场景），不一致（数字被规范化等）时比例映射兜底。

    Args:
        text: 原始口播稿（含标点）。
        words: [{"start","end","text"}]（秒）。
        max_chars: 单句最大发音字符数（超长按比例再切）。
        gap: 句尾留白秒数。
    Returns:
        str: SRT 文本。
    """
    def _fmt(sec):
        sec = max(0.0, float(sec))
        h, m = int(sec // 3600), int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = min(999, int(round((sec - int(sec)) * 1000)))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    # 1) 原文按标点切候选句 → 按最大发音字符数再切成「发音字符分片」
    pieces, buf = [], ""
    for ch in (text or ""):
        buf += ch
        if ch in _SENT_SPLIT:
            if _plain_units(buf):
                pieces.append(buf)
            buf = ""
    if _plain_units(buf):
        pieces.append(buf)
    if not pieces:
        return ""
    frags = []          # 每项 = 一句的发音字符列表（超长句已切分）
    for ptxt in pieces:
        chars = _plain_units(ptxt)
        for i in range(0, len(chars), max_chars):
            frags.append(chars[i:i + max_chars])

    # 2) 词字符流（词文本去标点展开）
    wchars = []         # [(char, word_idx)]
    for wi, w in enumerate(words):
        for c in _plain_units(w.get("text") or ""):
            wchars.append((c, wi))
    n_total = sum(len(f) for f in frags)
    m_total = len(wchars)
    if n_total == 0 or m_total == 0:
        return ""

    # 3) 每个分片按字符占比映射到词区间 → 句窗 = 首词 start → 末词 end
    out, p = [], 0
    for frag in frags:
        q0 = int(round(p * m_total / n_total))
        p += len(frag)
        q1 = int(round(p * m_total / n_total))
        q1 = max(q1, min(q0 + 1, m_total))       # 至少吃 1 个词字符
        idxs = [wi for _c, wi in wchars[q0:q1]]
        if not idxs:
            continue
        out.append([float(words[idxs[0]]["start"]),
                    float(words[idxs[-1]]["end"]),
                    "".join(frag)])

    # 4) 句尾留白 + 不越过下一句起点
    for i, item in enumerate(out):
        nxt = out[i + 1][0] if i + 1 < len(out) else item[1] + gap
        item[1] = max(item[1], min(item[1] + gap, nxt - 0.01))
    lines = []
    for i, (st, en, txt) in enumerate(out, start=1):
        lines.append(f"{i}\n{_fmt(st)} --> {_fmt(en)}\n{txt.strip()}\n")
    return "\n".join(lines)


async def _synthesize_one(text, voice, audio_path, srt_path, rate, volume, pitch):
    """合成音频 + 词级时间轴（.words.json）+ 句级 SRT（词轴+原文标点自切句）。"""
    communicate = edge_tts.Communicate(
        text, voice, rate=rate, volume=volume, pitch=pitch, boundary="WordBoundary"
    )
    words = []
    with open(audio_path, "wb") as af:
        async for event in communicate.stream():
            if event["type"] == "audio":
                af.write(event["data"])
            elif event["type"] == "WordBoundary":
                st = _ticks_to_sec(event.get("offset"))
                du = _ticks_to_sec(event.get("duration"))
                if du > 0:
                    words.append({"start": st, "end": st + du,
                                  "text": event.get("text") or ""})
    words_path = Path(srt_path).with_suffix(".words.json")
    words_path.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    srt = srt_from_words(text, words)
    srt_path.write_text(srt, encoding="utf-8")
    return audio_path, srt_path


def synthesize(text, *, voice=DEFAULT_VOICE, audio_path, srt_path,
               rate="+0%", volume="+0%", pitch="+0Hz", fallback_voices=None):
    """同步合成：音频 + 句级 SRT。主音色失败依次降级到 fallback_voices。

    Args:
        text: 口播稿文本。
        voice: 主音色 ID。
        audio_path / srt_path: pathlib.Path 输出路径（父目录会被创建）。
        rate/volume/pitch: Edge TTS 参数。
        fallback_voices: 备选音色 ID 列表。
    Returns:
        (audio_path, srt_path, used_voice)
    """
    audio_path = Path(audio_path)
    srt_path = Path(srt_path)
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.parent.mkdir(parents=True, exist_ok=True)

    voices = [voice] + [v for v in (fallback_voices or []) if v != voice]
    last_err = None
    for v in voices:
        try:
            asyncio.run(_synthesize_one(text, v, audio_path, srt_path,
                                        rate, volume, pitch))
            return audio_path, srt_path, v
        except Exception as e:
            last_err = e
            print(f"   ⚠️ TTS 音色 {v} 失败：{e}，尝试下一个")
    raise RuntimeError(f"所有 TTS 音色均失败：{last_err}")


def synthesize_clone(text, *, reference_audio, model_dir, audio_path, srt_path, **kwargs):
    """声线克隆分支（GPT-SoVITS）—— 委托 video_pipeline.clone 模块。

    任一环节失败会抛 clone.CloneUnavailable，由 pipeline 捕获并回退到 Edge TTS，
    保证出片不中断。config.clone.enabled 控制是否进入本分支。
    """
    from video_pipeline import clone
    return clone.synthesize_clone_impl(
        text, reference_audio=reference_audio, model_dir=model_dir or "",
        audio_path=audio_path, srt_path=srt_path, **kwargs,
    )


def synthesize_volcano(text, *, app_id, token, cluster, speaker, audio_path, srt_path, **kwargs):
    """火山引擎 TTS 分支（字节系·28 情感风格男声）—— 预留，默认未实现。

    接入时调用火山大模型语音合成 API（需 key）。当前抛 NotImplementedError。
    """
    raise NotImplementedError(
        "火山引擎 TTS 尚未接入：请在 config.volcano 配置 app_id/token/cluster/speaker 后，"
        "在此挂载火山语音合成；当前请保持 volcano.enabled=false 使用 Edge TTS。"
    )
