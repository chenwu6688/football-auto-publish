"""节点预生成的「已核实事实」注入单测（2026-10-26）。

背景：`scripts/pregen_deterministic.py` 长期只敢写赛制常识、不敢碰具体数字——
因为一旦写数字就有编造风险。结果节点稿空泛得像信息播报（"金球奖还有几天"这种）。
修法是把**人工交叉核对过**的事实作为唯一事实源喂进提示词，并在提示词里把边界写死：
区块之外的具体数字/比分/名单一律不许出现。

这组用例守住三件事：
  1. 没有 facts 时提示词与旧版**完全一致**（不能因为加功能让老节点稿变形）；
  2. 有 facts 时逐条出现、顺序不变、来源可见；
  3. 提示词里必须有「不得改写/不得加码」的硬约束——否则模型会把 36 球写成 40 球。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pregen_deterministic as pg  # noqa: E402


def _topic(**kw):
    base = {"node_type": "赛季节点", "node_label": "金球奖颁奖礼",
            "node_date": "2026-10-26", "days_to": 0,
            "conflict": "奖项要揭晓了，谁更该拿", "fact_source": {}}
    base.update(kw)
    return base


def test_no_facts_returns_empty_block():
    """没有 facts → 空串，提示词不留任何多余空白区块。"""
    assert pg._facts_block(_topic()) == ""
    assert pg._facts_block(_topic(facts=[])) == ""
    assert pg._facts_block(_topic(facts=["", "   "])) == ""


def test_facts_block_lists_every_fact_in_order():
    facts = ["凯恩上赛季德甲 31 场 36 球", "亚马尔随西班牙夺得世界杯"]
    block = pg._facts_block(_topic(facts=facts))
    assert "已核实事实" in block
    assert block.index(facts[0]) < block.index(facts[1]), "事实顺序必须与日历一致"
    assert "1. " in block and "2. " in block


def test_facts_block_carries_source_url():
    block = pg._facts_block(_topic(facts=["某条事实"],
                                   fact_source={"url": "https://example.com/a"}))
    assert "https://example.com/a" in block


def test_block_hardens_the_boundary():
    """边界必须写进提示词：区块之外不许出现数字，区块之内不许改写。"""
    block = pg._facts_block(_topic(facts=["36 球"]))
    assert "只能用下面这些" in block
    assert "一律不许出现" in block


def test_prompt_contains_anti_rewrite_rules():
    """模板里必须两条都在：无事实时不写数字；有事实时必须原样用。"""
    tmpl = pg._TMPL
    assert "若无【已核实事实】区块" in tmpl
    assert "若有【已核实事实】区块" in tmpl
    assert "不得改写" in tmpl


def test_prompt_renders_facts_block_without_keyerror():
    """_TMPL 增加占位符后，_make_one 的渲染不能 KeyError（历史踩过）。"""
    text = pg._TMPL.format(
        node_type="赛季节点", label="金球奖颁奖礼", node_date="2026-10-26",
        days_to=0, conflict="谁更该拿", content_type="人物故事",
        facts_block=pg._facts_block(_topic(facts=["36 球"])))
    assert "36 球" in text
    assert "{facts_block}" not in text


def test_no_facts_prompt_has_no_facts_section():
    """无 facts 的节点，提示词里不该出现「事实清单」区块头。

    注意：硬约束那两句本身就带「【已核实事实】」字样（那是给模型看的条件分支），
    所以这里只断言区块标题——否则会把规则文本当成注入成功，测试永远红。
    """
    text = pg._TMPL.format(
        node_type="转会窗截止日", label="冬窗关闭", node_date="2027-02-02",
        days_to=3, conflict="谁最着急", content_type="转会动态", facts_block="")
    assert "【已核实事实（只能用下面这些" not in text
    assert "1. " not in text
