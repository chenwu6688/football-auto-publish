#!/usr/bin/env python3
"""信息图版式库扩充后的契约（2026-11-05）。

用户当天的原话是「硬条件：单篇文章必须至少有三张图」，所以这里测的不是「有几种
版式」这种实现细节，而是**这条硬条件在各种烂稿上还能不能兑现**：

  1. 素材抽取：正文只有一句、没有 `## ` 分节、只有陈述句……都得有路可走；
  2. 语义正确：对比卡不能把比分和积分并排放（第一版真画出来了「2-1 vs 27分」）；
  3. 保底：极短稿 / 空正文也必须补满 3 张，且三张视觉上不一样；
  4. 接线：保存层要把「用了哪几种版式」和「哪几篇没达标」落盘，否则验收无从判起。
"""

import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cover_card as cc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class TestSentenceExtraction(unittest.TestCase):
    """金句卡的素材来源。"""

    def test_splits_by_sentence_and_drops_noise(self):
        content = ("![配图1](images/a.jpg)\n"
                   "皇马客场2-1拿下瓦伦西亚，积分来到27分。\n"
                   "## 一、小标题不算句子\n"
                   "真正的问题不在体能，而在换人顺序。")
        got = cc._sentences(content)
        self.assertNotIn("## 一、小标题不算句子", got, "小标题不该被当成正文句子")
        self.assertTrue(all("配图1" not in g for g in got), "图片标记要滤掉")
        self.assertEqual(len(got), 2)

    def test_drops_too_short_and_too_long(self):
        # 「是的。」太短当不了主角；超长一句塞不进卡片
        got = cc._sentences("是的。\n" + "这" * 80 + "。")
        self.assertEqual(got, [])

    def test_prefers_judgement_over_fact(self):
        content = ("皇马客场2-1拿下瓦伦西亚。\n"
                   "真正的问题不在体能，而在换人顺序。")
        got = cc._extract_quote(content)
        self.assertIn("换人顺序", got, "有判断词的句子应该压过纯事实句")

    def test_skip_lets_same_article_produce_multiple_quotes(self):
        content = ("真正的问题不在体能，而在换人顺序。\n"
                   "但是中场的第一道屏障在下半场就没了。")
        first = cc._extract_quote(content)
        second = cc._extract_quote(content, skip=(first,))
        self.assertTrue(first and second)
        self.assertNotEqual(first, second, "跳过已用句子后必须换一句，否则三张图会重复")

    def test_returns_empty_when_nothing_usable(self):
        self.assertEqual(cc._extract_quote(""), "")
        self.assertEqual(cc._extract_quote("短。"), "")


class TestQAExtraction(unittest.TestCase):
    def test_requires_question_title(self):
        q, a = cc._extract_qa("皇马2-1瓦伦西亚", "皇马赢了这场球。")
        self.assertEqual((q, a), ("", ""), "陈述句标题硬做问答 = 答非所问")

    def test_picks_answer_sentence(self):
        q, a = cc._extract_qa("里昂下半场到底哪一步走错了？",
                              "上半场把边路按住了。\n\n真正的问题不在体能，而在换人顺序。")
        self.assertTrue(q.endswith("？"))
        self.assertIn("换人顺序", a)


class TestOutlineExtraction(unittest.TestCase):
    """结构卡：不依赖 `## `，这是「没有小标题」的稿子唯一的出路。"""

    def test_uses_headings_when_present(self):
        content = "## 一、防守\n丢了三球。\n\n## 二、中场\n衔接断了。"
        self.assertEqual(cc._extract_outline(content), ["一、防守", "二、中场"])

    def test_splits_by_blank_line_without_headings(self):
        content = "第一段讲防守站位的问题。\n\n第二段讲中场衔接的问题。"
        got = cc._extract_outline(content)
        self.assertEqual(len(got), 2, "没有小标题时应按空行分段，不能把全文并成一段")
        self.assertIn("第一段", got[0])

    def test_single_block_yields_one(self):
        self.assertEqual(len(cc._extract_outline("就一段话，没有任何分段。")), 1)


