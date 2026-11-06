# -*- coding: utf-8 -*-
"""队名跨语言归一 + 赛程选题去重（2026-11-06 重复发同一场）。

事故：晨读写《领跑，那不勒斯这一关，队长憋的劲该往哪使》，午间又写
《登顶遇上那不勒斯，队长憋的那口气比积分更重》。两篇 keywords 重叠 4/5，
是同一场 Lazio vs Napoli。

三重失效叠在一起：

1. `collect_fixture_topics` 拿 **英文** 关键词（``napoli``）与 **中文** 队名
   （``那不勒斯``）比对，``"napoli" in "那不勒斯"`` 恒为 False；
2. `get_cross_batch_covered` 的 ``teams`` 用 ``constants.WIKI_TEAMS`` ——
   那张表只有 15 支队，那不勒斯/拉齐奥都不在里面；
3. 事件指纹要求「两队名 + 比分」同时出现在标题里，而这两篇标题只有一支队。

修法是把比对单位从「字符串」换成「实体」：所有写法归一到 entity_map 的
规范中文名，再按「对阵」判重（与语言、措辞都无关）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import data_collector  # noqa: E402
import team_names as tn  # noqa: E402


# ---------------------------------------------------------------- 归一

def test_canonical_team_chinese():
    assert tn.canonical_team("那不勒斯") == "那不勒斯"
    assert tn.canonical_team("拉齐奥") == "拉齐奥"
    assert tn.canonical_team("蓝鹰") == "拉齐奥"          # 别名


def test_canonical_team_english():
    assert tn.canonical_team("Napoli") == "那不勒斯"
    assert tn.canonical_team("SSC Napoli") == "那不勒斯"
    assert tn.canonical_team("Lazio") == "拉齐奥"
    assert tn.canonical_team("Racing Club de Lens") == "朗斯"


def test_canonical_team_unknown_is_none():
    assert tn.canonical_team("根本不存在的队名XYZ") is None
    assert tn.canonical_team("") is None


def test_canonical_team_does_not_split_multiword():
    """「Inter Milan」拆开只剩 Milan 会指向 AC 米兰，那是国米的口语混写。

    这条是本次实现的第一个 bug：v1 用「拆词后唯一命中就归一」，
    实测 ``Inter Milan -> AC米兰``。错认的后果比不认严重得多
    —— 会把国米的稿子判成 AC 米兰的重复而丢掉。
    """
    assert tn.canonical_team("Inter Milan") is None
    assert tn.canonical_team("AC Milan") == "AC米兰"
    assert tn.canonical_team("Internazionale") == "国际米兰"


def test_alias_forms_cover_both_languages():
    forms = tn.alias_forms("那不勒斯")
    assert "那不勒斯" in forms
    assert "napoli" in forms or "ssc napoli" in forms


def test_mentions_team_cross_language():
    assert tn.mentions_team("Napoli 3-1 Lazio", "那不勒斯") is True
    assert tn.mentions_team("那不勒斯3-1拉齐奥", "Napoli") is True
    assert tn.mentions_team("英超第11轮综述", "那不勒斯") is False


def test_teams_in_tokens():
    got = tn.teams_in_tokens(["Serie A", "Lazio", "Napoli",
                              "league table", "captain mentality"])
    assert got == {"拉齐奥", "那不勒斯"}


def test_same_fixture_cross_language():
    assert tn.same_fixture(("拉齐奥", "那不勒斯"), ("Napoli", "Lazio")) is True
    assert tn.same_fixture(("拉齐奥", "那不勒斯"), ("Napoli", "尤文")) is False
    # 只有一支队时不足以判「同一场」
    assert tn.same_fixture(("拉齐奥",), ("Lazio",)) is False


# ---------------------------------------------------------------- 已发侧收集

def test_cross_batch_covered_collects_fixture_pairs(tmp_path, monkeypatch):
    """已发侧必须能认出「今天写过哪一场」，且中英混排都要认。"""
    import orchestrator

    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path)
    d = tmp_path / "2026-11-06"
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(json.dumps({
        "batches_completed": ["morning"],
        "articles": [{
            "title": "领跑，那不勒斯这一关，队长憋的劲该往哪使",
            "keywords": ["Serie A", "Lazio", "Napoli", "league table", "captain mentality"],
            "tags": ["Serie A", "Lazio", "Napoli"],
            "content_type": "战术榜单",
        }],
    }, ensure_ascii=False), encoding="utf-8")

    covered = orchestrator.get_cross_batch_covered("2026-11-06")
    assert frozenset({"拉齐奥", "那不勒斯"}) in covered["fixture_pairs"]
    assert "那不勒斯" in covered["teams"]
    assert "拉齐奥" in covered["teams"]


def test_cross_batch_covered_collects_hot_keys(tmp_path, monkeypatch):
    """热点事件键必须被读回来，否则同一件热搜每批都会重写一篇。"""
    import orchestrator

    monkeypatch.setattr(orchestrator, "OUTPUT_DIR", tmp_path)
    d = tmp_path / "2026-11-06"
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(json.dumps({
        "articles": [{"title": "C罗退出国家队", "hot_event_key": "C罗#retire"}],
    }, ensure_ascii=False), encoding="utf-8")

    covered = orchestrator.get_cross_batch_covered("2026-11-06")
    assert covered["hot_event_keys"] == {"C罗#retire"}


# ---------------------------------------------------------------- 供给侧去重

def _write_pool(path: Path, topics):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"topics": topics}, ensure_ascii=False), encoding="utf-8")


def _fixture(home, away, publish_by="2099-01-01 20:00", priority=50.0):
    return {"home_zh": home, "away_zh": away, "publish_by": publish_by,
            "kickoff_cst": publish_by, "competition": "意甲",
            "angle_type": "冲突点", "priority": priority}


def test_collect_fixture_topics_drops_same_fixture(tmp_path, monkeypatch):
    """今日已发同一场（中文对中文）→ 跳过。"""
    pool = tmp_path / "topic_pool.json"
    _write_pool(pool, [_fixture("拉齐奥", "那不勒斯")])
    monkeypatch.setattr(data_collector, "FIXTURE_POOL_PATH", pool)

    covered = {"keywords": set(), "titles": set(),
               "fixture_pairs": {frozenset({"拉齐奥", "那不勒斯"})}}
    assert data_collector.collect_fixture_topics(
        "2026-11-06", max_topics=1, lookahead_days=36500,
        cross_batch_covered=covered) == []


def test_collect_fixture_topics_drops_same_fixture_english_keywords(tmp_path, monkeypatch):
    """今日已发同一场（已发侧只有英文关键词）→ 也必须跳过。

    这是 2026-11-06 事故原样：已发 keywords 是 ``lazio``/``napoli``，
    候选是中文「拉齐奥」「那不勒斯」，旧实现按字符串比对恒不相等。
    """
    pool = tmp_path / "topic_pool.json"
    _write_pool(pool, [_fixture("拉齐奥", "那不勒斯")])
    monkeypatch.setattr(data_collector, "FIXTURE_POOL_PATH", pool)

    covered = {"keywords": {"serie a", "lazio", "napoli"},
               "titles": set(), "fixture_pairs": set()}
    assert data_collector.collect_fixture_topics(
        "2026-11-06", max_topics=1, lookahead_days=36500,
        cross_batch_covered=covered) == []


def test_collect_fixture_topics_keeps_other_fixture(tmp_path, monkeypatch):
    """去重不能矫枉过正：不同场次必须照常产出。"""
    pool = tmp_path / "topic_pool.json"
    _write_pool(pool, [_fixture("科莫", "朗斯")])
    monkeypatch.setattr(data_collector, "FIXTURE_POOL_PATH", pool)

    covered = {"keywords": {"serie a", "lazio", "napoli"},
               "titles": set(),
               "fixture_pairs": {frozenset({"拉齐奥", "那不勒斯"})}}
    got = data_collector.collect_fixture_topics(
        "2026-11-06", max_topics=1, lookahead_days=36500,
        cross_batch_covered=covered)
    assert len(got) == 1 and got[0]["home_zh"] == "科莫"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
