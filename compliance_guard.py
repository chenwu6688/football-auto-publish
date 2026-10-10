"""合规红线熔断（计划第十章 · 硬性门槛）。

计划第十章原文：
    验收分两类：增长指标看效果，合规指标看底线。合规指标是硬性门槛，
    不达标即暂停自动发布。
    合规指标不得妥协。任一阶段出现一次不实内容级违规，立即暂停自动发布，
    回到人工审核模式直至定位原因。

增长指标可以慢慢爬，合规指标只有一条红线：**不实违规次数 = 0**。
本模块把这条红线做成一个可持久化的熔断开关：

  - 任何一次「已发布的不实内容级违规」被登记后，`is_suspended()` 立即为真，
    自动发布关闭，直到人工定位原因并显式解除。
  - 提供一道「疑似系统性失真」的自动熔断：同一批次内一致性校验被打回次数
    超过阈值，说明事实层可能整体失真，主动熔断并告警（宁停勿错）。

状态文件 data/compliance_state.json 属运行期资产，已 gitignore。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_PATH = PROJECT_ROOT / "data" / "compliance_state.json"

CST = ZoneInfo("Asia/Shanghai")

# 不实内容级违规（计划第十章合规指标）：出现一次即熔断
FALSE_CONTENT = "不实内容级违规"

# 自动熔断阈值：同批次一致性校验打回次数（计划第九章「一致性校验」降级之外的红线）
AUTO_SUSPEND_REJECT_THRESHOLD = 5


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _empty() -> dict:
    return {"suspended": False, "reason": "", "since": "", "cleared_at": "",
            "history": []}


def _load(path: Path | None = None) -> dict:
    p = Path(path) if path else STATE_PATH
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data.setdefault("suspended", False)
            data.setdefault("history", [])
            return data
        except Exception:
            pass
    return _empty()


def _save(data: dict, path: Path | None = None) -> None:
    p = Path(path) if path else STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------
# 熔断判定
# ------------------------------------------------------------
def is_suspended(path: Path | None = None) -> bool:
    """自动发布是否处于熔断（暂停）状态。"""
    return bool(_load(path).get("suspended"))


def status(path: Path | None = None) -> dict:
    """返回当前合规状态摘要（供 orchestrator / 看板 / CI 读取）。"""
    data = _load(path)
    false_count = sum(1 for h in data.get("history", []) if h.get("kind") == FALSE_CONTENT)
    return {
        "suspended": bool(data.get("suspended")),
        "reason": data.get("reason", ""),
        "since": data.get("since", ""),
        "false_content_count": false_count,
        "total_violations": len(data.get("history", [])),
    }


def violations(kind: str | None = None, path: Path | None = None) -> list[dict]:
    hist = _load(path).get("history", [])
    return [h for h in hist if (kind is None or h.get("kind") == kind)]


# ------------------------------------------------------------
# 登记 / 解除
# ------------------------------------------------------------
def record_violation(kind: str, detail: str, *, date: str = "", evidence: str = "",
                     operator: str = "", path: Path | None = None) -> dict:
    """登记一次违规并熔断。返回写入的违规记录。

    kind 默认视为「不实内容级违规」——只要发生即暂停自动发布（计划第十章）。
    """
    data = _load(path)
    rec = {
        "at": _now(),
        "kind": kind or FALSE_CONTENT,
        "detail": detail,
        "date": date,
        "evidence": evidence,
        "operator": operator,
    }
    data["history"].append(rec)
    data["suspended"] = True
    data["reason"] = f"{rec['kind']}：{detail}"
    data["since"] = rec["at"]
    _save(data, path)
    print(f"   ⛔ 合规熔断已触发：{rec['kind']} — {detail}（自动发布暂停，待人工定位）")
    return rec


def auto_suspend_if_systemic(reject_count: int, *, detail: str = "",
                             path: Path | None = None) -> bool:
    """疑似系统性失真时的自动熔断：一致性校验同批次打回超阈值。

    返回是否触发了熔断。已处于熔断状态时不重复登记。
    """
    if reject_count < AUTO_SUSPEND_REJECT_THRESHOLD:
        return False
    data = _load(path)
    if data.get("suspended"):
        return False
    record_violation("疑似系统性失真",
                     detail or f"同批次一致性校验打回 {reject_count} 次，疑事实层整体失真",
                     path=path)
    return True


def clear(operator: str = "", note: str = "", *, path: Path | None = None) -> dict:
    """人工定位原因后解除熔断。返回更新后的状态摘要。"""
    data = _load(path)
    data["suspended"] = False
    data["reason"] = ""
    data["cleared_at"] = _now()
    data.setdefault("history", []).append({
        "at": _now(), "kind": "解除熔断",
        "detail": note or "人工定位原因后恢复自动发布",
        "operator": operator, "date": "",
    })
    _save(data, path)
    print("   ✅ 合规熔断已解除，自动发布恢复")
    return status(path)


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------
def _cli() -> None:
    ap = argparse.ArgumentParser(description="合规红线熔断（计划第十章）")
    ap.add_argument("--status", action="store_true", help="查看合规状态")
    ap.add_argument("--violation", metavar="KIND", help="登记一次违规并熔断")
    ap.add_argument("--detail", default="", help="违规详情")
    ap.add_argument("--date", default="", help="关联日期 YYYY-MM-DD")
    ap.add_argument("--evidence", default="", help="证据链接/说明")
    ap.add_argument("--clear", action="store_true", help="人工定位后解除熔断")
    ap.add_argument("--operator", default="", help="操作人")
    ap.add_argument("--note", default="", help="解除说明")
    ap.add_argument("--exit-code-if-suspended", action="store_true",
                    help="处于熔断时以退出码 2 结束（供 CI 判断）")
    args = ap.parse_args()

    if args.violation:
        record_violation(args.violation, args.detail, date=args.date,
                         evidence=args.evidence, operator=args.operator)
        return
    if args.clear:
        clear(args.operator, args.note)
        return

    st = status()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    if args.exit_code_if_suspended and st["suspended"]:
        raise SystemExit(2)


if __name__ == "__main__":
    _cli()
