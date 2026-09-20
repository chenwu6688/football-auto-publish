"""维度2 连载栏目引擎 —— 单元测试。

覆盖：
- get_brand_series：返回 3 个系列 id 映射 + max_per_batch
- _build_series_hint：有效 series_id 返回提示；"无"/未知/缺省返回空串
- warn_series_continuity：空列表早退；同系列超上限告警；无系列正常
- 品牌手册含 series_playbook 且三系列齐全
- topic_selector.txt 含 series_id 字段与维度2 连载规则
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import (  # noqa: E402
    _build_series_hint,
    _get_brand_manual_data,
    get_brand_series,
    warn_series_continuity,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPIC_SELECTOR = os.path.join(PROJECT_ROOT, "prompts", "topic_selector.txt")


def test_get_brand_series():
    by_id, max_per = get_brand_series()
    assert max_per == 1
    assert set(by_id.keys()) == {"guozu-chronicle", "wc-classics", "old-fan-night"}
    assert by_id["guozu-chronicle"]["name"] == "国足编年史"


def test_build_series_hint_valid():
    hint = _build_series_hint({"series_id": "guozu-chronicle"})
    assert "国足编年史" in hint
    assert "系列" in hint


def test_build_series_hint_none_values():
    assert _build_series_hint({"series_id": "无"}) == ""
    assert _build_series_hint({"series_id": "无（不适配系列时）"}) == ""
    assert _build_series_hint({}) == ""
    assert _build_series_hint({"series_id": "unknown-id"}) == ""
    assert _build_series_hint(None) == ""


def test_warn_series_continuity_empty():
    warn_series_continuity([])


def test_warn_series_continuity_over_limit_and_zero(capsys):
    # 同系列 2 篇 > 上限 1，应告警
    over = [
        {"series_id": "guozu-chronicle"},
        {"series_id": "guozu-chronicle"},
    ]
    warn_series_continuity(over)
    out = capsys.readouterr().out
    assert "系列连载分布" in out
    assert "⚠️" in out

    capsys.readouterr()
    zero = [{"series_id": "无"}, {"series_id": ""}]
    warn_series_continuity(zero)
    out = capsys.readouterr().out
    assert "0/" in out  # 无系列选题，正常


def test_brand_manual_has_series_playbook():
    data = _get_brand_manual_data()
    assert "series_playbook" in data
    sp = data["series_playbook"]
    series = sp["series"]
    ids = [s["id"] for s in series]
    for expected in ("guozu-chronicle", "wc-classics", "old-fan-night"):
        assert expected in ids
    assert sp.get("max_per_batch") == 1


def test_topic_selector_has_series_field_and_rule():
    text = open(TOPIC_SELECTOR, encoding="utf-8").read()
    assert "series_id" in text
    assert "连载栏目引擎" in text
    assert "guozu-chronicle" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
