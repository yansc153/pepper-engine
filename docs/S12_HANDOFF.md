# S12 Deployment — Handoff

Owner: Subagent S12. Scope: Docker image, docker-compose, cron, entrypoint, VPS
bootstrap, cookie sync, container command dispatcher. Anything under `src/`,
`config/`, `voice/`, `templates/`, `secrets/` is owned by other subagents and
was deliberately left untouched.

---

## 1. Files delivered (this subagent only)

| Path | Purpose |
|---|---|
| `Dockerfile` | Base = `mcr.microsoft.com/playwright:v1.49-noble`. Installs Python 3.11 + cron + CJK fonts, pip-installs `requirements.txt`, copies app code, installs `crontab.txt` into `/etc/cron.d/pepperbot`. |
| `docker-compose.yml` | One service `pepperbot`. Bind-mounts `data/`, `logs/`, `tmp_images/` rw and `secrets/` ro. Loads `.env` + `secrets/discord.env`. 2 GB mem cap, 512m shm. |
| `crontab.txt` | 6 cron rows (observe / post / discord_poll / self_monitor / mine+review / weekly remine). Every row guarded by `flock`. |
| `scripts/entrypoint.sh` | PID 1. Exports a whitelist of env vars to `/etc/environment` so cron jobs can see them, runs `python -m src.migrations.runner` once, then `exec cron -f`. Alerts via `ALERT_WEBHOOK_URL` if migrations fail. |
| `scripts/run.sh` | Container-side dispatcher. Validates the command name, sources `/etc/environment`, then `exec python -m src.main <cmd>`. Falls back to a no-op placeholder if `src/main.py` is not yet implemented. |
| `scripts/vps_setup.sh` | Idempotent one-shot. Installs docker + compose plugin, clones (or pulls) the repo, creates runtime dirs, builds the image. Does NOT start the container — secrets must be uploaded first. |
| `scripts/cookie_sync.sh` | `rsync` wrapper that pushes local `secrets/` to `<user@host>:<repo>/secrets/` with `chmod 600`. |
| `.dockerignore` | Excludes `.git`, caches, `data/`, `logs/`, `tmp_*/`, **and `secrets/`** so cookies/API keys never get baked into the image. |
| `docs/S12_HANDOFF.md` | This file. |

Changes vs. the 创业板 image:

| 创业板 | content_2 |
|---|---|
| Base `python:3.12-slim` + manual Chromium libs | `mcr.microsoft.com/playwright:v1.49-noble` (bundled) |
| 2-row cron (`periodic2h` + `review`) | 6-row cron driven by `run.sh <command>` |
| `run_slot_vps.sh slotN` | `run.sh <observe\|post\|mine\|review\|remine\|discord_poll\|self_monitor>` |
| No flock | Every cron row wrapped in `flock -n /tmp/<cmd>.lock` |
| osascript alert (mac-only) | `curl POST $ALERT_WEBHOOK_URL` |
| Single `COPY . .` | Explicit `COPY src/ config/ ...` so `.dockerignore` is enforced surgically |
| No migrations step | Entrypoint runs `python -m src.migrations.runner` on every boot |

---

## 2. First-time dev → prod deployment

Assume a clean Ubuntu 22.04+ VPS reachable as `deploy@vps.example.com` and your
local repo at `~/Downloads/CC_testing/花椒的content_2`.

### 2.1 Local prep (one-time)

```bash
# 1. Make sure your local cookies/secrets exist
ls secrets/
# Expected: twitter_cookies.json  xueqiu_cookies.json  discord.env  ...

# 2. Sanity-check Docker assets locally
docker compose config           # validates yaml + env resolution
bash -n scripts/*.sh            # shell syntax check
```

### 2.2 VPS bootstrap

```bash
# Copy bootstrap script
scp scripts/vps_setup.sh deploy@vps:/tmp/

# Run it (needs sudo for docker install + /opt write)
ssh deploy@vps "sudo REPO_URL=git@github.com:<you>/pepperbot.git \
                INSTALL_DIR=/opt/pepperbot bash /tmp/vps_setup.sh"
```

What it does: installs Docker + compose plugin, clones the repo to
`/opt/pepperbot`, creates `secrets/ data/ logs/ tmp_images/`, builds the image.

### 2.3 Upload secrets (from your laptop)

```bash
./scripts/cookie_sync.sh deploy@vps:/opt/pepperbot
```

Then SSH in and fill out env files:

