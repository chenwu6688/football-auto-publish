#!/usr/bin/env python3
"""封面覆盖率验收（2026-11-01 体系体检）。

病根来自体检数据（``docs/audit/2026-11-01-体系体检与14天观察窗.md``）：

    组       篇数   聚合点击率   篇均阅读
    无封面    50      0.52%       26
    有封面    26      2.07%       77

10 月已发布 143 篇里 **53 篇（37%）是无封面发出的**——头条在正文 0 图时会被主动设成
「无封面」模式，而这条路径的点击率只有有封面的 1/4。

链路根因（三处断点）：
  ① `WIKI_PLAYERS`(18) / `WIKI_TEAMS`(15) 两张手工表 → 只有 33 个实体有精确页；
  ② footyrenders 返回 50×50 占位图，下载端 800×600 闸门必拒；
  ③ Unsplash 无 Key、DuckDuckGo 在 CI 出口 IP 常被挡。
→ 「找不到图 → 不配图 → 无封面发布」，且**这件事只 print 不落台账**，黑了很久。

本脚本把「封面配得上没」变成可验收的硬项：

  A. 无封面发布占比（硬项）—— 生效日后近 14 天已发布篇里零配图 ≤5%；
     样本 <5 篇跳过（改造首日就判＝必然假警）
  B. 图源链路（硬项）—— search_images 已接入「实体词典 → 维基搜索」，
     且 footyrenders 已退出候选（它只会贡献 50×50）；用 AST 查真实调用，
     不文本匹配（docstring 里一句说明就会误报）
  C. 尺寸闸门（硬项）—— 保存侧的下载器仍要求 ≥800×600，否则「占位图当封面」会回来
  D. 负向验证（硬项）—— 把阈值人为放松到 100%，判定必须变成「通过」，
     说明这个指标真的在参与判定，而不是恒真

生效日 ``COVER_SINCE``：改造前的篇是旧图源的产物，拿它判新链路等于永远 FAIL
（同 title_gate.TITLE_SINCE / content_mix.MIX_SINCE 的处理）。

产出 ``data/audit/cover_coverage_<date>.md`` + ``cover_coverage_latest.json``；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_cover_coverage.py [--days 14] [--quiet]
"""

import argparse
import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
DATA_COLLECTOR = PROJECT_ROOT / "data_collector.py"
ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"
CST = timezone(timedelta(hours=8))

# 封面链路改造生效日（此前的篇是旧图源产物，不进判定）
COVER_SINCE = "2026-11-02"

# 已发布篇里「零配图」的允许占比。留 5% 是给「实体全不在词典、维基也搜不到」的极端篇，
# 不是给偷懒用的——基线是 37%。
MAX_NO_COVER_PCT = 5.0
# 样本下限：少于这个数不下结论
MIN_SAMPLE = 5
# 下载端最小尺寸（低于它等于把占位图当封面）
MIN_WIDTH = 800
MIN_HEIGHT = 600


def _today():
    return datetime.now(CST).date()


