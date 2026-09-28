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

    rows = []
    for m in models:
        rec = usage.get(m, {})
        used = rec.get("total_tokens", 0)
        remaining = max(0, quota - used)
        rows.append((m, used, remaining, rec.get("disabled", False)))

    # 已消耗的排前面（按已用降序），全 0 的靠后
    rows.sort(key=lambda r: (-r[1], r[0]))
    consumed = [r for r in rows if r[1] > 0]
    untouched = [r for r in rows if r[1] == 0]

    lines = []
    lines.append(f"📅 {datetime.now().strftime('%Y-%m-%d')}（周一）— 免费额度校准提醒")
    lines.append("")
    lines.append("TokenHub 无额度 API，本地计数需手工与后台对齐，请对照后台截图核对。")
    lines.append("")
    lines.append(f"⚙️ 当前规则：已用 ≥{threshold_pct:.0%}（剩余 ≤{(1-threshold_pct):.0%}）切换，"
                 f"硬上限 {LLM_HARD_CAP_TOKENS:,} tokens，"
                 f"{'剩余降序' if LLM_SORT_BY_REMAINING else '原顺序'}轮换")
    lines.append("")

    if consumed:
        lines.append("【本地已消耗模型】")
        for m, used, remaining, disabled in consumed:
            flag = " 🚫已禁用" if disabled else ""
            bar = _bar(used / quota if quota else 0)
            lines.append(f"· {m}")
            lines.append(f"   已用 {used:,} / {quota:,}（{used / quota:.1%}） 剩余 {remaining:,}{flag}")
            lines.append(f"   {bar}")
    else:
        lines.append("【本地已消耗模型】无（全部为 0，可能刚重置）")

    lines.append("")
    lines.append(f"【未消耗模型】{len(untouched)} 个仍为 100%")
    lines.append("")

    # 提醒重点
    warn = [r for r in consumed if not r[3] and r[1] >= quota * threshold_pct]
    nearcap = [r for r in consumed if not r[3] and r[1] >= LLM_HARD_CAP_TOKENS]
    lines.append("【本周待办】")
    lines.append("1. 打开 TokenHub 后台，核对上表各模型「剩余额度」是否与本地一致")
    lines.append("2. 若偏差较大（本地少记），以**后台为准**修改 data/llm_usage.json")
    lines.append("3. 有新模型上线 / 旧模型下架时，同步更新 constants.py 的轮换列表")
    if warn:
        names = "、".join(f"{r[0]}(剩{r[2]:,})" for r in warn)
        lines.append(f"⚠️ 已达切换阈值（本地将跳过）：{names}")
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
