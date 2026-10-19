"""周一校准推送值守测试（scripts/check_calibration_push.py）。

背景：2026-10-19 GitHub 整次丢弃 calibration-reminder 的 09:03 调度触发，
既无 failed 也无 queued。值守脚本在 heartbeat 午检时核对，缺运行才告警。
"""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import check_calibration_push as ccp  # noqa: E402


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = str(self._payload)[:200]

    def json(self):
        return self._payload


def _runs(*created_ats, status="completed", conclusion="success"):
    return [
        {"created_at": ts, "status": status, "conclusion": conclusion}
        for ts in created_ats
    ]


@pytest.fixture
def github_env(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "chenwu6688/football-auto-publish")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("WXPUSHER_APPTOKEN", "fake-app")
    monkeypatch.setenv("WXPUSHER_UID", "fake-uid")


def test_non_monday_skips_without_api_call(monkeypatch):
    called = {"get": 0}
    monkeypatch.setattr("requests.get", lambda *a, **k: called.__setitem__("get", 1))
    # 2026-10-20 是周二
    assert ccp.main(["--date", "2026-10-20"]) == 0
    assert called["get"] == 0, "非周一不应查询 API"


def test_monday_with_run_does_not_alert(github_env, monkeypatch):
    posted = []
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: FakeResp(200, {"workflow_runs": _runs("2026-10-19T02:05:00Z")}),
    )
    monkeypatch.setattr("requests.post", lambda *a, **k: posted.append(k) or FakeResp(200, {"code": 1000}))

    assert ccp.main(["--date", "2026-10-19"]) == 0
    assert not posted, "已有运行不应告警"


def test_queued_run_counts_as_triggered(github_env, monkeypatch):
    """排队中说明只是延迟（GitHub 高峰期常见），不算丢调度，不告警。"""
    posted = []
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: FakeResp(200, {"workflow_runs": _runs(
            "2026-10-19T01:05:00Z", status="queued", conclusion=None)}),
    )
    monkeypatch.setattr("requests.post", lambda *a, **k: posted.append(k) or FakeResp(200, {"code": 1000}))

    assert ccp.main(["--date", "2026-10-19"]) == 0
    assert not posted, "queued 应视为已触发"


def test_monday_without_run_alerts(github_env, monkeypatch):
    posted = []
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: FakeResp(200, {"workflow_runs": _runs(
            "2026-10-12T01:05:00Z", "2026-10-05T01:05:00Z")}),
    )

    def fake_post(url, json=None, **kw):
        posted.append(json)
        return FakeResp(200, {"code": 1000})

    monkeypatch.setattr("requests.post", fake_post)

    assert ccp.main(["--date", "2026-10-19"]) == 0
    assert len(posted) == 1, "缺运行应推送一次告警"
    content = posted[0]["content"]
    assert "2026-10-19" in content
    assert "actions/workflows/calibration-reminder.yml" in content, "必须给出补跑链接"


def test_alert_push_failure_returns_nonzero(github_env, monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: FakeResp(200, {"workflow_runs": _runs("2026-10-12T01:05:00Z")}),
    )
    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResp(200, {"code": 1001}))

    assert ccp.main(["--date", "2026-10-19"]) == 1, "告警未受理应返回 1 便于 CI 标红"


def test_utc_previous_day_converts_to_today(monkeypatch, github_env):
    """UTC 10-18 17:00 = CST 10-19 01:00，必须算今日运行（时区换算回归）。"""
    posted = []
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: FakeResp(200, {"workflow_runs": _runs("2026-10-18T17:00:00Z")}),
    )
    monkeypatch.setattr("requests.post", lambda *a, **k: posted.append(k) or FakeResp(200, {"code": 1000}))

    assert ccp.main(["--date", "2026-10-19"]) == 0
    assert not posted, "跨 UTC 日界的同一天运行不得误判为缺失"


def test_missing_credentials_skips_silently(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    called = {"get": 0}
    monkeypatch.setattr("requests.get", lambda *a, **k: called.__setitem__("get", 1))
    assert ccp.main(["--date", "2026-10-19"]) == 0
    assert called["get"] == 0


def test_api_error_does_not_fake_alert(github_env, monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: FakeResp(500, {"message": "boom"}))
    posted = []
    monkeypatch.setattr("requests.post", lambda *a, **k: posted.append(k) or FakeResp(200, {"code": 1000}))
    assert ccp.main(["--date", "2026-10-19"]) == 0
    assert not posted, "拿不到运行记录时不得伪造「缺失」告警"


def test_dry_run_does_not_push(github_env, monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: FakeResp(200, {"workflow_runs": _runs("2026-10-12T01:05:00Z")}),
    )
    posted = []
    monkeypatch.setattr("requests.post", lambda *a, **k: posted.append(k) or FakeResp(200, {"code": 1000}))
    assert ccp.main(["--date", "2026-10-19", "--dry-run"]) == 0
    assert not posted
