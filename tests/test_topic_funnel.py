"""选题漏斗台账（topic_funnel）与接线的单测（2026-11-04）。

四段：
  1. 记录口径：相邻层算损失 / 缺层不当 0 / 变大记 gain / 空台账判未判定；
  2. 防呆：层级名拼错、负数、非整数都必须抛错（静默忽略=假接线温床）；
  3. 隔离：自检不写真实台账，落盘路径在 data/audit 下；
  4. 接线：orchestrator 里 8 层埋点必须全在，且是字符串常量首参。

负向断言是重点：只测「正常时记下了」等于没测 —— 这条线最大的风险不是算错，
而是**根本没被调用**（11-04 `_img_topic` 假接线：函数在、调用在、单测绿、数据恒空）。
"""

import ast
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import topic_funnel as tf  # noqa: E402
import verify_topic_funnel as vtf  # noqa: E402

ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"


class _TempLedger(unittest.TestCase):
    """每个用例一个临时台账，绝不碰真实账本（全量单测后复核台账为 0 行的纪律）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._old = tf.LEDGER_PATH
        tf.LEDGER_PATH = Path(self._td.name) / "funnel.jsonl"
        tf._CUR = None

    def tearDown(self):
        tf.LEDGER_PATH = self._old
        tf._CUR = None
        self._td.cleanup()

    def record(self, date="2026-11-04", batch="晨读", max_articles=2, stages=()):
        tf.begin(date, batch=batch, max_articles=max_articles)
        for item in stages:
            key, n = item[0], item[1]
            detail = item[2] if len(item) > 2 else None
            tf.stage(key, n, detail=detail)
        tf.flush()
        return tf.load()[-1]


class TestRecord(_TempLedger):
    def test_loss_is_adjacent_diff(self):
        rec = self.record(stages=[("llm", 8), ("dedup", 6), ("material", 6),
                                  ("category", 5), ("batch_mix", 2), ("slot", 2),
                                  ("generated", 2), ("final", 2)])
        self.assertEqual(rec["loss"], {"dedup": 2, "category": 1, "batch_mix": 3})
        self.assertEqual(rec["articles"], 2)

    def test_missing_stage_is_skipped_not_zero(self):
        """缺层（本批没跑 determinstic 供给）不能被当成「吃掉全部」。"""
        rec = self.record(stages=[("llm", 3), ("final", 1)])
        self.assertEqual(rec["loss"], {"final": 2})
        self.assertNotIn("slot", rec["loss"])
        self.assertNotIn("slot", rec["stages"])

    def test_growth_recorded_as_gain(self):
        """蓄水池补位让 final 大于 generated —— 记 gain，不记负损失。"""
        rec = self.record(stages=[("generated", 1), ("final", 2)])
        self.assertEqual(rec["loss"], {})
        self.assertEqual(rec["gain"], {"final": 1})

    def test_no_stage_means_articles_none(self):
        """批开了但一层没记 → articles 是 None，不是 0。"""
        rec = self.record(stages=[])
        self.assertIsNone(rec["articles"])
        self.assertEqual(rec["stages"], {})

    def test_detail_kept(self):
        rec = self.record(stages=[("llm", 5), ("dedup", 3, {"跨天": 2})])
        self.assertEqual(rec["stages"]["dedup"]["detail"], {"跨天": 2})

    def test_status_recorded(self):
        tf.begin("2026-11-04")
        tf.stage("llm", 1)
        tf.flush(status="aborted")
        self.assertEqual(tf.load()[-1]["status"], "aborted")

    def test_flush_idempotent(self):
        self.record(stages=[("llm", 1)])
        before = len(tf.load())
        self.assertIsNone(tf.flush())
        self.assertEqual(len(tf.load()), before)

    def test_stage_without_batch_returns_false(self):
        tf._CUR = None
        self.assertFalse(tf.stage("llm", 3))


class TestGuard(_TempLedger):
    def test_unknown_stage_raises(self):
        tf.begin("2026-11-04")
        with self.assertRaises(ValueError):
            tf.stage("dedupn", 1)

    def test_negative_raises(self):
        tf.begin("2026-11-04")
        with self.assertRaises(ValueError):
            tf.stage("llm", -1)

    def test_non_int_raises(self):
        tf.begin("2026-11-04")
        with self.assertRaises(ValueError):
            tf.stage("llm", "三")

    def test_stages_order_is_documented(self):
        """层级顺序即漏斗顺序，改顺序等于改口径 —— 这里把它钉住。"""
        self.assertEqual(tf._KEYS,
                         ("llm", "dedup", "material", "category",
                          "batch_mix", "slot", "generated", "final"))


class TestSummarize(_TempLedger):
    def test_empty_is_undecided(self):
        s = tf.summarize(7)
        self.assertFalse(s["decided"])
        self.assertIsNone(s["fill_rate"])
        self.assertIn("未判定", tf.report(7))

    def test_summary_numbers(self):
        self.record(max_articles=2, stages=[("llm", 4), ("final", 2)])
        self.record(max_articles=2, stages=[("llm", 3), ("final", 1)])
        s = tf.summarize(7)
        self.assertTrue(s["decided"])
        self.assertEqual(s["records"], 2)
        self.assertEqual(s["requested"], 4)
        self.assertEqual(s["articles"], 3)
        self.assertAlmostEqual(s["fill_rate"], 0.75)
        self.assertEqual(s["top_stage"], "final")     # 2 + 1 = 3 篇损失都在 final

    def test_report_mentions_top_stage(self):
        self.record(max_articles=2, stages=[("llm", 4), ("dedup", 2), ("final", 2)])
        self.assertIn("最大损失层", tf.report(7))

    def test_bad_line_skipped(self):
        tf.LEDGER_PATH.write_text('{"date":"2026-11-04","stages":{},"loss":{}}\n坏行\n',
                                  encoding="utf-8")
        self.assertEqual(len(tf.load()), 1)

    def test_write_failure_is_visible_not_silent(self):
        """台账写失败不抛异常，但 `LAST` 必须留下原因 —— 静默失败最危险。"""
        blocker = Path(self._td.name) / "blocked"
        blocker.write_text("我是文件不是目录", encoding="utf-8")
        tf.LEDGER_PATH = blocker / "funnel.jsonl"      # 父路径是文件 → 必然写不进去
        tf.begin("2026-11-04")
        tf.stage("llm", 1)
        self.assertEqual(tf.flush(), "2026-11-04")     # 不抛
        self.assertIs(tf.LAST["ok"], False)
        self.assertTrue(tf.LAST["reason"])


class TestSavedAccounting(_TempLedger):
    """final 层的「内存 / 落盘」两个口径（2026-11-09）。

    `final` 记的是落盘**前**的内存篇数，出口合规还会逐篇丢。只报内存篇数会让
    成稿数虚高（11-07/11-08 各虚高 1 篇，11-06 虚高 1 篇被合规拦下），
    所以落盘数单独记、单独报，不覆盖 `articles`（否则相邻层损失算不通）。
    """

    def test_记了落盘就单独汇总(self):
        self.record(stages=[("llm", 3), ("final", 2, {"落盘": 1, "落盘剔除": 1})])
        s = tf.summarize(7)
        self.assertEqual(s["articles"], 2)          # 内存口径不变，损失仍算得通
        self.assertEqual(s["saved"], 1)
        self.assertEqual(s["saved_batches"], 1)

    def test_老记录没有落盘不冒充零(self):
        # 记 0 会被读成「一篇都没落盘」，实际是「那批根本没记这个数」
        self.record(stages=[("llm", 2), ("final", 2)])
        s = tf.summarize(7)
        self.assertIsNone(s["saved"])
        self.assertEqual(s["saved_batches"], 0)
        self.assertNotIn("落盘", tf.report(7))

    def test_全覆盖时报告给剔除数(self):
        self.record(stages=[("llm", 2), ("final", 2, {"落盘": 2, "落盘剔除": 0})])
        out = tf.report(7)
        self.assertIn("落盘 2 篇", out)
        self.assertIn("逐篇丢 0 篇", out)

    def test_部分覆盖不做差(self):
        self.record(stages=[("llm", 2), ("final", 2, {"落盘": 1, "落盘剔除": 1})])
        self.record(date="2026-11-05", stages=[("llm", 2), ("final", 2)])
        out = tf.report(7)
        self.assertIn("覆盖 1/2 批", out)
        self.assertIn("不做差", out)

    def test_落盘为0也算记了(self):
        # 0 是有效值（整批被合规丢光），不能被 None 判定吞掉
        self.record(stages=[("llm", 2), ("final", 2, {"落盘": 0, "落盘剔除": 2})])
        s = tf.summarize(7)
        self.assertEqual(s["saved"], 0)
        self.assertEqual(s["saved_batches"], 1)


class TestIsolation(unittest.TestCase):
    def test_ledger_under_audit(self):
        self.assertIn(PROJECT_ROOT / "data" / "audit", Path(tf.LEDGER_PATH).parents)

    def test_selftest_does_not_touch_real_ledger(self):
        before = len(tf.load())
        bad = [n for n, ok in tf.selftest() if not ok]
        self.assertEqual(bad, [])
        self.assertEqual(len(tf.load()), before)

    def test_selftest_all_green(self):
        self.assertTrue(all(ok for _n, ok in tf.selftest()))


class TestWiring(unittest.TestCase):
    """接线硬项：8 层一个都不能少，且必须是字符串常量首参。"""

    def test_all_stages_wired(self):
        stages, calls, bad = vtf._funnel_calls(ORCHESTRATOR.read_text(encoding="utf-8"))
        # 案发现场顺序（行号升序）必须等于漏斗顺序 —— 顺序错=损失归错层
        by_line = sorted(stages, key=lambda k: min(stages[k]))
        self.assertEqual(by_line, [k for k in tf._KEYS], "有层级没接埋点或接反了顺序")
        self.assertEqual(bad, [], "埋点首参不是字符串常量，验收无法复核")

    def test_begin_and_flush_wired(self):
        _s, calls, _b = vtf._funnel_calls(ORCHESTRATOR.read_text(encoding="utf-8"))
        self.assertTrue(calls.get("begin"), "没有 begin()，台账永远开不了批")
        self.assertGreaterEqual(len(calls.get("flush") or []), 2,
                                "flush 少于 2 处：异常批次不留证")

    def test_negative_wiring_goes_red(self):
        """负向：把 dedup 埋点改名后，检查必须报红（否则「8/8 全绿」可能是恒真）。"""
        src = ORCHESTRATOR.read_text(encoding="utf-8")
        mutated = src.replace('_funnel("dedup"', '_funnel("dedupn"')
        self.assertNotEqual(src, mutated, "源码里找不到 dedup 埋点，负向用例已失效")
        self.assertFalse(vtf.check_wiring(mutated)["ok"])

    def test_negative_non_constant_arg_goes_red(self):
        src = ORCHESTRATOR.read_text(encoding="utf-8")
        self.assertIn('_funnel("llm", len(topics))', src)
        mutated = src.replace('_funnel("llm", len(topics))', "_funnel(_k, len(topics))", 1)
        self.assertFalse(vtf.check_wiring(mutated)["ok"])

    def test_funnel_helper_swallows_errors(self):
        """`_funnel` 必须有 try/except —— 埋点不许让发布中断。"""
        tree = ast.parse(ORCHESTRATOR.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_funnel")
        self.assertTrue(any(isinstance(s, ast.Try) and s.handlers for s in ast.walk(fn)))


class TestNotAGate(unittest.TestCase):
    """「不是闸门」这一项自己也要能报红，否则它只是句口号。"""

    def setUp(self):
        self._old = vtf.TIERS

    def tearDown(self):
        vtf.TIERS = self._old

    def test_registered_but_not_pre_publish(self):
        rep = vtf.check_not_a_gate()
        self.assertTrue(rep["ok"], rep["detail"])

    def test_negative_pre_publish_goes_red(self):
        """负向：把它挪进 pre-publish 必须报红。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tiers.yaml"
            p.write_text("items:\n  topic_funnel:\n    tiers: [pre-publish]\n    kind: hard\n",
                         encoding="utf-8")
            vtf.TIERS = p
            self.assertFalse(vtf.check_not_a_gate()["ok"])

    def test_presence_of_word_in_why_is_not_a_red(self):
        """回归：why 里出现 "pre-publish" 字样不算登记（首版就是这么误报的）。

        判词必须按结构判，不能全文找关键字 —— 与「闸门判词必须带语境」同一条纪律。
        """
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tiers.yaml"
            p.write_text(
                "items:\n  topic_funnel:\n    tiers: [weekly]\n    kind: advisory\n"
                "    why: 不要把它挪进 pre-publish，它是台账\n",
                encoding="utf-8")
            vtf.TIERS = p
            self.assertTrue(vtf.check_not_a_gate()["ok"])


if __name__ == "__main__":
    unittest.main()
