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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from constants import LLM_JSON_CANDIDATES  # noqa: E402
import utils  # noqa: E402

CST = timezone(timedelta(hours=8))


def _now_iso(hours_ago=0.0):
    """失败时间戳：默认「刚刚」，负值向前推（用于验证 24h 时效衰减）。"""
    return (datetime.now(CST) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


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


def test_stale_model_never_called(tmp_path):
    """2026-11-17：账本有记录但后台快照查不到（stale_since）→ 必须判不可用。

    这是「防按量扣钱」的第二道锁。第一道锁是轮换列表白名单（候选构造阶段就排除），
    但白名单可以被手工改、也可以被 env `HY3_ROTATION_MODELS` 覆盖 —— 那时第一道
    锁形同不存在，只有这道还拦得住。
    """
    # 额度还剩满（100%），只有 stale 一个理由 → 也必须拒绝
    rec = {"total_tokens": 0, "stale_since": "2026-11-17 10:25:20"}
    assert utils._is_model_available("m", {"m": rec}) is False
    # 去掉 stale → 同一个记录立刻变可用（证明判据真的只看 stale，不是顺带被别的条件拦的）
    assert utils._is_model_available("m", {"m": {"total_tokens": 0}}) is True


def test_stale_reason_is_not_reported_as_quota_exhausted(tmp_path, monkeypatch):
    """stale 的跳过原因不能印成「免费额度剩余 <=10%」——那是错误方向。

    额度耗尽 → 等重置；后台查不到 → 人类要去看后台。两者处置完全不同。
    """
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps({
        "ghost": {"total_tokens": 0, "stale_since": "2026-11-17 10:25:20"},
        "ok":   {"total_tokens": 0},
    }), encoding="utf-8")
    monkeypatch.setattr(utils, "_load_llm_usage", lambda p: json.loads(usage_file.read_text()))
    monkeypatch.setattr(utils, "call_llm",
                        lambda *a, **k: json.dumps({"x": 1}))

    out = []
    with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
        parsed, used = utils.call_llm_json(
            [{"role": "user", "content": "hi"}],
            [("http://u", "k", "ghost"), ("http://u", "k", "ok")],
            usage_file=usage_file,
        )
    assert used == "ok", "stale 模型必须被跳过，实际被调用了"
    joined = "\n".join(out)
    assert "后台快照已查不到" in joined, f"跳过原因应指明 stale，实际输出：{joined}"
    assert "剩余 <=10%" not in joined.split("ok")[0], f"stale 被误报成额度耗尽：{joined}"


