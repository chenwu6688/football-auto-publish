"""单事件多切口（P0-2）单测。

背景：2026-10-26 金球奖当天，我爱英超发了 15 条、我方 1 条。头部那 15 条反推出
六类切口（结果/冲突/人物情绪/影响/历史对比/数据）。本组测试钉住三件事：

1. 角度判定别退化成「什么都是数据角度」（「球/场/分」几乎出现在所有足球标题里）；
2. 只有 S 级 + 今天 + 有已核实事实才触发（否则每天都是多切口日，重回 10 月超发老路）；
3. 编排真的占配额、真的做去重、真的带标签——不是多了个没人调用的模块。
"""
import pytest

import event_cluster


# ---------------------------------------------------------------- 角度池
def test_six_angles_defined():
    assert len(event_cluster.ANGLES) == 6
    assert set(event_cluster.ANGLES) == {"结果", "冲突", "人物情绪", "影响", "历史对比", "数据"}


def test_every_angle_has_hint():
    assert all(event_cluster.angle_hint(a) for a in event_cluster.ANGLES)


@pytest.mark.parametrize("title,angle", [
    ("凯恩拿下2026金球奖，第70届颁奖礼在伦敦落幕", "结果"),
    ("73球！凯恩这个金球奖的数据有多硬", "数据"),
    ("上一次英格兰人拿金球，还是2001年的欧文", "历史对比"),
    ("凯恩上台第一句谢的是穆里尼奥", "人物情绪"),
    ("这座奖杯之后，拜仁更衣室要变天了", "影响"),
    ("梅西不服？金球奖评选再次引发争议", "冲突"),
])
def test_classify_real_headlines(title, angle):
    assert event_cluster.classify_angle(title) == angle


def test_data_angle_does_not_hijack_everything():
    """「球」字几乎在每个足球标题里都有，数据角度必须排在最后，不许抢走冲突/历史。"""
    assert event_cluster.classify_angle("梅西不服？金球奖评选再次引发争议") == "冲突"
    assert event_cluster.classify_angle("上一次英格兰人拿金球，还是2001年的欧文") == "历史对比"


def test_classify_empty_and_no_signal():
    assert event_cluster.classify_angle("") is None
    assert event_cluster.classify_angle("今天的天气不错") is None


def test_classify_accepts_angle_name():
    """plan_angles 的 used 既可能是标题也可能是角度名，两种都要认。"""
    assert event_cluster.classify_angle("影响") == "影响"


# ---------------------------------------------------------------- S 级
def test_is_s_event_requires_explicit_level():
    assert event_cluster.is_s_event({"level": "S"}) is True
    assert event_cluster.is_s_event({"level": "s"}) is True
    assert event_cluster.is_s_event({"level": "A"}) is False


def test_awareness_a_is_not_s_event():
    """认知度 A 是「多少人知道」，不是「事件多大」——混用会让每天都变多切口日。"""
    assert event_cluster.is_s_event({"awareness": "A"}) is False


def test_s_events_requires_today_and_facts():
    cal = {"nodes": [
        {"id": "ok", "date": "2026-11-01", "level": "S", "label": "有事实", "facts": ["f1"]},
        {"id": "no_facts", "date": "2026-11-01", "level": "S", "label": "无事实"},
        {"id": "future", "date": "2026-11-05", "level": "S", "label": "未来", "facts": ["f"]},
        {"id": "not_s", "date": "2026-11-01", "awareness": "A", "label": "普通", "facts": ["f"]},
    ]}
    assert [e["node_id"] for e in event_cluster.s_events("2026-11-01", cal)] == ["ok"]


def test_s_events_missing_calendar_returns_empty(tmp_path):
    assert event_cluster.s_events("2026-11-01", {"nodes": []}) == []


# ---------------------------------------------------------------- 角度计划
def _event(facts=True):
    return {"node_id": "ballon", "event_key": "node:ballon", "label": "金球奖",
            "column": "人物专栏" if False else "人物故事",
            "facts": ["凯恩 73 球"] if facts else []}


