"""Frozen contracts for the observer layer.

This module is the single source of truth for the data crossing the boundary
between scraping adapters (S5) and the pattern miner / database (S1, S6).
Every other module imports from here; nothing here may import from sibling
observer modules to keep the dependency graph acyclic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, get_args, runtime_checkable

__all__ = [
    "AuthorTier",
    "SourceName",
    "TopicLane",
    "ContentMode",
    "OptimalLength",
    "Observation",
    "SourceAdapter",
    "ObservationValidationError",
    "from_scrape_dict",
    "to_db_row",
    "from_db_row",
]

# tier 0 = news_flash (facts only, never enters learning corpus).
# tier 1/2/3 = KOL layers: top / mid / general.
AuthorTier = Literal[0, 1, 2, 3]

SourceName = Literal[
    "x_list_finance",
    "x_list_general",
    "xueqiu",
    "futu",
    "news_flash",
    "self_monitor",
    "eastmoney_guba",
]

TopicLane = Literal[
    "pre_market",
    "intraday",
    "post_market",
    "overnight",
    "general_tech_ai",
    "general_meme_career",
    "other",
]

ContentMode = Literal["insight", "meme", "emotional"]

OptimalLength = Literal["short", "medium", "long", "article"]

_VALID_SOURCES: frozenset[str] = frozenset(get_args(SourceName))
_VALID_TIERS: frozenset[int] = frozenset(get_args(AuthorTier))
_VALID_LANES: frozenset[str] = frozenset(get_args(TopicLane))


class ObservationValidationError(ValueError):
    """Raised when an external payload cannot be coerced into an Observation."""


@dataclass(frozen=True, slots=True)
class Observation:
    """A single scraped post normalised across all sources."""

    source: SourceName
    author_handle: str               # 不带 @
    author_tier: AuthorTier
    content: str
    posted_at: datetime              # UTC, tz-aware
    likes: int
    retweets: int
    replies: int
    impressions: int | None          # X List 抓不到时为 None
    has_image: bool
    raw_url: str
    topic_hint: TopicLane | None
    image_url: str | None = None      # source post's image URL, downloaded later

    def __post_init__(self) -> None:
        if self.source not in _VALID_SOURCES:
            raise ObservationValidationError(f"unknown source: {self.source!r}")
        if self.author_tier not in _VALID_TIERS:
            raise ObservationValidationError(
                f"author_tier must be one of {sorted(_VALID_TIERS)}, got {self.author_tier!r}"
            )
        if self.topic_hint is not None and self.topic_hint not in _VALID_LANES:
            raise ObservationValidationError(f"unknown topic_hint: {self.topic_hint!r}")
        if self.posted_at.tzinfo is None:
            raise ObservationValidationError("posted_at must be timezone-aware (UTC)")


@runtime_checkable
class SourceAdapter(Protocol):
    """Every scraping adapter implements this surface.

    Adapters are async to keep the cron driver non-blocking; `fetch_latest`
    returns observations strictly newer than `since` (exclusive).
    """

    name: SourceName
    cookie_env_key: str
    rate_limit_per_hour: int

    async def fetch_latest(self, since: datetime) -> list[Observation]: ...

    async def health_check(self) -> bool: ...


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

# Aliases accepted from twitter_bot.scrape_list_by_url and other scrapers.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "author_handle": ("author_handle", "author", "handle", "username", "screen_name"),
    "content": ("content", "text", "body"),
    "posted_at": ("posted_at", "created_at", "timestamp", "time"),
    "likes": ("likes", "favorite_count", "like_count"),
    "retweets": ("retweets", "retweet_count", "reposts"),
    "replies": ("replies", "reply_count", "comments"),
    "impressions": ("impressions", "view_count", "views"),
    "has_image": ("has_image", "has_media", "with_image"),
    "raw_url": ("raw_url", "url", "tweet_url", "link"),
    "topic_hint": ("topic_hint", "topic", "lane"),
    "image_url": ("image_url", "image", "pic_url", "first_pic", "media_url"),
}


def _pick(d: dict[str, Any], key: str, default: Any = None) -> Any:
    for alias in _FIELD_ALIASES[key]:
        if alias in d and d[alias] is not None:
            return d[alias]
    return default


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        # accept both "2026-05-17T10:00:00Z" and "2026-05-17T10:00:00+00:00"
        normalised = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError as exc:
            raise ObservationValidationError(f"unparseable datetime: {value!r}") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise ObservationValidationError(f"unsupported datetime type: {type(value).__name__}")


def _strip_at(handle: str) -> str:
    return handle.lstrip("@").strip()


def from_scrape_dict(d: dict[str, Any], source: SourceName, tier: AuthorTier) -> Observation:
    """Normalise a raw scraper payload into an Observation.

    Missing engagement counters default to 0; missing impressions stays None.
    Raises ObservationValidationError on unknown source/tier or bad timestamps.
    """
    if source not in _VALID_SOURCES:
        raise ObservationValidationError(f"unknown source: {source!r}")
    if tier not in _VALID_TIERS:
        raise ObservationValidationError(f"unknown tier: {tier!r}")

    raw_handle = _pick(d, "author_handle", default="")
    content = _pick(d, "content", default="")
    raw_url = _pick(d, "raw_url", default="")
    posted_raw = _pick(d, "posted_at")
    if posted_raw is None:
        raise ObservationValidationError("posted_at is required")

    return Observation(
        source=source,
        author_handle=_strip_at(str(raw_handle)),
        author_tier=tier,
        content=str(content),
        posted_at=_coerce_datetime(posted_raw),
        likes=int(_pick(d, "likes", 0) or 0),
        retweets=int(_pick(d, "retweets", 0) or 0),
        replies=int(_pick(d, "replies", 0) or 0),
        impressions=(
            int(_pick(d, "impressions")) if _pick(d, "impressions") is not None else None
        ),
        has_image=bool(_pick(d, "has_image", False)),
        raw_url=str(raw_url),
        topic_hint=_pick(d, "topic_hint"),
        image_url=(str(_pick(d, "image_url")) if _pick(d, "image_url") else None),
    )


def to_db_row(obs: Observation) -> dict[str, Any]:
    """Serialise an Observation for a `reaction_observations` INSERT.

    - posted_at -> ISO 8601 string (UTC)
    - has_image -> 0/1 int (SQLite has no bool)
    - topic_hint -> str | None passthrough
    """
    return {
        "source": obs.source,
        "author_handle": obs.author_handle,
        "author_tier": int(obs.author_tier),
        "content": obs.content,
        "posted_at": obs.posted_at.astimezone(timezone.utc).isoformat(),
        "likes": obs.likes,
        "retweets": obs.retweets,
        "replies": obs.replies,
        "impressions": obs.impressions,
        "has_image": 1 if obs.has_image else 0,
        "raw_url": obs.raw_url,
        "topic_hint": obs.topic_hint,
        "image_url": obs.image_url,
        "content_length": len(obs.content or ""),
    }


def from_db_row(row: dict[str, Any]) -> Observation:
    """Reverse of `to_db_row`; tolerates sqlite3.Row-style mappings."""
    try:
        return Observation(
            source=row["source"],
            author_handle=row["author_handle"],
            author_tier=int(row["author_tier"]),  # type: ignore[arg-type]
            content=row["content"],
            posted_at=_coerce_datetime(row["posted_at"]),
            likes=int(row["likes"]),
            retweets=int(row["retweets"]),
            replies=int(row["replies"]),
            impressions=int(row["impressions"]) if row["impressions"] is not None else None,
            has_image=bool(row["has_image"]),
            raw_url=row["raw_url"],
            topic_hint=row.get("topic_hint") if hasattr(row, "get") else row["topic_hint"],
            image_url=(
                row["image_url"]
                if (hasattr(row, "keys") and "image_url" in row.keys())
                else None
            ),
        )
    except KeyError as exc:
        raise ObservationValidationError(f"missing column in db row: {exc.args[0]}") from exc


# Silence unused-import linters in downstream stubs that only need the names.
_ = (field, fields, json)
