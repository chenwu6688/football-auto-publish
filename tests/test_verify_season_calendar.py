"""赛季节点日历验收脚本的单测（2026-10-26）。

验收脚本最容易犯的错是「永远通过」——检查写空、断言写反、或者只对真实日历
做一次肉眼确认。所以这里每条硬项都配一组**负向输入**：故意弄坏，必须报红。
"""

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import deterministic_supply as ds  # noqa: E402
import verify_season_calendar as v  # noqa: E402


def _nodes():
    return [dict(n) for n in ds.load_calendar()["nodes"]]


def test_real_calendar_passes_all_hard_items():
    rep = v.verify()
    assert rep["ok"], f"真实日历应有未通过硬项：{rep['hard_failed']}"
    assert len(rep["checks"]) == 4


def test_structure_rejects_bad_awareness_and_column():
    ns = _nodes()
    ns[0]["awareness"] = "D"
    ns[1]["column"] = "八卦"
    r = v.check_structure({"nodes": ns})
    assert not r["ok"]
    txt = "\n".join(r["detail"])
    assert "awareness" in txt and "column" in txt


def test_structure_rejects_duplicate_id_and_bad_date():
    ns = _nodes()
    ns[1]["id"] = ns[0]["id"]
    ns[2]["date"] = "2026-13-45"
    r = v.check_structure({"nodes": ns})
    assert not r["ok"]
    txt = "\n".join(r["detail"])
    assert "id 重复" in txt and "无法解析" in txt


def test_structure_rejects_forward_looking_template():
    ns = _nodes()
    ns[0]["template"] = "冬窗前瞻：谁会动手"
    r = v.check_structure({"nodes": ns})
    assert not r["ok"]
    assert "前瞻" in "\n".join(r["detail"])


def test_facts_without_source_is_rejected():
    cal = {"nodes": [{"id": "x", "type": "赛季节点", "label": "L", "date": "2026-12-01",
                      "column": "人物故事", "awareness": "A", "template": "t",
                      "facts": ["某球员 99 球"]}]}
    r = v.check_facts_traceable(cal)
    assert not r["ok"]
    assert "fact_source_url" in "\n".join(r["detail"])


def test_facts_with_source_passes():
    cal = {"nodes": [{"id": "x", "type": "赛季节点", "label": "L", "date": "2026-12-01",
                      "column": "人物故事", "awareness": "A", "template": "t",
                      "facts": ["某球员 99 球"],
                      "fact_source_url": "https://example.com/a"}]}
    assert v.check_facts_traceable(cal)["ok"]


def test_reveal_before_node_date_is_rejected():
    ns = _nodes()
    for n in ns:
        if n.get("id") == "ballon_dor_2026":
            n["reveal_at"] = "2026-10-25 03:00"
    r = v.check_reveal_window({"nodes": ns})
    assert not r["ok"]
    assert "早于节点日" in "\n".join(r["detail"])


def test_reveal_generates_nothing_after_deadline():
    """行为验证：过了揭晓时刻，该节点必须从选题里消失。"""
    ns = [n for n in _nodes() if n.get("id") == "ballon_dor_2026"]
    node = ns[0]
    rv = ds._as_dt(node["reveal_at"])
    from datetime import timedelta
    got = ds.build_node_topics(ds._as_date(node["date"]), horizon_days=7,
                               calendar={"nodes": ns}, now=rv + timedelta(minutes=1))
    assert got == []


def test_pregen_missing_facts_injection_is_caught(monkeypatch):
    """facts 存进日历但没注入提示词＝白存，必须被抓。"""
    tmp = Path(tempfile.mkdtemp()) / "pregen.py"
    tmp.write_text(v.PREGN.read_text(encoding="utf-8").replace("{facts_block}", ""),
                   encoding="utf-8")
    monkeypatch.setattr(v, "PREGN", tmp)
    r = v.check_pregen_wired()
    assert not r["ok"]
    assert "facts_block" in "\n".join(r["detail"])


def test_pregen_missing_anti_rewrite_rule_is_caught(monkeypatch):
    """缺「不得改写」约束 → 36 球会被模型写成别的数，必须抓。"""
    tmp = Path(tempfile.mkdtemp()) / "pregen.py"
    tmp.write_text(v.PREGN.read_text(encoding="utf-8").replace("不得改写", "可以改写"),
                   encoding="utf-8")
    monkeypatch.setattr(v, "PREGN", tmp)
    r = v.check_pregen_wired()
    assert not r["ok"]
    assert "不得改写" in "\n".join(r["detail"])
