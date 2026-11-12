"""0 篇发布原因归类单测（scripts/publish_zero_reason.py）。

背景（2026-11-11）：11-10 evening 与 11-11 morning 连续两批 0 篇，告警只写
「失败步骤：发布到头条号」。真实原因是风险闸门把 2 篇全转人工确认，而降级文案
一律写「被合规审核全部拦下」——排查方向被带偏。归因错 = 改错对象，比不给原因更坏。

2026-11-11 风险闸门整体移除后，现役成因只剩「上游空转 / 合规剔除」两种；
``pending_review`` 只作为历史字段保留可读性（旧批次回放）。
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location(
    "publish_zero_reason", ROOT / "scripts" / "publish_zero_reason.py")
pz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pz)


def _meta(**kw):
    base = {"date": "2026-11-11", "articles": []}
    base.update(kw)
    return base


def test_explicit_reason_wins():
    """orchestrator 写下的 zero_publish_reason 是唯一权威，脚本只读不重判。"""
    text, explicit = pz.reason_of(_meta(
        zero_publish_reason="合规审核：本批 2 篇全部被合规规则剔除，发布集合 0 篇"))
    assert explicit is True
    assert text.startswith("合规审核")


def test_legacy_pending_review_is_readable_but_marked_removed():
    """旧 metadata 的 pending_review 要能读懂，但必须标「已移除的历史闸门」。

    11-10 / 11-11 那两批的 metadata 里就是这个字段。回放时必须能解释
    「当时发生了什么」，同时不能让读者以为现在还有这道闸门。
    """
    text, explicit = pz.reason_of(_meta(pending_review=[
        {"id": "a", "title": "x", "reasons": ["伤病(命中：伤停)"]},
        {"id": "b", "title": "y", "reasons": ["合同金额(命中：合同)"]},
    ]))
    assert explicit is True
    assert "2 篇" in text and "已移除" in text


def test_compliance_dropped_without_pending_is_compliance():
    text, explicit = pz.reason_of(_meta(compliance_dropped=[
        {"index": 1, "title": "x", "blocked_by": ["TT-07"]}]))
    assert explicit is True
    assert text.startswith("合规审核")


def test_compliance_dropped_wins_over_legacy_field():
    """合规剔除有明细时以它为准；不能因为旧字段还在就说成「风险闸门」。"""
    text, _ = pz.reason_of(_meta(pending_review=[{"id": "a"}],
                                 compliance_dropped=[{"index": 2}]))
    assert text.startswith("合规审核")


def test_no_evidence_falls_back_to_upstream_empty():
    """没有任何证据时按「上游空转」处理，且标记为**非明确**（退出码 2）。"""
    text, explicit = pz.reason_of(_meta())
    assert explicit is False
    assert "上游空转" in text


def test_articles_present_is_not_zero_publish(tmp_path, monkeypatch):
    """有稿时不该走到这个脚本的归因分支（articles 非空）→ 不编成因。"""
    text, explicit = pz.reason_of(_meta(articles=[{"title": "正常稿"}]))
    assert explicit is False


def test_main_writes_reason_file(tmp_path, monkeypatch):
    """必须落盘到失败原因文件，否则失败通知还是只有「详见日志」。"""
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    meta_path = tmp_path / "metadata.json"
    meta_path.write_text(json.dumps(_meta(zero_publish_reason="合规审核：2 篇全部被合规规则剔除")),
                         encoding="utf-8")
    rc = pz.main(["publish_zero_reason.py", str(meta_path)])
    assert rc == 0
    assert (tmp_path / "wb_fail_reason.txt").read_text(encoding="utf-8").startswith("合规审核")


def test_dropped_brief_surfaces_rule_ids_and_titles():
    """合规全丢时，告警正文必须带「命中哪条规则 + 哪几篇」。

    11-11 晚间手机上只看到「本批 2 篇全部被合规规则剔除」，规则 ID 与篇名
    全在 runner 的 metadata 里，人得先登 GitHub 翻 artifact 才知道该改什么。
    合规剔除是可取证判定，取证成本不该高于看一眼手机。
    """
    text, explicit = pz.reason_of(_meta(
        zero_publish_reason="合规审核：本批 2 篇全部被合规规则剔除，发布集合 0 篇",
        compliance_dropped=[
            {"title": "梅西C罗生涯造进球都超1100球，但一个靠传球一个靠射门制造差距",
             "blocked_by": ["TT-07", "TT-08"]},
            {"title": "C罗欧冠140球领跑，梅西差11球，这榜单老球迷怎么看",
             "blocked_by": ["TT-07", "TT-08"]},
        ]))
    assert explicit is True
    assert "TT-07" in text and "TT-08" in text
    assert "梅西C罗" in text


def test_dropped_brief_absent_when_no_drop_detail():
    """没有剔除明细就不要编：宁可不写，也不许补一个假规则号。"""
    text, _ = pz.reason_of(_meta(
        zero_publish_reason="合规审核：本批 2 篇全部被合规规则剔除，发布集合 0 篇"))
    assert "TT-" not in text
