#!/usr/bin/env python3
"""足球自媒体 — 共享工具函数

retry, call_llm, safe_json_loads, load_prompt_template — 被所有模块使用。
"""

import json, time, requests
from pathlib import Path

from constants import LLM_USAGE_FILE, LLM_FREE_QUOTA_TOKENS, LLM_USAGE_THRESHOLD

PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompt_template(name):
    """Load a prompt template from prompts/{name}, stripping header comments.

    Header lines (starting with #) contain version metadata and are stripped.
    The body is returned as the prompt content.
    """
    path = PROMPT_DIR / name
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").split("\n")
    body = []
    in_header = True
    for line in lines:
        if in_header and (line.startswith("#") or line.strip() == ""):
            if line.strip() == "" and body:
                in_header = False
            continue
        in_header = False
        body.append(line)
    return "\n".join(body).strip()


def retry(func, *args, max_retries=3, base_delay=2, desc="API", **kwargs):
    last_err = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            if isinstance(e, requests.exceptions.HTTPError):
                status = e.response.status_code if hasattr(e, 'response') and e.response is not None else None
                # 401(认证失败)/402(配额耗尽)/403(权限)/404(不存在) — 不重试，立即抛出
                if status in (401, 402, 403, 404):
                    print(f"   [{desc}] HTTP {status} — 不可恢复错误，放弃重试: {e}")
                    raise
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"   [{desc}] 重试 {attempt+1}/{max_retries} (等待{delay}s): {e}")
                time.sleep(delay)
    raise last_err


def call_llm(url, api_key, model, messages, temperature=0.7, max_tokens=4096, timeout=120,
             fallback_url=None, fallback_key=None, fallback_model=None, usage_ref=None):
    """Call LLM with optional fallback to another provider on auth/credit errors.

    When the primary provider returns 401/402/403/404, automatically retry
    with the fallback provider. Set fallback_url/fallback_key/fallback_model
    to enable this behavior (e.g. hy3/Hunyuan → Qwen on quota exhaustion).

    Args:
        usage_ref: Optional dict-like object. If provided, it will be populated
                   with {"model": str, "usage": dict} from the response.
    """
    def _call(u, k, m):
        resp = requests.post(u, json={
            "model": m, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "stream": False
        }, headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json"}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if usage_ref is not None:
            usage_ref["model"] = m
            usage_ref["usage"] = data.get("usage", {})
        return data["choices"][0]["message"]["content"]

    try:
        print(f"   🔧 调用 LLM: {model}（兜底模型={fallback_model}）")
        return retry(lambda: _call(url, api_key, model), desc=f"LLM({model})")
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (401, 402, 403, 404) and fallback_url and fallback_key and fallback_model:
            print(f"   ⚠️ LLM({model}) HTTP {status}，自动降级至 {fallback_model}")
            return retry(lambda: _call(fallback_url, fallback_key, fallback_model), desc=f"LLM({fallback_model})")
        raise


def safe_json_loads(text):
    import re
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    def _try_all(s):
        strategies = [
            ("strict", lambda t: json.loads(t)),
            ("non-strict", lambda t: json.loads(t, strict=False)),
            ("fix-control-chars", lambda t: json.loads(re.sub(
                r'[\x00-\x08\x0b\x0c\x0e-\x1f]', lambda m: f'\\u{ord(m.group(0)):04x}', t))),
        ]
        for name, fn in strategies:
            try:
                return fn(s)
            except json.JSONDecodeError:
                continue
        return None

    result = _try_all(text)
    if result is not None:
        return result

    # Extract JSON block: find outermost [ ] or { }
    m = re.search(r'\[[\s\S]*\]|\{[\s\S]*\}', text)
    if m:
        block = m.group(0)
        result = _try_all(block)
        if result is not None:
            return result

    # Remove trailing commas and try again
    fixed = re.sub(r',\s*([]}])', r'\1', text)
    result = _try_all(fixed)
    if result is not None:
        return result

    # Extract block + remove trailing commas
    if m:
        block = re.sub(r',\s*([]}])', r'\1', m.group(0))
        result = _try_all(block)
        if result is not None:
            return result

    raise ValueError(f"Unable to parse JSON after all fixes. Raw (first 300 chars): {text[:300]}")


def try_parse_json(text):
    """Best-effort JSON parser that returns None on failure instead of raising.

    Strips common LLM reasoning blocks (<think>...</think>) and markdown fences
    before delegating to safe_json_loads. Treats empty/whitespace responses as
    failures so callers can rotate to the next model.
    """
    import re
    if not text or not text.strip():
        return None
    # Strip reasoning / thinking blocks (e.g., DeepSeek/Hunyuan reasoning)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    if not text:
        return None
    # If markdown fence exists anywhere, prefer the fenced block
    if '```' in text and not text.startswith('```'):
        m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text, flags=re.DOTALL)
        if m:
            text = m.group(1).strip()
    try:
        return safe_json_loads(text)
    except Exception:
        return None


