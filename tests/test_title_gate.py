"""标题闸门（P0-1）单测：疑问收尾限额 + 过程锚点口径 + 只重写标题。

全部用例来自 2026-11-01 头部账号对标的真实数据，不是构造出来的假想场景：

    - 我方 76 篇真实标题里 85.5% 以「？/吗/呢」收尾，中位阅读 8，锚点 0 篇；
    - 头部 181 条里疑问句只占 0%~20.2%，锚点覆盖 7.2%~35.7%；
    - 「终结…荒」这类**结果**曾被误算成锚点（实测 4 阅读），已移出词表。

两条最容易改回去的地方也钉了测试：
    1. prompt 里「必须以疑问？收尾」（85.5% 的直接成因）；
    2. 「标题打回 = 重跑整篇」（浪费额度、把已过闸门的正文重新掷骰子）。
"""
import pytest

import title_features
import title_gate


# ---------------------------------------------------------------- 疑问收尾判定
@pytest.mark.parametrize("title", [
    "当年那个追风少年曲波，如今在基层默默带娃，他到底图个啥？",
    "马拉多纳的上帝之手，英格兰到底冤不冤呢",
    "为什么说这场球输得比赢更值?",
    "米兰德比谁先崩？",
])
def test_question_ending_positive(title):
    assert title_gate.is_question_ending(title) is True


@pytest.mark.parametrize("title", [
    "让3追4！拜仁史诗级大逆转，30分钟连轰4球",
    "第94分钟丢球，阿森纳遭读秒绝杀！领先只剩3分",
    # 中段设问是有效手法，不该被判成「疑问收尾」
    "凭什么是他？金球奖这一夜凯恩用73球堵住了所有嘴",
    # 中段有疑问词、句末是陈述 —— 超限只卡句末，不误伤这种
    "米兰德比，到底谁先崩都不好说",
    "德国杯：拜仁6-1大胜马格德堡，多点开花挺进16强",
])
def test_question_ending_negative(title):
    assert title_gate.is_question_ending(title) is False


def test_empty_title_not_question():
    assert title_gate.is_question_ending("") is False
    assert title_gate.is_question_ending("   ") is False


# ---------------------------------------------------------------- 限额
def test_quota_exhausted_at_two():
    hist = ["他到底图个啥？", "这场谁更慌呢"]
    assert title_gate.count_question_endings(hist) == 2
    assert title_gate.quota_exhausted(hist) is True


def test_quota_not_exhausted_with_one():
    assert title_gate.quota_exhausted(["他到底图个啥？", "让3追4！拜仁逆转"]) is False


def test_quota_empty_history_not_exhausted():
    assert title_gate.quota_exhausted([]) is False
    assert title_gate.quota_exhausted(None) is False


# ---------------------------------------------------------------- check
def test_check_blocks_question_when_quota_full():
    r = title_gate.check("这场德比到底谁先崩？", ["他图个啥？", "谁更慌呢"])
    assert r["ok"] is False and r["hit"] is True
    assert r["quota"] == {"limit": 2, "used": 2, "exhausted": True}
    # 理由必须说清「几篇 / 限额几篇」，否则告警里看不出为什么被打回
    assert "2 篇" in r["reason"] and "限额 2" in r["reason"]


def test_check_allows_anchor_title_even_when_quota_full():
    r = title_gate.check("第94分钟丢球，阿森纳遭读秒绝杀！领先只剩3分",
                         ["他图个啥？", "谁更慌呢"])
    assert r["ok"] is True


def test_check_allows_question_when_quota_has_room():
    r = title_gate.check("这场德比到底谁先崩？", ["他图个啥？"])
    assert r["ok"] is True
    assert r["quota"]["used"] == 1


def test_check_without_history_only_checks_format():
    r = title_gate.check("这场德比到底谁先崩？", None)
    assert r["ok"] is True
    assert r["quota"] is None


def test_check_degraded_still_enforces_quota(monkeypatch):
    """配置读不到也要继续判限额（限额是代码常量），只是标记 degraded。

    若这里改成「配置不可用就全放行」，一次 yaml 手误就会让限额静默失效。
    """
    monkeypatch.setattr(title_gate, "load_config", lambda *a, **k: None)
    r = title_gate.check("这场德比到底谁先崩？", ["他图个啥？", "谁更慌呢"])
    assert r["ok"] is False
    assert r["degraded"] is True


def test_check_empty_title_passes():
    assert title_gate.check("", ["他图个啥？", "谁更慌呢"])["ok"] is True


