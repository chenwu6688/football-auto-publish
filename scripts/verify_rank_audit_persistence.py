#!/usr/bin/env python3
"""L3 打分留证验收（计划 12.4 · 量表校准的前提）。

## 防的是什么

`data/rank_audit/` 里那份 2026-10-10 的「量表校准」，10 条样本**全部 `来源=rule`**，
维度均值 冲突度 1.1 / 人物知名度 1.0。这组数字被当成「模型偏好」在讨论，
其实是**规则公式在这两维恒给 1**。两件事被混成一件。

根因有两条，都在本脚本的硬项里：

1. **跨批串号**：L4 缓存键是 `topic-{i}`（批内序号）。同一天第二批的 topic-0
   会命中第一批 topic-0 的 24h 缓存 → 直接复用**别的选题**的分。而且「命中缓存
   不花 token」，日志上完全看不出来。与 perf_key 用 index 串号是同一个教训。
2. **分数不留证**：缓存 `date` 一变就 `return {...judged: {}}`，L3 分数每天被
   覆盖丢弃。于是周审计永远读不到模型样本，只能退回规则分——**留了又扔**。

## 硬项（不达标退出码 1）

A. 两批不同选题、都用 `topic-0` 作为批内序号 → 必须各打一次分（不得共用缓存）
B. 同一选题 24h 内重复出现 → 必须命中缓存（不得重复烧 token）
C. 留证台账**追加**不覆盖，且每条含 主体 / 标题 / 维度 / 总分
D. `weekly_rank_audit` 必须优先读台账，`来源` 标 `model`、数据源标 `l3_ledger`
E. **负向验证**：把一条记录的某个维度删掉，`coverage_ok` 必须报缺维

用法：
  python3 scripts/verify_rank_audit_persistence.py [--quiet]
产出：data/audit/rank_audit_persistence_<date>.md + rank_audit_persistence_latest.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ranker  # noqa: E402

CST = timezone(timedelta(hours=8))
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"


def _load_audit_mod():
    spec = importlib.util.spec_from_file_location(
        "weekly_rank_audit", str(PROJECT_ROOT / "scripts" / "weekly_rank_audit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _today() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


class Ctx:
    """临时把 ranker / 审计模块的所有落盘路径指到 tempfile（台账隔离纪律）。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.audit = None

    def __enter__(self):
        self._cache = ranker.CACHE_PATH
        self._aw = ranker._awareness_cache
        ranker.CACHE_PATH = self.tmp / "ranker_cache.json"
        ranker._awareness_cache = {}
        self.audit = _load_audit_mod()
        self.audit.OUTPUT_DIR = self.tmp / "output"
        self.audit.CACHE_PATH = self.tmp / "ranker_cache.json"
        self.audit.AUDIT_DIR = self.tmp / "rank_audit"
        self.audit.CALIB_PATH = self.audit.AUDIT_DIR / "calibration.json"
        return self

    def __exit__(self, *a):
        ranker.CACHE_PATH = self._cache
        ranker._awareness_cache = self._aw
        return False


def _cands(subject: str, seq: str = "topic-0"):
    return [{"id": seq,
             "card": {"id": seq, "主体": subject, "动作": "热点球评", "发生时间": _today()},
             "板块": "热点球评", "title": f"{subject}的标题"}]


def _fake_llm(calls: list, dims: int = 4):
    def call(messages):
        calls.append(1)
        return {"scores": [{"id": "topic-0", "冲突度": dims, "人物知名度": dims,
                            "时效性": dims, "话题延展性": dims, "粉丝画像匹配度": dims}]}, "m"
    return call


def check_no_cross_batch_reuse() -> dict:
    """硬项 A：同名批内序号 + 不同选题，不得共用缓存。"""
    details, ok = [], True
    with Ctx():
        calls = []
        with mock.patch.object(ranker, "awareness_of", return_value="A"):
            ranker.rank_candidates(_cands("曼城"), use_llm=True,
                                   call_llm=_fake_llm(calls), now=datetime.now(CST))
            ranker.rank_candidates(_cands("皇马"), use_llm=True,
                                   call_llm=_fake_llm(calls), now=datetime.now(CST))
    if len(calls) == 2:
        details.append("两批不同选题（都叫 topic-0）→ 各打一次分 ✅")
    else:
        ok = False
        details.append(f"两批不同选题只打了 {len(calls)} 次分 —— 第二批吃到了第一批的缓存 ❌")
    return {"item": "跨批不同选题不得共用缓存（topic-N 不是身份）", "ok": ok,
            "advisory": False, "detail": details}


def check_same_topic_hits_cache() -> dict:
    """硬项 B：同一选题 24h 内必须命中缓存（否则白烧 token）。"""
    details, ok = [], True
    with Ctx():
        calls = []
        with mock.patch.object(ranker, "awareness_of", return_value="A"):
            ranker.rank_candidates(_cands("曼城"), use_llm=True,
                                   call_llm=_fake_llm(calls), now=datetime.now(CST))
            ranker.rank_candidates(_cands("曼城"), use_llm=True,
                                   call_llm=_fake_llm(calls), now=datetime.now(CST))
    if len(calls) == 1:
        details.append("同一选题重复出现 → 命中缓存，只打一次分 ✅")
    else:
        ok = False
        details.append(f"同一选题打了 {len(calls)} 次分 —— L4 缓存失效，token 白烧 ❌")
    return {"item": "同一选题 24h 内命中缓存", "ok": ok, "advisory": False, "detail": details}


