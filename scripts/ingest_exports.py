#!/usr/bin/env python3
"""导出文件自动入库（R11 内容诊断 2026-10-13「建议7」：绩效回填自动化）。

问题：绩效回填全靠人肉——「导出 xlsx → 手动跑 import_performance.py」，结果数据永远滞后
一周，效果闭环永远慢一拍（近 10 月 100 篇只 60 篇有绩效）。

做法：把「导出文件」当成投递到仓库的输入。用户只需把从头条后台下载的文件丢进
`data/inbox/`（或经 CI 上传），本脚本自动：
  1. 识别类型——头条「图文数据」导出 → 走 `import_performance.ingest_toutiao`（顺带回写
     metadata）；头条「粉丝分析」导出 → 走 `growth_import`；
  2. 幂等——按文件 sha256 记账，重复投递不重复导入；
  3. 归档——处理完把文件移到 `data/processed/<日期>/`，inbox 保持干净；
  4. 落台账——`data/ingest_state.json` 记录每次导入的文件、类型、命中数与时间。

纪律：识别不出类型 → 不导入、**原样留在 inbox** 并报 `unknown`，绝不猜；
     导入失败 → 留在 inbox 并记 `failed`（下次投递会重试）。

用法：
  python3 scripts/ingest_exports.py                 # 消费 data/inbox/
  python3 scripts/ingest_exports.py --dry-run       # 只识别不导入、不移动
  python3 scripts/ingest_exports.py --json          # 机器可读结果（CI 用）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import growth_import as gi              # noqa: E402
import toutiao_import as tti            # noqa: E402
import import_performance as ip         # noqa: E402

INBOX = PROJECT_ROOT / "data" / "inbox"
PROCESSED = PROJECT_ROOT / "data" / "processed"
STATE_PATH = PROJECT_ROOT / "data" / "ingest_state.json"
CST = timezone(timedelta(hours=8))
EXTS = (".xlsx", ".xlsm", ".csv", ".tsv", ".txt")

# 类型判据（表头关键词；避免误判：粉丝分析导出没有展现量/阅读量列）
GROWTH_KEYS = ("粉丝总数", "总粉丝数", "累计粉丝", "涨粉数", "涨粉量", "净增粉丝", "新增粉丝")
PERF_TITLE_KEYS = ("标题",)
PERF_METRIC_KEYS = ("展现量", "阅读量", "点击率", "点赞量", "评论量")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_kind(path: Path) -> tuple[str, list[str]]:
    """返回 (kind, header)；kind ∈ perf / growth / unknown / unreadable。"""
    try:
        rows = tti.read_table(path)
    except Exception:
        rows = []
    if not rows:
        rows = gi.read_table(path)
    if not rows:
        return "unreadable", []
    for row in rows[:6]:
        header = [str(c).strip() for c in row if c is not None and str(c).strip()]
        if not header:
            continue
        joined = "|".join(header)
        has_growth = any(k in joined for k in GROWTH_KEYS)
        has_perf = (any(k in c for c in header for k in PERF_TITLE_KEYS)
                    and any(k in joined for k in PERF_METRIC_KEYS))
        if has_growth and not has_perf:
            return "growth", header
        if has_perf:
            return "perf", header
    return "unknown", []


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"processed": {}, "history": []}
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        d.setdefault("processed", {})
        d.setdefault("history", [])
        return d
    except Exception:
        return {"processed": {}, "history": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def _archive(path: Path, stamp: str, *, dry_run: bool) -> str:
    dest_dir = PROCESSED / stamp
    if dry_run:
        return str(dest_dir / path.name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        dest = dest_dir / f"{path.stem}_{sha256_of(path)[:8]}{path.suffix}"
    shutil.move(str(path), str(dest))
    try:
        return str(dest.relative_to(PROJECT_ROOT))
    except ValueError:      # 归档目录被指到仓库外（测试/自定义）→ 记绝对路径
        return str(dest)


def ingest_one(path: Path, state: dict, *, dry_run: bool = False) -> dict:
    """处理单个文件；返回结果 dict。"""
    digest = sha256_of(path)
    if digest in state.get("processed", {}):
        rec = {"file": path.name, "status": "duplicate", "kind": state["processed"][digest].get("kind"),
               "sha256": digest[:12]}
        rec["archived_to"] = _archive(path, datetime.now(CST).strftime("%Y-%m-%d"), dry_run=dry_run)
        return rec

    kind, header = detect_kind(path)
    rec = {"file": path.name, "kind": kind, "sha256": digest[:12],
           "header": header[:8], "at": datetime.now(CST).strftime("%Y-%m-%d %H:%M")}

    if kind == "unknown" or kind == "unreadable":
        rec["status"] = "unknown"
        rec["note"] = "识别不出类型，**原样留在 inbox**（不猜、不导入）"
        return rec

    if dry_run:
        rec["status"] = "dry-run"
        return rec

    try:
        if kind == "perf":
            out = ip.ingest_toutiao(path)
            rep = out["report"]
            stats = out["stats"] or {}
            rec["matched"] = len(rep.get("matched") or [])
            rec["imported"] = stats.get("imported", 0)
            rec["dates"] = stats.get("dates", [])
            rec["errors"] = stats.get("errors", [])
            rec["status"] = "ok" if rec["imported"] else (
                "empty" if rep.get("ok") else "failed")
        else:  # growth
            recs = gi.parse_rows(tti.read_table(path) or gi.read_table(path))
            merged = gi.merge(gi.load(), recs, source=path.name)
            gi.save(merged)
            rec["rows"] = len(recs)
            rec["derived"] = len([1 for v in merged["days"].values()
                                  if isinstance(v, dict) and v.get("derived")])
            rec["status"] = "ok" if recs else "empty"
    except Exception as e:  # pragma: no cover - 环境相关
        rec["status"] = "failed"
        rec["errors"] = [f"{type(e).__name__}: {e}"]
        return rec          # 失败：留在 inbox，下次重试

    state.setdefault("processed", {})[digest] = {
        "kind": kind, "file": path.name,
        "at": rec["at"], "status": rec["status"]}
    rec["archived_to"] = _archive(path, datetime.now(CST).strftime("%Y-%m-%d"), dry_run=dry_run)
    return rec


def run(*, dry_run: bool = False) -> dict:
    INBOX.mkdir(parents=True, exist_ok=True)
    state = load_state()
    files = sorted(p for p in INBOX.iterdir()
                   if p.is_file() and p.suffix.lower() in EXTS and not p.name.startswith("."))
    results = [ingest_one(p, state, dry_run=dry_run) for p in files]
    if not dry_run:
        state.setdefault("history", []).append(
            {"at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
             "files": [{"file": r["file"], "kind": r.get("kind"), "status": r["status"]}
                       for r in results]})
        state["history"] = state["history"][-60:]
        state["updated_at"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
        save_state(state)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    try:
        inbox_label = str(INBOX.relative_to(PROJECT_ROOT))
    except ValueError:      # 投递箱被指到仓库外（测试/自定义）→ 记绝对路径
        inbox_label = str(INBOX)
    return {"at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
            "inbox": inbox_label,
            "files": len(files), "counts": counts, "results": results,
            "dry_run": dry_run}


def render_md(rep: dict) -> str:
    L = [f"# 导出文件自动入库（{rep['at']}）", "",
         f"- inbox：`{rep['inbox']}`　文件数：{rep['files']}"
         + ("　（dry-run）" if rep["dry_run"] else ""),
         "- 结果：" + ("、".join(f"{k} {v}" for k, v in sorted(rep["counts"].items())) or "无文件"),
         ""]
    for r in rep["results"]:
        L.append(f"- `{r['file']}` → {r['kind']}／**{r['status']}**"
                 + (f"　匹配 {r.get('matched')} 行、回填 {r.get('imported')} 行"
                    if r.get("imported") is not None else "")
                 + (f"　涨粉行 {r.get('rows')}（差分 {r.get('derived')}）"
                    if r.get("rows") is not None else "")
                 + (f"　→ `{r['archived_to']}`" if r.get("archived_to") else "")
                 + (f"　⚠️ {r['note']}" if r.get("note") else ""))
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="导出文件自动入库（建议7）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = ap.parse_args()

    rep = run(dry_run=args.dry_run)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render_md(rep))
    # unknown / failed 都算硬失败：前者要人工确认，后者要修
    return 1 if (rep["counts"].get("unknown") or rep["counts"].get("failed")
                 or rep["counts"].get("unreadable")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