# ---------------------------------------------------------------- 词表一致性
def test_declared_kinds_match_code():
    """config 声明的锚点类别必须与 title_features 一一对应。

    两处各写各的正是 10-24「比分口径三处不一致」熔断事故的成因。
    """
    assert set(title_gate.anchors_declared()) == set(title_gate.available_anchor_kinds())


def test_anchor_wordlist_not_empty():
    assert len(title_features.PROCESS_ANCHOR_PATTERNS) >= 10


def test_anchor_not_result_statement():
    """「终结…荒」是结果不是过程，2026-11-01 自检推翻过（实测 4 阅读）。"""
    assert title_gate.has_anchor("2026金球奖揭晓：凯恩凭73球夺魁，终结英格兰25年奖项荒") is False


def test_anchor_detected_on_real_headline():
    assert title_gate.anchor_kinds("让3追4！拜仁史诗级大逆转") != ()


# ---------------------------------------------------------------- coverage
def test_coverage_empty_sample_is_none_not_zero():
    cov = title_gate.coverage([])
    assert cov["n"] == 0
    assert cov["anchor_coverage"] is None and cov["question_pct"] is None


def test_coverage_computable():
    cov = title_gate.coverage([
        "第94分钟丢球，阿森纳遭读秒绝杀",
        "他到底图个啥？",
        "德国杯：拜仁6-1大胜马格德堡",
    ])
    assert cov["n"] == 3
    assert cov["anchors"] == 1 and cov["anchor_coverage"] == 33.3
    assert cov["question_endings"] == 1 and cov["question_pct"] == 33.3


def test_window_bounds():
    since, until = title_gate.window_bounds("2026-11-01", 14)
    assert until == "2026-11-01"
    assert since == "2026-10-19"


# ---------------------------------------------------------------- 评分器口径
def test_score_title_plan_prefers_anchor_over_empty_question():
    """带过程锚点的标题必须比同长度空问标题分高——这是本轮改口径的核心。"""
    import orchestrator as oc

    anchor = "第94分钟丢球，阿森纳遭读秒绝杀！领先只剩3分"
    empty_q = "阿森纳这场到底是怎么丢的球，防线问题出在哪？"
    s_a, ok_a, _ = oc.score_title_plan({}, anchor, "热点球评")
    s_q, _, _ = oc.score_title_plan({}, empty_q, "热点球评")
    assert ok_a is True
    assert s_a > s_q


def test_score_title_plan_blocks_question_when_quota_full():
    """限额满了以后，即使疑问标题其他维度都不错也要 0 分打回（硬拦，不给它加分复活）。"""
    import orchestrator as oc

    hist = ["他到底图个啥？", "这场谁更慌呢"]
    s, ok, reasons = oc.score_title_plan(
        {}, "阿森纳第94分钟的防守选择，到底该由谁负责？", "热点球评",
        recent_titles=hist)
    assert (s, ok) == (0, False)
    assert any("限额" in r for r in reasons)


def test_score_title_plan_without_history_does_not_block_question():
    """不给 recent_titles 时不判限额——单篇复算（verify_publish_gate 等）不能被误伤。"""
    import orchestrator as oc

    _, _, reasons = oc.score_title_plan({}, "阿森纳这块防线到底该由谁负责？", "热点球评")
    assert not any("限额" in r for r in reasons)


def test_score_fixture_title_question_ending_no_longer_bonus():
    """旧口径「疑问收尾 +30」必须已取消：加个问号不应提高分数。"""
    import orchestrator as oc

    topic = {"home_zh": "阿森纳", "away_zh": "曼城", "板块": "战术榜单"}
    # 两条同字数、同信息量，唯一差别是句末问号——若问号还能加分，说明旧口径没拆干净
    plain = "阿森纳主场迎战曼城，两队中场配置差距全在腰上"
    with_q = "阿森纳主场迎战曼城，两队中场配置到底差在哪？"
    assert len(plain) == len(with_q)
    s_plain, _ = oc.score_fixture_title(topic, plain)
    s_q, _ = oc.score_fixture_title(topic, with_q)
    assert s_q <= s_plain


def test_score_fixture_title_anchor_scores_higher():
    import orchestrator as oc

    topic = {"home_zh": "阿森纳", "away_zh": "曼城", "板块": "战术榜单"}
    plain = "阿森纳主场迎战曼城，两队中场配置差在哪"
    anchor = "阿森纳主场连丢3球，曼城下半场换人后彻底翻盘"
    s_plain, _ = oc.score_fixture_title(topic, plain)
    s_anchor, _ = oc.score_fixture_title(topic, anchor)
    assert s_anchor > s_plain


