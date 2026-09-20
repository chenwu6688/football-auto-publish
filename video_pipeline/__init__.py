"""数字人口播视频管线（Phase 0+1 MVP）。

文章 → 口播稿(script_gen) → TTS(tts) → 字幕(subtitles) → 竖屏视频(compose) → 元数据。

独立于现有图文发布链路，复用 orchestrator 的品牌手册 / LLM 额度，零新增成本。
"""

from .script_gen import generate_script, condense_fallback
from .tts import synthesize, resolve_voice, VOICE_PRESETS
from .subtitles import wrap_srt_lines, postprocess, ticks_to_seconds
from .compose import compose_video, verify_video, ffprobe_duration
from .clone import synthesize_clone_impl, split_sentences, build_proportional_srt, CloneUnavailable
from .talking_head import (
    generate_talking_head, generate_sadtalker, generate_wav2lip, TalkingHeadUnavailable,
)
from .pipeline import run_pipeline, load_video_config

__all__ = [
    "generate_script", "condense_fallback",
    "synthesize", "resolve_voice", "VOICE_PRESETS",
    "wrap_srt_lines", "postprocess", "ticks_to_seconds",
    "compose_video", "verify_video", "ffprobe_duration",
    "synthesize_clone_impl", "split_sentences", "build_proportional_srt", "CloneUnavailable",
    "generate_talking_head", "generate_sadtalker", "generate_wav2lip", "TalkingHeadUnavailable",
    "run_pipeline", "load_video_config",
]
