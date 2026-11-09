#!/usr/bin/env python3
"""TT-03 成稿侧「时间词」判据 + 本轮三处前移/换口径的接线单测（2026-11-09 立）。

守的是三件容易被「看起来改了」骗过去的事：

1. **TT-03 的第二截判据真的存在且能被打红**。此前 `why_keep` 声称三道判据，
   代码只有两道，而 `verify_rule_coverage` 的 B 项只验 judge 函数在不在，
   查不出「承诺了没实现」—— 假绿。所以这里既有正向（不误报）也有负向（必须红）。
2. **禁发类型前移到类别层**：`content_mix.is_banned` 是唯一判据，且必须**先归一后判**
   （`榜单`/`战术解析` 是别名，直接拿 ZERO_TARGET 做集合比对会漏）。
3. **素材层不再用 15 支硬编码球队**：表外球队不该再被无声误杀。

每条负向用例都写成「把实现弄坏就必须红」的形态，不是只测 happy path。
"""

from __future__ import annotations

import ast
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import content_mix  # noqa: E402
import verify_compliance as vc  # noqa: E402


def _item(body, *, day: date | None = None) -> dict:
    d = day or date.today()
    return {"date": d.isoformat(), "dir": PROJECT_ROOT / "output",
            "art": {"title": "正常标题"}, "_body": body}


class TestTimeWordJudge(unittest.TestCase):
    """TT-03 第二截：正文绝对日期 vs 成稿日。"""

    def setUp(self):
        self.rule = {"id": "TT-03"}

    def test_干净正文判通过(self):
        r = vc.j_time_word(_item("这是一段正常的正文，讲中场的变化。"), self.rule)
        self.assertEqual(r["status"], vc.PASS, r)

    def test_今天锚点与成稿日不符必须判红(self):
        d = date(2026, 11, 9)
        r = vc.j_time_word(_item("今天（11月3日）那场球，中场被压住了。", day=d), self.rule)
        self.assertEqual(r["status"], vc.FAIL, r)
        self.assertIn("11月3日", r["detail"])

    def test_今天锚点与成稿日一致不得误报(self):
        d = date(2026, 11, 9)
        r = vc.j_time_word(_item("今天（11月9日）那场球，中场被压住了。", day=d), self.rule)
        self.assertEqual(r["status"], vc.PASS, r)

    def test_多处未来日期必须判红(self):
        d = date(2026, 11, 9)
        body = "2026年12月1日还有一轮，2026年12月1日再确认一次。"
        r = vc.j_time_word(_item(body, day=d), self.rule)
        self.assertEqual(r["status"], vc.FAIL, r)

    def test_单处未来日期按赛程前瞻放行(self):
        """下一轮赛程写在正文里是常态，一处不能判红。"""
        d = date(2026, 11, 9)
        r = vc.j_time_word(_item("下一轮 2026年12月1日开打。", day=d), self.rule)
        self.assertEqual(r["status"], vc.PASS, r)

    def test_提过去日期不得误判为旧闻新发(self):
        """回顾/历史对比是足球稿的常态，这是最容易误伤的方向。"""
        d = date(2026, 11, 9)
        r = vc.j_time_word(_item("2018年5月26日那场决赛，他替补登场。", day=d), self.rule)
        self.assertEqual(r["status"], vc.PASS, r)

    def test_比分数字不得被当成日期(self):
        d = date(2026, 11, 9)
        r = vc.j_time_word(_item("最终 3-1，半场 1-0，控球 62%。", day=d), self.rule)
        self.assertEqual(r["status"], vc.PASS, r)

    def test_读不到正文一律未判定(self):
        """11-06 的规矩：拿标题冒充正文会把整批发布挡掉。"""
        r = vc.j_time_word({"date": "2026-11-09", "dir": PROJECT_ROOT / "no_such_dir",
                            "art": {"title": "今天（11月3日）"}, "_body": None}, self.rule)
        self.assertEqual(r["status"], vc.UNJ, r)

    def test_非法日期不炸(self):
        d = date(2026, 11, 9)
        r = vc.j_time_word(_item("2026年13月45日这种不是日期。", day=d), self.rule)
        self.assertEqual(r["status"], vc.PASS, r)


