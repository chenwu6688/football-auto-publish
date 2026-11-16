#!/usr/bin/env python3
"""发布成功率验收（2026-11-04 新增）。

## 为什么要有这个脚本

此前 38 个验收脚本里，**没有一个在看「稿子到底发出去没有」**：
`verify_publish_gate` 看成稿标题合不合规，`verify_batch_quota` 看篇数对不对，
`verify_cover_coverage` 看有没有图 —— 全是"该不该发"，没有"发没发成"。

2026-11-04 午间批次 2 篇里 1 篇发布失败（`发布超时: 无API响应`），
台账写了原因，**没有任何告警响**。这就是「有结论没留证据」的反面：
证据留了，但没人看。

## 判据

  A. 发布成功率（硬项）——近 7 天 `published / (published + failed)` ≥ 85%；
     `skipped_prediction`（预测稿按设计不发布）不进分母。
     阈值 85% 而非 90%：每天只发 2 篇，7 天约 14 篇，90% 只允许 1 次失败，
     偶发超时就会天天红；85% 允许 2 次，但连续失败仍会被 B 抓住。
  B. 连续失败（硬项）——按时间顺序不得出现连续 ≥2 篇失败。
     这是「发布链路断了」的信号，与比率无关：2 篇全失败时比率 0%，
     但 20 篇里 2 篇失败时比率 90%，后者不该阻断（偶发），前者必须告警。
  C. 失败原因可诊断（硬项）——失败记录必须带 reason。
     没有原因的告警等于没告警（2026-10-25 事故）。
  D. 台账覆盖（告警）——近 7 天有几天完全没有台账。
     缺台账 ≠ 没发布：可能是流程在发布前就断了，或台账没提交。
  E. 无封面发布数（告警，只报数）——判定权在 verify_cover_coverage，这里不重复判定。
  F. 成稿口径成功率（告警，只报数）——2026-11-16 新增，改动点 6。
     分母从「尝试数」回到「成稿 − 按设计跳过」。与 A 项**不合并**：
     A 问「尝试过的发成没有」，F 问「成稿里有多少篇真发出去了」，两件事。
     合并会把「成稿没进台账」算成「发布失败」，或者反过来把它藏起来。
  G. 台账恒等式（硬项）——2026-11-16 新增，改动点 6。
     I1 记录分区：``counts.total == len(records) ==
        published + failed + skipped_prediction + skipped_already_published``。
        直接数 records 复核，**不依赖文件自己写的数字**；老台账也能判。
     I2 成稿覆盖：``counts.drafts >= counts.total``（成稿不可能少于台账记录）。
        只在带 ``counts.drafts`` 的日期可判（口径生效日 2026-11-16 起），
        此前日期判「未判定」，不判通过也不判红。

未判定（``ok=None``）不算通过：窗口内发布样本 <5 篇时给 ⏸ 并打 CI warning。

用法：
  python3 scripts/verify_publish_success.py
  python3 scripts/verify_publish_success.py --days 7 --quiet
"""

import argparse
import glob
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ledger_funnel  # noqa: E402  成稿→发布口径的唯一实现（改动点 6）

OUTPUT_DIR = PROJECT_ROOT / "output"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

MIN_SAMPLE = 5            # 发布样本下限：低于此数不下结论
MIN_SUCCESS_PCT = 85.0    # A 项阈值
MAX_CONSEC_FAIL = 2       # B 项：连续失败达到这个数即判失败


def _today() -> datetime.date:
    return datetime.now(CST).date()


def _load_window(days: int) -> tuple[list[dict], list[str], list[str]]:
    """返回 (records, 有台账的日期, 缺台账的日期)。

    只读「当天聚合」文件 ``publish_ledger_YYYY-MM-DD.json``：按批次文件逐个数会
    在批次名不规范时重复计数，聚合文件已经是去重后的结果。
    """
    end = _today()
    recs: list[dict] = []
    have, missing = [], []
    for i in range(days - 1, -1, -1):
        day = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        p = AUDIT_DIR / f"publish_ledger_{day}.json"
        if not p.exists():
            missing.append(day)
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            missing.append(day)
            continue
        have.append(day)
        for r in d.get("records") or []:
            rr = dict(r)
            rr.setdefault("date", day)
            recs.append(rr)
    return recs, have, missing


def _ordered(recs: list[dict]) -> list[dict]:
    """按 (日期, 批次) 排序，用于「连续失败」判定。"""
    order = {"morning": 0, "晨间": 0, "noon": 1, "午间": 1, "evening": 2, "晚间": 2}
    return sorted(recs, key=lambda r: ((r.get("date") or ""),
                                       order.get(str(r.get("batch") or ""), 1)))


