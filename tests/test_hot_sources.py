# -*- coding: utf-8 -*-
"""热点源接入层（hot_sources）与三通道合并（hot_topics.pick_candidates）。

## 这组用例防的是什么

1. **加源没加时效** —— 2026-11-06 实测新浪体育 RSS 的 pubDate 全是 2018-09。
   如果「能拉到数据就接」，接进来的就是旧闻新发。所以媒体源必须过时效闸门，
   而且被剔的条数要记进台账（否则「今天为什么没有媒体热点」无法解释）。
2. **加了源却不区分类型** —— 贴吧是 UGC，只能当热度，**绝不能当事实底本**；
   懂球帝是编辑部稿，有正文，可当底本。两者混成一个池子就会出现
   「拿贴吧帖子当新闻事实写进正文」。
3. **同一件事多源出现就发多篇** —— 必须按 event_key 合并成一条，并把来源并起来
   （「微博 + 贴吧 + 懂球帝 都在说」本身就是最强的热度证据）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hot_sources as hs  # noqa: E402
import hot_topics as ht  # noqa: E402


# ---------------------------------------------------------------- provider 表

def test_disabled_providers_are_documented():
    """关掉的源必须写清原因：否则下一个人会以为是漏配又打开。"""
    for p in hs.PROVIDERS:
        assert p.get("note"), f"{p['name']} 缺 note"
        if not p.get("enabled"):
            assert p["tier"] in ("P1", "P2", "P3"), f"{p['name']} 关闭但 tier 未标注"


def test_only_dongqiudi_and_tieba_are_enabled_media():
    """本轮只接懂球帝 + 贴吧（用户 11-06 指令），其余一律关闭。"""
    on = {p["name"] for p in hs.PROVIDERS if p.get("enabled")}
    assert on == {"baidu", "weibo", "dongqiudi", "tieba"}, on


# ---------------------------------------------------------------- 贴吧

def test_fetch_tieba_parses_topic_list(monkeypatch):
    payload = {"data": {"bang_topic": {"topic_list": [
        {"topic_name": "C罗退出国家队", "discuss_num": "12345"},
        {"topic_name": "欧冠抽签", "discuss_num": "800"},
        {"topic_name": "", "discuss_num": "0"},
    ]}}}

    class _R:
        def json(self):
            return payload

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    rows = hs.fetch_tieba()
    assert [r["word"] for r in rows] == ["C罗退出国家队", "欧冠抽签"]
    assert all(r["kind"] == "ugc" for r in rows), "贴吧必须是 ugc，不能当事实底本"
    assert rows[0]["heat"] == 12345


# ---------------------------------------------------------------- 时效

def test_media_stale_is_dropped_and_counted(monkeypatch):
    """媒体稿 4 个月前 → 剔除；榜单行不受影响（榜上没有发布时间）。"""
    rows = [
        {"word": "旧闻", "rank": 1, "heat": 0, "source": "dongqiudi",
         "kind": "media", "url": "u1", "published_at": "07-09 11:15"},
        {"word": "C罗退出国家队", "rank": 2, "heat": 900, "source": "weibo",
         "kind": "board", "url": "", "published_at": ""},
    ]
    kept, dropped, _unknown = hs.apply_freshness(rows)
    assert [r["word"] for r in kept] == ["C罗退出国家队"]
    assert len(dropped) == 1


def test_media_without_time_is_kept_but_marked():
    """拿不到时间不能判过期（保守保留），但必须能被台账区分。"""
    rows = [{"word": "某队官宣新援", "rank": 1, "heat": 0, "source": "dongqiudi",
             "kind": "media", "url": "u", "published_at": ""}]
    kept, dropped, unknown = hs.apply_freshness(rows)
    assert len(kept) == 1 and not dropped and unknown == 1


# ---------------------------------------------------------------- collect

def test_collect_survives_provider_failure(monkeypatch):
    """一个源炸了不影响其它源，错误只进 diag。"""
    def _boom(timeout=8):
        raise RuntimeError("blocked")

    monkeypatch.setitem(hs._FETCHERS, "baidu", _boom)
    monkeypatch.setattr(hs, "_FETCHERS", dict(hs._FETCHERS))
    out = hs.collect()
    assert out["diag"]["providers"]["baidu"]["status"] == "error"
    assert out["diag"]["providers"]["baidu"]["error"].startswith("RuntimeError")
    # 其余源照常出现在 diag 里（不论成功与否）
    assert "dongqiudi" in out["diag"]["providers"]


# ---------------------------------------------------------------- 三通道合并

def _board(word, rank=1, source="weibo", kind="board"):
    return {"word": word, "rank": rank, "heat": 100, "source": source,
            "kind": kind, "url": "", "published_at": ""}


def _media(word, rank=1, source="dongqiudi", url="https://www.dongqiudi.com/article/1.html"):
    return {"word": word, "rank": rank, "heat": 0, "source": source,
            "kind": "media", "url": url, "published_at": "1小时前"}


def _news(title, source="zhibo8"):
    return {"title": title, "url": "http://x", "source": source, "article_text": ""}


def test_pick_prefers_board_and_merges_providers():
    """同一事件在榜上 + 媒体头条都出现 → 只出 1 条，来源合并。"""
    cands, channel = ht.pick_candidates(
        [_board("C罗退出国家队", rank=3)],
        [_media("C罗自宣退出葡萄牙国家队")],
        [_news("C罗退出国家队 官方确认")],
        limit=1)
    assert len(cands) == 1 and channel == "board"
    assert cands[0]["providers"] == ["dongqiudi", "weibo"], cands[0]["providers"]
    assert len(cands[0]["support"]) >= 1


def test_pick_falls_back_to_media_when_board_blocked():
    """榜单被墙（境外 runner 常态）时，媒体头条必须能独立成候选。"""
    cands, channel = ht.pick_candidates(
        [], [_media("C罗自宣退出葡萄牙国家队")], [], limit=1)
    assert channel == "media" and len(cands) == 1
    assert cands[0]["event_key"].endswith("#retire")


def test_pick_media_requires_event_family():
    """只有实体、没有事件的媒体稿不得占热点位（「某某生日快乐」类）。"""
    cands, _ = ht.pick_candidates([], [_media("C罗晒出家庭合照")], [], limit=1)
    assert cands == []


def test_pick_falls_back_to_news_cluster():
    """榜单与媒体都空 → 退到同题 ≥2 篇。"""
    news = [_news("姆巴佩转会皇马达成协议"), _news("姆巴佩转会皇马细节曝光")]
    cands, channel = ht.pick_candidates([], [], news, limit=1)
    assert channel == "news_cluster" and len(cands) == 1


def test_pick_respects_covered_keys():
    cands, _ = ht.pick_candidates(
        [_board("C罗退出国家队")], [], [_news("C罗退出国家队")],
        covered_keys={"C罗#retire"}, limit=1)
    assert cands == []


# ---------------------------------------------------------------- 正文派发

def test_article_text_of_dispatches_by_domain(monkeypatch):
    """按域名选解析器：直播吧与懂球帝是两套 DOM。"""
    calls = []

    class _S:
        def scrape_dongqiudi_article(self, url):
            calls.append(("dq", url))
            return {"article_text": "懂球帝正文"}

        def scrape_zhibo8_article_content(self, url):
            calls.append(("z8", url))
            return "直播吧正文"

    import media_scraper
    monkeypatch.setattr(media_scraper, "SportsScraper", _S)
    assert hs.article_text_of("https://www.dongqiudi.com/article/1.html") == "懂球帝正文"
    assert hs.article_text_of("https://news.zhibo8.cc/x.htm") == "直播吧正文"
    assert [c[0] for c in calls] == ["dq", "z8"]
    assert hs.article_text_of("") == ""
