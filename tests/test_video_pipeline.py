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
    assert cfg["voice"]["default"] == "zh-CN-YunjianNeural"
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


def _disable_optional(cfg):
    """关掉联网/素材相关可选块，让基础管线测试可离线、确定性运行。"""
    cfg.setdefault("footage", {})["enabled"] = False
    cfg.setdefault("teams", {})["enabled"] = False
    cfg.setdefault("audio", {})["enabled"] = False
    # 纯文字动效也关掉：基础管线测试要覆盖「常规 compose 分支」，不能被动效分支短路
    cfg.setdefault("textmotion", {})["enabled"] = False
    return cfg


def test_run_pipeline_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    cfg["output"]["keep_intermediate"] = True
    _disable_optional(cfg)
    meta = pipeline.run_pipeline(_SAMPLE_ARTICLE, config=cfg, out_dir=tmp_path,
                                 llm_fn=_fake_llm)
    assert meta["source_id"] == _SAMPLE_ARTICLE["source_id"] if "source_id" in _SAMPLE_ARTICLE else True
    assert Path(meta["video_path"]).exists()
    assert meta["voice"] == "zh-CN-YunjianNeural"
    assert meta["resolution"] == "1080x1920"
    assert meta["actual_duration_sec"] > 0
    # 元数据落盘
    assert Path(meta["video_path"]).with_suffix(".meta.json").exists()


def test_run_pipeline_without_intermediate_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    cfg["output"]["keep_intermediate"] = False
    _disable_optional(cfg)
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
    _disable_optional(cfg)
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
    _disable_optional(cfg)
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
    pool, used = footage.collect_footage("皇马更衣室炸了", cfg=cfg, cache_dir=str(tmp_path / "cache"))
    assert len(pool) == 2
    assert any(x["is_image"] for x in pool)
    assert any(not x["is_image"] for x in pool)
    assert used == ["local"]


def test_collect_footage_no_key_returns_empty():
    from video_pipeline import footage
    # 联网源但无 key → 应优雅返回空池（不抛异常），由上层回退纯主讲人
    cfg = {"sources": ["pexels_video"], "pexels_api_key": "", "keywords": 3,
           "per_query": 2, "max_clips": 6, "min_clip_dur": 0.0, "max_clip_dur": 100.0}
    pool, used = footage.collect_footage("皇马", cfg=cfg, cache_dir="/tmp/_fp_test_cache")
    assert pool == []
    assert used == []


def test_http_get_json_sets_user_agent(monkeypatch):
    """回归：_http_get_json 必须带浏览器 UA，否则 Pexels WAF 拦截 Python-urllib 返回 403。"""
    from video_pipeline import footage
    import urllib.request
    captured = {}
    class FakeResp:
        def read(self): return b'{"ok":1}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=20):
        hdrs = dict(req.header_items())
        # Request 可能把 key 规整为 'User-agent'，做大小写不敏感匹配
        ua = next((v for k, v in hdrs.items() if k.lower() == "user-agent"), None)
        captured["ua"] = ua
        return FakeResp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    footage._http_get_json("https://example.com/x", headers={"Authorization": "k"})
    ua = captured["ua"]
    assert ua and ua.startswith("Mozilla"), f"UA 应带浏览器标识，实际={ua}"
    assert "urllib" not in ua.lower(), f"UA 不能含 urllib（会被 Pexels 403），实际={ua}"


def test_to_english_translates_football_terms():
    from video_pipeline import footage
    # 中文足球词应映射到英文检索词
    assert footage._to_english("皇马") == "Real Madrid"
    assert footage._to_english("贝林厄姆") == "Bellingham"
    assert footage._to_english("更衣室") == "locker room"
    # 含中文的短语应子串命中
    assert footage._to_english("皇马更衣室炸了") == "Real Madrid"
    # 纯 ASCII 原样返回
    assert footage._to_english("Real Madrid") == "Real Madrid"
    # 无映射的纯中文 → None（避免拿中文去英文库搜 0 结果）
    assert footage._to_english("我今天心情不错") is None


def test_collect_footage_translates_cjk_to_english(tmp_path):
    from video_pipeline import footage
    # 用假的联网检索，验证中文脚本会被翻译成英文去搜
    captured = {}
    def fake_search(q, key, per_page=5, timeout=20):
        captured.setdefault("queries", []).append(q)
        return []  # 返回空，触发兜底
    footage.search_pexels_videos = fake_search
    footage.search_pixabay_videos = fake_search
    footage.search_pexels_images = fake_search
    cfg = {"sources": ["pexels_video"], "pexels_api_key": "x", "keywords": 4,
           "per_query": 2, "max_clips": 6, "min_clip_dur": 0.0, "max_clip_dur": 100.0}
    pool, used = footage.collect_footage(
        "皇马更衣室炸了，贝林厄姆和主帅当场互喷", cfg=cfg, cache_dir=str(tmp_path / "c"))
    # 至少应有英文检索词（含兜底 football match），且不应出现中文
    assert any("Real Madrid" in q or "football" in q for q in captured["queries"])
    assert not any(footage._has_cjk(q) for q in captured["queries"])


