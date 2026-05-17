#!/bin/bash
# One-shot VPS bootstrap. Run on a fresh Ubuntu 22.04+ box.
#   1) Install docker + compose plugin
#   2) Clone repo (skipped if already present)
#   3) Build image
# Cookies + secrets must be uploaded separately via scripts/cookie_sync.sh
# before the first `docker compose up`.
set -euo pipefail

REPO_URL="${REPO_URL:-}"
INSTALL_DIR="${INSTALL_DIR:-/opt/pepperbot}"

echo "=== PepperBot content_2 VPS setup ==="

# 1. Docker
if ! command -v docker >/dev/null 2>&1; then
    echo "[1/4] Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    if id -u "${SUDO_USER:-$USER}" >/dev/null 2>&1; then
        usermod -aG docker "${SUDO_USER:-$USER}" || true
    fi
else
    echo "[1/4] Docker already installed: $(docker --version)"
fi

# 2. Compose plugin
if ! docker compose version >/dev/null 2>&1; then
    echo "[2/4] Installing docker-compose-plugin..."
    apt-get update
    apt-get install -y docker-compose-plugin
else
    echo "[2/4] docker compose already present: $(docker compose version)"
fi

# 3. Clone or update repo
if [ ! -d "${INSTALL_DIR}/.git" ]; then
    if [ -z "${REPO_URL}" ]; then
        echo "ERROR: ${INSTALL_DIR} is empty and REPO_URL is not set." >&2
        echo "       Re-run with: REPO_URL=git@github.com:you/pepperbot.git $0" >&2
        exit 1
    fi
    echo "[3/4] Cloning ${REPO_URL} -> ${INSTALL_DIR}"
    git clone "${REPO_URL}" "${INSTALL_DIR}"
else
    echo "[3/4] Repo already cloned at ${INSTALL_DIR}, pulling latest..."
    git -C "${INSTALL_DIR}" pull --ff-only
fi

cd "${INSTALL_DIR}"

# Ensure runtime dirs exist on host (bind-mounted into container)
mkdir -p secrets data logs tmp_images

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "  Copied .env.example -> .env  (edit before starting)"
    else
        touch .env
    fi
fi

if [ ! -f secrets/discord.env ]; then
    echo "DISCORD_BOT_TOKEN=" > secrets/discord.env
    echo "DISCORD_CHANNEL_ID=" >> secrets/discord.env
    chmod 600 secrets/discord.env
    echo "  Created placeholder secrets/discord.env — fill in real values"
fi

# 4. Build image (does not start container)
echo "[4/4] Building image..."
docker compose build

cat <<'NEXT'

Setup complete. Next steps:
  1. From your laptop, rsync cookies:
       ./scripts/cookie_sync.sh user@<vps>:/opt/pepperbot
  2. Edit /opt/pepperbot/.env and /opt/pepperbot/secrets/discord.env
  3. Start the stack:
       cd /opt/pepperbot && docker compose up -d
  4. Tail logs:
       docker compose logs -f --tail=200
NEXT