class TestCompareExtraction(unittest.TestCase):
    """对比卡的铁律：只有语义可比的量才能并排。

    第一版直接取标题前两个数字 token，结果给「皇马2-1瓦伦西亚，27分还是不够用」
    画出了 `2-1 | 27分`——比分和积分并排在读者眼里就是这张图在胡说。
    """

    def test_score_is_split_into_two_sides(self):
        a, b, label, ta, tb = cc._extract_compare("皇马2-1瓦伦西亚，姆巴佩破荒")
        self.assertEqual((a, b), ("2", "1"))
        self.assertEqual(label, "比分")
        # 左右标签是队名：只画「2 | 1」读者不知道谁对谁
        self.assertEqual((ta, tb), ("皇马", "瓦伦西亚"))

    def test_same_unit_values_are_comparable(self):
        a, b, label, ta, tb = cc._extract_compare("巴萨34分领跑，皇马27分紧追")
        self.assertEqual((a, b), ("34分", "27分"))
        self.assertIn("分", label)
        self.assertEqual((ta, tb), ("巴萨", "皇马"))

    def test_single_number_yields_nothing(self):
        self.assertEqual(cc._extract_compare("皇马27分还不够用")[0], "")

    def test_different_units_never_get_paired(self):
        # 「3轮」和「27分」不是同一维度的量：只出现一个「分」时不能再凑一个「轮」
        a, b, *_ = cc._extract_compare("最近3轮只拿1分")
        self.assertFalse(a and b and a.endswith("分") != b.endswith("分"),
                         "不允许跨单位拼凑对比")


class TestThemes(unittest.TestCase):
    def test_all_themes_are_complete(self):
        keys = {"bg_top", "bg_bottom", "ink", "ink_dim", "ink_faint",
                "line", "accent", "chip", "highlight", "art"}
        for name, t in cc.THEMES.items():
            self.assertEqual(set(t), keys, f"主题 {name} 缺字段，会画出半张废图")

    def test_unknown_theme_falls_back(self):
        self.assertIs(cc.theme("nonsense"), cc.THEMES["navy"])
        self.assertIs(cc.theme(None), cc.THEMES["navy"])

    def test_paper_is_light_background_dark_ink(self):
        # 浅色主题一旦前景色没跟着反转，就是白底白字 —— 等于一张废图
        t = cc.THEMES["paper"]
        self.assertGreater(t["bg_top"][0], 200)
        self.assertLess(t["ink"][0], 80)


