# S7 HANDOFF — Publisher & Twitter Bot

**Files**: `src/twitter_bot.py` (forked + headless), `src/publisher.py` (thin shell),
`src/extract_ogimage.py` (helper). Tests: `tests/unit/test_twitter_bot.py` (13),
`tests/unit/test_publisher.py` (16). All 29 green.

## Public surface

```python
# src/publisher.py
@dataclass
class PostResult:
    success: bool
    tweet_url: str | None
    error: str | None

async def post_tweet(text, image_path=None, *, dry_run=None, bot=None) -> PostResult
async def get_post_metrics(tweet_url, *, bot=None) -> dict[str, int]

# src/twitter_bot.py
class TwitterBot:
    def __init__(cookie_file=None, headless=None, user_agent=None)
    async def start() / stop() / __aenter__ / __aexit__
    async def ensure_logged_in()                       # raises NotLoggedInError
    async def post_tweet(text, image_path=None)        -> dict
    async def reply_to_tweet(tweet_url, text, image_path=None) -> dict
    async def scrape_list_by_url(list_url, max_posts=30) -> list[dict]
    async def get_post_metrics(tweet_url)              -> dict[str, int]

class NotLoggedInError(RuntimeError)
```

`TwitterBot.scrape_list_by_url` returns dicts shaped for
`observers.base.from_scrape_dict` (handle / text / created_at / likes /
retweets / replies / views / has_media / url).

## Error semantics

| condition                              | publisher.PostResult                              |
|----------------------------------------|---------------------------------------------------|
| `DRY_RUN=1` env OR `dry_run=True`      | `success=True, tweet_url=None, error=None`        |
| same content_hash posted in last 24h   | `success=False, error="duplicate within 24h"`     |
| cookie file missing/expired            | `success=False, error="not_logged_in: ..."`       |
| upload/compose/toast failure           | `success=False, error=<bot.post_tweet error>`     |
| main ok, URL-reply fails               | `success=True` (reply failure logged, not bubbled)|

`twitter_bot.post_tweet` returns a dict (never raises for normal failures).
`start()` raises `NotLoggedInError` only when the cookie file is missing
or unreadable. `ensure_logged_in()` raises on `/login` redirect or missing
compose textarea.

## URL-as-reply rule (§10)

`split_trailing_url(text)` strips the LAST `https?://...` out of the body
and returns `(main_text, url_to_reply)`. `post_tweet` then posts main +
calls `reply_to_tweet(main_tweet_url, url)`. If body is only a URL, we
keep it inline rather than producing an empty main post.

## Callers

**S13 (Discord publisher_callback)** — primary caller:
```python
from publisher import post_tweet
result = await post_tweet(draft.content, draft.image_path)
if result.success and result.tweet_url:
    db.execute("UPDATE drafts SET status='published', tweet_url=?, "
               "posted_at=CURRENT_TIMESTAMP WHERE id=?", (result.tweet_url, did))
```

**S5b (x_list_adapter)** — scraping only, skip publisher:
```python
from twitter_bot import TwitterBot
async with TwitterBot() as bot:
    await bot.ensure_logged_in()
    raw = await bot.scrape_list_by_url(self.list_url, max_posts=30)
    return [from_scrape_dict(r, source=self.name, tier=1) for r in raw]
```

**S10 (reviewer)** — pulls 24/48/72h metrics:
```python
from publisher import get_post_metrics
m = await get_post_metrics(tweet_url)   # {"likes":..,"retweets":..,"replies":..,"impressions":..}
```

## Environment

- `TWITTER_COOKIE_FILE` (default `/app/secrets/x_dahao_cookies.json`)
- `BROWSER_HEADLESS` (`1` default; `0` for local debugging)
- `DRY_RUN` (`1` to short-circuit all posts)

## Important

**Main account cookie (x_dahao_cookies.json) NOT yet provided.** Until
the user drops it into `secrets/`, every real post will return
`error="not_logged_in: Twitter cookie file missing: ..."`. The small
account cookie `secrets/x_xiaohao_cookies.json` exists already (S5b
self_monitor adapter uses it directly, not via publisher).
