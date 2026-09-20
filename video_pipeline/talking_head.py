#!/usr/bin/env python3
"""真·说话数字人（Phase 2）—— SadTalker / Wav2Lip 本地推理集成。

设计要点：
- 主引擎 **SadTalker**：肖像图 + 音频 → 口型/头部微动视频。CPU 可跑（--device cpu --batched），
  但 60–90s 视频在 CPU 上可能要几十分钟（M710q 无独显的现实），故默认关闭，按需开启。
- 兜底引擎 **Wav2Lip**：更轻量，只对现有人脸/肖像做口型贴合，速度更快。
- 通过 subprocess 调用各自仓库的推理脚本（它们通常装在隔离 venv/conda 里，用各自的 python）。
- 任意失败抛 `TalkingHeadUnavailable`，由 pipeline 回退到「静态肖像 + Ken-Burns」，保证出片。
- 输出为竖屏适配前的中间视频（正方形/小尺寸），最终竖屏合成仍在 compose.py 完成（scale+pad+烧字幕）。
"""

import glob
import shutil
import subprocess
import tempfile
from pathlib import Path

# SadTalker 推理脚本相对仓库根的路径（不同版本可能是 inference.py）
_SADTALKER_ENTRY = "inference.py"


class TalkingHeadUnavailable(Exception):
    """说话数字人不可用（引擎未装/推理失败/无权重）。供 pipeline 回退静态肖像。"""


def _find_output_video(result_dir, prefer_ext=".mp4"):
    """在 SadTalker 输出目录里找最新生成的视频文件。"""
    cands = []
    for ext in ("*.mp4", "*.avi", "*.mov"):
        cands.extend(glob.glob(str(Path(result_dir) / "**" / ext), recursive=True))
    if not cands:
        return None
    return max(cands, key=lambda p: Path(p).stat().st_mtime)


def _run(cmd, cwd=None, timeout=3600):
    """运行命令，返回 (returncode, stderr)。"""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise TalkingHeadUnavailable(f"推理超时（>{timeout}s）：{' '.join(cmd)}") from e
    return proc.returncode, proc.stderr


def generate_sadtalker(portrait_path, audio_path, out_path, *,
                       sadtalker_dir, python="python", device="cpu",
                       size=256, preprocess="full", pose="none",
                       batch_size=1, timeout=3600):
    """SadTalker 生成说话脸视频。

    Args:
        portrait_path: 肖像图（人脸清晰、正面）。
        audio_path: 驱动音频。
        out_path: 输出视频路径。
        sadtalker_dir: SadTalker 仓库根目录。
        python: 该仓库的 python 解释器（含 torch 的环境）。
        device: cpu / cuda。
        size: 输出尺寸（256 轻量；512 更清晰更慢）。
        preprocess: crop（裁剪人脸）/ full（全图）。
        pose: 姿态参考（none/static/clone 等）。
        timeout: 单条推理超时（秒）。
    Returns:
        str: out_path
    """
    sadtalker_dir = Path(sadtalker_dir)
    entry = sadtalker_dir / _SADTALKER_ENTRY
    if not entry.exists():
        raise TalkingHeadUnavailable(f"未找到 SadTalker 入口：{entry}")
    if not Path(portrait_path).exists():
        raise TalkingHeadUnavailable(f"肖像不存在：{portrait_path}")
    if not Path(audio_path).exists():
        raise TalkingHeadUnavailable(f"音频不存在：{audio_path}")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 用临时 result_dir 收口输出，避免和 SadTalker 历史结果混在一起
    with tempfile.TemporaryDirectory() as td:
        cmd = [
            python, str(entry),
            "--driven_by", "audio",
            "--source", str(portrait_path),
            "--audio", str(audio_path),
            "--result_dir", td,
            "--preprocess", preprocess,
            "--size", str(size),
            "--pose_style", pose,
            "--device", device,
            "--batch_size", str(batch_size),
        ]
        if device == "cpu":
            cmd.append("--batched")  # CPU 下 batched 显存更友好
        rc, err = _run(cmd, cwd=str(sadtalker_dir), timeout=timeout)
        if rc != 0:
            raise TalkingHeadUnavailable(f"SadTalker 推理失败(rc={rc}):\n{err[-1500:]}")
        found = _find_output_video(td)
        if not found:
            raise TalkingHeadUnavailable("SadTalker 未产出视频文件")
        shutil.copy(found, out_path)
    return str(out_path)


