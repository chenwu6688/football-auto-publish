"""蓄水池入库前必须自判「诱导话术」，否则整批会被出口合规丢掉。

2026-11-11 晚间实证：池子里 8 条 status=stocked 的备货全部带
「…关注…」话术（生成它们的提示词当时强制要求写一句账号引导）。晚间批刚好吃掉
其中 2 条，出口合规按 TT-07/TT-08 各判 20 分把两篇都整篇剔除，发布集合 0 篇、
``batches_completed`` 不写 evening、心跳报「缺失批次」。

修法有两道：
  · 生产端：``scripts/refill_reservoir.py`` 的提示词删掉账号引导要求；
  · 入库端：``reservoir.stock()`` 入库前自判（本文件守的就是这道）。
这两道必须都在：只改提示词，下次谁改了示例又漏过去；只加入库判定，
模型会白烧一半额度在「写完被拒收」上。
"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import reservoir


class TestReservoirInducementGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = reservoir.RESERVOIR_DIR
        self._orig_items = reservoir.ITEMS_PATH
        reservoir.RESERVOIR_DIR = self.tmp / "reservoir"
        reservoir.ITEMS_PATH = reservoir.RESERVOIR_DIR / "items.json"
        self.now = datetime(2026, 11, 12, 12, 0)

    def tearDown(self):
        reservoir.RESERVOIR_DIR = self._orig_dir
        reservoir.ITEMS_PATH = self._orig_items

    def test_refuses_item_with_follow_solicitation(self):
        content = "老哥们聊完了。" + "x" * 240 + "关注老六，这类老故事我还存着一柜子。"
        rid = reservoir.stock({"title": "梅西C罗数据对比", "content": content,
                               "content_type": "数据对比"}, now=self.now)
        self.assertEqual(rid, "")
        self.assertEqual(reservoir.available(self.now), [])

    def test_refuses_item_with_digit_vote(self):
        content = ("这球你怎么看？" + "y" * 240
                   + "评论区站队，觉得该罚下的扣1，觉得没问题的扣2。")
        rid = reservoir.stock({"title": "争议判罚复盘", "content": content}, now=self.now)
        self.assertEqual(rid, "")

    def test_accepts_clean_item(self):
        content = "老哥们聊完了。" + "x" * 240 + "你要是主教练，这脚球会怎么处理？"
        rid = reservoir.stock({"title": "老将的最后一舞", "content": content,
                               "content_type": "人物故事"}, now=self.now)
        self.assertTrue(rid)
        self.assertEqual(len(reservoir.available(self.now)), 1)

    def test_refusal_is_not_silent(self):
        """拒收要能被读到：返回空串 + 打印篇名，调用方据此跳过并计数。"""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            reservoir.stock({"title": "带话术的稿", "content": "x" * 300 + "关注我"},
                            now=self.now)
        self.assertIn("拒收", buf.getvalue())


if __name__ == "__main__":
    unittest.main()


def test_live_pool_has_no_stocked_item_with_inducement():
    """真实池子里的现役备货必须是干净的（11-11 那次就是池子自己带毒）。

    这条读的是仓库里的 data/reservoir/items.json，不是夹具：池子是数据，
    数据脏了代码再对也白搭。文件不存在时跳过（本地精简检出）。
    """
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "data" / "reservoir" / "items.json"
    if not path.exists():
        import pytest
        pytest.skip("本地无蓄水池数据文件")
    data = json.loads(path.read_text(encoding="utf-8"))
    bad = []
    for it in data.get("items") or []:
        if it.get("status") != "stocked":
            continue
        content = (it.get("article") or {}).get("content") or ""
        if not reservoir_res_inducement_ok(content):
            bad.append((it.get("title") or "")[:24])
    assert not bad, f"现役备货含诱导话术：{bad}"


def reservoir_res_inducement_ok(content: str) -> bool:
    import interaction_bait
    res = interaction_bait.check_inducement(content)
    return bool(res.get("ok") or res.get("degraded"))
