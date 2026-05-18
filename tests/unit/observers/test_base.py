"""Contract tests for `src/observers/base.py`.

The Observation dataclass + SourceAdapter Protocol are imported by every other
subagent's module, so this file is the canary: it must stay green.
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import get_type_hints

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from observers.base import (  # noqa: E402
    Observation,
    ObservationValidationError,
    SourceAdapter,
    from_db_row,
    from_scrape_dict,
    to_db_row,
)


def _make_obs(**overrides: object) -> Observation:
    base = dict(
        source="x_list_finance",
        author_handle="cz_binance",
        author_tier=1,
        content="hello",
        posted_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
        likes=10,
        retweets=2,
        replies=1,
        impressions=None,
        has_image=True,
        raw_url="https://x.com/cz_binance/status/1",
        topic_hint=None,
    )
    base.update(overrides)
    return Observation(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. instantiation + frozen
# ---------------------------------------------------------------------------
def test_observation_can_be_instantiated_and_is_frozen() -> None:
    obs = _make_obs()
    assert obs.author_handle == "cz_binance"
    with pytest.raises(dataclasses.FrozenInstanceError):
        obs.likes = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. impressions=None is legal
# ---------------------------------------------------------------------------
def test_impressions_none_is_legal() -> None:
    obs = _make_obs(impressions=None)
    assert obs.impressions is None


# ---------------------------------------------------------------------------
# 3. invalid tier rejected at runtime (Literal is checked structurally
#    in our __post_init__, since CPython does not enforce Literal)
# ---------------------------------------------------------------------------
def test_invalid_tier_raises() -> None:
    with pytest.raises(ObservationValidationError):
        _make_obs(author_tier=4)  # type: ignore[arg-type]


def test_invalid_source_raises() -> None:
    with pytest.raises(ObservationValidationError):
        _make_obs(source="discord")  # type: ignore[arg-type]


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ObservationValidationError):
        _make_obs(posted_at=datetime(2026, 5, 17, 10, 0))


# ---------------------------------------------------------------------------
# 4. from_scrape_dict handles missing fields with sensible defaults
# ---------------------------------------------------------------------------
def test_from_scrape_dict_fills_defaults() -> None:
    payload = {
        "author": "@huajiao",
        "text": "盘前观察",
        "url": "https://x.com/huajiao/status/42",
        "created_at": "2026-05-17T01:00:00Z",
    }
    obs = from_scrape_dict(payload, source="x_list_finance", tier=2)
    assert obs.author_handle == "huajiao"          # @ stripped
    assert obs.likes == 0 and obs.retweets == 0    # defaulted
    assert obs.impressions is None
    assert obs.has_image is False
    assert obs.posted_at.tzinfo is timezone.utc


# ---------------------------------------------------------------------------
# 5. from_scrape_dict rejects unknown source
# ---------------------------------------------------------------------------
def test_from_scrape_dict_unknown_source() -> None:
    with pytest.raises(ObservationValidationError):
        from_scrape_dict(
            {"text": "x", "url": "u", "created_at": "2026-05-17T01:00:00Z"},
            source="bilibili",  # type: ignore[arg-type]
            tier=1,
        )


def test_from_scrape_dict_missing_posted_at() -> None:
    with pytest.raises(ObservationValidationError):
        from_scrape_dict({"text": "x", "url": "u"}, source="x_list_finance", tier=2)


# ---------------------------------------------------------------------------
# 6. round-trip Observation -> to_db_row -> from_db_row preserves equality
# ---------------------------------------------------------------------------
def test_db_round_trip_preserves_equality() -> None:
    obs = _make_obs(
        impressions=12345,
        topic_hint="pre_market",
        posted_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
    )
    row = to_db_row(obs)
    restored = from_db_row(row)
    assert restored == obs


def test_db_round_trip_with_news_flash_tier_zero() -> None:
    obs = _make_obs(
        source="eastmoney_guba",
        author_tier=0,
        author_handle="eastmoney",
        has_image=False,
    )
    restored = from_db_row(to_db_row(obs))
    assert restored == obs
    assert restored.author_tier == 0


# ---------------------------------------------------------------------------
# 7. posted_at serialised as ISO 8601 string
# ---------------------------------------------------------------------------
def test_to_db_row_serialises_posted_at_iso() -> None:
    obs = _make_obs(posted_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc))
    row = to_db_row(obs)
    assert isinstance(row["posted_at"], str)
    assert row["posted_at"].startswith("2026-05-17T10:00:00")
    assert row["posted_at"].endswith("+00:00")


# ---------------------------------------------------------------------------
# 8. to_db_row coerces bools to SQLite-friendly ints
# ---------------------------------------------------------------------------
def test_to_db_row_has_image_is_int() -> None:
    row = to_db_row(_make_obs(has_image=True))
    assert row["has_image"] == 1
    row2 = to_db_row(_make_obs(has_image=False))
    assert row2["has_image"] == 0


# ---------------------------------------------------------------------------
# 9. SourceAdapter Protocol is structurally satisfied at runtime
# ---------------------------------------------------------------------------
def test_source_adapter_protocol_is_runtime_checkable() -> None:
    class FakeAdapter:
        name = "x_list_finance"
        cookie_env_key = "XUEQIU_COOKIE_FILE"
        rate_limit_per_hour = 24

        async def fetch_latest(self, since: datetime) -> list[Observation]:
            return []

        async def health_check(self) -> bool:
            return True

    fake = FakeAdapter()
    assert isinstance(fake, SourceAdapter)


def test_source_adapter_rejects_missing_method() -> None:
    class IncompleteAdapter:
        name = "x_list_finance"
        cookie_env_key = "X"
        rate_limit_per_hour = 1
        # no fetch_latest / health_check

    assert not isinstance(IncompleteAdapter(), SourceAdapter)


# ---------------------------------------------------------------------------
# 10. Type hints are introspectable (proxy for mypy-strict cleanliness)
# ---------------------------------------------------------------------------
def test_observation_type_hints_resolve() -> None:
    hints = get_type_hints(Observation)
    assert hints["impressions"] == (int | None)
    assert hints["has_image"] is bool
    assert hints["posted_at"] is datetime


def test_from_db_row_missing_column_raises() -> None:
    with pytest.raises(ObservationValidationError):
        from_db_row({"source": "x_list_finance"})  # nowhere near complete
