# S5b Handoff — Twitter Observers (x_list_finance / x_list_general / self_monitor)

**Files**: `src/observers/{x_list_finance_adapter,x_list_general_adapter,self_monitor_adapter}.py`
+ `tests/unit/observers/test_{...}.py` (8 + 5 + 10 = **23 tests green**).

## Public surface (all implement `observers.base.SourceAdapter`)

```python
XListFinanceAdapter(list_url=..., tier_default=1, max_posts_per_fetch=30)
    # name="x_list_finance"  cookie_env_key="TWITTER_COOKIE_FILE"  rl=12/h
    # DEFAULT_LIST_URL = "https://x.com/i/lists/2056032482127175889"

XListGeneralAdapter(list_url="", tier_default=3, max_posts_per_fetch=30, enabled=False)
    # name="x_list_general"  enabled iff list_url non-empty AND enabled=True
    # disabled → fetch_latest()→[], health_check()→True (deliberate suspension)

SelfMonitorAdapter(twitter_handle=None, cookie_file=None,
                   lookback_hours=48, max_posts_per_fetch=50)
    # name="self_monitor"  cookie_env_key="X_XIAOHAO_COOKIE_FILE"
    # twitter_handle ← arg OR env TWITTER_HANDLE
    # cookie_file    ← arg OR env OR "secrets/x_xiaohao_cookies.json"
```

## Shape

Both X-list adapters call `_fetch_via_twitter_bot()` (test seam) which does
`TwitterBot() / async with / ensure_logged_in() / scrape_list_by_url(...)`,
then `from_scrape_dict(raw, source=self.name, tier=self._tier_default)`,
filter `obs.posted_at > since`, swallow all exceptions → `[]`.

## self_monitor — extra side-effect API

```python
@dataclass(frozen=True, slots=True)
class ReconcileResult:
    scanned: int; bound: int; wild: int; errors: int

await adapter.reconcile(*, db_path=None, timeline_fetcher=None, now=None)
```

- `fetch_latest()` always returns `[]` (runner skips it; not a learning source).
- `reconcile()` is the real entry — call from `main.py` on cron `0 */6 * * *`.
- Logs into small account, scrapes `https://x.com/{TWITTER_HANDLE}` via
  `TwitterBot(cookie_file=<xiaohao>)`, then per tweet within last 48h:
  - SELECT id FROM drafts WHERE content=? AND tweet_url IS NULL ORDER BY
    generated_at DESC LIMIT 1 → patch `tweet_url`, `cross_referenced=1`,
    `status='published'`, `posted_at`.
  - Else INSERT OR IGNORE INTO `wild_posts(tweet_url, content, content_hash,
    posted_at)`. `content_hash = sha1(text)` matches `publisher._content_hash`.
- Always writes `source_health` row for `adapter_name='self_monitor'`.
- Raises `RuntimeError` only when `TWITTER_HANDLE` is unset; all other errors
  → counted in `ReconcileResult.errors`, never bubble.

## Spec deviation

Drafts schema (migration 003) has no `content_hash` column, so `_bind_draft`
matches on exact `content` text instead. Semantics are identical for a sha1
lookup. If S1 adds the column later, swap to `WHERE content_hash=?`.

## Test seams

- X-list adapters: monkeypatch `adapter._fetch_via_twitter_bot` to return
  list[dict] without spinning Playwright.
- self_monitor: pass `timeline_fetcher=async lambda: [...]` + `db_path=tmp.db`
  + `now=fixed_dt` to `reconcile()` for hermetic tests.

## Runner integration

`src/observers/runner.py` already excludes all three from its EXTERNAL set;
none of these are picked up by `run_once()`. x_list_* will join the runner
once the user enables them in `config/sources.yaml` and S6 wires them in.
