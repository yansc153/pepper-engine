"""Xueqiu hot-feed adapter.

Pulls the public hot timeline JSON via HTTP using cookies from
``secrets/xueqiu_cookies.json`` (Playwright-format list). Returns
``Observation`` instances ready for INSERT into ``reaction_observations``.

Failures are swallowed and surfaced through ``source_health``. Designed to be
mocked with ``respx`` in tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from observers.base import (
    Observation,
    ObservationValidationError,
    from_scrape_dict,
)

logger = logging.getLogger(__name__)

XUEQIU_FEED_URL = (
    # Step 1: headline list. Each item carries only a topic-card stub with a
    # `target` link pointing to the actual article. Step 2 below hydrates each
    # via show.json to pull the real body+image.
    "https://xueqiu.com/v4/statuses/public_timeline_by_category.json"
    "?since_id=-1&max_id=-1&count=20&category=-1&type=11"
)
XUEQIU_SHOW_URL = "https://xueqiu.com/v4/statuses/show.json"

# Filter threshold: skip xueqiu items shorter than this — we only want long-form
# columns/articles as rewritable source material, not short status updates.
MIN_CONTENT_LENGTH = 250  # ~250 Chinese chars ≈ 750 bytes; enough for "column-grade" posts

# Two-stage fetch tuning
HYDRATION_CONCURRENCY = 5      # concurrent show.json calls
HYDRATION_TIMEOUT = 10.0       # per-call timeout (sec)
_TARGET_ID_RE = re.compile(r"/(\d+)/?$")  # extract trailing status_id from target
_HTML_TAG_RE = re.compile(r"<[^>]+>")     # strip HTML tags from show.json body
_HTML_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
XUEQIU_HOME_URL = "https://xueqiu.com/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


def _unwrap_status(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the real status object from a public_timeline_by_category wrapper.

    Priority:
      1. `original_status` — the canonical un-reposted status (preferred)
      2. `data` (dict or JSON-string) — fallback; usually a topic-card shape
      3. raw — already unwrapped
    """
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("original_status"), dict) and raw["original_status"]:
        return raw["original_status"]
    data = raw.get("data")
    if isinstance(data, str) and data.strip().startswith("{"):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            pass
    if isinstance(data, dict) and data:
        return data
    return raw


