"""内容类型轴 vs 板块轴（2026-11-09 修）。

病根：``_fixture_content_type`` 曾是 ``return topic.get("板块", "战术榜单")``。

「板块」是头条栏目词表（转会动态／人物故事／中国足球／战术榜单）；
「content_type」是配比与分槽词表（热点球评／数据对比／排行榜／战术榜单…）。
两者**不是同一个轴**，混用有三层后果：

  1. BC 型（suggested_column=数据对照）被记成战术榜单 →
     verify_batch_mix 看到「战术榜单超上限 + 数据型不足」，而真正的问题被掩盖；
  2. 中超赛程稿记成「中国足球」→ TARGET_PCT 里没有这一型，属未登记类型，
     既不进配比统计，也查不到 DAILY_CAP（等于没有单日上限）；
  3. 更根上：赛程 topic 只有 suggested_column 没有 content_type →
     ``batch_mix._norm`` 判「未分类」→ data 槽的候选池恒空，只能「次优类型补位」。

本文件守四件事：
  * ``content_mix.type_of`` 是唯一取值入口（两字段都认、先归一、别名合并）；
  * 两个供给侧产出的 topic **必须带** content_type（AST 查真实 dict 字面量）；
  * 存量 topic（只有 suggested_column）仍能进 data 槽；
  * 任何路径都不许把「板块」词当内容类型返回。
"""

import ast
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import batch_mix  # noqa: E402
import content_mix  # noqa: E402
import deterministic_supply  # noqa: E402
import orchestrator  # noqa: E402

# 计划 6.2 的四板块（栏目词表）。它们**不是**合法 content_type（除战术榜单/人物故事
# 恰好同名），所以任何「原样返回板块」的路径都会产出未登记类型。
BOARD_WORDS = ("转会动态", "中国足球")


class TestTypeOfIsTheOnlyEntry(unittest.TestCase):
    def test_reads_content_type_first(self):
        self.assertEqual(
            content_mix.type_of({"content_type": "排行榜", "suggested_column": "战术榜单"}),
            "排行榜")

    def test_falls_back_to_suggested_column(self):
        self.assertEqual(content_mix.type_of({"suggested_column": "数据对照"}), "数据对比")

    def test_alias_is_merged_before_lookup(self):
        """数据对照 → 数据对比。不归一的话它不在 TARGET_PCT 里，会被当成未登录。"""
        self.assertEqual(content_mix.type_of({"suggested_column": "数据对照"}), "数据对比")
        self.assertEqual(content_mix.type_of({"content_type": "转会动态"}), "转会资讯")

    def test_default_is_callable_specific(self):
        """分槽侧要「未分类」，生成侧要「战术榜单」——两边语义不同，不能共用一个默认。"""
        self.assertEqual(content_mix.type_of({}), "未分类")
        self.assertEqual(content_mix.type_of({}, default="战术榜单"), "战术榜单")

    def test_board_word_is_not_a_content_type(self):
        """只有「板块」时**不许**原样返回它——这正是本次要修的病。"""
        for w in BOARD_WORDS:
            got = content_mix.type_of({"板块": w})
            self.assertNotEqual(got, w, f"板块词 {w} 被当成了内容类型")
            self.assertEqual(got, "未分类")