def generate_wav2lip(portrait_path, audio_path, out_path, *,
                     wav2lip_dir, python="python", checkpoint_path="",
                     face_enlarge=True, timeout=3600):
    """Wav2Lip 生成说话脸视频（兜底引擎，更轻量）。

    Args:
        wav2lip_dir: Wav2Lip 仓库根目录。
        checkpoint_path: 预训练权重 .pt 路径（必填）。
        face_enlarge: 放大人脸区域。
    Returns:
        str: out_path
    """
    wav2lip_dir = Path(wav2lip_dir)
    entry = wav2lip_dir / "inference.py"
    if not entry.exists():
        raise TalkingHeadUnavailable(f"未找到 Wav2Lip 入口：{entry}")
    if not checkpoint_path or not Path(checkpoint_path).exists():
        raise TalkingHeadUnavailable(f"Wav2Lip 权重不存在：{checkpoint_path}")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        python, str(entry),
        "--face", str(portrait_path),
        "--audio", str(audio_path),
        "--checkpoint_path", str(checkpoint_path),
        "--outfile", str(out_path),
    ]
    if face_enlarge:
        cmd.append("--face_enlarge")
    rc, err = _run(cmd, cwd=str(wav2lip_dir), timeout=timeout)
    if rc != 0 or not out_path.exists():
        raise TalkingHeadUnavailable(f"Wav2Lip 推理失败(rc={rc}):\n{err[-1500:]}")
    return str(out_path)


def generate_talking_head(portrait_path, audio_path, out_path, *,
                          engine="sadtalker", cfg=None, **kwargs):
    """统一入口：按 engine 生成说话脸，失败按顺序兜底。

    策略：sadtalker 失败 → wav2lip（若配置了 checkpoint）→ 仍失败抛 TalkingHeadUnavailable。
    """
    cfg = cfg or {}
    if engine == "sadtalker":
        try:
            return generate_sadtalker(
                portrait_path, audio_path, out_path,
                sadtalker_dir=cfg.get("sadtalker_dir", ""),
                python=cfg.get("python", "python"),
                device=cfg.get("device", "cpu"),
                size=cfg.get("size", 256),
                preprocess=cfg.get("preprocess", "full"),
                pose=cfg.get("pose", "none"),
                batch_size=cfg.get("batch_size", 1),
            ), "sadtalker"
        except TalkingHeadUnavailable as e:
            wcfg = cfg.get("wav2lip", {})
            if wcfg.get("dir") and wcfg.get("checkpoint"):
                print(f"   ⚠️ SadTalker 失败：{e}，尝试 Wav2Lip 兜底")
                return generate_wav2lip(
                    portrait_path, audio_path, out_path,
                    wav2lip_dir=wcfg["dir"], python=cfg.get("python", "python"),
                    checkpoint_path=wcfg["checkpoint"],
                ), "wav2lip"
            raise
    elif engine == "wav2lip":
        return generate_wav2lip(
            portrait_path, audio_path, out_path,
            wav2lip_dir=cfg.get("wav2lip", {}).get("dir", ""),
            python=cfg.get("python", "python"),
            checkpoint_path=cfg.get("wav2lip", {}).get("checkpoint", ""),
        ), "wav2lip"
    else:
        raise TalkingHeadUnavailable(f"未知说话脸引擎：{engine}")
