#!/usr/bin/env python3
"""周一校准推送值守：防 GitHub 整次丢弃 schedule 触发。

背景
----
2026-10-19（周一）实锤：`calibration-reminder` 的 09:03 CST 定时触发**根本没有发生**——
工作流历史只有 4 次运行（9-28 手动×2、10-05 ✓、10-12 ✓），当天既无 failed 也无 queued，
属于 GitHub 侧丢调度（高峰期/平台故障均可能出现），不是脚本或推送故障。
上两次周一运行均 ~10s 成功，脚本本身健康。

一周额度失校的代价是「本地少记 → 后台超额扣费」，而这条提醒是唯一的人工兜底，
所以加一道值守：heartbeat 午检（12:37 CST）时核对今日是否真的跑过该工作流，
一次都没跑就 WxPusher 提醒手动补跑（并给出 Actions 链接）。

判定口径
--------
- 仅在周一执行（`--force` 可跳过该判断，便于补测历史日期）。
- 今日**任意状态**的运行都算「已触发」：queued/in_progress 说明只是延迟，不告警
  （避免与 GitHub 已知的数小时排队延迟重复告警）。
- 只负责提醒，不自动 dispatch：自动补跑需 actions:write 权限，且若延迟运行随后
  出现会重复推送，人工一键补跑更可控。

用法
----
    python3 scripts/check_calibration_push.py               # 检查 + 缺失时告警
    python3 scripts/check_calibration_push.py --dry-run     # 只打印
    python3 scripts/check_calibration_push.py --date 2026-10-12 --force
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))
DEFAULT_WORKFLOW = "calibration-reminder.yml"
API = "https://api.github.com"


def _cst_date(dt_str: str):
    """GitHub 返回的 ISO8601 UTC 时间 → CST 日期。"""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None
    return dt.astimezone(CST).date()


def fetch_runs(repo: str, token: str, workflow: str, per_page: int = 15):
    """取该工作流最近若干次运行。失败返回 None（网络/权限问题，调用方降级跳过）。"""
    import requests

    url = f"{API}/repos/{repo}/actions/workflows/{workflow}/runs"
    try:
        resp = requests.get(
            url,
            params={"per_page": per_page},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"⚠️ 查询运行记录失败: HTTP {resp.status_code} {resp.text[:200]}")
            return None
        return resp.json().get("workflow_runs", [])
    except Exception as e:
        print(f"⚠️ 查询运行记录异常: {type(e).__name__} {str(e)[:200]}")
        return None


def runs_on(runs, target_date):
    """筛出 target_date（CST）当天触发的运行。"""
    hit = []
    for r in runs or []:
        d = _cst_date(r.get("created_at") or r.get("run_started_at") or "")
        if d == target_date:
            hit.append(r)
    return hit


def send_alert(repo: str, workflow: str, target_date) -> bool:
    apptoken = os.environ.get("WXPUSHER_APPTOKEN", "")
    uid = os.environ.get("WXPUSHER_UID", "")
    if not apptoken or not uid:
        print("⚠️ 未配置 WXPUSHER_APPTOKEN / WXPUSHER_UID，跳过推送")
        return False

    link = f"https://github.com/{repo}/actions/workflows/{workflow}"
    content = (
        f"⚠️ 周一校准推送未触发（{target_date}）\n\n"
        f"GitHub 今日没有为「每周额度校准提醒」创建任何运行（非脚本/推送故障）。\n"
        f"请手动补跑一次，核对 TokenHub 后台与本地 data/llm_usage.json：\n{link}\n\n"
        f"（本地账本最近一次校准见 data/llm_usage.json 的 _calibration_note）"
    )
    try:
        import requests

        resp = requests.post(
            "https://wxpusher.zjiecode.com/api/send/message",
            json={
                "appToken": apptoken,
                "content": content,
                "contentType": 1,
                "uids": [uid],
            },
            timeout=10,
        )
        body = {}
        try:
            body = resp.json()
        except Exception:
            pass
        ok = resp.status_code == 200 and body.get("code") == 1000
        print(("✅ 值守告警已推送" if ok else f"❌ 值守告警推送失败: "
               f"HTTP {resp.status_code} {str(body)[:200]}"))
        return ok
    except Exception as e:
        print(f"❌ 值守告警推送异常: {type(e).__name__} {str(e)[:200]}")
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="检查日期（YYYY-MM-DD，CST；默认今天）")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    ap.add_argument("--force", action="store_true", help="忽略「仅周一」限制")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    args = ap.parse_args(argv)

    target = (datetime.strptime(args.date, "%Y-%m-%d").date()
              if args.date else datetime.now(CST).date())

    if target.weekday() != 0 and not args.force:
        print(f"ℹ️ {target} 不是周一（weekday={target.weekday()}），无需值守")
        return 0
    if not args.repo or not args.token:
        print("ℹ️ 未提供 GITHUB_REPOSITORY / GITHUB_TOKEN，无法核对运行记录，跳过")
        return 0

    runs = fetch_runs(args.repo, args.token, args.workflow)
    if runs is None:
        print("ℹ️ 运行记录不可得，跳过（不制造假告警）")
        return 0

    today_runs = runs_on(runs, target)
    if today_runs:
        states = "、".join(f"{r.get('status')}/{r.get('conclusion')}"
                           for r in today_runs[:3])
        print(f"✅ {target} 已有 {len(today_runs)} 次运行（{states}），无需告警")
        return 0

    print(f"⚠️ {target} 无任何「{args.workflow}」运行记录 —— GitHub 疑似丢弃调度触发")
    if args.dry_run:
        print("（dry-run：不推送）")
        return 0
    return 0 if send_alert(args.repo, args.workflow, target) else 1


if __name__ == "__main__":
    sys.exit(main())
