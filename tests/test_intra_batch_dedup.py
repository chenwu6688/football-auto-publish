#!/usr/bin/env python3
"""批内去重（2026-11-06 收敛后的口径）。

原来的 `_check_intra_batch_dedup` 只**告警不删题**，真正删题的是紧跟其后的
「关键词重叠 ≥50% 就丢分数低的那条」—— 而关键词是 LLM 自由生成的：
换个说法就绕过，同一支球队不同事情就误杀。

现在这条逻辑并进了 `dedup.apply`，判据换成与措辞无关的两条：
**同一场比赛（对阵键）** 与 **同一件事（实体 + 事件族）**。
批内重复不需要单独一道过滤 —— 逐条判定时把已保留候选的键加进集合，
第二条自然就被挡住。

本文件同时钉住「光写提示词不算数」：`select_topics` 必须真的调用了一遍去重。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import dedup  # noqa: E402


def _ctx(**kw):
    return dedup.build_context(**kw)


def T(title, kws=None, kws_cn=None):
    return {"title": title, "keywords": kws or [], "keywords_cn": kws_cn or []}


def test_different_topics_pass():
    """两支不同球队、不同事件 → 全留。"""
    kept, st = dedup.apply([
        T("阿森纳1-0曼联取三分", ["Arsenal", "ManUtd"]),
        T("姆巴佩转会皇马", ["Mbappe", "RealMadrid"]),
    ], _ctx())
    assert len(kept) == 2, st
    assert st["同场"] == 0 and st["同事件"] == 0


def test_same_fixture_in_batch_dropped():
    """批内同一场比赛换个措辞 → 只留一条（与语言无关）。"""
    kept, st = dedup.apply([
        T("阿森纳1-0曼联取三分", ["Arsenal", "ManUtd"]),
        T("阿森纳又赢了曼联", ["ManUtd", "Arsenal"]),
    ], _ctx())
    assert len(kept) == 1 and st["同场"] == 1, st


def test_same_event_across_wording_dropped():
    """同一个事件换个说法 → 只留一条。

    「宣布退出国家队」与「退役」在措辞上毫无重叠，旧的关键词判据算不出来；
    事件键（C罗 + retire）能。
    """
    kept, st = dedup.apply([
        T("C罗宣布退出国家队", ["C罗"]),
        T("C罗退役后首度发声", ["C罗", "国家队"]),
    ], _ctx())
    assert len(kept) == 1 and st["同事件"] == 1, st


def test_same_team_different_event_kept():
    """同一支球队、不同事件 → 必须留（旧判据会误杀）。"""
    kept, st = dedup.apply([
        T("那不勒斯主帅下课", ["那不勒斯", "意甲", "主帅"]),
        T("那不勒斯签下新前锋", ["那不勒斯", "意甲", "转会"]),
    ], _ctx())
    assert len(kept) == 2, st


def test_coverage_by_fixture_pairs():
    """今日已发同一场 → 丢掉（对阵键跨语言）。"""
    ctx = _ctx(cross_batch_covered={"fixture_pairs": {frozenset(["阿森纳", "曼联"])}})
    kept, st = dedup.apply([T("阿森纳1-0曼联取三分", ["Arsenal", "ManUtd"])], ctx)
    assert kept == [] and st["同场"] == 1, st


def test_coverage_by_event_key():
    """今日已发同一事件 → 丢掉（主稿通道也要看事件键，不能只靠热点通道）。"""
    ctx = _ctx(cross_batch_covered={"titles": {"C罗宣布退出国家队"}})
    kept, st = dedup.apply([T("C罗退役后首度发声", ["C罗"])], ctx)
    assert kept == [] and st["同事件"] == 1, st


def test_keyword_overlap_only_warns():
    """关键词重叠只告警、不删题（这是本轮最重要的口径变化）。"""
    ctx = _ctx(yesterday_keywords={"那不勒斯", "意甲", "主帅"})
    kept, st = dedup.apply([T("那不勒斯主帅赛后发声", ["那不勒斯", "意甲", "主帅"])], ctx)
    assert len(kept) == 1, "关键词重叠不得再作为删除判据"
    assert st["弱判据告警"] == 1, "但必须留痕，否则日后无法判断该不该恢复硬拦"


def test_empty_list_ok():
    kept, st = dedup.apply([], _ctx())
    assert kept == [] and st["同场"] == 0


def test_select_topics_prompt_and_call():
    """提示词要求 + 代码里真的跑了一遍去重。"""
    import inspect

    import orchestrator as orch

    src = inspect.getsource(orch.select_topics)
    assert "不同的事件" in src, "Prompt 必须要求每篇是不同事件（不能是同一场比赛的多个角度）"
    assert "核心关键词集合" in src, "Prompt 必须要求核心关键词集合交集为空"
    assert "dedup.apply(" in src, "select_topics 必须真正跑一遍去重（LLM 不一定听话）"
    assert "_check_intra_batch_dedup(" not in src, "旧的批内去重实现必须已下线，避免两套判据并存"


def test_topic_selector_file_has_dedup_rule():
    """topic_selector.txt must include intra-batch dedup in 禁止选题 section."""
    from utils import load_prompt_template
    tmpl = load_prompt_template("topic_selector.txt")
    assert "同一批次" in tmpl, "topic_selector.txt must mention intra-batch dedup"
    assert "核心关键词" in tmpl, "topic_selector.txt must mention core keyword diversity"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
    print(f"\n{len(tests)} 项，失败 {failed} 项")
    sys.exit(0 if failed == 0 else 1)
