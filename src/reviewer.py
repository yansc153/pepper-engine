"""S10 Reviewer — nightly retrospective + learning loop.

Pulls real metrics for recently published drafts, classifies them via the §16.4
four-case state machine, updates ``strategy_weights`` per ``topic_lane``, and
distils chronically under-performing hook patterns into ``slop_words`` (plus
rendering ``voice/slop_words.md``). Also flags ``approved`` drafts that the
human never published within 7 days (§16.13 manual mode).

This module only orchestrates — actual EMA / cooling logic lives in
``src.miner.feedback`` and the viral score formula in
``src.miner.viral_scorer``. We never touch S6/S7/S13 internals.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from src.database import get_conn, with_retry
from src.miner.feedback import apply_post_outcome
from src.miner.viral_scorer import viral_score

logger = logging.getLogger(__name__)

__all__ = [
    "review_and_update_weights",
    "ReviewReport",
    "MIN_SAMPLE_FOR_WEIGHT_UPDATE",
    "MAX_WEIGHT_STEP_PER_REVIEW",
    "RECENT_DRAFT_LIMIT",
    "STALE_DRAFT_DAYS",
    "SLOP_PATTERN_MIN_SAMPLE",
    "SLOP_PATTERN_BOTTOM_PERCENTILE",
    "SLOP_WORDS_MD_PATH",
]

# Algorithm constants (Appendix B + §16.4 + §16.13)
MIN_SAMPLE_FOR_WEIGHT_UPDATE: int = 5
MAX_WEIGHT_STEP_PER_REVIEW: float = 0.05
RECENT_DRAFT_LIMIT: int = 30
STALE_DRAFT_DAYS: int = 7
SLOP_PATTERN_MIN_SAMPLE: int = 10
SLOP_PATTERN_BOTTOM_PERCENTILE: float = 0.20

SLOP_WORDS_MD_PATH = (
    Path(__file__).resolve().parent.parent / "voice" / "slop_words.md"
)

# Quantile helpers ----------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. ``pct`` in [0,1]. Empty list returns 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if pct <= 0:
        return ordered[0]
    if pct >= 1:
        return ordered[-1]
    index = max(0, int(math.ceil(pct * len(ordered))) - 1)
    return ordered[index]


# Alerting stub -------------------------------------------------------------


def _alert(message: str) -> None:
    """Webhook alert stub. Real implementation lands in src/alerting.py later."""
    try:
        from src import alerting  # type: ignore

        alerting.alert(message)
    except (ImportError, AttributeError):
        logger.warning("[reviewer-alert] %s", message)


# Report dataclass ----------------------------------------------------------


@dataclass
class ReviewReport:
    posts_reviewed: int = 0
    metrics_collected: int = 0
    weights_updated: list[str] = field(default_factory=list)
    slop_words_added: list[str] = field(default_factory=list)
    stale_drafts_alerted: list[int] = field(default_factory=list)
    duration_seconds: float = 0.0


# Step 1 — metrics fetch ---------------------------------------------------


async def _fetch_recent_published(
    conn,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, content, topic_lane, persona, pattern_ids, tweet_url, "
        "posted_at, status FROM drafts "
        "WHERE status IN ('published', 'metrics_collected', 'learned') "
        "AND tweet_url IS NOT NULL "
        "ORDER BY COALESCE(posted_at, generated_at) DESC "
        "LIMIT ?",
        (RECENT_DRAFT_LIMIT,),
    ).fetchall()
    return [dict(r) for r in rows]


async def _collect_metrics(
    drafts: list[dict[str, Any]],
    metrics_fetcher: Callable[[str], Awaitable[dict[str, int]]],
) -> dict[int, dict[str, Any]]:
    """Returns ``{draft_id: {likes, retweets, replies, impressions, viral_score}}``."""
    results: dict[int, dict[str, Any]] = {}
    for draft in drafts:
        url = draft["tweet_url"]
        try:
            metrics = await metrics_fetcher(url)
        except Exception as exc:  # noqa: BLE001 — never let one URL kill the run
            logger.warning("metrics fetch failed for %s: %s", url, exc)
            _bump_source_health("publisher.get_post_metrics", str(exc))
            continue
        if not metrics:
            continue
        likes = int(metrics.get("likes", 0))
        retweets = int(metrics.get("retweets", 0))
        replies = int(metrics.get("replies", 0))
        impressions_raw = metrics.get("impressions")
        impressions = int(impressions_raw) if impressions_raw else None
        score = viral_score(likes, retweets, replies, impressions)
        results[int(draft["id"])] = {
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "impressions": impressions,
            "viral_score": score,
        }
    return results


def _bump_source_health(adapter: str, error: str) -> None:
    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO source_health (adapter_name, consecutive_failures, "
                    "last_error) VALUES (?, 1, ?) "
                    "ON CONFLICT(adapter_name) DO UPDATE SET "
                    "consecutive_failures = source_health.consecutive_failures + 1, "
                    "last_error = excluded.last_error",
                    (adapter, error[:500]),
                )
        finally:
            conn.close()

    try:
        with_retry(_write)
    except Exception as exc:  # noqa: BLE001
        logger.warning("source_health bump failed: %s", exc)


# Step 2 — write metrics timeseries ----------------------------------------


def _write_metrics_timeseries(
    metrics_by_draft: dict[int, dict[str, Any]],
) -> tuple[int, set[int]]:
    """Write timeseries rows; return ``(written_count, written_draft_ids)``.

    Only drafts with a matching ``posts`` row get a timeseries row written.
    Caller MUST use the returned ``written_draft_ids`` (not the input keys)
    to advance status — otherwise drafts without posts get falsely promoted
    to ``metrics_collected``/``learned`` while their timeseries stays empty.
    (Round 2 fix: prior version used input keys → silent state drift.)
    """
    if not metrics_by_draft:
        return 0, set()

    written = 0
    written_ids: set[int] = set()

    def _write() -> None:
        nonlocal written
        conn = get_conn()
        try:
            with conn:
                for draft_id, m in metrics_by_draft.items():
                    row = conn.execute(
                        "SELECT p.id FROM posts p "
                        "JOIN drafts d ON d.tweet_url = p.tweet_url "
                        "WHERE d.id = ? LIMIT 1",
                        (draft_id,),
                    ).fetchone()
                    if row is None:
                        logger.warning(
                            "draft %s has tweet_url but no posts row — "
                            "binding step skipped INSERT; skipping metrics + "
                            "status will NOT advance",
                            draft_id,
                        )
                        continue
                    post_id = int(row["id"])
                    conn.execute(
                        "INSERT OR REPLACE INTO post_metrics_timeseries "
                        "(post_id, collected_at, likes, retweets, replies, "
                        "impressions, viral_score) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            post_id,
                            datetime.now(timezone.utc).isoformat(),
                            m["likes"],
                            m["retweets"],
                            m["replies"],
                            m["impressions"],
                            m["viral_score"],
                        ),
                    )
                    written += 1
                    written_ids.add(int(draft_id))
        finally:
            conn.close()

    with_retry(_write)

    if written_ids:
        _advance_draft_status(list(written_ids), "published", "metrics_collected")

    return written, written_ids


def _advance_draft_status(
    draft_ids: list[int], from_status: str, to_status: str
) -> None:
    """Batch-advance draft statuses; skips rows already past the target state."""
    if not draft_ids:
        return

    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                for did in draft_ids:
                    conn.execute(
                        "UPDATE drafts SET status=? WHERE id=? AND status=?",
                        (to_status, did, from_status),
                    )
        finally:
            conn.close()

    with_retry(_write)


# Step 3 — 4-case feedback classification ----------------------------------


def _classify_and_dispatch(
    metrics_by_draft: dict[int, dict[str, Any]],
) -> tuple[float, float]:
    """Compute p30/p70 across the batch, call apply_post_outcome for each draft.

    Returns ``(p30, p70)`` for downstream logging.
    """
    scores = [m["viral_score"] for m in metrics_by_draft.values()]
    p30 = _percentile(scores, 0.30)
    p70 = _percentile(scores, 0.70)

    for draft_id, m in metrics_by_draft.items():
        score = m["viral_score"]
        if score >= p70 and score > 0:
            outcome = "top"
        elif score <= p30:
            outcome = "bottom"
        else:
            outcome = "mid"
        try:
            apply_post_outcome(draft_id, outcome)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            logger.warning("apply_post_outcome failed draft=%s: %s", draft_id, exc)

    return p30, p70


# Step 4 — strategy_weights update -----------------------------------------


def _aggregate_by_lane(
    conn, window_days: int
) -> dict[str, tuple[float, int]]:
    """Return ``{topic_lane: (avg_viral_score, sample_size)}`` for the window."""
    rows = conn.execute(
        "SELECT d.topic_lane AS lane, AVG(t.viral_score) AS avg_s, "
        "COUNT(*) AS n "
        "FROM drafts d "
        "JOIN posts p ON p.tweet_url = d.tweet_url "
        "JOIN post_metrics_timeseries t ON t.post_id = p.id "
        "WHERE d.status IN ('published', 'metrics_collected', 'learned') "
        "AND t.collected_at >= datetime('now', ?) "
        "GROUP BY d.topic_lane",
        (f"-{int(window_days)} days",),
    ).fetchall()
    return {r["lane"]: (float(r["avg_s"] or 0.0), int(r["n"])) for r in rows}


def _current_weights(conn) -> dict[str, float]:
    rows = conn.execute(
        "SELECT topic_lane, weight FROM strategy_weights"
    ).fetchall()
    return {r["topic_lane"]: float(r["weight"]) for r in rows}


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, w) for w in weights.values())
    if total <= 0:
        return weights
    return {k: max(0.0, v) / total for k, v in weights.items()}


def _update_strategy_weights(window_days: int) -> tuple[list[str], dict[str, float], dict[str, float]]:
    """Bump lane weights up/down by ``MAX_WEIGHT_STEP_PER_REVIEW``.

    Returns ``(updated_lanes, before_snapshot, after_snapshot)``.
    """
    conn = get_conn()
    try:
        lane_stats = _aggregate_by_lane(conn, window_days)
        before = _current_weights(conn)
    finally:
        conn.close()

    eligible = {
        lane: avg
        for lane, (avg, n) in lane_stats.items()
        if n >= MIN_SAMPLE_FOR_WEIGHT_UPDATE
    }
    if not eligible:
        return [], before, before

    sorted_lanes = sorted(eligible.items(), key=lambda kv: kv[1], reverse=True)
    top_cut = max(1, len(sorted_lanes) // 3)
    bottom_cut = max(1, len(sorted_lanes) // 3)
    winners = {lane for lane, _ in sorted_lanes[:top_cut]}
    losers = {lane for lane, _ in sorted_lanes[-bottom_cut:]}
    winners -= losers  # tie-break: tiny batch (1 lane) collapses → no movement

    after = dict(before)
    updated: list[str] = []
    for lane in eligible:
        prev = after.get(lane, 1.0 / max(1, len(eligible)))
        if lane in winners:
            after[lane] = prev + MAX_WEIGHT_STEP_PER_REVIEW
            updated.append(lane)
        elif lane in losers:
            after[lane] = max(0.0, prev - MAX_WEIGHT_STEP_PER_REVIEW)
            updated.append(lane)
        else:
            after[lane] = prev

    after = _normalize(after)
    if not updated:
        return [], before, before

    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                for lane, w in after.items():
                    conn.execute(
                        "INSERT INTO strategy_weights (topic_lane, weight, reason) "
                        "VALUES (?, ?, 'reviewer_auto') "
                        "ON CONFLICT(topic_lane) DO UPDATE SET "
                        "weight = excluded.weight, "
                        "reason = excluded.reason, "
                        "updated_at = CURRENT_TIMESTAMP",
                        (lane, w),
                    )
                conn.execute(
                    "INSERT INTO learning_log (window_days, winning_patterns, "
                    "losing_patterns, weights_before, weights_after, sample_size) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        window_days,
                        json.dumps(sorted(winners)),
                        json.dumps(sorted(losers)),
                        json.dumps(before),
                        json.dumps(after),
                        sum(n for _, n in lane_stats.values()),
                    ),
                )
        finally:
            conn.close()

    with_retry(_write)
    return updated, before, after


# Step 5 — channel B slop pattern distillation -----------------------------


def _pattern_success_stats(
    conn,
) -> list[tuple[str, float, int]]:
    """Return ``[(hook_pattern, avg_success_score, sample_size)]``."""
    rows = conn.execute(
        "SELECT hook_pattern, AVG(success_score) AS s, "
        "SUM(times_used_in_post) AS used "
        "FROM technique_entries "
        "GROUP BY hook_pattern"
    ).fetchall()
    return [
        (r["hook_pattern"], float(r["s"] or 0.0), int(r["used"] or 0))
        for r in rows
    ]


def _persist_slop_patterns(slop_patterns: list[str]) -> None:
    if not slop_patterns:
        return

    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                for pattern in slop_patterns:
                    conn.execute(
                        "INSERT OR IGNORE INTO slop_words (word, category, source) "
                        "VALUES (?, 'slop_pattern', 'reviewer_auto')",
                        (pattern,),
                    )
        finally:
            conn.close()

    with_retry(_write)


def _render_slop_words_md(all_patterns: Iterable[str]) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# Auto-generated by reviewer (DO NOT EDIT)",
        f"> Last updated: {ts}",
        "> Source: `src/reviewer.review_and_update_weights()`",
        "> Manual edits will be overwritten on the next nightly run.",
        "",
    ]
    sorted_patterns = sorted(set(all_patterns))
    if not sorted_patterns:
        lines.append("_No slop patterns flagged yet._")
    else:
        for p in sorted_patterns:
            lines.append(f"- {p}")
    SLOP_WORDS_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    SLOP_WORDS_MD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _distill_slop_patterns() -> list[str]:
    """Channel B: chronic low-success hook patterns → slop_words table + md."""
    conn = get_conn()
    try:
        stats = _pattern_success_stats(conn)
    finally:
        conn.close()

    eligible = [(p, s, n) for (p, s, n) in stats if n >= SLOP_PATTERN_MIN_SAMPLE]
    new_slops: list[str] = []
    if eligible:
        scores = [s for (_, s, _) in eligible]
        cutoff = _percentile(scores, SLOP_PATTERN_BOTTOM_PERCENTILE)
        new_slops = [p for (p, s, _) in eligible if s <= cutoff]
        if new_slops:
            _persist_slop_patterns(new_slops)

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT word FROM slop_words WHERE category = 'slop_pattern'"
        ).fetchall()
    finally:
        conn.close()
    _render_slop_words_md(r["word"] for r in rows)
    return new_slops


# Step 6 — stale approved drafts (§16.13) ----------------------------------


def _flag_stale_drafts() -> list[int]:
    """Find ``approved`` drafts older than STALE_DRAFT_DAYS; emit webhook alerts.

    NOTE: drafts.status CHECK constraint excludes 'stale', so we don't mutate
    status — we only alert and surface the ids. Owner of the schema (S1) can
    later add a 'stale' enum and we'll start writing it.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, discord_reacted_at FROM drafts "
            "WHERE status = 'approved' "
            "AND discord_reacted_at IS NOT NULL "
            "AND discord_reacted_at < datetime('now', ?)",
            (f"-{STALE_DRAFT_DAYS} days",),
        ).fetchall()
    finally:
        conn.close()
    stale_ids = [int(r["id"]) for r in rows]
    for did in stale_ids:
        _alert(
            f"Draft #{did} has been approved but never posted for "
            f"{STALE_DRAFT_DAYS}+ days"
        )
    return stale_ids