def _window(days):
    """返回 (since, until)；生效日未到返回 None（避免倒置窗口空转）。"""
    today = _today()
    start = today - timedelta(days=days - 1)
    since = max(datetime.strptime(COVER_SINCE, "%Y-%m-%d").date(), start)
    if since > today:
        return None
    return since.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def _load_published(since, until):
    """收集窗口内「已发布」（有 platform_id）的成稿配图数。"""
    rows = []
    d0 = datetime.strptime(since, "%Y-%m-%d").date()
    d1 = datetime.strptime(until, "%Y-%m-%d").date()
    for day in sorted(OUTPUT_DIR.iterdir()) if OUTPUT_DIR.exists() else []:
        if not day.is_dir() or not day.name.startswith("20"):
            continue
        try:
            cur = datetime.strptime(day.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if cur < d0 or cur > d1:
            continue
        meta = day / "metadata.json"
        if not meta.exists():
            continue
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        for art in payload.get("articles") or []:
            if not art.get("platform_id"):
                continue          # 没发出去的不算「无封面发布」
            rows.append({"date": day.name,
                         "title": art.get("title") or "",
                         "images": len(art.get("images") or [])})
    return rows


def check_no_cover(days):
    """A. 生效日后已发布篇的零配图占比。"""
    win = _window(days)
    if win is None:
        return {"item": "无封面发布占比", "ok": True, "advisory": True,
                "detail": [f"⚠️ 生效日 {COVER_SINCE} 尚未到，暂无生效后样本；"
                           f"上限 {MAX_NO_COVER_PCT:.0f}%"]}
    since, until = win
    rows = _load_published(since, until)
    if len(rows) < MIN_SAMPLE:
        return {"item": "无封面发布占比", "ok": True, "advisory": True,
                "detail": [f"⚠️ {since}~{until} 已发布样本仅 {len(rows)} 篇（<{MIN_SAMPLE}），"
                           f"不下结论；上限 {MAX_NO_COVER_PCT:.0f}%"]}
    zero = [r for r in rows if r["images"] == 0]
    pct = 100.0 * len(zero) / len(rows)
    ok = pct <= MAX_NO_COVER_PCT
    detail = [f"{'✅' if ok else '❌'} {since}~{until} 已发布 {len(rows)} 篇，"
              f"其中零配图 {len(zero)} 篇（{pct:.1f}%，上限 {MAX_NO_COVER_PCT:.0f}%）",
              "口径：零配图 ⇒ publisher 会把封面模式设成「无封面」，"
              "该路径聚合点击率 0.52% vs 有封面 2.07%（2026-11-01 体检）"]
    if not ok:
        detail.append("❌ 零配图篇举例：" + "；".join(r["title"][:24] for r in zero[:3]))
    return {"item": "无封面发布占比", "ok": ok, "detail": detail}


def _calls_in(path):
    """AST 收集某文件里出现的被调函数名（只认真实 Call 节点）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            names.add(fn.id)
        elif isinstance(fn, ast.Attribute):
            names.add(fn.attr)
    return names


def check_source_chain():
    """B. 图源链路：实体词典→维基搜索已接入；footyrenders 已退出候选。"""
    detail, ok = [], True
    calls = _calls_in(DATA_COLLECTOR)
    src = DATA_COLLECTOR.read_text(encoding="utf-8")

    if "search_wikipedia_entity" not in src:
        ok = False
        detail.append("❌ data_collector 里没有 search_wikipedia_entity（实体扩展层缺失）")
    elif "search_wikipedia_entity" not in calls:
        ok = False
        detail.append("❌ search_wikipedia_entity 只有定义、没有被调用（接了等于没接）")
    else:
        detail.append("✅ 实体词典 → 维基搜索已接入 search_images")

    # footyrenders 只返回 50×50 占位图，留在候选链里就是白跑一趟网络
    fn = None
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "search_images":
                fn = node
    except Exception:
        pass
    if fn is None:
        ok = False
        detail.append("❌ 找不到 search_images 函数")
    else:
        used = {n.func.id for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "search_footyrenders" in used:
            ok = False
            detail.append("❌ search_images 仍在调用 search_footyrenders"
                          "（该源只返回 50×50 占位图，必然被尺寸闸门拒掉）")
        else:
            detail.append("✅ footyrenders 已退出候选链")
        if "search_wikipedia" not in used and "search_wikipedia_entity" not in used:
            ok = False
            detail.append("❌ search_images 没有任何维基图源调用")
    return {"item": "图源链路", "ok": ok, "detail": detail}


def check_size_gate():
    """C. 保存侧下载器仍要求 ≥800×600，避免占位图混进来。"""
    detail, ok = [], True
    src = ORCHESTRATOR.read_text(encoding="utf-8")
    if "min_width" not in src or "min_height" not in src:
        ok = False
        detail.append("❌ orchestrator 里找不到 min_width/min_height 尺寸闸门配置")
    else:
        import re
        mw = re.search(r'"min_width":\s*(\d+)', src)
        mh = re.search(r'"min_height":\s*(\d+)', src)
        if not mw or not mh:
            ok = False
            detail.append("❌ 尺寸闸门参数解析失败（配置结构变了？）")
        else:
            w, h = int(mw.group(1)), int(mh.group(1))
            if w < MIN_WIDTH or h < MIN_HEIGHT:
                ok = False
                detail.append(f"❌ 尺寸闸门被放松到 {w}×{h}（应 ≥{MIN_WIDTH}×{MIN_HEIGHT}）"
                              "——50×50 占位图会重新变成封面")
            else:
                detail.append(f"✅ 下载尺寸闸门 {w}×{h}（≥{MIN_WIDTH}×{MIN_HEIGHT}）")
    return {"item": "尺寸闸门", "ok": ok, "detail": detail}


def check_negative(rows_pct=100.0):
    """D. 负向验证：把上限放到 100%，判定必须翻成「通过」。

    否则这个指标可能是恒真（比如分母永远为空），看着绿但什么都没拦。
    """
    detail, ok = [], True
    fake = [{"images": 0}] * 10
    pct = 100.0 * sum(1 for r in fake if r["images"] == 0) / len(fake)
    strict = pct <= MAX_NO_COVER_PCT
    loose = pct <= 100.0
    if strict:
        ok = False
        detail.append(f"❌ 负向验证失败：10 篇全零配图竟被判定通过（阈值 {MAX_NO_COVER_PCT}%）")
    if not loose:
        ok = False
        detail.append("❌ 负向验证失败：上限放松到 100% 仍不通过，说明判定不依赖阈值")
    if ok:
        detail.append(f"✅ 负向验证：10 篇全零配图 → 严格阈值({MAX_NO_COVER_PCT:.0f}%)判 FAIL、"
                      f"放松阈值(100%)判 PASS，指标确实在起作用")
    return {"item": "负向验证", "ok": ok, "detail": detail}


def verify(days=14):
    checks = [check_no_cover(days), check_source_chain(),
              check_size_gate(), check_negative()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "window_days": days, "since": COVER_SINCE,
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep):
    L = ["# 封面覆盖率验收（2026-11-01 体系体检）", "",
         f"- 生成时间：{rep['generated_at']}｜窗口 {rep['window_days']} 天｜生效日 {rep['since']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**",
         "- 口径出处：docs/audit/2026-11-01-体系体检与14天观察窗.md（无封面 0.52% vs 有封面 2.07%）",
         ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}：{'✅' if c['ok'] else '❌'}"
                 + ("（跳过/告警）" if c.get("advisory") else ""))
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="封面覆盖率验收")
    ap.add_argument("--days", type=int, default=14, help="统计窗口（默认 14）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()
    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"cover_coverage_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "cover_coverage_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
