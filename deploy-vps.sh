#!/bin/bash
# Pepperbot one-shot VPS deploy — paste this whole block once.
set -euo pipefail

# 0. Docker install if missing.
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
fi
docker version | head -1

# 1. Repo into /opt/pepperbot.
mkdir -p /opt/pepperbot
cd /opt/pepperbot
if [ -d .git ]; then
    git fetch --all && git reset --hard origin/main
else
    git clone https://github.com/yansc153/pepper-engine.git .
fi

# 2. Runtime dirs (volumes, outlive the container).
mkdir -p secrets data logs tmp_images
chmod 700 secrets

# 3. Decode secrets blob — discord.env + 3 cookie JSON files.
base64 -d <<'__SECRETS_BLOB__' | tar -xz -C secrets/
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
__SECRETS_BLOB__
chmod 600 secrets/*
ls -la secrets/

# 4. Write .env with MOONSHOT_API_KEY already filled in.
cat > .env <<'__ENV__'
LLM_BACKEND=moonshot
MOONSHOT_MODEL=kimi-k2-0905-preview
MOONSHOT_API_KEY=sk-KAOpQH5mUQ44QkwARfR465alxAOz8V2yBoeSJcsulrG2ogHq
MOONSHOT_BASE_URL=https://api.moonshot.cn/v1

BROWSER_BACKEND=playwright_headless
PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

TWITTER_HANDLE=off_tehtarget
TWITTER_COOKIE_FILE=/app/secrets/x_xiaohao_cookies.json
TWITTER_XIAOHAO_COOKIE_FILE=/app/secrets/x_xiaohao_cookies.json
XUEQIU_COOKIE_FILE=/app/secrets/xueqiu_cookies.json
FUTU_COOKIE_FILE=/app/secrets/futu_cookies.json

DB_PATH=/app/data/pepperbot.db
LOG_DIR=/app/logs/

DRY_RUN=0
TZ=Asia/Shanghai
PYTHONUNBUFFERED=1
DISCORD_APPROVAL_MODE=manual
__ENV__
chmod 600 .env

# 5. Build the image. First build ~3-5 min (Playwright base ~500MB).
echo "==> building image (this takes a few minutes the first time)"
docker compose build

# 6. Apply migrations.
echo "==> applying DB migrations"
docker compose run --rm pepperbot python -m src.migrations.runner

# 7. Smoke test — must print 7/7 modules ok + db_ok=true.
echo "==> smoke test"
docker compose run --rm pepperbot python -m src.main test

# 8. Bring up cron in the background.
echo "==> starting cron"
docker compose up -d

echo ""
echo "================================================================"
echo "DEPLOY DONE."
echo "  docker compose ps"
echo "  docker compose logs -f"
echo "  tail -f /opt/pepperbot/logs/pepperbot-*-\$(date +%F).log"
echo "  docker compose exec pepperbot python -m src.main observe"
echo "  docker compose down                # stop"
echo "================================================================"
