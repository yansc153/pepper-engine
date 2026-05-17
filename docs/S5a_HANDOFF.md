# S5a Handoff — External Observers (xueqiu / futu / news_flash)

**Files**: `src/observers/{xueqiu_adapter,futu_adapter,news_flash_adapter,runner}.py` +
`tests/unit/observers/test_{xueqiu,futu,news_flash,runner}.py` (44 tests green) +
3 fixtures under `tests/fixtures/observations/`.

## Adapter surface (all implement `observers.base.SourceAdapter`)

```python
XueqiuAdapter()       # name=xueqiu      cookie_env_key=XUEQIU_COOKIE_FILE  rl=24/h  tier=2
FutuAdapter()         # name=futu        cookie_env_key=FUTU_COOKIE_FILE    rl=12/h  tier=2
NewsFlashAdapter()    # name=news_flash  cookie_env_key=""                  rl=30/h  tier=0
```

All three:
- `async fetch_latest(since: datetime) -> list[Observation]` — never raises;
  on error returns `[]`. Filters by `obs.posted_at > since` (exclusive).
- `async health_check() -> bool` — checks cookie file presence + basic
  connectivity. Returns False on any failure.
- Cookie files are Playwright-format JSON lists; loaded from
  `os.environ[cookie_env_key]`.

**Futu specifics** (§16.7): `_fetch_via_browser()` launches headless Playwright,
adds cookies, navigates, **clicks `text=推荐`** to force-refresh, then waits
`networkidle`. Tests monkeypatch `_fetch_via_browser` so unit tests never spin
Playwright.

## Runner — `src.observers.runner.run_once`

```python
import asyncio
from src.observers.runner import run_once

report = asyncio.run(run_once())     # uses config/sources.yaml + DEFAULT_LOOKBACK_HOURS=6
# RunReport(success_count, error_count, observations_inserted)
print(report.as_tuple())
```

Internals:
1. Loads `config/sources.yaml` via `src.config_loader.load_all_configs`.
2. Picks enabled adapters whose name is in `EXTERNAL_ADAPTER_NAMES =
   {"xueqiu","futu","news_flash"}`. The two `x_list_*` adapters belong to S5b.
3. `asyncio.gather` runs all `fetch_latest`s concurrently; failures are caught
   per-adapter and recorded in `source_health` (`consecutive_failures` increments
   on failure, resets to 0 on success; `last_error` truncated to 500 chars).
4. For each returned `Observation`: computes `placeholder_viral_score(obs)`
   (`likes*0.5 + retweets*1 + replies*27`) → flips `is_viral` if score ≥
   `viral_threshold` (default 500) → INSERT OR IGNORE into
   `reaction_observations` (dedupes via `raw_url UNIQUE`).

Test-friendly variant: `run_adapters(adapters, since, db_path, viral_threshold)`
accepts an explicit adapter list and DB path — used by `test_runner.py`.

## For S11 orchestrator

```python
from pathlib import Path
from src.observers.runner import run_once
report = await run_once(repo_root=Path(...), db_path=Path(".../pepperbot.db"))
if report.error_count:
    logger.warning("observers: %d adapters failed", report.error_count)
```

## For S6 (when viral_scorer lands)

Swap `placeholder_viral_score` with `from src.miner.viral_scorer import score`;
runner already isolates the call to one helper at the top of `runner.py`.

## Known constraints
- Python 3.14 + respx 0.21 don't route-match cleanly, so HTTP mocking is done
  by monkeypatching the private `_fetch_payload` method (xueqiu / news_flash)
  instead of respx.
- Futu DOM selectors (`[data-feed-id], article, .feed-item`) are best-effort;
  expect to revisit if futunn redesigns.
- `eastmoney_kuaixun` timestamps are treated as UTC (acceptable for tier-0
  facts since they never feed the learning corpus).