def test_extract_keywords_rule_prefers_football_entities():
    """english=True：优先抽「B-roll 安全词」（球队/赛事/场景），且**排除球员名**。"""
    from video_pipeline import footage
    script = "姆巴佩接贝林厄姆直塞单刀破门，皇马主场逆转巴萨登顶积分榜。"
    kw = footage.extract_keywords_rule(script, k=6, english=True)
    # 只抽「场景/赛事/通用」类 B-roll 安全词，且它们都能译成英文
    assert all(footage._to_english(w) is not None for w in kw)
    # 球员名必须被排除：按球员名搜出的是「长相相似的路人/模特」，会张冠李戴
    assert "姆巴佩" not in kw
    assert "贝林厄姆" not in kw
    assert all(w not in footage._PLAYER_NAMES for w in kw)
    # 球队名也必须被排除：按队名（如 Barcelona）搜图会返回城市街景等无关画面
    assert "皇马" not in kw and "巴萨" not in kw
    assert all(w not in footage._TEAM_NAMES for w in kw)
    # 不足 k 个时用通用词补齐
    assert len(kw) == 6
    # 非 english 模式：不注入通用英文词
    kw2 = footage.extract_keywords_rule("皇马更衣室炸了", k=4, english=False)
    assert not any(w in ("football", "soccer stadium") for w in kw2)


def test_collect_footage_online_before_local(tmp_path, monkeypatch):
    from video_pipeline import footage
    # 本地放一条素材（制造"本地有货"的前提）
    local_dir = tmp_path / "local"; local_dir.mkdir()
    _make_clip(local_dir / "local_clip.mp4", size="320x320", dur=2)
    # mock Pexels 视频检索 + 下载（避免真实网络）；用 monkeypatch 自动还原，避免污染后续用例
    def fake_search(q, key, per_page=5, timeout=20):
        return [{"url": "http://example.com/v.mp4", "width": 1920, "height": 1080, "duration": 5}]
    monkeypatch.setattr(footage, "search_pexels_videos", fake_search)
    def fake_dl(url, out_path, timeout=60):
        open(out_path, "wb").close()
    monkeypatch.setattr(footage, "_http_download", fake_dl)
    cfg = {"sources": ["pexels_video", "local"], "pexels_api_key": "x",
           "local_dir": str(local_dir), "keywords": 2, "per_query": 1,
           "max_clips": 6, "min_clip_dur": 0.0, "max_clip_dur": 100.0}
    pool, used = footage.collect_footage("皇马", cfg=cfg, cache_dir=str(tmp_path / "cache"))
    # english=True 时不足 k 个会用通用足球词补齐（保证联网源有素材），
    # mock 每个 query 都返回 1 条 → 数量 = 实际查询数（≥1），全部来自 Pexels。
    assert len(pool) >= 1
    assert used == ["pexels_video"]            # 只用了联网源
    assert all("local_clip" not in p["path"] for p in pool)  # 没用本地那条


def test_collect_footage_local_fallback_when_online_empty(tmp_path):
    """联网全空（无 key/无网）→ 回退本地素材库。"""
    from video_pipeline import footage
    local_dir = tmp_path / "local"; local_dir.mkdir()
    _make_clip(local_dir / "local_clip.mp4", size="320x320", dur=2)
    # 联网源无 key → 抛 FootageUnavailable，最终回退本地
    cfg = {"sources": ["pexels_video", "local"], "pexels_api_key": "",
           "local_dir": str(local_dir), "keywords": 2, "per_query": 1,
           "max_clips": 6, "min_clip_dur": 0.0, "max_clip_dur": 100.0}
    pool, used = footage.collect_footage("皇马", cfg=cfg, cache_dir=str(tmp_path / "cache"))
    assert len(pool) == 1
    assert used == ["local"]
    assert "local_clip" in pool[0]["path"]


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
    segs = [{"start": i, "end": i + 1, "text": f"s{i}"} for i in range(6)]
    out = tmp_path / "edited.mp4"
    edit.edit_with_broll(str(anchor), str(anchor), segs, pool, str(out),
                         transition=0.4, lower_third="老六说球")
    assert out.exists()
    info = compose.verify_video(str(out))
    assert info["ok"] and info["has_video"] and info["has_audio"]
    # 视觉总长 = 6 - 5*0.4 = 4.0，小于锚层音频 5.0s → 应垫满到音频长度（末帧冻结），
    # 即成片时长 ≡ 音频时长 5.0s（修复「末句口播被切」）
    assert abs(info["duration"] - 5.0) < 0.5, f"应垫满到音频 5.0s，实际={info['duration']}"


def test_edit_with_broll_mixed_framerate(tmp_path):
    """真实场景回归：25fps 实拍素材 与 30fps 合成锚层（时间基不一致）
    混剪时，xfade 必须能过——验证段级 fps+settb 统一时间基的修复。

    注：用 color 源造不同帧率（锚层 30fps / 素材 25fps）即可触发 xfade 的
    「timebase do not match」冲突；gradients 源在本沙箱 ffmpeg 构建下会卡死，故不用。
    """
    from video_pipeline import edit
    # 锚层：color 30fps（时间基 1/15360 量级）+ 正弦音轨
    anchor = tmp_path / "anchor.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=1080x1920:r=30:d=5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-c:v", "libopenh264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(anchor)],
        capture_output=True, check=True)
    # 实拍素材：25fps（Pexels 真实片段典型帧率），横屏到竖屏会被 scale+pad
    real = tmp_path / "real25.mp4"
    subprocess.run(["ffmpeg", "-y", "-r", "25", "-f", "lavfi", "-i", "color=c=blue:s=1920x1080:r=25:d=3",
                    "-c:v", "libopenh264", "-pix_fmt", "yuv420p", str(real)],
                   capture_output=True, check=True)
    pool = [{"path": str(real), "is_image": False, "duration": 3.0},
            {"path": str(real), "is_image": False, "duration": 3.0}]
    segs = [{"start": i, "end": i + 1, "text": f"s{i}"} for i in range(6)]
    out = tmp_path / "edited.mp4"
    edit.edit_with_broll(str(anchor), str(anchor), segs, pool, str(out),
                         transition=0.4, lower_third="老六说球")
    assert out.exists()
    info = compose.verify_video(str(out))
    assert info["ok"] and info["has_video"] and info["has_audio"]
    # 视觉 4.0s < 音频 5.0s → 垫满到音频长度（末帧冻结）
    assert abs(info["duration"] - 5.0) < 0.5, f"应垫满到音频 5.0s，实际={info['duration']}"


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
    cfg["teams"]["enabled"] = False   # 关闭球队标识，避免测试触网
    cfg["audio"]["enabled"] = False   # 关闭音频混音，专注验证 B-roll
    cfg["textmotion"]["enabled"] = False  # 关闭文字动效，走常规 B-roll 剪接分支
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