def test_score_fixture_title_blocks_question_when_quota_full():
    import orchestrator as oc

    topic = {"home_zh": "阿森纳", "away_zh": "曼城"}
    s, reasons = oc.score_fixture_title(
        topic, "阿森纳主场迎战曼城，两队中场配置到底差在哪？",
        recent_titles=["他图个啥？", "谁更慌呢"])
    assert s == 0
    assert any("限额" in r for r in reasons)


# ---------------------------------------------------------------- 只重写标题
def test_repick_title_uses_new_title(monkeypatch):
    import orchestrator as oc

    monkeypatch.setattr(oc, "call_llm_json",
                        lambda *a, **k: ({"title": "第94分钟丢球，阿森纳遭读秒绝杀！领先只剩3分"}, "m"))
    art = {"title": "阿森纳这场到底是怎么丢的球？"}
    new = oc.repick_title({"content_type": "热点球评"}, art, ["缺过程锚点"], "2026-11-01")
    assert new and new != art["title"]
    # 正文一个字都不能被碰
    assert "content" not in art


def test_repick_title_rejects_still_failing_title(monkeypatch):
    """模型给回一个照样不过的标题（同一个空问）→ 必须返回 None，不能将就采用。"""
    import orchestrator as oc

    monkeypatch.setattr(oc, "call_llm_json",
                        lambda *a, **k: ({"title": "阿森纳这场到底是怎么丢的球，问题出在哪？"}, "m"))
    new = oc.repick_title({"content_type": "热点球评"}, {"title": "旧标题"},
                          ["缺过程锚点"], "2026-11-01",
                          recent_titles=["他图个啥？", "谁更慌呢"])
    assert new is None


def test_repick_title_handles_llm_exception(monkeypatch):
    import orchestrator as oc

    def boom(*a, **k):
        raise RuntimeError("网络挂了")

    monkeypatch.setattr(oc, "call_llm_json", boom)
    assert oc.repick_title({}, {"title": "旧标题"}, ["x"], "2026-11-01") is None


def test_repick_title_empty_old_title_returns_none():
    import orchestrator as oc

    assert oc.repick_title({}, {}, ["x"], "2026-11-01") is None


def test_ensure_fixture_title_keeps_content_untouched(monkeypatch):
    """标题重拟只动 title 字段，content 必须原样保留（这是「不重写全文」的实现保证）。"""
    import orchestrator as oc

    art = {"title": "阿森纳主场迎战曼城，两队中场配置到底差在哪",
           "content": "正文原样" * 100}
    monkeypatch.setattr(oc, "call_llm_json",
                        lambda *a, **k: ({"title": "阿森纳连丢3球！曼城下半场换人后彻底翻盘"}, "m"))
    title, score, _ = oc.ensure_fixture_title({"home_zh": "阿森纳", "away_zh": "曼城"},
                                              art, "2026-11-01")
    assert title == art["title"]
    assert art["content"] == "正文原样" * 100
    assert score >= 60


# ---------------------------------------------------------------- 批内累计
def test_session_titles_accumulate():
    """同一批两篇都写成空问时，第二篇必须能看到第一篇——否则限额在批内形同虚设。"""
    import orchestrator as oc

    oc.reset_session_titles()
    try:
        oc.note_generated_title("他到底图个啥？")
        oc.note_generated_title("这场谁更慌呢")
        recent = oc.recent_titles_for_gate("1970-01-01")   # 不存在的日期 → 只有进程内记录
        assert title_gate.quota_exhausted(recent) is True
    finally:
        oc.reset_session_titles()


def test_session_titles_dedup():
    import orchestrator as oc

    oc.reset_session_titles()
    try:
        oc.note_generated_title("同一标题")
        oc.note_generated_title("同一标题")
        assert oc._SESSION_TITLES.count("同一标题") == 1
    finally:
        oc.reset_session_titles()


