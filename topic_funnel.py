#!/usr/bin/env python3
"""选题漏斗台账（2026-11-04，S2 配额归并的前置度量）。

## 它回答什么问题

全仓有**四套配额同时在回答「今天写什么类型」**：

    supply_mix      三层供给 30/50/20（一级确定性 / 二级突发 / 三级软性）
    content_mix     七类月度配比（热点球评 45 / 数据对比 30 / 排行榜 18 / …）
    batch_mix       每批 2 篇的结构（main + data 两槽）
    topic_category  类别优先级 + 战报比分单批软上限（R11）

这四套能同时生效，是因为它们分别作用在漏斗的不同位置上；但**没有任何一条记录
能回答「这一篇是被哪一层挡掉的」**。于是「为什么今天只发了 1 篇」只能靠人肉读日志，
S2（把四套配额收成一套）也就无从下手 —— 凭直觉删闸门的代价，10-25 那次清 22 项
僵尸测试已经吃过一遍：删完才发现有几项其实还在拦。

本模块把漏斗显式化：**一个批次一条记录，记录同一批候选在每一层之后还剩多少**。

    llm ──▸ dedup ──▸ material ──▸ category ──▸ batch_mix ──▸ slot ──▸ generated ──▸ final

每一层的损失 = 上一层剩余 − 本层剩余，**只按相邻两层算，不跨层累加、不重算**。
损失必须能归因到具体某一层，报告里不允许出现「无主损失」。

## 三条硬口径（都是踩过坑的）

1. **未调用记 None，不记 0。** 某批没有确定性供给，`slot` 就没被调用 —— 记为
   `None`（缺失），不写 0。写 0 会被读成「这一层吃掉全部候选」，是 10-24
   「缺列当 0」事故的同款错误。
2. **数字变大也要记，叫 `gain` 不叫负损失。** 蓄水池补位会让 `final` 大于
   `generated`；把增量和损失混在一列里加总，会让「净损失」掩盖两层各自的动作。
3. **空台账 ≠ 零损失。** 埋点刚接上时台账是空的，报告必须说「未判定」，
   而不是打印「各层损失 0 篇」（那会被读成「漏斗很顺」）。

## 它不是闸门

本模块没有任何阈值、不做任何判定、不阻断任何流程，因此**不进 `pre-publish`**：
它只记录「多少候选活到了哪一步」。给它加阈值，它就变成第 43 项审核 ——
而这轮工作的主题恰恰是「审核太多、验证太少」。

## 命令行

    python3 topic_funnel.py --summary            # 近 7 天
    python3 topic_funnel.py --summary --days 30
    python3 topic_funnel.py --selftest
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LEDGER_PATH = PROJECT_ROOT / "data" / "audit" / "topic_funnel.jsonl"

KEEP_DAYS = 90
CST = timezone(timedelta(hours=8))

# 漏斗层级：(键, 中文名, 归属层)。**顺序即漏斗顺序**，损失按相邻两层相减，
# 调整顺序等于改口径 —— 改之前先看 report() 里「最大损失层」会不会跟着变。
STAGES = (
    ("llm", "LLM 候选", "题源"),
    ("dedup", "去重", "去重层"),
    ("material", "素材充分性", "事实层"),
    ("category", "类别软上限", "topic_category"),
    ("batch_mix", "批次结构", "batch_mix"),
    ("slot", "可用槽位", "supply_mix/确定性"),
    ("generated", "主稿成稿", "生成层"),
    ("final", "本批成稿", "蓄水池/突发"),
)
_KEYS = tuple(k for k, _n, _o in STAGES)
_LABEL = {k: (n, o) for k, n, o in STAGES}

_LOCK = threading.Lock()
_CUR: dict | None = None

# 最近一次落盘结果。台账写入失败**不抛异常**（绝不阻断发布），但必须有地方能看见 ——
# 「只 try/except 不吭声」等于台账坏了半年没人知道（11-04 之前的缺图只 print 不落台账，
# 无封面率 37% 就这么黑了很久）。
LAST = {"ok": None, "path": str(LEDGER_PATH), "reason": ""}


def _now():
    return datetime.now(CST)


# ---------------------------------------------------------------- 记录


def begin(date_str, *, batch: str = "", max_articles: int = 0, quota: str = "") -> None:
    """开一批的漏斗。重复调用会丢弃上一条未落盘的记录（同进程一批只该有一个）。"""
    global _CUR
    with _LOCK:
        _CUR = {
            "date": str(date_str or ""),
            "batch": str(batch or ""),
            "max_articles": _as_int(max_articles),
            "quota": str(quota or ""),
            "at": _now().isoformat(timespec="seconds"),
            "stages": {},
        }


def _as_int(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def stage(key: str, remaining, *, detail: dict | None = None) -> bool:
    """记录「走完这一层之后还剩多少候选」。返回是否真的记下了。

    返回 False 表示**当前没有打开的批次**（后台任务/单测直调），这是正常情况；
    层级键写错则抛 ValueError —— 静默忽略拼错的层级名正是「假接线」的温床
    （11-04 `_img_topic` 取错嵌套层：函数在、调用在、单测绿、数据恒空）。
    """
    if key not in _KEYS:
        raise ValueError(f"未知漏斗层级 {key!r}（合法：{'、'.join(_KEYS)}）")
    try:
        n = int(remaining)
    except (TypeError, ValueError):
        raise ValueError(f"层级 {key} 的剩余数必须是整数，收到 {remaining!r}")
    if n < 0:
        raise ValueError(f"层级 {key} 的剩余数不能为负，收到 {n}")
    entry: dict = {"n": n}
    if detail:
        entry["detail"] = {str(k): v for k, v in detail.items()}
    with _LOCK:
        if _CUR is None:
            return False
        _CUR["stages"][key] = entry
        return True


def _diff(stages: dict) -> tuple[dict, dict]:
    """相邻两层的差 → (loss, gain)。只比**相邻两个都已记录**的层，中间缺层就跳过。"""
    loss: dict = {}
    gain: dict = {}
    prev = None
    for k in _KEYS:
        cur = stages.get(k)
        if not cur:
            continue
        n = cur["n"]
        if prev is not None:
            d = prev - n
            if d > 0:
                loss[k] = d
            elif d < 0:
                gain[k] = -d
        prev = n
    return loss, gain


def flush(status: str = "ok") -> str | None:
    """落盘并清空。返回日期（无打开批次时返回 None，可安全重复调用）。"""
    global _CUR
    with _LOCK:
        cur, _CUR = _CUR, None
    if not cur:
        return None
    stages = cur.get("stages") or {}
    loss, gain = _diff(stages)
    rec = {
        "date": cur.get("date", ""),
        "batch": cur.get("batch", ""),
        "at": cur.get("at", ""),
        "status": str(status or "ok"),
        "max_articles": cur.get("max_articles", 0),
        "quota": cur.get("quota", ""),
        "stages": stages,
        "loss": loss,
        "gain": gain,
        # None = 这一层没被记录（不是 0 篇）
        "articles": (stages.get("final") or {}).get("n"),
    }
    _append(rec)
    return rec["date"]


def _append(rec: dict) -> None:
    LAST["path"] = str(LEDGER_PATH)
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        LAST["ok"] = True
        LAST["reason"] = ""
        _prune()
    except Exception as e:
        # 台账写失败绝不阻断发布（与 freshness_ledger / scraper_diagnostics 同纪律），
        # 但要留下原因供调用方打印 —— 静默失败是台账类模块最危险的失效方式。
        LAST["ok"] = False
        LAST["reason"] = f"{type(e).__name__}: {str(e)[:80]}"


def _prune(days: int = KEEP_DAYS) -> None:
    try:
        if LEDGER_PATH.stat().st_size < 2 * 1024 * 1024:
            return
        floor = (_now() - timedelta(days=days)).strftime("%Y-%m-%d")
        keep = []
        for ln in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
            try:
                if (json.loads(ln).get("date") or "") >= floor:
                    keep.append(ln)
            except Exception:
                continue
        LEDGER_PATH.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 读取


def load(days: int | None = None) -> list:
    """读台账（可带「近 N 天」筛选，含今天）。文件不存在 → 空列表，不是异常。"""
    out: list = []
    try:
        text = LEDGER_PATH.read_text(encoding="utf-8")
    except Exception:
        return out
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if isinstance(r, dict):
            out.append(r)
    if days:
        floor = (_now() - timedelta(days=max(1, int(days)) - 1)).strftime("%Y-%m-%d")
        out = [r for r in out if (r.get("date") or "") >= floor]
    return out


def summarize(days: int = 7) -> dict:
    """近 N 天汇总。`decided=False` 表示台账还空着 —— 那是「未判定」，不是「零损失」。"""
    recs = load(days)
    requested = sum(_as_int(r.get("max_articles")) for r in recs)
    got = sum(_as_int(r.get("articles")) for r in recs if r.get("articles") is not None)
    loss: dict = {}
    gain: dict = {}
    for r in recs:
        for k, v in (r.get("loss") or {}).items():
            loss[k] = loss.get(k, 0) + _as_int(v)
        for k, v in (r.get("gain") or {}).items():
            gain[k] = gain.get(k, 0) + _as_int(v)
    total_loss = sum(loss.values())
    top = max(loss.items(), key=lambda kv: kv[1])[0] if loss else None
    return {
        "days": int(days),
        "records": len(recs),
        "decided": bool(recs),
        "requested": requested,
        "articles": got,
        "fill_rate": (got / requested) if requested else None,
        "loss": loss,
        "gain": gain,
        "total_loss": total_loss,
        "top_stage": top,
        "top_share": (loss[top] / total_loss) if top and total_loss else None,
    }


def _disp_w(s: str) -> int:
    """显示宽度（中文算 2 列）——否则中文标签会把报告排成锯齿状。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s))