def _load_llm_usage(path=LLM_USAGE_FILE):
    """Load accumulated LLM token usage from disk."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_llm_usage(usage, path=LLM_USAGE_FILE):
    """Persist accumulated LLM token usage to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_model_quota_available(model, usage, quota=LLM_FREE_QUOTA_TOKENS, threshold=LLM_USAGE_THRESHOLD):
    """Return True if the model's accumulated usage is below the free-quota threshold."""
    used = usage.get(model, {}).get("total_tokens", 0)
    limit = quota * threshold
    return used < limit


def _add_llm_usage(usage, model, usage_info):
    """Add usage_info (prompt/completion/total_tokens) to accumulated usage for model."""
    if not usage_info or not isinstance(usage_info, dict):
        return usage
    prev = usage.setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        prev[k] = prev.get(k, 0) + usage_info.get(k, 0)
    return usage


def call_llm_json(messages, candidates, *, temperature=0.7, max_tokens=4096, timeout=120,
                  parser=try_parse_json,
                  usage_file=LLM_USAGE_FILE,
                  quota=LLM_FREE_QUOTA_TOKENS,
                  threshold=LLM_USAGE_THRESHOLD):
    """Try a list of LLM candidates until one returns parseable JSON.

    Skips models whose accumulated token usage has reached the free-quota
    threshold (default 90%) to avoid pay-as-you-go charges.

    Args:
        messages: OpenAI-compatible messages list.
        candidates: list of (url, api_key, model_name) tuples.
        parser: function(text) -> parsed object or None.

    Returns:
        (parsed_object, model_used)

    Raises:
        ValueError if all candidates fail.
    """
    usage = _load_llm_usage(usage_file)
    last_err = None
    for url, key, model in candidates:
        if not url or not key:
            continue
        if not _is_model_quota_available(model, usage, quota, threshold):
            print(f"   ⏭️ LLM({model}) 免费额度已接近上限 ({usage.get(model, {}).get('total_tokens', 0)}/{quota} tokens)，跳过")
            continue
        try:
            print(f"   🔧 尝试 LLM: {model}")
            usage_ref = {}
            resp_text = call_llm(url, key, model, messages,
                                 temperature=temperature, max_tokens=max_tokens, timeout=timeout,
                                 fallback_url=None, fallback_key=None, fallback_model=None,
                                 usage_ref=usage_ref)
            if not resp_text or not resp_text.strip():
                print(f"   ⚠️ LLM({model}) 返回空内容，跳过")
                continue
            parsed = parser(resp_text)
            if parsed is None:
                print(f"   ⚠️ LLM({model}) 返回内容无法解析为 JSON，尝试下一个模型")
                continue
            # Success: record usage and persist it
            usage = _add_llm_usage(usage, usage_ref.get("model", model), usage_ref.get("usage", {}))
            _save_llm_usage(usage, usage_file)
            print(f"   ✅ LLM({model}) 返回可用 JSON")
            return parsed, model
        except Exception as e:
            preview = str(e)[:200]
            print(f"   ⚠️ LLM({model}) 调用失败: {preview}")
            last_err = e
    raise ValueError(f"所有 LLM 候选均未能返回可用 JSON。最后错误: {last_err}")
