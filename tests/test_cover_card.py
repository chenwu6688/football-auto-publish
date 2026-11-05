#!/usr/bin/env python3
"""封面数据卡兜底（cover_card）的单测。

这里的核心不是「画得好看」，而是三条**契约**：

1. 生成的文件名必须落在 `publisher.extract_images` 的正则里——否则图生成了，
   但台账 `images` 计数为 0、头条封面也不会自动选中，等于没配图；
2. 尺寸必须过发布端 800×600 闸门；
3. 任何异常都必须返回 None，绝不把「配图」这条支线升级成阻塞发布的事故。
"""

import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cover_card  # noqa: E402

TITLE = "1平2负只拿0.33分，中超这根独苗还能撑到2028吗？"


def _publisher_image_regex() -> str:
    """从 publisher.extract_images 源码里取出真正的正则，避免测试自己复刻一份。"""
    src = (ROOT / "publisher.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "extract_images":
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "findall"
                        and sub.args
                        and isinstance(sub.args[0], ast.Constant)):
                    return sub.args[0].value
    raise AssertionError("publisher.extract_images 里没找到 re.findall 的正则")


class TestKeyNumbers(unittest.TestCase):
    def test_record_group_before_single_metric(self):
        # 「1平2负」必须整体成一个 token：交替式里若让 `\d+分` 之类先命中，
        # 会被切成「2负」，数据卡上就只剩半截战绩。
        self.assertEqual(cover_card._key_numbers(TITLE)[:2], ["1平2负", "0.33分"])

    def test_score_line(self):
        self.assertEqual(cover_card._key_numbers("皇马3-1巴萨，谁还说他老了")[0], "3-1")

    def test_no_number(self):
        self.assertEqual(cover_card._key_numbers("这场球看得人心里堵得慌"), [])

    def test_limit_respected(self):
        self.assertLessEqual(len(cover_card._key_numbers(
            "5轮3胜1平1负，积10分，排名第2，7个净胜球")), 3)


class TestRender(unittest.TestCase):
    def test_size_passes_publish_gate(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            p = cover_card.render({"title": TITLE}, out_dir=d)
            self.assertTrue(p and Path(p).exists())
            with Image.open(p) as im:
                self.assertGreaterEqual(im.width, 800)
                self.assertGreaterEqual(im.height, 600)
                self.assertLess(abs(im.width / im.height - 16 / 9), 0.02)

    def test_filename_contract_with_publisher(self):
        """数据卡文件名必须能被发布端 extract_images 认出来（真实正则，不是复刻）。"""
        pat = re.compile(_publisher_image_regex())
        with tempfile.TemporaryDirectory() as d:
            p = cover_card.render({"title": TITLE}, out_dir=d,
                                  filename="article-3-img-001.jpg")
            self.assertTrue(p)
            # 发布端从正文 markdown 里抓引用，这里按同样的正文形态验证
            body = f"开头\n\n![数据卡](images/{Path(p).name})\n\n正文"
            self.assertEqual(pat.findall(body), [f"images/{Path(p).name}"])
            # 默认命名（card-xxx.jpg）反而抓不到——这正是必须传 filename 的原因
            p2 = cover_card.render({"title": TITLE}, out_dir=d)
            self.assertTrue(p2)
            self.assertEqual(pat.findall(f"![x](images/{Path(p2).name})"), [])

    def test_empty_title_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(cover_card.render({"title": ""}, out_dir=d))
            self.assertIsNone(cover_card.render({}, out_dir=d))

    def test_selftest_all_pass(self):
        bad = [n for n, ok in cover_card.selftest() if not ok]
        self.assertEqual(bad, [])


class TestSaveFallbackWiring(unittest.TestCase):
    """零配图分支必须真的调 cover_card，且落盘名走发布端命名。"""

    def test_orchestrator_calls_cover_card_in_empty_branch(self):
        tree = ast.parse((ROOT / "orchestrator.py").read_text(encoding="utf-8"))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "save_articles_local"), None)
        self.assertIsNotNone(fn, "save_articles_local 不见了")
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "render"
                 and isinstance(n.func.value, ast.Name)
                 and n.func.value.id == "cover_card"]
        self.assertTrue(calls, "零配图分支没有接入 cover_card.render → P0-3 形同未做")
        # 必须传 filename，否则生成 card-xxx.jpg，发布端抓不到 → 台账仍记 0 图
        kws = {k.arg for k in calls[0].keywords}
        self.assertIn("filename", kws)

    def test_degrade_policy_has_cover_entry(self):
        import degrade
        self.assertIn("封面兜底", degrade.DEGRADE_POLICY)
        self.assertFalse(degrade.DEGRADE_POLICY["封面兜底"]["阻塞发布"])


