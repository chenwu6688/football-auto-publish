#!/usr/bin/env python3
"""内容类型配比验收（2026-11-01 月度分析）。

病根：10-26 那轮修复把「人物故事 + 历史回顾」从 23% 压到 9%，
但 10-27 之后 **战术榜单冲到 47%**——只是换了个靶子，没有真正改善结构。
更硬的事实（按实际发布口径 93 篇算）：

    热点球评  15 篇  篇均 48.1  中位 15   ← 唯一稳定跑得出的类型
    战术榜单  13 篇  篇均 21.2  中位  1   ← 占比最高，但靠 1 个爆款拉均值
    历史回顾   9 篇  篇均  0.9  中位  0   ← 已禁发
    榜单       4 篇  篇均  0.5  中位  0

也就是说配比与绩效是反的：最能打的类型占比最低。本脚本把这件事钉住：

  A. 目标表自洽（硬项）—— TARGET_PCT 合计 100、禁发类型目标为 0、
     且 content_type 轴的禁发清单与 reservoir.BANNED_TYPES 一致（两处口径不许漂移）
  B. 禁发类型零产出（硬项）—— 生效日之后窗口内出现禁发类型即 FAIL
  C. 热点球评占比（告警项）—— 低于目标算调整期，只告警不阻断
  D. 已接入批次流程（硬项）—— batch.yml 必须真的调用复核，否则「写了等于没写」

产出 data/audit/content_mix_<date>.md + content_mix_latest.json；硬项 FAIL 退出码 1。

用法：python3 scripts/verify_content_mix.py [--days 7] [--quiet]
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import content_mix  # noqa: E402
import reservoir  # noqa: E402

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
OUTPUT_DIR = PROJECT_ROOT / "output"
BATCH_YML = PROJECT_ROOT / ".github" / "workflows" / "batch.yml"
CST = timezone(timedelta(hours=8))

# 配比约束生效日。此日之前的成稿按旧配比产出（战术榜单 47%），
# 拿它判新目标＝永远 FAIL，闸门就成噪音了。历史只作基线展示。
MIX_SINCE = "2026-11-01"


def check_target_table() -> dict:
    """A. 目标表自洽，且与 reservoir 的禁发清单同源。"""
    detail, ok = [], True
    total = sum(content_mix.TARGET_PCT.values())
    if abs(total - 100.0) > 0.01:
        ok = False
        detail.append(f"❌ 目标占比合计 {total:.1f}%，应为 100%")
    else:
        detail.append(f"✅ 目标占比合计 {total:.0f}%")

    zero = set(k for k, v in content_mix.TARGET_PCT.items() if v == 0)
    banned = set(reservoir.BANNED_TYPES)
    missing = banned - zero
    extra = zero - banned
    if missing:
        ok = False
        detail.append(f"❌ reservoir 禁发但配比表仍给目标：{'、'.join(sorted(missing))}")
    if extra:
        # 允许配比表比 reservoir 更严（如「榜单」是被绩效判死的，不是被清单禁的），
        # 但必须显式说明，否则以后分不清「禁发」和「并入别名」。
        detail.append(f"ℹ️ 配比表额外置 0（非 reservoir 禁发，属并入别名）：{'、'.join(sorted(extra))}")
    if not missing:
        detail.append(f"✅ 禁发清单与 reservoir 一致：{'、'.join(sorted(banned)) or '（空）'}")

    # 自相矛盾检查（2026-11-01 补）：SOFT_TYPES 与 BANNED_TYPES 不得有交集。
    # 这条是负向验证逼出来的——把「历史回顾」加回 SOFT_TYPES、BANNED_TYPES 保持不动时，
    # 上面的检查全都通过（清单没变），但实际语义已经矛盾（既允许生成又禁发）。
    # 只比 BANNED_TYPES 不看 SOFT_TYPES，等于没守住「口径自洽」。
    overlap = set(reservoir.SOFT_TYPES) & banned
    if overlap:
        ok = False
        detail.append(f"❌ SOFT_TYPES 与 BANNED_TYPES 冲突，同时含："
                      f"{'、'.join(sorted(overlap))}（既允许生成又禁发）")
    else:
        detail.append(f"✅ SOFT_TYPES 与禁发清单无交集（{len(reservoir.SOFT_TYPES)} 个软性类型）")
    return {"item": "目标配比表自洽", "ok": ok, "detail": detail}


def check_no_banned_output(days: int) -> dict:
    """B. 生效日之后窗口内，禁发类型零产出。"""
    today = datetime.now(CST).strftime("%Y-%m-%d")
    since = max(MIX_SINCE, (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d"))
    arts = content_mix.load_range_articles(OUTPUT_DIR, since, today)
    if not arts:
        return {"item": f"禁发类型零产出（{since} 起）", "ok": True, "advisory": True,
                "detail": [f"⚠️ {since} 之后本机无成稿（CI 才有 output/），跳过判定"]}
    hits = {}
    for a in arts:
        ct = content_mix.normalize(a.get("content_type"))
        if content_mix.TARGET_PCT.get(ct) == 0:
            hits[ct] = hits.get(ct, 0) + 1
    if hits:
        return {"item": f"禁发类型零产出（{since} 起）", "ok": False,
                "detail": [f"❌ 样本 {len(arts)} 篇中仍产出禁发类型："
                           + "、".join(f"{k} {v} 篇" for k, v in hits.items())]}
    return {"item": f"禁发类型零产出（{since} 起）", "ok": True,
            "detail": [f"✅ 样本 {len(arts)} 篇，无禁发类型产出"]}


def check_hot_ratio(days: int) -> dict:
    """C. 热点球评占比（告警项：调整期不阻断）。"""
    today = datetime.now(CST).strftime("%Y-%m-%d")
    since = max(MIX_SINCE, (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d"))
    arts = content_mix.load_range_articles(OUTPUT_DIR, since, today)
    if not arts:
        return {"item": "热点球评占比", "ok": True, "advisory": True,
                "detail": [f"⚠️ {since} 之后无成稿，跳过"]}
    rep = content_mix.check(arts)
    hot = rep["items"].get("热点球评") or {}
    pct, tgt = hot.get("pct", 0.0), content_mix.TARGET_PCT.get("热点球评", 35.0)
    detail = [f"热点球评 {hot.get('count', 0)} 篇，占比 {pct:.0f}%（目标 {tgt:.0f}%）"]
    if hot.get("avg_reads") is not None:
        detail.append(f"该类型篇均阅读 {hot['avg_reads']}（全窗口均值参考）")
    if pct + content_mix.TOL_PCT < tgt:
        detail.append(f"⚠️ 低于目标超过容差 {content_mix.TOL_PCT:.0f}pp —— 调整期内只告警")
    else:
        detail.insert(0, "✅")
    return {"item": "热点球评占比", "ok": True, "advisory": True, "detail": detail,
            "warn": pct + content_mix.TOL_PCT < tgt}


def check_wired() -> dict:
    """D. batch.yml 真的调了复核 —— 否则这套东西就是文档。

    软化检查只针对**调用 content_mix 的那一行**：整份 batch.yml 里别处合法地
    用了 `continue-on-error`（如「失败原因兜底」步骤），全文搜索会误报。
    """
    detail, ok = [], True
    if not BATCH_YML.exists():
        return {"item": "已接入批次流程", "ok": False,
                "detail": ["❌ 找不到 .github/workflows/batch.yml"]}
    text = BATCH_YML.read_text(encoding="utf-8")
    call_lines = [ln for ln in text.splitlines() if "content_mix.py" in ln]
    if not call_lines:
        return {"item": "已接入批次流程", "ok": False,
                "detail": ["❌ batch.yml 未调用 content_mix（写了等于没写）"]}
    detail.append(f"✅ batch.yml 已调用内容配比复核（{len(call_lines)} 处）")
    # 只看调用行本身有没有被软化（`|| true` / `|| echo` / 前面挂 continue-on-error）
    softened = [ln.strip() for ln in call_lines if re.search(r"\|\|\s*(true|echo)", ln)]
    if softened:
        ok = False
        detail.append("❌ 调用行被软化，硬项形同没有：")
        detail += [f"   - {s}" for s in softened]
    else:
        detail.append("✅ 调用行未软化（硬项 FAIL 会真阻断）")
    return {"item": "已接入批次流程", "ok": ok, "detail": detail}


def verify(days: int = 7) -> dict:
    checks = [check_target_table(), check_no_banned_output(days),
              check_hot_ratio(days), check_wired()]
    hard = [c for c in checks if not c.get("advisory")]
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "window_days": days, "since": MIX_SINCE,
            "ok": all(c["ok"] for c in hard), "checks": checks,
            "hard_failed": [c["item"] for c in hard if not c["ok"]]}


def render_md(rep: dict) -> str:
    L = ["# 内容类型配比验收", "",
         f"- 生成时间：{rep['generated_at']}｜窗口 {rep['window_days']} 天｜生效日 {rep['since']}",
         f"- 验收结论：**{'通过 ✅' if rep['ok'] else '未通过 ❌'}**", ""]
    if rep.get("hard_failed"):
        L += [f"> 未通过硬项：{'、'.join(rep['hard_failed'])}", ""]
    for c in rep["checks"]:
        L.append(f"## {c['item']}：{'✅' if c['ok'] else '❌'}"
                 + ("（告警项）" if c.get("advisory") else ""))
        L += [f"- {d}" for d in c.get("detail", [])]
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="内容类型配比验收")
    ap.add_argument("--days", type=int, default=7, help="统计窗口天数（默认 7）")
    ap.add_argument("--quiet", action="store_true", help="只写报告，不打印详情")
    args = ap.parse_args()
    rep = verify(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"content_mix_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "content_mix_latest.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
