#!/usr/bin/env python3
"""「每篇至少 3 张相关配图」的契约测试。

2026-11-05 用户的原话：**「光有文字，没有配图不行，单篇文章至少保证 3 张相关配图」**。
在那之前，noon 批次三篇里两篇只有一张原创信息图、一篇零配图。修法不是「再加一个图源」，
而是四件事一起落地，任何一件缺了都会被悄悄退回 0~1 张：

    ① 爬取层（`news_images`）—— 真图的主要来源，本地与 CI 都通；
    ② 尺寸闸门分档 —— 封面 800×600、正文 600×400，否则新闻图床 720×480 全被拒；
    ③ 补齐机制（`cover_card.fill`）—— 网络断掉时用本篇内容派生的信息图兜住下限；
    ④ 正文注入不丢图 —— 下载了几张，正文里就必须有几张。

这个文件盯的就是 ②③④（① 在 test_news_images.py）。
"""

import ast
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cover_card  # noqa: E402
import orchestrator as orch  # noqa: E402
from image_service import ImageService  # noqa: E402

TITLE = "瓦伦西亚主帅先动还是姆巴佩先冷，皇马27分这趟客场不好踢"
CONTENT = """
开头两段导语，交代比赛背景。

## 一、27分不是稳，是刚好够用

联赛打到这个份上，领先身后的追兵只有两分。

## 二、姆巴佩的球荒被放大了

最近四轮他只进了一个球。

## 三、换帅后的第一道考题

新帅上任三场拿四分。
"""


def _imgs(n: int) -> list:
    return [{"filename": f"article-1-img-{i + 1:03d}.jpg", "local_path": f"/x/{i}.jpg",
             "description": f"配图{i + 1}"} for i in range(n)]


class TestInjectImages(unittest.TestCase):
    """下载了几张，正文里就必须有几张（旧实现会静默丢图）。"""

    def test_all_images_reach_content_when_sections_fewer(self):
        # 2 节 + 3 张：旧代码 `if si < len(downloaded)` 会让第 3 张只下载不进正文
        body = "开场\n\n## 一、第一节\n\n内容\n\n## 二、第二节\n\n内容"
        out = orch._inject_images(body, _imgs(3))
        for i in range(3):
            self.assertIn(f"article-1-img-{i + 1:03d}.jpg", out,
                          f"第 {i + 1} 张图没进正文 → 下载了读者也看不见")

    def test_one_per_section(self):
        out = orch._inject_images(CONTENT, _imgs(3))
        self.assertEqual(out.count("](images/"), 3)
        # 每节后面各一张：三个 `## ` 段落都在，图跟在段后
        self.assertEqual(len([l for l in out.split("\n") if l.startswith("## ")]), 3)

    def test_no_sections_appends_all(self):
        out = orch._inject_images("只有一段话。", _imgs(3))
        self.assertEqual(out.count("](images/"), 3)
        self.assertTrue(out.startswith("只有一段话。"))

    def test_zero_images_is_noop(self):
        self.assertEqual(orch._inject_images(CONTENT, []), CONTENT)

    def test_alt_text_uses_description(self):
        out = orch._inject_images(CONTENT, _imgs(1))
        self.assertIn("![配图1]", out)


class TestFillFloor(unittest.TestCase):
    """补齐三张：三个版式都来自这篇稿子自己的内容，且互不重复。"""

    def test_fill_reaches_three_distinct(self):
        with tempfile.TemporaryDirectory() as d:
            got = cover_card.fill({"title": TITLE, "content": CONTENT},
                                  out_dir=d, prefix="article-1-img",
                                  start_index=1, need=3)
            self.assertEqual(len(got), 3, "补不到 3 张 → 下限没达成")
            self.assertEqual(len({g["md5"] for g in got}), 3, "三张必须是不同内容")
            # 版式必须互不重复：同一篇出两张同版式卡 = 读者看到「同一张图两遍」。
            # 这里不写死具体是哪三种——版式库会扩充（11-05 从 3 种加到 7 种），
            # 写死清单只会让每次扩充都要改测试，而「互不相同」才是真正要守的契约。
            self.assertEqual(len({g["source"] for g in got}), 3, "三张必须来自不同版式")
            self.assertTrue(all(g["source"].startswith("card:") for g in got))

    def test_start_index_does_not_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as d:
            got = cover_card.fill({"title": TITLE, "content": CONTENT},
                                  out_dir=d, prefix="article-2-img",
                                  start_index=2, need=2)
            self.assertTrue(got)
            self.assertEqual(got[0]["filename"], "article-2-img-002.jpg",
                             "补齐的序号必须接在已下载的图后面，不能从 001 重来")

    def test_skip_variants_avoids_duplicate_cover(self):
        with tempfile.TemporaryDirectory() as d:
            got = cover_card.fill({"title": TITLE, "content": CONTENT},
                                  out_dir=d, prefix="article-3-img", start_index=2,
                                  need=2, skip_variants=("title",))
            self.assertEqual(len(got), 2)
            self.assertNotIn("card:title", {g["source"] for g in got},
                             "已经出过标题卡还再出一张 = 同一张图发两遍")

    def test_filename_matches_publisher_contract(self):
        import re
        with tempfile.TemporaryDirectory() as d:
            for g in cover_card.fill({"title": TITLE, "content": CONTENT},
                                     out_dir=d, prefix="article-4-img",
                                     start_index=1, need=3):
                self.assertRegex(g["filename"], r"^article-\d+-img-\d{3}\.jpg$")

    def test_need_zero_returns_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cover_card.fill({"title": TITLE}, out_dir=d,
                                             prefix="a", start_index=1, need=0), [])


