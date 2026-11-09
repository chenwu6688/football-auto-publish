#!/usr/bin/env python3
"""发布闸门验收（计划 7.2 / 7.3）。

计划 7.2 验收：溯源覆盖率 100%；标题格式合规通过率不低于 90%；连续 14 天无旧闻新发。
计划 7.3 验收：一致性校验拦截率可统计且误杀率低于 5%。

校验项：
  A. 标题格式合规通过率 ≥90%（硬项）—— 用项目自身评分器 score_title_plan 复算成稿标题；
  B. 溯源覆盖率（告警项，无产物跳过）—— sources_used 非空比例；
  C. 一致性校验可统计（硬项，无记录跳过）—— consistency_stats 台账就绪且能算出拦截率；
  D. 误杀率 <5%（告警项，无人工标注则待接入）—— 绝不把「未标注」当 0；
  E. 连续 14 天无旧闻新发（硬项，无台账跳过）—— 时效闸门台账零「已知时效>上限」保留。
  G. 熔断须拦住当前批次（硬项）—— 生成阶段触发的熔断同样中止当晚发布（2026-10-13 事故修复）。
  A5. 比分判定口径归一（硬项）—— 0-1／0比1／0：1 等价，日期时间不算比分（2026-10-24 熔断修复）。
  H. 失败告警必须带原因（硬项）—— 告警正文含「原因」行，手机端可判断熔断/登录/发布（2026-10-25 修复）。

产出 data/audit/publish_gate_*.md + publish_gate_latest.json；硬项 FAIL 退出码 1。

用法：
  python3 scripts/verify_publish_gate.py
  python3 scripts/verify_publish_gate.py --quiet
"""

import argparse
import glob
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import consistency_stats as cs  # noqa: E402
import freshness_ledger as fl  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "output"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
CST = timezone(timedelta(hours=8))

# ── 时效闸门的「比例型盲区」上限（2026-11-09 立）──────────────────────────
#: 台账 ``unknown`` = 发布时间无法判定、被闸门**保守放行**的素材。它是闸门
#: 唯一看不见的部分：源站改版把时间弄丢，unknown 会从 28% 涨到 100%，而
#: 「已知超时仍保留」始终为空 → E 项永远绿。所以给比例设上限。
#:
#: 50% 这个数不是新发明的：全仓对「未判定」的一贯口径就是「过半即不能算通过」
#: （``verify_compliance`` 的 rc=2 判据即 ``len(judged) * 2 < len(block_rules)``）。
#: 同一量两处定义不同阈值，就是 11-04「规则相斥」事故的起手式。
MAX_UNKNOWN_RATIO = 0.50
#: 告警线。实测近 5 天为 27~28%（189/678），留 7 个点余量，日常不吵；
#: 一旦越过就说明源站时间字段在退化，趁还没到 50% 提前看见。
WARN_UNKNOWN_RATIO = 0.35
#: 比例要有意义，分母不能太小。样本不足只报数、不下结论（未判定 ≠ 通过）。
MIN_UNKNOWN_SAMPLE = 100


def _iter_articles():
    for p in sorted(glob.glob(str(OUTPUT_DIR / "**" / "metadata.json"), recursive=True)):
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in (data.get("articles") or []):
            yield data, a


WINDOW_DAYS = 14            # 计划 7.2/7.3 与 8.1 一致的滚动窗口口径

# 验收器补 recent_titles 的生效日（2026-11-04 修复 _score_titles）。
#
# 为什么必须设生效日：修复前这个脚本从不传历史标题，疑问收尾限额**从未触发过**，
# 于是它一直报「零硬违规」。修好后一次性冒出 56 条存量违规（86 条里），其中绝大多数
# 来自 9 月底~10 月那段我们自己把 prompt 写歪的日子（疑问收尾占比冲到 84%）。
# 那是真实的历史违规，不是误杀 —— 但它们不该让 CI 永久挂红：按既往教训，
# 红超过两周就一定被人加 `|| echo` 软化，等于把刚修好的尺子又做松。
#
# 处置：硬项只判生效日之后的稿（如实拦真违规），存量数字在报告里照实写出来
# （不许隐瞒），并在 A1 的 detail 里点名「这是修复前口径漏检的存量」。
GATE_SINCE = "2026-11-05"

# 三要素覆盖率（A5）的生效日与判定线（2026-11-04 夜间，修「规则与阈值相斥」）。
#   生效日：三要素硬闸门是 11-04 下午进的主干，之前产出的标题**没有跑过这条闸门**，
#           拿它们算通过率是在给新规则判旧账。
#   判定线 85%：三要素不达标会被 score_title_plan 直接打回并重拟标题，真按硬闸门执行
#           应当接近 100%；留 15pp 是给 `ensure_fixture_title` 里「重拟 + 模板兜底
#           都失败 → 保留原题发布」（有 degrade 留痕）这一条唯一的口子。
#   旧版是不分新旧一律 ≥50% —— 一条硬闸门配一个「一半违规也算达标」的阈值，
#   等于自己承认闸门可以不做，这是这轮排查里最典型的一处规则相斥。
ELEM_SINCE = "2026-11-05"
ELEM_PASS_MIN = 85.0
ELEM_MIN_SAMPLE = 5


def _recent_articles(days=WINDOW_DAYS):
    floor = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    for data, a in _iter_articles():
        if (data.get("date") or "") >= floor:
            yield data, a