class TestTimeWordWiring(unittest.TestCase):
    """规则表 ↔ 判据表必须对得上，否则判据永远不被调度。"""

    def test_规则表_TT03_绑了两截判据(self):
        import yaml
        rules = {r["id"]: r for r in
                 (yaml.safe_load((PROJECT_ROOT / "config" / "toutiao_rules.yaml")
                                 .read_text(encoding="utf-8")) or {})["rules"]}
        names = [x for x in (rules["TT-03"].get("judge") or "").replace(" ", "").split("+") if x]
        self.assertEqual(names, ["freshness", "time_word"])

    def test_time_word_已注册进调度表(self):
        self.assertIn("time_word", vc._SINGLE)
        self.assertIn("time_word", vc._judge_names({"judge": "freshness + time_word"}))

    def test_两截判据合并时_任一判红即红(self):
        """_merge 的语义：任一分项 FAIL → 整条规则 FAIL，不许被 PASS 盖掉。"""
        merged = vc._merge([
            vc._res(vc.PASS, "a"),
            vc._res(vc.FAIL, "b"),
        ], total=1)
        self.assertEqual(merged["status"], vc.FAIL)


class TestBannedTypes(unittest.TestCase):
    """禁发类型：唯一判据是先归一再查 TARGET_PCT。"""

    def test_历史回顾是禁发(self):
        self.assertTrue(content_mix.is_banned("历史回顾"))

    def test_别名键不得被误判为禁发(self):
        """`榜单`/`战术解析` 在 TARGET_PCT 里是 0，但它们是别名，归一后不是禁发。"""
        self.assertFalse(content_mix.is_banned("榜单"))
        self.assertFalse(content_mix.is_banned("战术解析"))

    def test_正常类型不禁发(self):
        for ct in ("热点球评", "数据对比", "排行榜", "战术榜单"):
            self.assertFalse(content_mix.is_banned(ct), ct)

    def test_未知类型与空值不禁发(self):
        """未知类型不能因为查不到目标就当禁发——那是「拿缺失当事实」。"""
        for ct in ("", None, "某种没登记的新类型"):
            self.assertFalse(content_mix.is_banned(ct), ct)

    def test_与目标表同源(self):
        """禁发只有一处定义：TARGET_PCT。不许再维护第二份清单。"""
        for ct in content_mix.TARGET_PCT:
            banned = content_mix.TARGET_PCT[content_mix.normalize(ct)] == 0
            self.assertEqual(content_mix.is_banned(ct), banned, ct)


class TestMaterialLayerNoHardcodedTeams(unittest.TestCase):
    """素材层换判据：问「要不要比赛数据」，不再问「球队在不在 15 支词表里」。

    旧判据只有**一个方向**真错：表内球队的分析稿被过度丢弃（不需要比赛数据也丢）。
    表外球队带比赛细节的稿子旧实现本来就拦得住，这里保留成回归保护，
    别把「换口径」写成「修了两个 bug」。
    """

    def _run(self, topics, match_data=None):
        import orchestrator
        return orchestrator._check_topic_material_sufficiency(topics, match_data or {})

    def test_不需要比赛数据的分析稿不再因球队在词表里被丢(self):
        topics = [{"title": "拜仁的中场结构该怎么改", "angle": "",
                   "content_type": "战术榜单"}]
        self.assertEqual(len(self._run(topics)), 1,
                         "分析稿不含比赛细节，不该因为「今天没拜仁的完赛数据」被丢")

    def test_表外球队要比赛细节也照样丢(self):
        """回归保护：换口径不得把「要比赛数据但没数据」的稿子放进来。"""
        topics = [{"title": "那不勒斯中场被压住，进球全靠反击", "angle": "",
                   "content_type": "战术榜单"}]
        self.assertEqual(len(self._run(topics)), 0)

    def test_谁也认不出的猜测稿仍然被丢(self):
        topics = [{"title": "某人的点球绝杀帽子戏法", "angle": "",
                   "content_type": "战术榜单"}]
        self.assertEqual(len(self._run(topics)), 0)

    def test_转会稿不需要比赛数据(self):
        topics = [{"title": "那不勒斯有意签下这名中场", "angle": "",
                   "content_type": "转会资讯"}]
        self.assertEqual(len(self._run(topics)), 1)

    def test_有当天完赛数据就放行(self):
        md = {"data_source": "zhibo8",
              "all_fixtures": [{"home_team": "那不勒斯", "away_team": "拉齐奥",
                                "status": "FT", "home_score": 2, "away_score": 1}]}
        topics = [{"title": "那不勒斯2-1拉齐奥，进球来自定位球", "angle": "",
                   "content_type": "热点球评"}]
        self.assertEqual(len(self._run(topics, md)), 1)