class TestSizeGateTiers(unittest.TestCase):
    """封面 800×600、正文 600×400 —— 混用会把新闻图床整批拒掉。"""

    def _resp(self, data: bytes):
        class _R:
            headers = {"content-length": str(len(data))}
            status_code = 200

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=8192):
                yield data
        return _R()

    def _jpeg(self, w: int, h: int) -> bytes:
        import os
        from PIL import Image
        img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    def test_body_image_passes_with_relaxed_gate(self):
        data = self._jpeg(720, 480)          # 实测的新闻图床原图尺寸
        self.assertGreater(len(data), 50 * 1024, "样本图太小，会被 min_size 拒掉，测不到闸门")
        svc = ImageService({"images": {"min_width": 800, "min_height": 600}})
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(svc.session, "get", return_value=self._resp(data)):
            self.assertIsNone(svc.download_image("https://x/a.jpg", Path(d), "p", 1, set()),
                              "默认 800×600 闸门应当拒掉 720×480（这是旧行为，要保住）")
            got = svc.download_image("https://x/a.jpg", Path(d), "p", 1, set(),
                                     min_width=600, min_height=400)
            self.assertIsNotNone(got, "正文图放宽到 600×400 后应当能落盘")

    def test_fallback_url_used_when_primary_fails(self):
        good = self._jpeg(1280, 853)
        svc = ImageService({"images": {"min_width": 600, "min_height": 400}})
        calls = []

        def _get(url, **kw):
            calls.append(url)
            return None if "1280" in url else self._resp(good)

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(svc.session, "get", side_effect=_get):
            got = svc.download_image("https://tu.duoduocdn.com/a_1280.jpg", Path(d), "p", 1,
                                     set(), min_width=600, min_height=400,
                                     fallback_url="https://tu.duoduocdn.com/a_720.jpg")
        self.assertIsNotNone(got, "主地址失败时应回退到 _720 版本")
        self.assertEqual(len(calls), 2)
        self.assertEqual(got["url"], "https://tu.duoduocdn.com/a_1280.jpg",
                         "台账要记原始地址（可回溯），不是回退地址")


class TestSaveLayerWiring(unittest.TestCase):
    """保存层必须真的调补齐，而且下限是个常量（不是散落的魔法数 3）。"""

    def _save_fn(self):
        src = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "save_articles_local"), None)
        self.assertIsNotNone(fn, "save_articles_local 不见了")
        return fn

    def test_floor_constant_is_three(self):
        self.assertEqual(orch.MIN_ARTICLE_IMAGES, 3)
        self.assertEqual((orch.BODY_IMG_MIN_W, orch.BODY_IMG_MIN_H), (600, 400))

    def test_calls_cover_card_fill(self):
        fn = self._save_fn()
        fills = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "fill"
                 and isinstance(n.func.value, ast.Name)
                 and n.func.value.id == "cover_card"]
        self.assertTrue(fills, "保存层没有调 cover_card.fill → 下限形同未做")
        kws = {k.arg for k in fills[0].keywords}
        self.assertTrue({"need", "prefix", "start_index"} <= kws, f"补齐参数不全：{kws}")

    def test_inject_uses_helper_not_inline_loop(self):
        """正文注入必须走 `_inject_images`——内联版本会丢图（本文件第一个用例）。"""
        src = "".join(ast.get_source_segment(
            (ROOT / "orchestrator.py").read_text(encoding="utf-8"), n) or ""
            for n in ast.walk(self._save_fn()) if isinstance(n, ast.Call))
        self.assertIn("_inject_images", src)

    def test_download_passes_tier_gates(self):
        fn = self._save_fn()
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "download_image"]
        self.assertTrue(calls)
        kws = {k.arg for k in calls[0].keywords}
        self.assertTrue({"min_width", "min_height"} <= kws,
                        f"下载时没传分档闸门：{kws}")


class TestDegradePolicy(unittest.TestCase):
    def test_policy_entries_exist_and_do_not_block(self):
        import degrade
        for stage in ("配图补齐", "配图不足"):
            self.assertIn(stage, degrade.DEGRADE_POLICY)
            self.assertFalse(degrade.DEGRADE_POLICY[stage]["阻塞发布"],
                             "配图影响观感，不该拦发布")


class TestRealBatchFloor(unittest.TestCase):
    """真实产物的三态验收：没有批次产物 = 未判定（skip），不是通过。"""

    def test_latest_batch_articles_have_three_images(self):
        out = ROOT / "output"
        metas = sorted([p for p in out.glob("2026-*/metadata.json")], reverse=True)
        if not metas:
            self.skipTest("没有批次产物，未判定")
        import json
        data = json.loads(metas[0].read_text(encoding="utf-8"))
        arts = [a for a in data.get("articles") or [] if isinstance(a, dict)]
        if not arts:
            self.skipTest("批次为空，未判定")
        floor = data.get("image_floor") or {}
        since = str(floor.get("since") or "")
        day = metas[0].parent.name
        if not since or day < since:
            # 生效日之前的批次不参与判定：它们是改造前的产物，判 FAIL 只会制造噪音
            self.skipTest(f"批次 {day} 早于配图下限生效日 {since or '（未标注）'}，未判定")
        for a in arts:
            imgs = a.get("images") or a.get("downloaded_images") or []
            self.assertGreaterEqual(
                len(imgs), int(floor.get("min_per_article") or 3),
                f"《{a.get('title', '')[:20]}》只有 {len(imgs)} 张图")


if __name__ == "__main__":
    unittest.main()
