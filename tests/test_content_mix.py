"""内容类型配比（content_mix）与验收脚本单测。

覆盖 2026-11-01 月度分析落地的四件事，每条都带负向断言——
只测「正常时通过」等于没测，必须证明「弄坏会报警」。
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import content_mix  # noqa: E402
import reservoir  # noqa: E402
import verify_content_mix as vcm  # noqa: E402

CST = timezone(timedelta(hours=8))


def _mk(ct, n=1, reads=None):
    return [{"content_type": ct, "reads": reads} for _ in range(n)]


class TestTargetTable(unittest.TestCase):
    def test_targets_sum_to_100(self):
        self.assertAlmostEqual(sum(content_mix.TARGET_PCT.values()), 100.0, places=2)

    def test_banned_type_has_zero_target(self):
        """reservoir 禁发的类型，配比表必须是 0 —— 两处口径不许漂移。"""
        for ct in reservoir.BANNED_TYPES:
            self.assertEqual(content_mix.TARGET_PCT.get(ct), 0, f"{ct} 应为 0")

    def test_verify_catches_sum_drift(self):
        """负向：目标表合计改了 → 硬项 A 必须 FAIL。"""
        orig = dict(content_mix.TARGET_PCT)
        try:
            content_mix.TARGET_PCT["热点球评"] -= 1.0       # 合计变 99（不写死具体值）
            r = vcm.check_target_table()
            self.assertFalse(r["ok"])
            self.assertTrue(any("合计" in d for d in r["detail"]))
        finally:
            content_mix.TARGET_PCT.clear()
            content_mix.TARGET_PCT.update(orig)

    def test_verify_catches_banned_with_target(self):
        """负向：给禁发类型塞了目标 → 硬项 A 必须 FAIL。"""
        orig = dict(content_mix.TARGET_PCT)
        try:
            content_mix.TARGET_PCT["历史回顾"] = 5.0       # 合计超 100，且违反禁发
            r = vcm.check_target_table()
            self.assertFalse(r["ok"])
            self.assertTrue(any("禁发" in d for d in r["detail"]))
        finally:
            content_mix.TARGET_PCT.clear()
            content_mix.TARGET_PCT.update(orig)

    def test_verify_catches_soft_banned_overlap(self):
        """负向（负向验证逼出来的）：把禁发类型加回 SOFT_TYPES，硬项 A 必须 FAIL。

        此前只比 BANNED_TYPES —— 清单没动就全绿，但语义已经矛盾
        （既允许生成又禁发）。这条守的是「口径自洽」而不是「清单内容」。
        """
        orig = reservoir.SOFT_TYPES
        try:
            reservoir.SOFT_TYPES = tuple(orig) + ("历史回顾",)
            r = vcm.check_target_table()
            self.assertFalse(r["ok"])
            self.assertTrue(any("冲突" in d for d in r["detail"]), r["detail"])
        finally:
            reservoir.SOFT_TYPES = orig

    def test_soft_and_banned_disjoint(self):
        self.assertEqual(set(reservoir.SOFT_TYPES) & set(reservoir.BANNED_TYPES), set())


class TestNormalize(unittest.TestCase):
    def test_aliases_merged(self):
        """「战术解析」并入「战术榜单」，否则同一类内容被拆成两半互相「合格」。"""
        self.assertEqual(content_mix.normalize("战术解析"), "战术榜单")
        self.assertEqual(content_mix.normalize("榜单"), "排行榜")
        self.assertEqual(content_mix.normalize("紧急球评"), "热点球评")

    def test_known_type_passthrough(self):
        self.assertEqual(content_mix.normalize("热点球评"), "热点球评")

    def test_empty_becomes_unclassified(self):
        self.assertEqual(content_mix.normalize(""), "未分类")
        self.assertEqual(content_mix.normalize(None), "未分类")


class TestCheck(unittest.TestCase):
    def test_banned_type_is_hard_fail(self):
        """负向核心：出现禁发类型必须 ok=False 且进 zero_hits。"""
        rep = content_mix.check(_mk("历史回顾", 3) + _mk("热点球评", 10))
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["zero_hits"].get("历史回顾"), 3)

    def test_banned_detected_via_alias_free_name(self):
        """历史回顾没有别名，直接命中。反向确认别名不会把它「洗白」。"""
        rep = content_mix.check(_mk("历史回顾", 1))
        self.assertFalse(rep["ok"])

    def test_no_banned_passes(self):
        rep = content_mix.check(_mk("热点球评", 10) + _mk("战术榜单", 5))
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["zero_hits"], {})

    def test_small_sample_not_judged(self):
        """样本不足不下结论——否则「1 篇 100%」天天报警。"""
        rep = content_mix.check(_mk("数据对比", 1) + _mk("热点球评", 10))
        self.assertIn("样本不足", rep["items"]["数据对比"]["verdict"])

    def test_deviation_beyond_tolerance_warns(self):
        """超容差进 warnings，但**不影响 ok**（配比是慢变量，不该阻断）。"""
        rep = content_mix.check(_mk("战术榜单", 20) + _mk("热点球评", 2))
        self.assertTrue(rep["ok"])
        self.assertTrue(any("战术榜单" in w for w in rep["warnings"]))

    def test_unregistered_type_warns_not_fails(self):
        """新类型没登记 → 告警但不硬失败，避免「新分类一上就把发布卡死」。"""
        rep = content_mix.check(_mk("全新类型", 6) + _mk("热点球评", 10))
        self.assertTrue(rep["ok"])
        self.assertTrue(any("未在目标配比表登记" in w for w in rep["warnings"]))

    def test_avg_reads_only_when_present(self):
        rep = content_mix.check([{"content_type": "热点球评", "reads": 100},
                                 {"content_type": "热点球评", "reads": 50},
                                 {"content_type": "战术榜单"}])
        self.assertEqual(rep["items"]["热点球评"]["avg_reads"], 75.0)
        self.assertIsNone(rep["items"]["战术榜单"]["avg_reads"])


class TestLoadRange(unittest.TestCase):
    def test_range_reads_only_requested_days(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            for d, ct in (("2026-10-29", "热点球评"), ("2026-10-31", "战术榜单"),
                          ("2026-11-02", "人物故事")):
                dd = tmp / d
                dd.mkdir()
                (dd / "metadata.json").write_text(json.dumps(
                    {"date": d, "articles": [{"title": "t", "slug": "s",
                                              "content_type": ct,
                                              "performance": {"reads": 5}}]}),
                    encoding="utf-8")
            got = content_mix.load_range_articles(tmp, "2026-10-30", "2026-10-31")
            self.assertEqual([a["date"] for a in got], ["2026-10-31"])
            self.assertEqual(got[0]["content_type"], "战术榜单")
            self.assertEqual(got[0]["reads"], 5)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_non_date_dirs_ignored(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "logs").mkdir()
            (tmp / "2026-10-31").mkdir()
            (tmp / "2026-10-31" / "metadata.json").write_text(
                json.dumps({"articles": [{"content_type": "热点球评"}]}), encoding="utf-8")
            got = content_mix.load_range_articles(tmp, "2026-10-31", "2026-10-31")
            self.assertEqual(len(got), 1)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestVerifyWiring(unittest.TestCase):
    def test_wired_detects_call(self):
        r = vcm.check_wired()
        self.assertTrue(r["ok"], r["detail"])

    def test_wired_fails_when_softened(self):
        """负向：调用行加了 `|| true` → 必须报「被软化」。"""
        orig_text = vcm.BATCH_YML.read_text(encoding="utf-8")
        try:
            bad = orig_text.replace(
                "python3 scripts/verify_content_mix.py --days 7 --quiet",
                "python3 scripts/verify_content_mix.py --days 7 --quiet || true")
            vcm.BATCH_YML.write_text(bad, encoding="utf-8")
            r = vcm.check_wired()
            self.assertFalse(r["ok"])
            self.assertTrue(any("软化" in d for d in r["detail"]))
        finally:
            vcm.BATCH_YML.write_text(orig_text, encoding="utf-8")

    def test_wired_fails_when_missing(self):
        """负向：调用被删掉 → 必须报「未接入」。"""
        orig_text = vcm.BATCH_YML.read_text(encoding="utf-8")
        try:
            bad = orig_text.replace("python3 scripts/verify_content_mix.py --days 7 --quiet",
                                    "# 复核被删了")
            vcm.BATCH_YML.write_text(bad, encoding="utf-8")
            r = vcm.check_wired()
            self.assertFalse(r["ok"])
            self.assertTrue(any("未调用" in d for d in r["detail"]))
        finally:
            vcm.BATCH_YML.write_text(orig_text, encoding="utf-8")


class TestVerifyEndToEnd(unittest.TestCase):
    def test_verify_ok_on_clean_history(self):
        """只有干净成稿时整体通过（本机可能无 output → advisory 也算通过）。"""
        rep = vcm.verify(days=7)
        self.assertIn("checks", rep)
        self.assertEqual(len(rep["checks"]), 4)

    def test_banned_type_blocks_verify(self):
        """负向：把窗口内塞一篇历史回顾 → 硬项 B 必须 FAIL、整体 ok=False。"""
        tmp = Path(tempfile.mkdtemp())
        orig_dir = vcm.OUTPUT_DIR
        orig_since = vcm.MIX_SINCE
        try:
            today = datetime.now(CST).strftime("%Y-%m-%d")
            d = tmp / today
            d.mkdir()
            (d / "metadata.json").write_text(json.dumps(
                {"articles": [{"content_type": "历史回顾", "title": "x"}]}), encoding="utf-8")
            vcm.OUTPUT_DIR = tmp
            vcm.MIX_SINCE = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")
            rep = vcm.verify(days=7)
            self.assertFalse(rep["ok"])
            self.assertTrue(any("禁发类型" in x for x in rep["hard_failed"]))
        finally:
            vcm.OUTPUT_DIR = orig_dir
            vcm.MIX_SINCE = orig_since
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestReservoirBan(unittest.TestCase):
    def test_banned_not_in_soft_types(self):
        self.assertNotIn("历史回顾", reservoir.SOFT_TYPES)
        self.assertIn("历史回顾", reservoir.BANNED_TYPES)

    def test_is_banned(self):
        self.assertTrue(reservoir.is_banned("历史回顾"))
        self.assertFalse(reservoir.is_banned("人物故事"))
        self.assertFalse(reservoir.is_banned(""))
        self.assertFalse(reservoir.is_banned(None))

    def test_fill_gap_skips_banned_and_keeps_status(self):
        """禁发条目既不补位、也**不被标记 used** —— 否则库存悄悄被吃掉却从不产出。"""
        orig = reservoir.ITEMS_PATH
        tmp = Path(tempfile.mkdtemp()) / "items.json"
        try:
            now = datetime.now(CST)
            exp = (now + timedelta(days=10)).isoformat(timespec="seconds")
            tmp.write_text(json.dumps({"items": [
                {"id": "a", "status": "stocked", "expires_at": exp,
                 "content_type": "历史回顾", "article": {"title": "老故事"}},
                {"id": "b", "status": "stocked", "expires_at": exp,
                 "content_type": "人物故事", "article": {"title": "人物"}},
            ]}), encoding="utf-8")
            reservoir.ITEMS_PATH = tmp
            picked = reservoir.fill_gap(5, today=now)
            ids = [p["id"] for p in picked]
            self.assertEqual(ids, ["b"])
            after = json.loads(tmp.read_text(encoding="utf-8"))
            banned = next(i for i in after["items"] if i["id"] == "a")
            self.assertEqual(banned["status"], "stocked")
        finally:
            reservoir.ITEMS_PATH = orig
            import shutil
            shutil.rmtree(tmp.parent, ignore_errors=True)

    def test_available_hides_banned(self):
        orig = reservoir.ITEMS_PATH
        tmp = Path(tempfile.mkdtemp()) / "items.json"
        try:
            now = datetime.now(CST)
            exp = (now + timedelta(days=10)).isoformat(timespec="seconds")
            tmp.write_text(json.dumps({"items": [
                {"id": "a", "status": "stocked", "expires_at": exp, "content_type": "历史回顾"},
            ]}), encoding="utf-8")
            reservoir.ITEMS_PATH = tmp
            self.assertEqual(reservoir.available(today=now), [])
        finally:
            reservoir.ITEMS_PATH = orig
            import shutil
            shutil.rmtree(tmp.parent, ignore_errors=True)


class TestRefillPoolGuard(unittest.TestCase):
    def test_refill_pool_has_no_banned_type(self):
        """软性轮换池不得含禁发类型——含了会「生成完再被过滤」，白烧额度。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "refill_reservoir", str(PROJECT_ROOT / "scripts" / "refill_reservoir.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        bad = [a["content_type"] for a in mod.SOFT_ANGLES if reservoir.is_banned(a["content_type"])]
        self.assertEqual(bad, [], f"轮换池含禁发类型：{bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ============================================================
# 选题供给侧必须真的产出「数据对比」（2026-11-11）
# ============================================================
#
# 背景：结构验收长期红在「数据型 14.3%（≥38%）+ 战术榜单 19%（≤14%）」，
# 而根因不在验收侧——`prompts/topic_selector.txt` 的内容品类里**根本没有**
# 「数据对比」，只有「战术解析」（配比目标 0）。于是每批都写
# `degraded: ["data 槽用次优类型补位"]`，data 槽候选池恒空。
# 加媒体源、调阈值对这一型都无效：没有这类选题，就没有这类稿。

def _selector_prompt() -> str:
    return (PROJECT_ROOT / "prompts" / "topic_selector.txt").read_text(encoding="utf-8")


def test_selector_prompt_declares_data_compare_category():
    text = _selector_prompt()
    assert "数据对比" in text, "选题提示词没有「数据对比」品类——data 槽候选池会永远为空"
    assert "每批必须含 1 篇" in text or "每批至少 1 条" in text, \
        "只列品类不要求篇数，模型照样一篇都不写"
    # 数字必须来自素材，不许自己算/编（与三要素同一条纪律）
    assert "不许自己算或编" in text


def test_selector_prompt_bans_tactics_analysis_category():
    """「战术解析」配比目标是 0，不能再作为可选品类出现在提示词里。

    否则模型会继续产出这一类 → 生成侧按板块落成「战术榜单」→ 冲高上限。
    """
    text = _selector_prompt()
    assert "禁止「战术解析」" in text
    assert '"content_type": "热点球评/转会资讯/排行榜/八卦趣事/战术解析"' not in text


def test_content_type_enum_excludes_zero_target():
    """提示词允许模型选的类型，必须都在 TARGET_PCT 里且目标 > 0。"""
    text = _selector_prompt()
    for ct in ("热点球评", "数据对比", "排行榜", "转会资讯", "八卦趣事"):
        assert ct in text
        assert content_mix.TARGET_PCT.get(ct, 0) > 0, f"{ct} 目标为 0，不该出现在可选清单"
