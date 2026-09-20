"""维度1 品牌手册单一事实源 —— 加载/渲染/派生 单元测试。

覆盖：
- load_brand_manual() 从 config/brand_manual.yaml 渲染出非空文本
- 渲染文本包含关键分节（persona / redlines / columns / hook strategy）
- _render_brand_manual_node 对 dict/list/scalar 的递归渲染
- get_brand_style_guide() 从 columns 派生，且保留三大核心类型兜底
"""
import os
import sys

import pytest

# 允许直接以脚本方式运行（tests 目录在 sys.path 之外时）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import (  # noqa: E402
    _get_brand_manual_data,
    _render_brand_manual,
    _render_brand_manual_node,
    get_brand_style_guide,
    load_brand_manual,
)


def test_get_brand_manual_data_is_dict():
    data = _get_brand_manual_data()
    assert isinstance(data, dict)
    assert "persona" in data
    assert "columns" in data


def test_load_brand_manual_non_empty():
    block = load_brand_manual()
    assert isinstance(block, str)
    assert len(block) > 200  # 手册内容应有实质篇幅


def test_load_brand_manual_contains_sections():
    block = load_brand_manual()
    # 渲染后的分节标题（下划线转空格）：persona -> "persona"
    assert "### persona" in block
    assert "### redlines" in block
    assert "### columns" in block
    assert "### hook strategy" in block


def test_render_node_dict_list_scalar():
    node = {
        "a": "标量值",
        "b": ["列表项1", "列表项2"],
        "c": {"嵌套键": "嵌套值"},
    }
    out = _render_brand_manual_node(node, 0)
    text = "\n".join(out)
    assert "- a：标量值" in text
    assert "- 列表项1" in text
    assert "- b：" in text
    assert "- 嵌套键：嵌套值" in text


def test_get_brand_style_guide_derives_and_falls_back():
    sg = get_brand_style_guide()
    # 三大核心类型应来自 YAML columns（与兜底一致）
    assert "热点球评" in sg
    assert "转会资讯" in sg
    assert "八卦趣事" in sg
    # 新增栏目（排行榜/战术解析）也应被纳入
    assert "排行榜" in sg
    assert "战术解析" in sg
    # 兜底默认值存在（未知类型可回退）
    assert sg.get("未知类型", "自然口语化中文写作") == "自然口语化中文写作"


def test_render_brand_manual_top_level_headings():
    data = _get_brand_manual_data()
    block = _render_brand_manual(data)
    # 顶层键应渲染为 ### 标题（下划线在渲染时转为空格）
    for key in ("persona", "voice rules", "stances", "emotional tone", "redlines", "columns", "hook strategy", "type ratios"):
        assert f"### {key}" in block


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