# ---------------------------------------------------------------- 供给侧不许改回去
def test_prompts_no_longer_force_question_ending():
    """prompt 里「以疑问『？』收尾」是我方 85.5% 空问标题的直接成因，不许写回去。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    text = (root / "orchestrator.py").read_text(encoding="utf-8")
    assert "以疑问『？』收尾" not in text
    assert "必须以疑问？收尾" not in text


def test_selector_prompt_no_longer_demands_60pct_question():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    text = (root / "prompts" / "topic_selector.txt").read_text(encoding="utf-8")
    assert "疑问/悬念类（含「？」" not in text
    assert "过程锚点" in text


# ============================================================
# 出口复核（2026-11-11）：所有生成通道唯一汇聚点也要判 title_gate
# ============================================================
#
# 11-09「曼联4-4维拉，多古补时救主还是遮羞？」漏出去的路径：
# 生成侧 4 处 check 都是「按通道」接的，这条通道没传 recent_titles；
# 而 save_articles_local（所有通道唯一汇聚点）此前根本没有 title_gate 判定，
# metadata 里的 title_gate.pass 还是「不传 recent_titles」的裸分（64 分 pass）。
# 同一份数据在验收侧判违规、在记录侧判通过 —— 比没有记录更坏。

def _load_verify_title_anchor():
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "verify_title_anchor", root / "scripts" / "verify_title_anchor.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_export_gate_since_is_single_source():
    """出口复核生效日只能有一处定义：两个验收脚本都从 title_gate 取。

    同一生效日写两处，早晚只改一处 —— 10-24 已经用一次熔断换过这个教训。
    """
    from pathlib import Path
    assert title_gate.EXPORT_GATE_SINCE == "2026-11-11"
    vta = _load_verify_title_anchor()
    assert vta.QUESTION_CAP_SINCE == title_gate.EXPORT_GATE_SINCE
    gate_src = (Path(__file__).resolve().parent.parent
                / "scripts" / "verify_publish_gate.py").read_text(encoding="utf-8")
    assert "title_gate.EXPORT_GATE_SINCE" in gate_src
    assert '"2026-11-11"' not in gate_src, "把生效日又写死了一遍"


def test_export_gate_wired_detects_real_call(tmp_path):
    """AST 检查必须认「函数体里真的调用」，注释里写一句不算。"""
    from pathlib import Path
    vta = _load_verify_title_anchor()
    real = Path(__file__).resolve().parent.parent / "orchestrator.py"
    assert vta._export_gate_wired(real) is True

    fake = tmp_path / "o.py"
    fake.write_text(
        "def save_articles_local(a):\n"
        "    # title_gate.check(art, recent)  <-- 只写在注释里\n"
        "    return a\n", encoding="utf-8")
    assert vta._export_gate_wired(fake) is False, "注释里提一句就让判据变绿，那是假绿"


def test_title_gate_row_records_quota():
    """metadata 里的 title_gate 必须带 quota，否则看不出「是超限还是压根没判」。"""
    import orchestrator
    hist = ["他到底图个啥？", "这场谁更慌呢"]
    row = orchestrator._title_gate_row(
        {"title": "曼联4-4维拉，多古补时救主还是遮羞？", "content_type": "热点球评"}, "")
    # 不依赖真实历史：只要求字段存在且结构可读（历史为空时 used=0）
    assert "quota" in row and set(row["quota"]) == {"limit", "used", "exhausted"}
    assert row["quota"]["limit"] == title_gate.MAX_QUESTION_PER_WINDOW
    # 同一批内累计：传什么历史由 recent_titles_for_gate 决定，这里只验证字段口径
    assert title_gate.check("这场德比谁先崩？", hist)["ok"] is False


# ============================================================
# cfg 依赖注入（2026-11-11）：`check(..., cfg=...)` 曾被接收但从不生效
# ============================================================
#
# 修复前的 `check()` 签名里有 `cfg` 参数，函数体却一律 `load_config()`：
# 调用方以为改了限额，实际用的还是 yaml 里的值，而且**没有任何报错**。
# 与「注释里写了兜底但代码没求值」属同一类错误——接口承诺了就得生效。

def test_check_honors_explicit_cfg():
    """显式传 cfg 时必须用它，而不是去读文件。"""
    cfg = {"规则名": "注入规则", "限额": 5, "提示": "注入提示"}
    r = title_gate.check("这场德比到底谁先崩？", ["甲？", "乙？"], cfg=cfg)
    assert r["quota"]["limit"] == 5, "cfg 里的限额没生效"
    assert r["rule"] == "注入规则"
    assert r["degraded"] is False


def test_check_cfg_limit_changes_verdict():
    """同一个历史，cfg 限额 2 与 5 必须给出不同判定——参数不是摆设。"""
    hist = ["甲？", "乙？", "丙？"]                 # 3 篇疑问收尾
    tight = {"规则名": "紧", "限额": 2, "提示": "x"}
    loose = {"规则名": "松", "限额": 5, "提示": "x"}
    assert title_gate.check("这场德比到底谁先崩？", hist, cfg=tight)["ok"] is False
    assert title_gate.check("这场德比到底谁先崩？", hist, cfg=loose)["ok"] is True
