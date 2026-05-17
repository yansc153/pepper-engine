"""Unit tests for src.alerting — Discord bot REST delivery never raises."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.alerting import alert, _format


def test_alert_no_token_logs_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALERT_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DISCORD_DRAFT_CHANNEL_ID", raising=False)
    with caplog.at_level("WARNING", logger="src.alerting"):
        ok = alert("test message")
    assert ok is False
    assert any("test message" in r.message for r in caplog.records)


def test_alert_no_channel_logs_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.delenv("ALERT_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DISCORD_DRAFT_CHANNEL_ID", raising=False)
    with caplog.at_level("WARNING", logger="src.alerting"):
        ok = alert("test message")
    assert ok is False


def test_alert_uses_alert_channel_id_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok123")
    monkeypatch.setenv("ALERT_CHANNEL_ID", "999")
    monkeypatch.setenv("DISCORD_DRAFT_CHANNEL_ID", "111")  # should be ignored

    mock_resp = MagicMock(status_code=200)
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post = MagicMock(return_value=mock_resp)

    with patch("src.alerting.httpx.Client", return_value=mock_client):
        ok = alert("boom", context={"draft_id": 42})

    assert ok is True
    url = mock_client.post.call_args[0][0]
    assert "/channels/999/messages" in url
    headers = mock_client.post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bot tok123"
    body = mock_client.post.call_args[1]["json"]["content"]
    assert "boom" in body
    assert "draft_id" in body and "42" in body


def test_alert_falls_back_to_draft_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.delenv("ALERT_CHANNEL_ID", raising=False)
    monkeypatch.setenv("DISCORD_DRAFT_CHANNEL_ID", "555")

    mock_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post = MagicMock(return_value=mock_resp)

    with patch("src.alerting.httpx.Client", return_value=mock_client):
        ok = alert("hi")
    assert ok is True
    assert "/channels/555/messages" in mock_client.post.call_args[0][0]


def test_alert_appends_thread_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("ALERT_CHANNEL_ID", "9")
    monkeypatch.setenv("ALERT_THREAD_ID", "thr-7")

    mock_resp = MagicMock(status_code=200, raise_for_status=MagicMock())
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post = MagicMock(return_value=mock_resp)

    with patch("src.alerting.httpx.Client", return_value=mock_client):
        alert("x")
    url = mock_client.post.call_args[0][0]
    assert url.endswith("?thread_id=thr-7")


def test_alert_swallows_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("ALERT_CHANNEL_ID", "9")
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post = MagicMock(side_effect=RuntimeError("network down"))

    with patch("src.alerting.httpx.Client", return_value=mock_client):
        ok = alert("doesn't matter")
    assert ok is False


def test_format_truncates_long_messages() -> None:
    body = _format("X" * 3000, context=None)
    assert len(body) <= 1900
    assert body.endswith("(truncated)")


def test_format_includes_context() -> None:
    body = _format("err", context={"draft_id": 7, "lane": "intraday"})
    assert "draft_id" in body and "7" in body
    assert "lane" in body and "intraday" in body