# ---------------------------------------------------------------- 开场球队标识（去真人出镜）
def test_detect_teams_identifies_clubs_not_players():
    """球队识别：命中俱乐部/国家队；球员名（姆巴佩）不应被当作球队。"""
    from video_pipeline import teams
    script = "皇马更衣室炸了，姆巴佩和主帅互喷；巴萨与巴黎的比赛也起波澜。"
    got = teams.detect_teams(script)
    zhs = [t["zh"] for t in got]
    assert "皇马" in zhs and "巴萨" in zhs and "巴黎" in zhs
    assert "姆巴佩" not in zhs               # 球员不在球队表
    assert all(t.get("en") and t.get("wiki") for t in got)
    # 按出现顺序：皇马在前，巴萨其次
    assert zhs.index("皇马") < zhs.index("巴萨")


def test_detect_teams_empty_when_none():
    from video_pipeline import teams
    assert teams.detect_teams("今天天气不错，适合出门散步。") == []
    assert teams.detect_teams("") == []


def test_fetch_team_assets_mock_and_cache(tmp_path, monkeypatch):
    """fetch_team_assets：mock 掉网络 → 下载队标/球场图并落盘缓存；二次调用不再请求。"""
    from video_pipeline import teams
    calls = {"n": 0}
    def fake_json(url, timeout=20):
        calls["n"] += 1
        if "pageimages" in url:
            return {"query": {"pages": {"1": {"original": {"source": "http://x/crest.png"}}}}}
        if "pageprops" in url:
            return {"query": {"pages": {"1": {"pageprops": {"wikibase_item": "Q1"}}}}}
        if "Q1" in url and "EntityData" in url:
            return {"entities": {"Q1": {"claims": {"P115": [{"mainsnak": {"datavalue": {"value": {"id": "Q2"}}}}]}}}}
        if "Q2" in url:
            return {"entities": {"Q2": {"claims": {"P18": [{"mainsnak": {"datavalue": {"value": "Stadium.jpg"}}}]}}}}
        return {}
    def fake_dl(url, out_path, timeout=60):
        Path(out_path).write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(teams, "_http_get_json", fake_json)
    monkeypatch.setattr(teams, "_http_download", fake_dl)
    team = {"zh": "皇马", "en": "Real Madrid", "wiki": "Real Madrid CF"}
    cache = tmp_path / "teams"
    out1 = teams.fetch_team_assets(team, cache, stadium=True)
    assert out1["crest"] and Path(out1["crest"]).exists()
    assert out1["stadium"] and Path(out1["stadium"]).exists()
    n_after_first = calls["n"]
    # 二次调用：文件已缓存 → 网络请求数不再增加
    out2 = teams.fetch_team_assets(team, cache, stadium=True)
    assert out2["crest"] == out1["crest"] and out2["stadium"] == out1["stadium"]
    assert calls["n"] == n_after_first


def test_collect_team_images_builds_pool(tmp_path, monkeypatch):
    """collect_team_images：把队标/球场图组装成可 prepend 的素材池条目。"""
    from video_pipeline import teams
    def fake_assets(team, cache_dir, *, stadium=True, timeout=20):
        c = Path(cache_dir); c.mkdir(parents=True, exist_ok=True)
        cp = c / f"{team['en']}_crest.png"; cp.write_bytes(b"\x89PNG")
        return {"crest": str(cp), "stadium": None}
    monkeypatch.setattr(teams, "fetch_team_assets", fake_assets)
    cfg = {"cache_dir": str(tmp_path / "tc"), "stadium": False}
    pool = teams.collect_team_images(
        [{"zh": "皇马", "en": "Real Madrid", "wiki": "Real Madrid CF"}], cfg)
    assert len(pool) == 1
    assert pool[0]["is_image"] is True and Path(pool[0]["path"]).exists()


# ---------------------------------------------------------------- 音频混音（BGM + 音效）
def _make_tone(path, freq=440, dur=3):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}",
         "-c:a", "pcm_s16le", str(path)], capture_output=True, check=True)
    return path


def test_audio_mix_no_assets_returns_narration(tmp_path):
    """无 BGM/音效素材 → 原样返回旁白路径（优雅降级）。"""
    from video_pipeline import audio_mix
    narr = _make_tone(tmp_path / "narr.wav")
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:03,000\n皇马进球了。\n", encoding="utf-8")
    out = audio_mix.mix(str(narr), str(srt), {}, str(tmp_path / "nobgm"), str(tmp_path / "nosfx"))
    assert str(out) == str(narr)