def check_success_rate(days: int) -> dict:
    item = "发布成功率（近 %d 天）" % days
    recs, have, missing = _load_window(days)
    pub = [r for r in recs if r.get("status") == "published"]
    failed = [r for r in recs if r.get("status") == "failed"]
    denom = len(pub) + len(failed)
    if denom < MIN_SAMPLE:
        return {"item": item, "ok": None, "pending": True,
                "detail": [f"⏸ 近 {days} 天已发布 {len(pub)} + 失败 {len(failed)} = {denom} 篇"
                           f"（<{MIN_SAMPLE}），未判定（不等于通过）"]}
    pct = 100.0 * len(pub) / denom
    ok = pct >= MIN_SUCCESS_PCT
    detail = [f"{'✅' if ok else '❌'} 近 {days} 天：成功 {len(pub)} / 尝试 {denom} 篇，"
              f"成功率 {pct:.1f}%（下限 {MIN_SUCCESS_PCT:.0f}%）",
              f"台账覆盖 {len(have)} 天，缺 {len(missing)} 天"
              + (f"（{', '.join(missing[:3])}）" if missing else "")]
    if failed:
        from collections import Counter
        top = Counter((r.get("reason") or "（无原因）")[:40] for r in failed).most_common(3)
        detail.append("失败原因 TOP：" + "；".join(f"{k}×{v}" for k, v in top))
    return {"item": item, "ok": ok, "pending": False, "published": len(pub),
            "failed": len(failed), "rate": round(pct, 1), "detail": detail}


def check_consecutive_fail(days: int) -> dict:
    """B. 连续失败 ≥2 篇即失败——链路断了的信号，与比率无关。"""
    item = "连续发布失败（≥%d 篇）" % MAX_CONSEC_FAIL
    recs, _have, _missing = _load_window(days)
    seq = [r for r in _ordered(recs)
           if r.get("status") in ("published", "failed")]
    if not seq:
        return {"item": item, "ok": None, "pending": True,
                "detail": ["⏸ 窗口内无发布尝试，未判定（不等于通过）"]}
    run = best = 0
    worst_at = ""
    for r in seq:
        if r.get("status") == "failed":
            run += 1
            if run > best:
                best, worst_at = run, (r.get("date") or "")
        else:
            run = 0
    ok = best < MAX_CONSEC_FAIL
    return {"item": item, "ok": ok, "pending": False, "max_consecutive": best,
            "detail": [f"{'✅' if ok else '❌'} 最长连续失败 {best} 篇"
                       f"（连续 {MAX_CONSEC_FAIL} 篇才判失败）"
                       + (f"，发生在 {worst_at} 附近" if best else "")]}


def check_fail_reason(days: int) -> dict:
    """C. 失败必须带原因——没原因的告警等于没告警。"""
    item = "失败原因可诊断"
    recs, _have, _missing = _load_window(days)
    failed = [r for r in recs if r.get("status") == "failed"]
    if not failed:
        return {"item": item, "ok": True, "skipped": True,
                "detail": [f"近 {days} 天无失败，无需诊断（跳过）"]}
    naked = [r for r in failed if not (r.get("reason") or "").strip()]
    ok = not naked
    detail = [f"{'✅' if ok else '❌'} 失败 {len(failed)} 篇，带原因 "
              f"{len(failed) - len(naked)} 篇"]
    if naked:
        detail.append("❌ 无原因：" + "；".join((r.get("title") or "")[:20] for r in naked[:3]))
    return {"item": item, "ok": ok, "pending": False, "detail": detail}


def check_ledger_coverage(days: int) -> dict:
    """D. 台账覆盖（告警）——缺台账 ≠ 没发布，但两种都该看见。"""
    item = "发布台账覆盖"
    _recs, have, missing = _load_window(days)
    if not have and not missing:
        return {"item": item, "ok": True, "advisory": True, "skipped": True,
                "detail": ["窗口内无 data/audit，跳过"]}
    ok = not missing
    detail = [f"近 {days} 天有台账 {len(have)} 天"
              + (f"，缺 {len(missing)} 天（{', '.join(missing[:5])}）" if missing else "")]
    if missing:
        detail.append("缺台账的两种可能：① 该日没跑批次；② 跑了但发布前就中断；"
                      "③ 台账未提交。三者处置不同，需看当日 Actions 结论")
    return {"item": item, "ok": ok, "advisory": True, "detail": detail}


