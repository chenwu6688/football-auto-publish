#!/usr/bin/env python3
"""足球自媒体 — 共享工具函数

retry, call_llm, safe_json_loads, load_prompt_template — 被所有模块使用。
"""

import json, time, requests
from pathlib import Path

from constants import (
    LLM_USAGE_FILE,
    LLM_FREE_QUOTA_TOKENS,
    LLM_USAGE_THRESHOLD,
    LLM_HARD_CAP_TOKENS,
    LLM_SORT_BY_REMAINING,
)

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
             fallback_url=None, fallback_key=None, fallback_model=None, usage_ref=None,
             max_retries=3):
    """Call LLM with optional fallback to another provider on auth/credit errors.

    When the primary provider returns 401/402/403/404, automatically retry
    with the fallback provider. Set fallback_url/fallback_key/fallback_model
    to enable this behavior (e.g. hy3/Hunyuan → Qwen on quota exhaustion).

    Args:
        usage_ref: Optional dict-like object. If provided, it will be populated
                   with {"model": str, "usage": dict} from the response.
        max_retries: Number of attempts for primary (and fallback) call.
                     call_llm_json passes 1 to fail-fast across providers.
    """
    def _build_body(m, *, minimal=False):
        if minimal:
            # 最小参数集：仅 model/messages/max_tokens/stream。
            # 用于 400 后的降级重试——某些模型（如 glm-5.3 系列）不接受
            # temperature / thinking 等可选参数，传了直接 400 Bad Request。
            return {"model": m, "messages": messages,
                    "max_tokens": max_tokens, "stream": False}
        body = {
            "model": m, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "stream": False
        }
        # TokenHub 模型参数适配（2026-08-28 实测确认，2026-09-14 扩充）：
        # - kimi 系列只接受 temperature=1，传 0.7 会 400
        # - deepseek/glm/qwen3 为推理模型，不关 thinking 时 reasoning 吃掉大量 token
        #   且 glm 甚至可能 content 为空；关掉后 token 省一半以上且输出稳定
        #   注意：deepseek 前缀用 "deepseek"（不带连字符），以同时覆盖
        #   "deepseek-v4-..." 与 "deepseek/..."(如 deepseek/deepseek-flash) 两种命名
        if m.startswith("kimi-"):
            body["temperature"] = 1.0
        elif m.startswith(("deepseek", "glm-", "qwen3")):
            body["thinking"] = {"type": "disabled"}
        return body

    def _post(u, k, m, *, minimal=False):
        resp = requests.post(u, json=_build_body(m, minimal=minimal),
                             headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
                             timeout=timeout)
        return resp

    def _call(u, k, m):
        resp = _post(u, k, m)
        if resp.status_code == 400:
            # 400 = 请求参数不被该模型接受（实测 glm-5.3 系列拒绝 thinking 参数）。
            # 打印错误体后再用「最小参数集」重试一次，避免整池白白跳过满额模型。
            detail = resp.text[:300]
            print(f"   ⚠️ LLM({m}) HTTP 400，参数可能不被支持，降级重试。body={detail}")
            resp = _post(u, k, m, minimal=True)
        resp.raise_for_status()
        data = resp.json()
        if usage_ref is not None:
            usage_ref["model"] = m
            usage_ref["usage"] = data.get("usage", {})
        return data["choices"][0]["message"]["content"]

    try:
        print(f"   🔧 调用 LLM: {model}（兜底模型={fallback_model}）")
        return retry(lambda: _call(url, api_key, model), desc=f"LLM({model})", max_retries=max_retries)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (401, 402, 403, 404) and fallback_url and fallback_key and fallback_model:
            print(f"   ⚠️ LLM({model}) HTTP {status}，自动降级至 {fallback_model}")
            return retry(lambda: _call(fallback_url, fallback_key, fallback_model), desc=f"LLM({fallback_model})", max_retries=max_retries)
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


def _model_used_tokens(model, usage):
    """Return accumulated total_tokens for a model (0 if unknown)."""
    return usage.get(model, {}).get("total_tokens", 0)


def _remaining_tokens(model, usage, quota=LLM_FREE_QUOTA_TOKENS):
    """Return free tokens remaining for a model (never negative)."""
    return max(0, quota - _model_used_tokens(model, usage))


def _fail_streak(model, usage):
    """Return the current consecutive-failure count for a model (0 if none).

    用于把「稳定失灵」的模型（空响应/400/超时）沉到候选末尾，避免每次都先
    浪费一轮超时。成功后清零。持久化在 llm_usage.json 的 fail_streak 字段。
    """
    return usage.get(model, {}).get("fail_streak", 0)