def test_audio_mix_with_bgm_and_sfx(tmp_path):
    """有 BGM + 关键词音效 → 产出混音 wav，时长贴合旁白，含音轨。"""
    from video_pipeline import audio_mix
    narr = _make_tone(tmp_path / "narr.wav", 440, 4)
    bgm_dir = tmp_path / "bgm"; bgm_dir.mkdir()
    _make_tone(bgm_dir / "theme.wav", 220, 2)     # 2s，会被 loop 到 4s
    sfx_dir = tmp_path / "sfx"; sfx_dir.mkdir()
    _make_tone(sfx_dir / "cheer.wav", 880, 1)     # 命中"进球"
    srt = tmp_path / "a.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n大家好，聊聊这场比赛。\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n他完成绝杀进球。\n", encoding="utf-8")
    cfg = {"bgm_volume": 0.18, "sfx_volume": 0.4, "bgm_fade_out": 1.0}
    out = tmp_path / "mixed.wav"
    res = audio_mix.mix(str(narr), str(srt), cfg, str(bgm_dir), str(sfx_dir), out_path=str(out))
    assert res == str(out) and Path(res).exists()
    info = compose.verify_video(res)
    assert info["has_audio"]
    assert abs(info["duration"] - 4.0) < 0.6


def test_audio_mix_keyword_map_detection():
    """关键词 → 音效映射：进球→cheer，红牌→whistle，转会→news。"""
    from video_pipeline import audio_mix
    segs = [{"start": 1.0, "text": "他打进了绝杀进球"},
            {"start": 3.0, "text": "裁判出示红牌引发争议"},
            {"start": 5.0, "text": "俱乐部官宣签下新援"}]
    km = audio_mix._build_keyword_map({})
    trig = audio_mix._detect_sfx_triggers(segs, km)
    names = {n for n, _ in trig}
    assert "cheer" in names and "whistle" in names and "news" in names
    # 时间对齐：cheer 应在 1.0s 触发
    assert (("cheer", 1.0) in trig) or any(n == "cheer" and abs(t - 1.0) < 0.01 for n, t in trig)


def test_audio_mix_keyword_map_override():
    from video_pipeline import audio_mix
    km = audio_mix._build_keyword_map({"cheer": ["破门", "绝平"]})
    # 覆盖后旧关键词"进球"不再触发 cheer
    segs = [{"start": 0.5, "text": "他打进一球"}]
    assert all(n != "cheer" for n, _ in audio_mix._detect_sfx_triggers(segs, km))
    segs2 = [{"start": 0.5, "text": "他绝平了比分"}]
    assert ("cheer", 0.5) in audio_mix._detect_sfx_triggers(segs2, km)


# ---------------------------------------------------------------- 开场球队标识接入剪接
def test_edit_with_broll_intro_forces_first_segments(tmp_path):
    """intro_broll：前 N 段强制用球队标识图（覆盖开场真人），且能在空素材池时出片。"""
    from video_pipeline import edit
    anchor = tmp_path / "anchor.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=1080x1920:r=30:d=5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-c:v", "libopenh264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(anchor)],
        capture_output=True, check=True)
    from PIL import Image
    c1 = tmp_path / "c1.png"; c2 = tmp_path / "c2.png"
    Image.new("RGB", (600, 600), (240, 240, 240)).save(str(c1))
    Image.new("RGB", (600, 600), (20, 20, 80)).save(str(c2))
    segs = [{"start": i, "end": i + 1, "text": f"s{i}"} for i in range(6)]
    # 仅有开场标识、素材池为空 → 应能出片（非开场段回退主讲人）
    out = tmp_path / "edited.mp4"
    edit.edit_with_broll(str(anchor), str(anchor), segs, [], str(out),
                         transition=0.4, intro_broll=[str(c1), str(c2)])
    assert out.exists()
    info = compose.verify_video(str(out))
    assert info["ok"] and info["has_video"] and info["has_audio"]
    # 视觉 4.0s < 音频 5.0s → 垫满到音频长度
    assert abs(info["duration"] - 5.0) < 0.5, f"应垫满到音频 5.0s，实际={info['duration']}"


def test_edit_with_broll_intro_map_places_exact_segments(tmp_path):
    """intro_map：队标精确落到指定段（讲哪支队就显示哪支队标），空素材池也能出片。"""
    from video_pipeline import edit
    anchor = tmp_path / "anchor.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=1080x1920:r=30:d=5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
         "-c:v", "libopenh264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(anchor)],
        capture_output=True, check=True)
    from PIL import Image
    c1 = tmp_path / "c1.png"; c2 = tmp_path / "c2.png"
    Image.new("RGB", (600, 600), (240, 240, 240)).save(str(c1))
    Image.new("RGB", (600, 600), (20, 20, 80)).save(str(c2))
    segs = [{"start": i, "end": i + 1, "text": f"s{i}"} for i in range(6)]
    out = tmp_path / "edited.mp4"
    # 段 1 放 c1、段 3 放 c2（乱序、非连续）
    edit.edit_with_broll(str(anchor), str(anchor), segs, [], str(out),
                         transition=0.4, intro_map={1: str(c1), 3: str(c2)})
    assert out.exists()
    info = compose.verify_video(str(out))
    assert info["ok"] and info["has_video"] and info["has_audio"]
    # 视觉 4.0s < 音频 5.0s → 垫满到音频长度
    assert abs(info["duration"] - 5.0) < 0.5, f"应垫满到音频 5.0s，实际={info['duration']}"