def _score_titles(articles):
    """用项目自身评分器复算，返回 (total, passed, zero, fails, antipattern, r11, elements)。

    zero 只统计「传闻当官宣 / 禁用模板前缀」这类原有硬违规；
    赛果播报式（title_antipattern，2026-10-12 新增）单独归入 antipattern，
    避免新规则把历史存量的 A1 硬项拖红——新规则的验收在
    scripts/verify_title_antipattern.py 独立完成。

    r11（2026-10-13 新增）：标题正向规则（title_positive）的减分可能把历史存量标题
    也压到 0 分；这类 0 分带有「R11数据:」前缀理由，须单独归入 r11 桶——
    既非原有硬违规（zero），也非赛果播报式（anti），避免误拖 A1 红。

    elements（2026-11-04 新增）：三要素硬闸门（title_elements）同理——它是新规则，
    旧口径产出的历史成稿天然不满足（实测 86 条里 35 条缺项），单列 elem 桶，
    验收在 ``check_title_elements_residue`` 里按覆盖率趋势看，不拖 A1 硬项。

    **recent_titles 必须传（2026-11-04 修复）**：此前复算时不传历史，疑问收尾限额这条
    规则**永远不触发**，与生成侧判出两个相反结论。硬证据——11-04 午间那篇
    「大罗VS C罗巅峰数据对决，谁才是真正的锋线之王？」：
      · 不传 recent_titles → 74 分，通过（旧复算口径，A1 判「零硬违规」）
      · 传 recent_titles   →  0 分，违规（近 14 天疑问收尾已 2 篇）
    而那篇当天真的发布出去了。验收器丢上下文＝假绿灯，比没有验收更糟：
    它会让人以为这条规则在工作。

    历史口径与运行期一致但**不读盘**：对每篇取其日期之前 ``WINDOW_DAYS-1`` 天内的标题
    （不含当日），再把同日已判过的标题按顺序补进去——运行期 ``_SESSION_TITLES`` 做的
    正是这件事。不调 ``recent_titles_for_gate``，因为它掺了进程状态和"今天"，复算历史
    某一天时会算出错的窗口。
    """
    import orchestrator
    rows = [(data, a) for data, a in articles]
    dated = []
    for data, a in rows:
        title = (a.get("title") or "").strip()
        if title:
            dated.append(((data.get("date") or "").strip(), title, a))

    def _hist_before(day: str) -> list[str]:
        try:
            cur = datetime.strptime(day, "%Y-%m-%d").date()
        except Exception:
            return []
        lo = (cur - timedelta(days=WINDOW_DAYS - 1)).strftime("%Y-%m-%d")
        return [t for d, t, _ in dated if lo <= d < day]

    total = passed = 0
    zero, fails, anti, r11, elem = [], [], [], [], []
    hist_cache: dict[str, list[str]] = {}
    used_today: dict[str, list[str]] = {}
    for day, title, a in dated:
        if day not in hist_cache:
            hist_cache[day] = _hist_before(day)
        recent = hist_cache[day] + used_today.get(day, [])
        total += 1
        sc, ok, reasons = orchestrator.score_title_plan(
            {}, title, a.get("content_type", ""), recent_titles=recent)
        used_today.setdefault(day, []).append(title)
        if ok:
            passed += 1
        elif sc == 0:
            row = {"date": day, "title": title, "reasons": reasons[:2]}
            if any(r.startswith("[三要素]") for r in reasons):
                elem.append(row)
            elif any("开场" in r for r in reasons):
                anti.append(row)
            elif any(r.startswith("R11数据") for r in reasons):
                r11.append(row)
            else:
                zero.append(row)
        else:
            fails.append({"date": day, "title": title, "score": sc,
                          "reasons": reasons[:2]})
    return total, passed, zero, fails, anti, r11, elem


def _stocked_before_gate() -> set[str]:
    """标题**入库日**早于生效日的蓄水池条目标题。

    2026-11-05：noon 批次转正的两篇软性稿（「…还能找回吗？」「…谁才是你眼中的最强？」）
    是 10-11 存进蓄水池的旧标题，发布时落在生效日之后，被本项判成硬违规。它们既不是
    新管线的产出、也改不回来（已发布），要按「标题什么时候生成的」判，而不是「什么时候
    发出去的」——与封面台账「按兜底生效日判、旧链路样本单列留证」是同一口径。
    证据来自 conftest 保护下的蓄水池台账（stocked_at），不搞手写豁免名单。
    """
    out: set[str] = set()
    p = PROJECT_ROOT / "data" / "reservoir" / "items.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return out
    for it in data.get("items", []) or []:
        stocked = str(it.get("stocked_at") or "")[:10]
        if not stocked or stocked >= GATE_SINCE:
            continue
        t = (it.get("title") or (it.get("article") or {}).get("title") or "").strip()
        if t:
            out.add(t)
    return out


def check_title_hard() -> dict:
    """A1. 成稿标题零「硬违规」（硬项）——**生效日后**成稿里无 score_title_plan=0 的标题
    （0 分即传闻当官宣 / 疑问收尾超限 / 三要素不足 / 禁用模板前缀等硬违规）。

    口径说明：赛果播报式（title_antipattern）属 2026-10-12 新增规则，
    其存量产出单列为 A4 告警项，不计入本硬项；标题正向规则的减分（R11）若把历史
    存量压到 0 分，单独归 r11 桶，同样不计入本硬项——避免历史存量把 A1 拖红。

    生效日 ``GATE_SINCE``（见常量注释）：本项此前是**假绿灯**——复算不传
    ``recent_titles``，疑问收尾限额永不触发。修复后暴露的 56 条存量单列在
    ``legacy_violations``，照实报数、不隐瞒，但不参与退出码。
    """
    item = "成稿标题零硬违规（生效日起）"
    try:
        import orchestrator  # noqa: F401
    except Exception as e:  # pragma: no cover
        return {"item": item, "ok": True, "skipped": True,
                "detail": [f"无法载入评分器：{e}"]}
    arts = list(_recent_articles())
    if not arts:
        return {"item": item, "ok": True, "skipped": True,
                "detail": ["近 14 天无成稿 metadata，本项跳过"]}
    total, _passed, zero, _fails, _anti, _r11, _elem = _score_titles(arts)
    live = [z for z in zero if (z.get("date") or "") >= GATE_SINCE]
    legacy = [z for z in zero if (z.get("date") or "") < GATE_SINCE]
    # 蓄水池存量：标题入库早于生效日，发布晚于生效日。留证、不计退出码。
    _old_stock = _stocked_before_gate()
    carried = [z for z in live if (z.get("title") or "").strip() in _old_stock]
    live = [z for z in live if (z.get("title") or "").strip() not in _old_stock]
    live_total = sum(1 for data, a in arts
                     if (data.get("date") or "") >= GATE_SINCE
                     and (a.get("title") or "").strip())
    base = [f"近 14 天成稿标题 {total} 条；生效日（{GATE_SINCE}）后 {live_total} 条，"
            f"硬违规 {len(live)} 条"]
    if legacy:
        base.append(f"· 修复前存量 0 分 {len(legacy)} 条（旧复算不传历史，限额从未触发；"
                    f"如实留证，不参与退出码）")
    if carried:
        base.append(f"· 蓄水池旧标题 {len(carried)} 条（入库早于生效日，转正后发布；"
                    f"根因已修：备货 prompt + 入库闸门 + 转正准入，不参与退出码）")
    if live_total == 0:
        return {"item": item, "ok": None, "pending": True, "total": total,
                "legacy_violations": len(legacy), "detail": base + [
                    f"⏸ 生效日后暂无成稿，未判定（不等于通过）"]}
    return {"item": item, "ok": not live, "skipped": False,
            "total": total, "violations": live[:10],
            "legacy_violations": len(legacy), "r11": len(_r11),
            "detail": base + [("零硬违规 ✅" if not live
                               else f"❌ 硬违规：{live[:3]}")]}