def check_no_cover_count(days: int) -> dict:
    """E. 无封面发布数（告警，只报数，判定权在 verify_cover_coverage）。"""
    item = "无封面发布数"
    recs, _have, _missing = _load_window(days)
    pub = [r for r in recs if r.get("status") == "published"]
    if not pub:
        return {"item": item, "ok": True, "advisory": True, "skipped": True,
                "detail": [f"近 {days} 天无已发布篇，跳过"]}
    zero = [r for r in pub if not (r.get("images") or 0)]
    pct = 100.0 * len(zero) / len(pub)
    return {"item": item, "ok": True, "advisory": True, "no_cover": len(zero),
            "published": len(pub),
            "detail": [f"已发布 {len(pub)} 篇，其中零配图 {len(zero)} 篇（{pct:.1f}%）",
                       "判定权在 verify_cover_coverage（上限 5%）；本项只报数不回滚"]}


def _day_ledgers(days: int) -> list[tuple[str, dict]]:
    """(日期, 日级台账 json) 列表。缺文件/坏文件的日期直接不返回，由 D 项报覆盖。"""
    end = _today()
    out = []
    for i in range(days - 1, -1, -1):
        day = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        p = AUDIT_DIR / f"publish_ledger_{day}.json"
        if not p.exists():
            continue
        try:
            out.append((day, json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


def check_draft_rate(days: int) -> dict:
    """F. 成稿口径成功率（告警，只报数）。

    分母 = 成稿 − 按设计跳过（预测稿 / 同题重复）。成稿 = 落盘成稿 + 合规剔除。
    与 A 项并列报，**不取平均、不替换 A** —— 两个数回答不同问题。
    """
    item = "成稿口径成功率（近 %d 天，只报数）" % days
    ledgers = _day_ledgers(days)
    drafts = dropped = lost = gate = pub = skipped = 0
    days_with = 0
    for _day, d in ledgers:
        c = d.get("counts") or {}
        if not isinstance(c.get("drafts"), int):
            continue                     # 口径生效日之前：没有成稿侧字段
        days_with += 1
        drafts += c["drafts"]
        dropped += int(c.get("dropped_compliance") or 0)
        lost += int(c.get("lost_upstream") or 0)
        gate += int(c.get("published_with_title_gate_issue") or 0)
        pub += int(c.get("published") or 0)
        skipped += int(c.get("skipped_prediction") or 0) + int(c.get("skipped_already_published") or 0)
    if days_with == 0:
        return {"item": item, "ok": None, "advisory": True, "pending": True,
                "detail": [f"⏸ 近 {days} 天没有一天带 counts.drafts"
                           f"（口径生效日 {ledger_funnel.FUNNEL_SINCE}，此前台账无成稿侧字段）",
                           "未判定 ≠ 通过；等新台账落盘后再看"]}
    denom = drafts + dropped - skipped
    if denom < MIN_SAMPLE:
        return {"item": item, "ok": None, "advisory": True, "pending": True,
                "detail": [f"⏸ 成稿 {denom} 篇（<{MIN_SAMPLE}），未判定（不等于通过）"]}
    pct = 100.0 * pub / denom
    detail = [f"成稿 {drafts} 篇 + 合规剔除 {dropped} 篇 − 按设计跳过 {skipped} 篇 "
              f"= 应发 {denom} 篇；实发 {pub} 篇 → **{pct:.1f}%**",
              f"缺口拆解：成稿在但台账无记录 {lost} 篇、合规剔除 {dropped} 篇"
              "（后者是规则挡下的稿子，不是发失败）",
              f"对照 A 项（尝试口径）只算 published/(published+failed)，"
              f"两数并列看；把取证缺口读成发失败，会去修发布器——修错了地方"]
    if gate:
        detail.append(f"另：{gate} 篇标题结构不合格仍发布（已落 title_gate_blocked，"
                      "属**已发布**带瑕疵，不计入缺口）")
    return {"item": item, "ok": True, "advisory": True, "draft_rate": round(pct, 1),
            "drafts": drafts, "dropped_compliance": dropped, "lost_upstream": lost,
            "published": pub, "detail": detail}


def check_ledger_identity(days: int) -> dict:
    """G. 台账恒等式（硬项）—— 比阈值耐用：不依赖任何主观标定。

    I1 记录分区：``counts.total == len(records) == 各 status 之和``。
       破了 ⇒ 有 status 写进 records 却没计数（新状态码漏登记），或 counts 与
       records 不同步（合并时被覆盖）。
    I2 成稿覆盖：``counts.drafts >= counts.total``。
       破了 ⇒ 台账记录比成稿还多（日级聚合重复合并 / 跨日混入 / metadata 被
       后一批覆盖而台账没跟上）。

    I1 直接数 records 复核，所以**老台账也判**；I2 需要 counts.drafts，
    生效日（2026-11-16）之前的日期判「未判定」，不判通过也不判红。
    """
    item = "台账恒等式"
    ledgers = _day_ledgers(days)
    if not ledgers:
        return {"item": item, "ok": None, "pending": True,
                "detail": [f"⏸ 近 {days} 天无台账，未判定（不等于通过）"]}
    bad_partition, bad_cover, cover_judged = [], [], 0
    for day, d in ledgers:
        recs = d.get("records") or []
        c = d.get("counts") or {}
        known = sum(1 for r in recs if r.get("status") in ledger_funnel.STATUS_KEYS)
        c_total = c.get("total")
        if not (len(recs) == known == c_total):
            bad_partition.append(
                f"{day}：counts.total={c_total} 记录 {len(recs)} 条，"
                f"已登记 status {known} 条")
        if isinstance(c.get("drafts"), int):
            cover_judged += 1
            if c["drafts"] < int(c_total or 0):
                bad_cover.append(f"{day}：成稿 {c['drafts']} 篇 < 台账记录 {c_total} 条")
    detail, ok = [], True
    if bad_partition:
        ok = False
        detail.append(f"❌ I1 记录分区不成立 {len(bad_partition)} 天：")
        detail += [f"   · {x}" for x in bad_partition[:5]]
        detail.append("   ⇒ 查 ledger_funnel.STATUS_KEYS 是否漏登记新状态码；别去改阈值")
    else:
        detail.append(f"✅ I1 记录分区成立：{len(ledgers)} 天，"
                      "counts.total = 记录数 = 已登记 status 之和")
    if bad_cover:
        ok = False
        detail.append(f"❌ I2 成稿覆盖不成立 {len(bad_cover)} 天：")
        detail += [f"   · {x}" for x in bad_cover[:5]]
        detail.append("   ⇒ 台账记录多于成稿 = 日级聚合重复合并或混进别天记录")
    elif cover_judged == 0:
        detail.append(f"⏸ I2 成稿覆盖未判定：窗口内无一天带 counts.drafts"
                      f"（口径生效日 {ledger_funnel.FUNNEL_SINCE}）")
    else:
        detail.append(f"✅ I2 成稿覆盖成立：{cover_judged} 天，drafts ≥ total")
    # I1 全过、I2 一天都没判 → 整体「未判定」，不许读成通过
    if ok and cover_judged == 0:
        ok = None
    return {"item": item, "ok": ok, "pending": ok is None,
            "partition_bad": len(bad_partition), "cover_bad": len(bad_cover),
            "cover_judged": cover_judged, "detail": detail}


def verify(days: int = 7) -> dict:
    checks = [check_success_rate(days), check_consecutive_fail(days),
              check_fail_reason(days), check_ledger_coverage(days),
              check_no_cover_count(days), check_draft_rate(days),
              check_ledger_identity(days)]
    hard = [c for c in checks if not c.get("advisory")]
    failed = [c["item"] for c in hard if c.get("ok") is False]
    pending = [c["item"] for c in hard if c.get("ok") is None]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "window_days": days,
            "ok": (False if failed else (None if pending else True)),
            "checks": checks, "hard_failed": failed, "hard_pending": pending}


def render_md(rep: dict) -> str:
    verdict = {True: "通过 ✅", False: "未通过 ❌", None: "部分通过 ⏸"}[rep["ok"]]
    L = ["# 发布成功率验收（2026-11-04 新增）", "",
         f"- 生成时间：{rep['generated_at']}｜窗口 {rep['window_days']} 天",
         f"- 验收结论：**{verdict}**",
         "- 补的缺口：此前没有任何验收在看「稿子实际发出去没有」，"
         "11-04 午间 1 篇发布超时失败，台账有原因但零告警",
         ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    if rep.get("hard_pending"):
        L += [f"> 未判定（不等于通过）：{'、'.join(rep['hard_pending'])}", ""]
    for c in rep["checks"]:
        tag = "告警" if c.get("advisory") else "硬项"
        if c.get("skipped"):
            status = "跳过"
        elif c.get("ok") is None:
            status = "⏸ 未判定"
        else:
            status = "✅" if c["ok"] else "❌"
        L.append(f"## {c['item']}（{tag}）：{status}")
        for d in c.get("detail", []):
            L.append(f"- {d}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="发布成功率验收")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"publish_success_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "publish_success_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    if rep["ok"] is None:
        print(f"::warning::发布成功率验收有 {len(rep.get('hard_pending') or [])} 项未判定"
              f"（不等于通过）", file=sys.stderr)
        return 0
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