def test_align_teams_to_segments_positions():
    """align_teams_to_segments：按「队名首次出现的字符位置」对齐到对应句。"""
    from video_pipeline import teams as _teams
    script = "皇马率先破门取得领先。随后巴萨疯狂反扑扳平比分。最后皇马绝杀赢下比赛。"
    segs = [{"start": 0, "end": 3, "text": "皇马率先破门取得领先。"},
            {"start": 3, "end": 6, "text": "随后巴萨疯狂反扑扳平比分。"},
            {"start": 6, "end": 9, "text": "最后皇马绝杀赢下比赛。"}]
    detected = _teams.detect_teams(script)
    # 内置表里应有皇马/巴萨（此处不依赖联网）
    zhs = [t["zh"] for t in detected]
    assert "皇马" in zhs and "巴萨" in zhs
    # 每支队都应带 pos（首次出现位置）
    for t in detected:
        assert isinstance(t["pos"], int) and t["pos"] >= 0
    m = _teams.align_teams_to_segments(detected, segs, script)
    # 皇马首次出现在第 1 句（段 0），巴萨首次出现在第 2 句（段 1）
    assert m.get(0) == "皇马"
    assert m.get(1) == "巴萨"


def test_pipeline_teams_integration(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    from video_pipeline import teams as _teams
    from PIL import Image
    def fake_assets(team, cache_dir, *, stadium=False, timeout=20):
        c = Path(cache_dir); c.mkdir(parents=True, exist_ok=True)
        cp = c / f"{team['en']}_crest.png"
        Image.new("RGB", (600, 600), (200, 200, 200)).save(str(cp))
        return {"crest": str(cp), "stadium": None}
    monkeypatch.setattr(_teams, "fetch_team_assets", fake_assets)
    foot_dir = tmp_path / "footage"; foot_dir.mkdir()
    _make_clip(foot_dir / "clip1.mp4", size="640x360", dur=3)
    _make_clip(foot_dir / "clip2.mp4", size="640x360", dur=3)
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    cfg["output"]["keep_intermediate"] = True
    cfg["footage"]["enabled"] = True
    cfg["footage"]["sources"] = ["local"]
    cfg["footage"]["local_dir"] = str(foot_dir)
    cfg["footage"]["min_clip_dur"] = 0.0
    cfg["footage"]["max_clip_dur"] = 100.0
    cfg["teams"]["enabled"] = True
    cfg["teams"]["stadium"] = False
    cfg["audio"]["enabled"] = False
    cfg["textmotion"]["enabled"] = False  # 走常规 B-roll + 队标剪接分支
    meta = pipeline.run_pipeline(_SAMPLE_ARTICLE, config=cfg, out_dir=tmp_path,
                                 llm_fn=_fake_llm)
    assert meta["footage_used"] is True
    assert "皇马" in meta["teams_used"]      # 样例稿含"皇马"
    assert Path(meta["video_path"]).exists()


def test_pipeline_host_show_portrait_false(tmp_path, monkeypatch):
    """host.show_portrait=false → 元数据 has_portrait=False（去真人出镜）。"""
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    _disable_optional(cfg)
    cfg["host"]["show_portrait"] = False
    cfg["portrait_path"] = str(tmp_path / "nonexistent.jpg")  # 即便配了肖像也不该用
    meta = pipeline.run_pipeline(_SAMPLE_ARTICLE, config=cfg, out_dir=tmp_path,
                                 llm_fn=_fake_llm)
    assert meta["show_portrait"] is False
    assert meta["has_portrait"] is False
    assert Path(meta["video_path"]).exists()


def test_pipeline_audio_mix_integration(tmp_path, monkeypatch):
    """audio 开启 + 提供 BGM → 管线产出音频为混音结果，且标记 audio_mixed=True。"""
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    bgm_dir = tmp_path / "bgm"; bgm_dir.mkdir()
    _make_tone(bgm_dir / "theme.wav", 220, 2)
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    _disable_optional(cfg)
    cfg["output"]["keep_intermediate"] = True
    cfg["audio"]["enabled"] = True
    cfg["audio"]["bgm_dir"] = str(bgm_dir)
    cfg["audio"]["sfx_dir"] = str(tmp_path / "nosfx")
    meta = pipeline.run_pipeline(_SAMPLE_ARTICLE, config=cfg, out_dir=tmp_path,
                                 llm_fn=_fake_llm)
    assert meta["audio_mixed"] is True
    assert Path(meta["video_path"]).exists()


# ---------------------------------------------------------------- 时长回归（防截断）
def test_build_image_anchor_no_portrait_keeps_full_duration(tmp_path):
    """回归：无肖像（lavfi 渐变兜底）时锚层时长必须等于旁白时长。

    历史 bug：lavfi 源 + -shortest 会让锚层音频被截到 ~1.9s，
    整个成片随之变短。修复：源显式给 d=<dur> 且无肖像时不加 -shortest。
    """
    from video_pipeline import edit
    wav = _make_tone(tmp_path / "narr.wav", 440, 5)
    out = tmp_path / "anchor.mp4"
    edit.build_image_anchor("", str(wav), str(out), 5.0)
    info = compose.verify_video(str(out))
    assert info["ok"] and info["has_video"] and info["has_audio"]
    assert abs(info["duration"] - 5.0) < 0.3, f"锚层时长应≈5s，实际={info['duration']}"


def test_audio_mix_output_is_real_wav(tmp_path):
    """回归：混音输出必须是真·PCM WAV（后缀 .wav 与编码一致）。

    历史 bug：audio_mix 用 aac 编码却存成 .wav，下游 ffmpeg 按扩展名当 WAV 解析
    → "Invalid data found" → 音频被截断到 ~1.9s。修复：改用 pcm_s16le。
    """
    import subprocess as _sp
    from video_pipeline import audio_mix
    narr = _make_tone(tmp_path / "narr.wav", 440, 4)
    bgm_dir = tmp_path / "bgm"; bgm_dir.mkdir()
    _make_tone(bgm_dir / "theme.wav", 220, 2)
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:04,000\n皇马进球了。\n", encoding="utf-8")
    out = tmp_path / "m.wav"
    res = audio_mix.mix(str(narr), str(srt), {"bgm_volume": 0.18},
                        str(bgm_dir), str(tmp_path / "nosfx"), out_path=str(out))
    probe = _sp.run(["ffprobe", "-v", "error", "-show_entries",
                     "format=format_name:stream=codec_name",
                     "-of", "default=noprint_wrappers=1", res],
                    capture_output=True, text=True).stdout
    assert "format_name=wav" in probe
    assert "codec_name=pcm_s16le" in probe
    # 时长与旁白一致（不被截断）
    assert abs(compose.ffprobe_duration(res) - 4.0) < 0.3


def test_edit_with_broll_keeps_full_duration(tmp_path):
    """回归：edit_with_broll 成片时长 = 各段之和 - 转场重叠（不被 -shortest 截短）。"""
    from video_pipeline import edit
    anchor = tmp_path / "anchor.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=1080x1920:r=30:d=6",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-c:v", "libopenh264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(anchor)],
        capture_output=True, check=True)
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1920x1080:r=30:d=4",
                    "-c:v", "libopenh264", "-pix_fmt", "yuv420p", str(clip)],
                   capture_output=True, check=True)
    pool = [{"path": str(clip), "is_image": False, "duration": 4.0}]
    segs = [{"start": i * 2, "end": i * 2 + 2, "text": f"s{i}"} for i in range(3)]  # 6s
    out = tmp_path / "e.mp4"
    edit.edit_with_broll(str(anchor), str(anchor), segs, pool, str(out), transition=0.4)
    info = compose.verify_video(str(out))
    # 视觉 6 - 2*0.4 = 5.2s < 音频 6.0s → 应垫满到音频长度（末帧冻结），末句口播不被切
    assert abs(info["duration"] - 6.0) < 0.4, f"成片应垫满到音频≈6.0s，实际={info['duration']}"


