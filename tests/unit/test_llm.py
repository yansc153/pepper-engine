"""Unit tests for src/llm.py — dual backend adapter."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

import llm
from llm import LLMError, call_llm

_MOONSHOT_URL = "https://api.moonshot.cn/v1/chat/completions"


def _fake_completed(stdout: str = "", stderr: str = "",
                    returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _httpx_response(status: int, body: dict[str, Any] | None = None,
                    text: str = "") -> httpx.Response:
    if body is not None:
        return httpx.Response(status, json=body)
    return httpx.Response(status, text=text)


def _moonshot_body(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}]}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("LLM_BACKEND", "MOONSHOT_API_KEY", "MOONSHOT_BASE_URL",
                "CLAUDE_CLI_PATH"):
        monkeypatch.delenv(key, raising=False)


# ---------- Backend routing ----------

def test_claude_cli_backend_uses_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(llm.os.path, "exists", lambda _: True)
    with patch.object(llm.subprocess, "run",
                      return_value=_fake_completed(stdout="hello world")) as mock:
        result = call_llm("hi", backend="claude_cli")
    assert result == "hello world"
    cmd = mock.call_args.args[0]
    assert cmd[0] == "/usr/bin/claude"
    assert "--model" in cmd and "claude-sonnet-4-6" in cmd
    assert mock.call_args.kwargs["input"] == "hi"


def test_moonshot_backend_uses_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
    mock_post = MagicMock(return_value=_httpx_response(200, _moonshot_body("kimi reply")))
    with patch.object(llm.httpx, "post", mock_post):
        result = call_llm("hi", backend="moonshot")
    assert result == "kimi reply"
    assert mock_post.call_count == 1
    call = mock_post.call_args
    assert call.args[0] == _MOONSHOT_URL
    assert call.kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert call.kwargs["json"]["model"] == "kimi-k2-0905-preview"
    assert call.kwargs["json"]["messages"][0]["content"] == "hi"


def test_backend_defaults_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/bin/claude")
    monkeypatch.setattr(llm.os.path, "exists", lambda _: True)
    with patch.object(llm.subprocess, "run",
                      return_value=_fake_completed(stdout="ok")) as mock:
        result = call_llm("ping")
    assert result == "ok"
    assert mock.called


# ---------- JSON salvage ----------

def test_json_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
    payload = json.dumps({"a": 1, "b": [2, 3]})
    with patch.object(llm.httpx, "post",
                      return_value=_httpx_response(200, _moonshot_body(payload))):
        result = call_llm("x", backend="moonshot", response_format="json")
    assert json.loads(result) == {"a": 1, "b": [2, 3]}


def test_json_strips_code_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
    fenced = "```json\n{\"ok\": true}\n```"
    with patch.object(llm.httpx, "post",
                      return_value=_httpx_response(200, _moonshot_body(fenced))):
        result = call_llm("x", backend="moonshot", response_format="json")
    assert json.loads(result) == {"ok": True}


def test_json_invalid_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
    mock_post = MagicMock(
        return_value=_httpx_response(200, _moonshot_body("not json at all"))
    )
    with patch.object(llm.httpx, "post", mock_post):
        with pytest.raises(LLMError, match="not valid JSON"):
            call_llm("x", backend="moonshot", response_format="json",
                     max_retries=1)
    # max_retries=1 → 2 total attempts
    assert mock_post.call_count == 2


# ---------- Error semantics ----------

def test_unknown_backend_raises() -> None:
    with pytest.raises(LLMError, match="unknown backend 'xxx'"):
        call_llm("hi", backend="xxx")


def test_moonshot_missing_api_key_raises() -> None:
    with pytest.raises(LLMError, match="MOONSHOT_API_KEY"):
        call_llm("hi", backend="moonshot")


def test_claude_cli_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CLI_PATH", "/nonexistent/path/to/claude-xyz")
    monkeypatch.setattr(llm.shutil, "which", lambda _: None)
    with pytest.raises(LLMError, match="not found"):
        call_llm("hi", backend="claude_cli")


def test_claude_cli_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/bin/claude")
    monkeypatch.setattr(llm.os.path, "exists", lambda _: True)

    def _raise_timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(llm.subprocess, "run", _raise_timeout)
    with pytest.raises(LLMError, match="timeout"):
        call_llm("hi", backend="claude_cli", timeout=1)


def test_claude_cli_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/bin/claude")
    monkeypatch.setattr(llm.os.path, "exists", lambda _: True)
    monkeypatch.setattr(
        llm.subprocess, "run",
        lambda *a, **kw: _fake_completed(stderr="boom", returncode=2),
    )
    with pytest.raises(LLMError, match="exit=2"):
        call_llm("hi", backend="claude_cli")


def test_moonshot_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
    with patch.object(llm.httpx, "post",
                      return_value=_httpx_response(500, text="server explosion")):
        with pytest.raises(LLMError, match="HTTP 500"):
            call_llm("hi", backend="moonshot")


def test_moonshot_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")

    def _raise_timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise httpx.ConnectTimeout("slow")

    with patch.object(llm.httpx, "post", _raise_timeout):
        with pytest.raises(LLMError, match="timeout"):
            call_llm("hi", backend="moonshot", timeout=1)


def test_moonshot_response_format_json_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
    mock_post = MagicMock(
        return_value=_httpx_response(200, _moonshot_body('{"x":1}'))
    )
    with patch.object(llm.httpx, "post", mock_post):
        call_llm("hi", backend="moonshot", response_format="json")
    assert mock_post.call_args.kwargs["json"]["response_format"] == {
        "type": "json_object",
    }
