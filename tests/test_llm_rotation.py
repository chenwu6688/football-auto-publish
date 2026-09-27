"""验证「免费额度耗尽 → 自动切下一个免费模型」规则（以及 402 不永久禁用）。

背景
----
这是 2026-09-26 欠费故障后的验收测试：
- 确认额度低于阈值(5%)的模型会被跳过、顺位切下一个（用户最关心的规则）。
- 确认 parse 失败 / 空响应会顺位轮换。
- 确认 402 不再把模型永久标记为 disabled（修复项）。

全部用 unittest.mock 隔离网络，不消耗任何真实额度。
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from constants import LLM_JSON_CANDIDATES  # noqa: E402
import utils  # noqa: E402


# 用一组可控的候选（带假 key，避免被 url/key 空值筛掉）
_CAND = [
    ("https://fake/v1", "fake-key", "deepseek-v4-pro"),
    ("https://fake/v1", "fake-key", "kimi-k3"),
    ("https://fake/v1", "fake-key", "glm-5.3"),
]


def _usage_with(**overrides):
    """构造 usage 字典：默认都未用、未禁用，overrides 覆盖个别模型。"""
    usage = {}
    for _u, _k, m in _CAND:
        usage.setdefault(m, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    for m, v in overrides.items():
        usage.setdefault(m, {})
        usage[m].update(v)
    return usage


def test_quota_exhausted_model_is_skipped_and_rotates(tmp_path, monkeypatch):
    """deepseek-v4-pro 额度耗尽(>=95%) → 跳过它，用下一个 kimi-k3。"""
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with(**{
        "deepseek-v4-pro": {"total_tokens": 999_999},  # >= 950000 阈值 → 耗尽
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)

    calls = []
    def fake_call_llm(url, key, model, messages, **kw):
        calls.append(model)
        return '{"ok": true}'  # 任何模型都返回合法 JSON

    monkeypatch.setattr(utils, "call_llm", fake_call_llm)
    parsed, used = utils.call_llm_json(
        [{"role": "user", "content": "x"}],
        candidates=_CAND, usage_file=usage_file, max_candidates=5,
    )
    assert used == "kimi-k3", f"应跳过额度耗尽的 deepseek-v4-pro，实际用了 {used}"
    assert "deepseek-v4-pro" not in calls, "被耗尽的模型不应被调用"
    assert parsed == {"ok": True}


def test_all_candidates_exhausted_raises_quota_error(tmp_path, monkeypatch):
    """全部耗尽 → 抛 QuotaExhaustedError（而非静默产出 0 篇）。"""
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with(**{
        "deepseek-v4-pro": {"total_tokens": 999_999},
        "kimi-k3": {"total_tokens": 999_999},
        "glm-5.3": {"total_tokens": 999_999},
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(utils, "call_llm", lambda *a, **k: '{"ok": true}')

    with pytest.raises(utils.QuotaExhaustedError):
        utils.call_llm_json([{"role": "user", "content": "x"}],
                            candidates=_CAND, usage_file=usage_file)


def test_parse_failure_rotates_to_next(tmp_path, monkeypatch):
    """第一个可用模型返回无法解析的内容 → 顺位切下一个。"""
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with()))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)

    seq = iter(['这不是JSON', '{"ok": true}'])
    def fake_call_llm(url, key, model, messages, **kw):
        return next(seq)
    monkeypatch.setattr(utils, "call_llm", fake_call_llm)

    parsed, used = utils.call_llm_json(
        [{"role": "user", "content": "x"}], candidates=_CAND,
        usage_file=usage_file, parser=utils.try_parse_json,
    )
    assert used == "kimi-k3", f"首个模型解析失败应切下一个，实际 {used}"
    assert parsed == {"ok": True}


def test_disabled_model_is_skipped(tmp_path, monkeypatch):
    """disabled 标记（401/403 留下）的模型被跳过，切下一个。"""
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with(**{
        "deepseek-v4-pro": {"disabled": True},
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(utils, "call_llm", lambda *a, **k: '{"ok": true}')

    parsed, used = utils.call_llm_json(
        [{"role": "user", "content": "x"}], candidates=_CAND, usage_file=usage_file)
    assert used == "kimi-k3", f"被禁用的模型应被跳过，实际 {used}"


def test_402_does_not_permanently_disable(tmp_path, monkeypatch):
    """修复项：HTTP 402（欠费）不再把模型标记 disabled。

    mock call_llm 抛 402 → 应被当次跳过，但 usage 文件里该模型不应出现 disabled。
    """
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with()))
    saved = {}
    monkeypatch.setattr(utils, "_save_llm_usage", lambda u, p: saved.update(u))

    def fake_call_llm_402_then_ok(url, key, model, messages, **kw):
        if not hasattr(fake_call_llm_402_then_ok, "first"):
            fake_call_llm_402_then_ok.first = True
            raise mock_http_error(402)
        return '{"ok": true}'

    monkeypatch.setattr(utils, "call_llm", fake_call_llm_402_then_ok)
    parsed, used = utils.call_llm_json(
        [{"role": "user", "content": "x"}], candidates=_CAND, usage_file=usage_file)
    assert used == "kimi-k3", "402 后应切下一个模型"
    assert not saved.get("deepseek-v4-pro", {}).get("disabled"), \
        "402 不应把模型标记为 disabled（修复项）"


def mock_http_error(status):
    import requests
    resp = mock.Mock(status_code=status, text="x")
    return requests.exceptions.HTTPError(response=resp)


def test_is_model_available_threshold(tmp_path):
    """单元层面确认阈值判断：已用 < 95% 可用；>= 95% 不可用。"""
    q = utils.LLM_FREE_QUOTA_TOKENS
    th = utils.LLM_USAGE_THRESHOLD
    assert utils._is_model_available("m", {"m": {"total_tokens": int(q * 0.96)}}) is False
    assert utils._is_model_available("m", {"m": {"total_tokens": int(q * 0.50)}}) is True
    assert utils._is_model_available("m", {"m": {"disabled": True}}) is False


def test_global_candidate_list_is_nonempty():
    """线上候选列表非空（防止误写成空导致永远失败）。"""
    assert len(LLM_JSON_CANDIDATES) > 0