# ---------------------------------------------------------------- 队标源补充（api-sports 兜底 + 本地覆盖表）
def test_fetch_crest_falls_back_to_api_sports(tmp_path, monkeypatch):
    """Wikimedia 拿不到队标时，应降级到 api-sports CDN（无需 key）。"""
    from video_pipeline import teams
    calls = {"wiki": 0, "dl": []}
    monkeypatch.setattr(teams, "_fetch_crest", lambda wiki, timeout=20: (calls.__setitem__("wiki", calls["wiki"] + 1) or None))
    def fake_dl(url, out_path, timeout=60):
        calls["dl"].append(url)
        Path(out_path).write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(teams, "_http_download", fake_dl)
    monkeypatch.setattr(teams, "_fetch_stadium", lambda wiki, timeout=20: None)
    team = {"zh": "皇马", "en": "Real Madrid", "wiki": "Real Madrid CF", "api_id": 541}
    out = teams.fetch_team_assets(team, tmp_path / "c", stadium=False)
    assert out["crest"] and Path(out["crest"]).exists()
    assert calls["dl"] == ["https://media.api-sports.io/football/teams/541.png"]


def test_fetch_crest_api_sports_source_when_wiki_ok(tmp_path, monkeypatch):
    """Wikimedia 有队标时优先用它，不请求 api-sports。"""
    from video_pipeline import teams
    dl = []
    monkeypatch.setattr(teams, "_fetch_crest", lambda wiki, timeout=20: "http://wiki/crest.png")
    monkeypatch.setattr(teams, "_http_download", lambda u, o, timeout=60: (dl.append(u), Path(o).write_bytes(b"x")))
    monkeypatch.setattr(teams, "_fetch_stadium", lambda wiki, timeout=20: None)
    team = {"zh": "皇马", "en": "Real Madrid", "wiki": "Real Madrid CF", "api_id": 541}
    teams.fetch_team_assets(team, tmp_path / "c", stadium=False)
    assert dl == ["http://wiki/crest.png"]
    assert not any("api-sports" in u for u in dl)


def test_national_teams_have_no_api_id():
    """国家队不设 api_id（api-sports 对国家队返回国旗，非队徽）。"""
    from video_pipeline import teams
    for zh in ("英格兰", "西班牙", "德国", "法国", "巴西", "阿根廷", "葡萄牙"):
        assert "api_id" not in teams.TEAMS[zh], f"{zh} 不应有 api_id（会是国旗）"
    # 俱乐部应有 api_id（已核对的兜底源）
    for zh in ("皇马", "巴萨", "曼联", "拜仁", "尤文"):
        assert teams.TEAMS[zh].get("api_id"), f"{zh} 应有 api_id"


def test_local_teams_map_merges_and_detects(tmp_path, monkeypatch):
    """local_map：本地球队表并入识别（补中超/冷门队）；识别时一并命中。"""
    from video_pipeline import teams
    import json as _json
    lm = tmp_path / "teams_local.json"
    lm.write_text(_json.dumps({"上海海港": {"en": "Shanghai Port", "wiki": "Shanghai Port F.C.", "api_id": 1234}},
                              ensure_ascii=False), encoding="utf-8")
    cfg = {"local_map": str(lm)}
    table = teams.get_teams(cfg)
    assert "上海海港" in table and table["上海海港"]["api_id"] == 1234
    got = teams.detect_teams("上海海港主场迎战，皇马也在备战。", cfg)
    zhs = [t["zh"] for t in got]
    assert "上海海港" in zhs and "皇马" in zhs


