"""
Unified LLM adapter.

Two backends behind one signature:
- claude_cli (dev default): local `claude` CLI via subprocess
- moonshot   (prod):        Moonshot kimi HTTPS API

Backend is chosen by the `backend` kwarg or `LLM_BACKEND` env (default claude_cli).
Failures always raise LLMError; no silent fallback between backends.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Literal

import httpx

__all__ = ["LLMError", "call_llm"]

ResponseFormat = Literal["text", "json"]

_VALID_BACKENDS = frozenset({"claude_cli", "moonshot"})
_DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
_DEFAULT_MOONSHOT_MODEL = "kimi-k2-0905-preview"
_DEFAULT_MOONSHOT_BASE = "https://api.moonshot.cn/v1"


class LLMError(RuntimeError):
    """Any LLM call failure (transport, backend, parse, timeout)."""


def call_llm(
    prompt: str,
    *,
    model: str | None = None,
    backend: str | None = None,
    response_format: ResponseFormat = "text",
    timeout: int = 90,
    max_retries: int = 1,
) -> str:
    """Call configured LLM backend; raise LLMError on any failure."""
    chosen = (backend or os.environ.get("LLM_BACKEND") or "claude_cli").strip()
    if chosen not in _VALID_BACKENDS:
        raise LLMError(
            f"unknown backend '{chosen}'; expected one of {sorted(_VALID_BACKENDS)}"
        )

    attempts = max(1, max_retries + 1)
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            if chosen == "claude_cli":
                raw = _call_claude_cli(prompt, model or _DEFAULT_CLAUDE_MODEL, timeout)
            else:
                raw = _call_moonshot(
                    prompt,
                    model or _DEFAULT_MOONSHOT_MODEL,
                    timeout,
                    response_format,
                )
            if response_format == "json":
                return _coerce_json(raw)
            return raw
        except LLMError as exc:
            last_error = exc
            continue

    assert last_error is not None
    raise last_error


def _call_claude_cli(prompt: str, model: str, timeout: int) -> str:
    """Subprocess into local claude CLI; stdout is the answer."""
    cli_path = os.environ.get("CLAUDE_CLI_PATH", "claude")
    resolved = cli_path if os.path.isabs(cli_path) else shutil.which(cli_path)
    if not resolved or not os.path.exists(resolved):
        raise LLMError(f"claude CLI not found at '{cli_path}'")

    cmd = [resolved, "--print", "--model", model, "--output-format", "text"]
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"claude CLI timeout after {timeout}s") from exc
    except OSError as exc:
        raise LLMError(f"claude CLI exec failed: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:400]
        raise LLMError(f"claude CLI exit={result.returncode}: {stderr}")

    out = (result.stdout or "").strip()
    if not out:
        raise LLMError("claude CLI returned empty output")
    return out


def _call_moonshot(
    prompt: str,
    model: str,
    timeout: int,
    response_format: ResponseFormat,
) -> str:
    """POST to Moonshot chat completions; return assistant message content."""
    api_key = os.environ.get("MOONSHOT_API_KEY", "").strip()
    if not api_key:
        raise LLMError("MOONSHOT_API_KEY env var is required for moonshot backend")

    base = os.environ.get("MOONSHOT_BASE_URL", _DEFAULT_MOONSHOT_BASE).rstrip("/")
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    if response_format == "json":
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise LLMError(f"moonshot timeout after {timeout}s") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"moonshot transport error: {exc}") from exc

    if response.status_code >= 400:
        body = response.text[:400]
        raise LLMError(f"moonshot HTTP {response.status_code}: {body}")

    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"moonshot malformed response: {exc}") from exc

    if not isinstance(content, str) or not content.strip():
        raise LLMError("moonshot returned empty content")
    return content.strip()


def _coerce_json(raw: str) -> str:
    """Validate JSON; strip ``` fences if present. Return canonical text."""
    candidate = raw.strip()
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        pass

    stripped = _strip_code_fence(candidate)
    if stripped != candidate:
        try:
            json.loads(stripped)
            return stripped
        except json.JSONDecodeError:
            pass

    raise LLMError(f"response is not valid JSON: {candidate[:200]}")


def _strip_code_fence(text: str) -> str:
    """Remove leading ```json / ``` and trailing ``` markers."""
    if not text.startswith("```"):
        return text
    body = text[3:]
    if "\n" in body:
        first_line, rest = body.split("\n", 1)
    else:
        first_line, rest = body, ""
    # drop language tag line (e.g. 'json')
    if first_line.strip().lower() in {"", "json"}:
        body = rest
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()
