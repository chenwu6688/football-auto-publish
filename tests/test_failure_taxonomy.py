# -*- coding: utf-8 -*-
"""主稿失败原因归类（failure_taxonomy）与接线单测（2026-11-09）。

重点不在「分类对不对」，而在**漏斗有没有真的把原因记下来**：
11-05 的 8 次失败查不到成因，根因不是分类错，是这条数据压根没落台账。
所以这里有一条 AST 接线断言，并且带负向（把关键字删掉必须判红）。
"""

import ast
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import failure_taxonomy as ft  # noqa: E402

ORCHESTRATOR = PROJECT_ROOT / "orchestrator.py"


class TestClassify(unittest.TestCase):
    def test_闭集全部命中(self):
        for kind, prefix in ft.KINDS:
            self.assertEqual(ft.classify(prefix), kind, prefix)

    def test_带后缀的真实错误串也能归类(self):
        real = [
            ("合规熔断中，本篇不再生成（计划第十章·宁停勿错）", "熔断"),
            ("未找到与话题「C罗退役」匹配的源文章（预检跳过）", "预检跳过"),
            ("Pipeline A：未找到与话题「某队」匹配的源文章", "源文缺失"),
            ("Pipeline A 不可用：数据源非直播吧/懂球帝", "数据源不可用"),
            ("事实矛盾: 比分不一致", "事实闸门"),
            ("反洗稿检查未过: 事实点数少于最强源文（21 < 23）", "反洗稿"),
            ("标题评分未过: 钩子不足", "标题评分"),
            ("诱导话术未过: 请关注", "诱导话术"),
            ("改写异常: 'int' object has no attribute 'get'", "改写异常"),
            ("改写失败", "改写失败"),
        ]
        for err, kind in real:
            self.assertEqual(ft.classify(err), kind, err)

    def test_空原因记缺失而不是其他(self):
        # 漏传原因 ≠ 出现了新文案，两者混一起查起来又得靠猜
        for v in ("", "   ", None, 123):
            self.assertEqual(ft.classify(v), ft.MISSING, repr(v))

    def test_陌生文案记其他(self):
        self.assertEqual(ft.classify("某个还没登记的新失败"), ft.UNKNOWN)

    def test_只按前缀匹配不按子串(self):
        # 「事实矛盾」出现在句中但开头不是它 → 不许命中，否则短串误报复活
        self.assertEqual(ft.classify("改写到一半顺带提了事实矛盾"), ft.UNKNOWN)

    def test_熔断不被预检前缀抢走(self):
        self.assertEqual(ft.classify("合规熔断中，本篇不再生成"), "熔断")


class TestCount(unittest.TestCase):
    def test_只返回出现过的类别(self):
        got = ft.count(["事实矛盾: a", "事实矛盾: b", "合规熔断中，本篇不再生成"])
        self.assertEqual(got, {"事实闸门": 2, "熔断": 1})

    def test_空输入返回空dict而不是全零(self):
        # 「没调过」和「调了零失败」必须能分开（漏斗老规矩）
        self.assertEqual(ft.count([]), {})
        self.assertEqual(ft.count(None), {})

    def test_按次数降序(self):
        got = ft.count(["改写失败"] * 3 + ["合规熔断中，本篇不再生成"] * 1)
        self.assertEqual(list(got), ["改写失败", "熔断"])

    def test_不完整文案落其他而不是硬凑(self):
        # 「熔断中」少了两字就不算熔断 —— 宁可进「其他」让人看见，也别猜
        self.assertEqual(ft.classify("熔断中"), ft.UNKNOWN)

    def test_缺失与其他分开计数(self):
        got = ft.count(["", "未知新文案"])
        self.assertEqual(got, {ft.MISSING: 1, ft.UNKNOWN: 1})

    def test_合计等于输入条数(self):
        errs = ["", "x", "事实矛盾: y", "改写异常: z"]
        self.assertEqual(sum(ft.count(errs).values()), len(errs))

    def test_selftest全绿(self):
        self.assertEqual([n for n, ok in ft.selftest() if not ok], [])


def _generated_records_reason(src: str) -> bool:
    """检查 `_funnel("generated", ...)` 是否带 `失败原因=failure_taxonomy.count(...)`。

    用 AST 查真实 Call 节点，不文本匹配源码 —— 注释里写一句同样的字不算接线。
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "_funnel"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == "generated"):
            continue
        for kw in node.keywords:
            if kw.arg != "失败原因":
                continue
            v = kw.value
            if not isinstance(v, ast.Call):
                continue
            vf = v.func
            if (isinstance(vf, ast.Attribute) and vf.attr == "count"
                    and isinstance(vf.value, ast.Name)
                    and vf.value.id == "failure_taxonomy"):
                return True
    return False


class TestWiring(unittest.TestCase):
    def test_漏斗generated层真的记了失败原因(self):
        self.assertTrue(
            _generated_records_reason(ORCHESTRATOR.read_text(encoding="utf-8")),
            "orchestrator 的 _funnel('generated', ...) 丢了 失败原因 —— 下次又只能看数量")

    def test_负向_删掉关键字必须判红(self):
        src = ORCHESTRATOR.read_text(encoding="utf-8")
        self.assertTrue(_generated_records_reason(src), "前置条件：原文件应通过")
        broken = src.replace(
            "失败原因=failure_taxonomy.count(_batch_errors)", "")
        self.assertNotEqual(broken, src, "替换必须真的生效，否则负向是假绿")
        self.assertFalse(_generated_records_reason(broken))

    def test_失败原因取自本批切片而不是累计(self):
        src = ORCHESTRATOR.read_text(encoding="utf-8")
        self.assertIn("_err_mark", src)
        self.assertIn('stats.setdefault("failure_errors", [])', src)


if __name__ == "__main__":
    unittest.main()