class TestFixtureLibraryCarriesField(unittest.TestCase):
    """赛程 topic 的 dict 字面量里必须有 content_type。

    用 AST 查真实 Dict 节点而不是 grep 字符串：注释里写一句「必须有 content_type」
    也能让 grep 通过，但代码里其实没有这个键（本项目踩过「把注释当证据」的坑）。
    """

    def _topic_dict_key_sets(self):
        src = (PROJECT_ROOT / "scripts" / "fixture_library.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if {"topic_id", "suggested_column"} <= keys:
                out.append(keys)
        return out

    def test_dict_literal_has_content_type(self):
        keysets = self._topic_dict_key_sets()
        self.assertTrue(keysets, "fixture_library 里找不到赛程 topic 的 dict 字面量（测试需同步）")
        for keys in keysets:
            self.assertIn("content_type", keys,
                          "赛程 topic 缺 content_type：batch_mix 会把它判成未分类，data 槽永远没候选")


class TestDeterministicSupplyCarriesField(unittest.TestCase):
    def test_node_topics_have_registered_type(self):
        topics = deterministic_supply.build_node_topics(horizon_days=None)
        self.assertTrue(topics, "日历里没有可用节点，测试失去意义")
        for t in topics:
            ct = t.get("content_type")
            self.assertTrue(ct, f"节点 topic 缺 content_type：{t.get('node_label')}")
            self.assertIn(ct, content_mix.TARGET_PCT,
                          f"节点 topic 的 content_type={ct} 不在配比表里（未登记类型）")

    def test_transfer_node_maps_to_transfer_news(self):
        """「转会动态」是板块名，配比表里的那一型叫「转会资讯」。"""
        topics = deterministic_supply.build_node_topics(horizon_days=None)
        names = {t.get("content_type") for t in topics}
        self.assertNotIn("转会动态", names)
        self.assertIn("转会资讯", names)

    def test_stock_writes_registered_type(self):
        """入库时不许回退成 node_type（「赛季节点」是节点分类，不是内容类型）。"""
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "items.json"
            with mock.patch.object(deterministic_supply, "STORE_PATH", store), \
                 mock.patch.object(deterministic_supply, "STORE_DIR", Path(td)):
                topic = {"topic_id": "t1", "node_type": "赛季节点",
                         "suggested_column": "战术榜单", "板块": "战术榜单"}
                rid = deterministic_supply.stock(topic, {"title": "x", "content": "y"})
                data = __import__("json").loads(store.read_text(encoding="utf-8"))
                item = next(i for i in data["items"] if i["id"] == rid)
        self.assertNotEqual(item["content_type"], "赛季节点",
                            "content_type 回退成了 node_type —— 未登记类型会绕过 DAILY_CAP")
        self.assertIn(item["content_type"], content_mix.TARGET_PCT)


class TestSlotAssignment(unittest.TestCase):
    """回到病发现场：structure 验收报「数据型不足」，是因为 data 槽根本没有候选。"""

    def test_legacy_topic_with_only_suggested_column_enters_data_slot(self):
        # 2 篇才有 data 槽（structure = main + data）；1 篇只看 main，测不出这个 bug
        topics = [
            {"title": "热点稿", "content_type": "热点球评"},
            {"title": "旧缓存 BC 型", "suggested_column": "数据对照"},
        ]
        picked, rep = batch_mix.assign(topics, 2)
        self.assertEqual(len(picked), 2)
        slots = {p["slot"]: p["type"] for p in rep["picked"]}
        self.assertEqual(slots.get("data"), "数据对比",
                         "只有 suggested_column 的赛程稿进不了 data 槽 —— 这就是原 bug")
        self.assertEqual(slots.get("main"), "热点球评")

    def test_board_only_topic_is_not_silently_typed(self):
        """只有「板块」的候选判未分类，不许被算进战术榜单（那会挤爆上限还假装达标）。"""
        self.assertEqual(batch_mix._norm({"板块": "中国足球"}), "未分类")

    def test_new_field_topic_enters_data_slot(self):
        topics = [
            {"title": "热点稿", "content_type": "热点球评"},
            {"title": "新 BC 型", "content_type": content_mix.normalize("数据对照")},
        ]
        _, rep = batch_mix.assign(topics, 2)
        slots = {p["slot"]: p["type"] for p in rep["picked"]}
        self.assertEqual(slots.get("data"), "数据对比")


class TestGenerationSide(unittest.TestCase):
    def test_fixture_content_type_never_returns_board_word(self):
        for w in BOARD_WORDS:
            got = orchestrator._fixture_content_type({"板块": w})
            self.assertNotEqual(got, w)
            self.assertIn(got, content_mix.TARGET_PCT)

    def test_fixture_content_type_maps_data_angle(self):
        got = orchestrator._fixture_content_type(
            {"suggested_column": "数据对照", "板块": "战术榜单"})
        self.assertEqual(got, "数据对比")

    def test_fixture_content_type_defaults_to_registered(self):
        self.assertIn(orchestrator._fixture_content_type({}), content_mix.TARGET_PCT)


if __name__ == "__main__":
    unittest.main()
