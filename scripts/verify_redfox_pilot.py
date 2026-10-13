#!/usr/bin/env python3
"""RedFox 热点数据 Phase 1 只读试点验收。

背景（用户 2026-10-13 提供 Key 并授权试点）：RedFox（redfox.hk）按量付费的
新媒体数据 API，为选题择优（ranker L2「社媒讨论量」）提供抖音足球热榜作品的
真实互动量。本脚本验收红线纪律，防止试点跑偏：

  1. 只读试点：只调搜索端点，不写 RedFox 侧数据（由客户端代码结构保证）；
  2. 绝不假装有信号：无 Key / 网络失败 / 非 2000 / 超日上限 → 空，不造 0；
  3. 额度纪律：缓存 TTL 内不重复调用；日上限硬闸；台账落盘可查；
  4. 台账隔离：测试/验收路径全部指向 tempfile，真实台账计数不变；
  5. 信号只作输入：经 signal_sources 只读合并，不进正文。

硬项（不达标退出码 1）
  A. 配置结构——config/redfox.yaml 字段齐备、端点/关键词非空、数值合理
  B. 规则冒烟——redfox_client.selftest() 全通过（归一化/缓存/限额/降级）
  C. 台账隔离——mock 请求跑一遍搜索，真实 data/redfox_budget.json 计数不变
  D. 信号表接入——load_redfox 能读真实信号表（无文件=跳过不算失败）；
     load_external(redfox_path=不存在) 不炸；orchestrator 命名空间可见
     signal_sources（_rank_topics 链路）

告警项（只提示，不阻断）
  E. 实网探测——仅本地有 Key 时经缓存通道探测 1 个关键词（TTL 内 0 计费）；
     无 Key 自动跳过（CI 无 Secret 属正常）；有 Key 但调用失败只告警。

用法：
  python3 scripts/verify_redfox_pilot.py [--quiet]
产出：data/audit/redfox_pilot_<date>.md + redfox_pilot_latest.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import redfox_client as rc  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

DY_ITEM = {"workId": "7388", "title": "梅西倒钩太漂亮了", "workUrl": "https://v/1",
           "publishTime": "2026-10-13 10:00:00", "accountName": "足球哥",
           "followerCount": "6668", "likeCount": 100, "commentCount": 20,
           "shareCount": 5, "crawlTime": "2026-10-13 12:00:00"}


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def check_config() -> dict:
    raw = rc.load_config()
    if raw is None:
        return {"item": "配置结构（config/redfox.yaml）", "ok": False, "advisory": False,
                "detail": ["❌ 配置缺失或不可解析（客户端将整体降级，试点空转）"]}
    problems = []
    for key in ("base_url", "timeout", "endpoints", "sort", "per_keyword_limit",
                "keywords", "cache_ttl_hours", "daily_call_cap", "signals_path"):
        if raw.get(key) in (None, "", [], {}):
            problems.append(f"缺字段 {key}")
    eps = raw.get("endpoints") or {}
    if not isinstance(eps, dict) or not eps:
        problems.append("endpoints 为空")
    for name, ep in (eps or {}).items():
        if not str(ep).startswith("/"):
            problems.append(f"端点 {name} 路径应以 / 开头：{ep}")
    kws = [str(x) for x in (raw.get("keywords") or [])]
    if len(kws) < 3:
        problems.append(f"关键词过少（{len(kws)} 个），试点无统计意义")
    for key in ("cache_ttl_hours", "daily_call_cap", "timeout", "per_keyword_limit"):
        try:
            v = int(raw.get(key))
        except (TypeError, ValueError):
            problems.append(f"{key} 非整数：{raw.get(key)!r}")
            continue
        if v <= 0:
            problems.append(f"{key}={v} 应为正数")
    cap = raw.get("daily_call_cap")
    try:
        if int(cap) < len(kws):
            problems.append(f"daily_call_cap={cap} < 关键词数 {len(kws)}，一次完整刷新都跑不完")
    except (TypeError, ValueError):
        pass
    enabled = (raw.get("endpoint_enabled") or {})
    if not any(enabled.values()):
        problems.append("所有端点均关闭（endpoint_enabled 全 false）")
    return {"item": "配置结构（config/redfox.yaml）", "ok": not problems, "advisory": False,
            "version": raw.get("版本"), "keywords": len(kws),
            "detail": ([f"❌ {'；'.join(problems)}"] if problems
                       else [f"配置 v{raw.get('版本')} 字段齐备 ✅",
                             f"关键词 {len(kws)} 个；日上限 {cap}；TTL {raw.get('cache_ttl_hours')}h；"
                             f"启用端点 {[k for k, v in enabled.items() if v]} ✅"])}


def check_selftest() -> dict:
    cases = rc.selftest()
    bad = [n for n, ok in cases if not ok]
    return {"item": "规则冒烟（归一化 / 缓存 / 限额硬闸 / 降级路径）", "ok": not bad,
            "advisory": False, "total": len(cases),
            "detail": ([f"❌ 未通过：{bad}"] if bad else [f"{len(cases)} 项冒烟全部通过 ✅"])}


def check_ledger_isolation() -> dict:
    """mock 请求跑一遍搜索：必须只写 tempfile，真实台账/缓存内容不变。"""
    before = rc.calls_today(rc.BUDGET_PATH)
    cache_before = (rc.CACHE_PATH.read_text(encoding="utf-8")
                    if rc.CACHE_PATH.exists() else None)
    tmp = Path(tempfile.mkdtemp())
    cpath, bpath = tmp / "c.json", tmp / "b.json"

    def _fake(url, headers=None, json=None, timeout=None):
        return _Resp({"code": 2000, "data": {"list": [DY_ITEM]}})

    orig_key = os.environ.get(rc.ENV_KEY)   # 保存真实 Key，finally 原样还原
    os.environ[rc.ENV_KEY] = "verify_fake_key"   # （直接 pop 会把后面实网探测的 Key 弄丢）
    try:
        with mock.patch("requests.post", side_effect=_fake):
            cfg = rc.load_config() or {}
            recs = rc.search_works("隔离验证词", "douyin", cfg=cfg,
                                   cache_path=cpath, budget_path=bpath)
    finally:
        if orig_key is None:
            os.environ.pop(rc.ENV_KEY, None)
        else:
            os.environ[rc.ENV_KEY] = orig_key
    after = rc.calls_today(rc.BUDGET_PATH)
    cache_after = (rc.CACHE_PATH.read_text(encoding="utf-8")
                   if rc.CACHE_PATH.exists() else None)
    # 判据是「真实台账/缓存内容不变」（而非文件不存在——真实 refresh 会合法建它们）
    ok = (after == before and cache_after == cache_before
          and len(recs) == 1 and bpath.exists())
    detail = [f"真实台账 {before} → {after}（应不变）；真实缓存内容"
              f"{'不变' if cache_after == cache_before else '被改写 ❌'}；"
              f"临时台账已写 {bpath.exists()}；归一化返回 {len(recs)} 条"]
    return {"item": "台账隔离（验收路径全走 tempfile，真实台账零污染）",
            "ok": bool(ok), "advisory": False, "detail": detail}


def check_signals_wiring() -> dict:
    """信号表接入：load_redfox 可读真实表（无文件=跳过）；load_external 隔离不炸；
    orchestrator 命名空间可见 signal_sources。"""
    import signal_sources as ss
    tmp = Path(tempfile.mkdtemp()) / "nope.json"
    problems, notes = [], []

    isolated = ss.load_external(tmp, tmp)          # 两侧都指向不存在的临时路径
    if isolated != {}:
        problems.append(f"隔离输入下 load_external 应返回空表，实得 {isolated}")
    else:
        notes.append("load_external(隔离路径) 返回空表 ✅")

    real = ss.load_redfox()
    if real:
        bad = {k: v for k, v in real.items() if not isinstance(v.get("社媒讨论量"), int)}
        if bad:
            problems.append(f"真实信号表含非法记录：{list(bad)[:3]}")
        else:
            notes.append(f"真实信号表 {len(real)} 个命中词可读 ✅"
                         f"（Top：{max(real, key=lambda k: real[k]['社媒讨论量'])}）")
    else:
        notes.append("真实信号表暂无（未刷新或 CI 无文件），机制就绪，本项跳过")

    try:
        import orchestrator as o
        has = hasattr(o, "signal_sources") or "signal_sources" in dir(o)
        notes.append("orchestrator 命名空间可见 signal_sources ✅" if has
                     else "orchestrator 未直接持有 signal_sources 引用（_rank_topics 内 import，属预期）")
    except Exception as e:
        problems.append(f"import orchestrator 失败：{e}")

    return {"item": "信号表接入（只读合并 / 隔离不炸 / 编排链路）",
            "ok": not problems, "advisory": False,
            "skipped": not problems and not real, "detail": problems or notes}


def check_live_probe() -> dict:
    """实网探测（告警项）：有 Key 且缓存不新鲜 → 1 次真实调用；无 Key → 跳过。"""
    if not rc.api_key():
        return {"item": "实网探测（仅本地有 Key；CI 无 Secret 自动跳过）", "ok": True,
                "advisory": True, "skipped": True,
                "detail": ["未配置 REDFOX_API_KEY，跳过实网探测（不算失败）"]}
    cfg = rc.load_config()
    kws = [str(x) for x in ((cfg or {}).get("keywords") or [])]
    if not kws:
        return {"item": "实网探测", "ok": False, "advisory": True,
                "detail": ["❌ 配置无关键词，无法探测"]}
    kw = kws[0]
    try:
        recs = rc.search_works(kw, "douyin")
    except Exception as e:
        return {"item": f"实网探测（关键词「{kw}」）", "ok": False, "advisory": True,
                "detail": [f"❌ 探测异常：{e}"]}
    if not recs:
        return {"item": f"实网探测（关键词「{kw}」）", "ok": False, "advisory": True,
                "detail": ["❌ 调用未返回数据（Key 无效 / 积分不足 / 网络不通 / 端点未上线）"]}
    return {"item": f"实网探测（关键词「{kw}」）", "ok": True, "advisory": True,
            "detail": [f"返回 {len(recs)} 条，Top 互动 {recs[0]['engagement']:,}"
                       f"（{recs[0]['title'][:30]}）✅；今日累计 {rc.calls_today()} 次"]}


def verify() -> dict:
    hard = [check_config(), check_selftest(), check_ledger_isolation(), check_signals_wiring()]
    soft = [check_live_probe()]
    checks = hard + soft
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]],
            "advisory_failed": [c["item"] for c in soft if not c["ok"] and not c.get("skipped")]}


def render_md(rep: dict) -> str:
    L = ["# RedFox 热点数据 Phase 1 只读试点验收报告", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "告警项" if c.get("advisory") else ("硬项（跳过）" if c.get("skipped") else "硬项")
        L.append(f"## {c['item']}（{tag}）：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    if rep.get("advisory_failed"):
        L += [f"> 告警项待关注：{'、'.join(rep['advisory_failed'])}", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="RedFox Phase 1 只读试点验收")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"redfox_pilot_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "redfox_pilot_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