def _pad(s: str, width: int) -> str:
    return str(s) + " " * max(0, width - _disp_w(s))


def report(days: int = 7) -> str:
    s = summarize(days)
    if not s["decided"]:
        return (f"选题漏斗（近 {s['days']} 天）：台账为空 —— **未判定**，"
                f"等第一批真实运行落盘后再看（空台账不等于零损失）")
    lines = [f"选题漏斗（近 {s['days']} 天，{s['records']} 批）"]
    if s["fill_rate"] is not None:
        gap = s["requested"] - s["articles"]
        lines.append(f"  配额请求 {s['requested']} 篇 → 成稿 {s['articles']} 篇"
                     f"（缺 {gap} 篇，达成率 {s['fill_rate']:.0%}）")
    # 逐层：批均剩余（只统计记录过的批）
    recs = load(days)
    for key in _KEYS:
        label, owner = _LABEL[key]
        ns = [r["stages"][key]["n"] for r in recs
              if isinstance(r.get("stages"), dict) and (r["stages"].get(key) or {}).get("n") is not None]
        if not ns:
            continue
        avg = sum(ns) / len(ns)
        lost = s["loss"].get(key, 0)
        gained = s["gain"].get(key, 0)
        tail = []
        if lost:
            tail.append(f"-{lost}")
        if gained:
            tail.append(f"+{gained}（补位）")
        lines.append(f"  {_pad(label, 12)}批均 {avg:>4.1f} 篇   "
                     f"{_pad(' '.join(tail) or '—', 14)}〔{owner}〕")
    if s["top_stage"]:
        label, owner = _LABEL[s["top_stage"]]
        lines.append(f"  ⇒ 最大损失层：{label}（{owner}）"
                     f"，吃掉 {s['loss'][s['top_stage']]} 篇，占全部损失的 {s['top_share']:.0%}")
        lines.append("  注：这是观测值，不构成「该删哪一层」的结论 —— "
                     "合并配额前要同时看该层的拦截是否都是真拦截。")
    else:
        lines.append("  ⇒ 各层均无损失（所有候选都活到了成稿）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 自检


def selftest() -> list[tuple[str, bool]]:
    """冒烟自检（用临时台账，不碰真实账本）。"""
    import tempfile

    global LEDGER_PATH, _CUR
    old_path, old_cur = LEDGER_PATH, _CUR
    cases: list[tuple[str, bool]] = []
    try:
        with tempfile.TemporaryDirectory() as td:
            LEDGER_PATH = Path(td) / "funnel.jsonl"

            # 1) 正常一批：8 → 6 → 6 → 5 → 2 → 2 → 2 → 2
            begin("2026-11-04", batch="晨读", max_articles=2, quota="常态 2 篇")
            stage("llm", 8)
            stage("dedup", 6, detail={"跨天": 2})
            stage("material", 6)
            stage("category", 5, detail={"战报比分软上限": 1})
            stage("batch_mix", 2)
            stage("slot", 2)
            stage("generated", 2)
            stage("final", 2)
            flush()
            recs = load()
            cases.append(("落盘 1 条记录", len(recs) == 1))
            r = recs[0] if recs else {}
            cases.append(("损失按相邻层算", r.get("loss") == {"dedup": 2, "category": 1, "batch_mix": 3}))
            cases.append(("成稿数取自 final", r.get("articles") == 2))

            # 2) 缺层不许当 0：只记 llm 与 final
            begin("2026-11-04", batch="午间", max_articles=2)
            stage("llm", 3)
            stage("final", 1)
            flush()
            recs = load()
            last = recs[-1] if recs else {}
            cases.append(("缺层跳过、不按 0 算损失", last.get("loss") == {"final": 2}))

            # 3) 补位使数字变大 → 记 gain，不记负损失
            begin("2026-11-04", batch="晚间", max_articles=2)
            stage("generated", 1)
            stage("final", 2)
            flush()
            last = load()[-1]
            cases.append(("变大记 gain 不记负损失", last.get("loss") == {} and last.get("gain") == {"final": 1}))

            # 4) 没开批次时 stage 返回 False（后台/单测直调不炸）
            _CUR = None
            cases.append(("无打开批次时 stage 返回 False", stage("llm", 1) is False))

            # 5) 层级名拼错必须报错（静默忽略=假接线温床）
            begin("2026-11-04")
            try:
                stage("dedupn", 1)
                cases.append(("拼错层级名必须抛错", False))
            except ValueError:
                cases.append(("拼错层级名必须抛错", True))

            # 6) 负数/非整数必须报错
            try:
                stage("llm", -1)
                cases.append(("负数必须抛错", False))
            except ValueError:
                cases.append(("负数必须抛错", True))
            try:
                stage("llm", "三")
                cases.append(("非整数必须抛错", False))
            except ValueError:
                cases.append(("非整数必须抛错", True))
            # 上面这批只做了报错自检，丢弃不落盘（否则污染下面的批数统计）
            _CUR = None

            # 7) 空台账 → 未判定（不是零损失）
            LEDGER_PATH = Path(td) / "empty.jsonl"
            cases.append(("空台账判未判定", summarize(7)["decided"] is False))
            cases.append(("空台账报告说「未判定」", "未判定" in report(7)))

            # 8) 汇总：达成率与最大损失层
            LEDGER_PATH = Path(td) / "funnel.jsonl"
            s = summarize(7)
            cases.append(("汇总批数 3", s["records"] == 3))
            cases.append(("最大损失层=dedup/batch_mix 之一",
                          s["top_stage"] in ("dedup", "batch_mix", "final")))
            cases.append(("请求合计 6 篇", s["requested"] == 6))
            cases.append(("成稿合计 5 篇", s["articles"] == 5))

            # 9) flush 幂等：无打开批次返回 None，不重复写
            before = len(load())
            cases.append(("重复 flush 不重复写", flush() is None and len(load()) == before))

            # 10) 台账文件损坏不抛（只跳过坏行）
            LEDGER_PATH.write_text('{"date":"2026-11-04","stages":{},"loss":{}}\n坏行\n',
                                   encoding="utf-8")
            cases.append(("坏行跳过不抛", len(load()) == 1))
    finally:
        LEDGER_PATH, _CUR = old_path, old_cur
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="选题漏斗台账（S2 配额归并前置度量）")
    ap.add_argument("--summary", action="store_true", help="打印近 N 天漏斗报告")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true", help="输出汇总 JSON")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        bad = 0
        for name, ok in selftest():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            bad += 0 if ok else 1
        print(f"\n自检 {len(selftest())} 项，失败 {bad} 项")
        return 1 if bad else 0
    if args.json:
        print(json.dumps(summarize(args.days), ensure_ascii=False, indent=2))
        return 0
    print(report(args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
