#!/usr/bin/env python3
"""临时目录空间兜底的单测（2026-11-11）。

背景：本机 /tmp 是 10MB tmpfs。跑全量时 video_pipeline 那批要生成 1080x1920 的
mp4，几秒就把 /tmp 撑满，22 个用例报 `Errno 28 No space left on device`，
pytest 退出码 120、日志文件自己被清掉 —— 表象是「ffmpeg 合成失败」，
真因是磁盘。`conftest._pick_temp_root()` 就是为这件事加的。

判据只有一条：**只在系统临时目录真的不够用时才改道**。所以这里重点守
「够用时必须什么都不做」——兜底逻辑一旦误判，会让所有环境都悄悄换目录。
"""

import tempfile
from pathlib import Path

import conftest


def test_no_redirect_when_current_has_enough_space(tmp_path, monkeypatch):
    """空间充足 → 返回 None（不改 TMPDIR，不动任何环境变量）。"""
    monkeypatch.setattr(conftest, "_free_mb", lambda p: 99999.0)
    assert conftest._pick_temp_root(current=tmp_path,
                                    candidates=[tmp_path / "cand"]) is None


def test_redirects_when_current_is_nearly_full(tmp_path, monkeypatch):
    """当前目录只剩几 MB → 换到空间够的候选目录。"""
    cand = tmp_path / "roomy"
    monkeypatch.setattr(conftest, "_free_mb",
                        lambda p: 5.0 if str(p) == str(tmp_path) else 99999.0)
    got = conftest._pick_temp_root(current=tmp_path, candidates=[cand])
    assert got == str(cand)
    assert cand.is_dir(), "候选目录应被创建（否则 pytest 后续仍会写失败）"


def test_skips_candidate_when_also_full(tmp_path, monkeypatch):
    """第一个候选也满 → 继续找下一个，别在满盘上原地重试。"""
    full = tmp_path / "full"
    ok = tmp_path / "ok"

    def fake_free(p):
        # 当前目录与第一个候选都很挤，第二个候选有空间
        return 99999.0 if str(p) == str(ok) else 5.0

    monkeypatch.setattr(conftest, "_free_mb", fake_free)
    got = conftest._pick_temp_root(current=tmp_path, candidates=[full, ok])
    assert got == str(ok)


def test_returns_none_when_no_candidate_qualifies(tmp_path, monkeypatch):
    """所有候选都不够 → 返回 None（保持原状，让问题暴露而不是掩盖）。"""
    monkeypatch.setattr(conftest, "_free_mb", lambda p: 1.0)
    assert conftest._pick_temp_root(current=tmp_path,
                                    candidates=[tmp_path / "a", tmp_path / "b"]) is None


def test_unreadable_current_is_treated_as_fine(tmp_path, monkeypatch):
    """读不到可用空间（如特殊文件系统）→ 视为够用，不擅自改道。"""
    monkeypatch.setattr(conftest, "_free_mb", lambda p: -1.0)
    assert conftest._pick_temp_root(current=tmp_path,
                                    candidates=[tmp_path / "c"]) is None


def test_pytest_temp_root_has_space():
    """自检：pytest 当前真正在用的临时根目录，可用空间必须够。

    这是端到端那一条 —— 兜底若没生效，这里会先红，而不是等某个用例
    生成到一半才报 `Errno 28`（那时错误信息指向 ffmpeg，完全看不出是磁盘）。
    """
    root = Path(tempfile.gettempdir())
    free_mb = conftest._free_mb(root)
    assert free_mb < 0 or free_mb >= 64, (
        f"pytest 临时根 {root} 仅剩 {free_mb:.1f}MB，MediaCrawler 与 video_pipeline "
        f"用例会以 Errno 28 失败（错误信息会误导成 ffmpeg 问题）。"
        f"设 TMPDIR 到有空间的目录，或让 conftest 的兜底生效。")
