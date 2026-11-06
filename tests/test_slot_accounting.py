# -*- coding: utf-8 -*-
"""槽位账：结构（batch_mix）与供给（确定性/战报/主稿）说的必须是同一件事。

## 钉住的事故（2026-11-06）

漏斗原文：`llm 4 → dedup 4 → material 4 → category 4 → batch_mix 2 → slot 1 → generated 1`。

`batch_mix.assign` 按 `max_articles`(2) 定了 `main + data` 两槽，但确定性供给
在它**之后**才生成、先占 1 槽 → 主稿只剩 1 槽、`topics[:1]`，**data 那条被静默丢弃**。
metadata 里那句「data 槽无可用候选，本批少写 1 篇」是不实记录：候选有，是被顶掉的。

后果：`content_mix.TARGET_PCT` 给数据对比 30 + 排行榜 18 = 48%，结构性不可达；
而战术榜单（赛程稿 4/5 的角度都落这一型）日占 33%，超目标 8 倍。

三条修法各自的可验证点：
  1. 确定性供给只在**填得上那个空槽**时才占位（`need_type`），填不上让位；
  2. 主稿跳过「已被填掉的类型」（`_select_main_draft`），不重复写同型；
  3. 类型名归一（「数据对照」=「数据对比」），否则配比统计与日上限都落空。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import content_mix  # noqa: E402
import data_collector as dc  # noqa: E402
import orchestrator  # noqa: E402
from utils import CST  # noqa: E402


# ---------------------------------------------------------------- 类型归一

def test_data_angle_normalizes_to_data_type():
    """赛程角度表写的是「数据对照」，目标表写的是「数据对比」——必须归一。"""
    assert content_mix.normalize("数据对照") == "数据对比"


def test_data_type_has_daily_cap():
    """归一之后 DAILY_CAP 才查得到（归一前返回 None = 没有单日上限）。"""
    import batch_mix
    assert batch_mix.DAILY_CAP.get("数据对比") == 3


# ---------------------------------------------------------------- 主稿取题

def _t(title, ct):
    return {"title": title, "content_type": ct}


def test_main_draft_skips_type_already_provided():
    """确定性供给已写「数据对比」，主稿不得再写一篇同型。"""
    topics = [_t("热点标题", "热点球评"), _t("数据标题", "数据对比")]
    articles = [(0, {"content_type": "数据对比"})]
    pool, n = orchestrator._select_main_draft(topics, articles, main_slots=1)
    assert [t["title"] for t in pool] == ["热点标题"]
    assert n == 1


def test_main_draft_keeps_both_when_nothing_provided():
    """没有确定性供给时，两槽都归主稿（这正是被顶掉之前的正确行为）。"""
    topics = [_t("热点标题", "热点球评"), _t("数据标题", "数据对比")]
    pool, n = orchestrator._select_main_draft(topics, [], main_slots=2)
    assert len(pool) == 2 and n == 2


def test_main_draft_count_never_exceeds_pool():
    """篇数必须等于「池子大小」与「槽位」的较小值 —— 否则漏斗记 2、实际写 1。"""
    pool, n = orchestrator._select_main_draft([_t("只有一条", "热点球评")], [], main_slots=2)
    assert len(pool) == 1 and n == 1


def test_main_draft_zero_slots():
    _, n = orchestrator._select_main_draft([_t("x", "热点球评")], [], main_slots=0)
    assert n == 0


# ---------------------------------------------------------------- 确定性供给让位

def _pool_file(tmp_path, columns=("战术榜单",), days_ahead=2):
    """造一个赛程池：开赛时间在未来窗口内，角度落在指定栏目。"""
    now = datetime.now(CST)
    topics = []
    for i, col in enumerate(columns):
        kick = now + timedelta(days=days_ahead, hours=i)
        topics.append({
            "home_zh": "阿森纳", "away_zh": "曼联",
            "competition": "英超", "angle_type": "AA",
            "suggested_column": col,
            "kickoff_cst": kick.strftime("%Y-%m-%d %H:%M"),
            "publish_by": kick.strftime("%Y-%m-%d %H:%M"),
            "priority": 10.0 + i, "conflict": "", "awareness_pair": "AA",
            "fact_source": "fixture_library",
        })
    p = tmp_path / "topic_pool.json"
    p.write_text(__import__("json").dumps({"topics": topics}, ensure_ascii=False),
                 encoding="utf-8")
    return p


@pytest.fixture()
def pool_path(tmp_path, monkeypatch):
    p = _pool_file(tmp_path, columns=("战术榜单",))
    monkeypatch.setattr(dc, "FIXTURE_POOL_PATH", p)
    return p


def test_fixture_yields_when_cannot_fill_needed_type(pool_path):
    """本批需要「数据对比」，池里只有「战术榜单」→ 让位（返回空），不占槽。"""
    got = dc.collect_fixture_topics("2026-11-06", need_type="数据对比")
    assert got == []


def test_fixture_takes_slot_when_type_matches(pool_path):
    got = dc.collect_fixture_topics("2026-11-06", need_type="战术榜单")
    assert len(got) == 1 and got[0]["suggested_column"] == "战术榜单"


def test_fixture_without_need_type_keeps_old_behavior(pool_path):
    """不传 need_type 时行为不变（其它调用方/测试不受影响）。"""
    assert len(dc.collect_fixture_topics("2026-11-06")) == 1


def test_fixture_matches_aliased_column(tmp_path, monkeypatch):
    """池里标「数据对照」，need_type 是「数据对比」→ 必须命中（归一后才相等）。"""
    monkeypatch.setattr(dc, "FIXTURE_POOL_PATH",
                        _pool_file(tmp_path, columns=("数据对照",)))
    got = dc.collect_fixture_topics("2026-11-06", need_type="数据对比")
    assert len(got) == 1


# ---------------------------------------------------------------- 主流程接线

def test_main_flow_passes_need_type():
    """光有 need_type 参数不算数：主流程必须真的把「缺哪一型」传下去。"""
    import inspect
    src = inspect.getsource(orchestrator.main)
    assert "need_type=_need_type" in src, "collect_fixture_topics 必须收到本批缺口"
    assert "_select_main_draft(" in src, "主稿必须按「已填类型」过滤后才取"


def test_slot_funnel_reports_pool_not_raw_topics():
    """漏斗 slot 层必须记「过滤后能写的」，不能记过滤前的候选数。"""
    import inspect
    src = inspect.getsource(orchestrator.main)
    assert '_funnel("slot", min(len(_main_pool), _main_slots)' in src
