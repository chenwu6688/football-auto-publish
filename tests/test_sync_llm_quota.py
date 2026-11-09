"""额度同步脚本单测（scripts/sync_llm_quota.py）。

背景：TokenHub 无额度查询 API，后台截图是唯一真值。过去手改 llm_usage.json，
本地 `total_tokens` 系统性偏少 —— 而运行期只看这个字段，少记 = 会继续往一个
其实快用完的模型上打请求 = 直接按量计费。这里把「以后台为准」的每条边界钉死。
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "sync_llm_quota", ROOT / "scripts" / "sync_llm_quota.py")
sq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sq)

QUOTA = 1_000_000


def _snap(**models):
    """把 kwargs 组装成快照结构：name=(剩余%, 到期, status, enabled)。"""
    out = {}
    for name, v in models.items():
        pct, exp, status, enabled = v
        out[name] = {"remaining_pct": pct, "expires_at": exp,
                     "status": status, "enabled": enabled}
    return {"captured_at": "2026-11-03 10:29:34", "quota_total_tokens": QUOTA,
            "models": out}


# ============================================================
# 1. 核心口径：后台是真值，本地只作参考
# ============================================================

def test_backend_pct_overrides_local_undercount():
    """本地少记（后台剩 41.1%、本地以为剩 72.4%）必须按后台抬高。

    这条是本次同步的全部意义：本地少记会让运行期误判「还有 27 万可用」，
    实际只剩 41 万 → 多出来的请求直接按量计费。
    """
    snap = _snap(**{"deepseek/deepseek-v4-flash-vision-exp":
                    (41.1, "2027-09-14 09:24:30", "运行中", True)})
    usage = {"deepseek/deepseek-v4-flash-vision-exp":
             {"total_tokens": 276_000, "free_quota_pct": 72.4}}
    new, changes, _, errors = sq.sync(snap, usage, quota=QUOTA, rotation=[])
    rec = new["deepseek/deepseek-v4-flash-vision-exp"]
    assert rec["total_tokens"] == 589_000          # 1,000,000 × 58.9%
    assert rec["free_quota_pct"] == 41.1
    assert not errors
    assert any("72.4% → 41.1%" in c for c in changes)


def test_backend_pct_can_also_lower_local_overcount():
    """本地记多了也要按后台降下来——否则模型被永久沉底、白扔掉可用额度。"""
    snap = _snap(**{"glm-5.3": (95.3, "2027-09-14 09:24:30", "运行中", True)})
    usage = {"glm-5.3": {"total_tokens": 900_000, "free_quota_pct": 10.0}}
    new, _, _, _ = sq.sync(snap, usage, quota=QUOTA, rotation=[])
    assert new["glm-5.3"]["total_tokens"] == 47_000


def test_prompt_and_completion_preserved():
    """prompt/completion 是本地记账明细，同步不该动它们。"""
    snap = _snap(**{"kimi-k3": (86.7, "2027-08-11 11:04:10", "运行中", True)})
    usage = {"kimi-k3": {"prompt_tokens": 447, "completion_tokens": 591,
                         "total_tokens": 123_038}}
    new, _, _, _ = sq.sync(snap, usage, quota=QUOTA, rotation=[])
    assert new["kimi-k3"]["prompt_tokens"] == 447
    assert new["kimi-k3"]["completion_tokens"] == 591


def test_fail_streak_preserved_and_not_reset_by_sync():
    """同步额度≠重置失败计数：两件事混在一起，会把真实故障洗掉。"""
    snap = _snap(**{"glm-5.3": (95.3, "2027-09-14 09:24:30", "运行中", True)})
    usage = {"glm-5.3": {"total_tokens": 34_000, "fail_streak": 5,
                         "last_fail_at": "2026-11-01T12:02:56+08:00"}}
    new, _, _, _ = sq.sync(snap, usage, quota=QUOTA, rotation=[])
    assert new["glm-5.3"]["fail_streak"] == 5
    assert new["glm-5.3"]["last_fail_at"] == "2026-11-01T12:02:56+08:00"


# ============================================================
# 2. 停用判定（三种入口都必须拦住）
# ============================================================

@pytest.mark.parametrize("status,enabled,pct", [
    ("已停止", True, 0.0),          # hy3 的真实形态
    ("运行中", False, 0.0),         # 计费开关关掉
    ("运行中", True, 0.0),          # 额度耗尽
])
def test_stopped_detected_from_any_signal(status, enabled, pct):
    snap = _snap(**{"x": (pct, "2027-08-11 11:04:10", status, enabled)})
    new, _, _, errors = sq.sync(snap, {}, quota=QUOTA, rotation=[])
    assert new["x"]["disabled"] is True
    assert new["x"]["disabled_reason"]
    assert not errors


def test_stopped_model_keeps_zero_total():
    """停用模型不写 total：hy3 剩 0% 会被算成「已用 100 万」，冲到用量排行第一。

    它本来也不会被调用（_is_model_available 先看 disabled），这个假数字
    只会污染报表、误导「谁在吃额度」的判断。
    """
    snap = _snap(**{"hy3": (0.0, "2027-08-11 11:04:10", "已停止", False)})
    new, changes, _, _ = sq.sync(snap, {}, quota=QUOTA, rotation=[])
    assert new["hy3"]["total_tokens"] == 0
    assert new["hy3"]["free_quota_pct"] == 0.0
    assert any("已标停用" in c for c in changes)


def test_reenabled_model_clears_disabled_flag():
    """后台重新启用后必须能复活，否则一次停用就是永久拉黑（同 fail_streak 的教训）。"""
    snap = _snap(**{"kimi-k3": (86.7, "2027-08-11 11:04:10", "运行中", True)})
    usage = {"kimi-k3": {"total_tokens": 0, "disabled": True,
                         "disabled_reason": "旧原因"}}
    new, _, _, _ = sq.sync(snap, usage, quota=QUOTA, rotation=[])
    assert "disabled" not in new["kimi-k3"]
    assert "disabled_reason" not in new["kimi-k3"]


def test_pending_offline_model_not_disabled_but_keeps_note():
    """「模型待下线」还能用，只打 note —— 直接 disabled 会白扔掉 42% 额度。"""
    snap = _snap(**{"deepseek-v4-flash-0731":
                    (42.1, "2027-09-14 09:24:30", "运行中(模型待下线)", True)})
    snap["models"]["deepseek-v4-flash-0731"]["note"] = "后台标待下线，仅末位备援"
    new, _, _, _ = sq.sync(snap, {}, quota=QUOTA, rotation=[])
    assert "disabled" not in new["deepseek-v4-flash-0731"]
    assert "待下线" in new["deepseek-v4-flash-0731"]["note"]


# ============================================================
# 3. UI 名 vs API ID
# ============================================================

def test_ui_name_recorded_separately_from_api_id():
    """后台截图写 mimo-v2.5-pro，/v1/models 里真实 ID 是 mimo-v2.6-pro。

    账本键必须是 API ID（否则调用 404），UI 名单独记 —— 下次对账才不会以为
    「后台有个模型账本里没有」。
    """
    snap = {"captured_at": "2026-11-03 10:29:34", "models": {
        "mimo-v2.6-pro": {"ui_name": "mimo-v2.5-pro", "remaining_pct": 59.1,
                          "expires_at": "2027-09-27 10:07:07",
                          "status": "运行中", "enabled": True}}}
    new, _, _, _ = sq.sync(snap, {}, quota=QUOTA, rotation=[])
    assert "mimo-v2.6-pro" in new
    assert "mimo-v2.5-pro" not in new
    assert new["mimo-v2.6-pro"]["ui_name"] == "mimo-v2.5-pro"


# ============================================================
# 4. 硬校验：宁可报错也不许悄悄用错模型
# ============================================================

def test_rotation_model_missing_from_snapshot_is_error():
    """轮换里有个后台查不到额度的模型 = 按量计费（实测不报错、直接扣钱）。"""
    snap = _snap(**{"kimi-k3": (86.7, "2027-08-11 11:04:10", "运行中", True)})
    _, _, _, errors = sq.sync(snap, {}, quota=QUOTA, rotation=["kimi-k3", "glm-5.2"])
    assert any("glm-5.2" in e for e in errors)


def test_stopped_model_in_rotation_is_error():
    snap = _snap(**{"hy3": (0.0, "2027-08-11 11:04:10", "已停止", False)})
    _, _, _, errors = sq.sync(snap, {}, quota=QUOTA, rotation=["hy3"])
    assert any("hy3" in e for e in errors)


def test_stale_ledger_entry_warns_but_is_not_deleted():
    """账本有、快照无 → 只警告 + 标 stale，绝不自动删（可能只是这次截图没截到）。"""
    snap = _snap(**{"kimi-k3": (86.7, "2027-08-11 11:04:10", "运行中", True)})
    usage = {"ghost-model": {"total_tokens": 5000}}
    new, _, warns, errors = sq.sync(snap, usage, quota=QUOTA, rotation=[])
    assert "ghost-model" in new
    assert new["ghost-model"]["stale_since"] == "2026-11-03 10:29:34"
    assert any("ghost-model" in w for w in warns)
    assert not errors


def test_missing_remaining_pct_is_error_not_zero():
    """缺字段不许用 0 顶上（用了就等于宣布「这模型额度用完了」）。"""
    snap = {"captured_at": "x", "models": {"x": {"expires_at": "2027-01-01 00:00:00"}}}
    _, _, _, errors = sq.sync(snap, {}, quota=QUOTA, rotation=[])
    assert any("remaining_pct" in e for e in errors)


def test_empty_snapshot_refuses_to_sync():
    """空快照会把账本清零 → 必须拒绝，而不是「同步成功、账本空了」。"""
    with pytest.raises(SystemExit):
        sq.sync({"models": {}}, {"kimi-k3": {"total_tokens": 1}}, quota=QUOTA, rotation=[])


# ============================================================
# 5. 实际停用线口径（threshold 90% vs 硬上限 80% 谁更严）
# ============================================================

def test_effective_stop_line_is_stricter_of_two():
    """真实停用线 = min(90%, 80 万/100 万) = 80%，不是注释里常说的 90%。

    两个阈值并存时必须取更严者，否则报表说「还能用」、运行期已经跳过。
    """
    assert sq._skip_line(QUOTA) == pytest.approx(0.80)


def test_absolute_hard_cap_gets_stricter_when_quota_grows():
    """硬上限是**绝对值** 80 万，不是百分比 —— 额度涨到 1000 万时它会变得极严。

    写这条用例时我一开始以为「额度变大后 threshold(90%) 更严」，实测反了：
    min(0.90, 800000/10000000) = 0.08，只剩 8% 就停用，等于浪费 92% 的额度。
    这不是 bug（80 万是防本地少记的保险丝，本来就该按最坏情况设），
    但它意味着**改套餐必须同时改 LLM_HARD_CAP_TOKENS**，否则账本静悄悄地只剩
    一成功率可用。验收脚本 verify_llm_quota 会检查这个隐含比例是否离谱。
    """
    assert sq._skip_line(10_000_000) == pytest.approx(0.08)


def test_model_at_stop_line_warned():
    """剩 19.8%（已用 80.2%）已过 80% 线，必须提示「运行期会跳过」。"""
    snap = _snap(**{"deepseek-v4-flash-202605":
                    (19.8, "2027-08-11 11:04:10", "运行中", True)})
    _, _, warns, _ = sq.sync(snap, {}, quota=QUOTA,
                             rotation=["deepseek-v4-flash-202605"])
    assert any("将跳过" in w or "自动跳过" in w for w in warns)


# ============================================================
# 6. 落盘与 dry-run
# ============================================================

def test_main_dry_run_does_not_write(tmp_path, monkeypatch):
    snap = _snap(**{"kimi-k3": (86.7, "2027-08-11 11:04:10", "运行中", True)})
    snapf = tmp_path / "snap.json"
    snapf.write_text(json.dumps(snap), encoding="utf-8")
    usef = tmp_path / "usage.json"
    usef.write_text(json.dumps({"kimi-k3": {"total_tokens": 123}}), encoding="utf-8")
    before = usef.read_text(encoding="utf-8")

    monkeypatch.setattr(sq.sys, "argv",
                        ["x", "--dry-run", "--snapshot", str(snapf), "--usage", str(usef)])
    monkeypatch.setattr(sq, "HY3_ROTATION_MODELS", ["kimi-k3"])
    assert sq.main() == 0
    assert usef.read_text(encoding="utf-8") == before


def test_main_writes_when_ok(tmp_path, monkeypatch):
    snap = _snap(**{"kimi-k3": (86.7, "2027-08-11 11:04:10", "运行中", True)})
    snapf = tmp_path / "snap.json"
    snapf.write_text(json.dumps(snap), encoding="utf-8")
    usef = tmp_path / "usage.json"
    usef.write_text(json.dumps({"_calibration_note": "keep-me",
                                "kimi-k3": {"total_tokens": 123}}), encoding="utf-8")

    monkeypatch.setattr(sq.sys, "argv",
                        ["x", "--snapshot", str(snapf), "--usage", str(usef)])
    monkeypatch.setattr(sq, "HY3_ROTATION_MODELS", ["kimi-k3"])
    assert sq.main() == 0
    d = json.loads(usef.read_text(encoding="utf-8"))
    assert d["kimi-k3"]["total_tokens"] == 133_000
    assert d["_calibration_note"] == "keep-me"     # 说明键不能被清掉


def test_main_error_exits_nonzero_and_does_not_write(tmp_path, monkeypatch):
    """硬校验不过必须退出码 1 且不写盘，避免「报错但账本已被改」。"""
    snap = _snap(**{"kimi-k3": (86.7, "2027-08-11 11:04:10", "运行中", True)})
    snapf = tmp_path / "snap.json"
    snapf.write_text(json.dumps(snap), encoding="utf-8")
    usef = tmp_path / "usage.json"
    usef.write_text(json.dumps({"kimi-k3": {"total_tokens": 123}}), encoding="utf-8")
    before = usef.read_text(encoding="utf-8")

    monkeypatch.setattr(sq.sys, "argv",
                        ["x", "--snapshot", str(snapf), "--usage", str(usef)])
    # 轮换里塞一个后台查不到额度的模型
    monkeypatch.setattr(sq, "HY3_ROTATION_MODELS", ["kimi-k3", "qwen-max"])
    assert sq.main() == 1
    assert usef.read_text(encoding="utf-8") == before


# ============================================================
# 7. 真实快照自检（防手填时把数字写错）
# ============================================================

def test_repo_snapshot_is_sane():
    snap = json.loads((ROOT / "data" / "llm_quota_snapshot.json").read_text(encoding="utf-8"))
    models = snap["models"]
    assert len(models) >= 15
    assert snap.get("captured_at"), "快照必须带拍摄时间，否则无法判断新旧"
    assert snap.get("quota_total_tokens") == QUOTA
    for name, meta in models.items():
        pct = meta["remaining_pct"]
        assert 0 <= pct <= 100, f"{name} 剩余比例越界: {pct}"
        assert meta.get("expires_at"), f"{name} 缺到期时间"
        assert meta.get("status"), f"{name} 缺后台状态"


def test_repo_rotation_models_all_have_free_quota():
    """轮换列表里的每个模型都必须能在后台快照里找到 —— 这是「不按量计费」的唯一保证。"""
    import constants
    snap = json.loads((ROOT / "data" / "llm_quota_snapshot.json").read_text(encoding="utf-8"))
    models = snap["models"]
    missing = [m for m in constants.HY3_ROTATION_MODELS if m not in models]
    assert not missing, f"轮换列表含后台无免费额度的模型（按量计费风险）: {missing}"


def test_real_snapshot_sync_matches_ledger_shape():
    """拿真实快照跑一遍：账本里每个模型都得有 total/pct/到期三件套。"""
    snap = json.loads((ROOT / "data" / "llm_quota_snapshot.json").read_text(encoding="utf-8"))
    new, _, _, errors = sq.sync(snap, {}, quota=QUOTA, rotation=[])
    assert not errors
    for name, rec in new.items():
        assert "total_tokens" in rec
        assert "free_quota_pct" in rec
        assert "free_quota_expires_at" in rec


# ============================================================
# 8. 验收脚本的一致性判定必须带方向性（少记才危险，多记只是账本更新）
# ============================================================

_vspec = importlib.util.spec_from_file_location(
    "verify_llm_quota", ROOT / "scripts" / "verify_llm_quota.py")
vq = importlib.util.module_from_spec(_vspec)
_vspec.loader.exec_module(vq)


def _ledger(**models):
    """账本片段：name=(total_tokens, pct, 到期)。"""
    return {n: {"total_tokens": t, "free_quota_pct": p, "free_quota_expires_at": e}
            for n, (t, p, e) in models.items()}


def _one_snapshot(pct=41.1, exp="2027-09-14 09:24:30"):
    return {"captured_at": "2026-11-03 10:29:34", "quota_total_tokens": QUOTA,
            "models": {"m": {"remaining_pct": pct, "expires_at": exp,
                             "status": "运行中", "enabled": True}}}


def test_verify_passes_when_ledger_equals_snapshot():
    rep = vq.check_ledger_matches_snapshot(_one_snapshot(), _ledger(m=(589_000, 41.1, "2027-09-14 09:24:30")))
    assert rep["ok"] is True


def test_verify_passes_when_ledger_grew_after_snapshot():
    """快照是 10:29 的时点值，之后 CI 又跑了批次 —— 账本变大是正常的，不许判红。

    如果这里判红，每次批次结束这条硬项都会失败，最后只能加 || echo 软化，
    等于把「防按量计费」的唯一检查废掉。
    """
    rep = vq.check_ledger_matches_snapshot(_one_snapshot(),
                                          _ledger(m=(620_000, 41.1, "2027-09-14 09:24:30")))
    assert rep["ok"] is True
    assert any("校准后又消耗" in d for d in rep["detail"])


def test_verify_fails_when_ledger_undercounts():
    """本地少记 = 以为还有额度 = 直接按量扣钱 —— 这是唯一必须硬拦的方向。"""
    rep = vq.check_ledger_matches_snapshot(_one_snapshot(),
                                          _ledger(m=(276_000, 41.1, "2027-09-14 09:24:30")))
    assert rep["ok"] is False
    assert any("少记" in d or "按量计费" in d for d in rep["detail"])


def test_verify_fails_when_pct_drifted():
    """剩余%与快照不一致 → 说明有人在手改账本或没跑同步。"""
    rep = vq.check_ledger_matches_snapshot(_one_snapshot(),
                                          _ledger(m=(589_000, 72.4, "2027-09-14 09:24:30")))
    assert rep["ok"] is False


def test_verify_fails_when_expiry_drifted():
    rep = vq.check_ledger_matches_snapshot(_one_snapshot(),
                                          _ledger(m=(589_000, 41.1, "2027-01-01 00:00:00")))
    assert rep["ok"] is False


def test_verify_fails_when_stopped_model_has_total():
    """停用模型写 total 会把 hy3 顶到用量第一（纯噪音），必须拦住。"""
    snap = {"captured_at": "x", "quota_total_tokens": QUOTA,
            "models": {"hy3": {"remaining_pct": 0.0, "expires_at": "2027-08-11 11:04:10",
                               "status": "已停止", "enabled": False}}}
    rep = vq.check_ledger_matches_snapshot(
        snap, {"hy3": {"total_tokens": 1_000_000, "free_quota_pct": 0.0,
                       "free_quota_expires_at": "2027-08-11 11:04:10", "disabled": True}})
    assert rep["ok"] is False


def test_verify_usable_pool_counts_correctly():
    snap = {"captured_at": "x", "quota_total_tokens": QUOTA,
            "models": {f"m{i}": {"remaining_pct": p, "expires_at": "2027-01-01 00:00:00",
                                 "status": "运行中", "enabled": True}
                       for i, p in enumerate([90, 80, 50, 30, 5])}}
    rep = vq.check_usable_pool(snap)
    # 剩 5% 和 80%（已用 20% → 正好 80% 是停用线，仍算可用）要按实际口径数
    assert "可用" in rep["detail"][0]
    assert rep["ok"] is True


def test_verify_stop_line_is_80pct():
    """报表/告警/验收三处必须用同一个实际停用线，否则「报表说能用、运行期已跳过」。"""
    assert vq._stop_line() == pytest.approx(0.80)
    assert sq._skip_line(QUOTA) == pytest.approx(0.80)


# ============================================================
# 9. 单模型额度 ≠ 全局额度（hy4 = 1000 万，其余 100 万）
# ============================================================

def test_per_model_quota_overrides_global():
    """条目里的 quota_total_tokens 必须压过全局值。

    若按全局 100 万折算：hy4 后台剩 95%（= 已用 50 万）会被算成「已用 5 万」，
    本地少记 10 倍，正是「以为还有额度、后台已扣钱」那条事故的原样复现。
    """
    snap = {"captured_at": "2026-11-09 11:45:57", "quota_total_tokens": QUOTA,
            "models": {"hy4": {"quota_total_tokens": 10_000_000, "remaining_pct": 95.0,
                               "expires_at": "2027-11-09 11:41:10",
                               "status": "已上线", "enabled": True}}}
    new, _, _, errors = sq.sync(snap, {}, quota=QUOTA, rotation=[])
    assert not errors
    assert new["hy4"]["total_tokens"] == 500_000          # 1000 万 × 5%
    assert new["hy4"]["free_quota_total_tokens"] == 10_000_000


def test_sync_always_writes_quota_so_shrink_does_not_lag():
    """额度回落（10M → 1M）时必须覆盖旧值，否则运行期还按 10 倍额度算。"""
    snap = {"captured_at": "x", "quota_total_tokens": QUOTA,
            "models": {"hy4": {"remaining_pct": 100.0, "expires_at": "2027-11-09 11:41:10",
                               "status": "已上线", "enabled": True}}}
    usage = {"hy4": {"total_tokens": 0, "free_quota_total_tokens": 10_000_000}}
    new, _, _, _ = sq.sync(snap, usage, quota=QUOTA, rotation=[])
    assert new["hy4"]["free_quota_total_tokens"] == QUOTA