def test_stale_only_pool_reports_stale_not_quota_exhausted(tmp_path, monkeypatch):
    """整池都是 stale 时，异常文案必须说「后台查不到」，不能说「免费额度耗尽」。

    两者处置完全不同：额度耗尽 → 等额度重置/换号；后台查不到 → 去后台确认是不是
    下架了或转成了按量计费。文案错了，人就修错了方向。
    """
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps({
        "ghost": {"total_tokens": 0, "stale_since": "2026-11-17 10:25:20"},
    }), encoding="utf-8")
    monkeypatch.setattr(utils, "_load_llm_usage", lambda p: json.loads(usage_file.read_text()))

    with mock.patch("builtins.print"):
        with pytest.raises(utils.QuotaExhaustedError) as ei:
            utils.call_llm_json([{"role": "user", "content": "hi"}],
                                [("http://u", "k", "ghost")], usage_file=usage_file)
    msg = str(ei.value)
    assert "后台查不到额度" in msg, f"异常文案应指明 stale，实际：{msg}"
    assert "免费额度耗尽" not in msg, f"stale 被误报成额度耗尽：{msg}"


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

    kimi-k3 剩余最多（全新）但**刚刚**失败 3 次；deepseek-v4-pro 剩余较少但从未失败。
    失败优先沉底：应优先尝试 deepseek-v4-pro / glm-5.3，而非剩余更多但老失败的 kimi-k3。

    2026-10-26 补充：失败记录带 24h 时效（见 utils._fail_streak），
    所以本用例必须写 `last_fail_at`＝现在，才算「近期失败」。
    """
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with(**{
        "deepseek-v4-pro": {"total_tokens": 500_000},      # 剩余 50 万，稳定
        "kimi-k3": {"total_tokens": 0, "fail_streak": 3,
                    "last_fail_at": _now_iso()},           # 剩余 100 万，刚刚失败 3 次
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
    """回归保护：**近期**已失灵的模型，即便剩余额度最高，也不排到健康模型前。

    对应修复：data/llm_usage.json 给 known-unreliable 模型预置 fail_streak=1，
    避免全新 clone / reset --purge 后它们因剩余额度偏高被排到前面、白等超时。
    （2026-10-26：失败计数带 24h 时效，预置时必须同时写 last_fail_at，否则视为陈旧。）
    """
    usage_file = tmp_path / "usage.json"
    # unreliable 剩余最多（全新），但刚刚失败过；healthy 已用 50 万、fail_streak=0
    usage_file.write_text(json.dumps(_usage_with(**{
        "deepseek-v4-pro": {"total_tokens": 500_000},                 # 剩余 50 万，健康
        "kimi-k3": {"total_tokens": 0},                               # 剩余 100 万，健康
        "glm-5.3": {"total_tokens": 0, "fail_streak": 1,
                    "last_fail_at": _now_iso()},                      # 剩余 100 万，刚失灵
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
    """HTTP 400 时应用「最小参数集」重试一次。

    用假 requests.post 模拟：第一次返回 400，降级重试返回 200。
    注意（2026-10-19 修正）：glm-5.3 首次请求**不再带 thinking**——
    传了不报 400 而是把推理回显进 content，JSON 解析必失败（见下条回归）。
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
            return FakeResp(400, '{"error":"unsupported parameter"}')
        return FakeResp(200, "")

    monkeypatch.setattr(utils.requests, "post", fake_post)
    out = utils.call_llm("https://x/v1", "k", "glm-5.3", [{"role": "user", "content": "hi"}])
    assert out == '{"ok": true}'
    assert len(calls) == 2, "400 后应降级重试一次"
    assert "thinking" not in calls[0], "glm- 模型首次就不应带 thinking（修复项）"
    assert "temperature" in calls[0], "glm- 模型首次应带 temperature"
    assert "thinking" not in calls[1], "降级重试应去掉 thinking"
    assert "temperature" not in calls[1], "降级重试应去掉 temperature"


def test_glm_models_never_receive_thinking_param(monkeypatch):
    """回归保护（2026-10-19 事故）：glm- 系列绝不传 thinking 参数。

    根因：utils._build_body 曾给 glm- 传 thinking={"type":"disabled"}，TokenHub 不报错，
    而是 HTTP 200 把推理过程回显进 content（"The user is asking me to output only JSON..."），
    call_llm_json 的 JSON 解析必然失败 → 判「无法解析」沉底 fail_streak，满额模型白白闲置。
    实测 glm-5.3/5.3-flash/5.3-flashx/5.2 最小参数集均返回干净 JSON。
    """
    import requests as _rq

    calls = []
    class FakeResp:
        status_code = 200
        text = ""
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(dict(json or {}))
        return FakeResp()

    monkeypatch.setattr(utils.requests, "post", fake_post)
    for m in ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.2"]:
        calls.clear()
        out = utils.call_llm("https://x/v1", "k", m, [{"role": "user", "content": "hi"}])
        assert out == '{"ok": true}'
        assert len(calls) == 1, f"{m} 首次请求应直接成功（无需 400 降级）"
        assert "thinking" not in calls[0], f"{m} 请求体绝不应含 thinking"
        assert calls[0].get("temperature") == 0.7, f"{m} 应保留 temperature"


def test_deepseek_still_gets_thinking_disabled(monkeypatch):
    """配套保护：deepseek/qwen3 推理模型的 thinking 关断适配不能丢。

    与 glm- 修复互斥——deepseek 不关 thinking 时 reasoning 吃掉大量 token 且输出不稳。
    """
    calls = []
    class FakeResp:
        status_code = 200
        text = ""
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(dict(json or {}))
        return FakeResp()

    monkeypatch.setattr(utils.requests, "post", fake_post)
    for m in ["deepseek-v4-flash-202605", "deepseek/deepseek-flash", "qwen3-max"]:
        calls.clear()
        utils.call_llm("https://x/v1", "k", m, [{"role": "user", "content": "hi"}])
        assert calls[0].get("thinking") == {"type": "disabled"}, \
            f"{m} 应带 thinking disabled（防 reasoning 吃 token）"


