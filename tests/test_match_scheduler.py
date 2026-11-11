"""赛事日历（`match_scheduler`）纯逻辑测试。

**这个模块当前是 0% 覆盖**：`pytest --cov` 显示 110 条语句一条没跑，因为全仓
没有任何生产代码 import 它（`grep -rn match_scheduler --include=*.py` 只命中自己与
README），它的产物 `output/schedule/match_schedule.json` 也从未生成、无人读取。

给它补这组测试的理由不是「它很重要」，而是：**0% 覆盖的模块等于没有回归网**，
一旦有人把它接进主链路，里面的日期推算错了没人会发现。这里只覆盖能离线跑的部分
（`fetch_upcoming_matches` 要 football-data.org 的 key，不在此列）。

死链本身（有实现、无调用方、无消费者）是待决事项，不在测试里「顺手修掉」——
要么接进 pipeline，要么删掉，属产品决策。
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import match_scheduler as ms

CST = ZoneInfo("Asia/Shanghai")


def _match(days_ahead=1, focus=True, hhmm="20:00", home="Manchester United", away="Liverpool"):
    dt = (datetime.now(CST) + timedelta(days=days_ahead)).replace(
        hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
    return {
        "id": 1, "home_team": home, "away_team": away, "league": "Premier League",
        "cst_time": dt.strftime("%Y-%m-%d %H:%M"), "cst_date": dt.strftime("%Y-%m-%d"),
        "focus_match": focus, "status": "SCHEDULED",
    }


def test_focus_match_yields_three_stage_sequence():
    """焦点战 → 前瞻（晨）/ 复盘（晚，赛后次日）/ 深读（午，赛后第 3 天）。"""
    plans = ms.plan_content_sequence([_match(days_ahead=1)])
    kinds = [(p["content_type"], p["suggested_batch"]) for p in plans]
    assert kinds == [("preview", "morning"), ("review", "evening"), ("deep_dive", "noon")]


def test_review_and_deep_dive_dates_are_after_match_day():
    m = _match(days_ahead=1)
    plans = ms.plan_content_sequence([m])
    match_day = datetime.strptime(m["cst_date"], "%Y-%m-%d")
    by_type = {p["content_type"]: p for p in plans}
    assert by_type["review"]["date"] == (match_day + timedelta(days=1)).strftime("%Y-%m-%d")
    assert by_type["deep_dive"]["date"] == (match_day + timedelta(days=2)).strftime("%Y-%m-%d")


def test_non_focus_match_yields_no_plan():
    assert ms.plan_content_sequence([_match(days_ahead=1, focus=False)]) == []


def test_finished_match_gets_no_preview():
    """已经开打的比赛不能再规划「前瞻」（复盘/深读照排）。"""
    plans = ms.plan_content_sequence([_match(days_ahead=-2)])
    assert [p["content_type"] for p in plans] == ["review", "deep_dive"]


def test_match_without_time_is_skipped():
    m = _match(days_ahead=1)
    m["cst_time"] = ""
    assert ms.plan_content_sequence([m]) == []


def test_save_schedule_writes_json(tmp_path):
    m = _match(days_ahead=1)
    plans = ms.plan_content_sequence([m])
    path = ms.save_schedule([m], plans, output_dir=tmp_path)
    assert path == tmp_path / "match_schedule.json"
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"generated_at", "matches", "content_plans"}
    assert len(data["content_plans"]) == len(plans)
