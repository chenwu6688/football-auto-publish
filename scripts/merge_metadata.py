#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「本批 metadata」合并进仓库里的当日 metadata（供 batch.yml 两处回写调用）。

## 为什么抽成脚本（原来两处各写一份内联 python）

batch.yml 里有两处回写都做「合并跨批次文章」，各自 `python3 -c` 内联一段差不多
的代码。2026-11-05 暴露的后果：两处都只合并了 `articles` / `batches_completed`，
唯独 `image_floor` 直接沿用**本批**的值，于是合并后的文件里

    articles: 4 篇（今日累计，其中 3 篇各只有 1 张图）
    image_floor.articles_below: 0（只反映最后那批 1 篇 3 张）

同一份台账里两个字段口径不同 —— 验收读 `articles_below` 会得出「今天全达标」，
而实际有 3 篇没达标。合并这一动作必须自己负责把它的统计字段算对。

## 口径

* `articles` 按 `(title, content_type)` 去重，dst 在前（保留原有顺序）；
* `batches_completed` 取并集；
* 其余标量字段以 src（本批）为准 —— 保持原有行为；
* **`image_floor.articles_below` 按合并后的 articles 重算**，且只统计「有配图台账」
  的文章（`image_ledger.has_image_ledger`）：改造前的老产物没有台账，不参与判定。

用法：
    python3 scripts/merge_metadata.py --src <本批> --dst <仓库内当日>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import image_ledger  # noqa: E402


def merge_missing_articles(old_articles, new_articles):
    """按 ``(title, content_type)`` 去重追加，保持 old 的顺序在前。"""
    merged = list(old_articles or [])
    seen = {(a.get("title", ""), a.get("content_type", "")) for a in merged
            if isinstance(a, dict)}
    for a in new_articles or []:
        if not isinstance(a, dict):
            continue
        key = (a.get("title", ""), a.get("content_type", ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(a)
    return merged


def recompute_image_floor(meta: dict) -> None:
    """按合并后的 articles 重算 `image_floor.articles_below` / `below_titles`。

    没有 `image_floor` 字段（改造前的批次）就什么都不做——不要凭空造一个，
    那会让「未判定」变成「通过」。
    """
    floor = meta.get("image_floor")
    if not isinstance(floor, dict):
        return
    want = int(floor.get("min_per_article") or 3)
    n, titles = image_ledger.count_below_floor(meta.get("articles") or [], want)
    floor["articles_below"] = n
    if titles:
        floor["below_titles"] = titles
    else:
        floor.pop("below_titles", None)


def merge(src_path: Path, dst_path: Path) -> dict:
    src = json.loads(src_path.read_text(encoding="utf-8"))
    if dst_path.exists():
        dst = json.loads(dst_path.read_text(encoding="utf-8"))
        old_articles = dst.get("articles") or []
        # 今日累计：老批次文章在前（保持原有顺序），本批新文章追加在后
        merged = merge_missing_articles(old_articles, src.get("articles") or [])
        src["articles"] = merged
        src["total_articles"] = len(merged)
        src["batches_completed"] = sorted(
            set(dst.get("batches_completed") or []) | set(src.get("batches_completed") or []))
        # image_floor 优先用本批的（含 since / min_per_article），缺失时回退老文件
        if not isinstance(src.get("image_floor"), dict) and isinstance(
                dst.get("image_floor"), dict):
            src["image_floor"] = dict(dst["image_floor"])
    meta = src
    meta.setdefault("total_articles", len(meta.get("articles") or []))
    recompute_image_floor(meta)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description="合并当日 metadata 并重算配图下限统计")
    ap.add_argument("--src", required=True, help="本批生成的 metadata.json")
    ap.add_argument("--dst", required=True, help="仓库内当日 metadata.json（不存在则新建）")
    a = ap.parse_args()
    src, dst = Path(a.src), Path(a.dst)
    if not src.exists():
        print(f"⚠️ {src} 不存在，跳过合并")
        return 0
    meta = merge(src, dst)
    floor = meta.get("image_floor") or {}
    print(f"合并完成：{len(meta.get('articles') or [])} 篇 / "
          f"批次 {meta.get('batches_completed')} / "
          f"配图不足 {floor.get('articles_below', '（未标注）')} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
