#!/usr/bin/env python3
"""外部接口每日额度台账（计划 11.4「赛程源选型与额度管理」）。

计划 11.4 白纸黑字：
  「关键工程要求是赛程落库缓存：每日一次批量拉取未来 14 天赛程写库，业务侧只读库、
    不直连接口。免费额度按日计，缓存后每日调用量可控在 3 次以内。」

台账把这三句话落成可执行、可复盘的三条纪律：
  1. **按源按日计数**：以 CST 自然日切分，跨日自动归零；每次真实外部调用记一笔，
     落 data/api_budget.json，供复盘「今天到底打了几次接口」。
  2. **双闸门**：单源日额度 SOURCE_LIMITS + 全局日额度 daily_total_cap()（默认 3，
     即计划 11.4 的「3 次以内」）。任一超限即**拒绝**，返回 ok=False。
  3. **绝不阻塞**：拒绝只返回结果并告警，不抛异常、不静默放行、不假装成功。
     「额度用尽 → 降级」属计划第九章降级链，由调用方处理。

额度可覆盖（优先级 环境变量 > config/api_budget.yaml > 内置默认）：
  - 全局：API_DAILY_CAP=3
  - 单源：APIB_<源名大写、非字母数字转下划线>，如 APIB_API_FOOTBALL=4
  - 配置文件 config/api_budget.yaml：
        daily_total_cap: 3
        source_limits:
          football-data: 2
          juhe: 1
          api-football: 4

命令行：
  python3 api_budget.py                 # 打印今日额度使用情况
  python3 api_budget.py --date 2026-10-12
  python3 api_budget.py --reset         # 清空今日计数（运维用，慎用）
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LEDGER_PATH = PROJECT_ROOT / "data" / "api_budget.json"
CONFIG_PATH = PROJECT_ROOT / "config" / "api_budget.yaml"

CST = timezone(timedelta(hours=8))

# 计划 11.4：全局日调用量「可控在 3 次以内」
DEFAULT_DAILY_TOTAL_CAP = 3

# 单源日额度（默认分配，见 README「赛程落库与额度台账」）。
# 说明：football-data 免费层单次查询窗口硬上限 10 天，14 天窗口须拆成两笔请求；
# 补源联赛按日轮换（见 scripts/fixture_library.SUPPLEMENTS），故单源 1 次/日足够。
SOURCE_LIMITS = {
    "football-data": 2,
    "juhe": 1,
    "api-football": 1,
}

# 台账保留天数（超出自动裁剪，避免文件无界增长）
KEEP_DAYS = 30

_ENV_PREFIX = "APIB_"


def _today() -> str:
    return datetime.now(CST).date().isoformat()


def _norm_source(source: str) -> str:
    return (source or "").strip().lower()


def _env_key(source: str) -> str:
    return _ENV_PREFIX + re.sub(r"[^A-Za-z0-9]+", "_", source).upper()


def _load_config() -> dict:
    try:
        import yaml
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def source_limit(source: str) -> int:
    """单源当日额度：环境变量 > 配置文件 > 内置默认（未登记源默认 0，即禁止直连）。"""
    s = _norm_source(source)
    env = os.environ.get(_env_key(s))
    if env is not None and str(env).strip() != "":
        try:
            return max(0, int(env))
        except Exception:
            pass
    cfg = (_load_config().get("source_limits") or {})
    if s in cfg:
        try:
            return max(0, int(cfg[s]))
        except Exception:
            pass
    return int(SOURCE_LIMITS.get(s, 0))


def daily_total_cap() -> int:
    """全局当日额度（计划 11.4：默认 3）。"""
    env = os.environ.get("API_DAILY_CAP")
    if env is not None and str(env).strip() != "":
        try:
            return max(0, int(env))
        except Exception:
            pass
    cfg = _load_config()
    if cfg.get("daily_total_cap") is not None:
        try:
            return max(0, int(cfg["daily_total_cap"]))
        except Exception:
            pass
    return DEFAULT_DAILY_TOTAL_CAP


# ------------------------------------------------------------
# 账本读写
# ------------------------------------------------------------
def _load() -> dict:
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(json.dumps(_prune(data), ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:  # 落盘失败不得阻塞主流程
        print(f"⚠️ 额度台账写入失败（不影响主流程）：{str(e)[:80]}")


def _prune(data: dict) -> dict:
    """裁剪超过 KEEP_DAYS 的旧日期（字符串日期可安全按字典序比较）。"""
    cutoff = (datetime.now(CST).date() - timedelta(days=KEEP_DAYS)).isoformat()
    return {d: v for d, v in data.items() if isinstance(d, str) and d >= cutoff}


def usage(date: str | None = None) -> dict:
    """指定日（默认今日）各源已用次数。"""
    d = date or _today()
    day = (_load().get(d) or {})
    out: dict[str, int] = {}
    for k, v in day.items():
        try:
            out[k] = int(v)
        except Exception:
            continue
    return out


def total_used(date: str | None = None) -> int:
    return sum(usage(date).values())


def remaining(source: str | None = None, date: str | None = None) -> int:
    """剩余可用次数。给 source 时取「单源剩余」与「全局剩余」的较小值。"""
    d = date or _today()
    global_left = max(0, daily_total_cap() - total_used(d))
    if source is None:
        return global_left
    s = _norm_source(source)
    return max(0, min(source_limit(s) - usage(d).get(s, 0), global_left))


def record(source: str, n: int = 1, note: str = "", date: str | None = None) -> dict:
    """记一笔外部调用。允许则计数并返回 ok=True；任何闸门超限则拒绝且不计数。

    返回 dict：{ok, source, n, used, limit, total_used, total_cap, remaining, reason}
    """
    s = _norm_source(source)
    d = date or _today()
    n = max(1, int(n))
    lim = source_limit(s)
    cap = daily_total_cap()
    day_used = usage(d).get(s, 0)
    t_used = total_used(d)

    def _res(ok: bool, reason: str = "") -> dict:
        return {"ok": ok, "source": s, "n": n, "date": d,
                "used": day_used + (n if ok else 0), "limit": lim,
                "total_used": t_used + (n if ok else 0), "total_cap": cap,
                "remaining": max(0, min(lim - (day_used + (n if ok else 0)),
                                        cap - (t_used + (n if ok else 0)))),
                "reason": reason, "note": note}

    if lim <= 0:
        r = _res(False, f"源 {s} 未登记可用额度（当日上限 0）")
    elif day_used + n > lim:
        r = _res(False, f"源 {s} 当日额度已用 {day_used}/{lim}，本次需 {n} 次，超限")
    elif t_used + n > cap:
        r = _res(False, f"全局当日额度已用 {t_used}/{cap}（计划 11.4 上限），本次需 {n} 次，超限")
    else:
        r = _res(True)

    if r["ok"]:
        data = _load()
        day = dict(data.get(d) or {})
        day[s] = int(day.get(s, 0)) + n
        data[d] = day
        _save(_prune(data))
    return r


def reset(date: str | None = None) -> None:
    """清空指定日（默认今日）计数。运维/测试用。"""
    d = date or _today()
    data = _load()
    if d in data:
        data.pop(d, None)
        _save(data)


# ------------------------------------------------------------
# 汇总与呈现
# ------------------------------------------------------------
def summary(date: str | None = None) -> dict:
    d = date or _today()
    u = usage(d)
    cap = daily_total_cap()
    sources = sorted(set(u) | set(SOURCE_LIMITS))
    rows = [{"source": s, "used": u.get(s, 0), "limit": source_limit(s),
             "remaining": remaining(s, d)} for s in sources]
    t = sum(u.values())
    return {"date": d, "total_used": t, "total_cap": cap,
            "ok": t <= cap,
            "over": max(0, t - cap),
            "sources": rows,
            "history": {k: sum(v.values()) for k, v in sorted(_load().items())}}


def render(rep: dict | None = None) -> str:
    rep = rep or summary()
    L = [f"# 外部接口额度台账（{rep['date']}）", "",
         f"- 全局已用：**{rep['total_used']} / {rep['total_cap']}**"
         f"（计划 11.4 上限 {DEFAULT_DAILY_TOTAL_CAP} 次/日）"
         f"　{'✅ 未超限' if rep['ok'] else '❌ 超限 ' + str(rep['over']) + ' 次'}",
         "",
         "| 数据源 | 已用 | 当日额度 | 剩余 |", "| --- | --- | --- | --- |"]
    for r in rep["sources"]:
        L.append(f"| {r['source']} | {r['used']} | {r['limit']} | {r['remaining']} |")
    if rep.get("history"):
        L += ["", "## 近 {0} 天记录".format(len(rep["history"])), ""]
        for d, n in rep["history"].items():
            mark = "✅" if n <= rep["total_cap"] else "❌"
            L.append(f"- {d}：{n} 次 {mark}")
    return "\n".join(L)


def _cli() -> int:
    ap = argparse.ArgumentParser(description="外部接口每日额度台账（计划 11.4）")
    ap.add_argument("--date", default=None, help="查询日期 YYYY-MM-DD（默认今日）")
    ap.add_argument("--reset", action="store_true", help="清空指定日计数（运维用）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args()

    if args.reset:
        reset(args.date)
        print(f"🧹 已清空 {args.date or _today()} 的额度计数")
        return 0

    rep = summary(args.date)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else render(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
