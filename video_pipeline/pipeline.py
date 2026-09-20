#!/usr/bin/env python3
"""视频管线编排（Phase 0+1）+ 命令行入口。

把一篇图文（title/content/+可选 resonance_angle/series_id/source_id）变成一支竖屏短视频：
  口播稿 → TTS 音频+字幕 → ffmpeg 合成 → 元数据落盘。

零依赖现有图文发布逻辑；默认走 Edge TTS（免 key），声线克隆/火山引擎预留开关。
"""

import argparse
import json
import re
import sys
from datetime import datetime, date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml
# 用绝对导入，保证既可作为包导入，也可 `python video_pipeline/pipeline.py` 直接运行
from video_pipeline import script_gen, tts, subtitles, compose
from video_pipeline.clone import CloneUnavailable
from video_pipeline.talking_head import TalkingHeadUnavailable

CONFIG_PATH = Path(__file__).parent / "video_config.yaml"


def load_video_config(path=None):
    """加载 video_config.yaml；缺省回到模块同目录。"""
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        # 最小默认配置，保证不崩
        return {
            "portrait_path": "",
            "voice": {"provider": "edge", "default": tts.DEFAULT_VOICE,
                      "fallback_order": tts.DEFAULT_FALLBACK, "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"},
            "clone": {"enabled": False},
            "volcano": {"enabled": False},
            "video": {"width": 1080, "height": 1920, "fps": 30, "background_fallback": "gradient",
                      "bg_color": "0x10131A", "ken_burns": True,
                      "subtitle": {"font_size": 46, "primary_color": "0xFFFFFF",
                                   "outline_color": "0x000000", "outline": 4,
                                   "back_color": "0x80000000", "margin_v": 140, "max_chars_per_line": 18}},
            "output": {"base_dir": "output/videos", "keep_intermediate": True},
        }
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _slug(title, max_len=40):
    """从标题生成安全文件名片段。"""
    s = re.sub(r"[^\w一-鿿]+", "_", title or "video").strip("_")
    return s[:max_len] or "video"


