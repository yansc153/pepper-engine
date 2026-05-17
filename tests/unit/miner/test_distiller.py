"""Unit tests for src.miner.distiller — validation + idempotence + persona-leak."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.database import get_conn
from src.miner import distiller
from src.miner.distiller import (
    DistillError,
    full_distill,
    light_distill,
    validate_distillation,
)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "llm"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _insert_obs(db: Path, *, obs_id: int = 1, content: str = "test", viral: int = 1) -> None:
    conn = get_conn(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO reaction_observations (id, source, author_handle, "
                "author_tier, content, posted_at, likes, retweets, replies, "
                "has_image, raw_url, viral_score, is_viral) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    obs_id,
                    "xueqiu",
                    "alice",
                    1,
                    content,
                    "2026-05-01 00:00:00",
                    100,
                    0,
                    0,
                    0,
                    f"https://example.com/{obs_id}",
                    100.0,
                    viral,
                ),
            )
    finally:
        conn.close()


# ---------------- validation ----------------


def test_validate_accepts_clean_fixture() -> None:
    payload = json.loads(_load("distill_valid.json"))
    out = validate_distillation(payload)
    assert out["hook_pattern"] == "反共识开场"


def test_validate_rejects_bad_persona() -> None:
    payload = json.loads(_load("distill_bad_persona.json"))
    with pytest.raises(DistillError, match="bad persona"):
        validate_distillation(payload)


def test_validate_rejects_persona_leak_via_ticker() -> None:
    payload = json.loads(_load("distill_valid.json"))
    payload["hook_example"] = "看好 600519 的人请举手"
    with pytest.raises(DistillError, match="leaks identity"):
        validate_distillation(payload)


def test_validate_rejects_at_handle_leak() -> None:
    payload = json.loads(_load("distill_valid.json"))
    payload["hook_example"] = "请教 @somebody 老师"
    with pytest.raises(DistillError, match="leaks identity"):
        validate_distillation(payload)


def test_validate_rejects_unknown_hook_pattern() -> None:
    payload = json.loads(_load("distill_valid.json"))
    payload["hook_pattern"] = "未定义类型"
    with pytest.raises(DistillError, match="bad hook_pattern"):
        validate_distillation(payload)


def test_validate_rejects_too_many_emotions() -> None:
    payload = json.loads(_load("distill_valid.json"))
    payload["emotion_triggers"] = ["FOMO", "嘲讽", "焦虑", "共情"]
    with pytest.raises(DistillError, match="emotion_triggers"):
        validate_distillation(payload)


def test_validate_rejects_stance_out_of_range() -> None:
    payload = json.loads(_load("distill_valid.json"))
    payload["stance_strength"] = 9
    with pytest.raises(DistillError, match="stance_strength"):
        validate_distillation(payload)


# ---------------- light_distill end-to-end ----------------


def test_light_distill_persists_entry(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_obs(tmp_db, obs_id=1, content="this morning everyone bullish but one detail")
    monkeypatch.setattr(
        distiller, "call_llm", lambda *a, **kw: _load("distill_valid.json")
    )
    entry_id = light_distill(1)
    assert entry_id is not None and entry_id > 0
    conn = get_conn(tmp_db)
    try:
        row = conn.execute(
            "SELECT hook_pattern, distilled_at FROM technique_entries WHERE id=?",
            (entry_id,),
        ).fetchone()
        obs_row = conn.execute(
            "SELECT distilled_at FROM reaction_observations WHERE id=1"
        ).fetchone()
    finally:
        conn.close()
    assert row["hook_pattern"] == "反共识开场"
    assert obs_row["distilled_at"] is not None


def test_light_distill_idempotent(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_obs(tmp_db, obs_id=1, content="x")
    monkeypatch.setattr(
        distiller, "call_llm", lambda *a, **kw: _load("distill_valid.json")
    )
    first = light_distill(1)
    second = light_distill(1)
    assert first == second  # UNIQUE(observation_id) → update in place


def test_light_distill_failure_still_stamps_observation(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_obs(tmp_db, obs_id=2, content="x")
    monkeypatch.setattr(
        distiller, "call_llm", lambda *a, **kw: "not valid json at all"
    )
    result = light_distill(2)
    assert result is None
    conn = get_conn(tmp_db)
    try:
        row = conn.execute(
            "SELECT distilled_at FROM reaction_observations WHERE id=2"
        ).fetchone()
    finally:
        conn.close()
    assert row["distilled_at"] is not None, "must stamp to avoid retry avalanche"


def test_light_distill_retries_once_on_bad_json(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_obs(tmp_db, obs_id=3, content="x")
    calls = {"n": 0}

    def fake(*a: object, **kw: object) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "garbage"
        return _load("distill_valid.json")

    monkeypatch.setattr(distiller, "call_llm", fake)
    entry_id = light_distill(3)
    assert entry_id is not None
    assert calls["n"] == 2  # 1 fail + 1 retry succeeded


def test_full_distill_processes_only_viral_undistilled(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_obs(tmp_db, obs_id=10, content="a", viral=1)
    _insert_obs(tmp_db, obs_id=11, content="b", viral=0)  # not viral, skip
    monkeypatch.setattr(
        distiller, "call_llm", lambda *a, **kw: _load("distill_valid.json")
    )
    from datetime import datetime, timezone

    new_ids = full_distill(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert len(new_ids) == 1