def check_ledger_append() -> dict:
    """硬项 C：留证必须追加不覆盖，且字段够用。"""
    details, ok = [], True
    with Ctx():
        with mock.patch.object(ranker, "awareness_of", return_value="A"):
            for s in ("曼城", "皇马", "拜仁"):
                ranker.rank_candidates(_cands(s), use_llm=True,
                                       call_llm=_fake_llm([]), now=datetime.now(CST))
        rows = ranker.load_l3_ledger(7)
        if len(rows) == 3:
            details.append("三次打分 → 台账 3 行（追加不覆盖）✅")
        else:
            ok = False
            details.append(f"三次打分只有 {len(rows)} 行 —— 被覆盖了 ❌")
        need = ("at", "key", "主体", "标题", "维度", "总分")
        miss = [k for k in need if not rows or k not in rows[0]]
        if miss:
            ok = False
            details.append(f"台账缺字段：{miss} ❌")
        else:
            details.append(f"字段齐备：{'/'.join(need)} ✅")
        if rows and len({r["key"] for r in rows}) != len(rows):
            ok = False
            details.append("不同选题共用了同一个 key ❌")
        else:
            details.append(f"{len(rows)} 个选题 → {len({r['key'] for r in rows})} 个不同 key ✅")
    return {"item": "L3 留证追加写入且字段齐备", "ok": ok, "advisory": False, "detail": details}


def check_weekly_audit_reads_ledger() -> dict:
    """硬项 D：周审计优先读台账，来源标 model。"""
    details, ok = [], True
    with Ctx() as ctx:
        with mock.patch.object(ranker, "awareness_of", return_value="A"):
            ranker.rank_candidates(_cands("曼城"), use_llm=True,
                                   call_llm=_fake_llm([]), now=datetime.now(CST))
        recs, src = ctx.audit.collect_records(7)
        if src == "l3_ledger":
            details.append("数据源 = l3_ledger ✅")
        else:
            ok = False
            details.append(f"数据源 = {src}，期望 l3_ledger（说明仍在退回规则/缓存）❌")
        if recs and all(r.get("来源") == "model" for r in recs):
            details.append("来源标 model ✅")
        else:
            ok = False
            details.append(f"来源标记异常：{[r.get('来源') for r in recs]} ❌")
        if recs and all(isinstance(r.get("总分"), int) for r in recs):
            details.append("总分为整数，可参与排序与漂移计算 ✅")
        else:
            ok = False
            details.append("总分非整数，抽样排序会崩 ❌")
    return {"item": "周审计优先读台账并标 model", "ok": ok, "advisory": False, "detail": details}


def check_negative() -> dict:
    """硬项 E：删掉一个维度必须报缺维。"""
    details, ok = [], True
    with Ctx() as ctx:
        rows = [{"id": "k1", "来源": "model", "总分": 15,
                 "维度": {d: 3 for d in ranker.DIMENSIONS}}]
        cov, missing = ctx.audit.coverage_ok(rows)
        if cov and not missing:
            details.append("五维齐全 → 判覆盖 OK ✅")
        else:
            ok = False
            details.append(f"五维齐全却报缺维：{missing} ❌")
        broken = json.loads(json.dumps(rows))
        del broken[0]["维度"]["冲突度"]
        cov, missing = ctx.audit.coverage_ok(broken)
        if cov:
            ok = False
            details.append("删掉冲突度后仍判覆盖 OK —— 缺维检测失效 ❌")
        else:
            details.append(f"删掉冲突度 → 报缺维：{missing} ✅")
    return {"item": "负向验证：缺维必须被检出", "ok": ok, "advisory": False, "detail": details}


def check_real_ledger() -> dict:
    """告警项：真实台账有无数据（决定 11-16 复盘能不能算模型量表）。"""
    p = ranker.ledger_path()
    if not p.exists():
        return {"item": "真实 L3 台账（告警项）", "ok": True, "advisory": True,
                "detail": [f"{p.relative_to(PROJECT_ROOT)} 尚不存在——"
                           f"下一批生成后才会写入；在此之前的周审计仍只能退回规则分"]}
    rows = ranker.load_l3_ledger(7)
    detail = [f"文件 {p.relative_to(PROJECT_ROOT)}｜近 7 天 {len(rows)} 条"]
    if rows:
        detail.append(f"最近一条：{rows[-1].get('at', '')}｜{rows[-1].get('主体', '')}")
    else:
        detail.append("近 7 天 0 条 —— 模型可能一直未启用或持续降级，需查 L4 token 预算/模型可用性")
    return {"item": "真实 L3 台账（告警项）", "ok": bool(rows), "advisory": True, "detail": detail}


def verify() -> dict:
    hard = [check_no_cross_batch_reuse(), check_same_topic_hits_cache(),
            check_ledger_append(), check_weekly_audit_reads_ledger(), check_negative()]
    advisory = [check_real_ledger()]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": all(c["ok"] for c in hard), "checks": hard + advisory,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# L3 打分留证验收报告", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        tag = "（告警项）" if c.get("advisory") else "（硬项）"
        L.append(f"## {c['item']}{tag}：{'✅' if c['ok'] else '❌'}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="L3 打分留证验收")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"rank_audit_persistence_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "rank_audit_persistence_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
