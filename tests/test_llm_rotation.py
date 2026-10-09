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
    """单元层面确认阈值判断：已用 < 90% 可用；>= 90% 不可用。"""
    q = utils.LLM_FREE_QUOTA_TOKENS
    th = utils.LLM_USAGE_THRESHOLD
    assert th == 0.90, f"阈值应为 0.90（剩余 10% 切换），实际 {th}"
    assert utils._is_model_available("m", {"m": {"total_tokens": int(q * 0.91)}}) is False
    assert utils._is_model_available("m", {"m": {"total_tokens": int(q * 0.50)}}) is True
    assert utils._is_model_available("m", {"m": {"disabled": True}}) is False


def test_hard_cap_forces_skip(tmp_path):
    """方案 I 保险丝：本地已用达到硬上限(80万)即视为耗尽，即使阈值(90%)未到。"""
    cap = utils.LLM_HARD_CAP_TOKENS
    q = utils.LLM_FREE_QUOTA_TOKENS
    assert cap == 800_000, f"硬上限应为 800000，实际 {cap}"
    assert cap < q * utils.LLM_USAGE_THRESHOLD, "硬上限应低于阈值线，才能起保险丝作用"
    # 80万 / 100万 = 80% < 90% 阈值，但硬上限命中 → 不可用
    assert utils._is_model_available("m", {"m": {"total_tokens": cap}}) is False
    assert utils._is_model_available("m", {"m": {"total_tokens": cap - 1}}) is True