# Main orchestrator --------------------------------------------------------


async def _default_metrics_fetcher(tweet_url: str) -> dict[str, int]:
    # Lazy import so unit tests can mock at the reviewer boundary without
    # forcing publisher (Playwright) to import.
    from src.publisher import get_post_metrics

    return await get_post_metrics(tweet_url)


async def review_and_update_weights(
    window_days: int = 7,
    *,
    metrics_fetcher: Callable[[str], Awaitable[dict[str, int]]] | None = None,
) -> ReviewReport:
    """Nightly review entry point.

    ``metrics_fetcher`` is injectable so tests don't have to monkeypatch the
    publisher module.
    """
    start = time.monotonic()
    fetcher = metrics_fetcher or _default_metrics_fetcher

    conn = get_conn()
    try:
        drafts = await _fetch_recent_published(conn)
    finally:
        conn.close()

    metrics_by_draft = await _collect_metrics(drafts, fetcher)
    metrics_collected, written_ids = _write_metrics_timeseries(metrics_by_draft)
    # Only classify drafts whose metrics were actually persisted — drafts with
    # no posts row produce no timeseries and must not be marked 'learned'.
    classifiable = {did: m for did, m in metrics_by_draft.items() if did in written_ids}
    p30, p70 = _classify_and_dispatch(classifiable)
    logger.info(
        "reviewer classify: n_attempted=%d n_persisted=%d p30=%.2f p70=%.2f",
        len(metrics_by_draft),
        len(classifiable),
        p30,
        p70,
    )
    # Advance status: metrics_collected → learned (only those that actually wrote)
    if written_ids:
        _advance_draft_status(list(written_ids), "metrics_collected", "learned")

    updated, before, after = _update_strategy_weights(window_days)
    slop_added = _distill_slop_patterns()
    stale_ids = _flag_stale_drafts()

    return ReviewReport(
        posts_reviewed=len(drafts),
        metrics_collected=metrics_collected,
        weights_updated=updated,
        slop_words_added=slop_added,
        stale_drafts_alerted=stale_ids,
        duration_seconds=round(time.monotonic() - start, 3),
    )