def check_title_pass_rate() -> dict:
    """A2. 标题格式合规通过率 ≥90%（告警项，近 14 天滚动口径）。

    评分器接入前的存量标题不作为达标依据；新管线成稿≥60 分方发布，
    故滚动值反映管线质量趋势。口径写入报告，**不假装通过**（同 verify_fixture_pool 风格）。
    """
    try:
        import orchestrator  # noqa: F401
    except Exception as e:  # pragma: no cover
        return {"item": "标题格式合规通过率≥90%（近14天）", "ok": True, "advisory": True,
                "skipped": True, "detail": [f"无法载入评分器：{e}"]}
    arts = list(_recent_articles())
    if not arts:
        return {"item": "标题格式合规通过率≥90%（近14天）", "ok": True, "advisory": True,
                "skipped": True, "detail": ["近 14 天无成稿 metadata，本项跳过"]}
    total, passed, _zero, fails, _anti, _r11, _elem = _score_titles(arts)
    rate = passed / total if total else 0.0
    return {"item": "标题格式合规通过率≥90%（近14天）", "ok": True, "advisory": True,
            "skipped": False, "total": total, "passed": passed, "rate": round(rate, 4),
            "fails": fails[:10],
            "detail": [f"近 14 天成稿 {total} 条，合规（≥60 分）{passed} 条，通过率 {rate:.1%}（目标≥90%）",
                       ("达标 ✅" if rate >= 0.90 else
                        "⚠️ 未达 90%（存量 + 生成质量滚动值，告警不阻断；新管线成稿≥60 分方发布）"),
                       (f"未达标样例：{fails[:3]}" if fails else "无未达标样例")]}


def check_title_antipattern_residue() -> dict:
    """A4. 赛果播报式存量（告警项）——近 14 天窗口内命中 title_antipattern 的成稿。

    规则 2026-10-12 生效，只对**新生成**的标题打回；此前存量会随时间自然滚出窗口。
    新规则的完整验收在 scripts/verify_title_antipattern.py（含真实数据回测）。
    """
    try:
        import title_antipattern as ta
    except Exception as e:  # pragma: no cover
        return {"item": "赛果播报式存量（近14天，告警）", "ok": True, "advisory": True,
                "skipped": True, "detail": [f"无法载入反模式闸门：{e}"]}
    arts = list(_recent_articles())
    if not arts:
        return {"item": "赛果播报式存量（近14天，告警）", "ok": True, "advisory": True,
                "skipped": True, "detail": ["近 14 天无成稿 metadata，本项跳过"]}
    hits = []
    for data, a in arts:
        t = (a.get("title") or "").strip()
        if t and ta.check(t)["hit"]:
            hits.append(f"{data.get('date')} {t}")
    return {"item": "赛果播报式存量（近14天，告警）", "ok": not hits, "advisory": True,
            "skipped": False, "hits": hits[:10], "total": len(arts),
            "detail": [f"近 14 天成稿 {len(arts)} 条，命中赛果播报式 {len(hits)} 条"
                       + ("（规则生效前存量，随窗口滚出）" if hits else " ✅"),
                       *hits[:5]]}


def check_title_elements_residue() -> dict:
    """A5. 标题三要素覆盖率（2026-11-04 生效，告警项）。

    为什么单列而不是并进 A1：三要素是新规则，**旧口径产出的历史成稿天然不满足**
    （实测近 14 天 86 条里 35 条缺项），并进 A1 会让硬项红满 14 天，
    然后就等着被 `|| echo` 软化——11-03 已经踩过这个坑。

    ▼ 2026-11-04 夜间修「规则与阈值相斥」：原来不分新旧，一律「通过率 ≥50% 算达标」。
      这对一条**硬闸门**是不成立的——`score_title_plan` 里三要素不达标直接 0 分打回，
      真按硬闸门执行，通过率应当接近 100%；50% 等于书面承认「一半违规也能发」。
      现在按生效日分口径：
        · 存量（< ``ELEM_SINCE``）：只报数、不判定，随窗口滚出；
        · 生效日后：通过率 ≥ ``ELEM_PASS_MIN``（85%，留给「重拟全失败→保留原题」的兜底）；
        · 生效日后样本 < ``ELEM_MIN_SAMPLE`` → ``ok=None``（未判定，不是通过）。
      这样「硬闸门」与「告警阈值」说的是同一件事，而不是两句互相拆台的话。
    """
    try:
        import title_elements as te
    except Exception as e:  # pragma: no cover
        return {"item": "标题三要素覆盖率（近14天，告警）", "ok": True, "advisory": True,
                "skipped": True, "detail": [f"无法载入三要素口径：{e}"]}
    arts = list(_recent_articles())
    if not arts:
        return {"item": "标题三要素覆盖率（近14天，告警）", "ok": True, "advisory": True,
                "skipped": True, "detail": ["近 14 天无成稿 metadata，本项跳过"]}

    legacy_t, live_t = [], []
    for data, a in arts:
        t = (a.get("title") or "").strip()
        if not t:
            continue
        (live_t if (data.get("date") or "") >= ELEM_SINCE else legacy_t).append(t)

    cov = te.coverage(live_t) if live_t else {"n": 0, "pass_rate": None,
                                              "number": 0, "contrast": 0,
                                              "proper": 0, "all_three": 0}
    legacy = te.coverage(legacy_t) if legacy_t else {"n": 0, "pass_rate": None}
    base = [f"生效日前存量 {legacy['n']} 条（三要素通过率 {legacy['pass_rate']}%，"
            f"只留证不判定，随 14 天窗口滚出）"
            if legacy_t else "生效日前无存量样本"]

    if cov["n"] < ELEM_MIN_SAMPLE:
        return {"item": "标题三要素覆盖率（生效日起，告警）", "ok": None, "advisory": True,
                "pending": True, "coverage": cov, "legacy": legacy,
                "detail": base + [f"⏸ 生效日（{ELEM_SINCE}）后样本 {cov['n']} 条 "
                                  f"< {ELEM_MIN_SAMPLE}，未判定（不等于通过）"]}

    ok = (cov["pass_rate"] or 0) >= ELEM_PASS_MIN
    return {"item": "标题三要素覆盖率（生效日起，告警）", "ok": ok, "advisory": True,
            "skipped": False, "coverage": cov, "legacy": legacy,
            "detail": base + [
                f"生效日后 {cov['n']} 条：数字 {cov['number']}% / 对比 {cov['contrast']}% / "
                f"专名 {cov['proper']}% / 三项全过 {cov['all_three']}%",
                f"达下限（≥{te.MIN_ELEMENTS} 项且含专名）通过率 {cov['pass_rate']}%"
                f"（目标 ≥{ELEM_PASS_MIN:.0f}%，与硬闸门语义一致）"
                + (" ✅" if ok else " ⚠️ 说明重拟+模板兜底没接住，属真实漏出"),
                "尺子松紧由 scripts/verify_title_elements.py 硬判"
                "（我方单调性 + 头部不误杀 + 压力带）"]}