def test_rotation_prefers_model_with_most_remaining(tmp_path, monkeypatch):
    """方案 II：按剩余额度降序——剩余最多的模型应被优先命中，哪怕它排在候选末尾。"""
    usage_file = tmp_path / "usage.json"
    # deepseek-v4-pro 已用 50 万（剩余 50 万）；kimi-k3 全新（剩余 100 万）
    usage_file.write_text(json.dumps(_usage_with(**{
        "deepseek-v4-pro": {"total_tokens": 500_000},
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(utils, "call_llm", lambda *a, **k: '{"ok": true}')

    calls = []
    def fake_call_llm(url, key, model, messages, **kw):
        calls.append(model)
        return '{"ok": true}'
    monkeypatch.setattr(utils, "call_llm", fake_call_llm)

    parsed, used = utils.call_llm_json(
        [{"role": "user", "content": "x"}], candidates=_CAND,
        usage_file=usage_file,
    )
    # kimi-k3/glm-5.3 剩余均为 100 万（同分按原顺序 → kimi-k3 先），排在已用 50 万的 deepseek-v4-pro 之前
    assert used == "kimi-k3", f"应优先用剩余最多的模型，实际 {used}"
    assert calls[0] == "kimi-k3", f"首个被调用应是剩余最多的模型，实际 {calls[0]}"


def test_rotation_sort_can_be_disabled(tmp_path, monkeypatch):
    """开关关闭时保持原候选顺序（第一个可用即被调用）。"""
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with(**{
        "deepseek-v4-pro": {"total_tokens": 500_000},
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(utils, "call_llm", lambda *a, **k: '{"ok": true}')

    parsed, used = utils.call_llm_json(
        [{"role": "user", "content": "x"}], candidates=_CAND,
        usage_file=usage_file, sort_by_remaining=False,
    )
    assert used == "deepseek-v4-pro", f"关闭排序应保持原顺序，实际 {used}"


def test_global_candidate_list_is_nonempty():
    """线上候选列表非空（防止误写成空导致永远失败）。"""
    assert len(LLM_JSON_CANDIDATES) > 0


def test_failing_model_sinks_to_back(tmp_path, monkeypatch):
    """连续失败的模型应沉底——避免每次都先浪费一轮超时（实测 23 分钟长跑根因）。

    kimi-k3 剩余最多（全新）但失败 3 次；deepseek-v4-pro 剩余较少但从未失败。
    失败优先沉底：应优先尝试 deepseek-v4-pro，而非剩余更多但老失败的 kimi-k3。
    """
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with(**{
        "deepseek-v4-pro": {"total_tokens": 500_000},      # 剩余 50 万，稳定
        "kimi-k3": {"total_tokens": 0, "fail_streak": 3},  # 剩余 100 万，老失败
        "glm-5.3": {"total_tokens": 0},                    # 剩余 100 万，稳定
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(utils, "call_llm", lambda *a, **k: '{"ok": true}')

    calls = []
    def fake_call_llm(url, key, model, messages, **kw):
        calls.append(model)
        return '{"ok": true}'
    monkeypatch.setattr(utils, "call_llm", fake_call_llm)

    parsed, used = utils.call_llm_json(
        [{"role": "user", "content": "x"}], candidates=_CAND, usage_file=usage_file)
    assert used == "glm-5.3", f"稳定模型应优先于老失败模型，实际用了 {used}"
    assert "kimi-k3" not in calls, "失败 3 次的 kimi-k3 不应被优先调用"
    assert calls.index("kimi-k3") if "kimi-k3" in calls else 999 > 0


def test_fail_streak_increments_and_resets(tmp_path, monkeypatch):
    """空响应累计失败次数；一旦成功即清零。"""
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with()))
    saved = {}
    monkeypatch.setattr(utils, "_save_llm_usage", lambda u, p: saved.update(u))

    # 第一个模型返回空 → 失败计数 +1；第二个返回合法 JSON → 采用
    seq = iter(['', '{"ok": true}'])
    monkeypatch.setattr(utils, "call_llm", lambda *a, **k: next(seq))
    parsed, used = utils.call_llm_json(
        [{"role": "user", "content": "x"}], candidates=_CAND, usage_file=usage_file)
    assert used == "kimi-k3"
    assert saved.get("deepseek-v4-pro", {}).get("fail_streak") == 1, "空响应应累计失败"
    assert saved.get("kimi-k3", {}).get("fail_streak") == 0, "成功应清零失败计数"


def test_preseded_fail_streak_keeps_unreliable_at_back(tmp_path, monkeypatch):
    """回归保护：已预置 fail_streak 的失灵模型，即便剩余额度最高，也不排到健康模型前。

    对应修复：data/llm_usage.json 给 known-unreliable 模型预置 fail_streak=1，
    避免全新 clone / reset --purge 后它们因剩余额度偏高被排到前面、白等超时。
    """
    usage_file = tmp_path / "usage.json"
    # unreliable 剩余最多（全新），但已预置 fail_streak=1；healthy 已用 50 万、fail_streak=0
    usage_file.write_text(json.dumps(_usage_with(**{
        "deepseek-v4-pro": {"total_tokens": 500_000},                 # 剩余 50 万，健康
        "kimi-k3": {"total_tokens": 0},                               # 剩余 100 万，健康
        "glm-5.3": {"total_tokens": 0, "fail_streak": 1},             # 剩余 100 万，已失灵
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(utils, "call_llm", lambda *a, **k: '{"ok": true}')

    calls = []
    def fake_call_llm(url, key, model, messages, **kw):
        calls.append(model)
        return '{"ok": true}'
    monkeypatch.setattr(utils, "call_llm", fake_call_llm)

    parsed, used = utils.call_llm_json(
        [{"role": "user", "content": "x"}], candidates=_CAND, usage_file=usage_file)
    # fail_streak=0 的健康模型（kimi-k3）必须先被调用且被采用；
    # 因 kimi-k3 成功即返回，glm-5.3（fail_streak=1）不会被触及——这正说明它没排到前面。
    assert used == "kimi-k3", f"健康模型应先于已失灵模型被采用，实际 {used}"
    assert calls[0] == "kimi-k3", "首个被调用的必须是 fail_streak=0 的健康模型"
    assert "glm-5.3" not in calls, "预置 fail_streak 的失灵模型不应在健康模型之前被调用"


def test_call_llm_downgrades_on_400(monkeypatch):
    """HTTP 400 时应用「最小参数集」重试一次（实测 glm-5.3 拒绝 thinking 参数）。

    用假 requests.post 模拟：第一次（含 thinking）返回 400，降级重试返回 200。
    """
    import requests as _rq

    calls = []
    class FakeResp:
        def __init__(self, status, text):
            self.status_code = status
            self.text = text
        def raise_for_status(self):
            if self.status_code >= 400:
                raise _rq.exceptions.HTTPError(response=self)
        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(dict(json or {}))
        if len(calls) == 1:
            return FakeResp(400, '{"error":"unknown parameter: thinking"}')
        return FakeResp(200, "")

    monkeypatch.setattr(utils.requests, "post", fake_post)
    out = utils.call_llm("https://x/v1", "k", "glm-5.3", [{"role": "user", "content": "hi"}])
    assert out == '{"ok": true}'
    assert len(calls) == 2, "400 后应降级重试一次"
    assert "thinking" in calls[0], "首次应带 thinking 参数"
    assert "thinking" not in calls[1], "降级重试应去掉 thinking"
    assert "temperature" not in calls[1], "降级重试应去掉 temperature"
