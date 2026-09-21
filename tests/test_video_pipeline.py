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
from video_pipeline.clone import CloneUnavailable, split_sentences, build_proportional_srt
from video_pipeline.talking_head import TalkingHeadUnavailable
from video_pipeline import footage as _footage
from video_pipeline import edit as _edit

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


# ---------------------------------------------------------------- 克隆/说话脸 回退 & 分句SRT（Phase 2）
def test_split_sentences_basic():
    s = split_sentences("老球迷们，今天这条你一定得看。皇马更衣室炸了，评论区聊聊！")
    assert s[0].endswith("。") and s[1].endswith("！")
    assert len(s) == 2


def test_build_proportional_srt_timing():
    srt = build_proportional_srt("第一句内容。第二句更长一些的内容。", duration=5.0)
    blocks = [b for b in srt.strip().split("\n\n") if b]
    assert len(blocks) == 2
    # 末句结束时间 ≈ 总时长
    last = blocks[-1].split("\n")[1]
    end = last.split(" --> ")[1]
    h, m, rest = end.split(":")
    sec = int(h) * 3600 + int(m) * 60 + float(rest.replace(",", "."))
    assert 4.5 <= sec <= 5.0


def _make_clip(path, size="320x320", dur=2):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=30",
         "-t", str(dur), "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, check=True,
    )


def test_compose_with_talking_head_video(tmp_path):
    wav = tmp_path / "a.wav"; srt = tmp_path / "a.srt"; th = tmp_path / "th.mp4"
    mp4 = tmp_path / "v.mp4"
    _make_audio(wav); _make_srt(srt); _make_clip(th)
    out, info = compose.compose_video("", str(wav), str(srt), str(mp4),
                                      talking_head_video=str(th))
    assert info["ok"] and info["has_video"] and info["has_audio"]
    assert info["width"] == 1080 and info["height"] == 1920


