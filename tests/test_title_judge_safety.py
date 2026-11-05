"""重拟链的「未判定」语义（2026-11-05 立）。

## 隐患

`_rewrite_with_retry` 里有一段标题处理：评分 → 不过就重拟 → 再评分 → 三要素兜底。
当天只给 `repick_title` 包了 try/except，**评分器与三要素判定本身还是裸的**：

```
_sc, _sc_pass, _sc_reasons = score_title_plan(...)          # ← 裸
...
_sc, _sc_pass, _sc_reasons = score_title_plan(...)          # ← 裸（重拟后紧邻的 6 行）
if not title_elements.check(...)["ok"]:                     # ← 裸
    _fb = [c for c in title_brief.candidates(topic) ...]    # ← 裸
```

## 威胁

这些代码在 `for attempt in range(max_retries)` 里，所以一次类型错会**按重试次数放大**：
正文已经生成成功（最贵的环节），却在最后的标题评分上抛异常 → 整篇丢弃 + 白烧 N 次额度。
2026-11-05 晚间 3 篇丢 2 篇就是这么来的，而且同一形态在四个入口各有一份副本：

| 入口 | 说明 |
|---|---|
| 主改写 | `_rewrite_with_retry`（上述四处） |
| 紧急球评 | 直接调 `score_title_plan` |
| 赛前预测 | 直接调 `score_title_plan` |
| 蓄水池准入 | `_reservoir_accept` 里直接调 |

`ensure_fixture_title` 更关键：确定性供给/战报/结果稿/多切口四条线都从它过。

## 断言口径

评分崩了 = **这一项没判定**，不是「稿子不合格」：
  · `judged=False`，`passed=True`（不阻断）；
  · 原因写进 reasons / `_title_score`，可查；
  · 绝不允许异常冒到重试循环外。
"""

import pytest

import orchestrator as O


class TestSafeScore:
    def test_正常标题照旧给出分数(self):
        sc, ok, rs, judged = O._safe_title_score(
            {}, "科莫9比1血洗朗斯，这场欧冠惨案到底谁该脸红", "热点球评")
        assert judged is True and isinstance(sc, int) and sc >= 0

    @pytest.mark.parametrize("bad", [{"a": 1}, ["x"], 123])
    def test_非文本标题不崩且判未判定(self, bad):
        sc, ok, rs, judged = O._safe_title_score({}, bad, "热点球评", stage="自检")
        assert judged is False
        assert ok is True, "未判定不得当作不合格（否则等于让 bug 决定发不发）"
        assert any("未判定" in r for r in rs)

    def test_空标题是判定结果不是未判定(self):
        """None/"" 是「标题为空」这条判据的**结论**，不该混进未判定口径。"""
        sc, ok, rs, judged = O._safe_title_score({}, None, "热点球评")
        assert (sc, ok, judged) == (0, False, True)

    def test_评分器内部炸了也不冒异常(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("entity_map 读坏了")
        monkeypatch.setattr(O, "score_title_plan", boom)
        sc, ok, rs, judged = O._safe_title_score({}, "随便一个标题", "热点球评")
        assert (ok, judged) == (True, False)
        assert "RuntimeError" in rs[0]

    def test_extra_kwargs透传(self):
        """fact_tier 必须能透传：传闻断言式标题靠它判 0 分（计划 4.1）。"""
        sc, ok, rs, judged = O._safe_title_score(
            {"fact_tier": "rumor"}, "据传拜仁已经签下他", "热点球评",
            fact_tier="rumor")
        assert judged is True


class TestSafeElements:
    def test_正常标题判定(self):
        ok, judged = O._safe_elements_ok("科莫9比1血洗朗斯，这场欧冠惨案到底谁该脸红")
        assert judged is True

    def test_判定崩了返回未判定且放行(self, monkeypatch):
        def boom(*a, **k):
            raise ValueError("词表坏了")
        monkeypatch.setattr(O.title_elements, "check", boom)
        ok, judged = O._safe_elements_ok("随便")
        assert (ok, judged) == (True, False)

    def test_候选生成崩了返回空列表(self, monkeypatch):
        def boom(*a, **k):
            raise KeyError("候选模板缺字段")
        monkeypatch.setattr(O.title_brief, "candidates", boom)
        assert O._safe_title_candidates({}) == []


class TestFixturePath:
    """`ensure_fixture_title` 是四条供给线的公共入口，-1 = 未判定。"""

    def test_评分崩了返回负一而不是抛出(self, monkeypatch):
        def boom(*a, **k):
            raise TypeError("比分字段类型不对")
        monkeypatch.setattr(O, "score_fixture_title", boom)
        title, score, reasons = O.ensure_fixture_title(
            {"home_zh": "阿森纳", "away_zh": "曼城"}, {"title": "阿森纳对曼城"})
        assert score == -1
        assert title == "阿森纳对曼城"
        assert any("未判定" in r for r in reasons)

    def test_重拟崩了保留原标题(self, monkeypatch):
        monkeypatch.setattr(O, "score_fixture_title",
                            lambda *a, **k: (10, ["分数不够"]))
        monkeypatch.setattr(O, "repick_title",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸")))
        title, score, _ = O.ensure_fixture_title(
            {"home_zh": "阿森纳", "away_zh": "曼城"}, {"title": "阿森纳对曼城"})
        assert score != -1, "重拟崩了不该把整条评分判成未判定"
        assert title  # 有标题就行：模板兜底或原题都可以

    def test_负一不得被当作不合格(self):
        """四个调用方都是 `if s_score != -1 and s_score < 60`。"""
        import inspect
        src = inspect.getsource(O)
        assert src.count("if s_score != -1 and s_score < 60") == 4