def test_local_teams_map_missing_is_silent(tmp_path):
    """local_map 文件不存在 → 静默忽略，不影响内置识别。"""
    from video_pipeline import teams
    table = teams.get_teams({"local_map": str(tmp_path / "nope.json")})
    assert "皇马" in table


# ================================================================
# 纯文字动效（text_keywords 关键词抽取 + text_motion 渲染）
# ================================================================
def test_extract_highlights_num_and_decision():
    """比分/时间（num）与判罚词（decision）都应被抽出；序号整词不被切断。"""
    from video_pipeline import text_keywords as tk
    got = tk.extract_highlights("第67分钟姆巴佩主罚点球")
    assert ("第67分钟", "num") in got      # 整词优先，不能只取「第67分」
    assert ("点球", "decision") in got


def test_extract_highlights_team_and_num_priority():
    """球队名与比分：球队走 team，比分走 num（返回按出现位置排序，便于 ASS 顺序拼接）。"""
    from video_pipeline import text_keywords as tk
    got = tk.extract_highlights("皇马3比1巴萨", teams_table=["皇马", "巴萨"])
    kinds = dict(got)
    assert kinds.get("皇马") == "team" and kinds.get("巴萨") == "team"
    assert kinds.get("3比1") == "num"
    # 返回顺序 = 出现位置顺序
    assert [w for w, _ in got] == ["皇马", "3比1", "巴萨"]


def test_extract_highlights_emotion_words():
    """情绪动词（逆转/绝杀）应被识别为 emotion 类。"""
    from video_pipeline import text_keywords as tk
    got = tk.extract_highlights("皇马完成读秒逆转，完成绝杀")
    words = [w for w, _ in got]
    assert "读秒绝杀" in words or "绝杀" in words
    assert "逆转" in words


def test_extract_highlights_respects_max_kw():
    """max_kw 上限生效，且高亮词互不重叠。"""
    from video_pipeline import text_keywords as tk
    got = tk.extract_highlights(
        "第88分钟皇马3比1逆转巴萨完成绝杀", teams_table=["皇马", "巴萨"], max_kw=2)
    assert len(got) <= 2
    # 无重叠（词之间不互相包含）
    for a, _ in got:
        for b, _ in got:
            if a != b:
                assert not (a in b)


def test_extract_highlights_extra_words():
    """自定义额外关键词（球队昵称/黑话）可被命中。"""
    from video_pipeline import text_keywords as tk
    got = tk.extract_highlights("银河战舰今夜又炸了", extra_words=["银河战舰"])
    assert ("银河战舰", "emotion") in got


def test_split_by_highlights_roundtrip():
    """切分后拼回原文，且高亮片段 kind 正确、正文片段 kind 为 None。

    max_kw=4：num+team×2+emotion 各占一个名额（默认 3 会把"逆转"挤掉）。
    文本首尾带正文（"昨夜…完成了"），保证存在非高亮片段。
    """
    from video_pipeline import text_keywords as tk
    text = "昨夜皇马3比1逆转巴萨完成了登顶"
    hl = tk.extract_highlights(text, teams_table=["皇马", "巴萨"], max_kw=4)
    pieces = tk.split_by_highlights(text, hl)
    assert "".join(p for p, _ in pieces) == text
    kinds = {p: k for p, k in pieces}
    assert kinds.get("皇马") == "team"
    assert kinds.get("3比1") == "num"
    assert kinds.get("逆转") == "emotion"
    assert any(k is None for _, k in pieces)   # "昨夜"/"了" 等正文片段


def test_resolve_cjk_font_returns_cjk_capable_file():
    """字体探测：必须返回真实存在、且能覆盖中文的字体（绝不回退 Arial → 无方框乱码）。"""
    from video_pipeline import text_motion as tm
    ff, family = tm.resolve_cjk_font()
    assert Path(ff).exists()
    assert "CJK" in family or "Noto" in family or "YaHei" in family or "PingFang" in family


def test_resolve_cjk_font_explicit_missing_raises(tmp_path):
    """显式指定不存在的字体 → 抛异常，不静默回退。"""
    from video_pipeline import text_motion as tm
    with pytest.raises(RuntimeError):
        tm.resolve_cjk_font(str(tmp_path / "nope.ttf"))


def test_fit_font_size_shrinks_long_sentence():
    """长句应自动缩小字号（不超过 3 行），短句保持基准字号。"""
    from video_pipeline import text_motion as tm
    short = tm._fit_font_size("皇马炸了", 96, max_px=928)
    assert short == 96
    long_text = "皇马在输球之后更衣室爆发了激烈争执矛盾点集中在中场调度和换人时机的选择上面"
    long_fs = tm._fit_font_size(long_text, 96, max_px=928)
    assert long_fs < 96 and long_fs >= 56


def test_ass_color_converts_to_bgr():
    """颜色转换：0xRRGGBB → ASS 的 &H00BBGGRR（ASS 为 BGR 序）。"""
    from video_pipeline import text_motion as tm
    assert tm._ass_color("0xFF3B30") == "&H00303BFF"
    assert tm._ass_color("0xAABBCCDD") == "&HAADDCCBB"
    assert tm._ass_color(None, "&H00FFFFFF") == "&H00FFFFFF"


def test_ass_time_format():
    """秒 → ASS 时间 H:MM:SS.cc，含进位保护。"""
    from video_pipeline import text_motion as tm
    assert tm._ass_time(0) == "0:00:00.00"
    assert tm._ass_time(61.5) == "0:01:01.50"
    assert tm._ass_time(3661.999) == "1:01:01.99"


