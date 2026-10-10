"""高风险内容人工确认队列（计划 1.2 / 4.1）。

计划 1.2 定义了三个人工介入节点，其一是「高风险内容确认」：
    触发条件：涉及伤病、合同金额、争议指控
    处理方式：进入人工确认队列，确认后方可发布

设计要点：
- 判定只发生在「装配完成后、保存/发布前」，命中即从本次发布集合中剔除，
  写入 data/pending_review.json 队列（status=pending），绝不由自动流程发布。
- 人工用本模块 CLI 复核：--list / --approve / --reject。
- 确认通过后由 `materialize(date)` 把已确认条目写回 output 目录，供发布器下次拉取。

数据不入库密钥，队列文件属运行期资产，已 gitignore。
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
QUEUE_PATH = PROJECT_ROOT / "data" / "pending_review.json"

# 三类高风险触发词（计划 1.2 / 4.1 —— 制造谣言 70 / 不实 20 / 失实 10 的对冲）
RISK_CATEGORIES = {
    "伤病": [
        "受伤", "伤停", "缺阵", "赛季报销", "重伤", "十字韧带", "骨折", "手术",
        "养伤", "肌肉撕裂", "脚踝", "膝伤", "脑震荡", "伤退", "拉伤", "伤缺",
    ],
    "争议指控": [
        "指控", "起诉", "诉讼", "调查", "禁赛", "处罚", "罚款", "丑闻", "性侵",
        "种族歧视", "假球", "赌球", "禁药", "兴奋剂", "税务", "逃税", "拘留",
        "逮捕", "指控他", "上诉", "仲裁",
    ],
    "合同金额": [
        "转会费", "违约金", "解约金", "年薪", "周薪", "签字费", "买断费",
        "租借费", "报价", "要价", "合同", "续约",
    ],
}

# 合同金额类需同时出现金额/年限量词，避免「合同年」一类误判
_MONEY_RE = re.compile(r"(\d+(?:\.\d+)?\s*(?:万|亿|欧|元|美元|镑|年|w|W|€|\$))")


def assess(article: dict) -> dict:
    """判定一篇成稿是否属高风险，返回 {high_risk, categories, reasons}。

    只为「涉及伤病 / 合同金额 / 争议指控」的题材设闸；普通赛事、人物故事等不拦。
    """
    if not isinstance(article, dict):
        return {"high_risk": False, "categories": [], "reasons": []}
    title = article.get("title", "") or ""
    content = article.get("content", "") or ""
    blob = f"{title}\n{content}"

    cats, reasons = [], []
    for cat, words in RISK_CATEGORIES.items():
        hits = [w for w in words if w in blob]
        if cat == "合同金额":
            # 需同时有金额/年限量词才算「合同金额」高风险
            if hits and _MONEY_RE.search(blob):
                cats.append(cat)
                reasons.append(f"{cat}(命中：{'、'.join(hits[:3])}；含金额量词)")
        elif hits:
            cats.append(cat)
            reasons.append(f"{cat}(命中：{'、'.join(hits[:3])})")

    return {"high_risk": bool(cats), "categories": cats, "reasons": reasons}


# ─── 队列读写 ─────────────────────────────────────────────

def _load() -> dict:
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": []}


def _save(data: dict) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def enqueue(article: dict, date_str: str, index, risk: dict) -> str:
    """把高风险成稿写入队列，返回条目 id（status=pending）。"""
    data = _load()
    rid = f"{date_str}-{index}-{uuid.uuid4().hex[:6]}"
    data["items"].append({
        "id": rid,
        "date": date_str,
        "index": index,
        "title": article.get("title", ""),
        "reasons": risk.get("reasons", []),
        "categories": risk.get("categories", []),
        "status": "pending",
        "queued_at": datetime.now().isoformat(timespec="seconds"),
        "resolved_at": "",
        # 保留完整成稿，确认后可直接 materialize，无需重生成
        "article": article,
    })
    _save(data)
    return rid


def pending_items() -> list[dict]:
    return [it for it in _load().get("items", []) if it.get("status") == "pending"]


def all_items() -> list[dict]:
    return _load().get("items", [])


def resolve(item_id: str, approve: bool) -> dict | None:
    """人工复核：approve=True 通过 / False 驳回。返回被更新条目。"""
    data = _load()
    for it in data.get("items", []):
        if it.get("id") == item_id:
            it["status"] = "approved" if approve else "rejected"
            it["resolved_at"] = datetime.now().isoformat(timespec="seconds")
            _save(data)
            return it
    return None


def is_blocked(article: dict, *, queue_only: bool = True) -> bool:
    """发布前自检：该成稿是否仍处于待确认（未批准）状态。"""
    return bool(assess(article).get("high_risk")) if queue_only else False


def materialize(date_str: str, *, output_dir: Path | None = None) -> list[dict]:
    """把已确认(approved)的条目写回 output/{date}，供发布器下次拉取。

    返回写入条目列表；成功写回后条目状态置 published。
    """
    out = Path(output_dir) if output_dir else (PROJECT_ROOT / "output")
    date_dir = out / date_str
    date_dir.mkdir(parents=True, exist_ok=True)
    meta_path = date_dir / "metadata.json"
    meta = {"articles": []}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {"articles": []}
    meta.setdefault("articles", [])

    data = _load()
    written = []
    for it in data.get("items", []):
        if it.get("status") != "approved" or it.get("date") != date_str:
            continue
        art = it.get("article") or {}
        idx = it.get("index")
        slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", (art.get("title") or f"pending-{idx}"))[:40]
        md_path = date_dir / f"article-{idx}-{slug}.md"
        body = art.get("content", "")
        md_path.write_text(
            f"---\ntitle: \"{art.get('title','')}\"\ndate: {date_str}\n"
            f"review: approved\nid: {it['id']}\n---\n\n# {art.get('title','')}\n\n{body}\n",
            encoding="utf-8")
        meta["articles"].append({
            "index": idx, "title": art.get("title", ""), "path": str(md_path),
            "slug": slug, "tags": art.get("keywords", []), "keywords": art.get("keywords", []),
            "content_type": art.get("content_type", ""),
            "column_name": art.get("_column_name", ""),
            "batch_name": art.get("_batch_name", ""), "review": "approved"})
        it["status"] = "published"
        written.append(it)
    if written:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        _save(data)
    return written


# ─── CLI ─────────────────────────────────────────────────

def _cli() -> None:
    ap = argparse.ArgumentParser(description="高风险内容人工确认队列（计划 1.2）")
    ap.add_argument("--list", action="store_true", help="列出待确认条目")
    ap.add_argument("--all", action="store_true", help="列出全部条目")
    ap.add_argument("--approve", metavar="ID", help="确认通过并写回 output")
    ap.add_argument("--reject", metavar="ID", help="驳回")
    ap.add_argument("--date", help="materialize 的日期(默认取该条目日期)")
    args = ap.parse_args()

    if args.list or args.all:
        items = all_items() if args.all else pending_items()
        if not items:
            print("（无待确认条目）")
            return
        for it in items:
            print(f"[{it['status']:9s}] {it['id']}  {it['title'][:40]}")
            print(f"           风险：{'；'.join(it.get('reasons', []))}")
        return

    if args.approve:
        it = resolve(args.approve, approve=True)
        if not it:
            print(f"未找到条目: {args.approve}")
            return
        written = materialize(it["date"])
        print(f"✅ 已确认 {it['id']}，写回 {len(written)} 篇（{it['date']}），发布器下次运行即可拉取")
        return

    if args.reject:
        it = resolve(args.reject, approve=False)
        print(f"🚫 已驳回 {it['id']}" if it else f"未找到条目: {args.reject}")
        return

    ap.print_help()


if __name__ == "__main__":
    _cli()