class TestFillFloor(unittest.TestCase):
    """`fill(need=3)` 的硬承诺：各种烂稿都得凑够 3 张，且视觉互不重复。"""

    CASES = {
        "正常分节稿": {"title": "皇马2-1瓦伦西亚，27分还是不够用", "column": "西甲",
                       "content": ("皇马客场2-1拿下瓦伦西亚。\n\n"
                                   "## 一、27分不是稳\n领先身后的追兵只有两分。\n\n"
                                   "## 二、球荒被放大\n最近四轮他只进了一个球。\n\n"
                                   "## 三、换帅考题\n新帅上任三场拿四分。")},
        "正文仅一句": {"title": "这支球队的更衣室到底出了什么问题", "column": "中超",
                       "content": "其实问题不在球场上，而在训练场之外的那些事。"},
        "正文为空": {"title": "那个追风少年如今发福谢顶，你的青春还能找回吗？",
                     "column": "人物", "content": ""},
        "多段无小标题": {"title": "曼城客场碰维拉，切尔西热刺谁先掉链子", "column": "英超",
                         "content": ("曼城这轮要去维拉公园，客场并不轻松。\n\n"
                                     "维拉主场已经七轮不败，防守站位很扎实。\n\n"
                                     "切尔西和热刺那场更像互相拆台。")},
    }

    def test_every_case_reaches_three(self):
        for label, art in self.CASES.items():
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as d:
                    got = cc.fill(art, out_dir=d, prefix="article-1-img",
                                  start_index=1, need=3)
                    self.assertEqual(len(got), 3,
                                     f"{label} 只补到 {len(got)} 张（{cc.LAST_ERROR}）")

    def test_three_cards_look_different(self):
        # 判据是「版式 + 配色」组合唯一：极端稿允许两张标题卡换配色，
        # 但绝不允许两张一模一样（那等于只配了一张）。
        for label, art in self.CASES.items():
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as d:
                    got = cc.fill(art, out_dir=d, prefix="article-1-img",
                                  start_index=1, need=3)
                    self.assertEqual(len({(g["source"], g.get("theme")) for g in got}), 3)

    def test_normal_article_uses_distinct_variants(self):
        # 正常稿不该退到「换配色」：版式本身就应该有三种
        with tempfile.TemporaryDirectory() as d:
            got = cc.fill(self.CASES["正常分节稿"], out_dir=d,
                          prefix="article-1-img", start_index=1, need=3)
            self.assertEqual(len({g["source"] for g in got}), 3,
                             "内容够用时三张应来自三种不同版式")

    def test_fill_never_exceeds_plan(self):
        with tempfile.TemporaryDirectory() as d:
            got = cc.fill(self.CASES["正文为空"], out_dir=d, prefix="a",
                          start_index=1, need=99)
            self.assertLessEqual(len(got), len(cc._FILL_PLAN))

    def test_used_cards_skips_only_that_combination(self):
        """封面兜底已出 title+navy 时，正文只有一句的稿子仍要补到 3 张。

        这是 2026-11-05 端到端测试抓到的真实 bug：当时传的是
        `skip_variants=("title",)`，把整个 title 版式排除掉，于是只剩 quote 一条路，
        只能补到 2 张。正确语义是「只跳过 title+navy 这一个组合」。
        """
        art = self.CASES["正文仅一句"]
        with tempfile.TemporaryDirectory() as d:
            got = cc.fill(art, out_dir=d, prefix="article-1-img", start_index=2,
                          need=2, used_cards={("title", "navy")})
            self.assertEqual(len(got), 2, f"只补到 {len(got)} 张：{cc.LAST_ERROR}")
            self.assertNotIn(("title", "navy"),
                             {(g["source"].split(":", 1)[1], g.get("theme")) for g in got})

    def test_skip_variants_still_skips_whole_variant(self):
        # 旧语义保留：明确说「这篇不要标题卡」时，任何配色的标题卡都不出
        with tempfile.TemporaryDirectory() as d:
            got = cc.fill(self.CASES["正常分节稿"], out_dir=d, prefix="a",
                          start_index=1, need=2, skip_variants=("title",))
            self.assertTrue(got)
            self.assertNotIn("card:title", {g["source"] for g in got})


class TestRenderVariants(unittest.TestCase):
    def test_each_variant_renders_or_refuses_with_reason(self):
        # 这份稿子刻意给了「分节 + 问句标题 + 数字」三样，让每种版式都有素材可抽；
        # 缺素材时各自拒绝出图的行为由下一个用例覆盖。
        art = {"title": "半场零封巴黎，里昂下半场到底哪一步走错了？", "column": "法甲",
               "content": ("里昂上半场把巴黎的边路彻底按住。\n\n"
                           "## 一、换人顺序\n真正的问题不在体能，而在换人顺序。\n\n"
                           "## 二、积分差距\n巴黎34分领跑，里昂27分紧追。")}
        for variant in cc.VARIANTS:
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory() as d:
                    p = cc.render(art, out_dir=d, variant=variant)
                    self.assertTrue(p, f"{variant} 拒绝出图却没说原因：{cc.LAST_ERROR}")
                    self.assertEqual(cc.LAST_ERROR, "")

    def test_refusals_always_carry_reason(self):
        # 「返回 None」必须能回答「为什么」——否则又是一处黑箱
        for variant, art in (
            ("points", {"title": "标题", "content": "一段话"}),
            ("numbers", {"title": "没有数字的标题", "content": "也没有数字"}),
            ("quote", {"title": "标题", "content": ""}),
            ("qa", {"title": "陈述句标题", "content": "有一句答案在这里。"}),
            ("outline", {"title": "标题", "content": "只有一段。"}),
            ("compare", {"title": "只有一个数字27分", "content": ""}),
        ):
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory() as d:
                    self.assertIsNone(cc.render(art, out_dir=d, variant=variant))
                    self.assertTrue(cc.LAST_ERROR, f"{variant} 跳过时没给原因")

    def test_quote_nth_changes_sentence(self):
        art = {"title": "标题不加问号", "column": "中超",
               "content": ("真正的问题不在体能，而在换人顺序。\n\n"
                           "但是中场的第一道屏障在下半场就没了。")}
        with tempfile.TemporaryDirectory() as d:
            p0 = cc.render(art, out_dir=d, variant="quote", nth=0, filename="q0.jpg")
            p1 = cc.render(art, out_dir=d, variant="quote", nth=1, filename="q1.jpg")
            self.assertTrue(p0 and p1)
            self.assertNotEqual(Path(p0).read_bytes(), Path(p1).read_bytes())


