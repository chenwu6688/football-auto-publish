#!/usr/bin/env python3
"""video_pipeline 单元测试（沙箱可测部分，无需网络 / API key）。

覆盖：配置加载、口播稿（LLM 注入 + 规则兜底）、声线解析、字幕折行、
ffmpeg 合成端到端（肖像 Ken-Burns + 无肖像渐变兜底）、管线编排（mock TTS）。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from video_pipeline import (
    script_gen, tts, subtitles, compose, pipeline,
)

_SAMPLE_ARTICLE = {
    "title": "皇马更衣室炸了？贝林厄姆和主帅当场互喷",
    "content": (
        "昨夜伯纳乌的更衣室，据说比比分牌还热闹。据多家西媒透露，皇马在输球之后，"
        "贝林厄姆和主帅之间爆发了激烈争执，矛盾点集中在中场调度和换人时机上。"
        "这不是第一次了，本赛季皇马的更衣室气氛一直紧绷。"
    ),
    "resonance_angle": "名帅名宿沉浮",
    "series_id": "old-fan-night",
}


def _fake_llm_script():
    return ("老球迷们，今天这条你一定得看——皇马更衣室炸了，贝林厄姆和主帅当场互喷，"
            "矛盾点就在中场调度和换人时机。本赛季皇马更衣室气氛一直紧绷，当更衣室开始漏风，"
            "战绩往往跟着掉。当年那支所向披靡的皇马，靠的是拧成一股绳。如今新星崛起、老将退场，"
            "权力结构正在重写。你觉得这波内耗会让皇马掉队吗？评论区聊聊，关注老六每天球评不断更。")


def _fake_llm(messages):
    return {"title": "测试标题", "script": _fake_llm_script(),
            "hook_type": "冲突", "estimated_duration_sec": 60}, "fake-model"


# ---------------------------------------------------------------- 配置
def test_load_video_config_default():
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    assert cfg["voice"]["default"] == "zh-CN-YunxiNeural"
    assert cfg["video"]["width"] == 1080 and cfg["video"]["height"] == 1920
    assert cfg["output"]["base_dir"] == "output/videos"


def test_load_video_config_missing_returns_fallback():
    cfg = pipeline.load_video_config(_ROOT / "nonexistent_config.yaml")
    assert "voice" in cfg and "video" in cfg and "output" in cfg


# ---------------------------------------------------------------- 声线
def test_resolve_voice_primary_first():
    v = tts.resolve_voice({"default": "zh-CN-YunxiNeural",
                           "fallback_order": ["zh-CN-YunjianNeural", "zh-CN-YunyangNeural"]})
    assert v[0] == "zh-CN-YunxiNeural"
    assert len(v) == 3
    assert len(set(v)) == 3  # 去重


def test_resolve_voice_empty():
    v = tts.resolve_voice({})
    assert v  # 有兜底


# ---------------------------------------------------------------- 口播稿
def test_generate_script_llm():
    r = script_gen.generate_script(_SAMPLE_ARTICLE, llm_fn=_fake_llm)
    assert r["source"] == "llm"
    assert r["model"] == "fake-model"
    assert 120 <= len(r["script"]) <= 540


def test_generate_script_fallback_on_exception():
    def boom(messages):
        raise RuntimeError("llm unavailable")
    r = script_gen.generate_script(_SAMPLE_ARTICLE, llm_fn=boom)
    assert r["source"] == "fallback"
    assert r["title"] and r["script"]


def test_generate_script_fallback_on_invalid_json():
    def bad(messages):
        return {"script": "太短"}, "m"  # 缺 title 且 script 过短
    r = script_gen.generate_script(_SAMPLE_ARTICLE, llm_fn=bad)
    assert r["source"] == "fallback"


def test_condense_fallback_long_content():
    art = {"title": "标题", "content": "。".join(["这是一句较长的内容用来测试规则兜底是否能够正确截取并生成可用的口播稿"] * 20)}
    r = script_gen.condense_fallback(art)
    assert r["source"] == "fallback"
    assert r["title"] == "标题"
    assert r["script"]
    # 结尾应带互动钩子
    assert "评论" in r["script"] or "关注" in r["script"]


# ---------------------------------------------------------------- 字幕
def test_ticks_to_seconds():
    assert subtitles.ticks_to_seconds(10_000_000) == 1.0
    assert subtitles.ticks_to_seconds(0) == 0.0


def test_wrap_srt_lines():
    srt = ("1\n00:00:00,000 --> 00:00:02,000\n这是第一句话内容比较长需要折行处理一下看看效果如何。\n\n"
           "2\n00:00:02,000 --> 00:00:04,000\n第二句较短。\n")
    out = subtitles.wrap_srt_lines(srt, max_chars=12)
    # 长句被折成多行
    first_block = out.split("\n\n")[0]
    lines = first_block.split("\n")
    # 时间轴 + 至少 2 行内容
    content_lines = [l for l in lines if not l.strip().isdigit()
                     and "-->" not in l]
    assert len(content_lines) >= 2
    assert all(len(l) <= 12 for l in content_lines)


def test_wrap_srt_preserves_timecodes():
    srt = "1\n00:00:01,000 --> 00:00:03,500\n短句。\n"
    out = subtitles.wrap_srt_lines(srt, max_chars=12)
    assert "00:00:01,000 --> 00:00:03,500" in out


def test_srt_to_ass_playres_and_style():
    srt = "1\n00:00:01,000 --> 00:00:03,500\n老球迷们，今天这条你一定得看。\n"
    ass = subtitles.srt_to_ass(
        srt, width=1080, height=1920, font_size=46,
        primary_color="0xFFFFFF", outline_color="0x000000",
        back_color="0x80000000", outline=4, margin_v=140)
    # PlayRes 必须等于视频尺寸（否则字号会被按 288 缩放，重现"巨字盖脸"）
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    # ASS 颜色为 BGR 序：白 0xFFFFFF → &H00FFFFFF，黑 → &H00000000
    assert "&H00FFFFFF" in ass and "&H00000000" in ass
    # 字号/边距按真实像素写入样式行
    assert ",46," in ass
    assert "60,60,140,1" in ass
    # 时间换算：SRT 毫秒 → ASS 厘秒
    assert "Dialogue: 0,0:00:01.00,0:00:03.50,Default" in ass


def test_ass_color_bgr_order():
    assert subtitles._ass_color("0x0000FF") == "&H00FF0000"   # 蓝
    assert subtitles._ass_color("0xFF0000") == "&H000000FF"   # 红
    assert subtitles._ass_color("0xFFFFFF") == "&H00FFFFFF"   # 白
    assert subtitles._ass_color("0x80000000") == "&H80000000"  # 带 alpha


# ---------------------------------------------------------------- 合成端到端
def _make_audio(path, duration=3):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
         "-c:a", "pcm_s16le", str(path)],
        capture_output=True, check=True,
    )


def _make_portrait(path):
    from PIL import Image
    Image.new("RGB", (800, 1200), (30, 40, 60)).save(str(path))


def _make_srt(path):
    path.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n老球迷们，今天这条你一定得看。\n\n"
        "2\n00:00:02,000 --> 00:00:03,500\n皇马更衣室炸了，评论区聊聊。\n",
        encoding="utf-8",
    )


def test_compose_with_portrait(tmp_path):
    wav = tmp_path / "a.wav"; srt = tmp_path / "a.srt"; png = tmp_path / "p.png"
    mp4 = tmp_path / "v.mp4"
    _make_audio(wav); _make_portrait(png); _make_srt(srt)
    out, info = compose.compose_video(str(png), str(wav), str(srt), str(mp4), ken_burns=True)
    assert info["ok"] and info["has_video"] and info["has_audio"]
    assert info["width"] == 1080 and info["height"] == 1920
    assert abs(info["duration"] - 3.0) < 1.0


def test_compose_without_portrait_fallback(tmp_path):
    wav = tmp_path / "a.wav"; srt = tmp_path / "a.srt"
    mp4 = tmp_path / "v.mp4"
    _make_audio(wav); _make_srt(srt)
    out, info = compose.compose_video("", str(wav), str(srt), str(mp4), ken_burns=True)
    assert info["ok"] and info["has_video"] and info["has_audio"]
    assert info["width"] == 1080 and info["height"] == 1920


# ---------------------------------------------------------------- 管线编排（mock TTS）
def _fake_synthesize(text, *, voice, audio_path, srt_path, rate="+0%", volume="+0%",
                     pitch="+0Hz", fallback_voices=None):
    _make_audio(audio_path, duration=4)
    Path(srt_path).write_text(
        "1\n00:00:00,000 --> 00:00:04,000\n" + text[:30] + "\n", encoding="utf-8")
    return Path(audio_path), Path(srt_path), voice


def test_run_pipeline_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    cfg["output"]["keep_intermediate"] = True
    meta = pipeline.run_pipeline(_SAMPLE_ARTICLE, config=cfg, out_dir=tmp_path,
                                 llm_fn=_fake_llm)
    assert meta["source_id"] == _SAMPLE_ARTICLE["source_id"] if "source_id" in _SAMPLE_ARTICLE else True
    assert Path(meta["video_path"]).exists()
    assert meta["voice"] == "zh-CN-YunxiNeural"
    assert meta["resolution"] == "1080x1920"
    assert meta["actual_duration_sec"] > 0
    # 元数据落盘
    assert Path(meta["video_path"]).with_suffix(".meta.json").exists()


def test_run_pipeline_without_intermediate_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    cfg["output"]["keep_intermediate"] = False
    meta = pipeline.run_pipeline(_SAMPLE_ARTICLE, config=cfg, out_dir=tmp_path,
                                 llm_fn=_fake_llm)
    # 中间产物被清理
    assert not meta.get("audio_path") and not meta.get("srt_path")
    assert Path(meta["video_path"]).exists()
