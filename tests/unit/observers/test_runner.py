"""Tests for src/observers/runner.py."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from observers.base import Observation  # noqa: E402
from observers.runner import (  # noqa: E402
    EXTERNAL_ADAPTER_NAMES,
    RunReport,
    placeholder_viral_score,
    run_adapters,
)
from src.database import get_conn  # noqa: E402
from src.migrations.runner import run_migrations  # noqa: E402


@pytest.fixture
def db_path(tmp_path) -> Path:
    db = tmp_path / "test_runner.db"
    run_migrations(db, verbose=False)
    return db


def _obs(source: str, raw_url: str, likes: int = 1) -> Observation:
    tier = 0 if source == "eastmoney_guba" else 2
    return Observation(
        source=source,  # type: ignore[arg-type]
        author_handle="x",
        author_tier=tier,  # type: ignore[arg-type]
        content="hello",
        posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        likes=likes,
        retweets=0,
        replies=0,
        impressions=None,
        has_image=True,  # required: runner now drops has_image=False at ingest
        raw_url=raw_url,
        topic_hint=None,
    )


class _FakeAdapter:
    def __init__(self, name: str, observations: list[Observation] | None = None,
                 raise_exc: bool = False) -> None:
        self.name = name
        self.cookie_env_key = ""
        self.rate_limit_per_hour = 10
        self._observations = observations or []
        self._raise_exc = raise_exc

    async def fetch_latest(self, since):
        if self._raise_exc:
            raise RuntimeError("boom")
        return list(self._observations)

    async def health_check(self) -> bool:
        return True


def test_external_adapter_names_two_source_pipeline() -> None:
    """Two-source pipeline: x_list_finance (learn) + eastmoney_guba (content)."""
    assert EXTERNAL_ADAPTER_NAMES == frozenset(
        {"x_list_finance", "eastmoney_guba"}
    )


def test_placeholder_viral_score_weights() -> None:
    o = _obs("x_list_finance", "https://x.com/1", likes=10)
    # likes 10 * 0.5 + retweets 0 + replies 0 -> 5
    assert placeholder_viral_score(o) == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_run_adapters_inserts_observations(db_path) -> None:
    adapter = _FakeAdapter(
        "x_list_finance",
        observations=[
            _obs("x_list_finance", "https://xueqiu.com/1/100"),
            _obs("x_list_finance", "https://xueqiu.com/1/101"),
        ],
    )
    report = await run_adapters(
        [adapter],
        since=datetime(2020, 1, 1, tzinfo=timezone.utc),
        db_path=db_path,
    )
    assert isinstance(report, RunReport)
    assert report.success_count == 1
    assert report.error_count == 0
    assert report.observations_inserted == 2

    conn = get_conn(db_path)
    rows = conn.execute("SELECT raw_url FROM reaction_observations").fetchall()
    conn.close()
    assert {r["raw_url"] for r in rows} == {
        "https://xueqiu.com/1/100",
        "https://xueqiu.com/1/101",
    }


@pytest.mark.asyncio
async def test_run_adapters_dedupes_on_raw_url(db_path) -> None:
    adapter = _FakeAdapter(
        "x_list_finance",
        observations=[
            _obs("x_list_finance", "https://xueqiu.com/1/200"),
            _obs("x_list_finance", "https://xueqiu.com/1/200"),  # dup
        ],
    )
    report = await run_adapters(
        [adapter], since=datetime(2020, 1, 1, tzinfo=timezone.utc), db_path=db_path
    )
    assert report.observations_inserted == 1


@pytest.mark.asyncio
async def test_run_adapters_failure_does_not_block_others(db_path) -> None:
    good = _FakeAdapter(
        "x_list_finance", observations=[_obs("x_list_finance", "https://xueqiu.com/1/300")]
    )
    bad = _FakeAdapter("eastmoney_guba", raise_exc=True)
    report = await run_adapters(
        [good, bad],
        since=datetime(2020, 1, 1, tzinfo=timezone.utc),
        db_path=db_path,
    )
    assert report.success_count == 1
    assert report.error_count == 1
    assert report.observations_inserted == 1


@pytest.mark.asyncio
async def test_run_adapters_writes_source_health(db_path) -> None:
    good = _FakeAdapter("x_list_finance", observations=[])
    bad = _FakeAdapter("eastmoney_guba", raise_exc=True)
    await run_adapters(
        [good, bad],
        since=datetime(2020, 1, 1, tzinfo=timezone.utc),
        db_path=db_path,
    )
    conn = get_conn(db_path)
    rows = {
        r["adapter_name"]: dict(r)
        for r in conn.execute(
            "SELECT adapter_name, consecutive_failures, last_error, last_success_at "
            "FROM source_health"
        ).fetchall()
    }
    conn.close()
    assert rows["x_list_finance"]["consecutive_failures"] == 0
    assert rows["x_list_finance"]["last_success_at"] is not None
    assert rows["eastmoney_guba"]["consecutive_failures"] >= 1
    assert "boom" in (rows["eastmoney_guba"]["last_error"] or "")


@pytest.mark.asyncio
async def test_run_adapters_empty_returns_zero_report() -> None:
    report = await run_adapters(
        [], since=datetime(2020, 1, 1, tzinfo=timezone.utc)
    )
    assert report.as_tuple() == (0, 0, 0)


@pytest.mark.asyncio
async def test_run_adapters_marks_viral_threshold(db_path) -> None:
    # likes=1000 -> 500 score; threshold default 500 -> is_viral=1
    viral = _obs("x_list_finance", "https://xueqiu.com/1/v1", likes=1000)
    adapter = _FakeAdapter("x_list_finance", observations=[viral])
    await run_adapters(
        [adapter],
        since=datetime(2020, 1, 1, tzinfo=timezone.utc),
        db_path=db_path,
        viral_threshold=500.0,
    )
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT is_viral, viral_score FROM reaction_observations WHERE raw_url=?",
        ("https://xueqiu.com/1/v1",),
    ).fetchone()
    conn.close()
    assert row["is_viral"] == 1
    assert row["viral_score"] == pytest.approx(500.0)