def test_pipeline_clone_fallback_to_edge(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    # reference_audio 留空 → synthesize_clone 必抛 CloneUnavailable，应回退 Edge
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    cfg["voice"]["provider"] = "clone"
    cfg["clone"]["enabled"] = True
    cfg["clone"]["reference_audio"] = ""  # 故意缺失，触发不可用
    meta = pipeline.run_pipeline(_SAMPLE_ARTICLE, config=cfg, out_dir=tmp_path,
                                 llm_fn=_fake_llm)
    assert meta["tts_engine"] == "edge"
    assert meta["voice_cloned"] is False
    assert Path(meta["video_path"]).exists()


def test_pipeline_talking_head_fallback_to_static(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    # sadtalker_dir 留空 → 说话脸不可用，应回退静态肖像（无肖像则渐变兜底）
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    cfg["talking_head"]["enabled"] = True
    cfg["talking_head"]["sadtalker_dir"] = ""  # 故意缺失
    meta = pipeline.run_pipeline(_SAMPLE_ARTICLE, config=cfg, out_dir=tmp_path,
                                 llm_fn=_fake_llm)
    assert meta["talking_head_used"] is False
    assert meta["talking_head_engine"] == ""
    assert Path(meta["video_path"]).exists()


# ---------------------------------------------------------------- Phase 3：素材剪接
def test_extract_keywords_rule_basic():
    from video_pipeline.footage import extract_keywords_rule
    kws = extract_keywords_rule(
        "老球迷们，皇马更衣室炸了，贝林厄姆和主帅当场互喷，评论区聊聊。", k=4)
    # 朴素规则器应至少命中足球具象词（皇马/贝林厄姆/更衣室）
    joined = " ".join(kws)
    assert any(t in joined for t in ("皇马", "贝林厄姆", "更衣室"))
    # 超长复合句（>8 字）应被过滤，避免无效检索词
    assert all(len(w) <= 8 for w in kws)


def test_collect_footage_local_source(tmp_path, monkeypatch):
    from video_pipeline import footage
    # 放两个本地素材片段
    for name in ("a.mp4", "b.jpg"):
        p = tmp_path / name
        if name.endswith(".mp4"):
            _make_clip(p, size="320x320", dur=2)
        else:
            from PIL import Image
            Image.new("RGB", (400, 300), (10, 20, 30)).save(str(p))
    cfg = {"sources": ["local"], "local_dir": str(tmp_path), "max_clips": 8,
           "keywords": 4, "per_query": 3, "min_clip_dur": 0.0, "max_clip_dur": 100.0}
    pool = footage.collect_footage("皇马更衣室炸了", cfg=cfg, cache_dir=str(tmp_path / "cache"))
    assert len(pool) == 2
    assert any(x["is_image"] for x in pool)
    assert any(not x["is_image"] for x in pool)


def test_collect_footage_no_key_returns_empty():
    from video_pipeline import footage
    # 联网源但无 key → 应优雅返回空池（不抛异常），由上层回退纯主讲人
    cfg = {"sources": ["pexels_video"], "pexels_api_key": "", "keywords": 3,
           "per_query": 2, "max_clips": 6, "min_clip_dur": 0.0, "max_clip_dur": 100.0}
    pool = footage.collect_footage("皇马", cfg=cfg, cache_dir="/tmp/_fp_test_cache")
    assert pool == []


def test_edit_with_broll_real(tmp_path):
    """真实 ffmpeg 端到端：主讲人 + 素材交替 + xfade，输出竖屏且时长正确。"""
    from video_pipeline import edit
    anchor = tmp_path / "anchor.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=1080x1920:r=30:d=5",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                    "-c:v", "libopenh264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(anchor)],
                   capture_output=True, check=True)
    blue = tmp_path / "blue.mp4"; green = tmp_path / "green.mp4"
    for c in (blue, green):
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=blue:s=1920x1080:r=30:d=3",
                        "-c:v", "libopenh264", "-pix_fmt", "yuv420p", str(c)],
                       capture_output=True, check=True)
    pool = [{"path": str(blue), "is_image": False, "duration": 3.0},
            {"path": str(green), "is_image": False, "duration": 3.0}]
    segs = [{"start": i, "end": i + 1, "text": f"s{i}"} for i in range(5)]
    out = tmp_path / "edited.mp4"
    edit.edit_with_broll(str(anchor), str(anchor), segs, pool, str(out),
                         transition=0.4, lower_third="老六说球")
    assert out.exists()
    info = compose.verify_video(str(out))
    assert info["ok"] and info["has_video"] and info["has_audio"]
    # 时长 ≈ 5 - 4*0.4 = 3.4
    assert abs(info["duration"] - 3.4) < 0.5


def test_pipeline_footage_integration(tmp_path, monkeypatch):
    """footage 开启 + 本地素材库 → 应做 B-roll 剪接并记 footage_used=True。"""
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    # 准备本地素材片段
    foot_dir = tmp_path / "footage"
    foot_dir.mkdir()
    _make_clip(foot_dir / "clip1.mp4", size="640x360", dur=3)
    _make_clip(foot_dir / "clip2.mp4", size="640x360", dur=3)
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    cfg["output"]["keep_intermediate"] = True
    cfg["footage"]["enabled"] = True
    cfg["footage"]["sources"] = ["local"]
    cfg["footage"]["local_dir"] = str(foot_dir)
    cfg["footage"]["min_clip_dur"] = 0.0
    cfg["footage"]["max_clip_dur"] = 100.0
    cfg["footage"]["lower_third"] = True
    cfg["footage"]["lower_third_text"] = "老六说球"
    meta = pipeline.run_pipeline(_SAMPLE_ARTICLE, config=cfg, out_dir=tmp_path,
                                 llm_fn=_fake_llm)
    assert meta["footage_used"] is True
    assert meta["footage_count"] >= 2
    assert Path(meta["video_path"]).exists()


def test_pipeline_footage_disabled(tmp_path, monkeypatch):
    """footage 关闭 → 不应做 B-roll，footage_used=False（纯主讲人）。"""
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    cfg["footage"]["enabled"] = False
    meta = pipeline.run_pipeline(_SAMPLE_ARTICLE, config=cfg, out_dir=tmp_path,
                                 llm_fn=_fake_llm)
    assert meta["footage_used"] is False
    assert Path(meta["video_path"]).exists()
