"""维度4 情绪共鸣升级 —— 单元测试。

覆盖：
- _build_resonance_hint：真实角度返回提示；"无"/缺省返回空串
- warn_resonance_coverage：空列表早退；样本覆盖率统计与告警（capsys 捕获打印）
- 品牌手册含 resonance_playbook 且四角度齐全
- topic_selector.txt 输出格式含 resonance_angle 字段且四角度名出现
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import (  # noqa: E402
    _build_resonance_hint,
    _get_brand_manual_data,
    warn_resonance_coverage,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPIC_SELECTOR = os.path.join(PROJECT_ROOT, "prompts", "topic_selector.txt")


def test_build_resonance_hint_real_angle():
    hint = _build_resonance_hint({"resonance_angle": "国足情结"})
    assert "国足情结" in hint
    assert "共鸣角度" in hint


def test_build_resonance_hint_none_values():
    assert _build_resonance_hint({"resonance_angle": "无"}) == ""
    assert _build_resonance_hint({"resonance_angle": "无（确实难共鸣时）"}) == ""
    assert _build_resonance_hint({}) == ""
    assert _build_resonance_hint(None) == ""


def test_warn_resonance_coverage_empty():
    # 空列表应直接返回，不抛异常
    warn_resonance_coverage([])


def test_warn_resonance_coverage_low_and_high(capsys):
    low = [
        {"resonance_angle": "无"},
        {"resonance_angle": ""},
        {"resonance_angle": "国足情结"},
    ]
    warn_resonance_coverage(low)
    out = capsys.readouterr().out
    assert "共鸣角度覆盖" in out
    assert "⚠️" in out  # 1/3 < 60% 应告警

    capsys.readouterr()  # 清空
    high = [
        {"resonance_angle": "国足情结"},
        {"resonance_angle": "老球迷身份认同"},
        {"resonance_angle": "世界杯经典时刻"},
    ]
    warn_resonance_coverage(high)
    out = capsys.readouterr().out
    assert "✅" in out  # 3/3 ≥ 60% 应达标


def test_brand_manual_has_resonance_playbook():
    data = _get_brand_manual_data()
    assert "resonance_playbook" in data
    angles = data["resonance_playbook"]["angles"]
    names = [a["name"] for a in angles]
    for expected in ("国足情结", "老球迷身份认同", "世界杯经典时刻", "名帅名宿沉浮"):
        assert expected in names


def test_topic_selector_has_resonance_angle_field():
    text = open(TOPIC_SELECTOR, encoding="utf-8").read()
    assert "resonance_angle" in text
    for angle in ("国足情结", "老球迷身份认同", "世界杯经典时刻", "名帅名宿沉浮"):
        assert angle in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