def test_build_motion_ass_has_header_and_highlights():
    """ASS 生成：PlayRes 必须等于真实分辨率（否则 libass 缩放错位）；
    关键词带上内联高亮样式，且内联样式后 \rBody 必须复位。"""
    from video_pipeline import text_motion as tm
    segs = [
        {"start": 0.0, "end": 2.0, "text": "皇马3比1逆转巴萨"},
        {"start": 2.0, "end": 4.0, "text": "姆巴佩又炸了"},
    ]
    ass = tm.build_motion_ass(segs, width=1080, height=1920, total=4.0,
                              teams_table=["皇马", "巴萨"])
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Style: Body," in ass and "Style: HL," in ass and "Style: Num," in ass
    # 高亮内联样式存在，且紧跟复位标记（否则后文会继承高亮样式）
    assert r"{\r" in ass and r"{\rBody}" in ass
    # 两条 Dialogue（两句）
    assert ass.count("Dialogue: 0,") == 2
    # 每行不超过 max_chars_per_line（删掉全部 {..} 内联标签后按纯文本算）
    import re as _re
    for line in ass.splitlines():
        if line.startswith("Dialogue: 0,"):
            body = line.split(",,", 2)[-1]
            for sub in body.split(r"\N"):
                plain = _re.sub(r"\{[^}]*\}", "", sub)
                assert len(plain) <= 20, f"单行过长({len(plain)}字): {plain!r}"


def test_build_motion_ass_clamps_last_segment_to_total():
    """末句 end 超出总时长 → 夹到 total 内（不产出超长时间轴）。"""
    from video_pipeline import text_motion as tm
    segs = [{"start": 0.0, "end": 99.0, "text": "皇马炸了"}]
    ass = tm.build_motion_ass(segs, width=1080, height=1920, total=5.0, end_hold=0)
    assert "0:00:05.00" in ass and "0:00:99" not in ass


def test_render_text_motion_end_to_end(tmp_path):
    """真实渲染：渐变底 + 大字动效 + 关键词高亮；时长 ≡ 音频，且首帧非黑。"""
    from video_pipeline import text_motion as tm, compose
    audio = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                    str(audio)], capture_output=True, check=True)
    segs = [
        {"start": 0.0, "end": 2.0, "text": "皇马3比1逆转巴萨"},
        {"start": 2.0, "end": 4.0, "text": "贝林厄姆和主帅当场互喷"},
        {"start": 4.0, "end": 6.0, "text": "更衣室炸了"},
    ]
    out = tmp_path / "motion.mp4"
    tm.render_text_motion(str(audio), segs, str(out), width=1080, height=1920, fps=30)
    assert out.exists() and out.stat().st_size > 10000
    info = compose.verify_video(str(out))
    assert info["ok"] and info["has_video"] and info["has_audio"]
    assert "1080x1920" in (info.get("resolution") or f"{info.get('width')}x{info.get('height')}")
    # 时长 ≡ 音频（6s），既不黑屏也不切末句
    assert abs(info["duration"] - 6.0) < 0.3, f"时长应≈6.0s，实际={info['duration']}"
    # 首帧必须非黑（修「开场长时间全黑」）
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", "0.2", "-i", str(out), "-vframes", "1",
         "-vf", "scale=32:32,format=gray", "-f", "rawvideo", "-"],
        capture_output=True)
    pixels = probe.stdout
    mean = sum(pixels) / max(1, len(pixels))
    assert mean > 8, f"首帧过暗（mean={mean:.1f}），疑似黑屏"


def test_render_text_motion_with_crest_decoration(tmp_path):
    """队标点缀：段内时间窗叠加小图标（不影响出片与时长）。"""
    from video_pipeline import text_motion as tm, compose
    from PIL import Image
    audio = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    str(audio)], capture_output=True, check=True)
    crest = tmp_path / "crest.png"
    Image.new("RGBA", (300, 300), (255, 255, 255, 255)).save(str(crest))
    segs = [{"start": i * 1.0, "end": i * 1.0 + 1.0, "text": f"第{i}句 皇马"} for i in range(4)]
    out = tmp_path / "motion_crest.mp4"
    tm.render_text_motion(str(audio), segs, str(out), crest_map={1: str(crest), 3: str(crest)})
    assert out.exists()
    info = compose.verify_video(str(out))
    assert info["ok"] and abs(info["duration"] - 4.0) < 0.3


def test_textmotion_branch_takes_precedence(tmp_path, monkeypatch):
    """textmotion 开启时：应走动效分支出片（跳过 footage/compose），meta 标记 textmotion_used。"""
    monkeypatch.setattr(pipeline.tts, "synthesize", _fake_synthesize)
    cfg = pipeline.load_video_config(_ROOT / "video_pipeline" / "video_config.yaml")
    cfg["output"]["keep_intermediate"] = True
    cfg["textmotion"]["enabled"] = True
    cfg["footage"]["enabled"] = True      # 即便 footage 也开着，动效优先
    cfg["teams"]["enabled"] = False       # 避免触网
    cfg["audio"]["enabled"] = False
    meta = pipeline.run_pipeline(_SAMPLE_ARTICLE, config=cfg, out_dir=tmp_path,
                                 llm_fn=_fake_llm)
    assert meta["textmotion_used"] is True
    assert meta["footage_used"] is False
    assert Path(meta["video_path"]).exists()
    assert meta["actual_duration_sec"] > 0