class TestWiring(unittest.TestCase):
    """保存层必须把版式构成与「未达下限」落盘，否则验收无从判起。"""

    def _src(self, name):
        return (ROOT / name).read_text(encoding="utf-8")

    def test_metadata_records_card_variants(self):
        src = self._src("orchestrator.py")
        self.assertIn('"image_cards"', src,
                      "没记版式构成，事后无法回答「这篇为什么三张都是标题卡」")
        self.assertIn('"variant": str(d.get("source", "")).split(":", 1)[-1]', src)
        self.assertIn('"theme": d.get("theme", "")', src)

    def test_metadata_counts_articles_below_floor(self):
        src = self._src("orchestrator.py")
        self.assertIn("articles_below", src,
                      "没记未达下限的篇数，验收只能靠逐篇重算，容易被漏掉")
        self.assertIn("MIN_ARTICLE_IMAGES", src)

    def test_shortfall_is_recorded_on_article(self):
        src = self._src("orchestrator.py")
        self.assertIn("_image_shortfall", src,
                      "补不满时只写 degrade 台账 = 没人会看，必须挂到篇上让验收报红")
        self.assertIn('"image_shortfall": art.get("_image_shortfall")', src)

    def test_audit_tier_is_hard(self):
        cfg = (ROOT / "config" / "audit_tiers.yaml").read_text(encoding="utf-8")
        block = cfg.split("article_images:", 1)[1].split("image_relevance:", 1)[0]
        self.assertIn("kind: hard", block,
                      "用户定的是硬条件，登记成 advisory 就等于只是建议")

    def test_workflow_does_not_soften_the_gate(self):
        wf = (ROOT / ".github" / "workflows" / "batch.yml").read_text(encoding="utf-8")
        idx = wf.index("verify_article_images.py")
        window = wf[max(0, idx - 900):idx + 500]
        self.assertNotIn('verify_article_images.py --quiet \\\n            || echo', window,
                         "用 `|| echo` 软化硬门禁，FAIL 了照样发布")
        self.assertIn("rc=$?", window, "硬门禁必须按退出码三态处理（1 阻断 / 2 未判定）")


class TestVerifyScript(unittest.TestCase):
    def test_variant_checks_are_wired_into_verify(self):
        src = (ROOT / "scripts" / "verify_article_images.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "verify")
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        calls = {n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn("check_variant_variety", calls, "极端稿保底没接进 verify")
        self.assertIn("check_variant_diversity", calls, "版式多样性没接进 verify")
        self.assertIn("diversity", names)

    def test_verify_selftest_covers_extreme_cases(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "vai", ROOT / "scripts" / "verify_article_images.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rep = mod.check_variant_variety()
        self.assertTrue(rep["ok"], rep["detail"])


if __name__ == "__main__":
    unittest.main()