def test_hy4_models_get_thinking_disabled(monkeypatch):
    """hy4 / hy4-flash 也必须关 thinking（2026-11-17 实测，生产规模任务直接翻车）。

    不关时的实测（max_tokens=1200 + 长素材）：
      hy4       → finish_reason=length, content=0B（空内容，整篇失败）
      hy4-flash → finish_reason=length, content=0~46B（不可解析）
      两者都白烧 1649 tokens 才失败，还要背一次 fail_streak。
    关掉后：finish=stop、content≈180~230B、可解析、usage 582~621（**省 63%**）。

    为什么必须放在单测里：hy4 系是 1000 万额度的储备位，一旦不可用就只剩
    100 万额度的小池子；而它「失败」的表现是「返回空内容」，很容易被误判成
    「模型不稳定」而不是「少了这个参数」。
    """
    calls = []
    class FakeResp:
        status_code = 200
        text = ""
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'},
                                 "finish_reason": "stop"}], "usage": {}}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(dict(json or {}))
        return FakeResp()

    monkeypatch.setattr(utils.requests, "post", fake_post)
    for m in ["hy4", "hy4-flash"]:
        calls.clear()
        out = utils.call_llm("https://x/v1", "k", m, [{"role": "user", "content": "hi"}])
        assert out == '{"ok": true}'
        assert calls[0].get("thinking") == {"type": "disabled"}, \
            f"{m} 应带 thinking disabled（否则 reasoning 吃满 max_tokens，content 为空）"


def test_truncated_empty_content_is_reported_as_length_not_failure(monkeypatch, capsys):
    """finish_reason=length + content 为空 → 必须打出「被截断」告警，不能只当空响应。

    这一条的痛点是**归因**：call_llm_json 只看到空内容就把 fail_streak +1，
    运维看到的是「这个模型最近老失败」，而真实原因是 max_tokens 不够。
    两者处置完全不同：模型不稳 → 换掉；被截断 → 调大 max_tokens / 关 thinking。
    """
    class FakeResp:
        status_code = 200
        text = ""
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": "",
                                             "reasoning_content": "x" * 1800},
                                 "finish_reason": "length"}],
                    "usage": {"total_tokens": 1649}}

    monkeypatch.setattr(utils.requests, "post",
                        lambda *a, **k: FakeResp())
    with mock.patch("builtins.print") as mp:
        out = utils.call_llm("https://x/v1", "k", "hy4",
                             [{"role": "user", "content": "hi"}], max_tokens=1200)
    assert out == ""
    printed = " ".join(" ".join(map(str, c.args)) for c in mp.call_args_list)
    assert "被 max_tokens=1200 截断" in printed, f"未报告截断原因：{printed}"
    assert "不是模型故障" in printed, f"未明确排除模型故障：{printed}"
    assert "1649" in printed, f"未带上已烧掉的 token（取证需要）：{printed}"


def test_normal_empty_response_does_not_claim_truncation(monkeypatch):
    """负向：正常结束（finish_reason=stop）但内容为空时，不许误报「被截断」。"""
    class FakeResp:
        status_code = 200
        text = ""
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": ""},
                                 "finish_reason": "stop"}], "usage": {}}

    monkeypatch.setattr(utils.requests, "post", lambda *a, **k: FakeResp())
    with mock.patch("builtins.print") as mp:
        utils.call_llm("https://x/v1", "k", "hy4", [{"role": "user", "content": "hi"}])
    printed = " ".join(" ".join(map(str, c.args)) for c in mp.call_args_list)
    assert "被" not in printed and "截断" not in printed, f"stop 场景误报截断：{printed}"


def test_kimi_temperature_pinned_to_one(monkeypatch):
    """配套保护：kimi 系列 temperature 固定 1.0 的适配不能丢。"""
    calls = []
    class FakeResp:
        status_code = 200
        text = ""
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(dict(json or {}))
        return FakeResp()

    monkeypatch.setattr(utils.requests, "post", fake_post)
    utils.call_llm("https://x/v1", "k", "kimi-k3", [{"role": "user", "content": "hi"}])
    assert calls[0].get("temperature") == 1.0, "kimi 只接受 temperature=1"
    assert "thinking" not in calls[0], "kimi 不应带 thinking"


# ============================================================
# 2026-10-26 新增：免费额度「到期时间近→远」切换 + 失败记录时效衰减
# 背景：后台截图里 19 个模型各有到期时间；原来只按「剩余额度」排序，
# 快到期但剩余多的模型会被排到后面，白放到期作废。
# ============================================================

