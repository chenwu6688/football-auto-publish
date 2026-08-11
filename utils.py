#!/usr/bin/env python3
"""足球自媒体 — 共享工具函数

retry, call_llm, safe_json_loads, load_prompt_template — 被所有模块使用。
"""

import json, time, requests
from pathlib import Path

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
             fallback_url=None, fallback_key=None, fallback_model=None):
    """Call LLM with optional fallback to another provider on auth/credit errors.

    When the primary provider returns 401/402/403/404, automatically retry
    with the fallback provider. Set fallback_url/fallback_key/fallback_model
    to enable this behavior (e.g. hy3/Hunyuan → Qwen on quota exhaustion).
    """
    def _call(u, k, m):
        resp = requests.post(u, json={
            "model": m, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "stream": False
        }, headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json"}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    try:
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
