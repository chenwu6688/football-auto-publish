"""v2 硬闸门必须自己入一致性台账（计划 7.3「拦截率可统计」的观测缺口）。

2026-11-05 v2 首日实跑暴露：改写路径（Pipeline A）只在**被拦**时调用
`_note_consistency_reject`，通过时一条都不记。于是 `data/consistency_stats.json`
里 events=2 全是 warn-only、`blocking_checks=0` → `reject_rate` 恒为 None
→ `verify_publish_gate` 的硬项「一致性校验拦截率可统计」**永久红**。

那不是「没有违规」，而是**没有观测**。硬项红着不放，真回归就会被当成已知噪音。

本测试钉住两条：
1. 通过时也要留一条 `blocking=True, passed=True` 的事件（分母）；
2. 被拦时留 `passed=False`（分子），且拦截率能算出来。
"""

import tempfile
from pathlib import Path

import consistency_stats as cs

import orchestrator as O


TOPIC = {
    "title": "1-9！朗斯主场被打成筛子",
    "score": 90,                     # 选题排序分（int），不是比分 —— 撞车复现
    "content_type": "热点球评",
    "keywords_cn": ["朗斯", "科莫"],
    "_word_count_range": [200, 400],
}

SRC_TEXT = "朗斯主场1比9输给科莫，法布雷加斯赛后承认中场完全失控。" * 20
SOURCE = {
    "article_text": SRC_TEXT,
    "fixture": {"source": "zhibo8", "home_team": "朗斯", "away_team": "科莫",
                "league": "法甲", "article_text": SRC_TEXT, "source_images": []},
}
CTX = {"data_source": "zhibo8", "fixture": SOURCE["fixture"]}

ARTICLE = {
    "title": "科莫9比1血洗朗斯，这场欧冠惨案到底谁该脸红",
    "content": "朗斯主场1比9输给科莫，法布雷加斯赛后承认中场完全失控。" * 12,
}


class TestV2GateLedger:
    def setup_method(self, method):
        self._d = tempfile.TemporaryDirectory()
        self._paths = (cs.STATS_PATH, cs.LABELS_PATH)
        cs.STATS_PATH = Path(self._d.name) / "stats.json"
        cs.LABELS_PATH = Path(self._d.name) / "labels.json"
        self._rw = O.rewrite_article

    def teardown_method(self, method):
        cs.STATS_PATH, cs.LABELS_PATH = self._paths
        O.rewrite_article = self._rw
        self._d.cleanup()

    def _run(self, topic=None, max_retries=0):
        O.rewrite_article = lambda *a, **k: dict(ARTICLE)
        return O._rewrite_with_retry(topic or dict(TOPIC), CTX, 1, dict(SOURCE),
                                     max_retries, "2026-11-05")

    def test_pass_is_recorded_as_blocking_check(self):
        """通过也必须留一条 blocking 事件，否则分母恒为 0。"""
        art, err = self._run()
        assert art and not err, f"本应成稿，实际 err={err}"
        s = cs.summary()
        assert s["blocking_checks"] == 1
        assert s["rejects"] == 0
        assert s["reject_rate"] is not None          # 0.0%，不是「未接入」
        assert s["reject_rate_display"] == "0.0%"

    def test_block_is_recorded_as_reject(self, monkeypatch):
        """被拦时记 reject，拦截率算得出 100%。"""
        def _block(*a, **k):
            return {"block": True, "hint": "比分写反",
                    "record": {"contradictions": ["正文 1-9 与事实卡 9-1 相反"]}}
        monkeypatch.setattr(O.gate_v2, "fact_gate", _block)
        topic = dict(TOPIC, _degraded=True)          # 跳过「降级后重生成」分支，避免递归
        art, err = self._run(topic)
        assert not art and "事实矛盾" in err
        s = cs.summary()
        assert s["blocking_checks"] == 1
        assert s["rejects"] == 1
        assert s["reject_rate_display"] == "100.0%"

    def test_ledger_failure_never_blocks_generation(self, monkeypatch):
        """埋点不是闸门：台账写失败也必须照常成稿。"""
        def _boom(*a, **k):
            raise RuntimeError("台账磁盘满")
        monkeypatch.setattr(cs, "record", _boom)
        art, err = self._run()
        assert art and not err