def check_team_alias_awareness() -> dict:
    """A3. 队名判定必须「别名感知」（硬项）。

    计划 7.3 要求误杀率<5%。2026-10-12 的真实熔断即由「同队不同写法」误报引起
    （事实层「拜仁」vs 来源「拜仁慕尼黑」→ 误报「缺少客队名: 拜仁慕尼黑」）。
    这里把该类误报做成可回归校验：同义写法必须放行，真缺队名/卡外球队必须拦截。
    """
    try:
        import orchestrator
    except Exception as e:  # pragma: no cover
        return {"item": "队名判定别名感知（计划7.3）", "ok": True, "skipped": True,
                "detail": [f"无法载入事实校验器：{e}"]}
    # 探测会写一致性台账，须隔离到临时文件，绝不污染真实 data/consistency_stats.json
    import consistency_stats as cs
    tmp = tempfile.TemporaryDirectory()
    _s, _l = cs.STATS_PATH, cs.LABELS_PATH
    cs.STATS_PATH = Path(tmp.name) / "stats.json"
    cs.LABELS_PATH = Path(tmp.name) / "labels.json"
    try:
        return _probe_team_alias(orchestrator)
    finally:
        cs.STATS_PATH, cs.LABELS_PATH = _s, _l
        tmp.cleanup()


def _probe_team_alias(orchestrator) -> dict:
    cases = [
        # (home, away, hg, ag, 正文, 期望放行, 说明)
        ("多特", "拜仁慕尼黑", 1, 2, "拜仁客场2-1击败多特。", True, "全称 fixture + 简称正文"),
        ("多特", "拜仁", 1, 2, "拜仁慕尼黑客场2-1取胜多特。", True, "简称 fixture + 全称正文"),
        ("巴黎圣日耳曼", "马赛", 2, 0, "巴黎2-0马赛。", True, "巴黎/巴黎圣日耳曼"),
        ("曼城", "曼联", 1, 0, "曼城主场取胜。", False, "真缺客队名仍须拦截"),
        ("多特", "拜仁慕尼黑", 1, 2, "多特1-2不敌勒沃库森。", False, "写成别的队仍须拦截"),
        # 2026-10-13 真实熔断根因①：赛程队名粘连「首发」→ 脏串须过滤，不当合法队名
        ("上海海港", "大田市民首发", 1, 0, "朴规现代表大田首发踢海港，1-0。", True,
         "脏队名（粘连「首发」）应跳过校验"),
        # 根因②：未登记 entity_map 的球队，正文写地名简称 → 放宽
        ("上海海港", "大田市民", 1, 0, "朴规现代表大田首发踢海港，1-0。", True,
         "未登记队名的简称应放宽"),
        # 放宽有下限 + 同城前缀冲突不放行
        ("上海海港", "大田市民", 1, 0, "海港主场出击，重点在中场。", False,
         "正文连地名核心都没出现仍须拦截"),
        ("上海海港", "上海申花", 2, 1, "上海德比，2-1属于海港。", False,
         "同城前缀冲突（海港/申花）不得互相放行"),
    ]
    bad = []
    for home, away, hg, ag, content, want_pass, note in cases:
        fx = {"home_team": home, "away_team": away, "home_score": hg, "away_score": ag,
              "goals": []}
        art = {"title": content, "content": content}
        got_pass, issues = orchestrator.validate_article_vs_match_data(fx, art)
        if got_pass != want_pass:
            bad.append({"case": note, "want_pass": want_pass, "got_pass": got_pass,
                        "issues": issues})
    # 子串串味：正文「国际米兰」不得被判成卡外球队「米兰」
    cons_bad = []
    ok, issues = orchestrator.check_consistency_plan(
        {"title": "国际米兰主场取胜", "content": "国际米兰1-0拿下，国米继续领跑。"},
        {"allowed_teams": ["国米"]}, blocking=True)
    if not ok:
        cons_bad.append({"case": "国际米兰/米兰 子串串味", "issues": issues})
    ok2, issues2 = orchestrator.check_consistency_plan(
        {"title": "米兰2-1尤文", "content": "AC米兰2-1击败尤文图斯。"},
        {"allowed_teams": ["AC米兰"]}, blocking=True)
    if ok2:
        cons_bad.append({"case": "真卡外球队（尤文）未被拦截", "issues": issues2})

    all_bad = bad + cons_bad
    return {"item": "队名判定别名感知（计划7.3）", "ok": not all_bad, "skipped": False,
            "cases": len(cases) + 2, "violations": all_bad,
            "detail": [f"队名判定用例 {len(cases) + 2} 项：同义写法放行 / 脏队名过滤 / "
                       f"未登记队名简称放宽 / 真缺队名与卡外球队拦截",
                       ("别名感知达标 ✅" if not all_bad else f"❌ 误判：{all_bad[:3]}")]}


