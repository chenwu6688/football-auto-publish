#!/usr/bin/env python3
"""每周一 LLM 免费额度校准提醒（WxPusher 推送）。

背景
----
TokenHub 后台**没有额度查询 API**（/v1/user/balance、/v1/dashboard/billing/*、/v1/quota、
/v1/usage 全部 404），本地 `data/llm_usage.json` 与后台真实用量只能**手工对齐**。
为避免「本地少记 → 后台超额扣费」，除代码里的硬上限保险丝外，
再增加一道**人工校准提醒**：每周一自动推送本地用量快照，提醒对照后台校准。

用法
----
    python3 scripts/remind_llm_calibration.py            # 打印 + 推送（如已配置凭证）
    python3 scripts/remind_llm_calibration.py --dry-run  # 只打印，不推送

凭证从环境变量读取：WXPUSHER_APPTOKEN / WXPUSHER_UID（与其它脚本一致）。
未配置时静默跳过推送（不报错），便于本地调试。
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from constants import (  # noqa: E402
    LLM_USAGE_FILE,
    LLM_FREE_QUOTA_TOKENS,
    LLM_USAGE_THRESHOLD,
    LLM_HARD_CAP_TOKENS,
    LLM_SORT_BY_REMAINING,
    LLM_JSON_CANDIDATES,
    WXPUSHER_APPTOKEN,
    WXPUSHER_UID,
)


def _load_usage():
    import json
    try:
        data = json.loads(LLM_USAGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    # 去掉 _calibration_note 之类的说明键
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def build_report():
    """构造校准提醒正文（本地用量快照 + 待办清单）。"""
    usage = _load_usage()
    quota = LLM_FREE_QUOTA_TOKENS
    threshold_pct = LLM_USAGE_THRESHOLD

    # 收集所有候选模型 + 任何本地有记录的模型
    models = [m for _u, _k, m in LLM_JSON_CANDIDATES]
    for m in usage:
        if m not in models:
            models.append(m)

    # 后台快照里已查不到的模型（sync 打的 stale_since）：它们不在轮换里、也不该再
    # 出现在「该校准哪几个」的清单里 —— 否则每天催用户去核对 7 个已经不存在的模型。
    stale = [m for m in models if usage.get(m, {}).get("stale_since")]
    models = [m for m in models if m not in stale]

    # 实际停用线：threshold(90%) 与硬上限(80 万=80%) 取更严者 → 真实是 80%
    stop = min(threshold_pct, (LLM_HARD_CAP_TOKENS / quota) if quota else 1.0)

    rows = []
    for m in models:
        rec = usage.get(m, {})
        used = rec.get("total_tokens", 0)
        # 额度可能是本模型专属的（hy4 = 1000 万），按全局 100 万算会把剩余写错 10 倍
        q_model = float(rec.get("free_quota_total_tokens") or quota)
        remaining = max(0, int(q_model) - used)
        pct = rec.get("free_quota_pct")     # 后台剩余%，缺失=不知道，别拿 100 冒充
        rows.append((m, used, remaining, rec.get("disabled", False), pct, q_model))

    # 后台剩余低的排前面（「谁快用完」比「谁用得多」更该看），未知剩余的靠后
    rows.sort(key=lambda r: (r[4] if r[4] is not None else 999, -r[1], r[0]))
    consumed = [r for r in rows if r[1] > 0]
    # 「未消耗」= 一次都没调用且仍健康。已停用的必须单独拎出来：
    # hy3 的 total 是 0（停用不记账），按计数会被算成「仍为 100%」，纯误导。
    untouched = [r for r in rows if r[1] == 0 and not r[3]]
    stopped = [r for r in rows if r[1] == 0 and r[3]]

    lines = []
    now = datetime.now()
    lines.append(f"📅 {now.strftime('%Y-%m-%d')}（周{'一二三四五六日'[now.weekday()]}）"
                 f"— 免费额度校准提醒")
    lines.append("")
    lines.append("TokenHub 无额度 API，本地计数需手工与后台对齐，请对照后台截图核对。")
    lines.append("")
    lines.append(f"⚙️ 当前规则：**实际停用线 已用 ≥{stop:.0%}**"
                 f"（threshold {threshold_pct:.0%} 与硬上限 {LLM_HARD_CAP_TOKENS:,} "
                 f"取更严者，不是注释里常说的 90%），"
                 f"{'到期近→远' if LLM_SORT_BY_REMAINING else '原顺序'}轮换")
    synced = max((r.get("quota_synced_at") or "" for r in usage.values()), default="")
    if synced:
        lines.append(f"🗂️ 账本上次对齐后台：{synced}（来源 data/llm_quota_snapshot.json）")
    else:
        lines.append("🗂️ 账本没有 quota_synced_at —— 从来没跟后台对齐过，请优先补一次")
    lines.append("")

    if consumed:
        lines.append("【本地已消耗模型】（按后台剩余升序）")
        for m, used, remaining, disabled, pct, q_model in consumed:
            if disabled:
                flag = " 🚫已停用"
            elif pct is not None and pct <= (1 - stop) * 100:
                flag = " ⚠️已过停用线，运行期跳过"
            else:
                flag = ""
            ratio = used / q_model if q_model else 0
            bar = _bar(ratio)
            shown = f"后台剩 {pct}%" if pct is not None else "后台剩 missing"
            lines.append(f"· {m}")
            lines.append(f"   已用 {used:,} / {int(q_model):,}（{ratio:.1%}） {shown}"
                         f" 剩 {remaining:,}{flag}")
            lines.append(f"   {bar}")
    else:
        lines.append("【本地已消耗模型】无（全部为 0，可能刚重置）")

    lines.append("")
    lines.append(f"【未消耗模型】{len(untouched)} 个仍为 100%")
    if stopped:
        lines.append(f"【已停用】{len(stopped)} 个："
                     + "、".join(f"{r[0]}（后台剩 {r[4]}%）" for r in stopped))
    if stale:
        lines.append(f"【后台快照已查不到】{len(stale)} 个：{'、'.join(stale)}")
        lines.append("   已不在轮换里，不再参与调度；确认下架后可从 data/llm_usage.json 移除")
    lines.append("")

    # 提醒重点
    warn = [r for r in consumed if not r[3] and r[1] >= r[5] * stop]
    nearcap = [r for r in consumed
               if not r[3] and r[1] >= LLM_HARD_CAP_TOKENS * (r[5] / quota if quota else 1.0)]
    lines.append("【本周待办】")
    lines.append("1. 打开 TokenHub 后台「模型信息」页，核对上表各模型剩余额度")
    lines.append("2. 有偏差：把后台数值填进 data/llm_quota_snapshot.json（captured_at 一并改），"
                 "再跑 `python3 scripts/sync_llm_quota.py` —— **不要手改 data/llm_usage.json**"
                 "（手改没留证据，少记就会按量扣钱）")
    lines.append("3. 有新模型上线 / 旧模型下架时，同步更新 constants.py 的轮换列表"
                 "（只放后台确认有免费额度的；UI 名≠API ID，务必按 /v1/models 的 ID 写）")
    if warn:
        names = "、".join(f"{r[0]}(剩{r[2]:,})" for r in warn)
        lines.append(f"⚠️ 已达实际停用线（运行期将跳过）：{names}")
    if nearcap:
        names = "、".join(r[0] for r in nearcap)
        lines.append(f"⚠️ 已达本地硬上限 {LLM_HARD_CAP_TOKENS:,}：{names}")

    return "\n".join(lines)


def _bar(ratio, width=12):
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled) + f" {ratio:.0%}"


def send(title, content):
    if not WXPUSHER_APPTOKEN or not WXPUSHER_UID:
        print("⚠️ 未配置 WXPUSHER_APPTOKEN / WXPUSHER_UID，跳过推送")
        return False
    try:
        import requests
        resp = requests.post(
            "https://wxpusher.zjiecode.com/api/send/message",
            json={"appToken": WXPUSHER_APPTOKEN,
                  "content": f"{title}\n\n{content}",
                  "contentType": 1,
                  "uids": [WXPUSHER_UID]},
            timeout=10,
        )
        # WxPusher 返回 {"code":1000,"msg":"处理成功","data":[messageIds]}
        # code=1000 才是真正受理成功（HTTP 200 不代表业务成功）。
        try:
            body = resp.json()
        except Exception:
            body = {}
        code = body.get("code")
        msg = body.get("msg", "")
        ids = body.get("data") or []
        ok = resp.status_code == 200 and code == 1000
        if ok:
            print(f"✅ 校准提醒已推送: HTTP {resp.status_code}, code={code}, "
                  f"msg={msg}, messageIds={ids}")
        else:
            print(f"❌ 校准提醒推送未成功: HTTP {resp.status_code}, code={code}, "
                  f"msg={msg}, body={str(body)[:300]}")
        return ok
    except Exception as e:
        print(f"⚠️ WxPusher 推送失败: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    args = ap.parse_args()

    report = build_report()
    print("=" * 60)
    print(report)
    print("=" * 60)

    if not args.dry_run:
        ok = send("🔔 每周免费额度校准提醒", report)
        if not ok:
            # 推送失败让 Actions 运行标红，避免「静默失联」——提醒本身就是保险机制。
            print("❌ 提醒推送失败，返回非零退出码以便 Actions 报警")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
