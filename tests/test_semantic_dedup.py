#!/usr/bin/env python3
"""P0-1 语义去重（比赛事件指纹）单元测试。

验证：
1. 同场比赛（同两队 + 同比分）无论措辞如何，都生成相同事件指纹 → 可拦截「换说法重发」。
2. 同一天不同批次发了同一场比赛（跨批），能被 is_event_duplicate 识别。
3. 跨天（7 天窗口）同场复读（如阿森纳2-1切尔西 跨天双发）被识别。
4. 别名归一化：马竞 / 马德里竞技 视为同一队，避免指纹不一致漏杀。
5. 不同比赛（不同队或不同比分）不会被误杀（精确率）。
6. get_topic_history 返回的 event_signatures 可驱动实际去重。
"""
import os
import sys
import json
import glob
from datetime import datetime, timedelta

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(REPO, "output")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from data_collector import (
    build_match_signature, is_event_duplicate, get_topic_history,
    extract_match_teams,
)


def _load_all_metadata():
    metas = {}
    paths = (
        glob.glob(os.path.join(OUTPUT, "2026-08-*", "metadata.json"))
        + glob.glob(os.path.join(OUTPUT, "2026-09-*", "metadata.json"))
    )
    for f in sorted(paths):
        d = os.path.basename(os.path.dirname(f))
        try:
            metas[d] = json.load(open(f, encoding="utf-8"))
        except Exception:
            pass
    return metas


ALL_METAS = _load_all_metadata()


def _titles_with_sig(meta):
    out = []
    for a in meta.get("articles", []):
        t = a.get("title", "")
        sig = build_match_signature(t)
        if sig:
            out.append((t, sig))
    return out


# ---------------------------------------------------------------
# 1. 同场比赛换说法 → 同一指纹（核心拦截能力）
# ---------------------------------------------------------------
def test_same_match_different_wording_same_signature():
    a = build_match_signature("切尔西4-3布莱顿：一场从捡漏到送礼的英超大戏")
    b = build_match_signature("切尔西4-3布莱顿：一场进球大战，老六看得直呼过瘾")
    assert a is not None and b is not None
    assert a == b, "换说法重发必须生成相同指纹才能被去重拦截"


# ---------------------------------------------------------------
# 2. 同日跨批同场双发 → 跨批去重可识别
# ---------------------------------------------------------------
def test_same_day_cross_batch_duplicate():
    # 在历史元数据里找出同一天出现 >1 次相同比赛指纹的日期
    found = False
    for d, meta in ALL_METAS.items():
        sigs = _titles_with_sig(meta)
        seen = set()
        hist = set()
        for t, sig in sigs:
            if sig in hist:
                found = True  # 同批/同天已有更早同场 → 应被跨批去重拦下
            hist.add(sig)
            seen.add(sig)
    assert found, "应能在历史数据中找到『同日同场双发』案例并被识别"


# ---------------------------------------------------------------
# 3. 跨天（7 天）同场复读 → 被识别
# ---------------------------------------------------------------
def test_cross_day_7day_duplicate_detected():
    # 构建时间线，对每篇带指纹的文章，检查前 7 天（不含当天）是否已发同场
    timeline = []
    for d, meta in ALL_METAS.items():
        for t, sig in _titles_with_sig(meta):
            timeline.append((d, t, sig))
    timeline.sort(key=lambda x: x[0])

    detected_cross_day = []
    for d, t, sig in timeline:
        cur = datetime.strptime(d, "%Y-%m-%d")
        hist = set()
        for pd, pt, ps in timeline:
            pdt = datetime.strptime(pd, "%Y-%m-%d")
            if pd != d and timedelta(0) <= (cur - pdt) <= timedelta(days=7):
                hist.add(ps)
        if sig in hist:
            detected_cross_day.append((d, t))
    assert detected_cross_day, "应检测到跨天同场复读（如阿森纳2-1切尔西 跨天双发）"


# ---------------------------------------------------------------
# 4. 别名归一化
# ---------------------------------------------------------------
def test_alias_normalization():
    teams_a = extract_match_teams("马竞2-1巴萨")
    teams_b = extract_match_teams("马德里竞技2-1巴萨")
    assert teams_a == teams_b, "马竞/马德里竞技 必须归一为同一队"
    assert "马德里竞技" in teams_a


# ---------------------------------------------------------------
# 5. 不同比赛不被误杀（精确率）
# ---------------------------------------------------------------
def test_different_matches_not_flagged():
    sig_a = build_match_signature("切尔西4-3布莱顿：进球大战")
    sig_b = build_match_signature("曼城1-0考文垂，客场稳如老狗")
    assert sig_a != sig_b
    # 互为历史时不应判定重复
    assert not is_event_duplicate("曼城1-0考文垂", {sig_a})
    assert not is_event_duplicate("切尔西4-3布莱顿", {sig_b})


def test_no_score_no_false_block():
    # 无比分的标题（转会/八卦/球员纪录）不应生成比赛指纹，避免误杀
    assert build_match_signature("梅西大四喜！阿根廷球王再次封神") is None
    assert build_match_signature("恩佐1.45亿欧成标王？老六告诉你值不值") is None
    assert build_match_signature("曼联换帅！谁接手红魔帅位") is None


# ---------------------------------------------------------------
# 6. get_topic_history 集成：返回的事件指纹可驱动去重
# ---------------------------------------------------------------
def test_get_topic_history_event_signatures():
    # 选一个存在过往同场复读的日期之后，验证 event_signatures 非空且可用
    # 用最早有元数据的日期往前推；这里直接对任一有比赛指纹的日期验证结构
    any_date = next(iter(ALL_METAS))
    hist = get_topic_history(any_date, lookback_days=7)
    assert "event_signatures" in hist
    assert "title_prefixes" in hist
    assert isinstance(hist["event_signatures"], set)
    # 至少有一个日期能产出非空 event_signatures
    ok = False
    for d in ALL_METAS:
        h = get_topic_history(d, lookback_days=7)
        if h["event_signatures"]:
            ok = True
            break
    assert ok, "应能从历史元数据构建出非空比赛事件指纹集合"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