def _bump_fail_streak(usage, model, *, reset=False):
    """Increment (or reset) a model's consecutive-failure counter in usage."""
    rec = usage.setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    if reset:
        rec["fail_streak"] = 0
    else:
        rec["fail_streak"] = rec.get("fail_streak", 0) + 1
    return usage


def _is_model_available(model, usage, quota=LLM_FREE_QUOTA_TOKENS, threshold=LLM_USAGE_THRESHOLD,
                        hard_cap=LLM_HARD_CAP_TOKENS):
    """Return True if the model has free quota remaining and is not disabled.

    Two guards (2026-09-27 免费额度规则):
      1) 阈值 threshold：已用 >= quota*threshold（默认 0.90，即剩余 <=10%）→ 不可用。
      2) 硬上限 hard_cap：已用 >= hard_cap（默认 80 万 tokens）→ 不可用。
         作为「本地计数与后台实际不一致」时的保险丝，避免本地少记导致超额扣费。
    """
    if usage.get(model, {}).get("disabled"):
        return False
    used = _model_used_tokens(model, usage)
    if used >= quota * threshold:
        return False
    if hard_cap and used >= hard_cap:
        return False
    return True


def _add_llm_usage(usage, model, usage_info):
    """Add usage_info (prompt/completion/total_tokens) to accumulated usage for model."""
    if not usage_info or not isinstance(usage_info, dict):
        return usage
    prev = usage.setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        prev[k] = prev.get(k, 0) + usage_info.get(k, 0)
    return usage


class QuotaExhaustedError(ValueError):
    """Raised when all LLM candidates have exhausted their free quota or are disabled."""
    pass


