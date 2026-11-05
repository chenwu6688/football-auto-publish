"""「本批无可发布稿件」三态（2026-11-05 立）。

## 事故

18:00 批次 3 篇成稿全部没发出去（2 篇崩在标题重拟，1 篇是预测稿被跳过）。
publisher 用 `sys.exit(1)` 收尾，于是：

  · 上层 job 判 failure，与「浏览器崩了」长得一样；
  · 回写步骤照样把这批写成「已完成」→ 人工补发被幂等跳过 → 当批永久丢失。

根子是**四种终态共用一个 exit 1**。本文件钉住拆分后的契约：

| 退出码 | 含义 | 上层该做什么 |
|---|---|---|
| 0 | 至少一篇发出去了 | 正常 |
| 2 | 有稿但全被规则跳过 | 标完成（重跑没意义）+ 告警 |
| 3 | 本批一篇成稿都没有 | 不标完成，让兜底 cron 重跑 |
| 1 | 真失败 | 告警 + 人工 |

另外钉住两条防重复发文的保险丝（都在同一条降级路径上）：
  · 批次筛选 0 命中时**只**降级发布「无批次归属」的稿，不去发别批的稿；
  · 台账里已 published 的标题一律不再发第二次。
"""

import json

import pytest

import publisher as P


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把 output / audit 都指到临时目录：台账隔离是硬纪律。"""
    out = tmp_path / "output"
    audit = tmp_path / "audit"
    out.mkdir()
    audit.mkdir()
    monkeypatch.setattr(P, "OUTPUT_BASE", out)
    monkeypatch.setattr(P, "AUDIT_DIR", audit)
    return out, audit


def _mk(root, date, name, body):
    d = root / date
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


def _art(title, batch="晚间", pred="false", basis=None):
    extra = ""
    if basis is not None:
        extra = (f"prediction_basis: {basis[0]}\n"
                 f"prediction_basis_items: {basis[1]}\n")
    return (f'---\ntitle: "{title}"\ndate: 2026-11-05\nbatch_name: {batch}\n'
            f'is_prediction: {pred}\n{extra}---\n\n# {title}\n\n正文。')


class TestExitCodes:
    def test_退出码互不相等(self):
        assert len({P.EXIT_OK, P.EXIT_FAIL, P.EXIT_NOTHING_SKIPPED,
                    P.EXIT_NOTHING_EMPTY}) == 4

    def test_all_skipped_映射到2(self):
        e = P.NoPublishableBatch("all_skipped", "x")
        assert e.exit_code == P.EXIT_NOTHING_SKIPPED

    def test_empty_映射到3(self):
        e = P.NoPublishableBatch("empty", "x")
        assert e.exit_code == P.EXIT_NOTHING_EMPTY


class TestEmpty:
    def test_目录不存在判empty(self, env):
        with pytest.raises(P.NoPublishableBatch) as ei:
            P.load_articles("2026-11-05", batch_filter="evening")
        assert ei.value.code == "empty"

    def test_只有别批的稿判empty不降级重发(self, env):
        """这条以前会**降级发布全部文章**——晚上 0 篇时去把早上的两篇再发一遍。"""
        out, _ = env
        _mk(out, "2026-11-05", "article-1-a.md", _art("晨间稿A", batch="晨读"))
        with pytest.raises(P.NoPublishableBatch) as ei:
            P.load_articles("2026-11-05", batch_filter="evening")
        assert ei.value.code == "empty"
        assert "0 个" in ei.value.detail

    def test_无批次归属的稿仍降级发布(self, env):
        """保留兜底意图：batch_name 缺失的应急/补录稿**可能**是本批的。"""
        out, _ = env
        _mk(out, "2026-11-05", "article-1-a.md", _art("临时补录稿", batch=""))
        arts = P.load_articles("2026-11-05", batch_filter="evening")
        assert [a["title"] for a in arts] == ["临时补录稿"]


class TestAllSkipped:
    def test_只有观点预测稿判all_skipped(self, env):
        out, _ = env
        _mk(out, "2026-11-05", "article-1-a.md",
            _art("勒沃库森遇马赛", pred="true"))
        with pytest.raises(P.NoPublishableBatch) as ei:
            P.load_articles("2026-11-05", batch_filter="evening")
        assert ei.value.code == "all_skipped"
        assert ei.value.exit_code == 2
        # 原因必须能说清，不是「预测稿」三个字
        assert "依据不足" in ei.value.detail

    def test_已发过的稿不再发第二次(self, env):
        out, audit = env
        _mk(out, "2026-11-05", "article-1-a.md", _art("科莫9比1血洗朗斯"))
        (audit / "publish_ledger_2026-11-05_evening.json").write_text(json.dumps(
            {"records": [{"status": "published", "title": "科莫9比1血洗朗斯",
                          "platform_id": "770"}],
             "batch_filter": "evening"}, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(P.NoPublishableBatch) as ei:
            P.load_articles("2026-11-05", batch_filter="evening")
        assert ei.value.code == "all_skipped"
        assert ei.value.skipped[0]["status"] == "skipped_already_published"


class TestPredictionTiersThroughLoader:
    def test_有据前瞻会被放行(self, env):
        out, _ = env
        _mk(out, "2026-11-05", "article-1-a.md",
            _art("勒沃库森5轮4胜对马赛3轮1胜", pred="true",
                 basis=("2", "积分排名、近期战绩")))
        arts = P.load_articles("2026-11-05", batch_filter="evening")
        assert len(arts) == 1
        assert arts[0]["meta"]["_prediction_tier"] == "sourced_forward"

    def test_第二篇有据前瞻被配额挡(self, env):
        out, _ = env
        _mk(out, "2026-11-05", "article-1-a.md",
            _art("甲队5轮4胜对乙队3轮1胜", pred="true", basis=("2", "积分排名、近期战绩")))
        _mk(out, "2026-11-05", "article-2-b.md",
            _art("丙队5轮4胜对丁队3轮1胜", pred="true", basis=("2", "积分排名、近期战绩")))
        skip = []
        arts = P.load_articles("2026-11-05", batch_filter="evening", skip_report=skip)
        assert len(arts) == 1
        assert "配额已满" in skip[0]["reason"]


class TestNoPublishableLedger:
    def test_无稿台账写出原因(self, env):
        out, audit = env
        e = P.NoPublishableBatch("all_skipped", "全被跳过",
                                 [{"status": "skipped_prediction",
                                   "title": "x", "reason": "依据不足"}])
        P.write_publish_ledger("2026-11-05", e.skipped,
                               batch_filter="evening",
                               no_publishable={"code": e.code, "detail": e.detail})
        # 批次文件才有 counts.no_publishable（返回值是当天聚合，字段结构不同）
        d = json.loads((audit / "publish_ledger_2026-11-05_evening.json")
                       .read_text(encoding="utf-8"))
        assert d["counts"]["no_publishable"] == "all_skipped"
        assert d["no_publishable"]["detail"] == "全被跳过"
        # 当天聚合也要带上，否则看板只能看到「少了一批」，看不出为什么
        day = json.loads((audit / "publish_ledger_2026-11-05.json").read_text(encoding="utf-8"))
        assert day["no_publishable"]["evening"]["code"] == "all_skipped"
