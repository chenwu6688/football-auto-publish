"""合规审核「落盘回放」模式（2026-11-09 深夜）。

## 它守的是什么

`daily-result.yml` 的合规步骤自 2026-11-06 加入起**从未绿过**（恒 rc=2）。
根因不是判据坏了，而是它去**现判**：成稿 `.md` 故意不入库
（`output/**/*.md` 在 .gitignore），每日 job 全新 checkout 后正文根本不存在，
于是每条规则都落成「正文不可读」。

「读不到证据判未判定」这条纪律没错，错的是**找错了证据**：出口审核
（`audit_article`）在生成 job 里已经拿正文逐篇判过，结论落在
`metadata.articles[*]._compliance` —— 而 `metadata.json` 是 `git add -f` 入库的。
同一批稿子、同一份判据实现，每天却重新判一遍并把结论覆盖成「不可读」。

本文件守四件事：

1. **只搬运、不重写**：回放出来的 status/detail 必须就是出口审核当时那条，
   不许在这里重新判一遍（否则又多一处「同一规矩写两处」）。
2. **缺结论 ≠ 通过**：metadata 里查不到该规则的结论 → 未判定，绝不退回去现判
   （现判在「读得到正文」的机器上会给出另一个答案，两台机器两种结论）。
3. **落盘产物不互相覆盖**：daily 写 `compliance_ledger_latest.json`，
   batch 写 `compliance_latest.json`，不能共用。
4. **workflow 不许改回去**：`daily-result.yml` 必须带 `--from-ledger`。
   这条是防「明天有人觉得回放是在放宽标准，又改回现判」——那会重新变成恒红。
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import verify_compliance as vc  # noqa: E402


def _item(verdicts=None, date="2026-11-09", with_field=True):
    """造一篇 metadata 记录。``with_field=False`` 模拟出口审核没跑到的篇。"""
    art = {"title": "那不勒斯客场 2-1 逆转"}
    if with_field:
        art["_compliance"] = {"verdicts": list(verdicts or [])}
    return {"date": date, "dir": ROOT / "output" / date, "art": art, "_body": None}


def _block_rule_ids():
    return [r["id"] for r in vc.load_rules()["rules"]
            if str(r.get("tier")) in ("block", "rewrite")]


def _all_pass_verdicts():
    return [{"id": rid, "status": vc.PASS, "detail": "出口审核已判定", "hits": []}
            for rid in _block_rule_ids()]


class TestRecordedVerdict(unittest.TestCase):
    """`recorded_verdict` 是纯搬运函数，行为必须一眼可验。"""

    def test_returns_recorded_status_verbatim(self):
        it = _item([{"id": "TT-07", "status": vc.FAIL,
                     "detail": "正文含诱导话术", "hits": ["点个关注"]}])
        res = vc.recorded_verdict(it, "TT-07")
        self.assertEqual(res["status"], vc.FAIL)
        self.assertIn("正文含诱导话术", res["detail"])
        self.assertEqual(res["hits"], ["点个关注"])

    def test_missing_rule_returns_none(self):
        it = _item([{"id": "TT-07", "status": vc.PASS, "detail": "ok"}])
        self.assertIsNone(vc.recorded_verdict(it, "TT-99"))

    def test_no_compliance_field_returns_none(self):
        self.assertIsNone(vc.recorded_verdict(_item(with_field=False), "TT-07"))

    def test_compliance_not_dict_returns_none(self):
        it = _item()
        it["art"]["_compliance"] = "坏的"
        self.assertIsNone(vc.recorded_verdict(it, "TT-07"))

    def test_illegal_status_never_becomes_pass(self):
        """落了不认识的 status 比没落更糟——静默当通过就等于没判。"""
        it = _item([{"id": "TT-07", "status": "skipped", "detail": "???"}])
        res = vc.recorded_verdict(it, "TT-07")
        self.assertEqual(res["status"], vc.UNJ)
        self.assertIn("非法", res["detail"])

    def test_empty_detail_gets_placeholder(self):
        it = _item([{"id": "TT-07", "status": vc.PASS, "detail": ""}])
        self.assertTrue(vc.recorded_verdict(it, "TT-07")["detail"].strip())


class TestVerifyFromLedger(unittest.TestCase):
    """回放模式下 `verify()` 的数据来源必须是 metadata，不是正文。"""

    def _verify(self, items, days=1):
        with mock.patch.object(vc, "iter_items", return_value=items):
            return vc.verify(days, from_ledger=True)

    def test_reports_source_and_coverage(self):
        rep = self._verify([_item(_all_pass_verdicts())])
        self.assertEqual(rep["source"], "ledger")
        self.assertEqual(rep["articles"], 1)
        self.assertEqual(rep["ledger_articles"], 1)

    def test_full_pass_ledger_has_no_blocking(self):
        rep = self._verify([_item(_all_pass_verdicts())])
        self.assertEqual(rep["blocking"], [])
        self.assertEqual(vc.exit_code(rep), 0)

    def test_recorded_fail_becomes_blocking(self):
        """出口审核当时判红了 → 回放必须也红。回放不是「一律放行」。"""
        v = _all_pass_verdicts()
        v[0] = {"id": v[0]["id"], "status": vc.FAIL, "detail": "命中", "hits": []}
        rep = self._verify([_item(v)])
        self.assertEqual(rep["blocking"], [v[0]["id"]])
        self.assertEqual(vc.exit_code(rep), 1)

    def test_missing_rule_verdict_is_not_judged_not_pass(self):
        """规则表新增一条，旧篇里没有它的结论 → 未判定，不是通过。"""
        v = [x for x in _all_pass_verdicts() if x["id"] != _block_rule_ids()[0]]
        rep = self._verify([_item(v)])
        entry = next(r for r in rep["rules"] if r["id"] == _block_rule_ids()[0])
        self.assertEqual(entry["status"], vc.UNJ)
        self.assertIn("无本规则的出口审核结论", entry["detail"])

    def test_article_without_compliance_is_unjudged(self):
        rep = self._verify([_item(with_field=False)])
        self.assertEqual(rep["ledger_articles"], 0)
        self.assertTrue(all(r["status"] == vc.UNJ for r in rep["rules"]
                            if r["tier"] in ("block", "rewrite")))

    def test_replay_does_not_touch_body(self):
        """回放不许读正文：正文不在 runner 上正是要修的那个场景。

        用「正文可读且含违规」的假 item 反过来验——若回放偷读正文，
        TT-07 会变红；正确行为是照落盘的 PASS 走。
        """
        it = _item(_all_pass_verdicts())
        it["_body"] = "点个关注，后续我替你盯着。"
        rep = self._verify([it])
        tt07 = next(r for r in rep["rules"] if r["id"] == "TT-07")
        self.assertEqual(tt07["status"], vc.PASS)


class TestExitCode(unittest.TestCase):
    """退出码三态不可合并（11-06 立），回放模式沿用同一套。"""

    def _rep(self, n_articles=1, statuses=None):
        rules = vc.load_rules()["rules"]
        rows = [{"id": r["id"], "tier": str(r.get("tier")), "status": vc.PASS,
                 "detail": "", "hits": []}
                for r in rules if str(r.get("tier")) in ("block", "rewrite")]
        for row, st in zip(rows, statuses or []):
            row["status"] = st
        return {"articles": n_articles, "rules": rows, "blocking": [],
                "risk_score": 0}

    def test_all_judged_is_zero(self):
        self.assertEqual(vc.exit_code(self._rep()), 0)

    def test_half_unjudged_is_two(self):
        ids = _block_rule_ids()
        self.assertEqual(vc.exit_code(self._rep(statuses=[vc.UNJ] * len(ids))), 2)

    def test_exactly_half_judged_still_passes_threshold(self):
        """判过一半就不算「过半未判定」——边界写死在 `*2 <` 上，别手滑成 `<=`。"""
        n = len(_block_rule_ids())
        st = [vc.UNJ] * (n // 2) + [vc.PASS] * (n - n // 2)
        self.assertGreaterEqual((n - n // 2) * 2, n)
        self.assertEqual(vc.exit_code(self._rep(statuses=st)), 0)

    def test_no_articles_is_two_not_zero(self):
        self.assertEqual(vc.exit_code(self._rep(n_articles=0)), 2)

    def test_blocking_wins_over_everything(self):
        rep = self._rep()
        rep["blocking"] = ["TT-01"]
        self.assertEqual(vc.exit_code(rep, all_dropped=True), 1)

    def test_all_dropped_is_three(self):
        self.assertEqual(vc.exit_code(self._rep(), all_dropped=True), 3)

    def test_fail_on_block_only_suppresses_two(self):
        self.assertEqual(vc.exit_code(self._rep(n_articles=0), fail_on_block_only=True), 0)


class TestOutputPaths(unittest.TestCase):
    def test_ledger_mode_has_its_own_report_file(self):
        """daily 与 batch 共用一份 latest，就会后跑的覆盖先跑的，谁也解释不了自己。"""
        self.assertNotEqual(vc.LEDGER_OUT_PATH, vc.OUT_PATH)
        self.assertEqual(vc.LEDGER_OUT_PATH.name, "compliance_ledger_latest.json")
        self.assertEqual(vc.OUT_PATH.name, "compliance_latest.json")


class TestWindowOffset(unittest.TestCase):
    """每日层必须看**昨天**，不能看今天。

    实测时序：daily `00:10 UTC`（08:10 CST）跑，早批 `00:05 UTC`（08:05 CST）
    才写完当天 metadata —— 只差 5 分钟。早批一延迟，08:10 时当天目录还不存在，
    `articles=0` 直接 rc=2。那不是「合规不通过」，是看错了日期。

    这个类守的就是「窗口起点」这件事本身：offset=0 从今天起算（本机/批次内），
    offset=1 从昨天起算（每日层）。
    """

    def setUp(self):
        import tempfile
        from datetime import date
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._orig_root = vc.ROOT
        vc.ROOT = self._root
        self._today = date.today()

    def tearDown(self):
        vc.ROOT = self._orig_root
        self._tmp.cleanup()

    def _mk(self, days_ago, n=2):
        from datetime import timedelta
        ds = (self._today - timedelta(days=days_ago)).isoformat()
        d = self._root / "output" / ds
        d.mkdir(parents=True, exist_ok=True)
        arts = [{"title": f"第{i}篇", "_compliance": {"verdicts": []}} for i in range(n)]
        (d / "metadata.json").write_text(json.dumps({"articles": arts}), encoding="utf-8")
        return ds

    def test_offset_zero_starts_today(self):
        today = self._mk(0, 1)
        self._mk(1, 1)
        self.assertEqual([i["date"] for i in vc.iter_items(1)], [today])

    def test_offset_one_starts_yesterday_and_skips_today(self):
        self._mk(0, 3)          # 今天有 3 篇，但每日层不该看它
        y = self._mk(1, 2)
        got = [i["date"] for i in vc.iter_items(1, offset=1)]
        self.assertEqual(got, [y, y])   # 昨天的 2 篇，一篇不多一篇不少

    def test_offset_two_days_window(self):
        y, b = self._mk(1, 2), self._mk(2, 1)
        got = sorted({i["date"] for i in vc.iter_items(2, offset=1)})
        self.assertEqual(got, sorted([y, b]))

    def test_empty_today_does_not_pollute_window(self):
        """今天没有 metadata（早批还没跑）时，offset=1 仍要拿到昨天。"""
        y = self._mk(1, 2)
        self.assertEqual([i["date"] for i in vc.iter_items(2, offset=1)], [y] * 2)
        self.assertTrue(all(i["date"] == y for i in vc.iter_items(2, offset=1)))

    def test_verify_reports_dates_and_offset(self):
        y, b = self._mk(1, 2), self._mk(2, 1)
        rep = vc.verify(2, from_ledger=True, offset=1)
        self.assertEqual(rep["window_offset"], 1)
        self.assertEqual(rep["dates"], {y: 2, b: 1})
        self.assertEqual(rep["articles"], 3)

    def test_today_only_window_is_empty_before_early_batch(self):
        """复现事故形态：今天还没产物 → 空窗口 → rc=2。这正是要避开的那 5 分钟。"""
        self._mk(1, 2)
        rep = vc.verify(1, from_ledger=True, offset=0)
        self.assertEqual(rep["articles"], 0)
        self.assertEqual(vc.exit_code(rep), 2)


class TestWorkflowWiring(unittest.TestCase):
    """防回退：每日层的合规步骤必须走落盘回放。"""

    def _steps(self):
        import yaml
        wf = yaml.safe_load((ROOT / ".github" / "workflows" / "daily-result.yml")
                            .read_text(encoding="utf-8"))
        return wf["jobs"]["verify"]["steps"]

    def _compliance_step(self):
        for s in self._steps():
            if "verify_compliance.py" in str(s.get("run") or ""):
                return s
        return None

    @staticmethod
    def _argv(run: str) -> str:
        """只取**命令行**，去掉注释行。

        直接对整段 run 做文本匹配会被注释里的说明误伤——这正是项目里
        「判据短串误报」那条老毛病（`"eo-"` 命中 `video-player2.js`），
        我自己的测试不能重犯。注释里明写「不加 --fail-on-block-only」
        是为了给后来人看，不该被当成违规证据。
        """
        return "\n".join(ln for ln in str(run).splitlines()
                         if not ln.lstrip().startswith("#"))

    def test_daily_uses_from_ledger(self):
        step = self._compliance_step()
        self.assertIsNotNone(step, "daily-result.yml 里找不到合规步骤")
        self.assertIn("--from-ledger", self._argv(step["run"]),
                      "每日层必须回放出口审核结论，否则成稿 md 不入库会让它恒 rc=2")

    def test_daily_does_not_relax_with_fail_on_block_only(self):
        step = self._compliance_step()
        self.assertNotIn("--fail-on-block-only", self._argv(step["run"]),
                         "每日层不许放行「未判定」——那是发布前那一档才有的宽容")

    def test_daily_looks_at_yesterday_not_today(self):
        """与早批抢 5 分钟窗口，是这一步「看似修好又忽然红」的唯一来源。"""
        argv = self._argv(self._compliance_step()["run"])
        self.assertIn("--offset 1", argv,
                      "每日层必须 --offset 1：daily 08:10 跑，早批 08:05 才写完当天 metadata")

    def test_daily_window_covers_more_than_one_day(self):
        """单日窗口会把「昨天恰好空批」变成假红；与其它窗口式门禁口径保持一致。"""
        argv = self._argv(self._compliance_step()["run"])
        self.assertIn("--days 2", argv)

    def test_summary_table_lists_ledger_report(self):
        joined = "\n".join(str(s.get("run") or "") for s in self._steps())
        self.assertIn("compliance_ledger", joined)

    def test_mutual_exclusion_with_drop_violations(self):
        """`--from-ledger` 只回放不移动文件，与 `--drop-violations` 语义冲突。"""
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with mock.patch.object(sys, "argv",
                               ["verify_compliance.py", "--from-ledger",
                                "--drop-violations", "--days", "1"]), \
                redirect_stderr(buf):
            rc = vc.main()
        self.assertEqual(rc, 2)
        self.assertIn("不能同时用", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