class XueqiuAdapter:
    """Adapter for Xueqiu's hot-topic feed.

    Implements ``observers.base.SourceAdapter``.
    """

    name = "xueqiu"
    cookie_env_key = "XUEQIU_COOKIE_FILE"
    rate_limit_per_hour = 24

    def __init__(
        self,
        feed_url: str = XUEQIU_FEED_URL,
        tier_default: int = 0,  # tier=0: contributes topic candidates only, NOT learned
        max_posts_per_fetch: int = 30,
        request_timeout: float = 15.0,
    ) -> None:
        self._feed_url = feed_url
        self._tier_default = tier_default
        self._max_posts = max_posts_per_fetch
        self._timeout = request_timeout

    # ------------------------------------------------------------------
    # SourceAdapter surface
    # ------------------------------------------------------------------

    async def fetch_latest(self, since: datetime) -> list[Observation]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        try:
            payload = await self._fetch_payload()
        except Exception as exc:  # noqa: BLE001 — adapter must not raise
            logger.warning("xueqiu fetch failed: %s", exc)
            return []
        items = payload.get("list") or payload.get("statuses") or []
        if items:
            first = items[0]
            # public_timeline_by_category wraps the real status; unwrap it
            unwrapped = _unwrap_status(first)
            logger.info(
                "xueqiu item wrapped_keys=%s | unwrapped_keys=%s user=%s text_snip=%s",
                list(first.keys()),
                list(unwrapped.keys())[:15] if unwrapped else [],
                (unwrapped.get("user", {}).get("screen_name") if unwrapped else None),
                str(unwrapped.get("text", unwrapped.get("description", "")))[:80] if unwrapped else "",
            )
        return self._parse_payload(payload, since)

    async def health_check(self) -> bool:
        try:
            cookies = self._load_cookies()
        except FileNotFoundError:
            return False
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    XUEQIU_HOME_URL,
                    cookies=cookies,
                    headers=self._headers(),
                )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _cookie_file(self) -> Path:
        path = os.environ.get(self.cookie_env_key, "")
        if not path:
            raise FileNotFoundError(f"env {self.cookie_env_key} is unset")
        return Path(path)

    def _load_cookies(self) -> dict[str, str]:
        path = self._cookie_file()
        if not path.exists():
            raise FileNotFoundError(f"cookie file missing: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": XUEQIU_HOME_URL,
            "Accept": "application/json",
        }

    async def _fetch_payload(self) -> dict[str, Any]:
        """Two-stage fetch: headlines list → hydrate each via show.json.

        The headline endpoint returns topic-card stubs (title+target+pic
        only — no body, no real user). show.json returns the full status
        object with text body, image, author, engagement counters. We
        parallelize the hydration calls under a semaphore.
        """
        cookies = self._load_cookies()
        async with httpx.AsyncClient(
            timeout=self._timeout, cookies=cookies, headers=self._headers()
        ) as client:
            # Stage 1: headline list
            resp = await client.get(self._feed_url)
            resp.raise_for_status()
            headlines = resp.json()
            items = headlines.get("list") or headlines.get("statuses") or []

            # Extract status_ids from each item's target ("/user_id/status_id")
            status_ids: list[int] = []
            for item in items[: self._max_posts]:
                unwrapped = _unwrap_status(item)
                target = unwrapped.get("target") or item.get("target") or ""
                m = _TARGET_ID_RE.search(target)
                if m:
                    status_ids.append(int(m.group(1)))

            if not status_ids:
                logger.info("xueqiu headlines returned 0 valid targets")
                return {"list": []}

            # Stage 2: hydrate each via show.json (concurrent, bounded)
            sem = asyncio.Semaphore(HYDRATION_CONCURRENCY)

            async def _fetch_one(sid: int) -> dict[str, Any] | None:
                async with sem:
                    try:
                        r = await client.get(
                            XUEQIU_SHOW_URL,
                            params={"id": sid},
                            timeout=HYDRATION_TIMEOUT,
                        )
                        r.raise_for_status()
                        return r.json()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("xueqiu show id=%s failed: %s", sid, exc)
                        return None

            results = await asyncio.gather(*[_fetch_one(s) for s in status_ids])
            articles = [a for a in results if a]
            logger.info(
                "xueqiu hydrated %d/%d articles from headlines",
                len(articles), len(status_ids),
            )
            return {"list": articles}

    def _parse_payload(
        self, payload: dict[str, Any], since: datetime
    ) -> list[Observation]:
        items = payload.get("list") or payload.get("statuses") or []
        out: list[Observation] = []
        for raw in items[: self._max_posts]:
            unwrapped = _unwrap_status(raw)
            try:
                obs = self._row_to_observation(unwrapped)
            except ObservationValidationError as exc:
                logger.debug("xueqiu skip row: %s", exc)
                continue
            if obs.posted_at <= since:
                continue
            out.append(obs)
        return out

    def _row_to_observation(self, raw: dict[str, Any]) -> Observation:
        user = raw.get("user") or {}
        # public_timeline_by_category headline items don't carry user;
        # fall back to "xueqiu_topic" as the handle so we still capture them.
        handle = (user.get("screen_name") or "").strip() or "xueqiu_topic"

        target = raw.get("target") or ""
        if not target:
            raise ObservationValidationError("missing target url")
        url = target if target.startswith("http") else f"https://xueqiu.com{target}"
        pic = raw.get("pic_sizes") or raw.get("pic") or raw.get("first_pic") or ""
        has_image = bool(pic) and pic != ""

        # Content from show.json comes as HTML (`<p>...</p><img...><p>...`).
        # Strip tags for the body we store; extract first <img src=...> for image.
        raw_text = (
            raw.get("text")
            or raw.get("description")
            or raw.get("title")
            or raw.get("topic_desc")
            or raw.get("topic_title")
            or ""
        )
        # Pull first image from inline HTML BEFORE stripping tags
        inline_img_match = _HTML_IMG_RE.search(raw_text) if raw_text else None
        inline_img = inline_img_match.group(1) if inline_img_match else ""
        content = _HTML_TAG_RE.sub(" ", raw_text).strip()
        content = re.sub(r"\s+", " ", content)
        if not content:
            raise ObservationValidationError("empty content")
        if len(content) < MIN_CONTENT_LENGTH:
            raise ObservationValidationError(
                f"too short ({len(content)} < {MIN_CONTENT_LENGTH}) — only long-form columns are usable"
            )

        # Image priority: first_pic field → pic field → first <img> in HTML body
        first_pic = raw.get("first_pic") or raw.get("pic") or inline_img or ""
        image_url_str: str | None = (
            first_pic if first_pic and first_pic.startswith("http") else None
        )
        if not image_url_str:
            raise ObservationValidationError("no image — only image-bearing posts are usable")

        # xueqiu created_at is epoch milliseconds; topic items may lack it
        created_raw = raw.get("created_at") or raw.get("timeBefore")
        if isinstance(created_raw, (int, float)) and created_raw > 1e12:
            posted_at = datetime.fromtimestamp(created_raw / 1000, tz=timezone.utc)
        elif isinstance(created_raw, (int, float)) and created_raw > 0:
            posted_at = datetime.fromtimestamp(created_raw, tz=timezone.utc)
        else:
            # headline items often have no timestamp — use "now" so they fall
            # into the recent-observation window for topic clustering.
            posted_at = datetime.now(timezone.utc)

        return from_scrape_dict(
            {
                "author_handle": handle,
                "content": content,
                "posted_at": posted_at,
                "likes": raw.get("fav_count", 0),
                "retweets": raw.get("retweet_count", 0),
                "replies": raw.get("reply_count", 0),
                "impressions": raw.get("view_count"),
                "has_image": True,           # we already required image_url above
                "raw_url": url,
                "image_url": image_url_str,
            },
            source=self.name,
            tier=self._tier_default,  # type: ignore[arg-type]
        )
