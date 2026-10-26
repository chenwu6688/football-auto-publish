"""标题句式闸门（P0-3）单测。

实测基线：近 14 天 68 篇里「主帅压力体」16 次——同一个句式撑起了整个账号的标题。
闸门要能识别它、能在超过阈值时 FAIL、且**不能误杀正常标题**。
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import template_gate as tg  # noqa: E402


# 真实产出过的标题（来自 output/*/metadata.json，逐字复制，不做修饰）
REAL_BAD = [
    "没想到亚特兰大主帅这场压力比AC米兰还大，他输不起的到底是什么？",
    "拉齐奥主帅这场竟比尤文更输不起，他到底在赌什么？",
    "罗马主帅没想到：积分净胜球占优，为啥这场输不起？",
]
REAL_GOOD = [
    "皇马2-1巴萨，姆巴佩87分钟绝杀到底改了什么？",
    "金球奖颁给谁，评审这次真的没看错吗？",
    "曼联0-3惨败，中场休息时更衣室里发生了什么？",
]


@pytest.mark.parametrize("title", REAL_BAD)
def test_known_bad_titles_hit_a_signature(title):
    assert tg.signature_of(title) is not None, f"应识别为模板句式：{title}"


@pytest.mark.parametrize("title", REAL_GOOD)
def test_normal_titles_are_not_misjudged(title):
    """战报/人物稿的正常标题不能被误判——否则闸门会逼着模型写怪标题。"""
    assert tg.signature_of(title) is None, f"正常标题被误杀：{title}"


def test_threshold_is_two_per_fourteen_days():
    assert tg.MAX_PER_SIGNATURE == 2
    assert tg.DEFAULT_WINDOW_DAYS == 14


def test_check_fails_when_signature_exceeds_threshold():
    rep = tg.check(REAL_BAD)
    assert not rep["ok"]
    assert rep["violations"], "超阈值句式必须列出来"
    assert rep["threshold"] == 2


def test_check_passes_at_threshold():
    """恰好等于阈值不算违规（≤2 篇）。"""
    rep = tg.check(REAL_BAD[:2])
    assert rep["ok"], rep


def test_saturated_signatures_blocks_next_generation():
    sat = tg.saturated_signatures(REAL_BAD[:2])
    assert "主帅压力体" in sat
    assert "主帅压力体" not in tg.saturated_signatures(REAL_BAD[:1])


def test_signature_prefers_first_match():
    """首条命中即止：主帅+压力 归「主帅压力体」，不会同时算进「输不起体」。"""
    t = "某某主帅这场压力太大，他到底输不起什么？"
    assert tg.signature_of(t) == "主帅压力体"


def test_preview_titles_are_flagged():
    """前瞻腔单列一体（计划 7.1：前瞻体占比为零）。"""
    assert tg.signature_of("今晚欧冠前瞻，谁会赢？") == "前瞻腔"


def test_empty_title_is_safe():
    assert tg.signature_of("") is None
    assert tg.signature_of(None) is None
    assert tg.check([])["ok"] is True


def _write_meta(root: Path, date: str, titles, pred_indices=()):
    d = root / date
    d.mkdir(parents=True, exist_ok=True)
    arts = []
    for i, t in enumerate(titles):
        arts.append({"title": t, "is_prediction": i in pred_indices})
    (d / "metadata.json").write_text(
        json.dumps({"articles": arts}, ensure_ascii=False), encoding="utf-8")


def test_recent_titles_reads_window_and_skips_prediction():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_meta(root, "2026-10-26", ["今天的稿一", "明天的预测稿"], pred_indices=(1,))
        _write_meta(root, "2026-10-25", ["昨天的稿二"])
        got = tg.recent_titles(root, "2026-10-26", days=3)
        assert "今天的稿一" in got
        assert "昨天的稿二" in got
        assert "明天的预测稿" not in got, "预测稿不发布，不进标题口径"


def test_recent_titles_since_filter_skips_old_entries():
    """改造生效日之前的稿不参与判定（否则新规则永远 FAIL）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_meta(root, "2026-10-26", ["新稿"])
        _write_meta(root, "2026-10-20", ["旧稿"])
        got = tg.recent_titles(root, "2026-10-26", days=14, since="2026-10-25")
        assert got == ["新稿"]


def test_recent_titles_tolerates_broken_json():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        d = root / "2026-10-26"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text("{not json", encoding="utf-8")
        assert tg.recent_titles(root, "2026-10-26", days=3) == []
