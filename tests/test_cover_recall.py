#!/usr/bin/env python3
"""封面覆盖率链路单测（2026-11-01 体系体检）。

钉住的都是真实事故：

1. **footyrenders 侧栏污染**：查 Messi 返回的第一张是 C 罗——原来抓搜索结果页里「全部」png，
   而页面混着站点侧栏的「热门球员」推荐位，代码直接 `[:count]` 取前几张。
2. **50×50 占位图**：该 CDN 的 png 实测 2,173 字节 / 50×50，保存侧 800×600 闸门必拒，
   所以它作为图源等于白跑——search_images 必须不再调用它。
3. **只认 33 个实体**：`WIKI_PLAYERS`(18)+`WIKI_TEAMS`(15) 之外一概 0 图，
   而 entity_map 里有 1500+ 个条目。实体→维基搜索这一层必须真的被调用。
4. **维基搜索会飘**：拿「国际米兰」可能搜到「某赛季」条目，页名与实体不沾边时必须丢弃，
   否则又造出「图文不符」（这正是 cover_binding 想防的事）。
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import data_collector as dc  # noqa: E402
import verify_cover_coverage as vcc  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_wiki_breaker():
    """维基熔断是进程级状态，用例之间必须重置，否则前面的失败会把后面全带成 None。"""
    dc.reset_wiki_breaker()
    yield
    dc.reset_wiki_breaker()


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# ============================================================
# 1. footyrenders 相关性
# ============================================================

SIDEBAR_HTML = """
<a href="/cdn/players/cristiano-ronaldo.png"><img src="/cdn/players/cristiano-ronaldo.png"></a>
<a href="/cdn/players/jude-bellingham.png"><img src="/cdn/players/jude-bellingham.png"></a>
<a href="/cdn/players/lionel-messi.png"><img src="/cdn/players/lionel-messi.png"></a>
"""


def test_footyrenders_filters_sidebar_players(monkeypatch):
    """查 messi 只能拿到 messi，不能把侧栏的 C 罗/贝林厄姆当结果。"""
    monkeypatch.setattr(dc.requests, "get", lambda *a, **k: _Resp(200, text=SIDEBAR_HTML))
    imgs = dc.search_footyrenders(["Messi"], count=3)
    assert len(imgs) == 1
    assert "lionel-messi" in imgs[0]["url"]
    assert all("ronaldo" not in i["url"] and "bellingham" not in i["url"] for i in imgs)


def test_footyrenders_returns_empty_when_no_match(monkeypatch):
    """检索词与页面里任何 png 都不匹配时，宁可返回空，也不要拿侧栏图凑数。"""
    monkeypatch.setattr(dc.requests, "get", lambda *a, **k: _Resp(200, text=SIDEBAR_HTML))
    assert dc.search_footyrenders(["Haaland"], count=3) == []


# ============================================================
# 2. 维基实体检索
# ============================================================

def _fake_wiki(monkeypatch, summary_payload, search_titles=()):
    """按 URL 分派：summary 走 payload，w/api.php 搜索结果走 search_titles。"""
    def _get(url, *a, **k):
        if "/w/api.php" in url:
            hits = [{"title": t} for t in search_titles]
            return _Resp(200, {"query": {"search": hits}})
        return _Resp(200, summary_payload)
    monkeypatch.setattr(dc.requests, "get", _get)


def test_wikipedia_entity_exact_page(monkeypatch):
    _fake_wiki(monkeypatch, {"title": "哈兰德", "originalimage": {"source": "https://w/haaland.jpg"}})
    img = dc.search_wikipedia_entity("哈兰德")
    assert img["url"] == "https://w/haaland.jpg"
    assert img["source"] == "wikipedia"
    assert img["alt"] == "哈兰德"
    assert img["query"] == "哈兰德"


def test_wikipedia_entity_drops_unrelated_search_hit(monkeypatch):
    """搜索飘到「某赛季」这类条目时必须丢弃——否则封面与正文对不上。"""
    _fake_wiki(monkeypatch,
               {"title": "2021年国际冠军杯", "thumbnail": {"source": "https://w/x.jpg"}},
               search_titles=["2021年国际冠军杯"])
    assert dc.search_wikipedia_entity("国际米兰") is None


def test_wikipedia_entity_disambiguation_falls_through(monkeypatch):
    """消歧义页没有图，应继续往下试而不是直接当结果。"""
    _fake_wiki(monkeypatch, {"title": "AC米兰", "disambiguation": True})
    assert dc.search_wikipedia_entity("AC米兰") is None


# ============================================================
# 3. search_images 的图源顺序
# ============================================================

def test_search_images_never_calls_footyrenders(monkeypatch):
    """footyrenders 只剩 50×50 占位图，留在链里等于每篇白跑几秒网络。"""
    called = {"fr": 0}

    def _fr(*a, **k):
        called["fr"] += 1
        return []

    monkeypatch.setattr(dc, "search_footyrenders", _fr)
    monkeypatch.setattr(dc, "search_wikipedia", lambda *a, **k: [])
    monkeypatch.setattr(dc, "search_wikipedia_entity", lambda *a, **k: None)
    dc.search_images({"title": "哈兰德梅开二度，曼城主场3-1逆转阿森纳",
                      "keywords": ["Haaland"]}, count=3)
    assert called["fr"] == 0


def test_search_images_uses_entity_dictionary(monkeypatch):
    """实体在 entity_map 里（但不在 18 人/15 队手工表）时，必须走维基搜索取到图。"""
    seen = []

    def _entity(name, *a, **k):
        seen.append(name)
        return {"url": f"https://w/{name}.jpg", "source": "wikipedia", "alt": name}

    monkeypatch.setattr(dc, "search_wikipedia", lambda *a, **k: [])
    monkeypatch.setattr(dc, "search_wikipedia_entity", _entity)
    imgs = dc.search_images({"title": "半场零封巴黎，里昂下半场到底哪一步走错了？",
                             "keywords": []}, count=2)
    assert imgs, "实体词典命中却取不到图，等于扩展层没生效"
    # 手工表里的「巴黎」精确页查空后，必须仍由这一层补上（tried 只在真取到图时才置位）
    assert "巴黎" in seen and "里昂" in seen


def test_search_images_no_entity_returns_nothing_generic(monkeypatch):
    """一个实体都抽不到时，不许退回 football/stadium 这类通用词（计划 2.2）。"""
    monkeypatch.setattr(dc, "search_wikipedia", lambda *a, **k: [])
    called = []
    monkeypatch.setattr(dc, "search_wikipedia_entity",
                        lambda *a, **k: called.append(a) or None)
    imgs = dc.search_images({"title": "这场球真好看", "keywords": []}, count=3)
    # 没有实体就干脆不搜：标题词兜底必然搜到无关条目（维基不是图库）
    assert imgs == []
    assert called == []


def test_search_images_dedupes_urls(monkeypatch):
    """同一张图被多层取到只保留一次，否则三图模式会塞三张一样的。"""
    same = {"url": "https://w/a.jpg", "source": "wikipedia", "alt": "巴黎"}
    monkeypatch.setattr(dc, "search_wikipedia", lambda *a, **k: [dict(same)])
    monkeypatch.setattr(dc, "search_wikipedia_entity", lambda *a, **k: dict(same))
    imgs = dc.search_images({"title": "巴黎圣日耳曼主场取胜", "keywords": []}, count=3)
    assert len(imgs) == 1


# ============================================================
# 4. 台账验收脚本
# ============================================================

def _write_meta(root, day, articles):
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(
        __import__("json").dumps({"articles": articles}, ensure_ascii=False), encoding="utf-8")


def test_no_cover_counts_only_published(tmp_path, monkeypatch):
    """没发出去的篇不算「无封面发布」——分母错了会把指标做假。"""
    _write_meta(tmp_path, "2026-11-03", [
        {"title": "有封面", "platform_id": "1", "images": ["a.jpg", "b.jpg"]},
        {"title": "无封面已发", "platform_id": "2", "images": []},
        {"title": "无封面没发", "platform_id": None, "images": []},
    ])
    monkeypatch.setattr(vcc, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(vcc, "_window", lambda days: ("2026-11-03", "2026-11-03"))
    rows = vcc._load_published("2026-11-03", "2026-11-03")
    assert [r["title"] for r in rows] == ["有封面", "无封面已发"]


def test_no_cover_respects_since_date(tmp_path, monkeypatch):
    """生效日之前的篇不进判定——旧图源产物拿来判新链路等于永远 FAIL。"""
    _write_meta(tmp_path, "2026-10-30", [{"title": "旧稿", "platform_id": "1", "images": []}])
    _write_meta(tmp_path, "2026-11-03", [{"title": "新稿", "platform_id": "2", "images": ["a"]}])
    monkeypatch.setattr(vcc, "OUTPUT_DIR", tmp_path)
    rows = vcc._load_published("2026-11-02", "2026-11-03")
    assert [r["title"] for r in rows] == ["新稿"]


def test_no_cover_skips_when_window_not_open(monkeypatch):
    monkeypatch.setattr(vcc, "_window", lambda days: None)
    rep = vcc.check_no_cover(14)
    assert rep["ok"] is True and rep["advisory"] is True


def test_no_cover_fails_when_rate_too_high(tmp_path, monkeypatch):
    arts = [{"title": f"t{i}", "platform_id": str(i), "images": []} for i in range(10)]
    _write_meta(tmp_path, "2026-11-03", arts)
    monkeypatch.setattr(vcc, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(vcc, "_window", lambda days: ("2026-11-03", "2026-11-03"))
    rep = vcc.check_no_cover(14)
    assert rep["ok"] is False
    assert any("零配图" in d for d in rep["detail"])


def test_negative_check_flags_threshold_that_never_trips(monkeypatch):
    """把上限调成 100% 后，10 篇全零配图会被判通过——说明阈值确实在起作用。"""
    monkeypatch.setattr(vcc, "MAX_NO_COVER_PCT", 100.0)
    rep = vcc.check_negative()
    assert rep["ok"] is False


def test_source_chain_detects_unwired_entity_layer(monkeypatch, tmp_path):
    """只有定义没有调用 ⇒ 必须 FAIL（「接了等于没接」是这项目的常客）。"""
    fake = tmp_path / "data_collector.py"
    fake.write_text(
        "def search_wikipedia_entity(name):\n"
        "    return None\n\n"
        "def search_images(topic, count=5):\n"
        "    return []\n", encoding="utf-8")
    monkeypatch.setattr(vcc, "DATA_COLLECTOR", fake)
    rep = vcc.check_source_chain()
    assert rep["ok"] is False
    assert any("没有被调用" in d for d in rep["detail"])


# ============================================================
# 5. 维基熔断（防批次超时）
# ============================================================

def test_wiki_breaker_trips_after_repeated_timeouts(monkeypatch):
    """连不上维基时不能每篇都白等：连续 2 次异常后就整进程跳过。"""
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise TimeoutError("wikipedia unreachable")

    monkeypatch.setattr(dc.requests, "get", _boom)
    assert dc.search_wikipedia_entity("哈兰德") is None
    assert dc.search_wikipedia_entity("曼城") is None
    before = calls["n"]
    # 第三次开始不再发请求
    assert dc.search_wikipedia_entity("阿森纳") is None
    assert calls["n"] == before


def test_wiki_breaker_does_not_trip_on_empty_result(monkeypatch):
    """「搜不到」不是故障——不能让 404/无图把图源整体关掉。"""
    monkeypatch.setattr(dc.requests, "get", lambda *a, **k: _Resp(404, None))
    for _ in range(3):
        assert dc.search_wikipedia_entity("哈兰德") is None
    assert dc._wiki_available() is True
