"""0 篇发布原因归类单测（scripts/publish_zero_reason.py）。

背景（2026-11-11）：11-10 evening 与 11-11 morning 连续两批 0 篇，告警只写
「失败步骤：发布到头条号」。真实原因是风险闸门把 2 篇全转人工确认，而降级文案
一律写「被合规审核全部拦下」——排查方向被带偏。这里把「0 篇的三种成因」钉死：
归因错 = 改错对象，比不给原因更坏。
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
        zero_publish_reason="风险闸门：本批 2 篇全部转人工确认，发布集合 0 篇"))
    assert explicit is True
    assert text.startswith("风险闸门")


def test_pending_review_without_articles_is_risk_gate():
    """只有 pending_review、没有 compliance_dropped → 必须归到风险闸门。"""
    text, explicit = pz.reason_of(_meta(pending_review=[
        {"id": "a", "title": "x", "reasons": ["伤病(命中：伤停)"]},
        {"id": "b", "title": "y", "reasons": ["合同金额(命中：合同)"]},
    ]))
    assert explicit is True
    assert "风险闸门" in text and "2 篇" in text and "合规" not in text


def test_compliance_dropped_without_pending_is_compliance():
    text, explicit = pz.reason_of(_meta(compliance_dropped=[
        {"index": 1, "title": "x", "blocked_by": ["TT-07"]}]))
    assert explicit is True
    assert text.startswith("合规审核")


def test_both_present_is_reported_separately():
    """两类都有时必须分开报数，不能笼统说「全部被合规拦下」。"""
    text, _ = pz.reason_of(_meta(pending_review=[{"id": "a"}],
                                 compliance_dropped=[{"index": 2}]))
    assert "风险 1" in text and "合规 1" in text


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
    meta_path.write_text(json.dumps(_meta(zero_publish_reason="风险闸门：2 篇全部转人工确认")),
                         encoding="utf-8")
    rc = pz.main(["publish_zero_reason.py", str(meta_path)])
    assert rc == 0
    assert (tmp_path / "wb_fail_reason.txt").read_text(encoding="utf-8").startswith("风险闸门")