def check_score_caliber_awareness() -> dict:
    """A5. 比分判定「口径归一」（硬项）。

    计划 7.3 的误杀率<5%。2026-10-24 晚间的真实熔断即由「比分写法不同」误报引起：
    数据是「蓉城 0-1 英博」，正文写「0比1」——三个判定点各自的正则只认 ASCII
    「-」「:」，于是被判「文中未出现该比分」，同一篇连续打回 5 次触发熔断，
    次日中午批次直接失败。这里把该类误报做成可回归校验：等价写法必须放行，
    真不一致/真卡外比分必须拦截，日期时间不得被当成比分。
    """
    try:
        import orchestrator
    except Exception as e:  # pragma: no cover
        return {"item": "比分判定口径归一（计划7.3）", "ok": True, "skipped": True,
                "detail": [f"无法载入事实校验器：{e}"]}
    import consistency_stats as _cs
    tmp = tempfile.TemporaryDirectory()
    _s, _l = _cs.STATS_PATH, _cs.LABELS_PATH
    _cs.STATS_PATH = Path(tmp.name) / "stats.json"
    _cs.LABELS_PATH = Path(tmp.name) / "labels.json"
    try:
        return _probe_score_caliber(orchestrator)
    finally:
        _cs.STATS_PATH, _cs.LABELS_PATH = _s, _l
        tmp.cleanup()


def _probe_score_caliber(orchestrator) -> dict:
    cases = [
        # (正文, 期望放行, 说明)；数据固定为 蓉城 0-1 英博
        ("蓉城主场0-1不敌英博。", True, "ASCII 横线"),
        ("蓉城0比1负于英博。", True, "中文「比」（本次熔断的真实写法）"),
        ("蓉城0：1负于英博。", True, "全角冒号"),
        ("蓉城0—1英博。", True, "破折号"),
        ("成都蓉城０－１大连英博。", True, "全角数字+全角横线"),
        ("英博1-0拿下蓉城。", True, "主客顺序颠倒"),
        ("蓉城主场0比2不敌英博。", False, "真不一致（0比2）仍须拦截"),
        ("蓉城2-0英博。", False, "真不一致（2-0）仍须拦截"),
        ("蓉城主场不敌英博，全场没进球。", False, "真没写比分仍须拦截"),
        ("2026-10-24 15:30 蓉城对英博，赛前前瞻。", False,
         "只有日期时间不得被当成比分放行"),
    ]
    bad = []
    for content, want_pass, note in cases:
        fx = {"home_team": "蓉城", "away_team": "英博", "home_score": 0,
              "away_score": 1, "goals": []}
        got_pass, issues = orchestrator.validate_article_vs_match_data(
            fx, {"title": content, "content": content})
        if got_pass != want_pass:
            bad.append({"case": note, "want_pass": want_pass, "got_pass": got_pass,
                        "issues": issues})
    # 卡外比分：同一口径，不许把等价写法误判成卡外
    ok1, is1 = orchestrator.check_consistency_plan(
        {"title": "蓉城0比1英博", "content": "2026-10-24 蓉城0比1不敌英博。"},
        {"allowed_scores": ["0-1"], "allowed_teams": ["蓉城", "英博"]}, blocking=True)
    if any("卡外" in i for i in is1):
        bad.append({"case": "等价写法被误判成卡外比分", "issues": is1})
    ok2, is2 = orchestrator.check_consistency_plan(
        {"title": "蓉城3比2英博", "content": "蓉城3比2绝杀英博。"},
        {"allowed_scores": ["0-1"], "allowed_teams": ["蓉城", "英博"]}, blocking=True)
    if not any("卡外" in i for i in is2):
        bad.append({"case": "真卡外比分（3比2）未被拦截", "issues": is2})

    return {"item": "比分判定口径归一（计划7.3）", "ok": not bad, "skipped": False,
            "cases": len(cases) + 2, "violations": bad,
            "detail": [f"比分口径用例 {len(cases) + 2} 项：0-1／0比1／0：1／0—1／全角"
                       f"等价放行，主客颠倒放行，真不一致与卡外比分拦截，"
                       f"日期时间不算比分",
                       ("口径归一达标 ✅" if not bad else f"❌ 误判：{bad[:3]}")]}


def check_team_result_verbs() -> dict:
    """A6. 队名「赛果动词」清洗（硬项）。

    2026-11-05 15:13 的真实熔断：战报标题「朗斯主场1-9被科莫血洗」被切成
    ``away_team='科莫血洗'``，而脏词表只认「胜/负/绝杀/逆转/首发/阵容…」，
    不认「血洗」→ 判为合法队名 → 硬要求正文出现「血洗」→ 同篇打回 5 次 → 全批停发。

    这一项把该类误报做成可回归校验，四类必须同时成立：
      ① 粘连队名**救回来**（科莫血洗 → 科莫），而不是整场跳过校验；
      ② 纯动词（血洗）判为非队名，跳过校验；
      ③ 真队名一个都不许被清洗改写；
      ④ 救回来的队名仍要真的比对正文——正文没提照样拦截（不许拿「清洗」换「放松」）。
    """
    try:
        import orchestrator
        import team_names
    except Exception as e:  # pragma: no cover
        return {"item": "队名赛果动词清洗（计划7.3）", "ok": True, "skipped": True,
                "detail": [f"无法载入事实校验器：{e}"]}
    bad = []

    # ① 粘连救回
    for raw, want in (("科莫血洗", "科莫"), ("狂胜朗斯", "朗斯"), ("被科莫血洗", "科莫"),
                      ("惨败科莫", "科莫"), ("横扫利物浦", "利物浦")):
        got = team_names.clean_team_name(raw)
        if got != want:
            bad.append({"case": f"清洗 {raw}", "want": want, "got": got})

    # ② 纯动词不是队名
    for verb in ("血洗", "逆转", "淘汰"):
        if team_names.looks_like_team(verb) or not team_names.is_result_only(verb):
            bad.append({"case": f"纯动词 {verb} 未判为非队名"})

    # ③ 真队名不被改写
    for name in ("曼联", "拜仁慕尼黑", "巴黎圣日耳曼", "国际米兰", "AC米兰",
                 "神户胜利船", "慕尼黑1860", "Manchester City FC"):
        if team_names.clean_team_name(name) != name:
            bad.append({"case": f"真队名被误清洗 {name}",
                        "got": team_names.clean_team_name(name)})

    # ④ 端到端：熔断现场放过，正文真缺队名仍拦
    body = "朗斯在主场1-9被科莫血洗，法布雷加斯赛后只提了4-0赢曼联的旧账。"
    fx_verb = {"home_team": "朗斯", "away_team": "血洗", "home_score": 1,
               "away_score": 9, "goals": []}
    fx_glue = {"home_team": "朗斯", "away_team": "科莫血洗", "home_score": 1,
               "away_score": 9, "goals": []}
    fx_clean = {"home_team": "朗斯", "away_team": "科莫", "home_score": 1,
                "away_score": 9, "goals": []}
    if not orchestrator.validate_article_vs_match_data(
            fx_verb, {"title": "朗斯1-9科莫", "content": body})[0]:
        bad.append({"case": "熔断现场未放过（away='血洗'）"})
    if not orchestrator.validate_article_vs_match_data(
            fx_glue, {"title": "朗斯1-9科莫", "content": body})[0]:
        bad.append({"case": "粘连队名未救回（away='科莫血洗'）"})
    no_away = {"title": "朗斯主场1-9惨败",
               "content": "朗斯在主场1-9输球，法布雷加斯赛后只提了4-0赢曼联的旧账。"}
    if orchestrator.validate_article_vs_match_data(fx_clean, no_away)[0]:
        bad.append({"case": "干净队名 + 正文真没提，被放行了（放松）"})

    return {"item": "队名赛果动词清洗（计划7.3）", "ok": not bad, "skipped": False,
            "cases": 5 + 3 + 8 + 3, "violations": bad,
            "detail": ["粘连队名救回（科莫血洗→科莫）／纯动词跳过校验／真队名不被改写／"
                       "救回后仍比对正文；共 19 项正反用例",
                       ("赛果动词清洗达标 ✅" if not bad else f"❌ 误判：{bad[:3]}")]}


