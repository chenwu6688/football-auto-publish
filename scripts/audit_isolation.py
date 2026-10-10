#!/usr/bin/env python3
"""物理隔离 · 常态化审计（计划 11.3）。

计划 11.3 的核心原则是「正文只进抽取器，永不进生成器」，并辅以两道闸门：
    第二道：相似度闸门（连续重合>12字 或 8-gram 重合率>15% 打回）
    第三道：信息增量强制

上线前的单测能保证「代码逻辑正确」，但不能保证「运行期不退化」。本脚本把
隔离做成**常态化审计作业**，定期对实际产物与代码做检查，任何一项退化即告警。

审计四项：
    A. 生成器模板隔离——rewrite 模板必须声明「原文不可见」，且不得整段注入原文
    B. 生成路径静态隔离——生成函数不得把源文全文拼进消息（必须走抽取器事实块）
    C. 成稿重复自查——同一天内两两 8-gram 重合率与最长连续重合（防模板化/自我复制）
    D. 溯源覆盖率——已发布文章带来源标注的比例（计划第十章验收项）

用法：python3 scripts/audit_isolation.py [--days 7] [--quiet]
退出码：存在 FAIL 项时返回 1（供 CI 告警）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_DIR = PROJECT_ROOT / "output"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"

CST = ZoneInfo("Asia/Shanghai")

# 计划 11.3 阈值
MAX_RUN = 12          # 连续逐字重合上限（字）
JACCARD_WARN = 0.25   # 同天两文 8-gram 重合率——提示
JACCARD_FAIL = 0.35   # ——判失败


def _strip_md(text: str) -> str:
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)      # 图片
    t = re.sub(r"^---.*?---$", "", t, flags=re.S)     # front-matter
    t = re.sub(r"[#*>`\-]", "", t)
    return re.sub(r"\s+", "", t)


def _shingles(text: str, n: int = 8) -> set[str]:
    return {text[i:i + n] for i in range(max(0, len(text) - n + 1))}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ------------------------------------------------------------
# A. 生成器模板隔离
# ------------------------------------------------------------
def audit_prompt_isolation() -> dict:
    issues = []
    tpl = PROMPTS_DIR / "rewrite_article.txt"
    if not tpl.exists():
        return {"item": "生成器模板隔离", "ok": False, "detail": ["rewrite_article.txt 缺失"]}
    text = tpl.read_text(encoding="utf-8")
    if "原文不可见" not in text and "看不到任何原文" not in text:
        issues.append("模板缺少「原文不可见」隔离声明")
    if "抽取器" not in text:
        issues.append("模板未指明输入来自抽取器（结构化事实）")
    # 危险：把整段原文注入模板的占位符
    for bad in ("{article_text}", "{source_article}", "{raw_text}"):
        if bad in text:
            issues.append(f"模板疑似整段注入原文：{bad}")
    return {"item": "生成器模板隔离", "ok": not issues, "detail": issues or ["隔离声明齐备"]}


# ------------------------------------------------------------
# B. 生成路径静态隔离
# ------------------------------------------------------------
def audit_generator_static() -> dict:
    """检查 orchestrator 生成路径是否把源文全文拼进消息。"""
    issues = []
    orc = PROJECT_ROOT / "orchestrator.py"
    if not orc.exists():
        return {"item": "生成路径静态隔离", "ok": False, "detail": ["orchestrator.py 缺失"]}
    src = orc.read_text(encoding="utf-8")
    # 抽取器调用存在（事实块来源）
    if "build_source_facts_block" not in src and "extractor" not in src:
        issues.append("未见抽取器调用（事实块来源缺失）")
    # 危险模式：把 article_text 直接作为消息 content 注入生成
    for m in re.finditer(r"content[\"']?\s*[:=]\s*[^\n]{0,40}article_text", src):
        issues.append(f"疑似把源文全文注入生成消息：{m.group(0)[:60]}")
    return {"item": "生成路径静态隔离", "ok": not issues,
            "detail": issues or ["生成路径只经抽取器事实块"]}


# ------------------------------------------------------------
# C. 成稿重复自查
# ------------------------------------------------------------
def _load_day_articles(date_dir: Path) -> list[tuple[str, str]]:
    out = []
    for md in sorted(date_dir.glob("article-*.md")):
        try:
            body = _strip_md(md.read_text(encoding="utf-8"))
        except Exception:
            continue
        if len(body) >= 200:
            out.append((md.name, body))
    return out


def audit_published_duplication(days: int, *, now=None) -> dict:
    now = now or datetime.now(CST)
    since = (now - timedelta(days=days)).date()
    pairs = []
    for date_dir in sorted(OUTPUT_DIR.glob("*/")):
        try:
            d = datetime.strptime(date_dir.name, "%Y-%m-%d").date()
        except Exception:
            continue
        if d < since or d > now.date():
            continue
        arts = _load_day_articles(date_dir)
        sh = [(name, _shingles(body), body) for name, body in arts]
        for i in range(len(sh)):
            for j in range(i + 1, len(sh)):
                jac = _jaccard(sh[i][1], sh[j][1])
                if jac >= JACCARD_WARN:
                    run = ""
                    try:
                        from extractor import _longest_shared_run
                        run = _longest_shared_run(sh[i][2], sh[j][2])
                    except Exception:
                        run = ""
                    pairs.append({
                        "date": date_dir.name, "a": sh[i][0], "b": sh[j][0],
                        "jaccard": round(jac, 3), "longest_run": len(run),
                    })
    fails = [p for p in pairs if p["jaccard"] >= JACCARD_FAIL or p["longest_run"] >= MAX_RUN]
    pairs.sort(key=lambda p: -p["jaccard"])
    return {"item": "成稿重复自查", "ok": not fails,
            "detail": ([f"{len(fails)} 对疑似模板化/复制" ] if fails else ["同天两两重合均在阈值内"]),
            "pairs": pairs[:10]}


# ------------------------------------------------------------
# D. 溯源覆盖率
# ------------------------------------------------------------
def audit_traceability(days: int, *, now=None) -> dict:
    now = now or datetime.now(CST)
    since = (now - timedelta(days=days)).date()
    total = traced = 0
    for meta_path in OUTPUT_DIR.glob("*/metadata.json"):
        try:
            d = datetime.strptime(meta_path.parent.name, "%Y-%m-%d").date()
        except Exception:
            continue
        if d < since or d > now.date():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in meta.get("articles", []) or []:
            total += 1
            if a.get("sources_used") or a.get("source_post"):
                traced += 1
    rate = round(traced / total * 100, 1) if total else 0.0
    # 溯源覆盖率是「验收指标」而非「隔离失败」：低于目标仅告警，不阻断审计
    return {"item": "溯源覆盖率", "ok": True, "warn": rate < 90 and total > 0,
            "detail": [f"{traced}/{total} = {rate}%（计划第十章目标 100%）"],
            "rate": rate, "total": total}


# ------------------------------------------------------------
# 渲染
# ------------------------------------------------------------
def run(days: int) -> dict:
    checks = [
        audit_prompt_isolation(),
        audit_generator_static(),
        audit_published_duplication(days),
        audit_traceability(days),
    ]
    ok = all(c["ok"] for c in checks)
    return {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "window_days": days, "ok": ok, "checks": checks}


def render_md(rep: dict) -> str:
    L = [f"# 物理隔离审计报告（近 {rep['window_days']} 天）", "",
         f"生成时间：{rep['generated_at']}　|　计划 11.3", "",
         f"总体结论：{'✅ 通过' if rep['ok'] else '❌ 存在失败项'}", ""]
    for c in rep["checks"]:
        mark = "✅" if c["ok"] and not c.get("warn") else ("⚠️" if c["ok"] else "❌")
        L.append(f"## {c['item']}：{mark}")
        for d in c["detail"]:
            L.append(f"- {d}")
        if c.get("pairs"):
            L.append("")
            L.append("| 日期 | 文章A | 文章B | 8-gram 重合率 | 最长连续重合 |")
            L.append("| --- | --- | --- | --- | --- |")
            for p in c["pairs"]:
                L.append(f"| {p['date']} | {p['a']} | {p['b']} | {p['jaccard']} | {p['longest_run']} |")
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="物理隔离常态化审计（计划 11.3）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rep = run(args.days)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    (AUDIT_DIR / f"isolation_{stamp}.md").write_text(render_md(rep), encoding="utf-8")
    (AUDIT_DIR / "latest.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    if not args.quiet:
        print(render_md(rep))
    if not rep["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
