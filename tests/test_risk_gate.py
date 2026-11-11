"""风险闸门单测：只有「争议指控」一种结果，只有「拦 / 不拦」两种结论。

写法约定：负向断言必须带真实反例（足球稿里最常见的伤停/续约句子），
否则「闸门放宽」会被写成「闸门没坏」。
"""

import unittest

import risk_gate


class TestRiskGate(unittest.TestCase):
    def test_allegation_blocks(self):
        """断言真人涉违法/丑闻 → 必须转人工（平台 70 分档）。"""
        for art in (
            {"title": "某球员被指控假球，已立案调查", "content": "俱乐部配合调查。"},
            {"title": "主帅涉嫌受贿被拘留", "content": "俱乐部暂未回应。"},
            {"title": "他被查出使用兴奋剂", "content": "联盟已启动程序。"},
        ):
            self.assertTrue(risk_gate.blocked_reason(art), art["title"])
            self.assertEqual(risk_gate.blocked_reason(art).split("(命中")[0], "争议指控")

    def test_injury_and_money_never_block(self):
        """伤病/合同金额在足球稿里是常规背景，任何情况下都不许拦。

        2026-11-10 evening / 11-11 morning 连续两批 4/4 篇被拦、发布 0 篇，
        拦截原因就是「伤病(伤停)」「合同金额(合同)」——这两类平台根本不扣分。
        """
        for art in (
            {"title": "哈兰德伤停三周？曼城锋线告急", "content": "他因膝伤缺阵，慎防赛季报销。"},
            {"title": "曼城1.5亿欧报价维尔茨", "content": "据悉转会费1.5亿欧，周薪40万镑。"},
            {"title": "合同年的球员最拼？", "content": "续约谈判已启动，年薪方案待定。"},
            {"title": "他因累积黄牌禁赛一场", "content": "俱乐部被足协罚款，已提出申诉。"},
        ):
            self.assertEqual(risk_gate.blocked_reason(art), "", art["title"])

    def test_normal_commentary_passes(self):
        art = {"title": "曼城主场能否啃下铁桶阵？",
               "content": "双方上半场互交白卷，下半场易边再战。"}
        self.assertEqual(risk_gate.blocked_reason(art), "")

    def test_single_entry_point_only(self):
        """只留一个判定函数：旧的 assess/is_blocked/severity/队列全部已删。

        这条不是风格洁癖——留两套并行判定，就一定会出现「改了一处、另一处还在拦」，
        11-10/11-11 的 0 篇事故正是这么来的。
        """
        for gone in ("assess", "is_blocked", "enqueue", "pending_items",
                     "resolve", "materialize", "QUEUE_PATH",
                     "RISK_CATEGORIES", "BLOCKING_CATEGORIES"):
            self.assertFalse(hasattr(risk_gate, gone), f"{gone} 应已删除")

    def test_empty_or_bad_input_never_crashes(self):
        """字段缺失是常态（上游可能只给标题），闸门不许因此崩掉整批。"""
        self.assertEqual(risk_gate.blocked_reason({}), "")
        self.assertEqual(risk_gate.blocked_reason(None), "")
        self.assertEqual(risk_gate.blocked_reason({"title": None, "content": None}), "")
        self.assertTrue(risk_gate.blocked_reason({"title": "涉嫌打假球"}))


if __name__ == "__main__":
    unittest.main()