class TestPublishByCarry(unittest.TestCase):
    """`publish_by` 必须真的从选题流到成稿，否则 TT-03 第一截长期空转。"""

    def _src(self):
        return (PROJECT_ROOT / "orchestrator.py").read_text(encoding="utf-8")

    def test_主稿汇总时拷贝_publish_by(self):
        tree = ast.parse(self._src())
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.For):
                continue
            it = node.iter
            if not (isinstance(it, ast.Tuple) and it.elts):
                continue
            consts = [e.value for e in it.elts
                      if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if "_hot_event_key" in consts:
                self.assertIn("publish_by", consts,
                              "拷贝白名单里丢了 publish_by，TT-03 第一截又变空转")
                found = True
        self.assertTrue(found, "没找到成稿字段拷贝循环，接线方式已变，请复核本测试")

    def test_确定性节点装配时带入_publish_by(self):
        tree = ast.parse(self._src())
        keys = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
        self.assertIn("publish_by", keys,
                      "确定性节点成稿装配的字典里没有 publish_by")


class TestUnjudgedCountNeverExceedsArticleCount(unittest.TestCase):
    """计数口径只能是「篇」，不能是「判据次」。

    2026-11-09 实测踩到：TT-03 挂 `freshness + time_word` 两个判据，
    2 篇成稿摊平出 4 条未判定结果，报告印成「**4/2 篇**未判定」。
    自相矛盾的数字比没有数字更糟——它会让读报告的人直接不信任这一项。
    """

    def _vc(self):
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(root / "scripts"))
        import verify_compliance
        return verify_compliance

    def test_同一篇多判据先合并不计数(self):
        vc = self._vc()
        one = vc._res(vc.UNJ, "正文不可读，时间词未判定")
        two = vc._res(vc.UNJ, "无 publish_by / source_age_hours 证据")
        merged = vc._combine_same_article([one, two])
        self.assertEqual(merged["status"], vc.UNJ)
        self.assertNotRegex(merged["detail"], r"\d+/\d+ 篇",
                            "同一篇的合并结果里不应出现「N/M 篇」这种跨篇计数")

    def test_两个判据两篇仍是二分之二(self):
        vc = self._vc()
        per_article = [[vc._res(vc.UNJ, "a"), vc._res(vc.UNJ, "b")] for _ in range(2)]
        results = [vc._combine_same_article(rs) for rs in per_article]
        merged = vc._merge(results, 2)
        self.assertIn("2/2 篇未判定", merged["detail"])

    def test_负向_摊平不聚合就会印出四分之二(self):
        """守住反面：不聚合时计数会超过篇数。这条防的是「改回去」。"""
        vc = self._vc()
        flat = [vc._res(vc.UNJ, "a"), vc._res(vc.UNJ, "b")] * 2
        merged = vc._merge(flat, 2)
        self.assertIn("4/2", merged["detail"],
                      "本用例锁的是旧行为；若 _merge 已自带聚合，请改为断言 2/2")


if __name__ == "__main__":
    unittest.main()
