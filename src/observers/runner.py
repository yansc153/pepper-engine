"""External observer runner.

Reads ``config/sources.yaml``, instantiates each enabled non-Twitter adapter,
fans out ``fetch_latest()`` calls concurrently, computes a placeholder viral
score, and writes results into ``reaction_observations`` (de-duplicated by
``raw_url``). Per-adapter health is recorded in ``source_health``.

The x_list_finance / x_list_general adapters are owned by S5b and skipped
here; this runner handles xueqiu / futu / news_flash.

Public entry point: ``await run_once(...)`` — used by S11 orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

# Allow `python -c "from src.observers.runner import run_once"` to work even
# without a conftest by ensuring src/ is on sys.path.
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from observers.base import Observation, SourceAdapter, to_db_row  # noqa: E402
from observers.eastmoney_guba_adapter import EastmoneyGubaAdapter  # noqa: E402
from observers.futu_adapter import FutuAdapter  # noqa: E402
from observers.news_flash_adapter import NewsFlashAdapter  # noqa: E402
from observers.x_list_finance_adapter import XListFinanceAdapter  # noqa: E402
from observers.xueqiu_adapter import XUEQIU_FEED_URL, XueqiuAdapter  # noqa: E402

logger = logging.getLogger(__name__)

# Adapter names this runner owns (Twitter list adapters belong to S5b).
EXTERNAL_ADAPTER_NAMES: frozenset[str] = frozenset(
    {"xueqiu", "futu", "news_flash", "x_list_finance", "eastmoney_guba"}
)

# Default lookback if caller doesn't pass an explicit since.
DEFAULT_LOOKBACK_HOURS = 6


@dataclass(frozen=True)
class RunReport:
    success_count: int
    error_count: int
    observations_inserted: int

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.success_count, self.error_count, self.observations_inserted)


# ---------------------------------------------------------------------------
# Adapter factory
# ---------------------------------------------------------------------------

AdapterFactory = Callable[[Mapping[str, Any]], SourceAdapter]


def _build_xueqiu(cfg: Mapping[str, Any]) -> SourceAdapter:
    return XueqiuAdapter(
        feed_url=cfg.get("feed_url") or XUEQIU_FEED_URL,
        tier_default=int(cfg.get("tier_default", 0)),
        max_posts_per_fetch=int(cfg.get("max_posts_per_fetch") or 30),
    )


def _build_futu(cfg: Mapping[str, Any]) -> SourceAdapter:
    return FutuAdapter(
        feed_url=cfg.get("feed_url") or "https://q.futunn.com/nnq/recommend",
        tier_default=int(cfg.get("tier_default", 0)),
        max_posts_per_fetch=int(cfg.get("max_posts_per_fetch") or 30),
    )


def _build_news_flash(cfg: Mapping[str, Any]) -> SourceAdapter:
    return NewsFlashAdapter(
        tier_default=int(cfg.get("tier_default", 0)),
        max_posts_per_fetch=int(cfg.get("max_posts_per_fetch") or 30),
    )


def _build_x_list_finance(cfg: Mapping[str, Any]) -> SourceAdapter:
    return XListFinanceAdapter(
        list_url=cfg.get("list_url") or "https://x.com/i/lists/2056032482127175889",
        tier_default=int(cfg.get("tier_default", 1)),
        max_posts_per_fetch=int(cfg.get("max_posts_per_fetch") or 30),
    )


def _build_eastmoney_guba(cfg: Mapping[str, Any]) -> SourceAdapter:
    return EastmoneyGubaAdapter(
        homepage_url=cfg.get("homepage_url") or "https://guba.eastmoney.com/",
        min_content_length=int(cfg.get("min_content_length") or 3000),
        max_posts_per_fetch=int(cfg.get("max_posts_per_fetch") or 15),
        detail_concurrency=int(cfg.get("detail_concurrency") or 3),
        tier_default=int(cfg.get("tier_default", 0)),
    )


ADAPTER_BUILDERS: dict[str, AdapterFactory] = {
    "xueqiu": _build_xueqiu,
    "futu": _build_futu,
    "news_flash": _build_news_flash,
    "x_list_finance": _build_x_list_finance,
    "eastmoney_guba": _build_eastmoney_guba,
}


# ---------------------------------------------------------------------------
# Viral score placeholder (will be replaced by src.miner.viral_scorer in S6)
# ---------------------------------------------------------------------------


def placeholder_viral_score(obs: Observation) -> float:
    """Rough early-stage scorer.

    Engagement-weighted, replies counted heaviest. S6 will swap this out via
    ``src.miner.viral_scorer`` once the real signal is calibrated.
    """
    return obs.likes * 0.5 + obs.retweets * 1.0 + obs.replies * 27.0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_once(
    repo_root: Path | None = None,
    db_path: Path | None = None,
    since: datetime | None = None,
    viral_threshold: float = 500.0,
) -> RunReport:
    """Fetch latest from all enabled external adapters concurrently."""
    from src.config_loader import load_all_configs

    root = repo_root or Path(__file__).resolve().parents[2]
    app_config = load_all_configs(root)

    enabled = [
        adapter
        for adapter in app_config.sources.adapters
        if adapter.enabled and adapter.name in EXTERNAL_ADAPTER_NAMES
    ]
    if not enabled:
        logger.info("runner: no enabled external adapters")
        return RunReport(0, 0, 0)

    adapters: list[SourceAdapter] = []
    for entry in enabled:
        builder = ADAPTER_BUILDERS.get(entry.name)
        if builder is None:
            logger.warning("runner: no builder for %s", entry.name)
            continue
        adapters.append(builder(entry.model_dump()))

    since_dt = since or (datetime.now(timezone.utc) - timedelta(hours=DEFAULT_LOOKBACK_HOURS))
    return await run_adapters(
        adapters, since=since_dt, db_path=db_path, viral_threshold=viral_threshold
    )


async def run_adapters(
    adapters: list[SourceAdapter],
    since: datetime,
    db_path: Path | None = None,
    viral_threshold: float = 500.0,
) -> RunReport:
    """Concurrent driver — testable without yaml/config dependency."""
    if not adapters:
        return RunReport(0, 0, 0)

    results = await asyncio.gather(
        *(_fetch_with_health(a, since) for a in adapters),
        return_exceptions=False,  # _fetch_with_health swallows exceptions itself
    )

    successes = 0
    errors = 0
    inserted = 0
    per_source: dict[str, tuple[int, int]] = {}  # name -> (fetched, inserted)
    for adapter, outcome in zip(adapters, results):
        observations, ok, err_msg = outcome
        if ok:
            successes += 1
        else:
            errors += 1

        try:
            _write_source_health(adapter.name, ok=ok, err_msg=err_msg, db_path=db_path)
        except sqlite3.Error as exc:
            logger.warning("source_health write failed for %s: %s", adapter.name, exc)

        fetched = len(observations) if observations else 0
        local_inserted = 0
        if observations:
            try:
                local_inserted = _insert_observations(
                    observations, viral_threshold=viral_threshold, db_path=db_path
                )
                inserted += local_inserted
            except sqlite3.Error as exc:
                logger.error("insert failed for %s: %s", adapter.name, exc)
        per_source[adapter.name] = (fetched, local_inserted)

    per_source_str = ", ".join(
        f"{name}=({f}/{i})" for name, (f, i) in per_source.items()
    )
    logger.info(
        "runner done: success=%d error=%d inserted=%d | per_source(fetched/inserted): %s",
        successes,
        errors,
        inserted,
        per_source_str,
    )
    return RunReport(successes, errors, inserted)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _fetch_with_health(
    adapter: SourceAdapter,
    since: datetime | None = None,
) -> tuple[list[Observation], bool, str | None]:
    """Call ``adapter.fetch_latest`` and never let it raise."""
    cutoff = since or (datetime.now(timezone.utc) - timedelta(hours=DEFAULT_LOOKBACK_HOURS))
    try:
        observations = await adapter.fetch_latest(cutoff)
        return observations, True, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s fetch_latest raised: %s", adapter.name, exc)
        return [], False, str(exc)[:500]


def _write_source_health(
    adapter_name: str,
    ok: bool,
    err_msg: str | None,
    db_path: Path | None,
) -> None:
    from src.database import get_conn, with_retry

    def _do_write() -> None:
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO source_health (adapter_name, last_success_at, "
                    "consecutive_failures, last_error) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(adapter_name) DO UPDATE SET "
                    "last_success_at = CASE WHEN excluded.last_success_at IS NOT NULL "
                    "  THEN excluded.last_success_at ELSE source_health.last_success_at END, "
                    "consecutive_failures = CASE WHEN ? = 1 THEN 0 "
                    "  ELSE source_health.consecutive_failures + 1 END, "
                    "last_error = excluded.last_error",
                    (
                        adapter_name,
                        datetime.now(timezone.utc).isoformat() if ok else None,
                        0 if ok else 1,
                        err_msg,
                        1 if ok else 0,
                    ),
                )
        finally:
            conn.close()

    with_retry(_do_write)


def _insert_observations(
    observations: list[Observation],
    viral_threshold: float,
    db_path: Path | None,
) -> int:
    from src.database import get_conn, with_retry

    def _do_insert() -> int:
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            with conn:
                count = 0
                for obs in observations:
                    # Hard requirement: no image, no ingest. Twitter engagement
                    # heavily favors image posts; learning from text-only posts
                    # would teach a style that under-performs on X.
                    if not obs.has_image:
                        continue
                    row = to_db_row(obs)
                    score = placeholder_viral_score(obs)
                    is_viral = 1 if score >= viral_threshold else 0
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO reaction_observations ("
                        "source, author_handle, author_tier, content, posted_at, "
                        "likes, retweets, replies, impressions, has_image, raw_url, "
                        "topic_hint, image_url, content_length, viral_score, is_viral) "
                        "VALUES (:source, :author_handle, :author_tier, :content, "
                        ":posted_at, :likes, :retweets, :replies, :impressions, "
                        ":has_image, :raw_url, :topic_hint, :image_url, "
                        ":content_length, :viral_score, :is_viral)",
                        {**row, "viral_score": score, "is_viral": is_viral},
                    )
                    count += cur.rowcount if cur.rowcount > 0 else 0
                return count
        finally:
            conn.close()

    return with_retry(_do_insert)


__all__ = [
    "RunReport",
    "EXTERNAL_ADAPTER_NAMES",
    "placeholder_viral_score",
    "run_adapters",
    "run_once",
]


# Allow `python -m observers.runner` smoke runs.
async def _main_smoke() -> None:
    report = await run_once()
    print(report.as_tuple())


def _cli_entry() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main_smoke())


if __name__ == "__main__":  # pragma: no cover
    _cli_entry()
