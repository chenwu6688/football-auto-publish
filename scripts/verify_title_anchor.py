#!/usr/bin/env python3
"""标题锚点验收（P0-1，2026-11-01 头部账号对标）。

病根与口径全部来自 ``docs/benchmark/2026-11-01-头部账号对标与综合改进计划.md``：

    账号            粉丝    中位阅读  过千率  锚点覆盖  疑问句
    英超那些事     15.0万    5,526    100%   35.7%     0%
    我爱英超       10.3万    1,090     56%   13.1%    20.2%
    西甲二人转     23.0万       77     10%    7.2%     8.4%
    我方（~0 粉）      —          8      0%    2.6%    85.5%

本脚本把「标题改没改」变成可验收的硬项：

  A. 词表与自检（硬项）—— 锚点词表非空、title_features/title_gate 自检全过、
     配置声明的类别与代码里的类别**一一对应**（防两处口径漂移）
  B. 事实层能提供锚点（硬项）—— 锚点不能靠模型编。事实卡必须能给出「第几分钟」，
     否则生成侧只能写空泛标题。用合成战报 topic 跑纯函数验证，不需要网络
  C. 锚点覆盖率（硬项）—— 生效日后近 7 天成稿锚点覆盖率 ≥40%；样本 <5 篇时跳过
     （改造首日没有足够样本，拿它判＝必然假警）
  D. 疑问收尾限额（硬项）—— 生效日后 14 天窗口内疑问收尾 ≤2 篇
  E. 已接入生成侧（硬项）—— 两个评分器都真的调用 title_gate、存在「只重写标题」
     的 repick_title、且 prompt 里已经不再硬性要求「以疑问收尾」。
     只看代码里有没有 ≠ 真的生效，10-25 那次「预测稿靠字段缺失挡发布」就是教训

生效日 ``TITLE_SINCE``：拿改造前的稿判新规则等于永远 FAIL，闸门就成了噪音
（同 content_mix.MIX_SINCE 的处理）。此日之前的成稿只作基线展示，不进判定。

产出 ``data/audit/title_anchor_<date>.md`` + ``title_anchor_latest.json``；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_title_anchor.py [--days 7] [--quiet]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import title_features  # noqa: E402
import title_gate  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"
CST = timezone(timedelta(hours=8))

# 改造生效日：从这一天起生成的成稿才按新口径验收（此前的稿是旧公式产物）。
TITLE_SINCE = "2026-11-02"

# 目标锚点覆盖率。对标对象是英超那些事（35.7%），取 40% 略高——因为它的样本只有 14 条，
# 且我方起点是 2.6%，需要一个有压力的目标而不是「差不多就行」。
TARGET_ANCHOR_COVERAGE = 40.0
# 疑问收尾限额（计划 P0-1 第 3 条原文：任意 14 天内 ≤2 篇）
MAX_QUESTION_PER_WINDOW = 2
# 样本下限：少于这个数就不下结论（避免「改造首日 1 篇没锚点 → FAIL」）
MIN_SAMPLE = 5


def _today() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _window(days: int):
    """返回 (since, until)；若生效日还没到（since > today）返回 None。

    直接拿 max(生效日, 窗口起点) 会在生效日尚未到时算出一个**倒置窗口**
    （2026-11-02 ~ 2026-11-01），报告里读起来像 bug，判定也必然空转。
    """
    today = datetime.now(CST).date()
    start = today - timedelta(days=days - 1)
    since = max(datetime.strptime(TITLE_SINCE, "%Y-%m-%d").date(), start)
    if since > today:
        return None
    return since.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def check_wordlist() -> dict:
    """A. 词表非空、自检全过、配置与代码口径一致。"""
    detail, ok = [], True

    n_kinds = len(title_features.PROCESS_ANCHOR_PATTERNS)
    if n_kinds < 10:
        ok = False
        detail.append(f"❌ 锚点类别只有 {n_kinds} 类（应 ≥10；头部靠过程反常制造点开理由）")
    else:
        detail.append(f"✅ 锚点类别 {n_kinds} 类")

    for name, cases in (("title_features", title_features.selftest()),
                        ("title_gate", title_gate.selftest())):
        bad = [n for n, good in cases if not good]
        if bad:
            ok = False
            detail.append(f"❌ {name} 自检 {len(bad)} 项未过：" + "；".join(bad[:3]))
        else:
            detail.append(f"✅ {name} 自检 {len(cases)} 项全过")

    declared = set(title_gate.anchors_declared())
    available = set(title_gate.available_anchor_kinds())
    missing = available - declared
    extra = declared - available
    if missing:
        ok = False
        detail.append(f"❌ 配置未声明但代码里有的类别：{'、'.join(sorted(missing))}")
    if extra:
        ok = False
        detail.append(f"❌ 配置声明了但代码里没有的类别（已失效）：{'、'.join(sorted(extra))}")
    if not missing and not extra:
        detail.append(f"✅ config 与代码类别一一对应（{len(declared)} 类）")

    # 负向验证：词表清空后「锚点可识别」必须失败——否则说明判定走的是别的路径
    _orig = title_features.PROCESS_ANCHOR_PATTERNS
    title_features.PROCESS_ANCHOR_PATTERNS = ()
    try:
        emptied = title_gate.has_anchor("第94分钟丢球遭读秒绝杀")
    finally:
        title_features.PROCESS_ANCHOR_PATTERNS = _orig
    if emptied:
        ok = False
        detail.append("❌ 负向验证失败：清空词表后仍判为「有锚点」，判定不可信")
    else:
        detail.append("✅ 负向验证：清空词表 → 锚点判定失败（说明判定真的在读词表）")

    return {"item": "锚点词表与自检", "ok": ok, "detail": detail}


def check_fact_layer() -> dict:
    """B. 锚点必须能从事实层取到（不许模型编）。"""
    detail, ok = [], True
    try:
        import orchestrator
    except Exception as e:
        return {"item": "事实层可提供锚点", "ok": False,
                "detail": [f"❌ orchestrator 导入失败：{e}"]}

    topic = {
        "competition": "英超", "home_zh": "阿森纳", "away_zh": "曼城",
        "kickoff_cst": "2026-11-01 23:30",
        "score": {"home": 1, "away": 2, "ht_home": 1, "ht_away": 0},
        # 字段口径照抄 football-data 解析后的真实结构（scorer_zh / has_zh_name / type），
        # 不是随手起的名字——否则这条检查测的是我想象中的事实卡。
        "goals": [
            {"minute": 12, "scorer_zh": "萨卡", "team_zh": "阿森纳",
             "has_zh_name": True, "type": "REGULAR"},
            {"minute": 78, "scorer_zh": "哈兰德", "team_zh": "曼城",
             "has_zh_name": True, "type": "REGULAR"},
            {"minute": 90, "injury_time": 4, "scorer_zh": "福登", "team_zh": "曼城",
             "has_zh_name": True, "type": "PENALTY"},
        ],
        "winner_zh": "曼城", "goal_diff": 1,
    }
    try:
        card = orchestrator._build_recap_fact_card(topic)
    except Exception as e:
        return {"item": "事实层可提供锚点", "ok": False,
                "detail": [f"❌ 生成战报事实卡异常：{e}"]}

    if "分钟" not in card and "12'" not in card:
        ok = False
        detail.append("❌ 战报事实卡里没有任何「分钟」信息——生成侧想写锚点也无据可写")
    else:
        detail.append("✅ 战报事实卡含进球时间（可支撑「第X分钟」类锚点）")
    missing = [g["scorer_zh"] for g in topic["goals"] if g["scorer_zh"] not in card]
    if missing:
        ok = False
        detail.append(f"❌ 事实卡缺进球者：{'、'.join(missing)}")
    else:
        detail.append("✅ 进球者姓名全在卡里（锚点可点名，不必编）")
    if "90+4" not in card:
        ok = False
        detail.append("❌ 补时进球未标出「90+4」——「补时/读秒」类锚点会写不准确")
    else:
        detail.append("✅ 补时进球按「90+4」标出（补时/读秒锚点有据）")

    return {"item": "事实层可提供锚点", "ok": ok, "detail": detail}


def check_anchor_coverage(days: int) -> dict:
    """C. 生效日后近 N 天成稿的锚点覆盖率。"""
    win = _window(days)
    if win is None:
        return {"item": "锚点覆盖率", "ok": True, "advisory": True,
                "detail": [f"⚠️ 生效日 {TITLE_SINCE} 尚未到，暂无生效后样本；"
                           f"目标 {TARGET_ANCHOR_COVERAGE:.0f}%"]}
    since, until = win
    try:
        titles = title_gate.recent_titles(OUTPUT_DIR, until, days=days, since=TITLE_SINCE)
    except Exception as e:
        return {"item": "锚点覆盖率", "ok": True, "advisory": True,
                "detail": [f"⚠️ 读取成稿标题失败（{e}），跳过判定"]}
    cov = title_gate.coverage(titles)
    head = f"窗口 {since} ~ {until}（生效日 {TITLE_SINCE} 起）"
    if cov["n"] < MIN_SAMPLE:
        return {"item": "锚点覆盖率", "ok": True, "advisory": True,
                "detail": [f"⚠️ {head}：生效后样本仅 {cov['n']} 篇（<{MIN_SAMPLE}），"
                           f"暂不下结论；目标 {TARGET_ANCHOR_COVERAGE:.0f}%"]}
    pct = cov["anchor_coverage"]
    detail = [f"样本 {cov['n']} 篇，锚点覆盖 {cov['anchors']}/{cov['n']} = {pct}%"
              f"（目标 ≥{TARGET_ANCHOR_COVERAGE:.0f}%，对标英超那些事 35.7%）"]
    if cov["anchor_kinds"]:
        top = sorted(cov["anchor_kinds"].items(), key=lambda kv: -kv[1])
        detail.append("命中类别：" + "、".join(f"{k}×{v}" for k, v in top))
    ok = pct >= TARGET_ANCHOR_COVERAGE
    detail.insert(0, "✅" if ok else "❌")
    return {"item": "锚点覆盖率", "ok": ok, "detail": detail,
            "value": pct, "target": TARGET_ANCHOR_COVERAGE}


def check_question_quota() -> dict:
    """D. 生效日后 14 天窗口内疑问收尾 ≤2 篇。"""
    win = _window(title_gate.WINDOW_DAYS)
    if win is None:
        return {"item": "疑问收尾限额", "ok": True, "advisory": True,
                "detail": [f"⚠️ 生效日 {TITLE_SINCE} 尚未到，暂无生效后样本；"
                           f"限额 {MAX_QUESTION_PER_WINDOW} 篇"]}
    since, until = win
    try:
        titles = title_gate.recent_titles(OUTPUT_DIR, until,
                                         days=title_gate.WINDOW_DAYS, since=TITLE_SINCE)
    except Exception as e:
        return {"item": "疑问收尾限额", "ok": True, "advisory": True,
                "detail": [f"⚠️ 读取成稿标题失败（{e}），跳过判定"]}
    if not titles:
        return {"item": "疑问收尾限额", "ok": True, "advisory": True,
                "detail": [f"⚠️ 窗口 {since} ~ {until}：生效后无成稿，跳过"]}
    used = title_gate.count_question_endings(titles)
    ok = used <= MAX_QUESTION_PER_WINDOW
    detail = [f"窗口 {since} ~ {until}：样本 {len(titles)} 篇，"
              f"疑问收尾 {used} 篇（限额 {MAX_QUESTION_PER_WINDOW} 篇）"]
    if used:
        qs = [t for t in titles if title_gate.is_question_ending(t)][:3]
        detail += [f"  · {t[:40]}" for t in qs]
    detail.insert(0, "✅" if ok else "❌")
    return {"item": "疑问收尾限额", "ok": ok, "detail": detail,
            "value": used, "limit": MAX_QUESTION_PER_WINDOW}


def check_wired() -> dict:
    """E. 生成侧真的接了（不是只多了一个模块）。"""
    detail, ok = [], True
    if not ORCHESTRATOR.exists():
        return {"item": "已接入生成侧", "ok": False,
                "detail": ["❌ 找不到 orchestrator.py"]}
    text = ORCHESTRATOR.read_text(encoding="utf-8")

    if "import title_gate" not in text:
        ok = False
        detail.append("❌ orchestrator 未 import title_gate")
    else:
        detail.append("✅ orchestrator 已 import title_gate")

    n_calls = text.count("title_gate.check(")
    if n_calls < 2:
        ok = False
        detail.append(f"❌ title_gate.check 只被调用 {n_calls} 处"
                      f"（score_title_plan 与 score_fixture_title 两处都要）")
    else:
        detail.append(f"✅ title_gate.check 在 {n_calls} 个评分器里生效")

    if "def repick_title" not in text:
        ok = False
        detail.append("❌ 缺 repick_title：标题不过只能重跑全文，白白浪费额度并重新掷骰子")
    else:
        detail.append("✅ 存在 repick_title（只重写标题，正文不动）")

    # 供给侧是否还硬性要求疑问收尾（这是 85.5% 的根源，必须已铲掉）
    leftovers = [ln.strip() for ln in text.splitlines()
                 if "以疑问『？』收尾" in ln or "必须以疑问？收尾" in ln]
    if leftovers:
        ok = False
        detail.append("❌ prompt 仍在硬性要求「以疑问收尾」（这正是 85.5% 空问标题的根源）：")
        detail += [f"   - {s[:70]}" for s in leftovers[:3]]
    else:
        detail.append("✅ prompt 已不再硬性要求「以疑问收尾」")

    return {"item": "已接入生成侧", "ok": ok, "detail": detail}


def verify(days: int = 7) -> dict:
    checks = [check_wordlist(), check_fact_layer(), check_anchor_coverage(days),
              check_question_quota(), check_wired()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "window_days": days, "since": TITLE_SINCE,
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 标题锚点验收（P0-1）", "",
         f"- 生成时间：{rep['generated_at']}｜窗口 {rep['window_days']} 天｜生效日 {rep['since']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**",
         "- 口径出处：docs/benchmark/2026-11-01-头部账号对标与综合改进计划.md（P0-1）", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}：{'✅' if c['ok'] else '❌'}"
                 + ("（跳过/告警）" if c.get("advisory") else ""))
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="标题锚点验收（P0-1）")
    ap.add_argument("--days", type=int, default=7, help="锚点覆盖率统计窗口（默认 7）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()
    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"title_anchor_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "title_anchor_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