def test_plan_angles_capped():
    plan = event_cluster.plan_angles(_event())
    assert len(plan) == event_cluster.MAX_ANGLES_PER_EVENT
    assert all(p["angle"] in event_cluster.ANGLES for p in plan)


def test_plan_angles_respects_used():
    plan = event_cluster.plan_angles(_event(), used_titles=["凯恩拿下2026金球奖"])
    assert "结果" not in [p["angle"] for p in plan]


def test_plan_angles_never_duplicates():
    ev = _event()
    first = [p["angle"] for p in event_cluster.plan_angles(ev)]
    second = [p["angle"] for p in event_cluster.plan_angles(ev, used_titles=first)]
    assert not (set(first) & set(second))


def test_plan_angles_needs_facts():
    """没有已核实事实就不许多切口——三个角度说三遍空话，比不写更差。"""
    assert event_cluster.plan_angles(_event(facts=False)) == []


def test_plan_angles_carries_facts_and_hint():
    plan = event_cluster.plan_angles(_event())
    assert plan[0]["facts"] == ["凯恩 73 球"]
    assert plan[0]["angle_hint"]


# ---------------------------------------------------------------- 去重
def test_distinct_enough_same_signature():
    r = event_cluster.distinct_enough(["没想到罗马主帅这场压力比尤文还大",
                                       "没想到拉齐奥主帅这场压力比米兰还大"])
    assert r["ok"] is False and r["dups"]


def test_distinct_enough_same_head_ignores_punctuation():
    r = event_cluster.distinct_enough(["金球奖揭晓凯恩夺魁73球",
                                       "金球奖揭晓，凯恩这次赢得干净"])
    assert r["ok"] is False and r["heads"]


def test_distinct_enough_allows_different():
    r = event_cluster.distinct_enough(["第94分钟丢球，阿森纳遭读秒绝杀",
                                       "让3追4！拜仁30分钟连轰4球完成大逆转"])
    assert r["ok"] is True


def test_distinct_enough_empty():
    r = event_cluster.distinct_enough([])
    assert r["ok"] is True and r["n"] == 0


# ---------------------------------------------------------------- 事件键
def test_event_key_prefixes():
    assert event_cluster.event_key_of({"node_id": "x"}) == "node:x"
    assert event_cluster.event_key_of({"_event_result_key": "k"}) == "k"
    # 不做关键词聚类：认不出就返回 None，宁可少触发，也不把两件事合成一件
    assert event_cluster.event_key_of({"title": "某标题"}) is None


# ---------------------------------------------------------------- 编排接入
def test_add_event_angle_articles_respects_cap(monkeypatch):
    """占配额不超发：cap=2 时最多出 2 篇，哪怕事件角度有 3 个。"""
    import orchestrator as oc

    monkeypatch.setattr(event_cluster, "s_events", lambda *a, **k: [{
        "node_id": "n1", "event_key": "node:n1", "label": "金球奖",
        "node_date": "2026-11-01", "column": "人物故事", "facts": ["f1"]}])
    counter = {"i": 0}

    def fake_gen(topic, **kw):
        counter["i"] += 1
        return {"title": f"第{counter['i']}分钟打进关键球，比赛走势全变了", "content": "x" * 300,
                "content_type": "人物故事", "angle": topic.get("angle")}

    monkeypatch.setattr(oc, "generate_node_angle_article", fake_gen)
    monkeypatch.setattr(oc, "search_images", lambda *a, **k: [])
    monkeypatch.setattr(oc, "warn_if_low_info_increment", lambda *a, **k: None)
    monkeypatch.setattr(event_cluster, "audit", lambda *a, **k: None)

    articles, topics, stats = [], [], {"generated": 0, "valid": 0}
    n = oc._add_event_angle_articles(
        articles=articles, topics=topics, images_map={}, stats=stats,
        added=0, cap=2, date_str="2026-11-01", batch_mode="auto",
        topic_history={}, avoid_sigs=set())
    assert n == 2 and len(articles) == 2