class TestNoCJKFontContract(unittest.TestCase):
    """2026-11-05 方框事故的防回归：没有中文字体时**不许出图**。

    CI（ubuntu-latest）上没有 CJK 字体，`_font()` 退回 Pillow 位图字体，卡片上的
    「球评人老六」「老六心中的历史射手榜」全被画成一排 .notdef 方框，而 `render()`
    照常返回路径、台账照常记 1 张图、发布照常成功——用户看到的就是「封面是一堆方块」。
    """

    def setUp(self):
        self._saved = (cover_card._FONT_PATH_CACHE, cover_card.LAST_ERROR)
        cover_card._FONT_PATH_CACHE = ""

    def tearDown(self):
        cover_card._FONT_PATH_CACHE, cover_card.LAST_ERROR = self._saved

    def test_font_raises_instead_of_bitmap_fallback(self):
        with self.assertRaises(cover_card.NoCJKFont):
            cover_card._font(40)

    def test_render_returns_none_and_keeps_reason(self):
        with tempfile.TemporaryDirectory() as d:
            p = cover_card.render({"title": TITLE}, out_dir=d)
            self.assertIsNone(p, "没有中文字体却仍生成图片 → 会发布方框封面")
            self.assertIn("中文字体", cover_card.LAST_ERROR)
            self.assertEqual(list(Path(d).glob("*.jpg")), [], "不许留下半张图")

    def test_font_status_reports_missing(self):
        ok, detail = cover_card.font_status()
        self.assertFalse(ok)
        self.assertIn("字体", detail)


class TestCIFontInstall(unittest.TestCase):
    """根因在环境：CI 必须装中文字体，否则上面的 None 分支就是常态。"""

    def test_batch_workflow_installs_cjk_font(self):
        wf = (ROOT / ".github" / "workflows" / "batch.yml").read_text(encoding="utf-8")
        self.assertIn("fonts-noto-cjk", wf,
                      "batch.yml 没装中文字体 → 每批封面数据卡都会跳成无封面")
        self.assertIn("--fontstatus", wf, "装完字体应当场自检，别等出图才发现")

    def test_unit_test_workflow_installs_cjk_font(self):
        wf = (ROOT / ".github" / "workflows" / "unit-tests.yml").read_text(encoding="utf-8")
        self.assertIn("fonts-noto-cjk", wf,
                      "单测环境缺中文字体 → 渲染用例会红，而红的原因是环境不是代码")

    def test_font_candidates_have_no_latin_only_fallback(self):
        """候选表里不许出现 DejaVu 这类「能打开、画不出汉字」的字体。"""
        for p in cover_card._FONT_CANDIDATES:
            self.assertNotIn("DejaVu", p, f"{p} 没有汉字字形，不能当兜底字体")


class TestReservoirImages(unittest.TestCase):
    """蓄水池补位的软性稿也必须走配图检索（2026-11-05：2/3 篇封面是数据卡）。"""

    def test_reservoir_branch_calls_search_images(self):
        src = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "images_map[len(articles) - 1] = []", src,
            "蓄水池分支又被硬编码成「不配图」了 —— 软性稿会 100% 落到数据卡")
        tree = ast.parse(src)
        # 找到含 reservoir.fill_gap 的那段（所在函数体），确认里面有 search_images 调用
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                seg = ast.get_source_segment(src, node) or ""
                if "reservoir.fill_gap" in seg:
                    target = seg
                    break
        self.assertIsNotNone(target, "找不到蓄水池补位代码")
        self.assertIn("search_images(", target, "蓄水池条目没有检索配图")


if __name__ == "__main__":
    unittest.main()
