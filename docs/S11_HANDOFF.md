# S11 Orchestrator — HANDOFF

**Files**: `src/main.py`, `src/__main__.py`, `tests/unit/test_main.py` (18 tests green).

Single entry point for every cron row (`scripts/run.sh <cmd>` → `python -m src.main <cmd>`).

## 8 commands

| Command | Wires to | Notes |
|---|---|---|
| `observe` | `observers.runner.run_once()` → `selector.expire_old_candidates()` → `selector.score_topics()` | Runs 4 external adapters (xueqiu/futu/news_flash, x_list_* if enabled). Self-monitor NOT here. |
| `post` | `selector.pick_top_topic()` → `writer.write_draft()` → `discord.bot.push_draft_to_discord()` | Manual mode (§16.13): **never calls publisher**. Test asserts this. Skips cleanly on no topic / rejected draft / writer exception. |
| `discord_poll` | `discord.bot.poll_reactions()` | ✅/❌/🔄 sweep. Exits 0 if owner unset (logged inside bot). |
| `self_monitor` | `observers.self_monitor_adapter.SelfMonitorAdapter().reconcile()` | TWITTER_HANDLE unset → skipped, exit 0. |
| `mine` | `miner.full_distill(since=today_00_UTC)` → `miner.weave_nightly(new_ids)` | Daily. |
| `review` | `reviewer.review_and_update_weights()` | Full S10 reviewer. Falls back to no-op if module missing or entry symbol absent. |
| `remine` | `miner.weave_full()` | Weekly Sunday rebuild + decay + prune. |
| `test` | imports every module + `SELECT 1` on DB | Dev smoke. Always sets `DRY_RUN=1`. Not in cron. |

Cron rows in `crontab.txt` match exactly (7 active rows for 6 names — `mine`+`review` share one row chained). Verified.

## Behavioural contracts

- **Module imports are deferred** into each command function so `--dry-run` can set `DRY_RUN=1` before any module reads env at import time.
- **One JSON line per run** on stdout (cmd / duration_s / success / extras). Cron rows redirect to `logs/<cmd>.log`; `grep -h '"cmd":' logs/*.log | jq` powers dashboards.
- **Exit code policy**: 0 = ran cleanly (incl. "nothing to do" skips); 1 = uncaught exception; 130 = SIGINT; 2 = argparse misuse.
- **Top-level exception net** catches anything from downstream; the JSON summary always has `success`/`error` so log monitors can alert without parsing tracebacks.

## Diagnostic command cheatsheet (operator)

```bash
# Inside container (or with PYTHONPATH set + cwd = repo root locally)
python -m src.main --help                       # list 8 commands
python -m src.main test --dry-run               # smoke: imports + DB ping, no I/O

# Reproduce a cron row interactively
python -m src.main observe                      # full external fetch
python -m src.main post                         # one draft → Discord (no publish in manual mode)
python -m src.main discord_poll                 # sweep reactions once
python -m src.main self_monitor                 # cross-reference main-account timeline
python -m src.main mine                         # distill + weave nightly
python -m src.main review                       # nightly metrics + weight update
python -m src.main remine                       # weekly full re-weave

# Dry-run anything (sets DRY_RUN=1 → publisher/discord short-circuit)
python -m src.main post --dry-run

# Filter the structured summaries out of a log file
grep -h '^{' logs/post.log | jq -c '{ts: .duration_s, ok: .success, id: .draft_id}'
```

## Things to watch

- `review` works against the live reviewer; only the failsafe is the no-op fallback.
- `selector` is imported as the bare top-level name (`from selector import ...`) because `src/` is on sys.path via the project's `conftest.py` and `src/__init__.py`'s side-effect import. The miner / writer / discord modules use the `src.` prefix. Both forms work because `python -m src.main` adds the repo root to sys.path.
- Tests stub downstream modules via `sys.modules` (`monkeypatch.setitem`). Adding new commands? Mirror the `_install_module` helper to keep tests hermetic.
