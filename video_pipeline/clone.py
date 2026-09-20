#!/usr/bin/env python3
"""声线克隆（Phase 2）—— GPT-SoVITS 本地推理集成。

设计要点：
- 默认走 **HTTP 推理**：调用本地已启动的 GPT-SoVITS `api.py` 服务（`/tts` 端点）。
  为什么 HTTP 而非直接 import？GPT-SoVITS 依赖重（torch 等），通常装在与主项目隔离的
  venv/conda 里；起一个常驻服务，本管线用标准库 urllib 调它，零额外依赖、环境解耦。
- 支持 **免训练 few-shot**：只需参考音（老六声线）+ 参考文本，用预训练底座即可复刻声线；
  不训练 → CPU 也能跑（推理几分钟/条），契合 M710q 无独显的现实。
- GPT-SoVITS 的 `/tts` 只返回音频，**不返回逐词时间轴**。故字幕用「脚本分句 + 按音频时长
  等比分配」生成（SoVITS 基本保持句间节奏，句级字幕足够手机竖屏可读），无需额外对齐模型。
- 任意环节失败抛 `CloneUnavailable`，由 pipeline 回退到 Edge TTS，保证出片不中断。
"""

import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

# 句子切分标点（中英文常见句末/停顿）
_SENT_SPLIT = re.compile(r"([。！？!?；;])")
_CLAUSE_SPLIT = re.compile(r"([，,、：:])")


class CloneUnavailable(Exception):
    """声线克隆不可用（服务未起/推理失败/无参考音）。供 pipeline 回退 Edge TTS。"""


def split_sentences(text):
    """把口播稿切成句子（保留句末标点），用于字幕分句与节奏分配。"""
    text = (text or "").strip()
    if not text:
        return []
    parts = _SENT_SPLIT.split(text)
    # _SENT_SPLIT 捕获组会把标点单独切出，重新拼回句尾
    sents, buf = [], ""
    for seg in parts:
        buf += seg
        if seg and seg in "。！？!?；;":
            s = buf.strip()
            if s:
                sents.append(s)
            buf = ""
    if buf.strip():
        sents.append(buf.strip())
    return sents


def build_proportional_srt(text, duration, max_chars=15):
    """按音频时长等比分配句级 SRT（无逐词时间轴时的便宜可靠替代）。

    Args:
        text: 口播稿文本。
        duration: 音频时长（秒，ffprobe 取）。
        max_chars: 单行最大字数，超过从句中点折行（复用手机竖屏可读性）。
    Returns:
        str: SRT 文本。
    """
    sents = split_sentences(text)
    if not sents or duration <= 0:
        return ""
    # 按字数占比分配时长（长句占更久），最小保底 0.4s 防止除零/负
    weights = [max(len(s), 1) for s in sents]
    total_w = sum(weights)
    alloc = [max(duration * w / total_w, 0.4) for w in weights]

    def fmt(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        cs = int(round((t - int(t)) * 100))
        if cs == 100:
            cs = 0
            s += 1
        return f"{h:02d}:{m:02d}:{s:02d},{cs:02d}"

    out, cur = [], 0.0
    for i, s in enumerate(sents):
        start, end = cur, cur + alloc[i]
        cur = end
        # 长句按 max_chars 折行
        lines = []
        for j in range(0, len(s), max_chars):
            lines.append(s[j:j + max_chars])
        body = "\n".join(lines)
        out.append(f"{i + 1}\n{fmt(start)} --> {fmt(end)}\n{body}\n")
    return "\n".join(out)


def _call_tts_service(host, port, *, text, reference_audio="", model_dir="",
                      prompt_text="", text_language="zh", prompt_language="zh",
                      cut_punc="，。！？；", timeout=600):
    """调用 GPT-SoVITS `/tts` 端点，返回音频 bytes。标准库实现，无额外依赖。"""
    params = {
        "text": text,
        "text_language": text_language,
        "prompt_text": prompt_text,
        "prompt_language": prompt_language,
        "cut_punc": cut_punc,
    }
    # few-shot 模式需参考音路径（服务端按路径读盘，非上传）
    if reference_audio:
        params["refer_wav_path"] = reference_audio
    # 若已加载微调模型（model_dir 指向 GPT/SoVITS 权重目录），可走微调模式
    if model_dir:
        params["model_dir"] = model_dir
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{port}/tts",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def start_inference_server(gptsovits_dir, python="python", port=9880, host="127.0.0.1",
                          timeout=120, extra_args=None):
    """（可选）后台启动 GPT-SoVITS api.py 推理服务，等待就绪后返回 Popen。

    多数情况下建议用户手动起服务（环境依赖隔离清晰）；此函数仅作为便捷封装。
    """
    gptsovits_dir = Path(gptsovits_dir)
    api = gptsovits_dir / "api.py"
    if not api.exists():
        raise CloneUnavailable(f"未找到 GPT-SoVITS api.py：{api}")
    cmd = [python, str(api), "-a", host, "-p", str(port)]
    if extra_args:
        cmd += list(extra_args)
    proc = subprocess.Popen(cmd, cwd=str(gptsovits_dir),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 轮询端口直到就绪（最多 timeout 秒）
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/docs", timeout=2):
                return proc
        except Exception:
            if proc.poll() is not None:
                raise CloneUnavailable("GPT-SoVITS 服务进程意外退出，请检查环境/端口")
            time.sleep(2)
    proc.terminate()
    raise CloneUnavailable("GPT-SoVITS 服务在超时内未就绪")


def synthesize_clone_impl(
    text, *, reference_audio, model_dir="", audio_path, srt_path,
    host="127.0.0.1", port=9880, prompt_text="", timeout=600, **kwargs,
):
    """GPT-SoVITS 克隆声线合成：音频 + 句级 SRT。

    Args:
        text: 口播稿。
        reference_audio: 参考音路径（老六声线，建议 10–60s 清晰人声）。
        model_dir: 已训练模型权重目录；为空则走 few-shot（用预训练底座 + 参考音）。
        audio_path / srt_path: 输出路径。
        host / port: 本地 GPT-SoVITS 推理服务地址。
        prompt_text: 参考音对应的文本（few-shot 效果更稳，建议填）。
    Returns:
        (audio_path, srt_path, "gpt-sovits")
    Raises:
        CloneUnavailable: 服务不可达或推理失败。
    """
    audio_path = Path(audio_path)
    srt_path = Path(srt_path)
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.parent.mkdir(parents=True, exist_ok=True)

    if not reference_audio or not Path(reference_audio).exists():
        raise CloneUnavailable(f"参考音不存在：{reference_audio}")

    try:
        audio_bytes = _call_tts_service(
            host, port, text=text, reference_audio=reference_audio,
            model_dir=model_dir, prompt_text=prompt_text, timeout=timeout,
        )
    except (urllib.error.URLError, OSError) as e:
        raise CloneUnavailable(
            f"无法连接 GPT-SoVITS 服务 http://{host}:{port}（{e}）。"
            f"请先启动服务：python <GPT-SoVITS>/api.py -a {host} -p {port}"
        ) from e
    if not audio_bytes:
        raise CloneUnavailable("GPT-SoVITS 返回空音频")

    audio_path.write_bytes(audio_bytes)

    # 字幕：用音频真实时长等比分配句级时间轴
    from video_pipeline.compose import ffprobe_duration
    dur = ffprobe_duration(audio_path) or 0.0
    srt = build_proportional_srt(text, dur)
    srt_path.write_text(srt, encoding="utf-8")
    return audio_path, srt_path, "gpt-sovits"