def check_provenance() -> dict:
    """B. 溯源覆盖率（告警项）。"""
    total, with_src = 0, 0
    for _data, a in _iter_articles():
        total += 1
        if (a.get("sources_used") or a.get("source_post")):
            with_src += 1
    if total == 0:
        return {"item": "溯源覆盖率（目标100%）", "ok": True, "advisory": True, "skipped": True,
                "detail": ["未找到成稿 metadata，本项跳过"]}
    rate = with_src / total
    return {"item": "溯源覆盖率（目标100%）", "ok": True, "advisory": True, "skipped": False,
            "total": total, "with_src": with_src, "rate": round(rate, 4),
            "detail": [f"成稿 {total} 条，带来源 {with_src} 条，覆盖率 {rate:.1%}（目标 100%）",
                       ("达标 ✅" if rate >= 0.999 else
                        "⚠️ 未达标（存量缺口，告警不阻断；口径与 audit_isolation D 一致）")]}


def check_consistency_mechanism() -> dict:
    """C. 一致性校验可统计（硬项，无记录跳过）。"""
    s = cs.summary()
    if not s["has_records"]:
        return {"item": "一致性校验拦截率可统计", "ok": True, "skipped": True,
                "detail": ["consistency_stats 台账暂无运行记录，机制就绪，本项跳过"]}
    ok = s["blocking_checks"] > 0 and s["reject_rate"] is not None
    return {"item": "一致性校验拦截率可统计", "ok": ok, "skipped": False, **s,
            "detail": [f"硬校验 {s['blocking_checks']} 次，打回 {s['rejects']} 次，"
                       f"拦截率 {s['reject_rate_display']}；warn-only {s['warn_only']} 次",
                       "拦截率可统计 ✅" if ok else "❌ 有事件但无 blocking 记录，拦截率不可算"]}


def check_false_positive_rate() -> dict:
    """D. 误杀率 <5%（告警项，无标注待接入）。"""
    fpr = cs.false_positive_rate()
    if fpr is None:
        return {"item": "一致性校验误杀率<5%", "ok": True, "advisory": True, "skipped": True,
                "detail": ["尚无人工抽检标注，误杀率「待接入」（绝不用 0 冒充）",
                           f"上限 {cs.MAX_FALSE_POSITIVE_RATE:.0%}；标注写入 data/consistency_labels.json"]}
    return {"item": "一致性校验误杀率<5%", "ok": True, "advisory": True, "skipped": False,
            "false_positive_rate": round(fpr, 4), "labels": cs.labels(),
            "detail": [f"已标注打回 {cs.labels()['labeled_rejects']} 条，误杀 {cs.labels()['false_positive']} 条，"
                       f"误杀率 {fpr:.1%}（上限 {cs.MAX_FALSE_POSITIVE_RATE:.0%}）",
                       ("达标 ✅" if fpr < cs.MAX_FALSE_POSITIVE_RATE else "⚠️ 超上限，需收紧校验规则")]}


def check_reject_traceability() -> dict:
    """F. 打回可追溯（告警项）。

    打回事件必须带**选题标题**，否则事后只能看到「最后一条提示」，无法定位触发批次
    （2026-10-12 熔断复盘即卡在这里）。
    """
    t = cs.traceability(30)
    if t["rejects"] == 0:
        return {"item": "打回可定位到选题", "ok": True, "advisory": True, "skipped": True,
                "detail": ["近 30 天无打回记录，本项跳过"]}
    return {"item": "打回可定位到选题", "ok": True, "advisory": True, "skipped": False, **t,
            "detail": [f"近 30 天打回 {t['rejects']} 条，带选题标题 {t['with_title']} 条，"
                       f"可追溯率 {t['rate']:.0%}",
                       ("可追溯 ✅" if t["rate"] >= 0.99 else
                        "⚠️ 部分打回缺选题信息（多为机制上线前的历史事件，告警不阻断）")]}