```bash
ssh deploy@vps
cd /opt/pepperbot
vim .env                    # MOONSHOT_API_KEY, ALERT_WEBHOOK_URL, ...
vim secrets/discord.env     # DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID
chmod 600 secrets/*.env secrets/*.json
```

### 2.4 First start

```bash
docker compose up -d
docker compose logs -f --tail=200
```

You should see, in order:
1. `[entrypoint ...] exporting env vars to /etc/environment`
2. `[entrypoint ...] running DB migrations`
3. `[entrypoint ...] starting cron in foreground`
4. Within an hour, an `observe` cron row firing.

### 2.5 Verify

```bash
# Inside the container
docker compose exec pepperbot bash -lc 'crontab -l'
docker compose exec pepperbot bash -lc 'ls -la /app/secrets /app/data'
docker compose exec pepperbot python -m src.main status   # once S11 lands main.py
tail -F logs/observe.log logs/post.log logs/selfmon.log
```

---

## 3. Daily updates (after the first deploy)

```bash
# From laptop: push code
git push

# On VPS
ssh deploy@vps
cd /opt/pepperbot
git pull --ff-only
docker compose build               # rebuild only if Dockerfile/requirements changed
docker compose up -d               # recreate container if image changed
docker compose logs -f --tail=200  # confirm migrations + cron started clean
```

If only `src/` Python changed and you copied it in via `COPY src/ /app/src/`,
you must rebuild the image (no live-mount of source code in prod — by design).

### 3.1 Cookie refresh (every ~30 days)

```bash
# Re-export cookies on your Mac, then:
./scripts/cookie_sync.sh deploy@vps:/opt/pepperbot
ssh deploy@vps "cd /opt/pepperbot && docker compose restart pepperbot"
```

The `secrets/` volume is read-only inside the container; restart re-reads
the JSON via Playwright `context.add_cookies()`.

### 3.2 Tail-and-diagnose cheatsheet

```bash
# All logs, follow mode
docker compose logs -f --tail=200

# Cron table actually loaded
docker compose exec pepperbot crontab -l

# Confirm env was exported for cron
docker compose exec pepperbot cat /etc/environment

# Run a command manually (same path cron uses)
docker compose exec pepperbot /app/scripts/run.sh observe

# Inspect DB
docker compose exec pepperbot sqlite3 /app/data/pepperbot.db '.tables'

# Migration history
docker compose exec pepperbot sqlite3 /app/data/pepperbot.db \
    'SELECT * FROM schema_migrations ORDER BY id;'
```

---

## 4. Rollback

The image is tagged `pepperbot:latest` only. To roll back:

```bash
# Pin to last known good git SHA
ssh deploy@vps
cd /opt/pepperbot
git fetch --all
git checkout <last_good_sha>

# Back up the DB before any schema downgrade
cp data/pepperbot.db data/pepperbot.db.bak.$(date +%Y%m%d-%H%M)

# Rebuild + restart
docker compose build
docker compose up -d
```

Migrations are forward-only by policy (UNIFIED_SPEC §11.6). If the rollback
crosses a destructive migration, restore the `.bak` DB from before the upgrade.

---

## 5. Validation results (this subagent)

Run on macOS dev box (no Docker daemon assumed). Build was not attempted in
this environment per stop-condition relaxation; remaining checks ran clean:

- `bash -n scripts/entrypoint.sh scripts/run.sh scripts/cookie_sync.sh scripts/vps_setup.sh` → PASS
- `crontab.txt` columns checked: 6 user-cron rows, all 7-field `m h dom mon dow user cmd`
- `.dockerignore` includes `secrets/`, `data/`, `logs/`, `.env`, `__pycache__/`
- `docker-compose.yml` mounts `./secrets:/app/secrets:ro` and `env_file` lists both `.env` and `secrets/discord.env`

`docker compose config` and `docker build` should be re-run on the first VPS
where Docker is available; both are expected to pass given the validations
above.

---

## 6. What the next subagent (S11) needs from us

- Entry point for every cron command: `python -m src.main <observe|post|mine|review|remine|discord_poll|self_monitor>`
- Migrations entry point: `python -m src.migrations.runner` (idempotent, must exit 0 when already at head)
- Env var contract: see `ENV_WHITELIST` in `scripts/entrypoint.sh` for the variables that will reach cron jobs
- Working dir inside the container is `/app`. Writable bind mounts: `/app/data`, `/app/logs`, `/app/tmp_images`. Read-only: `/app/secrets`.

Until `src/main.py` exists, cron rows succeed with a no-op placeholder log line
so the container does not crash-loop during phased build-out.
