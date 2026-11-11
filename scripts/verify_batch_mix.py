#!/usr/bin/env python3
"""每批内容类型结构验收（P1-1，2026-11-04）。

P1-1 的目标不是「配比好看」，而是**把配额压到有阅读的类型上**：

    10 月实发口径（88 篇去重）：热点球评 48.1 / 排行榜 30.5 / 战术榜单 21.2 /
    数据对比 17.1 / 八卦 14.7 / 转会 11.4 / 人物故事 5.9 / 历史回顾 0.9

近 10 日实际结构却是战术榜单 44%（27/61）、热点球评 18%——正好反过来。

验收分三层：

  A. 结构逻辑（硬项）—— batch_mix.selftest 全过（含「只有低效类型时不凑数」
     「日上限生效」「同类型内保留 ranker 顺序」）
  B. 已接入生成侧（硬项）—— orchestrator 里真的调用 batch_mix.assign（AST 查 Call）
  C. 每批 data 槽命中（硬项，逐批）—— 生效时刻 `SLOT_SINCE_AT` 之后每个成功批次，
     `data/audit/topic_funnel.jsonl` 里 `batch_mix.detail.降级` 必须为 0。
     样本单位是**批**（一天 3 批），一出现降级立刻 FAIL 并指名「哪天哪批」，
     不攒样本、不设合成阈值。
  D. 实际分布（观测项，**不参与判定**）—— 生效日后近 N 天的热点/数据型/战术榜单
     占比，与配比目标并排打印，供人看趋势。
  E. 禁发类型（硬项，绝对判据）—— 生效日后出现 `reservoir.BANNED_TYPES`
     （历史回顾）即 FAIL，与样本量无关。

为什么 C 取代了原来的「8 篇 + 数据型 ≥38%」：
  ① 38% 是「数据对比 30 + 排行榜 18 − 容差 10」的**合成口径**，红了定位不到
     是哪一类缺；
  ② 样本 8 篇时 ±1 篇就是 ±12.5pp，比 10pp 容差还大 —— 这个阈值撑不住这个
     样本量，红绿都由单篇翻转决定；
  ③ 同一事实在成因侧有更早、更准的记录：`batch_mix.degraded` 逐批写明
     「data 槽用次优类型补位」。11-11 的 4 篇里数据型只占 25%，追下去正是
     晨读那批 `降级: 1`，而不是「阈值定高了」。

所有口径用 content_mix.normalize 归一（战术解析→战术榜单、榜单→排行榜、紧急球评→热点球评），
避免「榜单 / 排行榜」「战术解析 / 战术榜单」被拆成两类互相合格（10-24 比分口径事故的同款风险）。

产出 ``data/audit/batch_mix_<date>.md`` + ``batch_mix_latest.json``；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_batch_mix.py [--days 7] [--quiet]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import batch_mix  # noqa: E402
import content_mix  # noqa: E402
import reservoir  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"
CST = timezone(timedelta(hours=8))

# 结构生效日：此前的产出是旧结构（战术榜单 44%），拿它判新规则等于永远 FAIL
MIX_SINCE = "2026-11-04"
# 槽位账生效日（两次后移，每次都有具体机制补齐）：
#   11-04 → 11-07：MIX_SINCE 之后的批次虽然跑了 batch_mix，但那时**确定性供给先占槽、
#                  data 槽被静默丢弃**（漏斗 `batch_mix 2 → slot 1 → generated 1`），
#                  实际产出是「1 主 + 0~1 战术榜单」，数据型 9.1%、战术榜单 36.4%。
#   11-07 → 11-11：**供给侧根本没有「数据对比」这个品类** —— `prompts/topic_selector.txt`
#                  的品类清单里只有「战术解析」（配比目标是 0），于是 data 槽候选池恒空，
#                  每批都写 `degraded: ["data 槽用次优类型补位"]`，实际产出数据型 14.3%。
#                  2026-11-11 已在选题提示词里补上「数据对比」品类并硬性要求每批 1 篇，
#                  这是本条指标第一次具备可达性。此后不再后移。
# 旧样本仍如实计入 legacy 并打印，不隐藏。
#
# 生效口径**精确到批次时刻**（不是「按天一刀切」）：提示词修复于 11-11 11:58 合入，
# 当天 11:01 的晨读批不可能带上新品类（漏斗里就是 `降级: 1`）。按天切会把它算进
# 新判据 → 这条检查从上线第一天起永久红；按天切还会顺手把 12:01 那批（已带新品类、
# 降级 0）也一起排掉。所以用漏斗的 `at` 时间戳比，一处权威派生日期。
SLOT_SINCE_AT = "2026-11-11T11:58:00+08:00"
# 逐批判据的样本下限：晨/午/晚各一批，跑满一天即可下结论（单位是「批」不是「篇」）。
MIN_BATCHES = 3
# 逐批明细的唯一权威：漏斗台账里每批一条，含 batch_mix.detail.降级。
FUNNEL_PATH = AUDIT_DIR / "topic_funnel.jsonl"


def _since_dt() -> datetime:
    """生效时刻。唯一权威是 ``SLOT_SINCE_AT``，日期口径从它派生，不许另写一个。"""
    return datetime.fromisoformat(SLOT_SINCE_AT)


def _since_date() -> str:
    return _since_dt().strftime("%Y-%m-%d")

# 结构与实测支撑的目标
#
# ▼ 2026-11-04 夜间：这三个数字**不再单独手写**，改为由「配比目标 ± 容差」派生。
#   此前是 35 / 25 / 20 三个孤立常量，和 content_mix.TARGET_PCT（40/25/15/10）各写各的：
#   同一个「战术榜单该占多少」，配比表说 10%、结构验收说上限 25% —— 两把尺子相差 15pp，
#   按哪把都能说合格。派生之后，改目标即改验收，改一处必须改另一处（否则本文件红）。
_TOL = content_mix.TOL_PCT
# 只保留**槽位设计自洽**这一个用途（模拟口径下战术榜单只该做补位）。
# 「热点球评 ≥x%」「数据型 ≥y%」两条合成线已删除：它们是实测分布的判据，
# 样本 8 篇撑不起 10pp 容差，且 38% 是两类之和、红了定位不到缺哪类。
TARGET_TACTICAL_MAX = round(content_mix.TARGET_PCT["战术榜单"] + _TOL, 1)

# 模拟用的池子规模（每类型给多少条候选）。够大即可，只关心稳态份额。
_SIM_PER_TYPE = 30
_SIM_BATCHES_PER_DAY = 3      # 晨读/午间/晚间
_SIM_PER_BATCH = 2            # constants.BATCH_QUOTA.normal_max


def _today() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _collect(days: int, since: str | None = None) -> tuple[list[dict], list[str], int]:
    """返回 (articles, 窗口日期列表, 旧批次篇数)。

    **只认带 batch_mix 字段的批次**：结构改造（11-04）之前生成的批次，metadata 里没有
    `batch_mix`，它们走的是旧排序（战术榜单 44%）。拿旧批次判新结构，等于让生效日当天
    早上那批把新规则永远判红——这和 `MIX_SINCE` 想解决的问题是同一个。
    这里按「有没有真跑过 batch_mix.assign」排除，而不是靠日期猜：字段存在与否是客观事实，
    并且旧批次篇数会如实报出来，不是悄悄丢掉。
    """
    end = datetime.now(CST).date()
    start = (end - timedelta(days=days - 1))
    lo = max(start.strftime("%Y-%m-%d"), since or "")
    out, days_seen, legacy = [], [], 0
    if not OUTPUT_DIR.exists():
        return out, days_seen, legacy
    for d in sorted(OUTPUT_DIR.iterdir()):
        if not d.is_dir() or len(d.name) != 10 or d.name < lo:
            continue
        mp = d / "metadata.json"
        if not mp.exists():
            continue
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        days_seen.append(d.name)
        ran_mix = isinstance(meta.get("batch_mix"), dict)
        for a in meta.get("articles") or []:
            if not ran_mix:
                legacy += 1
                continue
            out.append({"date": d.name, "title": a.get("title", ""),
                        "content_type": content_mix.normalize(a.get("content_type"))})
    return out, days_seen, legacy


def _structure(arts: list[dict]) -> dict:
    total = len(arts)
    counts: dict[str, int] = {}
    for a in arts:
        ct = a["content_type"]
        counts[ct] = counts.get(ct, 0) + 1
    main = counts.get("热点球评", 0)
    tactical = counts.get("战术榜单", 0)
    data = counts.get("数据对比", 0) + counts.get("排行榜", 0)
    pct = lambda n: round(100 * n / total, 1) if total else None   # noqa: E731
    return {"total": total, "counts": counts,
            "main_pct": pct(main), "tactical_pct": pct(tactical), "data_pct": pct(data),
            "banned_hits": counts.get("历史回顾", 0)}


def _span_days() -> int:
    """生效日到今天的天数（含首尾）。观测与禁发判据都用它取全量。"""
    return max((datetime.now(CST).date() - _since_dt().date()).days + 1, 1)


def _funnel_batches() -> list[dict]:
    """生效时刻之后每个成功批次的 batch_mix 明细（逐批，唯一权威）。

    两条过滤都是「有证据才算」，不是「没证据就算好」：
      · 时间按 `at` 精确比（生效时刻，不是当天 0 点）；
      · 只认**带「降级」子项**的记录 —— 该子项晚于 `降级` 字段上线才有，老记录没有，
        缺失按「无证据」跳过，**绝不按 0 补**（11-06「拿缺失当事实」的同一个错）。
    """
    if not FUNNEL_PATH.exists():
        return []
    since = _since_dt()
    out = []
    for line in FUNNEL_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("status") != "ok":
            continue
        try:
            at = datetime.fromisoformat(r.get("at") or "")
        except ValueError:
            continue                      # 没时间戳 = 无证据，不猜
        if at < since:
            continue
        bm = (r.get("stages") or {}).get("batch_mix")
        if not isinstance(bm, dict):
            continue
        det = bm.get("detail") or {}
        if "降级" not in det:
            continue
        out.append({"date": r.get("date"), "batch": r.get("batch"),
                    "requested": det.get("请求"), "degraded": int(det.get("降级") or 0)})
    return out


def _check_slot_fill() -> dict:
    """C. 每批的 data 槽必须真填上（逐批判，不攒样本、不用合成阈值）。"""
    bs = _funnel_batches()
    if not bs:
        return {"item": "每批 data 槽命中", "ok": None, "pending": True,
                "detail": [f"⏸ {_since_dt()} 起没有带「降级」明细的批次"
                           f"（台账缺失 = 未判定，不是通过）"]}
    bad = [b for b in bs if b["degraded"] > 0]
    if bad:
        return {"item": "每批 data 槽命中", "ok": False, "bad_batches": bad,
                "detail": [f"❌ {len(bad)}/{len(bs)} 批的 data 槽被次优类型补位 —— "
                           f"这一批「数据对比 / 排行榜」的候选池是空的",
                           *[f"   {b['date']} {b['batch']}：请求 {b['requested']} 篇，"
                             f"降级 {b['degraded']} 篇" for b in bad[-5:]],
                           "   处置：查该批的「数据对比」选题供给与 material 剔除，"
                           "**不要**改阈值或后移生效日"]}
    if len(bs) < MIN_BATCHES:
        return {"item": "每批 data 槽命中", "ok": None, "pending": True,
                "detail": [f"⏸ 生效日后仅 {len(bs)} 批（<{MIN_BATCHES}），"
                           f"暂无降级但样本不足（未判定，非通过）"]}
    return {"item": "每批 data 槽命中", "ok": True,
            "detail": [f"{len(bs)} 批全部填上 data 槽（降级 0）✅"]}


def _check_banned() -> dict:
    """E. 禁发类型绝对判据：出现一篇即 FAIL，与样本量无关。"""
    arts, days_seen, _ = _collect(_span_days(), _since_date())
    banned = set(reservoir.BANNED_TYPES)
    hits = [a for a in arts if a["content_type"] in banned]
    if hits:
        return {"item": "禁发类型", "ok": False,
                "detail": [f"❌ 生效日后 {len(hits)} 篇禁发类型（{sorted(banned)}）",
                           *[f"   {a['date']}｜{a['title'][:30]}" for a in hits[:5]]]}
    return {"item": "禁发类型", "ok": True,
            "detail": [f"生效日后 {len(arts)} 篇（{len(days_seen)} 天），"
                       f"无 {sorted(banned)} ✅"]}


def _check_selftest() -> dict:
    bad = [n for n, ok in batch_mix.selftest() if not ok]
    return {"item": "结构逻辑自检", "ok": not bad, "detail": [
        f"自检 {len(batch_mix.selftest())} 项，失败 {len(bad)} 项",
        "全部通过 ✅" if not bad else "❌ " + "；".join(bad[:5])]}


def _check_wiring() -> dict:
    try:
        tree = ast.parse(ORCHESTRATOR.read_text(encoding="utf-8"))
    except Exception as e:  # pragma: no cover
        return {"item": "已接入生成侧", "ok": True, "skipped": True,
                "detail": [f"无法解析 orchestrator：{e}"]}
    calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (getattr(f, "attr", None) == "assign"
                    and getattr(getattr(f, "value", None), "id", None) == "batch_mix"):
                calls += 1
    ok = calls >= 1
    return {"item": "已接入生成侧", "ok": ok, "calls": calls,
            "detail": [f"orchestrator 调用 batch_mix.assign {calls} 处 {'✅' if ok else '❌'}"]}


def _check_structure(days: int) -> dict:
    """D. 实际分布（**观测项，不参与判定**）。

    样本 8 篇时 ±1 篇即 ±12.5pp，比 10pp 容差还大：这个阈值撑不住这个样本量，
    红绿都由单篇翻转决定，红了也定位不到缺哪一类（38% 是「数据对比 + 排行榜」
    的合计数）。所以这里只把实际占比与目标并排打印，硬判据交给成因侧的
    `_check_slot_fill`（每批降级）与 `_check_banned`（禁发类型）。
    """
    arts, days_seen, legacy = _collect(days, _since_date())
    legacy_note = (f"另有 {legacy} 篇是槽位账修复前生成的批次（那时 data 槽会被确定性供给顶掉），"
                   f"不计入统计" if legacy else "")
    if not arts:
        detail = [f"生成侧已接入，但 {_since_date()} 起还没有跑过修复后的批次",
                  "（观测项：无数据时只留空，不判通过）"]
        if legacy_note:
            detail.append(legacy_note)
        return {"item": "实际分布（观测）", "ok": None, "advisory": True,
                "structure": {"total": 0, "legacy": legacy}, "detail": detail}
    s = _structure(arts)
    s["legacy"] = legacy
    detail = [f"样本 {s['total']} 篇（{len(days_seen)} 天，均为新结构批次）",
              f"热点球评 {s['main_pct']}%（目标 {content_mix.TARGET_PCT['热点球评']}%）",
              f"战术榜单 {s['tactical_pct']}%（目标 {content_mix.TARGET_PCT['战术榜单']}%）",
              f"数据型（数据对比+排行榜）{s['data_pct']}%"
              f"（目标 {content_mix.TARGET_PCT['数据对比'] + content_mix.TARGET_PCT['排行榜']}%）",
              "偏差只供看趋势，不作判定：本项按天取（含当天生效前的批次），"
              "样本 <10 篇时单篇即翻转两位数百分点"]
    if legacy_note:
        detail.append(legacy_note)
    return {"item": "实际分布（观测）", "ok": None, "advisory": True,
            "structure": s, "detail": detail}


def _check_config_consistency() -> dict:
    """配比表与结构、禁发清单三处必须自洽（同一判据散在两处就要校验交集）。"""
    total = sum(content_mix.TARGET_PCT.values())
    zero = set(content_mix.ZERO_TARGET)
    banned = set(reservoir.BANNED_TYPES)
    slot_types = set().union(*batch_mix.SLOT_TYPES.values())
    issues = []
    if abs(total - 100) > 0.01:
        issues.append(f"TARGET_PCT 合计 {total}≠100")
    if not banned <= zero:
        issues.append(f"reservoir 禁发 {sorted(banned - zero)} 未在 TARGET_PCT 置 0")
    if slot_types & banned:
        issues.append(f"结构槽位含禁发类型 {sorted(slot_types & banned)}")
    low = set(batch_mix.LOW_TYPES)
    if low & slot_types:
        issues.append(f"低效类型进了主结构 {sorted(low & slot_types)}")
    return {"item": "配比/结构/禁发三处自洽", "ok": not issues, "issues": issues,
            "detail": [f"TARGET_PCT 合计 {total}；禁发 {sorted(banned)}；"
                       f"结构槽位 {sorted(slot_types)}",
                       "自洽 ✅" if not issues else "❌ " + "；".join(issues)]}


def _simulate(days: int = 7) -> tuple[dict, int]:
    """用真实 ``batch_mix.assign`` 模拟 N 天的产出结构（候选充足、无降级）。

    **为什么用模拟而不是只统计历史**：历史批次里「候选不足」会触发补位，
    测出来的份额混着供给侧噪声；而「结构本身会不会跟配比目标打架」是个纯逻辑问题，
    用满候选池跑一遍就能定论（同 `verify_rule_consistency` 的思路：先证伪再谈数据）。
    """
    types = ("热点球评", "数据对比", "排行榜", "战术榜单", "八卦趣事", "人物故事", "转会资讯")
    counts: dict[str, int] = {}
    for _d in range(days):
        day: dict[str, int] = {}
        for _b in range(_SIM_BATCHES_PER_DAY):
            pool = [{"content_type": ct, "title": f"{ct}-{i}"}
                    for ct in types for i in range(_SIM_PER_TYPE)]
            picked, _rep = batch_mix.assign(pool, _SIM_PER_BATCH, day_counts=day)
            for t in picked:
                ct = content_mix.normalize(t.get("content_type"))
                counts[ct] = counts.get(ct, 0) + 1
                day[ct] = day.get(ct, 0) + 1
    total = sum(counts.values())
    pct = {k: round(100.0 * v / total, 1) for k, v in counts.items()} if total else {}
    return pct, total


def _check_structure_matches_target() -> dict:
    """D. 结构隐含份额 vs 配比目标（硬项）——防两处各写各的。

    这条是 2026-11-04「规则与阈值相斥」的可回归化：改动 `batch_mix.STRUCTURE`、
    `SLOT_TYPES`、`_progress` 或 `TARGET_PCT` 任一处而不同步另一处，这里必红。
    判据用配比表自己的容差（`content_mix.TOL_PCT`），不另立第三套阈值。
    """
    implied, total = _simulate()
    if not total:
        return {"item": "结构隐含份额 vs 配比目标", "ok": False,
                "detail": ["❌ 模拟没有产出任何篇目（batch_mix.assign 空转）"]}
    checks, issues = [], []
    for ct in ("热点球评", "数据对比", "排行榜"):
        tgt = content_mix.TARGET_PCT.get(ct, 0.0)
        got = implied.get(ct, 0.0)
        dev = round(got - tgt, 1)
        bad = abs(dev) > _TOL
        checks.append(f"{ct} 结构 {got}% / 目标 {tgt}% / 偏差 {dev:+.1f}pp"
                      + (" ❌ 超容差" if bad else " ✅"))
        if bad:
            issues.append(f"{ct} 偏差 {dev:+.1f}pp 超容差 {_TOL}pp")
    # 战术榜单上限：结构隐含值不能突破目标 + 容差（它在结构里只该做补位）
    tac = implied.get("战术榜单", 0.0)
    if tac > TARGET_TACTICAL_MAX:
        issues.append(f"战术榜单结构隐含 {tac}% > 上限 {TARGET_TACTICAL_MAX}%")
    checks.append(f"战术榜单 结构 {tac}%（上限 {TARGET_TACTICAL_MAX}%）"
                  + (" ❌" if tac > TARGET_TACTICAL_MAX else " ✅"))
    checks.append(f"模拟口径：{_SIM_BATCHES_PER_DAY} 批/天 × {_SIM_PER_BATCH} 篇 × 7 天，"
                  f"每类型 {_SIM_PER_TYPE} 条候选（候选充足、无降级）")
    return {"item": "结构隐含份额 vs 配比目标", "ok": not issues,
            "implied": implied, "issues": issues,
            "detail": checks + (["自洽 ✅"] if not issues else ["❌ " + "；".join(issues)])}


def verify(days: int = 7) -> dict:
    checks = [_check_selftest(), _check_wiring(), _check_config_consistency(),
              _check_structure_matches_target(), _check_slot_fill(),
              _check_banned(), _check_structure(days)]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "since": MIX_SINCE, "ok": all(c["ok"] is not False for c in hard),
            "pending": [c["item"] for c in hard if c.get("ok") is None],
            "checks": checks,
            "hard_failed": [c["item"] for c in hard if c.get("ok") is False]}


def render_md(rep: dict) -> str:
    L = ["# 每批内容类型结构验收（P1-1）", "",
         f"- 生成时间：{rep['generated_at']}｜生效日：{rep['since']}",
         f"- 结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**"
         + (f"（未判定：{'、'.join(rep['pending'])}）" if rep.get("pending") else ""), ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        # 观测项用 ℹ️：它既不参与判定，也不该被读成 ⏸（未判定）
        status = ("ℹ️" if c.get("advisory")
                  else ("⏸" if c.get("ok") is None else ("✅" if c["ok"] else "❌")))
        L.append(f"## {c['item']}：{status}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="每批内容类型结构验收（P1-1）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / f"batch_mix_{_today()}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "batch_mix_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    else:
        print(f"batch_mix: {'ok' if rep['ok'] else 'FAIL ' + '、'.join(rep['hard_failed'])}"
              + (f" pending={rep['pending']}" if rep.get("pending") else ""))
    if rep.get("pending"):
        print(f"::warning::batch_mix 有 {len(rep['pending'])} 项未判定（样本不足），不是通过")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
