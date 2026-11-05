#!/usr/bin/env python3
"""配图相关性验收（2026-11-05 用户指令「不能偏离文章主题」）。

背景：用户要求每篇至少 3 张相关配图。真实图源有两条路——爬新闻站（版权灰线）
和免费图库（足球题材结构性为空）。本方案改走第三条：把正文已经写出来的事实
（比分、积分、名次）重绘成图，零版权，且相关性由数据源本身保证。

但「重绘」有两个必然的翻车点，任何一条失守都会静默配出离谱的图：

  翻车点 1：图画了正文没提的东西（拿当天全部比赛硬配，文章讲引援却配一场球）
  翻车点 2：闸门写好了但没接上（本项目有过「模块存在、调用缺失」的前科）

所以本脚本盯五条：

  硬项 A. 接线——保存层真的调用 data_chart.candidates 与 image_relevance.filter_candidates
  硬项 B. 闸门行为——负向自检：缺实体/缺数字/串味（国际米兰 vs AC米兰）都必须拒
  硬项 C. 不画假数据——无进球事件、无比分、无积分时必须返回 None
  硬项 D. 被拒的图不留在磁盘——否则 file_writer 扫目录仍会把它算成已配图
  硬项 E. 真实产物闭环——生效日后的批次，拿产物里的 anchor 配产物正文重跑闸门，
          必须仍然通过（证明图确实来自这篇文章，不是事后编的 anchor）

  告警 F. 拦截留痕——「配图相关性」降级出现几次，每次是否带原因

三态：没有可比对的产物 → 「未判定」，退出码 2（未判定 ≠ 通过）。

用法：python3 scripts/verify_image_relevance.py [--quiet] [--days 7] [--selftest]
产出：data/audit/image_relevance_<date>.md + data/audit/image_relevance_latest.json
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
ORCH = PROJECT_ROOT / "orchestrator.py"
CST = timezone(timedelta(hours=8))

import data_chart            # noqa: E402
import image_relevance as ir  # noqa: E402


def _ok(item: str, ok, detail: list[str], advisory: bool = False) -> dict:
    return {"item": item, "ok": ok, "detail": detail, "advisory": advisory}


# ——————————————————————————————————————————————
# 硬项 A：接线
# ——————————————————————————————————————————————

def check_wired() -> dict:
    """保存层必须真的调用两个模块，且 filter 在入库之前。"""
    src = ORCH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    detail, ok = [], True
    for name, why in (("candidates", "没调用 data_chart.candidates，数据图永远出不来"),
                      ("filter_candidates", "数据图没过相关性闸门就入库")):
        if name in attrs:
            detail.append(f"✅ orchestrator 调用 {name}")
        else:
            detail.append(f"❌ {why}")
            ok = False

    if "image_anchors" in src:
        detail.append("✅ metadata 落 image_anchors（相关性可追溯）")
    else:
        detail.append("❌ metadata 没有 image_anchors，配图相关性无法事后复核")
        ok = False

    # filter 必须出现在 append({...anchor...) 之前——顺序反了等于先入库后校验
    i_filter = src.find("filter_candidates")
    i_anchor = src.find('"anchor": _c.get("anchor")')
    if i_filter != -1 and i_anchor != -1 and i_filter < i_anchor:
        detail.append("✅ 闸门在入库之前执行")
    elif i_filter != -1 and i_anchor != -1:
        detail.append("❌ 先入库后过闸门，被拒的图已经进 downloaded 了")
        ok = False

    return _ok("A 接线", ok, detail)


# ——————————————————————————————————————————————
# 硬项 B：闸门行为（负向优先）
# ——————————————————————————————————————————————

def check_gate_behaviour() -> dict:
    cases = [
        ("正文讲这场球 → 必须放行", True,
         {"entities": ["皇马", "瓦伦西亚"], "numbers": ["2", "1"]},
         {"title": "皇马2-1瓦伦西亚", "content": "皇家马德里拿下瓦伦西亚。"}),
        ("正文只讲引援 → 必须拒", False,
         {"entities": ["皇马", "瓦伦西亚"], "numbers": ["2", "1"]},
         {"title": "皇马冬窗想签中卫", "content": "皇马正在评估后防。"}),
        ("国际米兰冒充 AC米兰 → 必须拒", False,
         {"entities": ["AC米兰"], "numbers": []},
         {"title": "国际米兰1-0小胜", "content": "蓝黑军团拿下三分。"}),
        ("AC米兰本体 → 必须放行", True,
         {"entities": ["AC米兰"], "numbers": []},
         {"title": "AC米兰1-0小胜", "content": "红黑军团拿下三分。"}),
        ("数字全不沾边 → 必须拒", False,
         {"entities": ["皇马"], "numbers": ["2", "27"]},
         {"title": "皇马这场怎么评价", "content": "过程难看。"}),
        ("没有 anchor → 必须拒", False, {},
         {"title": "皇马", "content": "皇马"}),
    ]
    detail, ok = [], True
    for name, want, anchor, art in cases:
        got = ir.check(anchor, art)["ok"]
        if got == want:
            detail.append(f"✅ {name}")
        else:
            detail.append(f"❌ {name}（期望 {'放行' if want else '拒绝'}，实际相反）")
            ok = False
    return _ok("B 闸门行为", ok, detail)


# ——————————————————————————————————————————————
# 硬项 C：不画假数据
# ——————————————————————————————————————————————

def check_no_fake_chart() -> dict:
    ok_font, why = data_chart.font_status()
    if not ok_font:
        return _ok("C 不画假数据", None,
                   [f"⚠️ 无中文字体（{why}），渲染类判定本次未判定"])

    tmp = Path(tempfile.mkdtemp())
    fx = {"home_team": "Real Madrid CF", "away_team": "Valencia CF",
          "home_score": 2, "away_score": 1}
    detail, ok = [], True

    if data_chart.chart_goals({**fx, "goals": []}, out_dir=tmp, filename="a.jpg") is None:
        detail.append("✅ 无进球事件 → 不画时间轴")
    else:
        detail.append("❌ 无进球事件却画了时间轴（就是画假图）")
        ok = False

    if data_chart.chart_score({"home_team": "A", "away_team": "B"},
                              out_dir=tmp, filename="b.jpg") is None:
        detail.append("✅ 缺比分 → 不画比分卡")
    else:
        detail.append("❌ 缺比分却画了比分卡")
        ok = False

    table = [{"position": 1, "team": "FC Barcelona", "points": 34, "played": 13}]
    row = {"position": 2, "team": "Real Madrid CF", "points": None, "played": 13}
    if data_chart.chart_standings(data_chart.resolve_team("Real Madrid CF"), row,
                                   table, "西甲", out_dir=tmp, filename="c.jpg") is None:
        detail.append("✅ 缺积分 → 不画积分榜")
    else:
        detail.append("❌ 缺积分却画了积分榜")
        ok = False

    if data_chart.candidates({"title": "皇马", "content": "皇马"}, {}) == []:
        detail.append("✅ 无比赛数据 → 不产出任何数据图")
    else:
        detail.append("❌ 无比赛数据却产出了图")
        ok = False

    return _ok("C 不画假数据", ok, detail)


# ——————————————————————————————————————————————
# 硬项 D：被拒的图不能留在磁盘
# ——————————————————————————————————————————————

def check_no_orphan_files() -> dict:
    """file_writer 是扫 images 目录得出 image_paths 的。

    被闸门拒掉的图如果还躺在 images/ 里，台账会记 3 张、读者只看到 2 张——
    正是「有结论没证据」那类事故。源码里必须有 unlink / os.remove。
    """
    src = ORCH.read_text(encoding="utf-8")
    has_cleanup = ("unlink()" in src) or ("os.remove" in src)
    has_glob = "{prefix}-src-*" in src
    detail = []
    if has_cleanup and has_glob:
        detail.append("✅ 数据图临时文件有清理（glob + unlink）")
        ok = True
    else:
        detail.append("❌ 被拒/未用的数据图没有被清理，会被当成已配图计入台账")
        ok = False
    return _ok("D 无残留图", ok, detail)


# ——————————————————————————————————————————————
# 硬项 E：真实产物闭环
# ——————————————————————————————————————————————

def _recent_metas(days: int) -> list[Path]:
    cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for p in sorted(OUTPUT_DIR.glob("2026-*/metadata.json"), reverse=True):
        if p.parent.name >= cutoff:
            out.append(p)
    return out


def check_products(days: int = 7) -> dict:
    metas = _recent_metas(days)
    if not metas:
        return _ok("E 产物闭环", None, [f"近 {days} 天没有批次产物，未判定"])

    checked = failed = 0
    detail, seen_batch = [], False
    for mp in metas:
        try:
            data = json.loads(mp.read_text(encoding="utf-8"))
        except Exception as exc:
            detail.append(f"⚠️ {mp.parent.name}: metadata 读取失败 {exc}")
            continue
        arts = [a for a in (data.get("articles") or []) if isinstance(a, dict)]
        # 只有本改动之后的批次才带 image_anchors 字段；字段缺失 = 早于本改动，未判定
        if not any("image_anchors" in a for a in arts):
            continue
        seen_batch = True

        for a in arts:
            body_path = a.get("path")
            body = ""
            if body_path and Path(body_path).exists():
                body = Path(body_path).read_text(encoding="utf-8")
            for anchor_rec in (a.get("image_anchors") or []):
                checked += 1
                anchor = anchor_rec.get("anchor")
                r = ir.check(anchor or {}, {"title": a.get("title", ""), "content": body})
                if not r["ok"]:
                    failed += 1
                    detail.append(f"❌ 《{str(a.get('title', ''))[:18]}》"
                                  f"{anchor_rec.get('file')}：{r['reason']}")

    if not seen_batch:
        return _ok("E 产物闭环", None,
                   [f"近 {days} 天 {len(metas)} 个批次均早于本改动（无 image_anchors），未判定"])

    if checked == 0:
        detail.append("ℹ️ 生效日后的批次没有产出任何数据图——"
                      "可能是当天无比赛数据，需人工看一眼")
        return _ok("E 产物闭环", True, detail)
    if failed:
        return _ok("E 产物闭环", False,
                   [f"❌ {failed}/{checked} 张数据图的 anchor 复验不通过"] + detail[:10])
    detail.append(f"✅ {checked} 张数据图 anchor 全部与正文一致（闭环复验通过）")
    return _ok("E 产物闭环", True, detail)


# ——————————————————————————————————————————————
# 告警 F：拦截留痕
# ——————————————————————————————————————————————

def check_reject_ledger(days: int = 7) -> dict:
    metas = _recent_metas(days)
    total = without_reason = 0
    for mp in metas:
        try:
            data = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in (data.get("degrade_events") or []):
            if not isinstance(e, dict) or e.get("环节") != "配图相关性":
                continue
            total += 1
            reason = str(e.get("说明") or e.get("reason") or "").strip()
            if not reason:
                without_reason += 1

    detail = [f"近 {days} 天「配图相关性」降级 {total} 次"]
    if without_reason:
        detail.append(f"❌ 其中 {without_reason} 次没写原因（= 没告警）")
        return _ok("F 拦截留痕", False, detail, advisory=True)
    detail.append("✅ 每次拦截都带原因" if total else "✅ 窗口内没有拦截记录")
    return _ok("F 拦截留痕", True, detail, advisory=True)


# ——————————————————————————————————————————————

def verify(days: int = 7) -> dict:
    products = check_products(days)
    hard = [check_wired(), check_gate_behaviour(), check_no_fake_chart(),
            check_no_orphan_files(), products]
    soft = [check_reject_ledger(days)]

    bad = [c["item"] for c in hard if c["ok"] is False]
    if bad:
        ok = False
    elif products["ok"] is None or any(c["ok"] is None for c in hard):
        ok = None
    else:
        ok = True
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            "days": days, "ok": ok,
            "undetermined": [c["item"] for c in hard + soft if c["ok"] is None],
            "hard_failed": bad,
            "advisory_failed": [c["item"] for c in soft if c["ok"] is False],
            "checks": hard + soft}


def render_md(rep: dict) -> str:
    verdict = {True: "通过 ✅", False: "未通过 ❌", None: "未判定 ⚠️"}[rep["ok"]]
    L = ["# 配图相关性验收（数据图 + 闸门）", "",
         f"- 生成时间：{rep['generated_at']}",
         f"- 统计窗口：近 {rep['days']} 天",
         f"- 验收结论：**{verdict}**", ""]
    if rep["hard_failed"]:
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    if rep.get("undetermined"):
        L += [f"> 未判定项：{'、'.join(rep['undetermined'])}", ""]
    for c in rep["checks"]:
        tag = "告警项" if c.get("advisory") else "硬项"
        mark = {True: "✅", False: "❌", None: "⚠️"}[c["ok"]]
        L.append(f"## {c['item']}（{tag}）：{mark}")
        L.extend(f"- {d}" for d in c.get("detail", []))
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="配图相关性验收")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="只跑闸门负向自检")
    args = ap.parse_args()

    if args.selftest:
        rep = check_gate_behaviour()
        print(("✅ " if rep["ok"] else "❌ ") + rep["item"])
        for d in rep["detail"]:
            print("   " + d)
        return 0 if rep["ok"] else 1

    rep = verify(days=args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"image_relevance_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "image_relevance_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    if rep["ok"] is None:
        return 2
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
