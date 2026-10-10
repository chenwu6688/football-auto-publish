#!/usr/bin/env python3
"""择优量表 · 每周人工抽查（计划 12.4）。

由单一模型长期打分会造成审美漂移：模型倾向给同类选题高分，最终又收敛到单一模板。
对策是评分量表强制覆盖五个维度（冲突度/人物知名度/时效性/话题延展性/粉丝画像匹配度），
并每周人工抽查一次校准。

本作业：
  1. 汇总本周（默认 7 天）的打分记录（优先 data/ranker_cache.json 的 L3 判定，
     缺失时退回 output/*/metadata.json 的选题池）；
  2. 抽样产出「人工复核清单」（含各维度分与总分、需人工确认的栏位）；
  3. 校验量表是否覆盖五维（缺维即报警，防量表退化为四维）；
  4. 与上一次抽查记录对比，输出维度均值漂移（检测模型偏好漂移）；
  5. 落档 data/rank_audit/，可选 WxPusher 推送摘要。

用法:
  python scripts/weekly_rank_audit.py                 # 本周抽查，默认抽 10 条
  python scripts/weekly_rank_audit.py --days 7 --sample 10
  python scripts/weekly_rank_audit.py --no-push
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ranker  # noqa: E402

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "output"))
CACHE_PATH = PROJECT_ROOT / "data" / "ranker_cache.json"
AUDIT_DIR = PROJECT_ROOT / "data" / "rank_audit"
CALIB_PATH = AUDIT_DIR / "calibration.json"

DIMENSIONS = list(ranker.DIMENSIONS)


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def collect_records(days: int) -> tuple[list[dict], str]:
    """收集近 days 天的打分记录。返回 (records, source)。"""
    since = datetime.now() - timedelta(days=days)
    recs, src = [], "none"

    cache = _load_cache()
    judged = cache.get("judged") or {}
    for cid, rec in judged.items():
        at = rec.get("at", "")
        try:
            t = datetime.strptime(at, "%Y-%m-%d %H:%M")
        except Exception:
            continue
        if t < since:
            continue
        dims = rec.get("scores") or {}
        recs.append({"id": cid, "来源": "model", "打分时间": at,
                     "维度": {d: dims.get(d) for d in DIMENSIONS},
                     "总分": rec.get("total", sum(v for v in dims.values() if isinstance(v, int)))})
    if recs:
        return recs, "ranker_cache"

    # 退回：从 output/*/metadata.json 的选题池抽样（模型未启用时的记录）
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir()):
            mf = d / "metadata.json"
            if not mf.is_file():
                continue
            try:
                mdate = datetime.strptime(d.name, "%Y-%m-%d")
            except Exception:
                continue
            if mdate < since:
                continue
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                continue
            for t in (meta.get("topics") or [])[:20]:
                title = t.get("title") or ""
                if not title:
                    continue
                card = {"主体": (t.get("keywords_cn") or [""])[0] if t.get("keywords_cn") else "",
                        "动作": t.get("content_type", ""), "发生时间": d.name}
                dims = ranker.rule_score(card, t)
                recs.append({"id": f"{d.name}:{title[:18]}", "来源": "rule",
                             "打分时间": d.name,
                             "维度": {dd: dims.get(dd) for dd in DIMENSIONS},
                             "总分": sum(dims.values()), "标题": title,
                             "板块": t.get("_column_name", "")})
        if recs:
            src = "metadata"
    return recs, src


def _sample(records: list[dict], n: int) -> list[dict]:
    """确定性抽样：按总分排序后等间隔抽取（覆盖高/中/低分，便于发现偏好漂移）。"""
    if len(records) <= n:
        return sorted(records, key=lambda r: r.get("总分", 0), reverse=True)
    ordered = sorted(records, key=lambda r: r.get("总分", 0), reverse=True)
    step = len(ordered) / n
    return [ordered[min(len(ordered) - 1, int(i * step))] for i in range(n)]


def coverage_ok(records: list[dict]) -> tuple[bool, list[str]]:
    """校验量表覆盖五维：任一维度在样本中全为空即判缺维。"""
    missing = []
    for d in DIMENSIONS:
        if not any(isinstance(r.get("维度", {}).get(d), int) for r in records):
            missing.append(d)
    return (len(missing) == 0), missing


def _dim_means(records: list[dict]) -> dict:
    out = {}
    for d in DIMENSIONS:
        vals = [r["维度"][d] for r in records if isinstance(r.get("维度", {}).get(d), int)]
        out[d] = round(sum(vals) / len(vals), 2) if vals else None
    return out


def _prev_record() -> dict | None:
    if CALIB_PATH.exists():
        try:
            data = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
            hist = data.get("history") or []
            return hist[-1] if hist else None
        except Exception:
            pass
    return None


def _drift(means: dict, prev: dict | None) -> dict:
    if not prev:
        return {}
    pm = prev.get("维度均值") or {}
    out = {}
    for d, v in means.items():
        pv = pm.get(d)
        if isinstance(v, (int, float)) and isinstance(pv, (int, float)):
            out[d] = round(v - pv, 2)
    return out


def build_worksheet(records, days, source, means, drift, cov_ok, missing) -> str:
    lines = [f"# 择优量表 · 每周人工抽查（{datetime.now():%Y-%m-%d}）", ""]
    lines.append(f"- 窗口：近 {days} 天｜来源：{source}｜样本：{len(records)} 条")
    lines.append(f"- 量表覆盖五维：{'✅' if cov_ok else '❌ 缺维: ' + '、'.join(missing)}")
    if means:
        lines.append("- 本周维度均值：" + "；".join(
            f"{d}={v}" for d, v in means.items() if v is not None))
    if drift:
        lines.append("- 较上次漂移：" + "；".join(
            f"{d}={'+' if v >= 0 else ''}{v}" for d, v in drift.items()))
        lines.append("  （某维度持续单向漂移即视为模型偏好漂移，需调整提示词或量表权重）")
    lines += ["", "## 待人工复核清单", "",
              "| # | 选题 | 来源 | 五维分 | 总分 | 人工意见（同意/调整） |",
              "|---|------|------|--------|------|----------------------|"]
    for i, r in enumerate(records, 1):
        dims = "、".join(f"{d[:2]}{r['维度'].get(d)}" for d in DIMENSIONS)
        title = r.get("标题") or r.get("id", "")
        lines.append(f"| {i} | {title[:36]} | {r.get('来源','')} | {dims} | {r.get('总分','')} | |")
    lines += ["", "> 复核说明：逐条确认模型/规则给分是否合理；若同一实体反复拿到高分，",
              "> 需人工下调或在量表中加惩罚项，防止选题同质化（计划 12.4）。", ""]
    return "\n".join(lines)


def persist(worksheet: str, records, means, drift, cov_ok, missing, source, days: int) -> tuple[Path, Path]:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    ws_path = AUDIT_DIR / f"audit_{datetime.now():%Y-%m-%d}.md"
    ws_path.write_text(worksheet, encoding="utf-8")

    data = {"history": []}
    if CALIB_PATH.exists():
        try:
            data = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {"history": []}
    data.setdefault("history", []).append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "窗口天数": days,
        "来源": source,
        "样本数": len(records),
        "量表覆盖五维": cov_ok,
        "缺维": missing,
        "维度均值": means,
        "漂移": drift,
        "样本id": [r.get("id") for r in records],
    })
    CALIB_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return ws_path, CALIB_PATH


def _push(title: str, content: str) -> None:
    token = os.environ.get("WXPUSHER_APPTOKEN", "").strip()
    uid = os.environ.get("WXPUSHER_UID", "").strip()
    if not (token and uid):
        print("⚠️ 未配置 WXPUSHER_APPTOKEN / WXPUSHER_UID，跳过推送")
        return
    try:
        import requests
        requests.post("https://wxpusher.zjiecode.com/api/send/message",
                      json={"appToken": token, "content": f"{title}\n\n{content}",
                            "contentType": 1, "uids": [uid]}, timeout=10)
        print("📣 抽查提醒已推送")
    except Exception as e:
        print(f"⚠️ 推送失败: {type(e).__name__}")


def main() -> int:
    ap = argparse.ArgumentParser(description="择优量表每周人工抽查（计划 12.4）")
    ap.add_argument("--days", type=int, default=7, help="回看窗口天数")
    ap.add_argument("--sample", type=int, default=10, help="抽样条数")
    ap.add_argument("--no-push", action="store_true", help="不推送 WxPusher")
    args = ap.parse_args()

    records, source = collect_records(args.days)
    if not records:
        print("（本周无打分记录，跳过抽查；请确认择优打分/选题池是否正常产出）")
        _push("择优抽查", f"近 {args.days} 天无打分记录，无法抽查。")
        return 0

    sample = _sample(records, args.sample)
    cov_ok, missing = coverage_ok(sample)
    means = _dim_means(sample)
    drift = _drift(means, _prev_record())
    worksheet = build_worksheet(sample, args.days, source, means, drift, cov_ok, missing)
    ws_path, calib = persist(worksheet, sample, means, drift, cov_ok, missing, source, args.days)

    print(worksheet.split("## 待人工复核清单")[0])
    print(f"✅ 抽查清单 → {ws_path.relative_to(PROJECT_ROOT)}")
    print(f"✅ 校准记录 → {calib.relative_to(PROJECT_ROOT)}")
    if not cov_ok:
        print(f"❌ 量表缺维：{'、'.join(missing)}")

    if not args.no_push:
        _push("择优量表每周抽查",
              f"样本 {len(sample)} 条｜五维覆盖 {'OK' if cov_ok else '缺维:' + '/'.join(missing)}\n"
              f"维度均值：" + "；".join(f"{d}={v}" for d, v in means.items() if v is not None) +
              (f"\n漂移：" + "；".join(f"{d}={v:+}" for d, v in drift.items()) if drift else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
