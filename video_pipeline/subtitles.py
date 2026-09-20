#!/usr/bin/env python3
"""字幕处理（Phase 1）—— 句级 SRT 的后期整理，适配手机竖屏可读性。

- ticks_to_seconds：Edge TTS 时间轴单位为 100 纳秒 tick，转秒（供测试/复用）。
- wrap_srt_lines：把过长字幕句按字数折行（中文无空格，按字符切），避免单行溢出。
"""

import re
from pathlib import Path

_TICKS_PER_SECOND = 10_000_000  # 100 纳秒 = 1 tick


def ticks_to_seconds(ticks):
    """Edge TTS offset/duration 单位换算：100ns tick → 秒。"""
    return ticks / _TICKS_PER_SECOND


def _parse_srt(srt_text):
    """极简 SRT 解析 → list of (index, start, end, content)。"""
    blocks = re.split(r"\r?\n\r?\n", srt_text.strip())
    out = []
    tc_re = re.compile(
        r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"
    )
    for blk in blocks:
        lines = [l for l in blk.splitlines() if l.strip() != ""]
        if not lines:
            continue
        # 找时间轴行
        tcm = None
        idx = None
        for i, ln in enumerate(lines):
            m = tc_re.search(ln)
            if m:
                tcm = m
                if i > 0 and lines[i - 1].strip().isdigit():
                    idx = int(lines[i - 1].strip())
                content = "\n".join(lines[i + 1:])
                break
        if not tcm:
            continue
        out.append((idx, tcm, content))
    return out


def _wrap_text(text, max_chars):
    """把一段文字按 max_chars 折行（中文按字符切，英文尽量按词）。"""
    text = text.strip()
    if not text:
        return text
    # 先按已有换行拆，再对超长行切分
    lines = []
    for raw in text.split("\n"):
        raw = raw.strip()
        if len(raw) <= max_chars:
            lines.append(raw)
            continue
        # 英文优先按空格切，中文退化为按字符
        if re.search(r"[A-Za-z]", raw) and " " in raw:
            words = raw.split(" ")
            cur = ""
            for w in words:
                if cur and len(cur) + len(w) + 1 > max_chars:
                    lines.append(cur)
                    cur = w
                else:
                    cur = (cur + " " + w) if cur else w
            if cur:
                lines.append(cur)
        else:
            for i in range(0, len(raw), max_chars):
                lines.append(raw[i:i + max_chars])
    return "\n".join(lines)


def wrap_srt_lines(srt_text, max_chars=18):
    """对 SRT 每条字幕内容做字数折行，返回处理后的 SRT 文本。"""
    cues = _parse_srt(srt_text)
    out = []
    for i, (idx, tcm, content) in enumerate(cues, start=1):
        wrapped = _wrap_text(content, max_chars)
        out.append(f"{i}\n{tcm.group(0)}\n{wrapped}")
    return "\n\n".join(out) + ("\n" if out else "")


def postprocess(srt_path, max_chars=18):
    """就地优化字幕文件（折行）。返回 Path。"""
    p = Path(srt_path)
    if not p.exists():
        return p
    text = p.read_text(encoding="utf-8")
    wrapped = wrap_srt_lines(text, max_chars=max_chars)
    p.write_text(wrapped, encoding="utf-8")
    return p
