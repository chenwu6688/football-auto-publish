"""A/B 探针的判据单测。

为什么这个脚本也值得测：它给出的结论直接决定「要不要引一个新依赖」。
判据函数自己有 bug 的话，我们就会拿一个有问题的尺子去量，然后按量出来的
结果改生产代码——这比不做实验更糟。

所以这里只测两件最容易错、且错了最致命的事：
  1. ``fisher_p`` 与教科书值对得上（茶与牛奶实验 p=0.4857 双侧）；
  2. ``verdict`` 的四条分支各自能命中，且**两组都为 0 时不许判成「指纹有效」**
     ——那是最容易犯的错：把「这个环境根本没复现问题」读成「换了库就好了」。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ab_fingerprint_probe as ab  # noqa: E402


class TestFisher:
    def test_tea_and_milk_textbook_value(self):
        """茶与牛奶实验：[[3,1],[1,3]]，双侧 p = 34/70 = 0.4857。

        这个值是所有统计教材的标准答案，用来验尺子本身对不对。
        """
        p = ab.fisher_p(3, 4, 1, 4)
        assert p == pytest.approx(34 / 70, abs=1e-9)

    def test_strong_difference_is_significant(self):
        p = ab.fisher_p(20, 50, 0, 50)
        assert p < 0.001

    def test_same_rate_is_not_significant(self):
        p = ab.fisher_p(10, 50, 10, 50)
        assert p == pytest.approx(1.0, abs=1e-6)

    def test_zero_sample_does_not_crash(self):
        assert ab.fisher_p(0, 0, 0, 0) == 1.0
        assert ab.fisher_p(0, 0, 5, 10) == 1.0

    def test_p_within_range(self):
        for a_s, a_n, b_s, b_n in [(1, 10, 2, 10), (5, 10, 5, 10),
                                   (10, 10, 0, 10), (0, 10, 10, 10)]:
            assert 0.0 <= ab.fisher_p(a_s, a_n, b_s, b_n) <= 1.0


def _grp(n, ch):
    return {"n": n, "challenge": ch, "challenge_rate": ch / n if n else 0.0}


class TestVerdict:
    def test_fingerprint_effective(self):
        """基线高、curl_cffi 低且显著 → 该引 curl_cffi。"""
        v, why, rc = ab.verdict(_grp(50, 19), {"curl_cffi": _grp(50, 2)})
        assert v == "最优通道 curl_cffi"
        assert rc == 0
        assert "p=" in why

    def test_urllib_win_is_flagged_as_zero_cost(self):
        """最优通道是 urllib 时必须说明「零成本」——那是比装依赖更该先试的路。"""
        v, why, rc = ab.verdict(_grp(50, 25), {"urllib": _grp(50, 1)})
        assert v == "最优通道 urllib"
        assert "零成本" in why
        assert rc == 0

    def test_not_fingerprint_when_both_high(self):
        """两组都高 → 换库无用，别浪费时间引依赖。"""
        v, why, rc = ab.verdict(_grp(50, 20), {"curl_cffi": _grp(50, 18)})
        assert v == "不是请求栈问题"
        assert rc == 0

    def test_not_reproducible_must_not_be_read_as_fixed(self):
        """两组都 0 —— 最容易误读成「修好了」。必须判未判定并说明。"""
        v, why, rc = ab.verdict(_grp(50, 0), {"curl_cffi": _grp(50, 0)})
        assert v == "该环境不复现"
        assert rc == 2
        assert "不能据此宣称" in why

    def test_missing_b_group(self):
        v, _why, rc = ab.verdict(_grp(50, 10), None)
        assert v == "未判定" and rc == 2

    def test_empty_others_dict(self):
        v, _why, rc = ab.verdict(_grp(50, 10), {})
        assert v == "未判定" and rc == 2

    def test_small_delta_is_inconclusive(self):
        """差值够大但样本小 → p 不显著时不许下结论。"""
        v, _why, rc = ab.verdict(_grp(8, 2), {"curl_cffi": _grp(8, 0)})
        assert v in ("证据不足", "未判定")
        assert rc == 2

    def test_delta_large_but_not_significant(self):
        """差值达标、B 够低，但 p 不到显著线 → 证据不足，别改代码。"""
        # 10 次里 3 次 vs 0 次：差 30 个百分点，但 Fisher p≈0.21
        v, _why, rc = ab.verdict(_grp(10, 3), {"curl_cffi": _grp(10, 0)})
        assert v == "证据不足"
        assert rc == 2


class TestSummarize:
    def test_challenge_index_recorded_for_rate_attribution(self):
        """被挑战的序号必须留档：challenge 集中在后段 = 频率风控的特征。"""
        recs = [
            {"kind": "ok", "status": 200, "bytes": 100, "i": 0, "err": ""},
            {"kind": "challenge", "status": 200, "bytes": 200, "i": 1, "err": ""},
            {"kind": "challenge", "status": 567, "bytes": 0, "i": 2, "err": ""},
            {"kind": "ok", "status": 200, "bytes": 300, "i": 3, "err": ""},
        ]
        out = ab._summarize({"requests": recs}, "u", 4, "chrome131", aborted=False)
        g = out["groups"]["requests"]
        assert g["challenge"] == 2
        assert g["challenge_at_index"] == [1, 2]
        # 0 字节（被 567 挡掉、连 body 都没有）不进中位数：
        # 含 0 会算成 150，排除后是 200。混进去会把「被拦」读成「页面很小」。
        assert g["bytes_median"] == 200

    def test_empty_group_yields_undecided(self):
        out = ab._summarize({"requests": []}, "u", 0, "chrome131", aborted=False)
        assert out["verdict"] == "未判定"
        assert out["exit_code"] == 2


class TestNoProductionLedgerPollution:
    def test_probe_never_writes_diagnostics_ledger(self):
        """探针请求绝不能进生产采集台账——100 次探针会把采集成功率指标污染掉。

        用 AST 查真实调用（文本匹配会漏掉 `diag.record` 写成别名的情况）。
        """
        import ast
        tree = ast.parse((ROOT / "scripts" / "ab_fingerprint_probe.py").read_text(encoding="utf-8"))
        bad = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr in ("record", "flush", "reset")]
        assert bad == [], f"探针调用了台账写入函数：{[ast.unparse(b) for b in bad]}"

    def test_judgement_reuses_production_classifier(self):
        """不另立口径：必须复用 scraper_diagnostics 的判据函数。"""
        src = (ROOT / "scripts" / "ab_fingerprint_probe.py").read_text(encoding="utf-8")
        assert "diag.classify_status" in src
        assert "diag.looks_like_challenge" in src
