"""P1/P2 非阻断监控单测：标题钩子分布 + 内容类型再平衡。

这些监控只打印告警、不阻断流程，但分类逻辑必须稳定可测，
避免上线后把"好标题"误判为"无钩子"或反过来。
"""
import orchestrator as oc


def test_classify_question_hook():
    is_q, has_conflict, has_num = oc._classify_title_hook("今年的转会窗是不是变味了？")
    assert is_q is True


def test_classify_conflict_hook():
    is_q, has_conflict, has_num = oc._classify_title_hook("没了梅西巴萨反而更强？")
    assert has_conflict is True
    # 同时是疑问
    assert is_q is True


def test_classify_data_hook():
    is_q, has_conflict, has_num = oc._classify_title_hook("51血洗！马竞遭遇史上最大惨败")
    assert has_num is True
    assert has_conflict is True


def test_classify_plain_title_no_false_hook():
    # 平铺直叙陈述句：不应被判为疑问，也不应判为冲突
    is_q, has_conflict, has_num = oc._classify_title_hook("曼联战胜切尔西")
    assert is_q is False
    assert has_conflict is False


def test_warn_title_hook_distribution_balanced(capsys):
    topics = [
        {"title": "今年的转会窗是不是变味了？", "content_type": "转会资讯"},
        {"title": "没了梅西巴萨反而更强？", "content_type": "战术解析"},
        {"title": "51血洗！马竞遭遇史上最大惨败", "content_type": "热点球评"},
    ]
    oc.warn_title_hook_distribution(topics)
    out = capsys.readouterr().out
    assert "标题钩子分布" in out
    assert "达标" in out


def test_warn_title_hook_distribution_low_question(capsys):
    topics = [
        {"title": "曼联战胜切尔西", "content_type": "热点球评"},
        {"title": "拜仁击败多特", "content_type": "热点球评"},
        {"title": "没了梅西巴萨反而更强？", "content_type": "战术解析"},
    ]
    oc.warn_title_hook_distribution(topics)
    out = capsys.readouterr().out
    assert "疑问钩子偏低" in out


def test_warn_type_balance_newseason_floor(capsys):
    # 新赛季进行期：球评 < 40% 应告警
    topics = [
        {"title": "某转会传闻a", "content_type": "转会资讯"},
        {"title": "某八卦b", "content_type": "八卦趣事"},
        {"title": "某转会c", "content_type": "转会资讯"},
    ]
    oc.warn_type_balance(topics, season_label="新赛季进行期")
    out = capsys.readouterr().out
    assert "内容类型分布" in out
    assert "热点球评占比偏低" in out


def test_warn_type_balance_newseason_ceiling(capsys):
    # 新赛季进行期：转会+八卦 > 50% 应告警
    topics = [
        {"title": "转会a", "content_type": "转会资讯"},
        {"title": "八卦b", "content_type": "八卦趣事"},
        {"title": "八卦c", "content_type": "八卦趣事"},
    ]
    oc.warn_type_balance(topics, season_label="新赛季进行期")
    out = capsys.readouterr().out
    assert "转会+八卦占比偏高" in out


def test_warn_type_balance_offseason_no_floor_warning(capsys):
    # 休赛期不执行球评下限检查
    topics = [
        {"title": "转会a", "content_type": "转会资讯"},
        {"title": "八卦b", "content_type": "八卦趣事"},
    ]
    oc.warn_type_balance(topics, season_label="休赛期过渡")
    out = capsys.readouterr().out
    assert "热点球评占比偏低" not in out


def test_warn_type_balance_single_category(capsys):
    topics = [
        {"title": "球评a", "content_type": "热点球评"},
        {"title": "球评b", "content_type": "热点球评"},
        {"title": "球评c", "content_type": "热点球评"},
    ]
    oc.warn_type_balance(topics, season_label="新赛季进行期")
    out = capsys.readouterr().out
    assert "品类单一" in out
