"""Unit tests for src.miner.static_fallback."""

from __future__ import annotations

from src.miner.static_fallback import filter_static, load_static_entries


def test_load_static_entries_returns_negative_ids() -> None:
    entries = load_static_entries()
    assert len(entries) > 0
    assert all(e.id < 0 for e in entries)


def test_load_static_entries_covers_all_finance_lanes() -> None:
    entries = load_static_entries()
    lanes = {e.topic_lane for e in entries}
    # The static markdown should at least populate the 4 finance lanes
    assert {"pre_market", "intraday", "post_market", "overnight"} & lanes


def test_filter_static_respects_avoid() -> None:
    entries = load_static_entries()
    sample = entries[0]
    out = filter_static(
        entries,
        topic_lane=sample.topic_lane,
        post_hour_utc=sample.post_hour_utc,
        persona=sample.applicable_personas[0],
        avoid={sample.id},
        hour_window=24,
        k=5,
    )
    assert sample.id not in {e.id for e in out}


def test_filter_static_caps_at_k() -> None:
    entries = load_static_entries()
    out = filter_static(
        entries,
        topic_lane="pre_market",
        post_hour_utc=22,
        persona="finance_neutral",
        avoid=set(),
        hour_window=24,
        k=2,
    )
    assert len(out) <= 2
