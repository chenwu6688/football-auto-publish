# -*- coding: utf-8 -*-
"""热点选题通道（hot_topics）回归用例。

2026-11-06 用户反馈：「昨晚 C 罗国家队退役，我文章里没有任何体现，重新梳理选题
逻辑，要紧跟热点，根据每日热点排行，不要重复发过期内容。」

查证：微博/百度榜上「C罗退出国家队」确实在前列，但选题素材只有赛程池 + 15 条
直播吧新闻，热搜从来没进过生产链路。本模块是补上的那条通道。

这里钉住三件事：**认得出足球热点**（且不把网球/篮球/萌娃当足球）、
**同一件事只有一个 event_key**（否则同一热点会写多篇）、
**没有报道支撑就不出候选**（否则就是拿一句热搜词编稿）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hot_topics as h  # noqa: E402


# ---------------------------------------------------------------- 实体识别

def test_entities_in_longest_match_wins():
    """「国际米兰」里躺着「米兰」，不能算 AC 米兰命中。"""
    assert h.entities_in("国际米兰1-0小胜") == ["国际米兰"]
    assert h.entities_in("AC米兰1-0小胜") == ["AC米兰"]
    assert h.entities_in("C罗退出国家队") == ["C罗"]


def test_entities_in_player_names():
    assert "C罗" in h.entities_in("C罗退出国家队")
    assert "姆巴佩" in h.entities_in("姆巴佩梅开二度")


# ---------------------------------------------------------------- 足球过滤

def test_is_football_strong_terms_without_entity():
    """「国足」不是俱乐部队名，靠专有词表兜。"""
    assert h.is_football("国足新帅浮出水面", []) is True
    assert h.is_football("欧冠抽签结果出炉", []) is True


def test_is_football_entity_needs_event():
    """只有实体、没有事件词 → 不算足球事件。

    实测反例：「三岁半男孩给电视里的红军递饼干」命中了利物浦别名「红军」，
    只看实体就会把一条萌娃视频塞进足球选题。
    """
    ents = h.entities_in("三岁半男孩给电视里的红军递饼干")
    assert ents == ["利物浦"]                       # 确实认出了实体
    assert h.is_football("三岁半男孩给电视里的红军递饼干", ents) is False


def test_is_football_rejects_other_sports():
    """网球场上的「球」、篮球的「封盖」都不是足球。"""
    for word in ("郑钦文神仙球获评月最佳", "克莱封盖库里",
                 "温瑞博3比0王楚钦", "换一袋盐 全球每年300万人因此得救"):
        assert h.is_football(word, h.entities_in(word)) is False, word


def test_is_football_rejects_generic_announce():
    """「官宣」跨领域通用（手机/明星），不能当足球词。

    实测反例：「荣耀700系列外观官宣」曾靠「官宣」混进足球切片。
    """
    assert h.is_football("荣耀700系列外观官宣", []) is False


def test_football_hits_respects_max_rank():
    board = [{"word": "欧冠决赛前瞻", "rank": 3, "heat": 100, "source": "baidu"},
             {"word": "欧冠决赛前瞻（长尾）", "rank": 80, "heat": 1, "source": "weibo"}]
    hits = h.football_hits(board, max_rank=60)
    assert [x["rank"] for x in hits] == [3]


# ---------------------------------------------------------------- 事件键

def test_event_key_groups_same_event_across_wording():
    """同一件事换措辞必须得到同一个 key，否则会被写多篇。"""
    keys = {h.event_key(w) for w in ("C罗退役", "C罗宣布退出国家队",
                                     "C罗告别国家队", "C罗退出国家队后首度发声")}
    assert keys == {"C罗#retire"}


def test_event_key_separates_different_events():
    assert h.event_key("C罗转会皇马") != h.event_key("C罗退役")
    assert h.event_key("C罗受伤") != h.event_key("C罗退役")


def test_event_key_empty_without_entity():
    """没有实体就没有去重键 —— 宁可重复，也不能把所有无实体稿判成同一件事。"""
    assert h.event_key("今天天气不错") == ""


def test_event_key_without_family_is_not_an_event():
    """「C罗妻子晒照」没有事件族，不是「事件」，不该占热点位。"""
    assert h.event_key("C罗妻子晒照") == "C罗#"


# ---------------------------------------------------------------- 与新闻配对

def test_pair_news_by_entity():
    news = [{"title": "C罗宣布退出葡萄牙国家队"},
            {"title": "英超第11轮综述"},
            {"title": "郑钦文夺得网球冠军"}]
    got = h.pair_news(["C罗"], "C罗退出国家队", news)
    assert [n["title"] for n in got] == ["C罗宣布退出葡萄牙国家队"]


def test_pair_news_requires_title():
    """没有标题的条目不参与配对（不拿正文片段冒充）。"""
    assert h.pair_news(["C罗"], "C罗退出国家队", [{"summary": "C罗退出国家队"}]) == []


def test_pair_news_cjk_overlap_when_no_entity():
    news = [{"title": "国足新帅浮出水面，足协已定人选"}]
    assert h.pair_news([], "国足新帅浮出水面", news)


# ---------------------------------------------------------------- 候选装配

def _board():
    return [
        {"word": "C罗退出国家队 甲亢哥痛哭流涕弹钢琴", "rank": 21, "heat": 900, "source": "baidu"},
        {"word": "荣耀700系列外观官宣", "rank": 30, "heat": 800, "source": "baidu"},
        {"word": "三岁半男孩给电视里的红军递饼干", "rank": 34, "heat": 700, "source": "baidu"},
    ]


def test_build_candidates_needs_support():
    """榜上有足球词、但当天没有对应报道 → 不产出候选（不编）。"""
    got = h.build_candidates(_board(), [{"title": "英超第11轮综述"}])
    assert got == []


def test_build_candidates_happy_path():
    news = [{"title": "C罗宣布退出葡萄牙国家队，20年国脚生涯落幕"}]
    got = h.build_candidates(_board(), news)
    assert len(got) == 1
    c = got[0]
    assert c["event_key"] == "C罗#retire"
    assert c["rank"] == 21 and c["source"] == "baidu"
    assert [s["title"] for s in c["support"]] == ["C罗宣布退出葡萄牙国家队，20年国脚生涯落幕"]


def test_build_candidates_skips_covered():
    news = [{"title": "C罗宣布退出葡萄牙国家队"}]
    assert h.build_candidates(_board(), news, covered_keys={"C罗#retire"}) == []


def test_build_candidates_limit():
    news = [{"title": "C罗宣布退出葡萄牙国家队"},
            {"title": "国足新帅浮出水面，足协已定人选"}]
    board = _board() + [{"word": "国足新帅浮出水面", "rank": 40, "heat": 600, "source": "weibo"}]
    got = h.build_candidates(board, news, limit=1)
    assert len(got) == 1


# ---------------------------------------------------------------- 降级通道

def test_build_from_news_needs_two_reports():
    """只有一条报道不算「热」，那是普通新闻，交给 LLM 选题走原路。"""
    news = [{"title": "C罗宣布退出葡萄牙国家队"}]
    assert h.build_from_news(news) == []


def test_build_from_news_clusters_same_event():
    news = [{"title": "C罗宣布退出葡萄牙国家队"},
            {"title": "C罗退出国家队后首度发声"},
            {"title": "C罗退役传闻再起"}]          # 同族，一起算
    got = h.build_from_news(news)
    assert len(got) == 1
    assert got[0]["event_key"] == "C罗#retire"
    assert got[0]["heat"] == 3
    assert got[0]["source"] == "news_cluster"


def test_build_from_news_skips_no_family():
    news = [{"title": "C罗妻子晒照"}, {"title": "C罗晒新发型"}]
    assert h.build_from_news(news) == []


def test_build_from_news_respects_covered():
    news = [{"title": "C罗宣布退出葡萄牙国家队"},
            {"title": "C罗退出国家队后首度发声"}]
    assert h.build_from_news(news, covered_keys={"C罗#retire"}) == []


# ---------------------------------------------------------------- 台账

def test_write_ledger_keeps_evidence(tmp_path):
    """台账必须能回答「今天榜上到底热了什么、为什么没选」。"""
    p = tmp_path / "hot_board.json"
    out = h.write_ledger("2026-11-06", board=_board(),
                         hits=h.football_hits(_board()),
                         candidates=[{"word": "C罗退出国家队", "event_key": "C罗#retire"}],
                         diag={"baidu": 50, "weibo": 52, "errors": []}, path=p)
    assert out == p
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["board_total"] == 3
    assert [x["word"] for x in data["football_hits"]]            # 足球切片留档
    assert data["candidates"][0]["event_key"] == "C罗#retire"
    assert data["diag"]["baidu"] == 50


def test_write_ledger_does_not_raise_on_bad_path(tmp_path):
    bad = tmp_path / "a.txt"
    bad.write_text("x", encoding="utf-8")
    assert h.write_ledger("2026-11-06", board=[], hits=[], candidates=[],
                          diag={}, path=bad / "sub" / "x.json") is None


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))


# ---------------------------------------------------------------- 正文补抓（接线）

def _card(url="https://news.zhibo8.com/zuqiu/a.htm", title="C罗宣布退出葡萄牙国家队"):
    return {"title": "C罗退出国家队", "_hot_support_raw": [{"title": title, "url": url}]}


def test_attach_source_text_failure_returns_empty(monkeypatch):
    """抓不到正文 → 不返回 card。

    这是关键的一步：news_articles 是懒加载（正文为空串），而
    `_find_source_article` 要求正文 ≥100 字。若抓不到正文还把热点塞进
    topics，它会占掉 batch_mix 已分配好的槽位、再被预检跳过 ——
    结果比不加热点还少一篇。
    """
    import media_scraper
    import orchestrator

    monkeypatch.setattr(media_scraper.SportsScraper, "scrape_zhibo8_article_content",
                        lambda self, url: None)
    md = {"news_articles": []}
    assert orchestrator._attach_hot_source_text(md, [_card()]) == []
    assert md["news_articles"] == []          # 没有凭空插入半条记录


def test_attach_source_text_success_writes_back(monkeypatch):
    """抓得到正文 → 写回 news_articles，并返回可用 card。"""
    import media_scraper
    import orchestrator

    body = "C罗在新闻发布会上宣布退出葡萄牙国家队，结束二十年国脚生涯。" * 3
    monkeypatch.setattr(media_scraper.SportsScraper, "scrape_zhibo8_article_content",
                        lambda self, url: body)
    md = {"news_articles": [{"title": "C罗宣布退出葡萄牙国家队", "url": "x",
                             "article_text": ""}]}
    ready = orchestrator._attach_hot_source_text(md, [_card()])
    assert len(ready) == 1
    # 已在 news_articles 里 → 补正文而不是重复插入
    assert len(md["news_articles"]) == 1
    assert md["news_articles"][0]["article_text"] == body


def test_attach_source_text_inserts_when_absent(monkeypatch):
    """源文章不在 news_articles 里 → 插一条带正文的记录（供 _find_source_article 用）。"""
    import media_scraper
    import orchestrator

    body = "C罗宣布退出国家队，葡萄牙足协发文致谢。" * 5
    monkeypatch.setattr(media_scraper.SportsScraper, "scrape_zhibo8_article_content",
                        lambda self, url: body)
    md = {"news_articles": []}
    ready = orchestrator._attach_hot_source_text(md, [_card()])
    assert len(ready) == 1
    assert md["news_articles"][0]["article_text"] == body
    assert md["news_articles"][0]["_hot_support"] is True
