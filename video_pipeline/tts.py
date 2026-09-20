#!/usr/bin/env python3
"""TTS 语音合成（Phase 1）—— Edge TTS（免 key，免费），输出音频 + 字幕时间轴。

- 默认 zh-CN-YunxiNeural（云希·男声·沉稳专业）；云健/晓睿作降级备选。
- 用 SentenceBoundary 事件喂 edge_tts.SubMaker 直接产出可读句级 SRT（适配手机竖屏）。
- 预留 synthesize_clone()（GPT-SoVITS），由 config.clone.enabled 控制，默认关闭。
- volcanic/火山引擎分支预留接口（config.volcano.enabled）。
"""

import asyncio
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


async def _synthesize_one(text, voice, audio_path, srt_path, rate, volume, pitch):
    communicate = edge_tts.Communicate(
        text, voice, rate=rate, volume=volume, pitch=pitch, boundary="SentenceBoundary"
    )
    submaker = edge_tts.SubMaker()
    with open(audio_path, "wb") as af:
        async for event in communicate.stream():
            if event["type"] == "audio":
                af.write(event["data"])
            elif event["type"] in ("WordBoundary", "SentenceBoundary"):
                submaker.feed(event)
    srt = submaker.get_srt()
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