def run_pipeline(article, config=None, out_dir=None, brand_manual=None, llm_fn=None):
    """跑完整支线：稿→音→字幕→视频→元数据。

    Args:
        article: dict {title, content, resonance_angle?, series_id?, source_id?}
        config: 已加载配置；None 则读默认。
        out_dir: 覆盖输出目录（默认 output/videos/YYYY-MM-DD）。
        brand_manual: 预渲染品牌手册（None 则懒加载）。
        llm_fn: 注入 LLM（测试用）；None 走真实调用。
    Returns:
        dict: 元数据（含各产物路径、时长、声线等）。
    """
    cfg = config or load_video_config()
    today = date.today().isoformat()
    base = Path(_ROOT) / cfg.get("output", {}).get("base_dir", "output/videos") / today
    if out_dir:
        base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    # 中间产物（wav/srt/ass）统一放进 _intermediate 子目录，输出目录只留 mp4 + meta.json。
    # 关键：避免与 mp4 同名的 .srt 落在输出目录，导致播放器自动加载外挂字幕、
    # 与烧录字幕叠成"两条字幕"（曾反复踩坑）。
    inter = base / "_intermediate"
    inter.mkdir(parents=True, exist_ok=True)

    slug = _slug(article.get("title", ""))
    audio_path = inter / f"{slug}.wav"
    srt_path = inter / f"{slug}.srt"
    mp4_path = base / f"{slug}.mp4"

    # 1) 口播稿
    print(f"① 口播稿生成：{article.get('title', '')[:30]}")
    script_info = script_gen.generate_script(article, llm_fn=llm_fn, brand_manual=brand_manual)
    script_text = script_info["script"]
    print(f"   来源={script_info.get('source')} 字数={len(script_text)}")

    # 2) TTS（声线：clone/volcano 优先，失败自动回退 Edge TTS，保证出片）
    print("② TTS 合成音频 + 字幕时间轴")
    vc = cfg.get("voice", {})
    rate = vc.get("rate", "+0%"); volume = vc.get("volume", "+0%"); pitch = vc.get("pitch", "+0Hz")
    provider = vc.get("provider", "edge")
    tts_engine = "edge"
    voice_cloned = False
    try:
        if provider == "clone" and cfg.get("clone", {}).get("enabled"):
            cc = cfg["clone"]
            audio_path, srt_path, used = tts.synthesize_clone(
                script_text, audio_path=audio_path, srt_path=srt_path,
                reference_audio=cc.get("reference_audio"),
                model_dir=cc.get("model_dir", ""),
                host=cc.get("host", "127.0.0.1"),
                port=cc.get("port", 9880),
                prompt_text=cc.get("prompt_text", ""),
                timeout=cc.get("timeout", 600))
            tts_engine = "gpt-sovits"
            voice_cloned = True
        elif provider == "volcano" and cfg.get("volcano", {}).get("enabled"):
            vol = cfg["volcano"]
            audio_path, srt_path, used = tts.synthesize_volcano(
                script_text, audio_path=audio_path, srt_path=srt_path,
                app_id=vol.get("app_id"), token=vol.get("token"),
                cluster=vol.get("cluster"), speaker=vol.get("speaker"))
            tts_engine = "volcano"
        else:
            voices = tts.resolve_voice(vc)
            audio_path, srt_path, used = tts.synthesize(
                script_text, voice=voices[0], audio_path=audio_path, srt_path=srt_path,
                rate=rate, volume=volume, pitch=pitch, fallback_voices=voices[1:])
    except Exception as e:
        # 克隆/火山失败 → 回退 Edge TTS（不中断出片）
        print(f"   ⚠️ TTS 分支({provider})失败：{e}，回退 Edge TTS")
        voices = tts.resolve_voice(vc)
        audio_path, srt_path, used = tts.synthesize(
            script_text, voice=voices[0], audio_path=audio_path, srt_path=srt_path,
            rate=rate, volume=volume, pitch=pitch, fallback_voices=voices[1:])
        tts_engine = "edge"
        voice_cloned = False
    print(f"   声线={used} 引擎={tts_engine}")

    # 3) 字幕折行优化
    sub_cfg = cfg.get("video", {}).get("subtitle", {})
    max_chars = sub_cfg.get("max_chars_per_line", 18)
    subtitles.postprocess(srt_path, max_chars=max_chars)

    # 3.5) 说话数字人（可选）：肖像 + 配音 → 说话脸中间视频；失败回退静态肖像
    talking_head_video = None
    talking_head_engine = ""
    talking_head_used = False
    th_cfg = cfg.get("talking_head", {})
    if th_cfg.get("enabled"):
        try:
            from video_pipeline import talking_head as _th
            th_out = inter / f"{slug}.talking.mp4"
            portrait = cfg.get("portrait_path", "")
            talking_head_video, th_engine = _th.generate_talking_head(
                portrait, str(audio_path), str(th_out),
                engine=th_cfg.get("engine", "sadtalker"), cfg=th_cfg)
            talking_head_engine = th_engine
            talking_head_used = True
            print(f"   说话脸={th_engine}")
        except TalkingHeadUnavailable as e:
            print(f"   ⚠️ 说话数字人不可用：{e}，回退静态肖像")
            talking_head_video = None
        except Exception as e:
            print(f"   ⚠️ 说话数字人异常：{e}，回退静态肖像")
            talking_head_video = None

    # 4) 合成视频
    print("③ ffmpeg 合成竖屏视频")
    vcfg = cfg.get("video", {})
    sub_style = {k: sub_cfg[k] for k in ("font_size", "primary_color", "outline_color",
                                         "outline", "back_color", "margin_v") if k in sub_cfg}
    mp4_path, info = compose.compose_video(
        cfg.get("portrait_path", ""),
        audio_path, srt_path, mp4_path,
        width=vcfg.get("width", 1080), height=vcfg.get("height", 1920),
        fps=vcfg.get("fps", 30),
        bg_fallback=vcfg.get("background_fallback", "gradient"),
        bg_color=vcfg.get("bg_color", "0x10131A"),
        ken_burns=vcfg.get("ken_burns", True),
        talking_head_video=talking_head_video,
        sub_style=sub_style,
    )
    print(f"   视频={mp4_path} 时长={info.get('duration'):.1f}s 校验={info.get('ok')}")

    # 4.5) 自愈：清掉输出根目录里与 mp4 同名的外挂字幕（历史版本残留的 .srt/.ass）。
    # 播放器会把与视频同名的字幕文件当外挂自动加载，与烧录字幕叠成"两条字幕"。
    # 新版中间产物已隔离到 _intermediate，这里再兜一层，连历史残留一起自愈。
    for _ext in (".srt", ".ass"):
        _stale = Path(mp4_path).with_suffix(_ext)
        if _stale.exists():
            try:
                _stale.unlink()
            except OSError:
                pass

    # 5) 元数据
    meta = {
        "title": script_info.get("title", article.get("title", "")),
        "script": script_text,
        "hook_type": script_info.get("hook_type", ""),
        "estimated_duration_sec": script_info.get("estimated_duration_sec", 0),
        "source_id": article.get("source_id", ""),
        "resonance_angle": article.get("resonance_angle", ""),
        "series_id": article.get("series_id", ""),
        "voice": used,
        "tts_engine": tts_engine,
        "voice_cloned": voice_cloned,
        "talking_head_engine": talking_head_engine,
        "talking_head_used": talking_head_used,
        "resolution": f"{vcfg.get('width', 1080)}x{vcfg.get('height', 1920)}",
        "has_portrait": bool(cfg.get("portrait_path")) and Path(cfg["portrait_path"]).exists(),
        "video_path": str(mp4_path),
        "audio_path": str(audio_path),
        "srt_path": str(srt_path),
        "actual_duration_sec": round(info.get("duration", 0), 1),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta_path = base / f"{slug}.meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6) 中间产物清理（可选）；连 compose 阶段产生的瞬时 .ass、说话脸中间视频一并清理
    if not cfg.get("output", {}).get("keep_intermediate", True):
        ass_path = Path(srt_path).with_suffix(".ass")
        cleanup = [audio_path, srt_path, ass_path]
        if talking_head_video:
            cleanup.append(Path(talking_head_video))
        for p in cleanup:
            try:
                Path(p).unlink()
            except Exception:
                pass
        meta["audio_path"] = ""
        meta["srt_path"] = ""

    print(f"✅ 完成：{mp4_path}")
    return meta


_SAMPLE_ARTICLE = {
    "title": "皇马更衣室炸了？贝林厄姆和主帅当场互喷",
    "content": (
        "昨夜伯纳乌的更衣室，据说比比分牌还热闹。据多家西媒透露，皇马在输球之后，"
        "贝林厄姆和主帅之间爆发了激烈争执，矛盾点集中在中场调度和换人时机上。"
        "这不是第一次了，本赛季皇马的更衣室气氛一直紧绷。老球迷都懂，当更衣室开始漏风，"
        "战绩往往跟着掉。当年那支所向披靡的皇马，靠的从来不是某个巨星，而是更衣室的拧成一股绳。"
        "如今新星崛起、老将退场，权力结构正在重写。你觉得这波内耗，会让皇马这个赛季直接掉队吗？"
    ),
    "resonance_angle": "名帅名宿沉浮",
    "series_id": "old-fan-night",
    "source_id": "demo-2026",
}


def _main():
    ap = argparse.ArgumentParser(description="文章 → 数字人口播短视频（Phase 0+1 MVP）")
    ap.add_argument("--article", help="文章 JSON 路径（含 title/content，可选 resonance_angle/series_id/source_id）")
    ap.add_argument("--demo", action="store_true", help="用内置样例文章跑一遍")
    ap.add_argument("--config", help="video_config.yaml 路径（默认模块内）")
    ap.add_argument("--out", help="输出目录（默认 output/videos/YYYY-MM-DD）")
    ap.add_argument("--list-voices", action="store_true", help="列出可用 Edge TTS 男声预设")
    args = ap.parse_args()

    if args.list_voices:
        for k, v in tts.VOICE_PRESETS.items():
            print(f"  {k:8s} -> {v}")
        return

    if args.demo:
        article = _SAMPLE_ARTICLE
    elif args.article:
        article = json.loads(Path(args.article).read_text(encoding="utf-8"))
    else:
        ap.error("需指定 --article <json> 或 --demo")

    cfg = load_video_config(args.config)
    meta = run_pipeline(article, config=cfg, out_dir=args.out)
    print("\n--- 元数据 ---")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
