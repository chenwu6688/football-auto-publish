#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""队名归一 + 单一判定（2026-11-06 配图相关性阻断事故的回归钉）。

事故链（当天 noon 批次 0 篇）：
  直播吧把「布拉格斯拉维亚」写成「拉格斯拉维亚」
  → data_chart.resolve_team 认不出，anchor 存下脏名、图上印错字
  → candidates 用裸 `in` 放行（残缺名恰好是完整名的子串）
  → 验收侧 hits_all 判它「实体不在正文」→ 硬项 E 失败 → 发布整段跳过

所以这里钉三件事：
  1. 残缺中文名能归一到规范名，但**认不准就不猜**（未知名 / 2 字短名 / 多命中）
  2. 串味消解不能因为归一而退化（国际米兰 ≠ AC米兰）
  3. 绘图侧与验收侧必须是同一个判定（同一张图不会一边过一边不过）
"""

from __future__ import annotations

import data_chart as dc
import image_relevance as ir


def test_truncated_name_normalized():
    """缺字的中文队名要归一——图上的名字必须是登记过的规范名。"""
    assert dc.normalize_team_zh("拉格斯拉维亚") == "布拉格斯拉维亚"
    assert dc.normalize_team_zh("布拉格斯拉维亚") == "布拉格斯拉维亚"
    assert dc.normalize_team_zh("斯拉维亚") == "布拉格斯拉维亚"
    print("   PASS test_truncated_name_normalized")


def test_unknown_is_not_guessed():
    """认不出来就返回 None，绝不硬凑一个最像的。

    这是本项目最贵的教训：把「不知道」写成 0 或写成猜测，下游全被污染。
    """
    assert dc.normalize_team_zh("未知队伍XYZ") is None
    assert dc.normalize_team_zh("米") is None          # 2 字不做包含匹配
    assert dc.normalize_team_zh("") is None
    print("   PASS test_unknown_is_not_guessed")


def test_no_cross_team_confusion():
    """归一不能把「米兰」放宽到命中「国际米兰」。"""
    ok, missing = ir.hits_all("国际米兰1-0小胜", ["米兰"])
    assert ok is False, "国米正文不该命中 AC米兰 的锚点"
    assert missing == ["米兰"]

    ok2, missing2 = ir.hits_all("AC米兰1-0小胜", ["米兰"])
    assert ok2 is True and missing2 == []
    print("   PASS test_no_cross_team_confusion")


def test_both_sides_share_one_judgement():
    """绘图侧（candidates）与验收侧（image_relevance）必须同判。

    同一份 anchor + 同一份正文，两边结论必须一致；否则就会出现
    「图入了正文却过不了验收」——11-06 的硬项失败就是这么来的。
    """
    art_title = "阿森纳客场2-1拿下斯拉维亚，阿尔特塔这换人差点玩脱"
    art = {"title": art_title, "content": art_title}
    for anchor_entities in (["阿森纳", "布拉格斯拉维亚"],
                            ["阿森纳", "拉格斯拉维亚"]):
        anchor = {"kind": "score", "entities": anchor_entities,
                  "numbers": ["1", "2"], "source": "公开赛事比分"}
        gate_ok, _ = ir.hits_all(f"{art['title']}\n{art['content']}", anchor_entities)
        assert gate_ok is True, f"{anchor_entities} 应当命中（正文写的就是这支队）"
        assert ir.check(anchor, art)["ok"] is True
    print("   PASS test_both_sides_share_one_judgement")


def test_anchor_writes_canonical_name(tmp_path):
    """残缺 fixture 名画出来的图，anchor 必须是规范名（否则图上印错字）。"""
    ok_font, why = dc.font_status()
    if not ok_font:
        print(f"   SKIP test_anchor_writes_canonical_name（无中文字体：{why}）")
        return

    title = "阿森纳客场2-1拿下斯拉维亚"
    art = {"title": title, "content": title + "，阿尔特塔的换人差点玩脱。"}
    md = {"all_fixtures": [{"home_team": "拉格斯拉维亚", "away_team": "阿森纳",
                            "home_score": 1, "away_score": 2}]}
    cands = dc.candidates(art, md, out_dir=tmp_path, prefix="norm")
    assert cands, "两队都在正文里，应该能出一张数据图"
    ents = cands[0]["anchor"]["entities"]
    assert "布拉格斯拉维亚" in ents, f"anchor 必须是规范名，实际 {ents}"
    assert "拉格斯拉维亚" not in ents, "残缺名不得进入 anchor"
    print("   PASS test_anchor_writes_canonical_name")


def test_unrelated_fixture_still_rejected(tmp_path):
    """正文没提这支队，就不许出图（归一是纠错，不是放宽）。"""
    art = {"title": "皇马冬窗想签中卫", "content": "皇马正在评估后防配置。"}
    md = {"all_fixtures": [{"home_team": "拉格斯拉维亚", "away_team": "阿森纳",
                            "home_score": 1, "away_score": 2}]}
    assert dc.candidates(art, md, out_dir=tmp_path, prefix="miss") == []
    print("   PASS test_unrelated_fixture_still_rejected")


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    test_truncated_name_normalized()
    test_unknown_is_not_guessed()
    test_no_cross_team_confusion()
    test_both_sides_share_one_judgement()
    with tempfile.TemporaryDirectory() as td:
        test_anchor_writes_canonical_name(Path(td))
        test_unrelated_fixture_still_rejected(Path(td))
    print("ALL PASS")