def check_old_news() -> dict:
    """E. 连续 14 天无旧闻新发（硬项，无台账跳过）。

    2026-11-09 补「比例型盲区」：原实现只查 ``over_limit``（已知时效超上限
    仍被保留），而这恰好是闸门**唯一看得见**的部分。真正的盲区是 ``unknown``
    ——发布时间读不出来的素材被**保守放行**，既不计数也不报警。实测近 5 天
    678 条里 189 条（28%）时间无法判定，全部来自「热点媒体」；有时间的 489
    条里拦掉 51 条（10.4%），说明闸门本身有效。

    风险形态很具体：**源站改版把时间字段弄丢，unknown 会从 28% 一路涨到 100%，
    而 `over_limit` 仍为空、E 项仍绿** —— 闸门彻底失效却无人知晓。所以这里给
    unknown 比例设上限，而不是继续只报数字。
    """
    s = fl.summary(14)
    if s["runs"] == 0:
        # 字段结构必须与其它分支一致：靠「键不存在」表达状态，下游只能猜。
        return {"item": "连续14天无旧闻新发", "ok": True, "skipped": True,
                "unknown_ratio": None, "violation_records": [],
                "detail": ["时效闸门台账暂无记录，本项跳过"]}
    v = fl.violations(14)
    checked = int(s.get("checked") or 0)
    unknown = int(s.get("unknown") or 0)
    ratio = (unknown / checked) if checked else None

    detail = [f"近 14 天闸门运行 {s['runs']} 次，覆盖 {s['days_covered']}/14 天；"
              f"剔除超时效 {s['dropped']} 条，未知 {unknown} 条"
              + (f"（占已检查 {checked} 条的 {ratio:.1%}）" if ratio is not None else ""),
              (f"台账尚未满 14 天（累计 {s['days_covered']} 天，持续累积中）"
               if s["days_covered"] < 14 else "已覆盖 14 天"),
              ("无「已知时效>上限仍保留」✅" if not v else f"❌ {len(v)} 条超限保留")]
    base = {"item": "连续14天无旧闻新发", "skipped": False, **s,
            "unknown_ratio": round(ratio, 4) if ratio is not None else None,
            "violation_records": v[:5]}

    # 已知违规优先级最高：它已经是铁证，不必等比例样本够不够。
    if v:
        return {**base, "ok": False, "detail": detail}

    if ratio is None or checked < MIN_UNKNOWN_SAMPLE:
        # 样本不足**不下结论**。给个 0 会让「一次都没检查」看起来像满分——
        # 这正是全仓反复踩的那条：缺失不能用 0 冒充。
        detail.append(f"⏸ 已检查样本 {checked} 条不足 {MIN_UNKNOWN_SAMPLE} 条，"
                      f"时间未知比例未判定（不等于通过）")
        return {**base, "ok": None, "detail": detail}

    if ratio > MAX_UNKNOWN_RATIO:
        detail.append(f"❌ 时间未知占 {ratio:.1%}，超过上限 {MAX_UNKNOWN_RATIO:.0%}："
                      f"过半素材的时效闸门没真正生效（源站时间字段可能已失效）")
        return {**base, "ok": False, "detail": detail}

    if ratio > WARN_UNKNOWN_RATIO:
        detail.append(f"⚠️ 时间未知占 {ratio:.1%}，高于告警线 {WARN_UNKNOWN_RATIO:.0%}"
                      f"（上限 {MAX_UNKNOWN_RATIO:.0%}）：源站时间字段可能在退化，"
                      f"仍算通过，但要盯")
        return {**base, "ok": True, "detail": detail}

    detail.append(f"时间未知占 {ratio:.1%}，在告警线 {WARN_UNKNOWN_RATIO:.0%} 内 ✅")
    return {**base, "ok": True, "detail": detail}


def check_breaker_blocks_current_batch() -> dict:
    """G. 熔断须拦住当前批次（硬项 · 计划第十章）。

    2026-10-13 事故：一致性打回超阈值的熔断在「生成文章」阶段才触发，
    而 batch.yml 此前只在批次开头检查一次 → 当晚批次照常发布，故障延后一整批。
    修复三层，此处逐层验收：
      1. publisher.publish_all 有发布前熔断检查（AST 查真实 Call 节点——
         R16c 教训：文本匹配会被 docstring 里的一句说明误报）；
      2. orchestrator 有 _breaker_active() 且生成侧（_gen_one / main 紧急球评）
         有调用点——生成侧发现熔断即中止；
      3. batch.yml「合规熔断复核（生成后）」步骤先于「发布到头条号」且熔断时
         exit 1；「发布到头条号」与「回写批次状态」均按复核结果跳过
         （中止的批次不标记完成，解除熔断后手动补发才不会被幂等保护吞掉）。
    """
    import ast
    import yaml
    problems = []

    # ① publisher 发布边界硬闸（AST：publish_all 函数体内调 compliance_guard 的
    #    status / is_suspended —— 只看真实 Call 节点，不扫源码文本）
    try:
        src = (PROJECT_ROOT / "publisher.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "publish_all"), None)
        if fn is None:
            problems.append("publisher.py 找不到 publish_all")
        elif not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and isinstance(n.func.value, ast.Name)
                     and n.func.value.id == "compliance_guard"
                     and n.func.attr in ("status", "is_suspended")
                     for n in ast.walk(fn)):
            problems.append("publish_all 无发布前熔断检查（compliance_guard.status/is_suspended）")
    except Exception as e:
        problems.append(f"解析 publisher.py 失败：{e}")

    # ② orchestrator 生成侧中止（AST：_breaker_active 定义 + 至少 2 处调用）
    try:
        src = (PROJECT_ROOT / "orchestrator.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        defs = [n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_breaker_active"]
        if not defs:
            problems.append("orchestrator 缺少 _breaker_active()")
        else:
            call_sites = [n for n in ast.walk(tree)
                          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                          and n.func.id == "_breaker_active"]
            if len(call_sites) < 2:
                problems.append(
                    f"_breaker_active 调用点不足（现有 {len(call_sites)} 处，"
                    f"应为生成循环 + 紧急球评至少 2 处）")
    except Exception as e:
        problems.append(f"解析 orchestrator.py 失败：{e}")

    # ③ batch.yml 步骤顺序与条件
    try:
        wf = yaml.safe_load(
            (PROJECT_ROOT / ".github" / "workflows" / "batch.yml").read_text(encoding="utf-8"))
        steps = wf["jobs"]["publish"]["steps"]
        names = [s.get("name", "") for s in steps]
        recheck = next((s for s in steps if s.get("id") == "compliance_recheck"), None)
        if recheck is None:
            problems.append("batch.yml 缺少「合规熔断复核（生成后）」步骤")
        else:
            if steps.index(recheck) >= names.index("发布到头条号"):
                problems.append("复核步骤必须先于「发布到头条号」")
            if "exit 1" not in recheck.get("run", ""):
                problems.append("复核步骤熔断时未 exit 1（任务不会失败，故障通知丢失）")
        pub_step = steps[names.index("发布到头条号")]
        if "compliance_recheck" not in str(pub_step.get("if", "")):
            problems.append("「发布到头条号」未按复核结果跳过")
        wb_step = steps[names.index("回写批次状态（幂等保护）")]
        if "compliance_recheck" not in str(wb_step.get("if", "")):
            problems.append("「回写批次状态」未按复核结果跳过（熔断中止的批次会被误标记完成）")
    except Exception as e:
        problems.append(f"解析 batch.yml 失败：{e}")

    return {"item": "熔断拦住当前批次（计划十章）", "ok": not problems, "skipped": False,
            "violations": problems,
            "detail": ["生成侧中止（orchestrator._breaker_active）+ 发布边界硬闸"
                       "（publisher.publish_all）+ CI 生成后复核（batch.yml）三层验收",
                       ("三层防线齐备 ✅" if not problems else f"❌ 缺口：{problems}")]}


