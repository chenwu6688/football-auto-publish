#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次「生成」与「完成」必须分开（2026-11-06 丢批事故的回归钉）。

事故：noon 批次生成 2 篇 → 配图相关性硬项失败 → 发布步骤整段 skipped。
但 `orchestrator.save_batch_state` 在保存成稿时就写了 `batches_completed`，
metadata 早在「生成完成元数据回写」步骤就提交进仓库。幂等保护据此认定
「noon 今日已完成」：兜底 cron 不重跑、人工补发被跳过 —— 两篇稿永久丢。

所以钉四条：
  1. 生成侧只写 batches_generated，绝不写 batches_completed
  2. 发布成功后才由 mark_batch_completed.py 写 completed（幂等）
  3. --unmark 能解除（失败批次的补救通道）
  4. 配图相关性验收遇到「正文文件不在本 runner」必须判未判定，不许拿标题冒充正文
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_verify():
    """按文件路径加载 scripts/verify_image_relevance.py（scripts 不是包）。"""
    path = PROJECT_ROOT / "scripts" / "verify_image_relevance.py"
    spec = importlib.util.spec_from_file_location("verify_image_relevance_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_save_batch_state_never_marks_completed():
    """生成侧写 generated，不写 completed。"""
    import orchestrator as orch

    orig = orch.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            orch.OUTPUT_DIR = Path(td)
            orch.save_batch_state("2026-11-06", "noon", [{"title": "x"}])
            meta = json.loads((Path(td) / "2026-11-06" / "metadata.json").read_text())
            assert meta["batches_generated"] == ["noon"]
            assert "batches_completed" not in meta, \
                "生成成功就写 completed = 发布失败也无法补发（11-06 丢批根因）"
            assert meta["last_batch"] == "noon"
    finally:
        orch.OUTPUT_DIR = orig
    print("   PASS test_save_batch_state_never_marks_completed")


def _run_mark(*args):
    return subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "mark_batch_completed.py"),
                           *args], capture_output=True, text=True, cwd=str(PROJECT_ROOT))


def test_mark_completed_is_idempotent():
    """发布成功后标记完成；重复调用不产生重复项。"""
    with tempfile.TemporaryDirectory() as td:
        meta_path = Path(td) / "metadata.json"
        meta_path.write_text(json.dumps({"articles": [], "batches_completed": []}))

        r1 = _run_mark("--date", "2026-11-06", "--batch", "noon", "--path", str(meta_path))
        assert r1.returncode == 0, r1.stderr
        meta = json.loads(meta_path.read_text())
        assert meta["batches_completed"] == ["noon"]

        r2 = _run_mark("--date", "2026-11-06", "--batch", "noon", "--path", str(meta_path))
        assert r2.returncode == 0
        assert json.loads(meta_path.read_text())["batches_completed"] == ["noon"], "重复标记不该产生重复项"
    print("   PASS test_mark_completed_is_idempotent")


def test_unmark_allows_republish():
    """被误标完成的批次必须能解除标记，否则补发永远被幂等跳过。"""
    with tempfile.TemporaryDirectory() as td:
        meta_path = Path(td) / "metadata.json"
        meta_path.write_text(json.dumps({"articles": [],
                                         "batches_completed": ["morning", "noon"]}))
        r = _run_mark("--date", "2026-11-06", "--batch", "noon",
                      "--unmark", "--path", str(meta_path))
        assert r.returncode == 0, r.stderr
        assert json.loads(meta_path.read_text())["batches_completed"] == ["morning"]
    print("   PASS test_unmark_allows_republish")


def test_missing_body_is_undetermined_not_failed():
    """正文文件不在本 runner（跨批次累计的旧记录）→ 未判定，不是失败。

    11-06 noon：metadata 带着 morning 那篇的 anchor，md 文件在其他 runner 上，
    旧实现退回「只用标题比对」→ 判「实体不在正文」→ 硬项失败 → 整批不发。
    """
    mod = _load_verify()
    orig = mod.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            day = Path(td) / "2026-11-06"
            day.mkdir(parents=True)
            (day / "metadata.json").write_text(json.dumps({
                "articles": [{
                    "title": "阿森纳客场2-1拿下斯拉维亚",
                    "path": str(day / "不存在的正文.md"),     # 文件不在本 runner
                    "image_anchors": [{"file": "a.jpg", "anchor": {
                        "kind": "score", "entities": ["拉格斯拉维亚", "阿森纳"],
                        "numbers": ["1", "2"]}}],
                }]
            }, ensure_ascii=False))

            mod.OUTPUT_DIR = Path(td)
            rep = mod.check_products(days=7)
            assert rep["ok"] is None, f"应判未判定，实际 {rep['ok']}：{rep['detail']}"
            assert any("未判定" in d for d in rep["detail"])
    finally:
        mod.OUTPUT_DIR = orig
    print("   PASS test_missing_body_is_undetermined_not_failed")


def test_present_body_is_judged():
    """正文在本 runner 上 → 正常复验（不能因为放宽缺失而整体失效）。"""
    mod = _load_verify()
    orig = mod.OUTPUT_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            day = Path(td) / "2026-11-06"
            day.mkdir(parents=True)
            body = day / "a.md"
            body.write_text("阿森纳客场2-1拿下斯拉维亚，阿尔特塔换人差点玩脱。", encoding="utf-8")
            (day / "metadata.json").write_text(json.dumps({
                "articles": [{
                    "title": "阿森纳客场2-1拿下斯拉维亚",
                    "path": str(body),
                    "image_anchors": [{"file": "a.jpg", "anchor": {
                        "kind": "score", "entities": ["布拉格斯拉维亚", "阿森纳"],
                        "numbers": ["1", "2"]}}],
                }]
            }, ensure_ascii=False))

            mod.OUTPUT_DIR = Path(td)
            rep = mod.check_products(days=7)
            assert rep["ok"] is True, f"正文与锚点一致应通过，实际 {rep['ok']}：{rep['detail']}"
    finally:
        mod.OUTPUT_DIR = orig
    print("   PASS test_present_body_is_judged")


if __name__ == "__main__":
    test_save_batch_state_never_marks_completed()
    test_mark_completed_is_idempotent()
    test_unmark_allows_republish()
    test_missing_body_is_undetermined_not_failed()
    test_present_body_is_judged()
    print("ALL PASS")