def call_llm_json(messages, candidates, *, temperature=0.7, max_tokens=4096, timeout=60,
                  parser=try_parse_json,
                  usage_file=LLM_USAGE_FILE,
                  quota=LLM_FREE_QUOTA_TOKENS,
                  threshold=LLM_USAGE_THRESHOLD,
                  hard_cap=LLM_HARD_CAP_TOKENS,
                  sort_by_remaining=LLM_SORT_BY_REMAINING,
                  max_per_provider=None, max_candidates=18, llm_max_retries=1):
    """Try LLM candidates sequentially until one returns parseable JSON.

    Rotation policy (2026-09-27 免费额度规则 I+II):
    - skips models whose free quota is below threshold (default used >=90%)
    - skips models whose local usage hit the hard cap (default 800k tokens)
    - skips models marked disabled (e.g. returned 401/403)
    - if sort_by_remaining: order candidates by remaining tokens DESC
      (tie broken by original candidate order) → always spend the
      least-used model first, so no single model burns to over-quota
    - single HTTP attempt per candidate (fail-fast)

    Args:
        messages: OpenAI-compatible messages list.
        candidates: list of (url, api_key, model_name) tuples.
        parser: function(text) -> parsed object or None.
        hard_cap: local token cap; usage >= hard_cap ⇒ treat as exhausted.
        sort_by_remaining: True = sort available by remaining desc.
        max_per_provider: legacy per-provider cap; default None = no limit
                          (sequential calls never fan out per provider).
        max_candidates: max number of available candidates to actually call.
        llm_max_retries: retries inside call_llm for each candidate (default 1:
                         zero retries, fail-fast so we rotate to next model).

    Returns:
        (parsed_object, model_used)

    Raises:
        QuotaExhaustedError if all candidates are quota-exhausted or disabled.
        ValueError if available candidates all fail to return usable JSON.
    """
    usage = _load_llm_usage(usage_file)
    provider_attempts = {}  # 防御：max_per_provider 分支引用，未初始化会触发 NameError

    # Check ALL candidates so we can tell whether the pool is truly exhausted.
    available = []
    exhausted = []
    disabled = []
    for url, key, model in candidates:
        if not url or not key:
            continue
        if max_per_provider is not None:
            provider_key = (url, key)
            if provider_attempts.get(provider_key, 0) >= max_per_provider:
                continue
            provider_attempts[provider_key] = provider_attempts.get(provider_key, 0) + 1
        if usage.get(model, {}).get("disabled"):
            disabled.append(model)
            continue
        if not _is_model_available(model, usage, quota, threshold, hard_cap):
            used = _model_used_tokens(model, usage)
            remaining = _remaining_tokens(model, usage, quota)
            cap_hit = hard_cap and used >= hard_cap
            why = f"已达本地硬上限 {hard_cap}" if cap_hit else f"免费额度剩余 <=10% ({(1-threshold)*100:.0f}%)"
            print(f"   ⏭️ LLM({model}) 跳过：{why}（已用 {used}/{quota}，剩余 {remaining} tokens）")
            exhausted.append(model)
            continue
        available.append((url, key, model))

    # 方案 II：按剩余额度降序（同分按原候选顺序）——优先用剩余最多的模型，
    # 避免单个模型被反复命中而先行超额。list.sort 是稳定排序，天然保持同分原顺序。
    #
    # 同时把「连续失败」的模型沉底：某些模型会稳定地返回空/400/超时
    # （实测 hy4-preview 空响应、glm-5.3 系列 400），若一直排在候选最前，
    # 每次都要先浪费一轮 60s 超时，导致整跑耗时 20+ 分钟。失败次数越多越靠后，
    # 成功后清零（见下方 _bump_fail_streak）。
    if sort_by_remaining and len(available) > 1:
        available.sort(key=lambda c: (
            _fail_streak(c[2], usage),               # 失败次数升序：少的优先（沉底失灵模型）
            -_remaining_tokens(c[2], usage, quota),  # 其次按剩余额度降序
        ))
        top = available[0][2]
        print(f"   🧮 候选排序：首位 {top}（失败次数 {_fail_streak(top, usage)}，"
              f"剩余 {_remaining_tokens(top, usage, quota)} tokens）")

    if not available:
        reasons = []
        if exhausted:
            reasons.append(f"免费额度耗尽（剩余 <{(1-threshold)*100:.0f}%）: {', '.join(exhausted)}")
        if disabled:
            reasons.append(f"key 无效/模型被禁用: {', '.join(disabled)}")
        if reasons:
            raise QuotaExhaustedError("；".join(reasons))
        raise ValueError("没有可用的 LLM 候选（请检查 API key 与额度）")

    last_err = None
    for i, (url, key, model) in enumerate(available):
        if i >= max_candidates:
            print(f"   ⏭️ 已达最大尝试数 {max_candidates}，停止 LLM 轮换")
            break
        try:
            print(f"   🔧 尝试 LLM: {model}")
            usage_ref = {}
            resp_text = call_llm(url, key, model, messages,
                                 temperature=temperature, max_tokens=max_tokens, timeout=timeout,
                                 fallback_url=None, fallback_key=None, fallback_model=None,
                                 usage_ref=usage_ref,
                                 max_retries=llm_max_retries)
            if not resp_text or not resp_text.strip():
                print(f"   ⚠️ LLM({model}) 返回空内容，跳过")
                _bump_fail_streak(usage, model)
                _save_llm_usage(usage, usage_file)
                continue
            parsed = parser(resp_text)
            if parsed is None:
                print(f"   ⚠️ LLM({model}) 返回内容无法解析为 JSON，尝试下一个模型")
                _bump_fail_streak(usage, model)
                _save_llm_usage(usage, usage_file)
                continue
            usage = _add_llm_usage(usage, usage_ref.get("model", model), usage_ref.get("usage", {}))
            _bump_fail_streak(usage, model, reset=True)  # 成功 → 失败连击清零
            _save_llm_usage(usage, usage_file)
            print(f"   ✅ LLM({model}) 返回可用 JSON")
            return parsed, model
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (401, 403):
                # 401/403 = key 无效 / 无权限 —— 换 key 之前必然一直失败，值得持久禁用。
                hint = "key 无效" if status == 401 else "无权限"
                print(f"   🚫 LLM({model}) 返回 HTTP {status}（{hint}），标记禁用")
                usage.setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
                usage[model]["disabled"] = True
                _save_llm_usage(usage, usage_file)
            elif status == 402:
                # 402 = 账户欠费 / 余额不足 —— 这是**账户级**问题，不是模型坏了。
                # 充值后即可恢复，因此**绝不持久禁用**，否则充了钱也永远跑不起来
                # （曾导致 2026-09-26 欠费后即使充值，后续批次仍被全部跳过）。
                print(f"   💸 LLM({model}) 返回 HTTP 402（账户欠费/余额不足），本次跳过；"
                      f"充值后自动恢复（不标记禁用）")
            else:
                preview = str(e)[:200]
                print(f"   ⚠️ LLM({model}) HTTP 调用失败: {preview}")
                _bump_fail_streak(usage, model)  # 含 400/404/5xx：沉底以防反复超时
                _save_llm_usage(usage, usage_file)
            last_err = e
        except Exception as e:
            preview = str(e)[:200]
            print(f"   ⚠️ LLM({model}) 调用失败: {preview}")
            _bump_fail_streak(usage, model)  # 超时/连接错误：沉底以防反复超时
            _save_llm_usage(usage, usage_file)
            last_err = e

    raise ValueError(f"前 {len(available)} 个可用候选均未能返回可用 JSON。最后错误: {last_err}")
