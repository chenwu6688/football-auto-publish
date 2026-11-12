"""小程序静态导出：同一份 today.json 不许被「本次没产出」覆盖成空。

2026-11-11 晚间实证：CI 每次全新 checkout，成稿 md 不入库，``scan_articles``
只能看到本次运行产出的 md。那批 0 篇（合规全丢）时，导出把
``static_data/today.json`` 从 148 行覆盖成 7 行——晨间/午间已发布的 4 篇当场从
小程序里消失，``batches_completed`` 还被写成 ``[]``（明明早上中午都发完了）。

规则：同日合并；本次某批次为空则保留旧内容；``batches_completed`` 取并集；
日期不同（新的一天）才整体覆盖。
"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "export_static_data", ROOT / "scripts" / "export_static_data.py")
esd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(esd)


def _setup(tmp_path, *, meta, prev=None, md_files=None):
    out = tmp_path / "output"
    static = tmp_path / "static_data"
    day = out / "2026-11-11"
    day.mkdir(parents=True)
    (day / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    for name, text in (md_files or {}).items():
        (day / name).write_text(text, encoding="utf-8")
    static.mkdir(parents=True)
    if prev is not None:
        (static / "today.json").write_text(json.dumps(prev, ensure_ascii=False), encoding="utf-8")
    esd.OUTPUT_DIR = out
    esd.STATIC_DIR = static
    return static / "today.json"


PREV = {
    "date": "2026-11-11",
    "batches_completed": ["morning", "noon"],
    "batches": [{"batch_name": "午间", "batch_key": "noon", "column_name": "老六辣评",
                 "articles": [{"id": "2026-11-11_1ab9f8", "title": "已发布的午间稿"}]}],
    "articles": [{"id": "2026-11-11_1ab9f8", "title": "已发布的午间稿"}],
}


def test_zero_article_batch_does_not_wipe_same_day(tmp_path):
    """11-11 晚间的原样复现：本次 0 篇，旧内容必须原封不动。"""
    path = _setup(tmp_path, meta={"articles": [], "batches_completed": ["morning", "noon"]},
                  prev=PREV)
    esd.export_today("2026-11-11")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [a["title"] for a in data["articles"]] == ["已发布的午间稿"]
    assert data["batches"][0]["batch_key"] == "noon"
    assert data["batches_completed"] == ["morning", "noon"]


def test_batches_completed_is_union(tmp_path):
    """本次 metadata 只写了 evening，旧文件里的 morning/noon 不能被抹掉。"""
    path = _setup(tmp_path, meta={"articles": [], "batches_completed": ["evening"]}, prev=PREV)
    esd.export_today("2026-11-11")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["batches_completed"] == ["morning", "noon", "evening"]


def test_new_batch_is_merged_not_replaced(tmp_path):
    md = """---
title: 晚间稿标题
batch_name: 晚间
tags: []
keywords: []
---
第一段正文，够长就行。

## 小标题

第二段正文。
"""
    path = _setup(tmp_path,
                  meta={"articles": [{"title": "晚间稿标题", "index": 1}],
                        "batches_completed": ["morning", "noon", "evening"]},
                  prev=PREV, md_files={"article-1-晚间稿标题-abc123.md": md})
    esd.export_today("2026-11-11")
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = [b["batch_key"] for b in data["batches"]]
    assert "noon" in keys, "旧批次被覆盖掉了"
    titles = [a.get("title") for a in data["articles"]]
    assert "已发布的午间稿" in titles
    assert any("晚间稿" in (t or "") for t in titles)


def test_different_day_overwrites(tmp_path):
    """换天必须整体覆盖，否则昨天的稿子会挂到今天。"""
    path = _setup(tmp_path, meta={"articles": [], "batches_completed": []}, prev=PREV)
    esd.export_today("2026-11-12")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["date"] == "2026-11-12"
    assert data["articles"] == []
    assert data.get("merged_from_previous") is None