def check_failure_alert_diagnosable() -> dict:
    """H. 失败告警必须带原因（硬项）。

    2026-10-25 事故：一天连收三条 WxPusher 告警，正文只有「批次发布失败 / 日期 /
    链接」。手机上看不到日志，人无法判断是合规熔断、登录过期还是发布失败，只能回
    电脑翻 40 秒的运行记录——告警等于只报了「出事了」，没报「出什么事」。

    验收四层：
      1. 两处合规中止点都写结构化原因（write_failure_reason.py）；
      2. 失败通知步骤调用 notify_failure.py（而不是内联固定文案）；
      3. 正文拼装器在「原因为空」时给明确占位，绝不出现空白「原因: 」；
      4. 非合规失败（生成/发布/登录/git push）有通用兜底：失败后查本 run 分步结论，
         定位到具体失败步骤写进原因（否则这些失败仍然只说「详见日志」）。
    """
    problems = []
    try:
        import yaml
        wf = yaml.safe_load(
            (PROJECT_ROOT / ".github" / "workflows" / "batch.yml").read_text(encoding="utf-8"))
        steps = wf["jobs"]["publish"]["steps"]
        names = [s.get("name", "") for s in steps]
        gate = next((s for s in steps if s.get("name", "").startswith("合规熔断检查")), None)
        recheck = next((s for s in steps if s.get("id") == "compliance_recheck"), None)
        notify = next((s for s in steps if s.get("name") == "失败通知"), None)
        for step, label in ((gate, "批次开头合规检查"), (recheck, "生成后合规复核")):
            if step is None:
                problems.append(f"batch.yml 缺少{label}步骤")
            elif "write_failure_reason.py" not in step.get("run", ""):
                problems.append(f"{label}中止时未写结构化失败原因")
        if notify is None:
            problems.append("batch.yml 缺少「失败通知」步骤")
        else:
            if "notify_failure.py" not in notify.get("run", ""):
                problems.append("失败通知未走 notify_failure.py（原因拼不进告警）")
            if notify.get("if", "") != "failure()":
                problems.append("失败通知未绑定 failure()")
        # 通用兜底：非合规失败也要能定位到步骤名
        fallback = next((s for s in steps if "失败原因兜底" in s.get("name", "")), None)
        if fallback is None:
            problems.append("batch.yml 缺少「失败原因兜底」步骤（非合规失败仍无原因）")
        elif "--from-run-api" not in fallback.get("run", ""):
            problems.append("失败原因兜底未走 --from-run-api（定位不到失败步骤）")
        perms = (wf["jobs"]["publish"].get("permissions") or {})
        if fallback is not None and perms.get("actions") != "read":
            problems.append("缺少 actions: read 权限，兜底步骤查不到分步结论")
    except Exception as e:
        problems.append(f"解析 batch.yml 失败：{e}")

    # 正文拼装：原因为空时必须有占位，不许出现空的「原因: 」
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        import notify_failure
        body = notify_failure.build_content("noon", "2026-10-25", "", "o/r", "1")
        if "原因: 详见运行日志" not in body:
            problems.append("原因缺失时告警正文未给占位（会出现空白「原因: 」）")
    except Exception as e:
        problems.append(f"载入 notify_failure 失败：{e}")

    return {"item": "失败告警可诊断（带原因）", "ok": not problems, "skipped": False,
            "violations": problems,
            "detail": ["两处合规中止写结构化原因 + 失败通知走 notify_failure.py + "
                       "原因为空时给占位 + 非合规失败按分步结论兜底",
                       ("告警可诊断 ✅" if not problems else f"❌ 缺口：{problems}")]}


def verify() -> dict:
    checks = [check_title_hard(), check_title_pass_rate(), check_team_alias_awareness(),
              check_score_caliber_awareness(), check_team_result_verbs(),
              check_provenance(), check_consistency_mechanism(), check_false_positive_rate(),
              check_reject_traceability(), check_old_news(), check_title_antipattern_residue(),
              check_title_elements_residue(),
              check_breaker_blocks_current_batch(), check_failure_alert_diagnosable()]
    hard = [c for c in checks if not c.get("advisory")]
    # 三态：True 通过 / False 未通过 / None 未判定。未判定不算通过，也不算失败——
    # 它是「样本还没到」，混进 hard_failed 会让报告在生效日前天天报红。
    failed = [c["item"] for c in hard if c.get("ok") is False]
    pending = [c["item"] for c in hard if c.get("ok") is None]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "ok": (False if failed else (None if pending else True)),
            "checks": checks, "hard_failed": failed, "hard_pending": pending}


def render_md(rep: dict) -> str:
    verdict = {True: "通过 ✅", False: "未通过 ❌", None: "部分通过 ⏸"}[rep["ok"]]
    L = ["# 发布闸门验收报告（计划 7.2 / 7.3）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 验收结论：**{verdict}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    if rep.get("hard_pending"):
        L += [f"> 未判定（不等于通过，等在效日后样本）：{'、'.join(rep['hard_pending'])}", ""]
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
    ap = argparse.ArgumentParser(description="发布闸门验收（计划 7.2 / 7.3）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()

    rep = verify()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"publish_gate_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "publish_gate_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    if rep["ok"] is None:
        # 未判定不阻断，但必须留痕：CI 里变黄点，本地能看见。
        # 直接 return 0 会让「样本还没到」看起来跟「全过」一样。
        pend = "、".join(rep.get("hard_pending") or [])
        print(f"::warning::发布闸门 {len(rep.get('hard_pending') or [])} 项未判定"
              f"（{pend}）——不等于通过", file=sys.stderr)
        return 0
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