def _usage_with_expiry(spec):
    """spec: {model: (total_tokens, expires_at)} → usage 字典。"""
    usage = {}
    for m, (used, exp) in spec.items():
        usage[m] = {"prompt_tokens": 0, "completion_tokens": 0,
                    "total_tokens": used, "free_quota_expires_at": exp}
    return usage


def test_earlier_expiry_wins_over_more_remaining(tmp_path, monkeypatch):
    """到期早的先用——哪怕它剩余额度更少。

    kimi-k3 剩 87.8 万、2027-08-11 到期；minimax-m3.1-flash 剩 100 万、2027-10-26 到期。
    正确顺序是先烧 kimi-k3（快到期的额度不用就作废）。
    """
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with_expiry({
        "kimi-k3": (122_000, "2027-08-11 11:04:10"),        # 剩余 87.8 万，最早到期
        "minimax-m3.1-flash": (0, "2027-10-26 09:32:41"),   # 剩余 100 万，最晚到期
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(utils, "call_llm",
                        lambda url, key, model, messages, **kw: (calls.append(model), '{"ok": true}')[1])

    cand = [("https://fake/v1", "k", "minimax-m3.1-flash"),
            ("https://fake/v1", "k", "kimi-k3")]   # 候选列表顺序故意反过来
    _parsed, used = utils.call_llm_json([{"role": "user", "content": "x"}],
                                        candidates=cand, usage_file=usage_file)
    assert used == "kimi-k3", f"应先烧最早到期的额度，实际 {used}"
    assert calls == ["kimi-k3"], f"只应调用一次且是 kimi-k3，实际 {calls}"


def test_same_expiry_breaks_tie_by_remaining_desc(tmp_path, monkeypatch):
    """同到期日 → 先花剩余最多的，避免单个模型先超额。"""
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with_expiry({
        "glm-5.3": (34_000, "2027-09-14 09:24:30"),                  # 剩余 96.6 万
        "deepseek/deepseek-flash": (331_000, "2027-09-14 09:24:30"), # 剩余 66.9 万
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(utils, "call_llm", lambda *a, **k: '{"ok": true}')
    cand = [("https://fake/v1", "k", "deepseek/deepseek-flash"),
            ("https://fake/v1", "k", "glm-5.3")]
    _p, used = utils.call_llm_json([{"role": "user", "content": "x"}],
                                   candidates=cand, usage_file=usage_file)
    assert used == "glm-5.3", f"同到期日应先用剩余最多的，实际 {used}"


def test_model_without_expiry_sinks_behind_known_expiry(tmp_path, monkeypatch):
    """没记录到期时间的模型排最后——不给它抢在「快过期额度」前面的机会。"""
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with_expiry({
        "kimi-k3": (500_000, "2027-08-11 11:04:10"),   # 只剩 50 万，但最早到期
        "minimax-m3.1-flash": (0, ""),                  # 满额，无到期信息
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(utils, "call_llm", lambda *a, **k: '{"ok": true}')
    cand = [("https://fake/v1", "k", "minimax-m3.1-flash"),
            ("https://fake/v1", "k", "kimi-k3")]
    _p, used = utils.call_llm_json([{"role": "user", "content": "x"}],
                                   candidates=cand, usage_file=usage_file)
    assert used == "kimi-k3", f"无到期信息的模型不应抢在已知到期前面，实际 {used}"


def test_sort_by_expiry_can_be_disabled(tmp_path, monkeypatch):
    """开关关掉 → 退回「仅按剩余额度降序」（保留给排障用）。"""
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with_expiry({
        "kimi-k3": (122_000, "2027-08-11 11:04:10"),
        "minimax-m3.1-flash": (0, "2027-10-26 09:32:41"),
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(utils, "call_llm", lambda *a, **k: '{"ok": true}')
    cand = [("https://fake/v1", "k", "kimi-k3"),
            ("https://fake/v1", "k", "minimax-m3.1-flash")]
    _p, used = utils.call_llm_json([{"role": "user", "content": "x"}],
                                   candidates=cand, usage_file=usage_file,
                                   sort_by_expiry=False)
    assert used == "minimax-m3.1-flash", f"关闭到期排序后应退回剩余降序，实际 {used}"


def test_stale_fail_streak_decays_after_ttl(tmp_path, monkeypatch):
    """超过 24h 的失败记录自动失效——否则一次性故障 = 永久拉黑。

    实测事故：19 个模型里有 15 个背着 3~4 次历史 fail_streak，被永久沉底，
    整整一个月没被调用过（其中多数是已修复的 glm thinking bug 留下的）。
    """
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with(**{
        "kimi-k3": {"total_tokens": 100_000, "fail_streak": 4,
                    "last_fail_at": _now_iso(48)},          # 两天前失败 → 应视为健康
        "glm-5.3": {"total_tokens": 500_000},                # 健康但剩余更少
    })))
    monkeypatch.setattr(utils, "_save_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(utils, "call_llm", lambda *a, **k: '{"ok": true}')
    cand = [("https://fake/v1", "k", "glm-5.3"),
            ("https://fake/v1", "k", "kimi-k3")]
    _p, used = utils.call_llm_json([{"role": "user", "content": "x"}],
                                   candidates=cand, usage_file=usage_file)
    # 若衰减失效，kimi-k3 会因 4 次失败被沉底，此时会选到 glm-5.3
    assert used == "kimi-k3", f"48h 前的失败不该继续沉底，实际 {used}"


def test_fail_streak_without_timestamp_is_treated_stale(tmp_path, monkeypatch):
    """老账本没有 last_fail_at → 按陈旧处理（宁可再试一次）。"""
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps(_usage_with(**{
        "kimi-k3": {"total_tokens": 0, "fail_streak": 3},   # 无时间戳
    })))
    assert utils._fail_streak("kimi-k3", utils._load_llm_usage(usage_file)) == 0


def test_fail_streak_ttl_boundary():
    """TTL 边界：23h 内仍沉底，25h 外视为失效。"""
    base = {"m": {"total_tokens": 0, "fail_streak": 2, "last_fail_at": _now_iso(23)}}
    assert utils._fail_streak("m", base) == 2
    base2 = {"m": {"total_tokens": 0, "fail_streak": 2, "last_fail_at": _now_iso(25)}}
    assert utils._fail_streak("m", base2) == 0


def test_bump_fail_streak_writes_timestamp():
    """失败必须落时间戳，否则 TTL 衰减永远拿不到参照。"""
    usage = {}
    utils._bump_fail_streak(usage, "m")
    assert usage["m"]["fail_streak"] == 1
    assert usage["m"].get("last_fail_at"), "应写入 last_fail_at"
    utils._bump_fail_streak(usage, "m", reset=True)
    assert usage["m"]["fail_streak"] == 0


def test_quota_expiry_parsing():
    """到期时间解析：缺失/坏值→inf（排最后），合法值→可比时间戳。"""
    usage = {
        "a": {"free_quota_expires_at": "2027-08-11 11:04:10"},
        "b": {"free_quota_expires_at": "2027/10/26 09:32:41"},
        "c": {},
        "d": {"free_quota_expires_at": "不是日期"},
    }
    ta, tb = utils._quota_expiry_ts("a", usage), utils._quota_expiry_ts("b", usage)
    assert ta < tb, "2027-08-11 应早于 2027-10-26"
    assert utils._quota_expiry_ts("c", usage) == float("inf")
    assert utils._quota_expiry_ts("d", usage) == float("inf")


def test_rotation_list_matches_ledger_models():
    """轮换列表与账本必须对得上：列表里每个模型都要有到期时间。

    否则「按到期时间切换」对它是空操作（永远排最后），额度白放。
    """
    usage = utils._load_llm_usage()
    missing = [m for _u, _k, m in LLM_JSON_CANDIDATES
               if m in usage and not usage[m].get("free_quota_expires_at")]
    assert not missing, f"这些模型缺 free_quota_expires_at：{missing}"


def test_rotation_list_excludes_non_free_models():
    """后台免费体验列表里没有的模型不得进轮换（用它们＝按量计费，最贵的一类错）。

    判据必须从快照派生，**不能写死模型名**：2026-11-09 后台把 glm-5.2、kimi-k2.7-code、
    deepseek-v4-pro-202606 放了进来（原来这条测试把它们写成「无免费额度」），
    硬编码名单当场就变成了假警报 —— 而真正的红线（hy3 / deepseek-v4-pro 剩 0%）
    反而是靠 remaining_pct 才判得准。
    """
    snap = json.loads((ROOT / "data" / "llm_quota_snapshot.json").read_text(encoding="utf-8"))
    models = [m for _u, _k, m in LLM_JSON_CANDIDATES]
    for m in models:
        meta = snap["models"].get(m)
        assert meta is not None, f"{m} 不在后台快照里（按量计费风险）"
        assert float(meta.get("remaining_pct") or 0) > 0, f"{m} 剩余 0%，不该在轮换里"
        assert meta.get("enabled") is not False, f"{m} 计费开关未开通，不该在轮换里"
    # 明令排除的两个：后台仍显示「已上线」，但 0% + 未开通
    for bad in ("hy3", "deepseek-v4-pro"):
        assert bad not in models, f"{bad} 无免费额度/未开通，不应出现在轮换列表"


# ============================================================
# 9. 单模型额度 ≠ 全局额度（hy4 = 1000 万）
# ============================================================

def test_big_quota_model_is_not_cut_by_global_hard_cap():
    """1000 万额度的模型不能被 80 万这个绝对硬上限砍到 8%。

    2026-11-09 后台给 hy4 单独开了 1000 万额度。硬上限是按「已用 80%」设计的保险丝，
    绝对值不随额度放大，就等于给 hy4 加了个「用 8% 就闭嘴」的天花板 —— 不报错，
    只是 92% 的免费额度永远用不上。
    """
    usage = {"hy4": {"total_tokens": 1_000_000, "free_quota_total_tokens": 10_000_000}}
    assert utils._effective_quota("hy4", usage) == (10_000_000, 8_000_000)
    assert utils._is_model_available("hy4", usage) is True
    assert utils._remaining_tokens("hy4", usage) == 9_000_000
    usage["hy4"]["total_tokens"] = 8_100_000          # 已用 81%（> 80% 硬上限，< 90% 阈值）
    assert utils._is_model_available("hy4", usage) is False


def test_old_ledger_without_quota_field_falls_back_to_global():
    """老账本没有 free_quota_total_tokens，必须回落到全局值，不能当成「额度 0」。"""
    usage = {"glm-5.3": {"total_tokens": 61_000}}
    assert utils._effective_quota("glm-5.3", usage) == (1_000_000, 800_000)
    assert utils._remaining_tokens("glm-5.3", usage) == 939_000


def test_sorting_compares_ratio_not_absolute_tokens():
    """排序比的是「剩余比例」，不是绝对 token 数。

    比绝对值的话，1000 万额度的 hy4（剩 500 万）会永远压过只剩 80 万的 100 万额度模型，
    「先用快到期额度」的规则就被彻底架空了。
    """
    usage = {
        "big": {"total_tokens": 5_000_000, "free_quota_total_tokens": 10_000_000},
        "small": {"total_tokens": 200_000},
    }
    assert utils._remaining_tokens("big", usage) > utils._remaining_tokens("small", usage)
    assert utils._remaining_ratio("big", usage) < utils._remaining_ratio("small", usage)


# ============================================================
# 10. 可用池容量合计要按「各模型自己的额度」算
# ============================================================

def test_usable_pool_sum_uses_each_model_quota():
    """验收脚本印的「可用额度合计」必须按每个模型自己的额度加，不能全用同一个额度。

    2026-11-17 实机发现：`check_usable_pool` 里 `usable.append((name, pct, exp))`
    没带 meta，求和时却写 `meta.get("quota_total_tokens")` —— `meta` 是**上一个
    循环的残留变量**，于是每项都按「最后一个模型」的额度折算。印出来 1,136 万，
    实际 2,936 万，差 2.6 倍。它不报错、也不离谱，正是「看起来合理所以没人查」
    那一类。这条测试用两个额度差 10 倍的模型把它钉住。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "verify_llm_quota", ROOT / "scripts" / "verify_llm_quota.py")
    vq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vq)

    snap = {"models": {
        "big": {"remaining_pct": 100.0, "quota_total_tokens": 10_000_000,
                "expires_at": "2027-11-17 10:17:01", "enabled": True, "status": "已上线"},
        "small": {"remaining_pct": 100.0,
                  "expires_at": "2027-08-11 11:04:10", "enabled": True, "status": "已上线"},
    }}
    r = vq.check_usable_pool(snap)
    txt = " ".join(r["detail"])
    assert "11,000,000" in txt, f"应按 1000 万 + 100 万 = 1100 万，实际：{txt}"
    assert "1,100,000" not in txt, "差额 10 倍会被读成正常值，正是这个 bug 的形态"
