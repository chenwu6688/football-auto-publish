import json
import tempfile
import unittest
from pathlib import Path

import risk_gate


class TestRiskGate(unittest.TestCase):
    def test_injury_high_risk(self):
        r = risk_gate.assess({"title": "哈兰德伤停三周？曼城锋线告急", "content": "他因膝伤缺阵。"})
        self.assertTrue(r["high_risk"])
        self.assertIn("伤病", r["categories"])

    def test_transfer_fee_high_risk_with_amount(self):
        r = risk_gate.assess({"title": "曼城1.5亿欧报价维尔茨",
                              "content": "据悉转会费为1.5亿欧。"})
        self.assertTrue(r["high_risk"])
        self.assertIn("合同金额", r["categories"])

    def test_contract_keyword_without_amount_not_flagged(self):
        # 「合同年」不含金额量词 → 不应判高风险（保守不过度拦截）
        r = risk_gate.assess({"title": "合同年的球员最拼？", "content": "这赛季他状态回暖。"})
        self.assertFalse(r["high_risk"])

    def test_allegation_high_risk(self):
        r = risk_gate.assess({"title": "某球员被指控假球，已立案调查",
                              "content": "俱乐部回应称将配合调查。"})
        self.assertTrue(r["high_risk"])
        self.assertIn("争议指控", r["categories"])

    def test_normal_match_not_flagged(self):
        r = risk_gate.assess({"title": "曼城主场能否啃下铁桶阵？",
                              "content": "双方上半场互交白卷，下半场易边再战。"})
        self.assertFalse(r["high_risk"])

    def test_queue_roundtrip_and_materialize(self):
        tmp = Path(tempfile.mkdtemp())
        orig_q = risk_gate.QUEUE_PATH
        risk_gate.QUEUE_PATH = tmp / "pending_review.json"
        try:
            art = {"title": "哈兰德伤停", "content": "膝伤缺阵三周。",
                   "keywords": ["哈兰德"], "content_type": "热点球评",
                   "_column_name": "人物故事", "_batch_name": "晨读"}
            rid = risk_gate.enqueue(art, "2026-10-10", 1, {"reasons": ["伤病"], "categories": ["伤病"]})
            self.assertEqual(len(risk_gate.pending_items()), 1)
            self.assertEqual(risk_gate.pending_items()[0]["id"], rid)

            # 未确认 → 不入 publish（pending 仍为 1）
            out = tmp / "output"
            self.assertEqual(risk_gate.materialize("2026-10-10", output_dir=out), [])

            # 确认 → materialize 写回
            risk_gate.resolve(rid, approve=True)
            written = risk_gate.materialize("2026-10-10", output_dir=out)
            self.assertEqual(len(written), 1)
            meta = json.loads((out / "2026-10-10" / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["articles"][0]["review"], "approved")
            self.assertEqual(risk_gate.pending_items(), [])
        finally:
            risk_gate.QUEUE_PATH = orig_q

    def test_reject(self):
        tmp = Path(tempfile.mkdtemp())
        orig = risk_gate.QUEUE_PATH
        risk_gate.QUEUE_PATH = tmp / "q.json"
        try:
            rid = risk_gate.enqueue({"title": "x", "content": "受伤"}, "2026-10-10", 2, {"reasons": []})
            risk_gate.resolve(rid, approve=False)
            self.assertEqual(risk_gate.pending_items(), [])
        finally:
            risk_gate.QUEUE_PATH = orig


if __name__ == "__main__":
    unittest.main()
