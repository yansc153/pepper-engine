"""Unit tests for src/main.py — orchestrator dispatch.

Strategy: each command function is exercised with downstream modules mocked
out via ``monkeypatch.setitem(sys.modules, ...)``. We assert (a) the right
downstream entry was called with the right args, (b) the JSON summary line
was emitted, and (c) the exit code matches expectations including error
paths.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import main as main_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> types.ModuleType:
    """Drop a fake module into ``sys.modules`` so command imports resolve to it."""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    # If the name is dotted, also register every parent so ``from a.b import c`` works.
    parts = name.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
    return mod


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = main_module.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    # Last line is the JSON summary.
    summary = json.loads(out[-1])
    return code, summary


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_help_lists_eight_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main_module.main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    for cmd in ("observe", "post", "discord_poll", "self_monitor",
                "mine", "review", "remine", "test"):
        assert cmd in help_text


def test_unknown_command_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main_module.main(["bogus"])
    assert exc.value.code != 0


def test_dry_run_sets_env(monkeypatch: pytest.MonkeyPatch,
                          capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("DRY_RUN", raising=False)
    # Use the test command — fully self-contained.
    code, summary = _run(["test", "--dry-run"], capsys)
    # test command itself also sets DRY_RUN=1; assert it's present after the run.
    import os
    assert os.environ.get("DRY_RUN") == "1"
    assert summary["cmd"] == "test"


# ---------------------------------------------------------------------------
# observe
# ---------------------------------------------------------------------------


def test_observe_runs_runner_and_scores(monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    @dataclass
    class FakeReport:
        success_count: int = 3
        error_count: int = 0
        observations_inserted: int = 11

    run_once_mock = AsyncMock(return_value=FakeReport())
    score_mock = MagicMock(return_value=type("R", (), {"created": 4, "top_score": 87.5})())
    expire_mock = MagicMock(return_value=2)

    fake_runner = _install_module(monkeypatch, "src.observers.runner", run_once=run_once_mock)
    assert fake_runner.run_once is run_once_mock
    fake_db = _install_module(
        monkeypatch, "src.database",
        get_conn=MagicMock(return_value=MagicMock(close=MagicMock())),
    )
    assert fake_db.get_conn
    _install_module(
        monkeypatch, "selector",
        score_topics=score_mock,
        pick_top_topic=MagicMock(),
        expire_old_candidates=expire_mock,
    )

    code, summary = _run(["observe"], capsys)
    assert code == 0
    assert summary["observations_inserted"] == 11
    assert summary["topics_created"] == 4
    assert summary["top_virality_score"] == 87.5
    run_once_mock.assert_awaited_once()
    score_mock.assert_called_once()
    expire_mock.assert_called_once()


# ---------------------------------------------------------------------------
# post
# ---------------------------------------------------------------------------


def test_post_writes_and_pushes_to_discord(monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    topic = {"id": 7, "topic_summary": "x"}
    pick_mock = MagicMock(return_value=topic)
    draft_obj = MagicMock(success=True, draft_id=42, error=None, score_total=80)
    write_mock = AsyncMock(return_value=draft_obj)
    push_mock = AsyncMock(return_value="msg-123")

    _install_module(
        monkeypatch, "src.database",
        get_conn=MagicMock(return_value=MagicMock(close=MagicMock())),
    )
    _install_module(
        monkeypatch, "selector",
        pick_top_topic=pick_mock,
        score_topics=MagicMock(),
        expire_old_candidates=MagicMock(),
    )
    _install_module(monkeypatch, "src.writer", write_draft=write_mock)
    _install_module(monkeypatch, "src.discord.bot", push_draft_to_discord=push_mock)

    code, summary = _run(["post"], capsys)
    assert code == 0
    assert summary["draft_id"] == 42
    assert summary["discord_message_id"] == "msg-123"
    write_mock.assert_awaited_once_with(topic)
    push_mock.assert_awaited_once_with(42)


def test_post_returns_exit_1_when_writer_raises(monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    """Writer exceptions must be visible to cron: exit non-zero + alert called."""
    topic = {"id": 7, "topic_summary": "x", "predicted_topic_lane": "intraday"}
    write_mock = AsyncMock(side_effect=RuntimeError("LLM timeout"))
    alert_mock = MagicMock(return_value=True)

    _install_module(
        monkeypatch, "src.database",
        get_conn=MagicMock(return_value=MagicMock(close=MagicMock())),
    )
    _install_module(
        monkeypatch, "selector",
        pick_top_topic=MagicMock(return_value=topic),
        score_topics=MagicMock(),
        expire_old_candidates=MagicMock(),
    )
    _install_module(monkeypatch, "src.writer", write_draft=write_mock)
    _install_module(monkeypatch, "src.discord.bot", push_draft_to_discord=AsyncMock())
    _install_module(monkeypatch, "src.alerting", alert=alert_mock)

    code, summary = _run(["post"], capsys)
    assert code == 1, "writer failure must surface as non-zero exit"
    assert summary["error"] == "write_failed"
    assert "LLM timeout" in summary["detail"]
    alert_mock.assert_called_once()


def test_post_skips_when_no_topic(monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    _install_module(
        monkeypatch, "src.database",
        get_conn=MagicMock(return_value=MagicMock(close=MagicMock())),
    )
    _install_module(
        monkeypatch, "selector",
        pick_top_topic=MagicMock(return_value=None),
        score_topics=MagicMock(),
        expire_old_candidates=MagicMock(),
    )
    write_mock = AsyncMock()
    _install_module(monkeypatch, "src.writer", write_draft=write_mock)
    _install_module(monkeypatch, "src.discord.bot", push_draft_to_discord=AsyncMock())

    code, summary = _run(["post"], capsys)
    assert code == 0
    assert summary["skipped"] == "no_fresh_topic"
    write_mock.assert_not_called()


def test_post_does_not_call_publisher(monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    """Manual-mode regression: post command MUST NOT import or call publisher."""
    pick_mock = MagicMock(return_value={"id": 1})
    draft_obj = MagicMock(success=True, draft_id=1, error=None, score_total=70)
    publisher_sentinel = MagicMock()
    publisher_sentinel.post_tweet = MagicMock(
        side_effect=AssertionError("publisher must not be called in manual mode")
    )

    _install_module(
        monkeypatch, "src.database",
        get_conn=MagicMock(return_value=MagicMock(close=MagicMock())),
    )
    _install_module(
        monkeypatch, "selector",
        pick_top_topic=pick_mock,
        score_topics=MagicMock(),
        expire_old_candidates=MagicMock(),
    )
    _install_module(monkeypatch, "src.writer", write_draft=AsyncMock(return_value=draft_obj))
    _install_module(monkeypatch, "src.discord.bot", push_draft_to_discord=AsyncMock(return_value="m"))
    monkeypatch.setitem(sys.modules, "src.publisher", publisher_sentinel)

    code, _ = _run(["post"], capsys)
    assert code == 0
    publisher_sentinel.post_tweet.assert_not_called()


def test_post_draft_rejected_returns_zero(monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    draft_obj = MagicMock(success=False, draft_id=None, error="score 40 below threshold",
                          score_total=40)
    _install_module(
        monkeypatch, "src.database",
        get_conn=MagicMock(return_value=MagicMock(close=MagicMock())),
    )
    _install_module(
        monkeypatch, "selector",
        pick_top_topic=MagicMock(return_value={"id": 9}),
        score_topics=MagicMock(),
        expire_old_candidates=MagicMock(),
    )
    _install_module(monkeypatch, "src.writer", write_draft=AsyncMock(return_value=draft_obj))
    push_mock = AsyncMock()
    _install_module(monkeypatch, "src.discord.bot", push_draft_to_discord=push_mock)

    code, summary = _run(["post"], capsys)
    assert code == 0
    assert summary["skipped"] == "draft_rejected"
    assert "below threshold" in summary["error"]
    push_mock.assert_not_called()


# ---------------------------------------------------------------------------
# discord_poll
# ---------------------------------------------------------------------------


def test_discord_poll_returns_advanced_count(monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    poll_mock = AsyncMock(return_value=5)
    _install_module(monkeypatch, "src.discord.bot", poll_reactions=poll_mock)
    code, summary = _run(["discord_poll"], capsys)
    assert code == 0
    assert summary["drafts_advanced"] == 5
    poll_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# self_monitor
# ---------------------------------------------------------------------------


def test_self_monitor_calls_reconcile(monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    result = type("R", (), {"scanned": 10, "bound": 3, "wild": 1, "errors": 0})()
    adapter = MagicMock()
    adapter.reconcile = AsyncMock(return_value=result)
    cls = MagicMock(return_value=adapter)
    _install_module(monkeypatch, "src.observers.self_monitor_adapter", SelfMonitorAdapter=cls)

    code, summary = _run(["self_monitor"], capsys)
    assert code == 0
    assert summary["bound"] == 3
    assert summary["wild"] == 1
    adapter.reconcile.assert_awaited_once()


def test_self_monitor_misconfig_exits_nonzero_and_alerts(monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """Codex Round-2 fix: TWITTER_HANDLE unset is misconfig (silent failure
    before this fix), must exit non-zero and trigger alert so cron is loud."""
    adapter = MagicMock()
    adapter.reconcile = AsyncMock(side_effect=RuntimeError("TWITTER_HANDLE env var unset"))
    cls = MagicMock(return_value=adapter)
    _install_module(monkeypatch, "src.observers.self_monitor_adapter", SelfMonitorAdapter=cls)
    alert_mock = MagicMock(return_value=True)
    _install_module(monkeypatch, "src.alerting", alert=alert_mock)

    code, summary = _run(["self_monitor"], capsys)
    assert code == 1, "misconfig must surface as non-zero exit, not silent skip"
    assert summary["error"] == "self_monitor_misconfig"
    alert_mock.assert_called_once()


# ---------------------------------------------------------------------------
# mine / remine
# ---------------------------------------------------------------------------


def test_mine_distills_and_weaves(monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    distill_mock = MagicMock(return_value=[1, 2, 3])
    weave_mock = MagicMock(return_value=7)
    _install_module(monkeypatch, "src.miner",
                    full_distill=distill_mock, weave_nightly=weave_mock,
                    weave_full=MagicMock())

    code, summary = _run(["mine"], capsys)
    assert code == 0
    assert summary["entries_distilled"] == 3
    assert summary["edges_created"] == 7
    distill_mock.assert_called_once()
    weave_mock.assert_called_once_with([1, 2, 3])


def test_remine_runs_weave_full(monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture[str]) -> None:
    weave_full_mock = MagicMock(return_value=(12, 4))
    _install_module(monkeypatch, "src.miner",
                    full_distill=MagicMock(), weave_nightly=MagicMock(),
                    weave_full=weave_full_mock)
    code, summary = _run(["remine"], capsys)
    assert code == 0
    assert summary["entries_decayed"] == 12
    assert summary["entries_pruned"] == 4


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def test_review_skips_when_reviewer_entry_missing(monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    """If reviewer module is present but the entry function is missing, skip cleanly."""
    fake = types.ModuleType("src.reviewer")
    # Intentionally no review_and_update_weights attribute.
    monkeypatch.setitem(sys.modules, "src.reviewer", fake)
    import src as src_pkg
    monkeypatch.setattr(src_pkg, "reviewer", fake, raising=False)

    code, summary = _run(["review"], capsys)
    assert code == 0
    assert summary["skipped"] == "reviewer_entry_missing"


def test_review_calls_reviewer_when_present(monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    fn = MagicMock(return_value={"posts_reviewed": 8, "weights_updated": 2})
    fake = types.ModuleType("src.reviewer")
    fake.review_and_update_weights = fn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.reviewer", fake)
    # Ensure ``from src import reviewer`` finds it on the package.
    import src as src_pkg
    monkeypatch.setattr(src_pkg, "reviewer", fake, raising=False)

    code, summary = _run(["review"], capsys)
    assert code == 0
    assert summary["reviewer_summary"]["posts_reviewed"] == 8
    fn.assert_called_once()


# ---------------------------------------------------------------------------
# test command
# ---------------------------------------------------------------------------


def test_test_command_smoke_runs(capsys: pytest.CaptureFixture[str]) -> None:
    """``python -m src.main test`` should exit 0 in a healthy checkout."""
    code, summary = _run(["test"], capsys)
    # DB might be uninitialised in a fresh checkout — we only require imports OK.
    assert "modules_ok" in summary
    # Should not raise; exit code matches db_ok + imports state.
    assert code in (0, 1)


# ---------------------------------------------------------------------------
# error path
# ---------------------------------------------------------------------------


def test_command_exception_surfaces_as_exit_one(monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    poll_mock = AsyncMock(side_effect=RuntimeError("kaboom"))
    _install_module(monkeypatch, "src.discord.bot", poll_reactions=poll_mock)
    code, summary = _run(["discord_poll"], capsys)
    assert code == 1
    assert summary["success"] is False
    assert "kaboom" in summary["error"]


# ---------------------------------------------------------------------------
# JSON summary contract
# ---------------------------------------------------------------------------


def test_summary_always_includes_required_fields(capsys: pytest.CaptureFixture[str]) -> None:
    code, summary = _run(["test"], capsys)
    del code
    for required in ("cmd", "duration_s", "success"):
        assert required in summary, f"missing {required} in summary"
    assert isinstance(summary["duration_s"], (int, float))
