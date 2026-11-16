#!/usr/bin/env python3
"""发布台账的「成稿 → 发布」漏斗口径（2026-11-16，改动点 6）。

## 为什么要有这个模块

台账此前只记「尝试发布的归宿」—— published / failed / skipped_prediction /
skipped_already_published。分母天然就是「尝试数」，于是三件事永远不在分母里：

  ① 出口合规剔除的篇数（``metadata.compliance_dropped``）—— 稿子成了，规则挡下，没发；
  ② 成稿了却连台账都没进的篇数 —— 早期单文件覆盖写（11-04 前三层写入还没上）、
     批次台账文件缺失，这批稿子在漏斗上凭空消失；
  ③ 标题闸门判不合格仍发布的篇数（``articles[].title_gate_blocked``）——
     注意这是**已发布**、带瑕疵留证，**不是**丢稿，绝不能算进「丢掉」的那一栏。

11-02~11-15 实测：旧口径 ``published/(published+failed)`` = 61/62 = **98.4%**，
按成稿口径 = 61/74 = **82.4%**。差的 17 个百分点不是「发布失败」，其中 8 篇是
「成稿在、台账没有」的取证缺口（全部发生在 11-05 之前），其余是合规剔除。

口径一旦分成两个，就必须**两个都报、不许合成一个数**：
「尝试过的成功率」和「成稿到发布的比例」回答的是不同问题，合并会让
取证缺口被当成发布失败（或反过来被藏起来）。

## 本模块的边界

只做两件事：从 metadata 读成稿侧计数、校验两条恒等式。
**不做判定、不写文件、不发布、不猜缺数据**（读不到返回 ``available=False`` 且不写 0）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# 台账记录的全部合法状态。新增状态码必须同时在这里登记，
# 否则恒等式会把它算成「未计数」——这正是这条判据存在的意义。
STATUS_KEYS = ("published", "failed", "skipped_prediction", "skipped_already_published")

# 口径生效日：此前的日级台账没有成稿侧字段，只能留证不能判红。
FUNNEL_SINCE = "2026-11-16"


def _default_output_base() -> Path:
    return Path(os.environ.get("OUTPUT_DIR", Path(__file__).resolve().parent / "output"))


def draft_snapshot(date_str: str, output_base=None) -> dict:
    """成稿侧计数（**当天累计**，不是本批）。

    ``metadata.json`` 是同日跨批次累加的（晨/午/晚写进同一份），所以这里的
    ``drafts`` 是「当天落盘成稿数」，与单批次台账的 ``records`` **不同口径**。
    调用方必须看 ``scope`` 字段，别拿本批 records 去减当天 drafts。

    读不到 / 解析失败一律 ``available=False`` + ``reason``，**不写 0**：
    「列根本没导出」和「真的是 0 篇」在漏斗上是两件完全不同的事。
    """
    base = Path(output_base) if output_base else _default_output_base()
    p = base / str(date_str) / "metadata.json"
    out = {"available": False, "reason": "", "scope": "day_cumulative",
           "path": str(p), "drafts": None, "dropped_compliance": None,
           "draft_total": None, "title_gate_blocked_published": None}
    if not p.exists():
        out["reason"] = f"metadata 不存在（{p}）"
        return out
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                          # 解析失败也是「读不到」，不是 0
        out["reason"] = f"metadata 解析失败: {e.__class__.__name__}: {e}"
        return out
    if not isinstance(m, dict):
        out["reason"] = "metadata 顶层不是对象"
        return out

    arts = [a for a in (m.get("articles") or []) if isinstance(a, dict)]
    # compliance_dropped 里非 dict 的条目不计：宁可少计也不要让脏数据把分母算歪。
    dropped = [d for d in (m.get("compliance_dropped") or []) if isinstance(d, dict)]
    gate = [a for a in arts if a.get("title_gate_blocked")]

    out.update({
        "available": True,
        "drafts": len(arts),
        "dropped_compliance": len(dropped),
        "draft_total": len(arts) + len(dropped),
        # 这一项**不是**丢稿：这些篇已经在 articles 里、已经发出去了，
        # 只是标题结构不合格留了证。单独一栏，避免被并进「剔除」重复计数。
        "title_gate_blocked_published": len(gate),
    })
    return out


def lost_upstream(drafts, n_records) -> int | None:
    """成稿落盘数 − 台账记录数 = 「成稿在、台账没有」的篇数。

    在「当天累计 drafts」与「当天累计 records」同口径时可算（日级聚合文件）；
    拿单批次 records 去减当天 drafts 会得出一个巨大的假缺口 —— 那不是缺口，
    是两把尺子混用。所以调用方有责任先确认 scope。

    返回**原始差值**，不为负时也不截断：为负说明台账记录比成稿还多
    （重复合并 / 混进别天记录），那本身就是证据，抹平就成了掩盖。
    """
    if not isinstance(drafts, int) or not isinstance(n_records, int):
        return None
    return drafts - n_records


def identity(counts: dict, n_records: int, drafts=None) -> dict:
    """两条恒等式。比阈值耐用的原因：它们不依赖任何主观标定。

    I1 记录分区：``n_records == published + failed + skipped_prediction +
       skipped_already_published``。破了 ⇒ 有 status 没人计数（新状态码写进
       records 却没加计数），或 counts 与 records 不同步。
    I2 成稿覆盖：``drafts >= n_records``。破了 ⇒ 台账记录比成稿还多
       （日级聚合重复合并、跨日混入、或 metadata 被后一批覆盖而台账没跟上）。

    ``drafts`` 传 None 时 I2 判 ``None``（未判定），不判通过。
    """
    n_records = int(n_records or 0)
    known = sum(int(counts.get(k) or 0) for k in STATUS_KEYS)
    unaccounted = n_records - known
    out = {"n_records": n_records, "known_status": known,
           "unaccounted_status": unaccounted,
           "records_identity_ok": unaccounted == 0,
           "cover_ok": None, "lost_upstream": None}
    if isinstance(drafts, int):
        out["lost_upstream"] = drafts - n_records
        out["cover_ok"] = drafts >= n_records
    return out


def describe(ident: dict) -> str:
    """一行说明，给日志用。"""
    parts = [f"记录 {ident['n_records']} = 已计数 {ident['known_status']}"]
    if ident.get("unaccounted_status"):
        parts.append(f"⚠️ 未计数状态 {ident['unaccounted_status']} 条")
    if ident.get("lost_upstream") is not None:
        parts.append(f"成稿未进台账 {ident['lost_upstream']} 篇")
    return "；".join(parts)
