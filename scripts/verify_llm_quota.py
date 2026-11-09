#!/usr/bin/env python3
"""LLM 免费额度账本验收（2026-11-03 三张后台截图全量同步）。

为什么要有这个脚本
------------------
TokenHub **没有额度查询 API**，后台「模型信息」页是唯一真值，靠人工截图 + 手填。
手填有两个致命失败模式，而且都不会报错：

  ① **本地少记**——账本 `total_tokens` 比后台少，运行期便以为还有额度，
     继续往一个其实快用完的模型上打请求 → **直接按量计费**（实测不报 402、直接扣钱）。
  ② **模型名错位**——后台 UI 显示 `mimo-v2.5-pro`，`/v1/models` 里真实 ID 是
     `mimo-v2.6-pro`；拿 UI 名去调用必 404，然后被当成「模型故障」沉底。
  2026-11-03 同步时本地账本整体偏少：vision-exp 少记 9.0 万、step-5 少记 8.1 万、
  deepseek-flash 少记 7.7 万、0731 少记 7.5 万。

硬项（FAIL → 退出码 1）
----------------------
  A. 账本不低于快照：`total_tokens >= round(本模型额度 × (100-剩余%))`
     （本模型额度 = 条目里的 quota_total_tokens，缺省用全局值；hy4 是 1000 万），
     `free_quota_pct` / `free_quota_expires_at` 与快照完全相同；
     停用模型不写 total（写 100 万会把 hy3 顶到用量排行第一）。
     **方向性**：快照是时点值，之后 CI 还会继续消耗，账本只会变大 ——
     要求严格相等会让每批跑完都判红，最后被迫软化。少记才危险（按量扣钱），
     多记只是账本更新。
  B. 轮换列表 ⊆ 快照：任何轮换模型在快照里查不到 = 按量计费/已下架，直接 FAIL；
     在快照里但已停止/未启用/额度 0% 也 FAIL（hy3 就是这种）
  C. 可用池容量：扣掉停用 + 已过实际停用线的模型后，剩余可用 ≥ 4 个；
     8~4 个之间只告警（跑一天的批次约耗 5 万 tokens，池子不需要很大）
  D. 负向验证：人为把某个模型的 pct 改 1 个点，A 项必须变 FAIL ——
     证明一致性检查真的在比对，而不是恒真

告警（不阻断）
------------
  · 账本有、快照没有的模型（stale）：可能只是这次截图没截到，标了 `stale_since` 等人工确认；
  · 硬上限 `LLM_HARD_CAP_TOKENS` 与单模型额度之比落在 (0, 0.5) 区间：说明套餐变了，
    80 万这个绝对值已经不合时宜（额度涨到 1000 万时它会只剩 8% 可用）；
  · 轮换列表里已过实际停用线的模型（剩余 ≤20%）：运行期会自动跳过，属于正常现象，
    但如果全池有 1/3 以上都在线上，说明该充值或减量了。

口径说明：实际停用线 = min(LLM_USAGE_THRESHOLD, LLM_HARD_CAP_TOKENS/quota)
= min(90%, 80%) = **80%**，不是注释里常说的 90%。两个阈值并存，谁先到谁生效。

产出 `data/audit/llm_quota_<date>.md` + `llm_quota_latest.json`。

用法：python3 scripts/verify_llm_quota.py [--quiet]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from constants import (  # noqa: E402
    HY3_ROTATION_MODELS,
    LLM_FREE_QUOTA_TOKENS,
    LLM_HARD_CAP_TOKENS,
    LLM_USAGE_FILE,
    LLM_USAGE_THRESHOLD,
)

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
SNAPSHOT_FILE = PROJECT_ROOT / "data" / "llm_quota_snapshot.json"
CST = timezone(timedelta(hours=8))

MIN_USABLE = 4      # 少于这个数 → 硬 FAIL
WARN_USABLE = 8     # 少于这个数 → 告警


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return {}


def _stop_line(quota=None):
    """实际生效的「已用比例」停用线：两个阈值取更严者。"""
    quota = quota or LLM_FREE_QUOTA_TOKENS
    cap_ratio = (LLM_HARD_CAP_TOKENS / quota) if quota else 1.0
    return min(LLM_USAGE_THRESHOLD, cap_ratio)


def _is_stopped(meta):
    status = str(meta.get("status") or "")
    pct = meta.get("remaining_pct")
    if "已停止" in status:
        return True, f"后台已停止（{status}）"
    if meta.get("enabled") is False:
        return True, "后台未启用计费开关"
    if pct is not None and float(pct) <= 0:
        return True, f"剩余 {pct}%"
    return False, ""


# ---------------------------------------------------------------- A
def check_ledger_matches_snapshot(snapshot=None, usage=None):
    """账本必须与快照逐项对得上（四舍五入到 tokens）。"""
    snapshot = snapshot if snapshot is not None else _load(SNAPSHOT_FILE)
    usage = usage if usage is not None else (_load(LLM_USAGE_FILE) or {})
    if not snapshot or not snapshot.get("models"):
        return {"item": "账本与快照一致", "ok": True, "advisory": True,
                "detail": ["⚠️ 无快照，跳过（先按后台截图填 data/llm_quota_snapshot.json）"]}

    q = float(snapshot.get("quota_total_tokens") or LLM_FREE_QUOTA_TOKENS)
    bad, ok_n, stopped_n, detail_extra = [], 0, 0, []
    for name, meta in snapshot["models"].items():
        pct = meta.get("remaining_pct")
        rec = usage.get(name)
        if rec is None:
            bad.append(f"{name}: 账本里没有这条记录（同步没跑到它？）")
            continue
        stopped, _why = _is_stopped(meta)
        if stopped:
            stopped_n += 1
            if rec.get("disabled") is not True:
                bad.append(f"{name}: 后台已停用但账本未标 disabled")
            if rec.get("total_tokens"):
                bad.append(f"{name}: 停用模型不应写 total_tokens"
                           f"（现值 {rec.get('total_tokens'):,}，会污染用量排行）")
            continue
        # 额度可能按模型不同（hy4 = 1000 万，其余 100 万）。用全局值折算等于把
        # 「后台已用 5%」算成 5 万，而真实是 50 万 —— 少记 10 倍，A 项还判绿。
        qm = float(meta.get("quota_total_tokens") or q)
        expect = int(round(qm * (100.0 - float(pct)) / 100.0))
        got = int(rec.get("total_tokens") or 0)
        # 只判「不能少于后台推算值」。快照是 **10:29 的时点值**，之后 CI 还会继续
        # 消耗，账本必然越跑越大 —— 要求严格相等会让每次批次结束后这条硬项都红，
        # 最后被迫加 || echo 软化，等于没检查。
        # 方向性很重要：少记 = 以为还有额度 = 按量扣钱；多记 = 只是账本更新。
        if got < expect:
            bad.append(f"{name}: total_tokens {got:,} < 后台推算 {expect:,}"
                       f"（剩余 {pct}%）—— 本地少记，会按量计费")
        elif got > expect + int(qm * 0.005):     # 超过 0.5% 才提，避免 round 误差刷屏
            detail_extra.append(f"{name}: 账本 {got:,} > 快照推算 {expect:,}"
                                f"（校准后又消耗了 {got - expect:,}，正常）")
        if float(rec.get("free_quota_pct", -1)) != float(pct):
            bad.append(f"{name}: free_quota_pct {rec.get('free_quota_pct')} ≠ {pct}")
        if rec.get("free_quota_expires_at") != meta.get("expires_at"):
            bad.append(f"{name}: 到期时间 {rec.get('free_quota_expires_at')} "
                       f"≠ {meta.get('expires_at')}")
        ok_n += 1

    detail = [f"✅ {ok_n} 个在用模型：total 不低于后台推算值、剩余%与到期时间一致；"
              f"{stopped_n} 个停用模型已标 disabled"]
    detail += [f"  · {d}" for d in detail_extra[:8]]
    if bad:
        detail = [f"❌ {len(bad)} 处不一致"] + [f"  · {b}" for b in bad[:12]]
    return {"item": "账本与快照一致", "ok": not bad, "detail": detail}


# ---------------------------------------------------------------- B
def check_rotation_is_free(snapshot=None):
    """轮换列表只许放「后台确认有免费额度且未停止」的模型。"""
    snapshot = snapshot if snapshot is not None else _load(SNAPSHOT_FILE)
    if not snapshot or not snapshot.get("models"):
        return {"item": "轮换列表合法", "ok": True, "advisory": True,
                "detail": ["⚠️ 无快照，跳过"]}
    models = snapshot["models"]
    bad, warns = [], []
    for m in HY3_ROTATION_MODELS:
        if m not in models:
            bad.append(f"{m}: 后台快照里查不到 —— 按量计费/已下架信号，必须移出轮换列表")
            continue
        stopped, why = _is_stopped(models[m])
        if stopped:
            bad.append(f"{m}: {why} —— 必须移出轮换列表")
            continue
        pct = float(models[m].get("remaining_pct") or 0)
        if pct <= (1 - _stop_line()) * 100:
            warns.append(f"{m}: 剩 {pct}% 已过停用线，运行期会自动跳过（额度重置后自动复活）")

    detail = [f"✅ 轮换列表 {len(HY3_ROTATION_MODELS)} 个模型全部有免费额度且未停止"]
    if bad:
        detail = [f"❌ {len(bad)} 个模型不该在轮换列表里"] + [f"  · {b}" for b in bad]
    detail += [f"  ⚠️ {w}" for w in warns]
    return {"item": "轮换列表合法", "ok": not bad, "detail": detail}


# ---------------------------------------------------------------- C
def check_usable_pool(snapshot=None):
    """可用池容量：扣掉停用 + 已过停用线的，还剩几个真能打。"""
    snapshot = snapshot if snapshot is not None else _load(SNAPSHOT_FILE)
    if not snapshot or not snapshot.get("models"):
        return {"item": "可用池容量", "ok": True, "advisory": True,
                "detail": ["⚠️ 无快照，跳过"]}
    stop = _stop_line()
    usable, out = [], []
    for name, meta in snapshot["models"].items():
        stopped, _why = _is_stopped(meta)
        pct = float(meta.get("remaining_pct") or 0)
        if stopped or pct <= (1 - stop) * 100:
            out.append(f"{name}（剩 {pct}%）")
        else:
            usable.append((name, pct, meta.get("expires_at") or ""))

    total_tokens = sum(int(meta.get("quota_total_tokens") or LLM_FREE_QUOTA_TOKENS) * p / 100.0
                       for _n, p, _e in usable)
    ok = len(usable) >= MIN_USABLE
    detail = [
        f"{'✅' if ok else '❌'} 可用 {len(usable)} 个 / 停用或到线 {len(out)} 个"
        f"（下限 {MIN_USABLE}）",
        f"可用额度合计约 {int(total_tokens):,} tokens（单模型 {LLM_FREE_QUOTA_TOKENS:,}，"
        f"按各模型实际额度累加）",
    ]
    if out:
        detail.append(f"  不可用：{'、'.join(out)}")
    if len(usable) < WARN_USABLE:
        detail.append(f"  ⚠️ 可用模型少于 {WARN_USABLE} 个，轮换余量偏薄（单批约耗 5 万 tokens）")
    return {"item": "可用池容量", "ok": ok, "detail": detail}


# ---------------------------------------------------------------- D
def check_hard_cap_ratio():
    """硬上限是绝对值 80 万：跟单模型额度之比必须还是个像样的百分比。

    额度涨到 1000 万时，80 万 = 8% → 92% 的额度永远用不上，而且不会报错。
    """
    ratio = LLM_HARD_CAP_TOKENS / LLM_FREE_QUOTA_TOKENS if LLM_FREE_QUOTA_TOKENS else 1
    stop = _stop_line()
    detail = [f"硬上限 {LLM_HARD_CAP_TOKENS:,} / 单模型额度 {LLM_FREE_QUOTA_TOKENS:,}"
              f" = {ratio:.0%}；实际停用线 已用 ≥{stop:.0%}"]
    ok = 0.5 <= ratio <= 0.95
    if not ok:
        detail.append(f"⚠️ 比例 {ratio:.0%} 离谱（应在 50%~95%）——套餐变了？"
                      f"改额度必须同时改 LLM_HARD_CAP_TOKENS，否则账本会静悄悄只剩一点可用")
    # 恒为告警项：比例不对值得看，但不该拦住流水线（额度本身没坏）
    return {"item": "硬上限与额度比例", "ok": ok, "advisory": True, "detail": detail}


# ---------------------------------------------------------------- E
def check_negative(snapshot=None):
    """负向验证：篡改 1 个点后 A 项必须 FAIL，否则一致性检查是恒真的摆设。"""
    snapshot = snapshot if snapshot is not None else _load(SNAPSHOT_FILE)
    usage = _load(LLM_USAGE_FILE)
    if not snapshot or not snapshot.get("models") or not usage:
        return {"item": "负向验证", "ok": True, "advisory": True,
                "detail": ["⚠️ 无快照或账本，跳过"]}

    # 优先挑一个「剩余既不是 0 也不是 100」的在用模型：对 100% 的模型改 1 个点，
    # expect 会变成 1~10 万而账本必然是 0，虽然也会报错，但验的是「空账本」不是「真比对」。
    live = [(n, m) for n, m in snapshot["models"].items() if not _is_stopped(m)[0]]
    pick = next(((n, m) for n, m in live
                 if 5 <= float(m.get("remaining_pct") or 0) <= 95), None)
    name = (pick or (live[0] if live else (None, None)))[0]
    if not name:
        return {"item": "负向验证", "ok": True, "advisory": True,
                "detail": ["⚠️ 快照里没有在用模型，跳过"]}
    tampered = json.loads(json.dumps(snapshot))
    pct = float(tampered["models"][name].get("remaining_pct") or 0)
    tampered["models"][name]["remaining_pct"] = round(max(0.0, pct - 1.0), 1)
    rep = check_ledger_matches_snapshot(tampered, usage)
    if rep["ok"]:
        return {"item": "负向验证", "ok": False,
                "detail": [f"❌ 把 {name} 的剩余额度改动 1 个点后一致性检查仍然通过 —— "
                           f"说明它没在真比对数"]}
    return {"item": "负向验证", "ok": True,
            "detail": [f"✅ 篡改 {name}（{pct}% → {tampered['models'][name]['remaining_pct']}%）"
                       f"后一致性检查正确报错"]}


def verify():
    snap = _load(SNAPSHOT_FILE)
    usage = _load(LLM_USAGE_FILE) or {}
    checks = [check_ledger_matches_snapshot(snap, usage),
              check_rotation_is_free(snap),
              check_usable_pool(snap),
              check_hard_cap_ratio(),
              check_negative(snap)]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "snapshot_at": (snap or {}).get("captured_at"),
            "models": len((snap or {}).get("models") or {}),
            "rotation": len(HY3_ROTATION_MODELS),
            "ok": all(c["ok"] for c in hard),
            "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep):
    L = [f"# LLM 免费额度账本验收（快照 {(rep.get('snapshot_at') or 'missing')[:10]}）", "",
         f"- 生成时间：{rep['generated_at']}｜后台快照：{rep.get('snapshot_at') or 'missing'}"
         f"｜模型 {rep['models']} 个｜轮换 {rep['rotation']} 个",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**",
         "- 口径：`total_tokens = 本模型额度 × (100-剩余%)`，额度缺省 1,000,000、"
         "条目里可覆盖（hy4 = 10,000,000），以后台截图为准；"
         "实际停用线 = min(threshold 90%, 硬上限 80%) = **80%**，硬上限随模型额度同比放大",
         ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}：{'✅' if c['ok'] else '❌'}"
                 + ("（告警/跳过）" if c.get("advisory") else ""))
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="LLM 免费额度账本验收")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"llm_quota_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "llm_quota_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
