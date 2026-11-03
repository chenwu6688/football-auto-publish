#!/usr/bin/env python3
"""把 TokenHub 后台额度快照同步进本地账本 data/llm_usage.json。

为什么要有这个脚本
------------------
TokenHub **没有额度查询 API**（/v1/user/balance、/v1/dashboard/billing/*、/v1/quota、
/v1/usage 全部 404），后台「模型信息」页是唯一真值。过去校准靠手改 JSON：
既没留证据（改完不知道依据哪张截图、改了哪些字段），也容易漏改一个模型
——而本地账本 `total_tokens` 偏少正是**超额扣费**的直接原因（后台按量计费，
本地还以为有免费额度）。2026-11-01 体检刚吃过「有结论没留证据 = 黑箱」的亏。

数据流
------
    data/llm_quota_snapshot.json   ← 人工照后台填（唯一真值，带 captured_at）
              │  scripts/sync_llm_quota.py
              ▼
    data/llm_usage.json            ← 运行期 utils.call_llm_json 读它排序/跳过

同步口径（严格按后台，不用本地推测）
------------------------------------
    total_tokens        = quota_total × (100 - remaining_pct) / 100   # 后台「已用」
    free_quota_pct      = remaining_pct                              # 原样落账
    free_quota_expires_at = expires_at                               # 原样落账
    quota_synced_at     = 快照 captured_at                           # 本次校准时间
保留不动：prompt_tokens / completion_tokens（本地记账子集）/ disabled / fail_streak。
**注意** prompt+completion 不等于 total（历史上后台校准重置过总量），这不是 bug：
total 是后台真值，前两者是本地累计明细，两者本来就不对等。

状态映射
--------
    status 含「已停止」/ enabled=false / remaining_pct<=0  → disabled=true + disabled_reason
    status 含「待下线」                                    → 不 disabled，但打 note（末位备援）

三道硬校验（任一不过 → 退出码 1）
--------------------------------
  1. 轮换列表里的 TokenHub 模型必须在快照里（否则等于在用一个「后台查不到额度」的
     模型 —— 那就是按量计费，实测不报错、直接扣钱，最危险的一类）；
  2. 轮换列表里的模型不能是 disabled / 剩余 0%（hy3 就是这种：已停止 + 0%）；
  3. 账本里有、快照里没有的模型 → 报 stale 警告（不自动删，等人工确认）。

用法
----
    python3 scripts/sync_llm_quota.py --dry-run   # 只看 diff，不落盘
    python3 scripts/sync_llm_quota.py             # 落盘
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from constants import (  # noqa: E402
    HY3_ROTATION_MODELS,
    LLM_FREE_QUOTA_TOKENS,
    LLM_HARD_CAP_TOKENS,
    LLM_USAGE_FILE,
    LLM_USAGE_THRESHOLD,
)

SNAPSHOT_FILE = ROOT / "data" / "llm_quota_snapshot.json"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"❌ 找不到 {path}")
    except Exception as e:
        raise SystemExit(f"❌ {path} 解析失败: {e}")


def _used_tokens(rec: dict) -> int:
    return int(rec.get("total_tokens") or 0)


def _is_stopped(meta: dict) -> tuple[bool, str]:
    """按后台状态判「不该再被调用」。返回 (disabled, reason)。"""
    status = str(meta.get("status") or "")
    pct = meta.get("remaining_pct")
    if "已停止" in status:
        return True, f"后台状态「{status}」，剩余 {pct}%"
    if meta.get("enabled") is False:
        return True, "后台未启用（启用 Token 计费开关为关）"
    if pct is not None and float(pct) <= 0:
        return True, f"免费额度剩余 {pct}%"
    return False, ""


def sync(snapshot: dict, usage: dict, *, quota: int,
         rotation=None) -> tuple[dict, list[str], list[str], list[str]]:
    """返回 (新账本, 变更明细, 警告, 错误)。纯函数，便于单测。

    `rotation` 显式传入轮换列表便于测试（默认读 constants.HY3_ROTATION_MODELS）。
    """
    models = snapshot.get("models") or {}
    rotation = HY3_ROTATION_MODELS if rotation is None else rotation
    if not models:
        raise SystemExit("❌ 快照里没有 models，拒绝同步（空快照会把账本清零）")

    captured = str(snapshot.get("captured_at") or "")
    q = float(snapshot.get("quota_total_tokens") or quota)
    new = {k: dict(v) for k, v in usage.items() if isinstance(v, dict)}
    changes: list[str] = []
    warns: list[str] = []
    errors: list[str] = []

    for name, meta in models.items():
        pct = meta.get("remaining_pct")
        if pct is None:
            errors.append(f"{name}: 快照缺 remaining_pct（不许用 0 或猜一个顶上）")
            continue
        pct = float(pct)
        target_total = int(round(q * (100.0 - pct) / 100.0))
        rec = new.setdefault(name, {})
        old_total = _used_tokens(rec)
        old_pct = rec.get("free_quota_pct")
        stopped, reason = _is_stopped(meta)

        # 后台真值覆盖本地推测：total 抬高＝本地少记（有超额风险），必须覆盖。
        # 但**已停用的模型不写**：hy3 后台剩 0% 会被算成「已用 100 万」，直接冲到
        # 用量排行榜第一，纯噪音；它本来也不会被调用（_is_model_available 先看 disabled）。
        if not stopped:
            rec["total_tokens"] = target_total
        else:
            rec.setdefault("total_tokens", 0)
        rec.setdefault("prompt_tokens", 0)
        rec.setdefault("completion_tokens", 0)
        rec["free_quota_pct"] = pct
        if meta.get("expires_at"):
            rec["free_quota_expires_at"] = meta["expires_at"]
        rec["quota_synced_at"] = captured
        if meta.get("ui_name"):
            rec["ui_name"] = meta["ui_name"]   # UI 名 ≠ API ID，别拿 UI 名去调用

        if stopped:
            rec["disabled"] = True
            rec["disabled_reason"] = reason
        else:
            rec.pop("disabled", None)
            rec.pop("disabled_reason", None)
        if meta.get("note"):
            rec["note"] = meta["note"]

        if stopped:
            # 停用模型不记 total（见上），变更说明也只说状态，不报一个假的用量差
            changes.append(f"{name}: 剩余 {old_pct if old_pct is not None else '?'}% → {pct}%"
                           f"（已标停用：{reason}）")
        else:
            delta = target_total - old_total
            if abs(delta) >= 1000 or old_pct != pct:
                sign = "+" if delta > 0 else ""
                changes.append(
                    f"{name}: 剩余 {old_pct if old_pct is not None else '?'}% → {pct}%"
                    f"（本地已用 {old_total:,} → 后台 {target_total:,}，Δ{sign}{delta:,}）"
                )

    # 账本里有、后台快照里没有 → 大概率是「按量计费」或已下架，绝不能当免费额度用
    for name in new:
        if name in models:
            continue
        new[name]["stale_since"] = captured
        warns.append(f"{name}: 账本有记录但后台快照无此模型（可能已下架或按量计费），"
                     f"已标 stale_since，请人工确认后再决定是否从轮换列表移除")

    # 硬校验：轮换列表只能放「后台确认有免费额度且未停止」的模型
    for m in rotation:
        if m not in models:
            errors.append(f"轮换列表含 {m}，但后台快照里没有它 —— "
                          f"这是按量计费/已下架的信号，继续用会直接扣钱")
            continue
        stopped, reason = _is_stopped(models[m])
        if stopped:
            errors.append(f"轮换列表含 {m}，但{reason} —— 必须从 constants.HY3_ROTATION_MODELS 移除")
        elif float(models[m].get("remaining_pct") or 0) <= (1 - _skip_line(quota)) * 100:
            warns.append(f"轮换列表含 {m}，剩余 {models[m].get('remaining_pct')}% "
                         f"已到实际停用线（已用 ≥{_skip_line(quota):.0%}），运行期会被自动跳过")

    return new, changes, warns, errors


def _skip_line(quota: int) -> float:
    """运行期实际生效的「已用比例」停用线。

    账本里有两个阈值同时存在：LLM_USAGE_THRESHOLD（默认 90%）与
    LLM_HARD_CAP_TOKENS（默认 80 万 = 80%）。后者更严，谁先到谁生效 ——
    所以真实停用线是 min(0.90, 0.80) = 80%，不是注释里常说的 90%。
    报告必须按实际生效线标「将跳过」，否则会出现「报表说还能用、运行期已跳过」。
    """
    cap_ratio = (LLM_HARD_CAP_TOKENS / quota) if quota else 1.0
    return min(LLM_USAGE_THRESHOLD, cap_ratio)


def render(changes, warns, errors, *, quota: int, usage: dict, snapshot: dict) -> str:
    stop_ratio = _skip_line(quota)
    lines = [f"后台快照时间: {snapshot.get('captured_at')}  模型数: {len(snapshot.get('models') or {})}",
             f"单模型免费额度: {quota:,} tokens  "
             f"实际停用线: 已用 ≥{stop_ratio:.0%}"
             f"（threshold {LLM_USAGE_THRESHOLD:.0%} 与硬上限 {LLM_HARD_CAP_TOKENS:,} 取更严者）", ""]
    lines.append("【本次变更】" if changes else "【本次变更】无（账本已与后台一致）")
    lines += [f"  · {c}" for c in changes]

    # 全量余额表（按到期日近→远，与运行期排序同向，方便人眼核对）
    lines.append("")
    lines.append("【同步后余额】")
    rows = []
    for name, meta in (snapshot.get("models") or {}).items():
        rec = usage.get(name, {})
        rows.append((meta.get("expires_at") or "9999", name, meta.get("remaining_pct"),
                     _used_tokens(rec), rec.get("disabled")))
    for exp, name, pct, used, dis in sorted(rows):
        flag = " 🚫已停用" if dis else (" ⚠️将跳过" if (pct or 0) <= (1 - stop_ratio) * 100 else "")
        lines.append(f"  {exp[:10]}  {name:<42} 剩 {pct:>5.1f}%  已用 {used:>9,}{flag}")

    if warns:
        lines.append("")
        lines.append("【警告】")
        lines += [f"  ⚠️ {w}" for w in warns]
    if errors:
        lines.append("")
        lines.append("【硬校验失败】")
        lines += [f"  ❌ {e}" for e in errors]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印 diff，不写账本")
    ap.add_argument("--snapshot", default=str(SNAPSHOT_FILE))
    ap.add_argument("--usage", default=str(LLM_USAGE_FILE))
    args = ap.parse_args()

    snapshot = _load_json(Path(args.snapshot))
    usage_path = Path(args.usage)
    usage = _load_json(usage_path) if usage_path.exists() else {}

    new, changes, warns, errors = sync(snapshot, usage, quota=LLM_FREE_QUOTA_TOKENS)
    print(render(changes, warns, errors, quota=LLM_FREE_QUOTA_TOKENS,
                 usage=new, snapshot=snapshot))

    if errors:
        print("\n❌ 硬校验未过，账本未写入（先修轮换列表再同步）")
        return 1

    if args.dry_run:
        print("\n（--dry-run，未写入）")
        return 0

    # 说明键保留在最前，便于人读
    out = {}
    note = usage.get("_calibration_note")
    if note:
        out["_calibration_note"] = note
    out.update({k: v for k, v in new.items() if not k.startswith("_")})
    usage_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n✅ 已写入 {usage_path}（{len([k for k in out if not k.startswith('_')])} 个模型）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
