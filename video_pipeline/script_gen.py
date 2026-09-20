#!/usr/bin/env python3
"""视频口播稿生成（Phase 0）—— 复用品牌手册 + LLM，产出 60–90s 口语稿。

设计：
- 复用 orchestrator.load_brand_manual()（单一事实源）与 get_brand_series()，保证人设/共鸣/系列与图文一致。
- 复用 utils.call_llm_json + constants.LLM_JSON_CANDIDATES（现有 LLM 额度，零新增成本）。
- 任何 LLM 失败都回退到规则兜底稿（condense_fallback），绝不阻断视频生成。
- llm_fn 可注入，便于离线单测（不依赖真实 API key / 网络）。
"""

import re
import sys
from pathlib import Path

# 确保仓库根在 sys.path（无论以包导入还是直接 python video_pipeline/pipeline.py 运行）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 延迟导入，避免 video_pipeline 被 import 时强拉 orchestrator 重型依赖
from utils import load_prompt_template  # 轻量，仅读 prompts/

_PROMPT_NAME = "video_script.txt"
_LLM_MAX_TOKENS = 1200
_LLM_TEMPERATURE = 0.7

# 口播稿期望区间（用于校验与兜底）
_SCRIPT_MIN_CHARS = 120
_SCRIPT_MAX_CHARS = 360


def _get_brand_manual():
    import orchestrator
    return orchestrator.load_brand_manual()


def _get_series_name(series_id):
    if not series_id:
        return ""
    try:
        import orchestrator
        by_id, _ = orchestrator.get_brand_series()
        s = by_id.get(series_id) or {}
        return s.get("name", "")
    except Exception:
        return ""


def _build_hints(article):
    """根据文章自带的共鸣角度 / 系列 ID 注入提示。"""
    resonance_angle = (article or {}).get("resonance_angle") or ""
    series_id = (article or {}).get("series_id") or ""

    if resonance_angle:
        resonance_hint = (
            f"本次优先带入共鸣角度：{resonance_angle}（自然带出，不强行煽情、不脱离事实）。"
        )
    else:
        resonance_hint = "（无指定共鸣角度，按内容自然选择最合适的情绪切入点。）"

    if series_id:
        name = _get_series_name(series_id)
        label = f"《{name}》" if name else f"系列 {series_id}"
        series_hint = (
            f"本篇归属连载系列 {label}：结尾可用一句『这系列还在更，点关注别错过』式引导追更，"
            "但不得臆造具体『第几期』等不实信息。"
        )
    else:
        series_hint = "（本篇非连载，无需追更引导。）"

    return resonance_hint, series_hint


def _build_messages(article, brand_manual):
    template = load_prompt_template(_PROMPT_NAME)
    if not template:
        # 模板缺失：退回极简指令，仍尽量可用
        template = (
            "把下面图文改写成 60-90 秒短视频口播稿（纯口语，强钩子开头，结尾互动钩子，"
            "事实零改动）。只输出 JSON: {\"title\":..,\"script\":..,\"hook_type\":..,\"estimated_duration_sec\":..}\n"
            "标题：{title}\n正文：{content}"
        )
    resonance_hint, series_hint = _build_hints(article)
    prompt = (
        template
        .replace("{brand_manual}", brand_manual or "（无品牌手册）")
        .replace("{title}", (article or {}).get("title", ""))
        .replace("{content}", (article or {}).get("content", ""))
        .replace("{resonance_hint}", resonance_hint)
        .replace("{series_hint}", series_hint)
    )
    return [
        {"role": "system", "content": "你是足球短视频口播稿编剧，输出严格 JSON。"},
        {"role": "user", "content": prompt},
    ]


def _real_llm(messages):
    """真实 LLM 调用（被 llm_fn 默认指向）。返回 (parsed_dict, model_used)。"""
    from utils import call_llm_json
    from constants import LLM_JSON_CANDIDATES
    return call_llm_json(
        messages,
        LLM_JSON_CANDIDATES,
        temperature=_LLM_TEMPERATURE,
        max_tokens=_LLM_MAX_TOKENS,
        timeout=90,
    )


def _validate(parsed):
    """校验 LLM 返回的口播稿 JSON 是否可用。"""
    if not isinstance(parsed, dict):
        return False
    script = (parsed.get("script") or "").strip()
    title = (parsed.get("title") or "").strip()
    if not title or not script:
        return False
    if not (_SCRIPT_MIN_CHARS <= len(script) <= _SCRIPT_MAX_CHARS * 1.5):
        return False
    return True


def condense_fallback(article):
    """规则兜底：把图文压成一段可用的口播稿，LLM 不可用时保证有产出。"""
    title = (article or {}).get("title", "今日足球热点")
    content = (article or {}).get("content", "")
    # 去 Markdown / 多余空白
    text = re.sub(r"#+\s*", "", content)
    text = re.sub(r"[*_`>#-]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # 去链接保留文字
    text = re.sub(r"\s+", "", text)
    # 以句号/问号/感叹号切句，尽量拼到 ~260 字
    sentences = re.split(r"(?<=[。！？])", text)
    picked, total = [], 0
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        picked.append(s)
        total += len(s)
        if total >= 260:
            break
    body = "".join(picked)[:300]
    if body and body[-1] not in "。！？":
        body += "。"
    hook = "老球迷们，今天这条你一定得看——"
    closing = "你觉得呢？评论区聊聊，关注老六，每天球评不断更。"
    script = f"{hook}{body}{closing}"
    return {
        "title": title,
        "script": script,
        "hook_type": "冲突",
        "estimated_duration_sec": max(45, min(90, len(script) // 4)),
        "source": "fallback",
    }


def generate_script(article, *, llm_fn=None, brand_manual=None):
    """生成口播稿。

    Args:
        article: dict，至少含 title/content；可选 resonance_angle/series_id。
        llm_fn: 可选，签名 (messages) -> (parsed_dict, model)；默认走真实 LLM。便于测试。
        brand_manual: 可选，预渲染品牌手册文本；默认懒加载。
    Returns:
        dict: {title, script, hook_type, estimated_duration_sec, source, model?}
    """
    if brand_manual is None:
        try:
            brand_manual = _get_brand_manual()
        except Exception:
            brand_manual = ""

    messages = _build_messages(article, brand_manual)
    llm = llm_fn if llm_fn is not None else _real_llm

    try:
        parsed, model = llm(messages)
        if _validate(parsed):
            parsed["source"] = "llm"
            parsed["model"] = model
            # 清理可能的多余字段
            parsed.setdefault("hook_type", "疑问")
            parsed.setdefault("estimated_duration_sec", 75)
            return parsed
        print("   ⚠️ 口播稿 LLM 返回未通过校验，回退规则兜底")
    except Exception as e:
        print(f"   ⚠️ 口播稿 LLM 调用失败（{type(e).__name__}: {e}），回退规则兜底")

    return condense_fallback(article)
