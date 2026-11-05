"""单测的全局隔离（2026-11-05）。

## 为什么需要它

`data_collector.search_images` 在 2026-11-05 多了一层**爬新闻站正文图**（`news_images`）。
这一层是真实网络调用，而各图层的用例都是「mock 掉自己关心的那两层，让其余层自然返回空」。
新层一加进来，那些用例就会**真的连到 news.zhibo8.com**：

    · 本机网络通 → 3 张真实新闻图把 count=3 的配额占满 → Commons 那一层根本不执行，
      `test_commons_result_used` 报「没有 wikimedia 结果」；
    · 断网环境 → 又变成另一套结果。

同一个用例在不同网络下结论不同，它验的就不是代码，而是网线。项目的纪律是
「不把网络状态写进单测断言」，所以这里统一把这一层的外部 IO 掐掉：
**默认所有用例都不联网**；真要验解析逻辑，自己在用例里 patch `_get` 喂固定 HTML。

注意：只掐 `news_images`。其他源（维基/图库）各用例历来自己 mock，
这里不越俎代庖，免得把「某层没接上」这种真问题一起盖掉。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:            # 让用例能直接 import 顶层模块
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _offline_news_images(monkeypatch):
    """默认断开新闻站爬取层的网络（返回 None 等价于「请求失败」）。"""
    try:
        import news_images
    except Exception:                     # 模块被删了也不该让全部用例崩
        yield
        return
    monkeypatch.setattr(news_images, "_get", lambda *a, **k: None)
    monkeypatch.setitem(news_images._CACHE, "index", None)
    monkeypatch.setitem(news_images._CACHE, "at", 0.0)
    yield


@pytest.fixture(autouse=True)
def _isolate_consistency_ledger(monkeypatch, tmp_path):
    """默认隔离一致性台账（计划 7.3 观测缺口带来的副作用）。

    2026-11-05 给 v2 硬闸门加了「通过也入账」以后，任何跑到 `_rewrite_with_retry`
    的用例都会调 `consistency_stats.record()`；不隔离就直接写进真实的
    `data/consistency_stats.json` —— 实测跑一次全量单测，真实台账里多出 3 条
    假事件（title 是某条用例的假稿「曼城主场能否啃下铁桶阵？」）。

    台账是证据：混进测试噪音之后，「昨天拦了几次」就再也说不清。所以默认指到
    tmp_path；需要验台账语义的用例自己 monkeypatch 或直接用 cs.summary()。
    """
    try:
        import consistency_stats
    except Exception:                     # 模块被删也不该让全部用例崩
        return
    monkeypatch.setattr(consistency_stats, "STATS_PATH",
                        tmp_path / "consistency_stats.json")
    monkeypatch.setattr(consistency_stats, "LABELS_PATH",
                        tmp_path / "consistency_labels.json")
