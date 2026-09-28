"""验证每周 LLM 额度校准提醒脚本（remind_llm_calibration.py）。

- build_report() 能从 usage 文件生成含已消耗模型 / 待办清单的正文
- 全 0 / 缺失文件时优雅降级
- 无凭证时 send() 静默跳过，不抛异常
全部离线，不消耗网络与额度。
"""

import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import remind_llm_calibration as R  # noqa: E402


def test_report_lists_consumed_models(tmp_path, monkeypatch):
    f = tmp_path / "usage.json"
    f.write_text(json.dumps({
        "_calibration_note": "should be ignored",
        "deepseek-v4-flash-202605": {"total_tokens": 636128},
        "kimi-k3": {"total_tokens": 117000},
    }))
    monkeypatch.setattr(R, "LLM_USAGE_FILE", f)
    report = R.build_report()
    assert "deepseek-v4-flash-202605" in report
    assert "636,128" in report
    assert "kimi-k3" in report
    assert "本周待办" in report
    assert "_calibration_note" not in report, "说明键不应出现在正文"


def test_report_handles_empty_usage(tmp_path, monkeypatch):
    f = tmp_path / "usage.json"
    f.write_text("{}")
    monkeypatch.setattr(R, "LLM_USAGE_FILE", f)
    report = R.build_report()
    assert "校准提醒" in report
    assert "本周待办" in report


def test_report_handles_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "LLM_USAGE_FILE", tmp_path / "nope.json")
    report = R.build_report()  # 不应抛异常
    assert "校准提醒" in report


def test_send_noop_without_credentials(monkeypatch):
    monkeypatch.setattr(R, "WXPUSHER_APPTOKEN", "")
    monkeypatch.setattr(R, "WXPUSHER_UID", "")
    assert R.send("t", "c") is False  # 静默跳过，不抛异常


def test_send_posts_when_configured(monkeypatch):
    monkeypatch.setattr(R, "WXPUSHER_APPTOKEN", "tok")
    monkeypatch.setattr(R, "WXPUSHER_UID", "uid")
    fake = mock.Mock(status_code=200)
    fake.json.return_value = {"code": 1000, "msg": "处理成功", "data": [12345]}
    with mock.patch("requests.post", return_value=fake) as mp:
        assert R.send("标题", "正文") is True
        args, kwargs = mp.call_args
        assert "wxpusher" in args[0]
        assert kwargs["json"]["appToken"] == "tok"
        assert kwargs["json"]["uids"] == ["uid"]


def test_send_reports_business_failure(monkeypatch):
    """HTTP 200 但 code != 1000（业务失败）应返回 False，避免假成功。"""
    monkeypatch.setattr(R, "WXPUSHER_APPTOKEN", "tok")
    monkeypatch.setattr(R, "WXPUSHER_UID", "uid")
    fake = mock.Mock(status_code=200)
    fake.json.return_value = {"code": 1005, "msg": "appToken 无效", "data": None}
    with mock.patch("requests.post", return_value=fake):
        assert R.send("标题", "正文") is False