def test_add_event_angle_articles_zero_cap_does_nothing(monkeypatch):
    import orchestrator as oc

    monkeypatch.setattr(event_cluster, "s_events", lambda *a, **k: [{
        "node_id": "n1", "event_key": "node:n1", "label": "金球奖",
        "node_date": "2026-11-01", "column": "人物故事", "facts": ["f1"]}])
    called = {"n": 0}

    def fake_gen(*a, **k):
        called["n"] += 1
        return {"title": "x", "content": "y" * 300}

    monkeypatch.setattr(oc, "generate_node_angle_article", fake_gen)
    n = oc._add_event_angle_articles(
        articles=[], topics=[], images_map={}, stats={"generated": 0, "valid": 0},
        added=0, cap=0, date_str="2026-11-01", batch_mode="auto",
        topic_history={}, avoid_sigs=set())
    assert n == 0 and called["n"] == 0


def test_add_event_angle_articles_no_s_event_no_llm(monkeypatch):
    """没有 S 级事件时一次 LLM 都不该调（成本纪律）。"""
    import orchestrator as oc

    monkeypatch.setattr(event_cluster, "s_events", lambda *a, **k: [])
    called = {"n": 0}

    def fake_gen(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(oc, "generate_node_angle_article", fake_gen)
    n = oc._add_event_angle_articles(
        articles=[], topics=[], images_map={}, stats={"generated": 0, "valid": 0},
        added=0, cap=3, date_str="2026-11-01", batch_mode="auto",
        topic_history={}, avoid_sigs=set())
    assert n == 0 and called["n"] == 0


def test_add_event_angle_articles_drops_duplicate_title(monkeypatch):
    """同事件两篇标题撞车 → 后一篇丢弃，不许靠改标题凑数。"""
    import orchestrator as oc

    monkeypatch.setattr(event_cluster, "s_events", lambda *a, **k: [{
        "node_id": "n1", "event_key": "node:n1", "label": "金球奖",
        "node_date": "2026-11-01", "column": "人物故事",
        "facts": ["f1", "f2"]}])
    monkeypatch.setattr(oc, "generate_node_angle_article",
                        lambda *a, **k: {"title": "金球奖揭晓凯恩夺魁73球",
                                         "content": "x" * 300, "content_type": "人物故事"})
    monkeypatch.setattr(oc, "search_images", lambda *a, **k: [])
    monkeypatch.setattr(oc, "warn_if_low_info_increment", lambda *a, **k: None)
    monkeypatch.setattr(event_cluster, "audit", lambda *a, **k: None)
    monkeypatch.setattr(oc.degrade, "note", lambda *a, **k: None)

    articles = []
    n = oc._add_event_angle_articles(
        articles=articles, topics=[], images_map={},
        stats={"generated": 0, "valid": 0}, added=0, cap=3,
        date_str="2026-11-01", batch_mode="auto", topic_history={}, avoid_sigs=set())
    assert n == 1 and len(articles) == 1


def test_add_event_angle_articles_tags_supply_layer(monkeypatch):
    """标签必须显式：否则配比统计会把它算成「未分层」。"""
    import orchestrator as oc

    monkeypatch.setattr(event_cluster, "s_events", lambda *a, **k: [{
        "node_id": "n1", "event_key": "node:n1", "label": "金球奖",
        "node_date": "2026-11-01", "column": "人物故事", "facts": ["f1"]}])
    monkeypatch.setattr(oc, "generate_node_angle_article",
                        lambda *a, **k: {"title": "第94分钟绝杀改变一切", "content": "x" * 300,
                                         "content_type": "人物故事"})
    monkeypatch.setattr(oc, "search_images", lambda *a, **k: [])
    monkeypatch.setattr(oc, "warn_if_low_info_increment", lambda *a, **k: None)
    monkeypatch.setattr(event_cluster, "audit", lambda *a, **k: None)

    topics = []
    oc._add_event_angle_articles(
        articles=[], topics=topics, images_map={},
        stats={"generated": 0, "valid": 0}, added=0, cap=1,
        date_str="2026-11-01", batch_mode="auto", topic_history={}, avoid_sigs=set())
    assert topics and topics[0]["_supply_layer"] == "一级·确定性·事件多切口"
    assert topics[0]["_event_key"] == "node:n1"
